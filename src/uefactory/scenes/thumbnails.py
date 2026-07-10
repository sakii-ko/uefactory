from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import yaml

from uefactory.catalog import Catalog, SceneArtifactUpsert, SceneRecord, SceneUpsert
from uefactory.core.config import Settings
from uefactory.core.paths import utc_timestamp
from uefactory.render.job import RenderJobResult, render_job
from uefactory.render.thumbnails import (
    MAX_BACKGROUND_CONTAMINATION_RATIO,
    _create_subject_mask_png,
    _validate_black_background_consistency,
)
from uefactory.scenes.locking import scene_lock

SCENE_THUMBNAIL_PRESET = "scene_thumbnail_v1"


@dataclass(frozen=True)
class SceneThumbnailResult:
    scene: SceneRecord
    render: RenderJobResult
    thumbnail_path: Path
    subject_mask_path: Path
    catalog_path: Path
    artifact_ids: tuple[str, ...]


def thumbnail_catalog_scene(
    *,
    settings: Settings,
    scene_id: str,
    database_path: Path | None = None,
    timeout_sec: int = 1800,
) -> SceneThumbnailResult:
    data_dir = settings.data_dir
    if not data_dir.is_absolute():
        data_dir = settings.project_root / data_dir
    with scene_lock(data_dir=data_dir, scene_id=scene_id):
        return _thumbnail_catalog_scene_locked(
            settings=settings,
            scene_id=scene_id,
            database_path=database_path,
            timeout_sec=timeout_sec,
        )


def _thumbnail_catalog_scene_locked(
    *,
    settings: Settings,
    scene_id: str,
    database_path: Path | None,
    timeout_sec: int,
) -> SceneThumbnailResult:
    catalog_path = database_path or settings.data_dir / "catalog.db"
    if not catalog_path.is_absolute():
        catalog_path = settings.project_root / catalog_path
    catalog = Catalog(catalog_path, project_root=settings.project_root)
    catalog.preflight_write()
    record = catalog.get_scene(scene_id)
    if record is None or record.status not in {"built", "render_ok"}:
        status = None if record is None else record.status
        raise ValueError(f"Catalog scene {scene_id!r} is not built: status={status!r}")
    if record.build_sha256 is None:
        raise RuntimeError(f"Catalog scene {scene_id!r} has no active build generation")
    build_artifacts = catalog.list_scene_artifacts(
        scene_id=scene_id,
        kind="scene_build_manifest",
    )
    if len(build_artifacts) != 1 or build_artifacts[0].sha256 != record.build_sha256:
        raise RuntimeError("Catalog scene build manifest does not match its active generation")
    build_params = build_artifacts[0].params
    if build_params.get("build_sha256") != record.build_sha256:
        raise RuntimeError("Catalog scene build artifact has stale generation metadata")
    export = build_params.get("export")
    if not isinstance(export, bool):
        raise RuntimeError("Catalog scene build artifact is missing its export policy")

    job_dir = (
        settings.project_root / "out/scene_thumbnail_jobs" / f"{utc_timestamp()}_{uuid4().hex[:8]}"
    )
    job_dir.mkdir(parents=True, exist_ok=False)
    job_path = job_dir / f"{scene_id}.yaml"
    job_path.write_text(
        yaml.safe_dump(_thumbnail_jobspec(scene_id), sort_keys=False),
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
    if not beauty_frames or not mask_frames or render.artifacts is None:
        raise RuntimeError("Scene thumbnail render is missing beauty/mask artifacts")
    render_manifest = _read_object(render.manifest_path)
    render_asset = render_manifest.get("asset")
    if not isinstance(render_asset, dict) or render_asset.get("kind") != "scene":
        raise RuntimeError("Scene thumbnail render manifest has no scene provenance")
    if (
        render_asset.get("scene_id") != scene_id
        or render_asset.get("source") != record.source
        or render_asset.get("source_id") != record.source_id
        or render_asset.get("source_url") != record.source_url
        or render_asset.get("source_file") != record.source_file
        or render_asset.get("source_sha256") != record.source_sha256
        or render_asset.get("scene_spec_sha256") != record.spec_sha256
        or render_asset.get("build_sha256") != record.build_sha256
        or render_asset.get("license") != record.license
        or render_asset.get("license_tier") != record.license_tier
        or render_asset.get("license_url") != record.license_url
        or render_asset.get("attribution") != record.attribution
        or render_asset.get("export") is not export
    ):
        raise RuntimeError("Scene thumbnail provenance does not match the catalog scene")
    raw_stencil_ids = render_asset.get("expected_object_stencil_ids")
    if (
        not isinstance(raw_stencil_ids, list)
        or not raw_stencil_ids
        or any(isinstance(value, bool) or not isinstance(value, int) for value in raw_stencil_ids)
    ):
        raise RuntimeError("Scene thumbnail render has no valid object stencil inventory")
    stencil_ids = tuple(raw_stencil_ids)
    if len(stencil_ids) > 255 or stencil_ids != tuple(range(1, len(stencil_ids) + 1)):
        raise RuntimeError("Scene thumbnail object stencil inventory must be contiguous from 1")
    if render_asset.get("static_mesh_actor_count") != len(stencil_ids):
        raise RuntimeError("Scene thumbnail stencil count does not match the rendered scene")
    contamination_payload = render_asset.get(
        "maximum_background_contamination_ratio",
        MAX_BACKGROUND_CONTAMINATION_RATIO,
    )
    if (
        isinstance(contamination_payload, bool)
        or not isinstance(contamination_payload, int | float)
        or not 0.001 <= float(contamination_payload) <= 0.01
    ):
        raise RuntimeError("Scene thumbnail has an invalid background contamination policy")
    maximum_contamination_ratio = float(contamination_payload)
    consistency = _validate_black_background_consistency(
        beauty_frames,
        mask_frames,
        subject_stencil_ids=stencil_ids,
        maximum_contamination_ratio=maximum_contamination_ratio,
    )
    selected_view_index = max(
        range(len(consistency)),
        key=lambda index: float(consistency[index]["subject_area_ratio"]),
    )
    thumbnail_path = render.run_dir / "thumbnail.png"
    temporary_thumbnail = thumbnail_path.with_suffix(".png.tmp")
    shutil.copy2(beauty_frames[selected_view_index], temporary_thumbnail)
    temporary_thumbnail.replace(thumbnail_path)
    subject_mask_path = render.run_dir / "subject_mask.png"
    _create_subject_mask_png(
        mask_frames[selected_view_index],
        subject_mask_path,
        subject_stencil_ids=stencil_ids,
    )

    planned = (
        ("thumb_beauty", "scene_thumbnail_beauty", thumbnail_path),
        ("thumb_mask", "scene_thumbnail_mask", subject_mask_path),
        ("thumb_mask_raw", "scene_thumbnail_mask_raw", mask_frames[selected_view_index]),
        ("thumb_manifest", "scene_thumbnail_render_manifest", render.manifest_path),
        ("thumb_contact", "scene_thumbnail_contact_sheet", render.artifacts.contact_sheet),
    )
    artifact_ids = tuple(f"{scene_id}_{suffix}" for suffix, _, _ in planned)
    render_manifest["scene_catalog_commit"] = {
        "scene_id": scene_id,
        "target_status": "render_ok",
        "thumbnail_preset": SCENE_THUMBNAIL_PRESET,
        "selected_view_index": selected_view_index,
        "artifact_ids": list(artifact_ids),
        "source_sha256": record.source_sha256,
        "scene_spec_sha256": record.spec_sha256,
        "build_sha256": record.build_sha256,
        "subject_stencil_ids": list(stencil_ids),
        "maximum_background_contamination_ratio": maximum_contamination_ratio,
    }
    render_manifest["scene_thumbnail_validation"] = {
        "status": "passed",
        "selected_view_index": selected_view_index,
        "subject_stencil_ids": list(stencil_ids),
        "maximum_background_contamination_ratio": maximum_contamination_ratio,
        "frames": consistency,
    }
    _write_json(render.manifest_path, render_manifest)

    params = {
        "schema_version": 2,
        "thumbnail_preset": SCENE_THUMBNAIL_PRESET,
        "render_manifest": _project_relative(settings.project_root, render.manifest_path),
        "selected_view_index": selected_view_index,
        "views": 8,
        "resolution": [512, 512],
        "lighting": "three_point",
        "source_sha256": record.source_sha256,
        "scene_spec_sha256": record.spec_sha256,
        "build_sha256": record.build_sha256,
        "map_path": record.map_path,
        "license": record.license,
        "license_tier": record.license_tier,
        "license_url": record.license_url,
        "attribution": record.attribution,
        "export": export,
        "subject_stencil_ids": list(stencil_ids),
        "maximum_background_contamination_ratio": maximum_contamination_ratio,
    }
    artifacts = tuple(
        SceneArtifactUpsert(
            artifact_id=artifact_id,
            scene_id=scene_id,
            kind=kind,
            path=path,
            params=params,
            sha256=_sha256(path),
        )
        for artifact_id, (_, kind, path) in zip(artifact_ids, planned, strict=True)
    )
    updated, _ = catalog.finalize_scene_render(_render_ok_upsert(record), artifacts)
    return SceneThumbnailResult(
        scene=updated,
        render=render,
        thumbnail_path=thumbnail_path,
        subject_mask_path=subject_mask_path,
        catalog_path=catalog_path,
        artifact_ids=artifact_ids,
    )


def _thumbnail_jobspec(scene_id: str) -> dict[str, object]:
    return {
        "job": "render",
        "assets": [f"scene:{scene_id}"],
        "camera": {
            "rig": "orbit",
            "views": 8,
            "elevation_deg": 22.5,
            "fov": 45,
            "resolution": [512, 512],
        },
        "lighting": {"preset": "three_point"},
        "passes": ["beauty_lit", "object_mask"],
        "output": {"dir": "out/scene_thumbnails"},
    }


def _render_ok_upsert(record: SceneRecord) -> SceneUpsert:
    return SceneUpsert(
        scene_id=record.scene_id,
        name=record.name,
        source=record.source,
        source_id=record.source_id,
        source_url=record.source_url,
        license=record.license,
        license_tier=record.license_tier,
        license_url=record.license_url,
        attribution=record.attribution,
        source_path=record.source_path,
        source_file=record.source_file,
        source_sha256=record.source_sha256,
        spec_sha256=record.spec_sha256,
        build_sha256=record.build_sha256,
        status="render_ok",
        map_path=record.map_path,
        actor_count=record.actor_count,
        static_mesh_count=record.static_mesh_count,
        triangle_count=record.triangle_count,
        material_count=record.material_count,
        texture_count=record.texture_count,
        bounds=record.bounds,
    )


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON manifest must be an object: {path}")
    return value


def _write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _project_relative(project_root: Path, path: Path) -> str:
    return path.resolve().relative_to(project_root.resolve()).as_posix()


__all__ = ["SceneThumbnailResult", "thumbnail_catalog_scene"]
