from __future__ import annotations

import hashlib
import json
import shutil
import struct
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from PIL import Image

from uefactory.catalog import ArtifactUpsert, AssetUpsert, Catalog
from uefactory.core.asset_locking import asset_lock
from uefactory.core.config import Settings
from uefactory.ingest.executor import IngestResult
from uefactory.ingest.package_evidence import collect_package_bundle_evidence
from uefactory.ingest.pipeline import ingest_batch
from uefactory.ingest.quality import (
    QUALITY_RULESET_VERSION,
    IngestQualityError,
    require_static_mesh_quality,
)
from uefactory.ingest.source_structure import (
    inspect_source_structure,
    source_structure_sha256,
)


def _thumbnail_validation_fixture() -> dict[str, object]:
    return {
        "rule_version": "catalog_thumbnail_visual_v1",
        "max_background_contamination_ratio": 0.001,
        "min_subject_max_area_ratio": 0.02,
        "min_subject_median_area_ratio": 0.01,
        "selected_view_index": 0,
        "subject_area": {"minimum": 0.25, "median": 0.25, "maximum": 0.25},
        "frames": [
            {
                "frame": f"frame_{index:04d}.png",
                "safe_background_pixels": 48,
                "contaminated_pixels": 0,
                "contamination_ratio": 0.0,
                "total_pixels": 64,
                "subject_pixels": 16,
                "subject_area_ratio": 0.25,
            }
            for index in range(8)
        ],
        "status": "passed",
    }


def _scene_sanitization_fixture() -> dict[str, object]:
    return {
        "policy": "catalog_hide_all_pawns_v2",
        "subjobs": [
            {
                "subjob_index": index,
                "hidden_pawn_count": 1,
                "editor_hidden_pawn_count": 1,
                "hidden_static_meshes": ["/Engine/EngineMeshes/Sphere.Sphere"],
            }
            for index in range(2)
        ],
    }


def _settings(tmp_path: Path) -> Settings:
    project_root = tmp_path / "project"
    project_root.mkdir()
    return Settings(
        project_root=project_root,
        ue_root=project_root / "engine",
        ue_home=project_root / "ue-home",
        data_dir=project_root / "data",
        log_dir=project_root / "logs",
    )


def _start_external_asset_lock(
    *,
    data_dir: Path,
    asset_id: str,
) -> tuple[threading.Thread, threading.Event, list[BaseException]]:
    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_lock() -> None:
        try:
            with asset_lock(data_dir=data_dir, asset_id=asset_id):
                acquired.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("test did not release the external asset lock")
        except BaseException as exc:  # pragma: no cover - surfaced by the caller
            errors.append(exc)
            acquired.set()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert acquired.wait(timeout=5)
    return thread, release, errors


def _write_batch_manifest(project_root: Path, asset_ids: tuple[str, ...]) -> Path:
    source_dir = project_root / "sources"
    source_dir.mkdir(exist_ok=True)
    assets: list[dict[str, object]] = []
    for index, asset_id in enumerate(asset_ids, start=1):
        source = source_dir / f"{asset_id}.glb"
        _write_glb(source, asset_id=asset_id, ordinal=index)
        assets.append(
            {
                "asset_id": asset_id,
                "name": asset_id.replace("_", " ").title(),
                "normalization": {
                    "source_units": "auto",
                    "source_up_axis": "auto",
                    "source_handedness": "auto",
                    "uniform_scale": 1.0,
                    "pivot_policy": "preserve_source",
                },
                "path": f"sources/{asset_id}.glb",
                "dependencies": [],
                "source": "local",
                "source_id": f"fixture-{index}",
                "source_url": f"https://example.test/assets/{asset_id}",
                "license": "CC0-1.0",
                "license_tier": "open",
                "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                "attribution": "Public domain pipeline fixture.",
                "tags": ["fixture", "pipeline", "textured"],
            }
        )
    manifest = project_root / "batch.yaml"
    manifest.write_text(yaml.safe_dump({"assets": assets}, sort_keys=False), encoding="utf-8")
    return manifest


def _write_glb(path: Path, *, asset_id: str, ordinal: int) -> None:
    document = {
        "asset": {"version": "2.0", "generator": f"pipeline fixture {ordinal}"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": asset_id}],
        "meshes": [{}],
    }
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * (-len(json_chunk) % 4)
    length = 12 + 8 + len(json_chunk)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, length)
        + struct.pack("<I4s", len(json_chunk), b"JSON")
        + json_chunk
    )


def _mesh(asset_id: str, *, suffix: str = "") -> dict[str, object]:
    return {
        "object_path": f"/Game/UEF/Ingested/{asset_id}/SM_{asset_id}{suffix}",
        "name": f"SM_{asset_id}{suffix}",
        "lod_count": 1,
        "triangle_count": 24,
        "vertex_count": 16,
        "material_count": 2,
        "material_slots": [
            {
                "index": 0,
                "slot_name": "body",
                "material_path": f"/Game/UEF/Ingested/{asset_id}/M_{asset_id}.M_{asset_id}",
                "texture_paths": [f"/Game/UEF/Ingested/{asset_id}/T_{asset_id}.T_{asset_id}"],
            },
            {
                "index": 1,
                "slot_name": "detail",
                "material_path": f"/Game/UEF/Ingested/{asset_id}/M_{asset_id}.M_{asset_id}",
                "texture_paths": [f"/Game/UEF/Ingested/{asset_id}/T_{asset_id}.T_{asset_id}"],
            },
        ],
        "bounds_cm": {
            "min": [-50.0, -25.0, 0.0],
            "max": [50.0, 25.0, 100.0],
            "size": [100.0, 50.0, 100.0],
        },
    }


def _fake_ingest_result(
    settings: Settings,
    asset_id: str,
    meshes: list[dict[str, object]],
    *,
    content_sha256: str,
    bundle_sha256: str | None = None,
    requested_normalization: dict[str, str | float] | None = None,
    source_structure: dict[str, Any] | None = None,
    source_structure_sha256: str | None = None,
    require_texture_references: bool = True,
) -> IngestResult:
    resolved_bundle_sha256 = bundle_sha256 or content_sha256
    normalization = requested_normalization or {
        "source_units": "auto",
        "source_up_axis": "auto",
        "source_handedness": "auto",
        "uniform_scale": 1.0,
        "pivot_policy": "preserve_source",
    }
    normalization_key = hashlib.sha256(
        json.dumps(normalization, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    run_dir = settings.project_root / "out/fake_ue" / f"{asset_id}_{normalization_key}"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    static_mesh_paths = tuple(str(mesh["object_path"]) for mesh in meshes)
    material_path = f"/Game/UEF/Ingested/{asset_id}/M_{asset_id}.M_{asset_id}"
    texture_path = f"/Game/UEF/Ingested/{asset_id}/T_{asset_id}.T_{asset_id}"
    imported_objects = [
        *[{"object_path": object_path, "class": "StaticMesh"} for object_path in static_mesh_paths],
        {"object_path": material_path, "class": "Material"},
        {"object_path": texture_path, "class": "Texture2D"},
    ]
    imported_paths = tuple(str(item["object_path"]) for item in imported_objects)
    for object_path in imported_paths:
        relative_package = object_path.removeprefix("/Game/").partition(".")[0]
        package_file = settings.project_root / "ue/UEFBase/Content" / f"{relative_package}.uasset"
        package_file.parent.mkdir(parents=True, exist_ok=True)
        package_file.write_bytes(f"uasset fixture: {object_path}".encode())
    package_evidence = collect_package_bundle_evidence(
        settings.project_root,
        asset_id=asset_id,
        imported_object_paths=imported_paths,
    )
    if source_structure is None or source_structure_sha256 is None:
        staged_sources = tuple((settings.data_dir / "raw/local" / asset_id).glob(f"{asset_id}.*"))
        assert len(staged_sources) == 1
        evidence = inspect_source_structure(staged_sources[0])
        source_structure = evidence.payload
        source_structure_sha256 = evidence.sha256
    quality_input = {
        "source_format": str(source_structure["source_format"]),
        "source_structure": source_structure,
        "source_structure_sha256": source_structure_sha256,
        "static_meshes": meshes,
        "imported_objects": imported_objects,
        "texture_count": 1,
        "requested_normalization": requested_normalization,
    }
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "status": "ok",
                "asset_id": asset_id,
                "import_backend": "asset_tools_auto",
                "normalization": {
                    "target_units": "centimeters",
                    "target_up_axis": "Z",
                    "target_handedness": "left_handed",
                    "source_conversion": "delegated_to_engine_importer",
                    "package_pivot_policy": "preserve",
                    "uniform_scale": 1.0,
                },
                "material_postprocess": {"policy": "not_applicable", "materials": []},
                "bundle_sha256": resolved_bundle_sha256,
                "content_sha256": content_sha256,
                "requested_normalization": normalization,
                "source_format": source_structure["source_format"],
                "source_structure": source_structure,
                "source_structure_sha256": source_structure_sha256,
                "ue_package_bundle": package_evidence,
                "imported_object_paths": list(imported_paths),
                "imported_objects": imported_objects,
                "texture_count": 1,
                "static_meshes": meshes,
                "quality": require_static_mesh_quality(
                    quality_input,
                    require_texture_references=require_texture_references,
                ),
            }
        ),
        encoding="utf-8",
    )
    return IngestResult(
        run_dir=run_dir,
        manifest_path=manifest_path,
        ue_log_path=run_dir / "ue.log",
        reload_log_path=run_dir / "ue_reload.log",
        finalize_log_path=run_dir / "ue_finalize.log",
        asset_id=asset_id,
        imported_object_paths=imported_paths,
        static_mesh_paths=static_mesh_paths,
    )


def _commit_fake_thumbnail(
    *,
    settings: Settings,
    database_path: Path,
    asset_id: str,
    ordinal: int,
) -> Any:
    catalog = Catalog(database_path, project_root=settings.project_root)
    record = catalog.get_asset(asset_id)
    assert record is not None and record.status == "imported"
    import_artifact = next(
        item for item in catalog.list_artifacts(asset_id=asset_id) if item.kind == "import_manifest"
    )
    bundle_sha256 = str(import_artifact.params["bundle_sha256"])
    package_evidence = import_artifact.params["ue_package_bundle"]
    assert isinstance(package_evidence, dict)
    ue_package_bundle_sha256 = str(package_evidence["package_bundle_sha256"])
    requested_normalization = dict(import_artifact.params["requested_normalization"])
    run_dir = settings.project_root / "out/fake_thumbnails" / f"{asset_id}_{ordinal}"
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    paths = {
        "thumbnail_beauty": run_dir / "thumbnail.png",
        "thumbnail_mask": run_dir / "subject_mask.png",
        "thumbnail_mask_raw": run_dir / "object_mask.exr",
        "thumbnail_render_manifest": manifest_path,
        "thumbnail_contact_sheet": run_dir / "contact_sheet.png",
    }
    artifact_ids = {kind: f"{asset_id}_{kind}_{ordinal}" for kind in paths}
    for kind, path in paths.items():
        if path != manifest_path:
            if kind in {"thumbnail_beauty", "thumbnail_contact_sheet"}:
                Image.new("RGB", (32, 32), (40, 90, 160)).save(path)
            else:
                path.write_bytes(f"{kind} fixture {ordinal}".encode())
    relative_manifest = manifest_path.relative_to(settings.project_root).as_posix()
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "status": "ok",
                "asset_id": asset_id,
                "asset": {
                    "kind": "catalog",
                    "asset_id": asset_id,
                    "bundle_sha256": bundle_sha256,
                    "ue_package_bundle_sha256": ue_package_bundle_sha256,
                    "content_sha256": record.sha256,
                    "import_manifest": import_artifact.path,
                    "normalization": {"request": requested_normalization},
                },
                "job": {
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
                },
                "thumbnail_validation": _thumbnail_validation_fixture(),
                "scene_sanitization": _scene_sanitization_fixture(),
                "catalog_commit": {
                    "asset_id": asset_id,
                    "target_status": "render_ok",
                    "bundle_sha256": bundle_sha256,
                    "ue_package_bundle_sha256": ue_package_bundle_sha256,
                    "thumbnail_preset": "catalog_thumbnail_v1",
                    "selected_view_index": 0,
                    "requested_normalization": requested_normalization,
                    "import_manifest": import_artifact.path,
                    "artifact_ids": list(artifact_ids.values()),
                },
            }
        ),
        encoding="utf-8",
    )
    render_ok = AssetUpsert(
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
    artifacts = tuple(
        ArtifactUpsert(
            artifact_id=artifact_ids[kind],
            asset_id=asset_id,
            kind=kind,
            path=path,
            params={
                "schema_version": 1,
                "thumbnail_preset": "catalog_thumbnail_v1",
                "render_manifest": relative_manifest,
                "views": 8,
                "resolution": [512, 512],
                "lighting": "three_point",
                "subject_stencil_id": 1,
                "selected_view_index": 0,
                "bundle_sha256": bundle_sha256,
                "ue_package_bundle_sha256": ue_package_bundle_sha256,
                "requested_normalization": requested_normalization,
                "import_manifest": import_artifact.path,
            },
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for kind, path in paths.items()
    )
    catalog.finalize_render(render_ok, artifacts)
    return SimpleNamespace(render=SimpleNamespace(manifest_path=manifest_path))


def test_ingest_batch_imports_all_assets_and_second_run_skips_idempotently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("chair_01", "duck_01"))
    calls: list[str] = []

    def fake_ingest_asset(
        *,
        settings: Settings,
        asset_id: str,
        source_file: Path,
        timeout_sec: int,
        bundle_root: Path,
        bundle_files: tuple[Path, ...],
        expected_bundle_sha256: str,
        expected_content_sha256: str,
        require_single_static_mesh: bool,
        require_texture_references: bool,
        requested_normalization: dict[str, str | float],
        expected_source_structure: dict[str, Any],
        expected_source_structure_sha256: str,
    ) -> IngestResult:
        assert source_file == settings.data_dir / "raw/local" / asset_id / f"{asset_id}.glb"
        assert timeout_sec == 120
        assert bundle_root == source_file.parent
        assert bundle_files == (Path(source_file.name),)
        assert len(expected_bundle_sha256) == 64
        assert require_single_static_mesh is True
        assert require_texture_references is True
        assert requested_normalization["uniform_scale"] == 1.0
        assert expected_source_structure["source_format"] == "glb"
        assert expected_source_structure["ue_hierarchy_preserved"] is False
        assert len(expected_source_structure_sha256) == 64
        calls.append(asset_id)
        return _fake_ingest_result(
            settings,
            asset_id,
            [_mesh(asset_id)],
            bundle_sha256=expected_bundle_sha256,
            content_sha256=expected_content_sha256,
            source_structure=expected_source_structure,
            source_structure_sha256=expected_source_structure_sha256,
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)

    first = ingest_batch(settings=settings, manifest_path=manifest_path, timeout_sec=120)
    second = ingest_batch(settings=settings, manifest_path=manifest_path, timeout_sec=120)

    assert first.status == "ok"
    assert [item.status for item in first.assets] == ["imported", "imported"]
    assert second.status == "ok"
    assert [item.status for item in second.assets] == ["skipped", "skipped"]
    assert calls == ["chair_01", "duck_01"]
    assert first.run_dir != second.run_dir

    catalog = Catalog(first.catalog_path, project_root=settings.project_root)
    assert catalog.stats().as_dict() == {
        "total_assets": 2,
        "total_artifacts": 2,
        "by_status": {"imported": 2},
        "by_source": {"local": 2},
        "by_license": {"CC0-1.0": 2},
        "by_license_tier": {"open": 2},
    }
    for asset_id in ("chair_01", "duck_01"):
        record = catalog.get_asset(asset_id)
        assert record is not None
        assert record.status == "imported"
        assert record.ue_package_path == f"/Game/UEF/Ingested/{asset_id}/SM_{asset_id}"
        assert record.tri_count == 24
        assert record.material_count == 2
        assert record.raw_path == f"data/raw/local/{asset_id}/{asset_id}.glb"
        artifacts = catalog.list_artifacts(asset_id=asset_id)
        assert len(artifacts) == 1
        assert artifacts[0].kind == "import_manifest"
        assert len(artifacts[0].params["bundle_sha256"]) == 64
        assert artifacts[0].params["content_sha256"] == record.sha256
        assert artifacts[0].params["quality_ruleset_version"] == QUALITY_RULESET_VERSION
        assert artifacts[0].params["quality_policy"] == {
            "require_single_static_mesh": True,
            "require_texture_references": True,
        }
        assert artifacts[0].params["schema_version"] == 2
        assert artifacts[0].params["source_structure"]["source_format"] == "glb"
        assert len(artifacts[0].params["source_structure_sha256"]) == 64
        assert len(artifacts[0].params["ue_package_bundle"]["package_bundle_sha256"]) == 64
        import_payload = json.loads(
            (settings.project_root / artifacts[0].path).read_text(encoding="utf-8")
        )
        assert import_payload["source_structure"] == artifacts[0].params["source_structure"]
        assert (
            import_payload["source_structure_sha256"]
            == artifacts[0].params["source_structure_sha256"]
        )
        assert import_payload["ue_package_bundle"] == artifacts[0].params["ue_package_bundle"]

    first_manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    second_manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert first_manifest["status"] == "ok"
    assert first_manifest["catalog"] == "data/catalog.db"
    assert first_manifest["source_manifest"] == "batch.yaml"
    assert [item["status"] for item in first_manifest["assets"]] == [
        "imported",
        "imported",
    ]
    assert all(item["raw_path"].startswith("data/raw/local/") for item in first_manifest["assets"])
    assert [item["status"] for item in second_manifest["assets"]] == [
        "skipped",
        "skipped",
    ]
    assert all(item["ingest_manifest"] is None for item in second_manifest["assets"])


def test_busy_batch_asset_preserves_existing_catalog_and_artifacts_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("locked_asset",))
    ingest_calls = 0

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        nonlocal ingest_calls
        ingest_calls += 1
        return _fake_ingest_result(
            settings,
            "locked_asset",
            [_mesh("locked_asset")],
            bundle_sha256=str(kwargs["expected_bundle_sha256"]),
            content_sha256=str(kwargs["expected_content_sha256"]),
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    successful = ingest_batch(settings=settings, manifest_path=manifest_path)
    assert successful.assets[0].status == "imported"

    catalog = Catalog(successful.catalog_path, project_root=settings.project_root)
    record_before = catalog.get_asset("locked_asset")
    assert record_before is not None
    artifacts_before = tuple(
        item.as_dict() for item in catalog.list_artifacts(asset_id="locked_asset")
    )
    artifact_bytes_before = {
        item["path"]: (settings.project_root / str(item["path"])).read_bytes()
        for item in artifacts_before
    }
    database_bytes_before = successful.catalog_path.read_bytes()

    thread, release, errors = _start_external_asset_lock(
        data_dir=settings.data_dir,
        asset_id="locked_asset",
    )
    try:
        busy = ingest_batch(settings=settings, manifest_path=manifest_path)
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert ingest_calls == 1
    assert busy.status == "failed"
    assert busy.assets[0].status == "failed"
    assert busy.assets[0].catalog_status is None
    assert busy.assets[0].error is not None
    assert busy.assets[0].error["type"] == "AssetLockError"
    assert successful.catalog_path.read_bytes() == database_bytes_before
    assert catalog.get_asset("locked_asset") == record_before
    assert (
        tuple(item.as_dict() for item in catalog.list_artifacts(asset_id="locked_asset"))
        == artifacts_before
    )
    assert {
        path: (settings.project_root / path).read_bytes() for path in artifact_bytes_before
    } == artifact_bytes_before
    busy_manifest = json.loads(busy.manifest_path.read_text(encoding="utf-8"))
    assert busy_manifest["status"] == "failed"
    assert busy_manifest["assets"][0]["catalog_status"] is None
    assert busy_manifest["assets"][0]["error"]["type"] == "AssetLockError"


def test_concurrent_busy_batch_cannot_overwrite_late_import_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("concurrent_asset",))
    ingest_started = threading.Event()
    allow_ingest = threading.Event()
    successful_results: list[Any] = []
    worker_errors: list[BaseException] = []

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        ingest_started.set()
        if not allow_ingest.wait(timeout=10):
            raise TimeoutError("test did not release the successful ingest")
        return _fake_ingest_result(
            settings,
            "concurrent_asset",
            [_mesh("concurrent_asset")],
            bundle_sha256=str(kwargs["expected_bundle_sha256"]),
            content_sha256=str(kwargs["expected_content_sha256"]),
        )

    def run_successful_batch() -> None:
        try:
            successful_results.append(ingest_batch(settings=settings, manifest_path=manifest_path))
        except BaseException as exc:  # pragma: no cover - surfaced below
            worker_errors.append(exc)

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    worker = threading.Thread(target=run_successful_batch)
    worker.start()
    assert ingest_started.wait(timeout=5)
    try:
        busy = ingest_batch(settings=settings, manifest_path=manifest_path)
    finally:
        allow_ingest.set()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert worker_errors == []
    assert len(successful_results) == 1
    successful = successful_results[0]
    assert successful.status == "ok"
    assert successful.assets[0].status == "imported"
    assert busy.status == "failed"
    assert busy.assets[0].error is not None
    assert busy.assets[0].error["type"] == "AssetLockError"

    catalog = Catalog(settings.data_dir / "catalog.db", project_root=settings.project_root)
    record = catalog.get_asset("concurrent_asset")
    assert record is not None and record.status == "imported"
    assert record.error is None
    artifacts = catalog.list_artifacts(asset_id="concurrent_asset")
    assert len(artifacts) == 1 and artifacts[0].kind == "import_manifest"


def test_batch_asset_lock_releases_after_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("interrupted_asset",))

    def interrupt_ingest(**kwargs: Any) -> None:
        del kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", interrupt_ingest)

    with pytest.raises(KeyboardInterrupt):
        ingest_batch(settings=settings, manifest_path=manifest_path)

    thread, release, errors = _start_external_asset_lock(
        data_dir=settings.data_dir,
        asset_id="interrupted_asset",
    )
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []


def test_ingest_batch_reimports_when_bundle_paths_change_with_identical_contents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("path_sensitive",))
    source_dir = settings.project_root / "sources"
    old_dependency = source_dir / "textures/old_name.png"
    old_dependency.parent.mkdir()
    old_dependency.write_bytes(b"identical texture bytes")
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    raw["assets"][0]["dependencies"] = ["textures/old_name.png"]
    manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        bundle = str(kwargs["expected_bundle_sha256"])
        content = str(kwargs["expected_content_sha256"])
        calls.append((bundle, content))
        asset_id = str(kwargs["asset_id"])
        return _fake_ingest_result(
            settings,
            asset_id,
            [_mesh(asset_id)],
            bundle_sha256=bundle,
            content_sha256=content,
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    first = ingest_batch(settings=settings, manifest_path=manifest_path)

    shutil.rmtree(settings.data_dir / "raw/local/path_sensitive")
    new_dependency = old_dependency.with_name("new_name.png")
    old_dependency.rename(new_dependency)
    raw["assets"][0]["dependencies"] = ["textures/new_name.png"]
    manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    second = ingest_batch(settings=settings, manifest_path=manifest_path)

    assert first.assets[0].status == "imported"
    assert second.assets[0].status == "imported"
    assert len(calls) == 2
    assert calls[0][0] != calls[1][0]
    assert calls[0][1] == calls[1][1]
    catalog = Catalog(second.catalog_path, project_root=settings.project_root)
    artifacts = catalog.list_artifacts(asset_id="path_sensitive")
    assert len(artifacts) == 1
    assert artifacts[0].params["bundle_sha256"] == calls[1][0]


def test_ingest_batch_records_one_asset_failure_and_continues_with_next(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(
        settings.project_root,
        ("broken_asset", "good_asset"),
    )
    calls: list[str] = []

    def fake_ingest_asset(
        *,
        settings: Settings,
        asset_id: str,
        source_file: Path,
        timeout_sec: int,
        bundle_root: Path,
        bundle_files: tuple[Path, ...],
        expected_bundle_sha256: str,
        expected_content_sha256: str,
        require_single_static_mesh: bool,
        require_texture_references: bool,
        requested_normalization: dict[str, str | float],
        expected_source_structure: dict[str, Any],
        expected_source_structure_sha256: str,
    ) -> IngestResult:
        del (
            source_file,
            timeout_sec,
            bundle_root,
            bundle_files,
            require_single_static_mesh,
            require_texture_references,
            requested_normalization,
            expected_source_structure,
            expected_source_structure_sha256,
        )
        calls.append(asset_id)
        if asset_id == "broken_asset":
            raise RuntimeError("UE quality gate rejected fixture")
        return _fake_ingest_result(
            settings,
            asset_id,
            [_mesh(asset_id)],
            bundle_sha256=expected_bundle_sha256,
            content_sha256=expected_content_sha256,
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)

    result = ingest_batch(settings=settings, manifest_path=manifest_path)

    assert result.status == "failed"
    assert calls == ["broken_asset", "good_asset"]
    assert [item.status for item in result.assets] == ["failed", "imported"]
    assert result.assets[0].ingest_manifest is None
    assert result.assets[0].error == {
        "type": "RuntimeError",
        "message": "UE quality gate rejected fixture",
    }
    assert result.assets[1].error is None

    catalog = Catalog(result.catalog_path, project_root=settings.project_root)
    broken = catalog.get_asset("broken_asset")
    good = catalog.get_asset("good_asset")
    assert broken is not None
    assert broken.status == "failed"
    assert broken.error == result.assets[0].error
    assert broken.ue_package_path is None
    assert good is not None
    assert good.status == "imported"
    assert good.error is None
    assert catalog.list_artifacts(asset_id="broken_asset") == ()
    assert len(catalog.list_artifacts(asset_id="good_asset")) == 1
    assert catalog.stats().by_status == {"failed": 1, "imported": 1}

    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert [item["asset_id"] for item in payload["assets"]] == [
        "broken_asset",
        "good_asset",
    ]
    assert payload["assets"][0]["status"] == "failed"
    assert payload["assets"][0]["error"] == result.assets[0].error
    assert payload["assets"][0]["ingest_manifest"] is None
    assert payload["assets"][1]["status"] == "imported"
    assert payload["assets"][1]["error"] is None


def test_ingest_batch_repairs_missing_package_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("repair_asset",))
    calls: list[str] = []

    def fake_ingest_asset(
        *,
        settings: Settings,
        asset_id: str,
        source_file: Path,
        timeout_sec: int,
        bundle_root: Path,
        bundle_files: tuple[Path, ...],
        expected_bundle_sha256: str,
        expected_content_sha256: str,
        require_single_static_mesh: bool,
        require_texture_references: bool,
        requested_normalization: dict[str, str | float],
        expected_source_structure: dict[str, Any],
        expected_source_structure_sha256: str,
    ) -> IngestResult:
        del (
            source_file,
            timeout_sec,
            bundle_root,
            bundle_files,
            require_single_static_mesh,
            require_texture_references,
            requested_normalization,
            expected_source_structure,
            expected_source_structure_sha256,
        )
        calls.append(asset_id)
        return _fake_ingest_result(
            settings,
            asset_id,
            [_mesh(asset_id)],
            bundle_sha256=expected_bundle_sha256,
            content_sha256=expected_content_sha256,
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    first = ingest_batch(settings=settings, manifest_path=manifest_path)
    package = (
        settings.project_root
        / "ue/UEFBase/Content/UEF/Ingested/repair_asset/SM_repair_asset.uasset"
    )
    package.unlink()

    repaired = ingest_batch(settings=settings, manifest_path=manifest_path)

    assert first.assets[0].status == "imported"
    assert repaired.assets[0].status == "imported"
    assert package.is_file()
    assert calls == ["repair_asset", "repair_asset"]


def test_ingest_batch_reimports_tampered_package_bytes_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("tampered_package",))
    calls: list[str] = []

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        asset_id = str(kwargs["asset_id"])
        calls.append(asset_id)
        return _fake_ingest_result(
            settings,
            asset_id,
            [_mesh(asset_id)],
            bundle_sha256=str(kwargs["expected_bundle_sha256"]),
            content_sha256=str(kwargs["expected_content_sha256"]),
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    first = ingest_batch(settings=settings, manifest_path=manifest_path)
    package = (
        settings.project_root
        / "ue/UEFBase/Content/UEF/Ingested/tampered_package/SM_tampered_package.uasset"
    )
    package.write_bytes(b"tampered package generation")

    repaired = ingest_batch(settings=settings, manifest_path=manifest_path)
    repeated = ingest_batch(settings=settings, manifest_path=manifest_path)

    assert first.assets[0].status == "imported"
    assert repaired.assets[0].status == "imported"
    assert repeated.assets[0].status == "skipped"
    assert calls == ["tampered_package", "tampered_package"]
    catalog = Catalog(repaired.catalog_path, project_root=settings.project_root)
    artifact = catalog.list_artifacts(asset_id="tampered_package")[0]
    payload = json.loads((settings.project_root / artifact.path).read_text(encoding="utf-8"))
    assert artifact.params["ue_package_bundle"] == payload["ue_package_bundle"]


def test_ingest_batch_reimports_stale_quality_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("stale_quality",))
    calls: list[str] = []

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        asset_id = str(kwargs["asset_id"])
        calls.append(asset_id)
        return _fake_ingest_result(
            settings,
            asset_id,
            [_mesh(asset_id)],
            bundle_sha256=str(kwargs["expected_bundle_sha256"]),
            content_sha256=str(kwargs["expected_content_sha256"]),
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    first = ingest_batch(settings=settings, manifest_path=manifest_path)
    catalog = Catalog(first.catalog_path, project_root=settings.project_root)
    artifact = catalog.list_artifacts(asset_id="stale_quality")[0]
    import_manifest = settings.project_root / artifact.path
    payload = json.loads(import_manifest.read_text(encoding="utf-8"))
    payload["quality"]["ruleset_version"] = "m2_static_mesh_v0"
    import_manifest.write_text(json.dumps(payload), encoding="utf-8")
    catalog.upsert_artifact(
        ArtifactUpsert(
            artifact_id=artifact.artifact_id,
            asset_id=artifact.asset_id,
            kind=artifact.kind,
            path=artifact.path,
            params=artifact.params,
            sha256=hashlib.sha256(import_manifest.read_bytes()).hexdigest(),
        )
    )

    repaired = ingest_batch(settings=settings, manifest_path=manifest_path)

    assert first.assets[0].status == "imported"
    assert repaired.assets[0].status == "imported"
    assert calls == ["stale_quality", "stale_quality"]
    repaired_payload = json.loads(import_manifest.read_text(encoding="utf-8"))
    assert repaired_payload["quality"]["ruleset_version"] == QUALITY_RULESET_VERSION


def test_ingest_batch_reimports_forged_source_structure_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("forged_structure",))
    calls: list[str] = []

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        asset_id = str(kwargs["asset_id"])
        calls.append(asset_id)
        return _fake_ingest_result(
            settings,
            asset_id,
            [_mesh(asset_id)],
            bundle_sha256=str(kwargs["expected_bundle_sha256"]),
            content_sha256=str(kwargs["expected_content_sha256"]),
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    first = ingest_batch(settings=settings, manifest_path=manifest_path)
    catalog = Catalog(first.catalog_path, project_root=settings.project_root)
    artifact = catalog.list_artifacts(asset_id="forged_structure")[0]
    import_manifest = settings.project_root / artifact.path
    payload = json.loads(import_manifest.read_text(encoding="utf-8"))
    forged_structure = dict(payload["source_structure"])
    forged_structure["ue_hierarchy_preserved"] = True
    forged_digest = source_structure_sha256(forged_structure)
    payload["source_structure"] = forged_structure
    payload["source_structure_sha256"] = forged_digest
    import_manifest.write_text(json.dumps(payload), encoding="utf-8")
    catalog.upsert_artifact(
        ArtifactUpsert(
            artifact_id=artifact.artifact_id,
            asset_id=artifact.asset_id,
            kind=artifact.kind,
            path=artifact.path,
            params={
                **artifact.params,
                "source_structure": forged_structure,
                "source_structure_sha256": forged_digest,
            },
            sha256=hashlib.sha256(import_manifest.read_bytes()).hexdigest(),
        )
    )

    repaired = ingest_batch(settings=settings, manifest_path=manifest_path)

    assert repaired.assets[0].status == "imported"
    assert calls == ["forged_structure", "forged_structure"]
    repaired_artifact = catalog.list_artifacts(asset_id="forged_structure")[0]
    assert repaired_artifact.params["source_structure"]["ue_hierarchy_preserved"] is False


def test_ingest_batch_reimports_when_requested_normalization_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("scaled_asset",))
    calls: list[float] = []

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        normalization = dict(kwargs["requested_normalization"])
        calls.append(float(normalization["uniform_scale"]))
        return _fake_ingest_result(
            settings,
            str(kwargs["asset_id"]),
            [_mesh(str(kwargs["asset_id"]))],
            bundle_sha256=str(kwargs["expected_bundle_sha256"]),
            content_sha256=str(kwargs["expected_content_sha256"]),
            requested_normalization=normalization,
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    first = ingest_batch(settings=settings, manifest_path=manifest_path)
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    raw["assets"][0]["normalization"]["uniform_scale"] = 2.0
    manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    changed = ingest_batch(settings=settings, manifest_path=manifest_path)
    repeated = ingest_batch(settings=settings, manifest_path=manifest_path)

    assert first.assets[0].status == "imported"
    assert changed.assets[0].status == "imported"
    assert repeated.assets[0].status == "skipped"
    assert calls == [1.0, 2.0]
    catalog = Catalog(changed.catalog_path, project_root=settings.project_root)
    assert any(
        item.params["requested_normalization"]["uniform_scale"] == 2.0
        for item in catalog.list_artifacts(asset_id="scaled_asset")
    )


def test_ingest_batch_reimports_when_texture_quality_policy_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("policy_asset",))
    calls: list[bool] = []

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        required = bool(kwargs["require_texture_references"])
        calls.append(required)
        return _fake_ingest_result(
            settings,
            str(kwargs["asset_id"]),
            [_mesh(str(kwargs["asset_id"]))],
            bundle_sha256=str(kwargs["expected_bundle_sha256"]),
            content_sha256=str(kwargs["expected_content_sha256"]),
            require_texture_references=required,
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    first = ingest_batch(settings=settings, manifest_path=manifest_path)
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    raw["assets"][0]["tags"].remove("textured")
    raw["assets"][0]["tags"].append("untextured")
    manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    changed = ingest_batch(settings=settings, manifest_path=manifest_path)

    assert first.assets[0].status == "imported"
    assert changed.assets[0].status == "imported"
    assert calls == [True, False]
    catalog = Catalog(changed.catalog_path, project_root=settings.project_root)
    artifact = catalog.list_artifacts(asset_id="policy_asset")[0]
    assert artifact.params["quality_policy"] == {
        "require_single_static_mesh": True,
        "require_texture_references": False,
    }


def test_quality_failure_manifest_is_preserved_in_batch_and_catalog_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("bad_quality",))
    failed_manifest = settings.project_root / "out/fake_ue/bad_quality/manifest.json"
    evidence = inspect_source_structure(settings.project_root / "sources/bad_quality.glb")
    report = require_static_mesh_quality(
        {
            "source_format": "glb",
            "source_structure": evidence.payload,
            "source_structure_sha256": evidence.sha256,
            "static_meshes": [_mesh("bad_quality")],
            "texture_count": 1,
            "imported_objects": [
                {
                    "object_path": "/Game/UEF/Ingested/bad_quality/SM_bad_quality",
                    "class": "StaticMesh",
                },
                {
                    "object_path": "/Game/UEF/Ingested/bad_quality/M_bad_quality",
                    "class": "Material",
                },
            ],
        },
        require_texture_references=False,
    )
    report["status"] = "failed"
    report["checks"]["material_references"]["status"] = "failed"

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        del kwargs
        failed_manifest.parent.mkdir(parents=True)
        failed_manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "failed",
                    "asset_id": "bad_quality",
                    "static_meshes": [_mesh("bad_quality")],
                    "quality": report,
                }
            ),
            encoding="utf-8",
        )
        error = IngestQualityError(report)
        error.manifest_path = failed_manifest
        raise error

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)

    result = ingest_batch(settings=settings, manifest_path=manifest_path)

    expected_relative = "out/fake_ue/bad_quality/manifest.json"
    failed = result.assets[0]
    assert failed.status == "failed"
    assert failed.ingest_manifest == failed_manifest
    assert failed.error is not None
    assert failed.error["type"] == "IngestQualityError"
    assert failed.error["quality"] == report
    assert failed.error["ingest_manifest"] == expected_relative
    batch_payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert batch_payload["assets"][0]["ingest_manifest"] == expected_relative
    catalog = Catalog(result.catalog_path, project_root=settings.project_root)
    record = catalog.get_asset("bad_quality")
    assert record is not None and record.status == "failed"
    assert record.error == failed.error


def test_ingest_batch_supplements_thumbnail_then_skips_complete_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("thumbnail_asset",))
    ingest_calls: list[str] = []
    thumbnail_calls: list[str] = []

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        asset_id = str(kwargs["asset_id"])
        ingest_calls.append(asset_id)
        return _fake_ingest_result(
            settings,
            asset_id,
            [_mesh(asset_id)],
            bundle_sha256=str(kwargs["expected_bundle_sha256"]),
            content_sha256=str(kwargs["expected_content_sha256"]),
        )

    def fake_thumbnail(**kwargs: Any) -> Any:
        asset_id = str(kwargs["asset_id"])
        thumbnail_calls.append(asset_id)
        return _commit_fake_thumbnail(
            settings=settings,
            database_path=Path(kwargs["database_path"]),
            asset_id=asset_id,
            ordinal=len(thumbnail_calls),
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    monkeypatch.setattr("uefactory.ingest.pipeline.thumbnail_catalog_asset", fake_thumbnail)

    imported = ingest_batch(settings=settings, manifest_path=manifest_path)
    supplemented = ingest_batch(
        settings=settings,
        manifest_path=manifest_path,
        render_thumbnails=True,
    )
    repeated = ingest_batch(
        settings=settings,
        manifest_path=manifest_path,
        render_thumbnails=True,
    )

    assert imported.assets[0].status == "imported"
    assert supplemented.assets[0].status == "render_ok"
    assert supplemented.assets[0].thumbnail_manifest is not None
    assert repeated.assets[0].status == "skipped"
    assert ingest_calls == ["thumbnail_asset"]
    assert thumbnail_calls == ["thumbnail_asset"]
    catalog = Catalog(supplemented.catalog_path, project_root=settings.project_root)
    record = catalog.get_asset("thumbnail_asset")
    assert record is not None and record.status == "render_ok"
    supplemented_payload = json.loads(supplemented.manifest_path.read_text(encoding="utf-8"))
    assert supplemented_payload["report_error"] is None
    assert supplemented_payload["report"]["contact_sheet"].endswith("/report/contact_sheet.png")
    assert supplemented_payload["report"]["index_html"].endswith("/report/index.html")
    assert len(supplemented_payload["report"]["thumbnails"]) == 1
    assert (supplemented.run_dir / "report/contact_sheet.png").is_file()
    assert (supplemented.run_dir / "report/index.html").is_file()


def test_ingest_batch_downgrades_corrupt_render_before_failed_thumbnail_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("corrupt_thumbnail",))

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        asset_id = str(kwargs["asset_id"])
        return _fake_ingest_result(
            settings,
            asset_id,
            [_mesh(asset_id)],
            bundle_sha256=str(kwargs["expected_bundle_sha256"]),
            content_sha256=str(kwargs["expected_content_sha256"]),
        )

    def successful_thumbnail(**kwargs: Any) -> Any:
        return _commit_fake_thumbnail(
            settings=settings,
            database_path=Path(kwargs["database_path"]),
            asset_id=str(kwargs["asset_id"]),
            ordinal=1,
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    monkeypatch.setattr(
        "uefactory.ingest.pipeline.thumbnail_catalog_asset",
        successful_thumbnail,
    )
    first = ingest_batch(
        settings=settings,
        manifest_path=manifest_path,
        render_thumbnails=True,
    )
    catalog = Catalog(first.catalog_path, project_root=settings.project_root)
    beauty = next(
        item
        for item in catalog.list_artifacts(asset_id="corrupt_thumbnail")
        if item.kind == "thumbnail_beauty"
    )
    (settings.project_root / beauty.path).write_bytes(b"tampered")

    def failed_thumbnail(**kwargs: Any) -> Any:
        current = Catalog(
            Path(kwargs["database_path"]), project_root=settings.project_root
        ).get_asset("corrupt_thumbnail")
        assert current is not None and current.status == "imported"
        raise RuntimeError("thumbnail repair failed")

    monkeypatch.setattr(
        "uefactory.ingest.pipeline.thumbnail_catalog_asset",
        failed_thumbnail,
    )
    repaired = ingest_batch(
        settings=settings,
        manifest_path=manifest_path,
        render_thumbnails=True,
    )

    assert repaired.status == "failed"
    assert repaired.assets[0].error == {
        "type": "RuntimeError",
        "message": "thumbnail repair failed",
        "phase": "thumbnail",
    }
    record = catalog.get_asset("corrupt_thumbnail")
    assert record is not None and record.status == "imported"


def test_ingest_batch_rejects_obsolete_and_forged_thumbnail_groups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("strict_thumbnail",))
    thumbnail_calls: list[str] = []

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        asset_id = str(kwargs["asset_id"])
        return _fake_ingest_result(
            settings,
            asset_id,
            [_mesh(asset_id)],
            bundle_sha256=str(kwargs["expected_bundle_sha256"]),
            content_sha256=str(kwargs["expected_content_sha256"]),
        )

    def fake_thumbnail(**kwargs: Any) -> Any:
        asset_id = str(kwargs["asset_id"])
        thumbnail_calls.append(asset_id)
        return _commit_fake_thumbnail(
            settings=settings,
            database_path=Path(kwargs["database_path"]),
            asset_id=asset_id,
            ordinal=len(thumbnail_calls),
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    monkeypatch.setattr("uefactory.ingest.pipeline.thumbnail_catalog_asset", fake_thumbnail)
    first = ingest_batch(
        settings=settings,
        manifest_path=manifest_path,
        render_thumbnails=True,
    )
    catalog = Catalog(first.catalog_path, project_root=settings.project_root)

    for artifact in catalog.list_artifacts(asset_id="strict_thumbnail"):
        if artifact.kind.startswith("thumbnail_"):
            catalog.upsert_artifact(
                ArtifactUpsert(
                    artifact_id=artifact.artifact_id,
                    asset_id=artifact.asset_id,
                    kind=artifact.kind,
                    path=artifact.path,
                    params={
                        **artifact.params,
                        "ue_package_bundle_sha256": "0" * 64,
                    },
                    sha256=artifact.sha256,
                )
            )
    generation_repaired = ingest_batch(
        settings=settings,
        manifest_path=manifest_path,
        render_thumbnails=True,
    )

    for artifact in catalog.list_artifacts(asset_id="strict_thumbnail"):
        catalog.upsert_artifact(
            ArtifactUpsert(
                artifact_id=artifact.artifact_id,
                asset_id=artifact.asset_id,
                kind=artifact.kind,
                path=artifact.path,
                params={**artifact.params, "thumbnail_preset": "obsolete_v0"},
                sha256=artifact.sha256,
            )
        )
    obsolete_repaired = ingest_batch(
        settings=settings,
        manifest_path=manifest_path,
        render_thumbnails=True,
    )

    for artifact in catalog.list_artifacts(asset_id="strict_thumbnail"):
        if artifact.artifact_id.endswith("_3"):
            catalog.upsert_artifact(
                ArtifactUpsert(
                    artifact_id=artifact.artifact_id,
                    asset_id=artifact.asset_id,
                    kind=artifact.kind,
                    path=artifact.path,
                    params={**artifact.params, "render_manifest": "out/forged.json"},
                    sha256=artifact.sha256,
                )
            )
    forged_repaired = ingest_batch(
        settings=settings,
        manifest_path=manifest_path,
        render_thumbnails=True,
    )

    assert generation_repaired.assets[0].status == "render_ok"
    assert obsolete_repaired.assets[0].status == "render_ok"
    assert forged_repaired.assets[0].status == "render_ok"
    assert thumbnail_calls == ["strict_thumbnail"] * 4


def test_ingest_batch_marks_batch_failed_when_final_report_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_batch_manifest(settings.project_root, ("report_failure",))

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        asset_id = str(kwargs["asset_id"])
        return _fake_ingest_result(
            settings,
            asset_id,
            [_mesh(asset_id)],
            bundle_sha256=str(kwargs["expected_bundle_sha256"]),
            content_sha256=str(kwargs["expected_content_sha256"]),
        )

    def fake_thumbnail(**kwargs: Any) -> Any:
        return _commit_fake_thumbnail(
            settings=settings,
            database_path=Path(kwargs["database_path"]),
            asset_id=str(kwargs["asset_id"]),
            ordinal=1,
        )

    def failed_report(**kwargs: Any) -> None:
        raise RuntimeError("synthetic report failure")

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)
    monkeypatch.setattr("uefactory.ingest.pipeline.thumbnail_catalog_asset", fake_thumbnail)
    monkeypatch.setattr("uefactory.ingest.pipeline.create_batch_report", failed_report)

    result = ingest_batch(
        settings=settings,
        manifest_path=manifest_path,
        render_thumbnails=True,
    )

    assert result.status == "failed"
    assert result.assets[0].status == "render_ok"
    assert result.assets[0].catalog_status == "render_ok"
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["report"] is None
    assert payload["report_error"] == {
        "type": "RuntimeError",
        "message": "synthetic report failure",
        "phase": "batch_report",
    }
    catalog = Catalog(result.catalog_path, project_root=settings.project_root)
    record = catalog.get_asset("report_failure")
    assert record is not None and record.status == "render_ok"


def test_ingest_batch_succeeds_with_project_relative_data_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    settings = Settings(
        project_root=project_root,
        ue_root=Path("engine"),
        ue_home=Path("ue-home"),
        data_dir=Path("relative-data"),
        log_dir=Path("relative-logs"),
        ddc_dir=Path("relative-ddc"),
        runtime_lib_dir=Path("runtime/lib"),
    )
    manifest_path = _write_batch_manifest(project_root, ("relative_asset",))

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        assert kwargs["settings"].data_dir == project_root / "relative-data"
        assert Path(kwargs["source_file"]).is_relative_to(project_root / "relative-data")
        return _fake_ingest_result(
            settings,
            "relative_asset",
            [_mesh("relative_asset")],
            bundle_sha256=str(kwargs["expected_bundle_sha256"]),
            content_sha256=str(kwargs["expected_content_sha256"]),
        )

    monkeypatch.setattr("uefactory.ingest.pipeline.ingest_asset", fake_ingest_asset)

    result = ingest_batch(settings=settings, manifest_path=manifest_path)

    assert result.status == "ok"
    assert result.assets[0].status == "imported"
    assert result.catalog_path == project_root / "relative-data/catalog.db"
    assert result.catalog_path.is_file()
    assert result.assets[0].raw_path is not None
    assert result.assets[0].raw_path.is_relative_to(project_root / "relative-data")
    assert (project_root / "relative-data/locks/assets/relative_asset.lock").is_file()


def test_ingest_batch_fails_before_writing_when_data_dir_is_external(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    external_data = tmp_path / "external_data"
    settings = Settings(
        project_root=project_root,
        data_dir=external_data,
        log_dir=project_root / "logs",
    )
    manifest_path = _write_batch_manifest(project_root, ("external_asset",))

    with pytest.raises(ValueError, match="data_dir to be inside project_root"):
        ingest_batch(settings=settings, manifest_path=manifest_path)

    assert not external_data.exists()
