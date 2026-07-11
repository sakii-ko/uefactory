from __future__ import annotations

import fcntl
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import stat
import statistics
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

from uefactory.render.passes import STENCIL_NORMALIZED_ATOL, read_object_mask_exr

SHOWCASE_SCHEMA_VERSION = 1
SHOWCASE_FPS = 24
SHOWCASE_CRF = 16
MIN_SHOWCASE_FRAMES = 72
MIN_SHOWCASE_SHORT_EDGE = 1080
MAX_SHOWCASE_FRAMES = 720
MAX_SHOWCASE_PIXELS = 40_000_000
MAX_SOURCE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SHOWCASE_FRAME_BYTES = 256 * 1024 * 1024
MAX_SHOWCASE_TOTAL_FRAME_BYTES = 32 * 1024 * 1024 * 1024
MIN_FOREGROUND_AREA_RATIO = 0.10
MIN_FOREGROUND_BBOX_AREA_RATIO = 0.18
MIN_FOREGROUND_MARGIN_RATIO = 0.03

_STAGE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_ASSET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_FRAME_RE = re.compile(r"frame_([0-9]{4,})\.png\Z")


class ShowcaseError(RuntimeError):
    """Raised when a showcase cannot be proven and published safely."""


@dataclass(frozen=True)
class ShowcaseResult:
    stage: str
    asset_id: str
    run_dir: Path
    video_path: Path
    manifest_path: Path
    frame_count: int
    resolution: tuple[int, int]
    duration_sec: float
    video_sha256: str


@dataclass(frozen=True)
class _FrameCohort:
    paths: tuple[Path, ...]
    records: tuple[dict[str, Any], ...]
    resolution: tuple[int, int]
    total_size: int
    aggregate_sha256: str


@dataclass(frozen=True)
class _MaskCohort:
    paths: tuple[Path, ...]
    records: tuple[dict[str, Any], ...]
    total_size: int
    aggregate_sha256: str
    summary: dict[str, Any]


@dataclass(frozen=True)
class _MaskMetrics:
    pixel_sha256: str
    foreground_pixels: int
    foreground_area_ratio: float
    x_min: int
    y_min: int
    x_max: int
    y_max: int
    bbox_width_ratio: float
    bbox_height_ratio: float
    bbox_area_ratio: float
    minimum_margin_ratio: float


def create_showcase(
    *,
    project_root: Path,
    render_run_dir: Path,
    stage: str,
    clock: Callable[[], datetime] | None = None,
) -> ShowcaseResult:
    """Create and atomically publish a verified 1080p-or-better stage video."""

    try:
        root = project_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ShowcaseError(f"Cannot resolve project root {project_root}: {exc}") from exc
    _require_directory(root, "project root")
    _validate_stage(stage)
    source_run = _resolve_source_run(root, render_run_dir)
    source_manifest_path = source_run / "manifest.json"
    source_manifest_bytes = _read_regular_file(
        source_manifest_path,
        root=source_run,
        label="render manifest",
        maximum_bytes=MAX_SOURCE_MANIFEST_BYTES,
    )
    source_manifest = _parse_json_object(source_manifest_bytes, source_manifest_path)
    asset_id, asset_payload = _validate_source_manifest(source_manifest)
    rights = _rights_payload(asset_payload)
    frame_cohort = _validate_frame_cohort(
        source_run=source_run,
        manifest=source_manifest,
    )
    mask_cohort = _validate_object_mask_cohort(
        source_run=source_run,
        manifest=source_manifest,
        expected_frames=len(frame_cohort.paths),
        expected_resolution=frame_cohort.resolution,
    )

    ffmpeg = _required_executable("ffmpeg")
    ffprobe = _required_executable("ffprobe")
    git_commit, git_dirty = _git_evidence(root)
    now = _utc_now(clock)
    created_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    output_stamp = now.strftime("%Y%m%dT%H%M%SZ")
    asset_slug = _asset_slug(asset_id)
    stage_root = root / "out" / "showcases" / stage
    _mkdir_tree_without_symlinks(stage_root, root=root)
    final_dir = stage_root / f"{output_stamp}_{asset_slug}"
    staging_dir = stage_root / f".{output_stamp}_{asset_slug}.{uuid.uuid4().hex}.tmp"
    try:
        staging_dir.mkdir(mode=0o755)
    except OSError as exc:
        raise ShowcaseError(
            f"Cannot create showcase staging directory {staging_dir}: {exc}"
        ) from exc
    video_path = staging_dir / "showcase.mp4"
    manifest_path = staging_dir / "manifest.json"
    archived_source_manifest_path = staging_dir / "source_render_manifest.json"

    try:
        _write_bytes_durable(archived_source_manifest_path, source_manifest_bytes)
        command = _encode_showcase(
            ffmpeg=ffmpeg,
            frames=frame_cohort,
            output_path=video_path,
        )
        _require_source_stable(
            source_run=source_run,
            source_manifest_path=source_manifest_path,
            source_manifest_bytes=source_manifest_bytes,
            frames=frame_cohort,
            masks=mask_cohort,
        )
        _fsync_regular_file(video_path, root=staging_dir, label="showcase video")
        video_evidence = _probe_showcase(
            ffprobe=ffprobe,
            video_path=video_path,
            expected_resolution=frame_cohort.resolution,
            expected_frames=len(frame_cohort.paths),
        )
        _require_faststart(video_path)
        video_evidence.update(
            {
                "path": "showcase.mp4",
                "sha256": _file_sha256(video_path),
                "encoding": {
                    "codec": "libx264",
                    "crf": SHOWCASE_CRF,
                    "preset": "slow",
                    "pixel_format": "yuv420p",
                    "movflags": "+faststart",
                    "audio": False,
                    "command_sha256": _canonical_sha256(command),
                },
            }
        )
        manifest = {
            "schema_version": SHOWCASE_SCHEMA_VERSION,
            "status": "success",
            "stage": stage,
            "created_at": created_at,
            "source": {
                "run_dir": source_run.relative_to(root).as_posix(),
                "manifest_path": source_manifest_path.relative_to(root).as_posix(),
                "archived_manifest_path": archived_source_manifest_path.name,
                "manifest_sha256": hashlib.sha256(source_manifest_bytes).hexdigest(),
                "manifest_size": len(source_manifest_bytes),
                "render_schema_version": source_manifest["schema_version"],
                "render_kind": source_manifest["render_kind"],
                "render_status": source_manifest["status"],
            },
            "asset": _showcase_asset_payload(asset_id, asset_payload),
            "license": rights["license"],
            "attribution": rights["attribution"],
            "rights_provenance": rights["provenance"],
            "frames": {
                "root": "beauty_lit",
                "count": len(frame_cohort.paths),
                "resolution": list(frame_cohort.resolution),
                "total_size": frame_cohort.total_size,
                "aggregate_sha256": frame_cohort.aggregate_sha256,
                "items": list(frame_cohort.records),
                "object_mask": {
                    "root": "object_mask",
                    "count": len(mask_cohort.paths),
                    "total_size": mask_cohort.total_size,
                    "aggregate_sha256": mask_cohort.aggregate_sha256,
                    "policy": {
                        "minimum_foreground_area_ratio": MIN_FOREGROUND_AREA_RATIO,
                        "minimum_bbox_area_ratio": MIN_FOREGROUND_BBOX_AREA_RATIO,
                        "minimum_margin_ratio": MIN_FOREGROUND_MARGIN_RATIO,
                        "background_stencil_atol": STENCIL_NORMALIZED_ATOL,
                    },
                    "summary": mask_cohort.summary,
                    "items": list(mask_cohort.records),
                },
            },
            "video": video_evidence,
            "git_commit": git_commit,
            "git_dirty": git_dirty,
        }
        _write_json_durable(manifest_path, manifest)
        if (
            _parse_json_object(
                _read_regular_file(
                    manifest_path,
                    root=staging_dir,
                    label="showcase manifest",
                    maximum_bytes=MAX_SOURCE_MANIFEST_BYTES,
                ),
                manifest_path,
            )
            != manifest
        ):
            raise ShowcaseError("Showcase manifest did not round-trip exactly")
        _fsync_directory(staging_dir)
        _publish_directory(staging_dir=staging_dir, final_dir=final_dir)
    except BaseException:
        _remove_staging_directory(staging_dir)
        raise

    final_video = final_dir / video_path.name
    final_manifest = final_dir / manifest_path.name
    return ShowcaseResult(
        stage=stage,
        asset_id=asset_id,
        run_dir=final_dir,
        video_path=final_video,
        manifest_path=final_manifest,
        frame_count=len(frame_cohort.paths),
        resolution=frame_cohort.resolution,
        duration_sec=float(video_evidence["duration_sec"]),
        video_sha256=str(video_evidence["sha256"]),
    )


def _resolve_source_run(project_root: Path, value: Path) -> Path:
    raw = value.expanduser()
    candidate = raw if raw.is_absolute() else project_root / raw
    if ".." in candidate.parts:
        raise ShowcaseError(f"Render run path may not contain '..': {value}")
    candidate = Path(os.path.abspath(candidate))
    output_root = project_root / "out"
    try:
        candidate.relative_to(output_root)
    except ValueError:
        raise ShowcaseError(f"Render run must be inside {output_root}: {value}") from None
    _require_no_symlink_path(candidate, root=project_root)
    _require_directory(candidate, "render run")
    if candidate.resolve(strict=True) != candidate:
        raise ShowcaseError(f"Render run path is not canonical: {value}")
    return candidate


def _validate_source_manifest(manifest: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    if manifest.get("status") != "ok":
        raise ShowcaseError("Render manifest status must be 'ok'")
    if manifest.get("render_kind") != "job":
        raise ShowcaseError("Render manifest render_kind must be 'job'")
    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ShowcaseError("Render manifest schema_version must be an integer")
    if schema_version != 3:
        raise ShowcaseError("Render manifest schema_version must be 3")
    asset_id = manifest.get("asset_id")
    if not isinstance(asset_id, str) or _ASSET_ID_RE.fullmatch(asset_id) is None:
        raise ShowcaseError("Render manifest asset_id is invalid")
    asset = manifest.get("asset")
    if not isinstance(asset, dict):
        raise ShowcaseError("Render manifest asset must be an object")
    if asset.get("asset_id") != asset_id:
        raise ShowcaseError("Render manifest asset identity does not match asset_id")
    if asset.get("kind") not in {"catalog", "scene"}:
        raise ShowcaseError("Render manifest asset kind is unsupported")
    if asset.get("kind") == "scene" and asset.get("export") is not True:
        raise ShowcaseError("Scene showcase source must explicitly permit export")
    cleanup = manifest.get("asset_cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("status") != "ok":
        raise ShowcaseError("Render manifest does not prove successful host cleanup")
    return asset_id, asset


def _validate_frame_cohort(
    *,
    source_run: Path,
    manifest: Mapping[str, Any],
) -> _FrameCohort:
    frame_paths = manifest.get("frame_paths")
    if not isinstance(frame_paths, dict):
        raise ShowcaseError("Render manifest frame_paths must be an object")
    beauty_paths = frame_paths.get("beauty_lit")
    if (
        not isinstance(beauty_paths, list)
        or not MIN_SHOWCASE_FRAMES <= len(beauty_paths) <= MAX_SHOWCASE_FRAMES
    ):
        raise ShowcaseError(f"Showcase requires at least {MIN_SHOWCASE_FRAMES} beauty_lit frames")
    pass_payloads = manifest.get("passes")
    beauty_validation = pass_payloads.get("beauty_lit") if isinstance(pass_payloads, dict) else None
    if not isinstance(beauty_validation, dict):
        raise ShowcaseError("Render manifest lacks beauty_lit validation evidence")
    if beauty_validation.get("frame_count") != len(beauty_paths):
        raise ShowcaseError("beauty_lit validation frame count does not match frame_paths")
    declared_resolution = _resolution(beauty_validation.get("resolution"), "validation")
    if beauty_validation.get("format") != {
        "extension": ".png",
        "bit_depth": 8,
        "pixel_type": "uint8",
        "channels": 3,
    }:
        raise ShowcaseError("beauty_lit validation must prove canonical 8-bit RGB PNG")
    if manifest.get("frames_expected") != len(beauty_paths):
        raise ShowcaseError("Render manifest frames_expected does not match beauty_lit frames")
    frames_found = manifest.get("frames_found")
    if not isinstance(frames_found, dict) or frames_found.get("beauty_lit") != len(beauty_paths):
        raise ShowcaseError("Render manifest frames_found does not match beauty_lit frames")
    camera = manifest.get("camera")
    if (
        not isinstance(camera, dict)
        or camera.get("rig") != "orbit"
        or camera.get("views") != len(beauty_paths)
        or camera.get("resolution") != list(declared_resolution)
    ):
        raise ShowcaseError("Render manifest camera does not prove the showcase orbit cohort")
    frame_luma = manifest.get("frame_luma")
    if not isinstance(frame_luma, list) or len(frame_luma) != len(beauty_paths):
        raise ShowcaseError("Render manifest frame_luma evidence is incomplete")
    for value in frame_luma:
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 255.0
        ):
            raise ShowcaseError("Render manifest frame_luma contains an invalid value")
    validation_frames = beauty_validation.get("frames")
    if not isinstance(validation_frames, list) or len(validation_frames) != len(beauty_paths):
        raise ShowcaseError("beauty_lit validation frame evidence is incomplete")

    paths: list[Path] = []
    records: list[dict[str, Any]] = []
    total_size = 0
    unique_pixel_hashes: set[str] = set()
    for index, (raw_path, raw_validation) in enumerate(
        zip(beauty_paths, validation_frames, strict=True)
    ):
        expected_relative = f"beauty_lit/frame_{index:04d}.png"
        if raw_path != expected_relative:
            raise ShowcaseError(
                "beauty_lit frames must be a continuous canonical sequence; "
                f"expected {expected_relative!r}, got {raw_path!r}"
            )
        relative = _safe_relative_path(raw_path, "beauty_lit frame")
        if _FRAME_RE.fullmatch(relative.name) is None:
            raise ShowcaseError(f"Invalid beauty_lit frame name: {raw_path!r}")
        path = source_run.joinpath(*relative.parts)
        file_bytes = _read_regular_file(
            path,
            root=source_run,
            label="beauty_lit frame",
            maximum_bytes=MAX_SHOWCASE_FRAME_BYTES,
        )
        try:
            with Image.open(io.BytesIO(file_bytes)) as image:
                if image.format != "PNG" or image.mode != "RGB":
                    raise ShowcaseError(f"Showcase frame must be RGB PNG: {raw_path}")
                actual_resolution = (image.width, image.height)
                if image.width * image.height > MAX_SHOWCASE_PIXELS:
                    raise ShowcaseError(
                        f"Showcase frame exceeds {MAX_SHOWCASE_PIXELS} pixels: {raw_path}"
                    )
                image.load()
                pixel_sha256 = hashlib.sha256(image.tobytes()).hexdigest()
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise ShowcaseError(f"Cannot decode showcase frame {raw_path}: {exc}") from exc
        if actual_resolution != declared_resolution:
            raise ShowcaseError(
                f"Showcase frame resolution changed: {raw_path}: "
                f"{actual_resolution} != {declared_resolution}"
            )
        if min(actual_resolution) < MIN_SHOWCASE_SHORT_EDGE:
            raise ShowcaseError(
                f"Showcase short edge must be at least {MIN_SHOWCASE_SHORT_EDGE}: "
                f"{actual_resolution}"
            )
        if actual_resolution[0] % 2 or actual_resolution[1] % 2:
            raise ShowcaseError(
                f"Showcase resolution must be even for yuv420p: {actual_resolution}"
            )
        if not isinstance(raw_validation, dict):
            raise ShowcaseError(f"Invalid beauty_lit validation item at index {index}")
        if raw_validation.get("frame") != relative.name:
            raise ShowcaseError(f"beauty_lit validation frame identity mismatch at index {index}")
        declared_pixel_sha256 = raw_validation.get("pixel_sha256")
        if (
            not isinstance(declared_pixel_sha256, str)
            or _SHA256_RE.fullmatch(declared_pixel_sha256) is None
            or declared_pixel_sha256 != pixel_sha256
        ):
            raise ShowcaseError(f"beauty_lit decoded pixel hash mismatch: {raw_path}")
        size = len(file_bytes)
        total_size += size
        if total_size > MAX_SHOWCASE_TOTAL_FRAME_BYTES:
            raise ShowcaseError(
                f"Showcase beauty_lit cohort exceeds {MAX_SHOWCASE_TOTAL_FRAME_BYTES} bytes"
            )
        unique_pixel_hashes.add(pixel_sha256)
        paths.append(path)
        records.append(
            {
                "index": index,
                "path": raw_path,
                "size": size,
                "sha256": hashlib.sha256(file_bytes).hexdigest(),
                "pixel_sha256": pixel_sha256,
            }
        )
    minimum_unique_frames = min(8, len(paths))
    if len(unique_pixel_hashes) < minimum_unique_frames:
        raise ShowcaseError(
            "Showcase orbit lacks temporal diversity: "
            f"{len(unique_pixel_hashes)} unique frames, expected at least {minimum_unique_frames}"
        )
    aggregate_sha256 = _canonical_sha256(records)
    return _FrameCohort(
        paths=tuple(paths),
        records=tuple(records),
        resolution=declared_resolution,
        total_size=total_size,
        aggregate_sha256=aggregate_sha256,
    )


def _validate_object_mask_cohort(
    *,
    source_run: Path,
    manifest: Mapping[str, Any],
    expected_frames: int,
    expected_resolution: tuple[int, int],
) -> _MaskCohort:
    frame_paths = manifest.get("frame_paths")
    mask_paths = frame_paths.get("object_mask") if isinstance(frame_paths, dict) else None
    if not isinstance(mask_paths, list) or len(mask_paths) != expected_frames:
        raise ShowcaseError("Showcase requires one object_mask frame for every beauty_lit frame")
    frames_found = manifest.get("frames_found")
    if not isinstance(frames_found, dict) or frames_found.get("object_mask") != expected_frames:
        raise ShowcaseError("Render manifest frames_found does not match object_mask frames")
    pass_payloads = manifest.get("passes")
    validation = pass_payloads.get("object_mask") if isinstance(pass_payloads, dict) else None
    if not isinstance(validation, dict):
        raise ShowcaseError("Render manifest lacks object_mask validation evidence")
    if validation.get("frame_count") != expected_frames:
        raise ShowcaseError("object_mask validation frame count does not match frame_paths")
    if validation.get("resolution") != list(expected_resolution):
        raise ShowcaseError("object_mask validation resolution does not match beauty_lit")
    if validation.get("format") != {
        "extension": ".exr",
        "bit_depth": 16,
        "pixel_type": "float16",
        "channels": 4,
    }:
        raise ShowcaseError("object_mask validation must prove canonical half-float RGBA EXR")
    validation_frames = validation.get("frames")
    if not isinstance(validation_frames, list) or len(validation_frames) != expected_frames:
        raise ShowcaseError("object_mask validation frame evidence is incomplete")

    paths: list[Path] = []
    records: list[dict[str, Any]] = []
    total_size = 0
    # Real orbit masks are usually unique; retaining RGBA arrays here would scale to gigabytes.
    decoded_cache: dict[str, _MaskMetrics] = {}
    for index, (raw_path, raw_validation) in enumerate(
        zip(mask_paths, validation_frames, strict=True)
    ):
        expected_relative = f"object_mask/frame_{index:04d}.exr"
        if raw_path != expected_relative:
            raise ShowcaseError(
                "object_mask frames must be a continuous canonical sequence; "
                f"expected {expected_relative!r}, got {raw_path!r}"
            )
        relative = _safe_relative_path(raw_path, "object_mask frame")
        path = source_run.joinpath(*relative.parts)
        file_bytes = _read_regular_file(
            path,
            root=source_run,
            label="object_mask frame",
            maximum_bytes=MAX_SHOWCASE_FRAME_BYTES,
        )
        size = len(file_bytes)
        total_size += size
        if total_size > MAX_SHOWCASE_TOTAL_FRAME_BYTES:
            raise ShowcaseError(
                f"Showcase object_mask cohort exceeds {MAX_SHOWCASE_TOTAL_FRAME_BYTES} bytes"
            )
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()
        metrics = decoded_cache.get(file_sha256)
        if metrics is None:
            metrics = _inspect_object_mask(
                path=path,
                display_path=raw_path,
                expected_resolution=expected_resolution,
            )
            decoded_cache[file_sha256] = metrics
        if not isinstance(raw_validation, dict) or raw_validation.get("frame") != relative.name:
            raise ShowcaseError(f"object_mask validation frame identity mismatch at index {index}")
        if raw_validation.get("pixel_sha256") != metrics.pixel_sha256:
            raise ShowcaseError(f"object_mask decoded pixel hash mismatch: {raw_path}")
        if metrics.foreground_area_ratio < MIN_FOREGROUND_AREA_RATIO:
            raise ShowcaseError(
                f"Showcase subject is too small in {raw_path}: "
                f"foreground={metrics.foreground_area_ratio:.6f}, "
                f"required={MIN_FOREGROUND_AREA_RATIO:.6f}"
            )
        if metrics.bbox_area_ratio < MIN_FOREGROUND_BBOX_AREA_RATIO:
            raise ShowcaseError(
                f"Showcase subject bbox is too small in {raw_path}: "
                f"bbox={metrics.bbox_area_ratio:.6f}, "
                f"required={MIN_FOREGROUND_BBOX_AREA_RATIO:.6f}"
            )
        if metrics.minimum_margin_ratio < MIN_FOREGROUND_MARGIN_RATIO:
            raise ShowcaseError(
                f"Showcase subject violates frame margin in {raw_path}: "
                f"margin={metrics.minimum_margin_ratio:.6f}, "
                f"required={MIN_FOREGROUND_MARGIN_RATIO:.6f}"
            )
        paths.append(path)
        records.append(
            {
                "index": index,
                "path": raw_path,
                "size": size,
                "sha256": file_sha256,
                "pixel_sha256": metrics.pixel_sha256,
                "foreground_pixels": metrics.foreground_pixels,
                "foreground_area_ratio": round(metrics.foreground_area_ratio, 9),
                "bbox": {
                    "x_min": metrics.x_min,
                    "y_min": metrics.y_min,
                    "x_max": metrics.x_max,
                    "y_max": metrics.y_max,
                    "width_ratio": round(metrics.bbox_width_ratio, 9),
                    "height_ratio": round(metrics.bbox_height_ratio, 9),
                    "area_ratio": round(metrics.bbox_area_ratio, 9),
                    "minimum_margin_ratio": round(metrics.minimum_margin_ratio, 9),
                },
            }
        )
    foreground_ratios = [float(item["foreground_area_ratio"]) for item in records]
    bbox_ratios = [float(item["bbox"]["area_ratio"]) for item in records]
    margin_ratios = [float(item["bbox"]["minimum_margin_ratio"]) for item in records]
    summary = {
        "foreground_area_ratio": _ratio_summary(foreground_ratios),
        "bbox_area_ratio": _ratio_summary(bbox_ratios),
        "minimum_margin_ratio": _ratio_summary(margin_ratios),
    }
    return _MaskCohort(
        paths=tuple(paths),
        records=tuple(records),
        total_size=total_size,
        aggregate_sha256=_canonical_sha256(records),
        summary=summary,
    )


def _inspect_object_mask(
    *,
    path: Path,
    display_path: str,
    expected_resolution: tuple[int, int],
) -> _MaskMetrics:
    import numpy as np

    try:
        pixels, resolution = read_object_mask_exr(path)
    except RuntimeError as exc:
        raise ShowcaseError(f"Cannot decode showcase object mask {display_path}: {exc}") from exc
    if resolution != expected_resolution:
        raise ShowcaseError(
            f"object_mask resolution changed: {display_path}: {resolution} != {expected_resolution}"
        )
    pixel_sha256 = hashlib.sha256(np.ascontiguousarray(pixels).tobytes()).hexdigest()
    stencil_vectors = np.asarray(pixels, dtype=np.float32)
    if not np.all(np.isfinite(stencil_vectors)):
        raise ShowcaseError(f"object_mask contains non-finite pixels: {display_path}")
    channel_spread = np.max(stencil_vectors[:, :, :3], axis=2) - np.min(
        stencil_vectors[:, :, :3], axis=2
    )
    if float(np.max(channel_spread)) > 1e-4:
        raise ShowcaseError(f"object_mask must contain scalar stencil vectors: {display_path}")
    stencil = stencil_vectors[:, :, 0]
    foreground = ~np.isclose(
        stencil,
        0.0,
        rtol=0.0,
        atol=STENCIL_NORMALIZED_ATOL,
    )
    coordinates = np.argwhere(foreground)
    if coordinates.size == 0:
        raise ShowcaseError(f"object_mask contains no foreground: {display_path}")
    height, width = foreground.shape
    y_min, x_min = (int(value) for value in coordinates.min(axis=0))
    y_max, x_max = (int(value) for value in coordinates.max(axis=0))
    foreground_pixels = int(np.count_nonzero(foreground))
    foreground_area_ratio = foreground_pixels / foreground.size
    bbox_width = x_max - x_min + 1
    bbox_height = y_max - y_min + 1
    bbox_area_ratio = bbox_width * bbox_height / foreground.size
    minimum_margin_ratio = min(
        x_min / width,
        (width - 1 - x_max) / width,
        y_min / height,
        (height - 1 - y_max) / height,
    )
    return _MaskMetrics(
        pixel_sha256=pixel_sha256,
        foreground_pixels=foreground_pixels,
        foreground_area_ratio=foreground_area_ratio,
        x_min=x_min,
        y_min=y_min,
        x_max=x_max,
        y_max=y_max,
        bbox_width_ratio=bbox_width / width,
        bbox_height_ratio=bbox_height / height,
        bbox_area_ratio=bbox_area_ratio,
        minimum_margin_ratio=minimum_margin_ratio,
    )


def _ratio_summary(values: list[float]) -> dict[str, float]:
    return {
        "minimum": round(min(values), 9),
        "maximum": round(max(values), 9),
        "mean": round(statistics.fmean(values), 9),
        "median": round(statistics.median(values), 9),
    }


def _require_source_stable(
    *,
    source_run: Path,
    source_manifest_path: Path,
    source_manifest_bytes: bytes,
    frames: _FrameCohort,
    masks: _MaskCohort,
) -> None:
    current_manifest = _read_regular_file(
        source_manifest_path,
        root=source_run,
        label="render manifest",
        maximum_bytes=MAX_SOURCE_MANIFEST_BYTES,
    )
    if current_manifest != source_manifest_bytes:
        raise ShowcaseError("Render manifest changed while creating showcase")
    for path, record in zip(frames.paths, frames.records, strict=True):
        payload = _read_regular_file(
            path,
            root=source_run,
            label="beauty_lit frame",
            maximum_bytes=MAX_SHOWCASE_FRAME_BYTES,
        )
        if (
            len(payload) != record["size"]
            or hashlib.sha256(payload).hexdigest() != record["sha256"]
        ):
            raise ShowcaseError(f"Showcase source frame changed during encoding: {record['path']}")
    for path, record in zip(masks.paths, masks.records, strict=True):
        payload = _read_regular_file(
            path,
            root=source_run,
            label="object_mask frame",
            maximum_bytes=MAX_SHOWCASE_FRAME_BYTES,
        )
        if (
            len(payload) != record["size"]
            or hashlib.sha256(payload).hexdigest() != record["sha256"]
        ):
            raise ShowcaseError(
                f"Showcase object_mask frame changed during encoding: {record['path']}"
            )


def _encode_showcase(
    *,
    ffmpeg: str,
    frames: _FrameCohort,
    output_path: Path,
) -> list[str]:
    pattern = frames.paths[0].parent / "frame_%04d.png"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-framerate",
        str(SHOWCASE_FPS),
        "-start_number",
        "0",
        "-i",
        str(pattern),
        "-frames:v",
        str(len(frames.paths)),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        str(SHOWCASE_CRF),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = _run(command, timeout=1800)
    if result.returncode != 0:
        raise ShowcaseError(
            f"ffmpeg failed with exit code {result.returncode}; "
            f"command: {shlex.join(command)}; stderr: {_tail(result.stderr) or '<empty>'}"
        )
    _require_regular_nonempty_file(output_path, root=output_path.parent, label="showcase video")
    return command


def _probe_showcase(
    *,
    ffprobe: str,
    video_path: Path,
    expected_resolution: tuple[int, int],
    expected_frames: int,
) -> dict[str, Any]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-count_frames",
        "-show_entries",
        (
            "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate,"
            "nb_frames,nb_read_frames:format=duration,size,format_name"
        ),
        "-of",
        "json",
        str(video_path),
    ]
    result = _run(command, timeout=60)
    if result.returncode != 0:
        raise ShowcaseError(
            f"ffprobe failed with exit code {result.returncode}; "
            f"stderr: {_tail(result.stderr) or '<empty>'}"
        )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ShowcaseError(f"ffprobe returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ShowcaseError("ffprobe output must be an object")
    streams = payload.get("streams")
    format_payload = payload.get("format")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
        raise ShowcaseError("Showcase must contain exactly one probed video stream")
    if not isinstance(format_payload, dict):
        raise ShowcaseError("ffprobe output lacks format evidence")
    stream = streams[0]
    if stream.get("codec_type") != "video":
        raise ShowcaseError("Showcase must contain exactly one video stream and no audio")
    if stream.get("codec_name") != "h264":
        raise ShowcaseError(f"Showcase codec must be h264, got {stream.get('codec_name')!r}")
    if stream.get("pix_fmt") != "yuv420p":
        raise ShowcaseError(f"Showcase pixel format must be yuv420p, got {stream.get('pix_fmt')!r}")
    resolution = (
        _strict_positive_int(stream.get("width"), "video width"),
        _strict_positive_int(stream.get("height"), "video height"),
    )
    if resolution != expected_resolution:
        raise ShowcaseError(f"Showcase resolution mismatch: {resolution} != {expected_resolution}")
    r_frame_rate = _frame_rate(stream.get("r_frame_rate"), "r_frame_rate")
    avg_frame_rate = _frame_rate(stream.get("avg_frame_rate"), "avg_frame_rate")
    if r_frame_rate != SHOWCASE_FPS or avg_frame_rate != SHOWCASE_FPS:
        raise ShowcaseError(
            f"Showcase frame rate must be {SHOWCASE_FPS} fps, got {r_frame_rate} / {avg_frame_rate}"
        )
    frame_count = _probe_frame_count(stream)
    if frame_count != expected_frames:
        raise ShowcaseError(f"Showcase frame count mismatch: {frame_count} != {expected_frames}")
    duration = _strict_finite_float(format_payload.get("duration"), "video duration")
    expected_duration = expected_frames / SHOWCASE_FPS
    if not math.isclose(duration, expected_duration, rel_tol=0.0, abs_tol=0.05):
        raise ShowcaseError(
            f"Showcase duration mismatch: {duration:.6f} != {expected_duration:.6f}"
        )
    size = _strict_positive_int_from_string(format_payload.get("size"), "video size")
    actual_size = video_path.stat().st_size
    if size != actual_size:
        raise ShowcaseError(f"Showcase size mismatch: ffprobe={size}, file={actual_size}")
    format_name = format_payload.get("format_name")
    if not isinstance(format_name, str) or "mp4" not in format_name.split(","):
        raise ShowcaseError(f"Showcase container must be MP4, got {format_name!r}")
    return {
        "container": "mp4",
        "codec": "h264",
        "pixel_format": "yuv420p",
        "width": resolution[0],
        "height": resolution[1],
        "fps": SHOWCASE_FPS,
        "frame_count": frame_count,
        "duration_sec": round(duration, 6),
        "size": actual_size,
        "faststart": True,
    }


def _rights_payload(asset: Mapping[str, Any]) -> dict[str, Any]:
    license_name = _optional_string(asset.get("license"), "asset license")
    license_tier = _optional_string(asset.get("license_tier"), "asset license_tier")
    license_url = _optional_string(asset.get("license_url"), "asset license_url")
    attribution = _optional_string(asset.get("attribution"), "asset attribution")
    complete = all(
        value is not None for value in (license_name, license_tier, license_url, attribution)
    )
    if not complete:
        raise ShowcaseError(
            "Showcase source must contain complete license and attribution provenance"
        )
    assert license_tier is not None and license_url is not None
    if license_tier not in {"open", "nc", "ue-only"}:
        raise ShowcaseError(f"Showcase source has invalid license tier: {license_tier!r}")
    parsed_license_url = urlparse(license_url)
    if parsed_license_url.scheme not in {"http", "https", "file", "urn"}:
        raise ShowcaseError(f"Showcase source has invalid license URL: {license_url!r}")
    return {
        "license": {
            "name": license_name,
            "tier": license_tier,
            "url": license_url,
        },
        "attribution": attribution,
        "provenance": "render_manifest",
    }


def _showcase_asset_payload(asset_id: str, asset: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "kind",
        "scene_id",
        "source",
        "source_id",
        "source_url",
        "source_sha256",
        "content_sha256",
        "build_sha256",
        "bundle_sha256",
        "package_bundle_sha256",
    )
    payload: dict[str, Any] = {
        "asset_id": asset_id,
        "render_asset_sha256": _canonical_sha256(asset),
    }
    for key in keys:
        value = asset.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ShowcaseError(f"Render asset {key} must be a string")
            payload[key] = value
    export = asset.get("export")
    if export is not None:
        if not isinstance(export, bool):
            raise ShowcaseError("Render asset export must be a boolean")
        payload["export"] = export
    return payload


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise ShowcaseError(f"{name} not found; run `uef doctor` and install FFmpeg")
    return executable


def _git_evidence(project_root: Path) -> tuple[str, bool]:
    commit_result = _run(
        ["git", "-C", str(project_root), "rev-parse", "--verify", "HEAD"],
        timeout=30,
    )
    commit = commit_result.stdout.strip()
    if commit_result.returncode != 0 or _GIT_COMMIT_RE.fullmatch(commit) is None:
        raise ShowcaseError("Cannot resolve the Git commit for showcase provenance")
    status_result = _run(
        ["git", "-C", str(project_root), "status", "--porcelain", "--untracked-files=normal"],
        timeout=30,
    )
    if status_result.returncode != 0:
        raise ShowcaseError("Cannot resolve the Git worktree state for showcase provenance")
    return commit, bool(status_result.stdout.strip())


def _run(command: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ShowcaseError(
            f"Command timed out after {timeout}s: {shlex.join(command)}; "
            f"stderr: {_tail(exc.stderr) or '<empty>'}"
        ) from exc
    except OSError as exc:
        raise ShowcaseError(f"Cannot run command {shlex.join(command)}: {exc}") from exc


def _publish_directory(*, staging_dir: Path, final_dir: Path) -> None:
    lock_path = final_dir.parent / ".publish.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ShowcaseError(f"Cannot open showcase publish lock {lock_path}: {exc}") from exc
    renamed = False
    try:
        with os.fdopen(descriptor, "rb+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            if final_dir.exists() or final_dir.is_symlink():
                raise ShowcaseError(f"Showcase output already exists: {final_dir}")
            os.rename(staging_dir, final_dir)
            renamed = True
            _fsync_directory(final_dir.parent)
    except BaseException as exc:
        if renamed and final_dir.exists() and not staging_dir.exists():
            try:
                os.rename(final_dir, staging_dir)
                _fsync_directory(final_dir.parent)
            except BaseException as rollback_exc:
                raise ShowcaseError(
                    f"Showcase publish failed and rollback was not durable: {rollback_exc}"
                ) from exc
        if isinstance(exc, ShowcaseError):
            raise
        raise ShowcaseError(f"Cannot atomically publish showcase {final_dir}: {exc}") from exc


def _require_faststart(path: Path) -> None:
    moov_offset: int | None = None
    mdat_offset: int | None = None
    file_size = path.stat().st_size
    offset = 0
    try:
        with path.open("rb") as file:
            while offset + 8 <= file_size:
                file.seek(offset)
                header = file.read(8)
                if len(header) != 8:
                    break
                box_size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                header_size = 8
                if box_size == 1:
                    extended = file.read(8)
                    if len(extended) != 8:
                        raise ShowcaseError("Showcase MP4 has a truncated extended box header")
                    box_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = file_size - offset
                if box_size < header_size or offset + box_size > file_size:
                    raise ShowcaseError("Showcase MP4 has an invalid top-level box size")
                if box_type == b"moov" and moov_offset is None:
                    moov_offset = offset
                elif box_type == b"mdat" and mdat_offset is None:
                    mdat_offset = offset
                offset += box_size
    except OSError as exc:
        raise ShowcaseError(f"Cannot inspect showcase MP4 structure: {exc}") from exc
    if moov_offset is None or mdat_offset is None or moov_offset >= mdat_offset:
        raise ShowcaseError("Showcase MP4 does not prove +faststart (moov must precede mdat)")


def _parse_json_object(payload: bytes, path: Path) -> dict[str, Any]:
    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ShowcaseError(f"JSON contains duplicate key {key!r}: {path}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ShowcaseError(f"JSON contains non-finite number {value}: {path}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ShowcaseError(f"Invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ShowcaseError(f"JSON root must be an object: {path}")
    return value


def _read_regular_file(
    path: Path,
    *,
    root: Path,
    label: str,
    maximum_bytes: int | None = None,
) -> bytes:
    _require_no_symlink_path(path, root=root)
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ShowcaseError(f"Cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ShowcaseError(f"{label.capitalize()} must be a regular file: {path}")
    if maximum_bytes is not None and metadata.st_size > maximum_bytes:
        raise ShowcaseError(f"{label.capitalize()} exceeds {maximum_bytes} bytes: {path}")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ShowcaseError(f"Cannot read {label} {path}: {exc}") from exc
    if len(payload) != metadata.st_size:
        raise ShowcaseError(f"{label.capitalize()} changed while being read: {path}")
    return payload


def _require_regular_nonempty_file(path: Path, *, root: Path, label: str) -> None:
    payload = _read_regular_file(path, root=root, label=label)
    if not payload:
        raise ShowcaseError(f"{label.capitalize()} is empty: {path}")


def _require_no_symlink_path(path: Path, *, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ShowcaseError(f"Path escapes trusted root {root}: {path}") from None
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            raise ShowcaseError(f"Required path does not exist: {current}") from None
        except OSError as exc:
            raise ShowcaseError(f"Cannot inspect path {current}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ShowcaseError(f"Symlink paths are not allowed: {current}")


def _require_directory(path: Path, label: str) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ShowcaseError(f"Cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ShowcaseError(f"{label.capitalize()} must be a directory: {path}")


def _mkdir_tree_without_symlinks(path: Path, *, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ShowcaseError(f"Output path escapes project root {root}: {path}") from None
    current = root
    for part in relative.parts:
        current /= part
        try:
            current.mkdir(mode=0o755)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ShowcaseError(
                f"Cannot create showcase output directory {current}: {exc}"
            ) from exc
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ShowcaseError(f"Cannot inspect output directory {current}: {exc}") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ShowcaseError(f"Showcase output ancestor must be a real directory: {current}")


def _remove_staging_directory(path: Path) -> None:
    try:
        if path.exists() and not path.is_symlink():
            shutil.rmtree(path)
    except OSError as exc:
        raise ShowcaseError(
            f"Cannot remove failed showcase staging directory {path}: {exc}"
        ) from exc


def _write_json_durable(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = (
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    _write_bytes_durable(path, rendered)


def _write_bytes_durable(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_regular_file(path: Path, *, root: Path, label: str) -> None:
    _require_no_symlink_path(path, root=root)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ShowcaseError(f"Cannot open {label} for fsync {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            raise ShowcaseError(f"{label.capitalize()} must be a non-empty regular file: {path}")
        os.fsync(descriptor)
    except OSError as exc:
        raise ShowcaseError(f"Cannot fsync {label} {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise ShowcaseError(f"Cannot open directory for fsync {path}: {exc}") from exc
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ShowcaseError(f"Cannot hash file {path}: {exc}") from exc
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    rendered = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _safe_relative_path(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ShowcaseError(f"{label.capitalize()} path must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ShowcaseError(f"Unsafe {label} path: {value!r}")
    return path


def _resolution(value: Any, label: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ShowcaseError(f"Showcase {label} resolution must contain width and height")
    return (
        _strict_positive_int(value[0], f"{label} width"),
        _strict_positive_int(value[1], f"{label} height"),
    )


def _strict_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ShowcaseError(f"{label.capitalize()} must be a positive integer")
    return value


def _strict_positive_int_from_string(value: Any, label: str) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise ShowcaseError(f"{label.capitalize()} must be a decimal string")
    return _strict_positive_int(int(value), label)


def _strict_finite_float(value: Any, label: str) -> float:
    if not isinstance(value, str):
        raise ShowcaseError(f"{label.capitalize()} must be a numeric string")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ShowcaseError(f"{label.capitalize()} is invalid: {value!r}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ShowcaseError(f"{label.capitalize()} must be finite and positive")
    return parsed


def _frame_rate(value: Any, label: str) -> Fraction:
    if not isinstance(value, str):
        raise ShowcaseError(f"{label} must be a rational string")
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ShowcaseError(f"Invalid {label}: {value!r}") from exc
    if rate <= 0:
        raise ShowcaseError(f"{label} must be positive")
    return rate


def _probe_frame_count(stream: Mapping[str, Any]) -> int:
    values: list[int] = []
    for key in ("nb_frames", "nb_read_frames"):
        raw = stream.get(key)
        if raw in (None, "N/A"):
            continue
        values.append(_strict_positive_int_from_string(raw, key))
    if not values:
        raise ShowcaseError("ffprobe did not report a video frame count")
    if len(set(values)) != 1:
        raise ShowcaseError(f"ffprobe frame counts disagree: {values}")
    return values[0]


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ShowcaseError(f"{label.capitalize()} must be null or a non-empty string")
    return value


def _asset_slug(asset_id: str) -> str:
    slug = asset_id.lower().replace(":", "_").replace(".", "_")
    if _STAGE_RE.fullmatch(slug) is None:
        raise ShowcaseError(f"Asset id cannot form a safe showcase slug: {asset_id!r}")
    return slug


def _validate_stage(stage: str) -> None:
    if _STAGE_RE.fullmatch(stage) is None:
        raise ShowcaseError(
            "Showcase stage must be a lowercase ASCII slug (letters, digits, '_' or '-')"
        )


def _utc_now(clock: Callable[[], datetime] | None) -> datetime:
    value = clock() if clock is not None else datetime.now(UTC)
    if value.tzinfo is None:
        raise ShowcaseError("Showcase clock must return a timezone-aware datetime")
    return value.astimezone(UTC).replace(microsecond=0)


def _tail(value: str | bytes | None, limit: int = 1500) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return value[-limit:].strip()
