from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard
from uuid import uuid4

import yaml
from PIL import Image

from uefactory.catalog import ArtifactUpsert, AssetRecord, AssetUpsert, Catalog
from uefactory.core.config import Settings
from uefactory.core.identity import validate_asset_id
from uefactory.core.paths import utc_timestamp
from uefactory.render.job import RenderJobResult, render_job
from uefactory.render.passes import STENCIL_NORMALIZED_ATOL, _read_half_rgba_exr

THUMBNAIL_PRESET = "catalog_thumbnail_v1"
THUMBNAIL_VALIDATION_RULE = "catalog_thumbnail_visual_v1"
MAX_BACKGROUND_CONTAMINATION_RATIO = 0.001
MIN_SUBJECT_MAX_AREA_RATIO = 0.02
MIN_SUBJECT_MEDIAN_AREA_RATIO = 0.01


@dataclass(frozen=True)
class ThumbnailResult:
    asset_id: str
    render: RenderJobResult
    thumbnail_path: Path
    subject_mask_path: Path
    catalog_path: Path
    artifact_ids: tuple[str, ...]


def thumbnail_catalog_asset(
    *,
    settings: Settings,
    asset_id: str,
    database_path: Path | None = None,
    timeout_sec: int = 1800,
) -> ThumbnailResult:
    validate_asset_id(asset_id)
    catalog_path = database_path or settings.data_dir / "catalog.db"
    if not catalog_path.is_absolute():
        catalog_path = settings.project_root / catalog_path
    catalog = Catalog(catalog_path, project_root=settings.project_root)
    record = catalog.get_asset(asset_id)
    if record is None or record.status not in {"imported", "render_ok"}:
        status = None if record is None else record.status
        raise ValueError(f"Catalog asset {asset_id!r} is not imported: status={status!r}")

    job_dir = settings.project_root / "out/thumbnail_jobs" / f"{utc_timestamp()}_{uuid4().hex[:8]}"
    job_dir.mkdir(parents=True, exist_ok=False)
    job_path = job_dir / f"{asset_id}.yaml"
    job_path.write_text(
        yaml.safe_dump(_thumbnail_jobspec(asset_id), sort_keys=False),
        encoding="utf-8",
    )
    render = render_job(
        settings=settings,
        job_path=job_path,
        timeout_sec=timeout_sec,
        database_path=catalog_path,
    )
    beauty_frames = render.frame_paths.get("beauty_lit", [])
    mask_frames = render.frame_paths.get("object_mask", [])
    if not beauty_frames or not mask_frames:
        raise RuntimeError("Standard thumbnail render requires beauty_lit and object_mask frames")
    if render.artifacts is None:
        raise RuntimeError("Standard thumbnail render did not create derived artifacts")
    consistency = _validate_black_background_consistency(beauty_frames, mask_frames)
    selected_view_index = max(
        range(len(consistency)),
        key=lambda index: float(consistency[index]["subject_area_ratio"]),
    )
    selected_beauty = beauty_frames[selected_view_index]
    selected_mask = mask_frames[selected_view_index]

    thumbnail_path = render.run_dir / "thumbnail.png"
    temporary_thumbnail = thumbnail_path.with_suffix(".png.tmp")
    shutil.copy2(selected_beauty, temporary_thumbnail)
    temporary_thumbnail.replace(thumbnail_path)
    subject_mask_path = render.run_dir / "subject_mask.png"
    _create_subject_mask_png(selected_mask, subject_mask_path)

    manifest = json.loads(render.manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("status") != "ok":
        raise RuntimeError(f"Thumbnail render manifest is not complete: {render.manifest_path}")
    render_asset = manifest.get("asset")
    render_normalization = (
        render_asset.get("normalization") if isinstance(render_asset, dict) else None
    )
    requested_normalization = (
        render_normalization.get("request") if isinstance(render_normalization, dict) else None
    )
    import_manifest = (
        render_asset.get("import_manifest") if isinstance(render_asset, dict) else None
    )
    bundle_sha256 = render_asset.get("bundle_sha256") if isinstance(render_asset, dict) else None
    if (
        not isinstance(requested_normalization, dict)
        or not isinstance(import_manifest, str)
        or not isinstance(bundle_sha256, str)
    ):
        raise RuntimeError("Thumbnail render is missing import/normalization provenance")
    planned = (
        ("thumb_beauty", "thumbnail_beauty", thumbnail_path),
        ("thumb_mask", "thumbnail_mask", subject_mask_path),
        ("thumb_mask_raw", "thumbnail_mask_raw", selected_mask),
        ("thumb_manifest", "thumbnail_render_manifest", render.manifest_path),
        ("thumb_contact", "thumbnail_contact_sheet", render.artifacts.contact_sheet),
    )
    artifact_ids = tuple(
        _artifact_id(asset_id, short_kind, path) for short_kind, _, path in planned
    )
    manifest["catalog_commit"] = {
        "database": _relative_project_path(settings.project_root, catalog_path),
        "asset_id": asset_id,
        "target_status": "render_ok",
        "artifact_ids": list(artifact_ids),
        "thumbnail_preset": THUMBNAIL_PRESET,
        "selected_view_index": selected_view_index,
        "bundle_sha256": bundle_sha256,
        "requested_normalization": requested_normalization,
        "import_manifest": import_manifest,
    }
    manifest["thumbnail_validation"] = {
        "rule_version": THUMBNAIL_VALIDATION_RULE,
        "max_background_contamination_ratio": MAX_BACKGROUND_CONTAMINATION_RATIO,
        "min_subject_max_area_ratio": MIN_SUBJECT_MAX_AREA_RATIO,
        "min_subject_median_area_ratio": MIN_SUBJECT_MEDIAN_AREA_RATIO,
        "selected_view_index": selected_view_index,
        "subject_area": {
            "minimum": min(float(item["subject_area_ratio"]) for item in consistency),
            "median": statistics.median(float(item["subject_area_ratio"]) for item in consistency),
            "maximum": max(float(item["subject_area_ratio"]) for item in consistency),
        },
        "frames": consistency,
        "status": "passed",
    }
    _write_json(render.manifest_path, manifest)

    common_params = {
        "schema_version": 1,
        "thumbnail_preset": THUMBNAIL_PRESET,
        "render_manifest": _relative_project_path(settings.project_root, render.manifest_path),
        "views": 8,
        "resolution": [512, 512],
        "lighting": "three_point",
        "subject_stencil_id": 1,
        "selected_view_index": selected_view_index,
        "bundle_sha256": bundle_sha256,
        "requested_normalization": requested_normalization,
        "import_manifest": import_manifest,
    }
    artifacts = tuple(
        ArtifactUpsert(
            artifact_id=artifact_id,
            asset_id=asset_id,
            kind=kind,
            path=path,
            params=common_params,
            sha256=_sha256(path),
        )
        for artifact_id, (_, kind, path) in zip(artifact_ids, planned, strict=True)
    )
    catalog.finalize_render(_render_ok_upsert(record), artifacts)
    return ThumbnailResult(
        asset_id=asset_id,
        render=render,
        thumbnail_path=thumbnail_path,
        subject_mask_path=subject_mask_path,
        catalog_path=catalog.database_path,
        artifact_ids=artifact_ids,
    )


def _thumbnail_jobspec(asset_id: str) -> dict[str, object]:
    return {
        "job": "render",
        "assets": [asset_id],
        "camera": {
            "rig": "orbit",
            "views": 8,
            "elevation_deg": 20,
            "fov": 45,
            "resolution": [512, 512],
        },
        "lighting": {"preset": "three_point"},
        "passes": ["beauty_lit", "object_mask"],
        "output": {"dir": "out/thumbnails"},
    }


def _create_subject_mask_png(
    mask_exr: Path,
    output_path: Path,
    *,
    subject_stencil_ids: tuple[int, ...] = (1,),
) -> None:
    pixels, resolution = _read_half_rgba_exr("object_mask", mask_exr)
    import numpy as np

    del resolution
    subject = _stencil_union_mask(
        np.asarray(pixels[:, :, 0], dtype=np.float32),
        subject_stencil_ids,
    )
    if not bool(np.any(subject)) or bool(np.all(subject)):
        raise RuntimeError(
            "Object mask does not contain a bounded subject stencil union "
            f"{list(subject_stencil_ids)}: {mask_exr}"
        )
    image = Image.fromarray((subject.astype(np.uint8) * 255), mode="L")
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    image.save(temporary, format="PNG", optimize=False, compress_level=6)
    image.close()
    os.replace(temporary, output_path)


def _validate_black_background_consistency(
    beauty_frames: list[Path],
    mask_frames: list[Path],
    *,
    subject_stencil_ids: tuple[int, ...] = (1,),
    maximum_contamination_ratio: float = MAX_BACKGROUND_CONTAMINATION_RATIO,
) -> list[dict[str, float | int | str]]:
    if (
        isinstance(maximum_contamination_ratio, bool)
        or not isinstance(maximum_contamination_ratio, int | float)
        or not math.isfinite(float(maximum_contamination_ratio))
        or not 0.001 <= float(maximum_contamination_ratio) <= 0.01
    ):
        raise ValueError("maximum background contamination ratio must be in [0.001, 0.01]")
    if len(beauty_frames) != len(mask_frames) or not beauty_frames:
        raise RuntimeError(
            "Thumbnail beauty/mask consistency requires matching non-empty frame sequences"
        )
    import numpy as np

    results: list[dict[str, float | int | str]] = []
    for beauty_path, mask_path in zip(beauty_frames, mask_frames, strict=True):
        pixels, resolution = _read_half_rgba_exr("object_mask", mask_path)
        stencil = np.asarray(pixels[:, :, 0], dtype=np.float32)
        subject = _stencil_union_mask(stencil, subject_stencil_ids)
        total_pixels = int(stencil.size)
        subject_pixels = int(np.count_nonzero(subject))
        subject_area_ratio = subject_pixels / total_pixels
        non_background = ~np.isclose(
            stencil,
            0.0,
            rtol=0.0,
            atol=STENCIL_NORMALIZED_ATOL,
        )
        expanded = non_background.copy()
        height, width = non_background.shape
        for y_offset in (-1, 0, 1):
            for x_offset in (-1, 0, 1):
                source_y = slice(max(0, -y_offset), min(height, height - y_offset))
                source_x = slice(max(0, -x_offset), min(width, width - x_offset))
                target_y = slice(max(0, y_offset), min(height, height + y_offset))
                target_x = slice(max(0, x_offset), min(width, width + x_offset))
                expanded[target_y, target_x] |= non_background[source_y, source_x]
        safe_background = ~expanded
        safe_pixels = int(np.count_nonzero(safe_background))
        if safe_pixels <= 0:
            raise RuntimeError(f"Thumbnail mask leaves no safe background pixels: {mask_path}")
        with Image.open(beauty_path) as image:
            image.load()
            if image.size != resolution:
                raise RuntimeError(
                    f"Thumbnail beauty/mask resolution mismatch: {beauty_path}={image.size} "
                    f"{mask_path}={resolution}"
                )
            converted = image.convert("RGB")
            beauty = np.asarray(converted, dtype=np.uint8).copy()
            converted.close()
        contaminated = safe_background & np.any(beauty > 2, axis=2)
        contaminated_pixels = int(np.count_nonzero(contaminated))
        ratio = contaminated_pixels / safe_pixels
        if ratio > maximum_contamination_ratio:
            raise RuntimeError(
                "Thumbnail beauty contains non-stenciled foreground contamination: "
                f"beauty={beauty_path} mask={mask_path} ratio={ratio:.6f} "
                f"limit={maximum_contamination_ratio:.6f}"
            )
        results.append(
            {
                "frame": beauty_path.name,
                "safe_background_pixels": safe_pixels,
                "contaminated_pixels": contaminated_pixels,
                "contamination_ratio": round(ratio, 9),
                "total_pixels": total_pixels,
                "subject_pixels": subject_pixels,
                "subject_area_ratio": round(subject_area_ratio, 9),
            }
        )
    subject_ratios = [float(item["subject_area_ratio"]) for item in results]
    maximum = max(subject_ratios)
    median = statistics.median(subject_ratios)
    if maximum < MIN_SUBJECT_MAX_AREA_RATIO or median < MIN_SUBJECT_MEDIAN_AREA_RATIO:
        raise RuntimeError(
            "Thumbnail subject occupies too little of the frame: "
            f"max={maximum:.6f} required_max={MIN_SUBJECT_MAX_AREA_RATIO:.6f} "
            f"median={median:.6f} required_median={MIN_SUBJECT_MEDIAN_AREA_RATIO:.6f}"
        )
    return results


def _stencil_union_mask(stencil: Any, stencil_ids: tuple[int, ...]) -> Any:
    if (
        not stencil_ids
        or tuple(sorted(set(stencil_ids))) != stencil_ids
        or any(
            isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255
            for value in stencil_ids
        )
    ):
        raise ValueError("subject stencil IDs must be unique ascending integers in [1, 255]")
    import numpy as np

    subject = np.zeros(stencil.shape, dtype=bool)
    for stencil_id in stencil_ids:
        subject |= np.isclose(
            stencil,
            np.float32(stencil_id / 255.0),
            rtol=0.0,
            atol=STENCIL_NORMALIZED_ATOL,
        )
    return subject


def is_valid_thumbnail_validation(value: Any, *, expected_frames: int) -> bool:
    if not isinstance(value, dict):
        return False
    frames = value.get("frames")
    subject_area = value.get("subject_area")
    if (
        value.get("rule_version") != THUMBNAIL_VALIDATION_RULE
        or value.get("status") != "passed"
        or value.get("max_background_contamination_ratio") != MAX_BACKGROUND_CONTAMINATION_RATIO
        or value.get("min_subject_max_area_ratio") != MIN_SUBJECT_MAX_AREA_RATIO
        or value.get("min_subject_median_area_ratio") != MIN_SUBJECT_MEDIAN_AREA_RATIO
        or not _nonnegative_int(value.get("selected_view_index"))
        or value["selected_view_index"] >= expected_frames
        or not isinstance(frames, list)
        or len(frames) != expected_frames
        or not isinstance(subject_area, dict)
    ):
        return False
    subject_ratios: list[float] = []
    for frame in frames:
        if not isinstance(frame, dict):
            return False
        safe_pixels = frame.get("safe_background_pixels")
        contaminated_pixels = frame.get("contaminated_pixels")
        contamination_ratio = frame.get("contamination_ratio")
        total_pixels = frame.get("total_pixels")
        subject_pixels = frame.get("subject_pixels")
        subject_ratio = frame.get("subject_area_ratio")
        if (
            not isinstance(frame.get("frame"), str)
            or not _positive_int(safe_pixels)
            or not _nonnegative_int(contaminated_pixels)
            or not _positive_int(total_pixels)
            or not _nonnegative_int(subject_pixels)
            or subject_pixels > total_pixels
            or not _finite_ratio(contamination_ratio)
            or not _finite_ratio(subject_ratio)
            or float(contamination_ratio) > MAX_BACKGROUND_CONTAMINATION_RATIO
            or not math.isclose(
                float(subject_ratio),
                subject_pixels / total_pixels,
                rel_tol=0.0,
                abs_tol=5e-10,
            )
        ):
            return False
        subject_ratios.append(float(subject_ratio))
    actual_summary = {
        "minimum": min(subject_ratios),
        "median": statistics.median(subject_ratios),
        "maximum": max(subject_ratios),
    }
    if any(
        not _finite_ratio(subject_area.get(key))
        or not math.isclose(
            float(subject_area[key]),
            actual,
            rel_tol=0.0,
            abs_tol=5e-10,
        )
        for key, actual in actual_summary.items()
    ):
        return False
    selected_view_index = int(value["selected_view_index"])
    if not math.isclose(
        subject_ratios[selected_view_index],
        actual_summary["maximum"],
        rel_tol=0.0,
        abs_tol=5e-10,
    ):
        return False
    return (
        actual_summary["maximum"] >= MIN_SUBJECT_MAX_AREA_RATIO
        and actual_summary["median"] >= MIN_SUBJECT_MEDIAN_AREA_RATIO
    )


def is_valid_catalog_scene_sanitization(value: Any, *, expected_subjobs: int) -> bool:
    if not isinstance(value, dict) or value.get("policy") != "catalog_hide_all_pawns_v2":
        return False
    subjobs = value.get("subjobs")
    if not isinstance(subjobs, list) or len(subjobs) != expected_subjobs:
        return False
    for index, item in enumerate(subjobs):
        if not isinstance(item, dict) or item.get("subjob_index") != index:
            return False
        count = item.get("hidden_pawn_count")
        editor_count = item.get("editor_hidden_pawn_count")
        meshes = item.get("hidden_static_meshes")
        if (
            not _nonnegative_int(count)
            or not _nonnegative_int(editor_count)
            or editor_count != count
            or not isinstance(meshes, list)
            or any(not isinstance(path, str) for path in meshes)
        ):
            return False
    return True


def _positive_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _finite_ratio(value: Any) -> TypeGuard[int | float]:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


def _render_ok_upsert(record: AssetRecord) -> AssetUpsert:
    return AssetUpsert(
        asset_id=record.asset_id,
        name=record.name,
        source=record.source,
        source_id=record.source_id,
        source_url=record.source_url,
        license=record.license,
        license_tier=record.license_tier,
        license_url=record.license_url,
        attribution=record.attribution,
        status="render_ok",
        tags=record.tags,
        raw_path=record.raw_path,
        ue_package_path=record.ue_package_path,
        tri_count=record.tri_count,
        material_count=record.material_count,
        sha256=record.sha256,
    )


def _artifact_id(asset_id: str, short_kind: str, path: Path) -> str:
    path_hash = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{asset_id}_{short_kind}_{path_hash}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_project_path(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
