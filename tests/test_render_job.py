from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from uefactory.catalog import (
    ArtifactUpsert,
    AssetUpsert,
    Catalog,
    SceneArtifactUpsert,
    SceneObjectUpsert,
    SceneUpsert,
)
from uefactory.core.asset_locking import AssetLockError, asset_lock
from uefactory.core.config import Settings
from uefactory.core.ingest_contracts import QUALITY_CHECK_NAMES
from uefactory.ingest.package_evidence import collect_package_bundle_evidence
from uefactory.ingest.source_structure import inspect_source_structure
from uefactory.render.job import (
    _canonical_digest,
    _catalog_geometry_payload,
    _new_run_id,
    _render_asset_payload,
    _ue_job_payload_with_lighting,
    _validate_scene_sanitization,
    render_job,
)
from uefactory.render.jobspec import RenderJobSpec, load_jobspec
from uefactory.render.ue_runner import LogSummary, UERunnerError, UERunResult


def test_new_run_id_is_unique_for_consecutive_calls_in_same_timestamp(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("uefactory.render.job.utc_timestamp", lambda: "20260710T120000Z")

    run_ids = [_new_run_id() for _ in range(8)]

    assert len(set(run_ids)) == len(run_ids)
    assert all(run_id.startswith("20260710T120000Z_") for run_id in run_ids)


def test_hdri_payload_uses_a_separate_beauty_sequence(tmp_path: Path) -> None:
    _, job_path = _local_render_fixture(tmp_path)
    job_path.write_text(
        job_path.read_text(encoding="utf-8").replace(
            "  preset: three_point",
            "  preset: hdri\n  hdri: studio_small_03_1k",
        ),
        encoding="utf-8",
    )
    spec = load_jobspec(job_path)
    run_id = "20260710T120000Z_deadbeef"
    sequence_path = f"/Game/UEF/RenderJobs/{run_id}/UEF_RenderJob_{run_id}.UEF_RenderJob_{run_id}"

    payload = _ue_job_payload_with_lighting(
        spec=spec,
        run_id=run_id,
        run_dir=tmp_path / "out",
        sequence_path=sequence_path,
        lighting={"preset": "hdri", "hdri": "studio_small_03_1k"},
    )

    assert payload["sequence_path"] == sequence_path
    assert payload["beauty_sequence_path"] == (
        f"/Game/UEF/RenderJobs/{run_id}/UEF_RenderJobBeauty_{run_id}.UEF_RenderJobBeauty_{run_id}"
    )
    assert payload["asset"]["kind"] == "builtin"


def test_catalog_geometry_centers_grounds_and_frames_asset() -> None:
    payload = _catalog_geometry_payload(
        {
            "min": [-10.0, 20.0, -5.0],
            "max": [30.0, 80.0, 95.0],
            "size": [40.0, 60.0, 100.0],
        },
        resolution=(512, 512),
        horizontal_fov_deg=45.0,
    )

    assert payload["actor_location_cm"] == [-10.0, -50.0, 5.0]
    assert payload["camera_target_cm"] == [0.0, 0.0, 50.0]
    assert payload["floor_location_z_cm"] == -2.5
    assert payload["camera_near_clip_cm"] == 0.1
    assert payload["actor_scale"] == [1.0, 1.0, 1.0]
    assert payload["camera_radius_cm"] > 100.0
    assert payload["normalization"]["pivot_policy"] == "bounds_bottom_center_to_origin"


def test_catalog_geometry_applies_requested_uniform_scale_to_logical_framing() -> None:
    payload = _catalog_geometry_payload(
        {
            "min": [-10.0, 20.0, -5.0],
            "max": [30.0, 80.0, 95.0],
            "size": [40.0, 60.0, 100.0],
        },
        resolution=(512, 512),
        horizontal_fov_deg=45.0,
        requested_normalization={
            "source_units": "auto",
            "source_up_axis": "auto",
            "source_handedness": "auto",
            "uniform_scale": 2.0,
            "pivot_policy": "preserve_source",
        },
    )

    assert payload["bounds_cm"]["size"] == [40.0, 60.0, 100.0]
    assert payload["actor_scale"] == [2.0, 2.0, 2.0]
    assert payload["actor_location_cm"] == [-20.0, -100.0, 10.0]
    assert payload["camera_target_cm"] == [0.0, 0.0, 100.0]
    assert payload["normalization"]["logical_size_cm"] == [80.0, 120.0, 200.0]


def test_catalog_geometry_rejects_inconsistent_reported_size() -> None:
    with pytest.raises(RuntimeError, match="does not match max-min"):
        _catalog_geometry_payload(
            {
                "min": [0.0, 0.0, 0.0],
                "max": [10.0, 20.0, 30.0],
                "size": [10.0, 999.0, 30.0],
            },
            resolution=(512, 512),
            horizontal_fov_deg=45.0,
        )


def test_render_asset_payload_resolves_catalog_manifest_and_packages(tmp_path: Path) -> None:
    settings, job_path = _local_render_fixture(tmp_path)
    job_path.write_text(
        job_path.read_text(encoding="utf-8").replace("builtin:cube", "test_asset"),
        encoding="utf-8",
    )
    spec = load_jobspec(job_path)
    object_path = "/Game/UEF/Ingested/test_asset/SM_Test.SM_Test"
    package = settings.project_root / "ue/UEFBase/Content/UEF/Ingested/test_asset/SM_Test.uasset"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"uasset")
    import_manifest = settings.project_root / "out/ingest/test_asset/manifest.json"
    import_manifest.parent.mkdir(parents=True)
    requested_normalization = {
        "source_units": "auto",
        "source_up_axis": "auto",
        "source_handedness": "auto",
        "uniform_scale": 2.0,
        "pivot_policy": "preserve_source",
    }
    engine_normalization = {
        "target_units": "centimeters",
        "target_up_axis": "Z",
        "target_handedness": "left_handed",
        "source_conversion": "delegated_to_engine_importer",
        "package_pivot_policy": "preserve",
        "uniform_scale": 1.0,
    }
    source = tmp_path / "source.gltf"
    source.write_text(
        json.dumps(
            {
                "asset": {"version": "2.0"},
                "scene": 0,
                "scenes": [{"nodes": [0]}],
                "nodes": [{"mesh": 0}],
                "meshes": [{}],
            }
        ),
        encoding="utf-8",
    )
    source_structure = inspect_source_structure(source)
    package_evidence = collect_package_bundle_evidence(
        settings.project_root,
        asset_id="test_asset",
        imported_object_paths=[object_path],
    )
    import_manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "status": "ok",
                "asset_id": "test_asset",
                "import_backend": "asset_tools_auto",
                "normalization": engine_normalization,
                "requested_normalization": requested_normalization,
                "material_postprocess": {"policy": "not_applicable", "materials": []},
                "bundle_sha256": "b" * 64,
                "source_structure": source_structure.payload,
                "source_structure_sha256": source_structure.sha256,
                "ue_package_bundle": package_evidence,
                "quality": {
                    "ruleset_version": "m2_static_mesh_v2",
                    "policy": {
                        "require_single_static_mesh": True,
                        "require_texture_references": False,
                    },
                    "status": "passed",
                    "checks": {name: {"status": "passed"} for name in QUALITY_CHECK_NAMES},
                },
                "content_sha256": "a" * 64,
                "transaction": {"state": "committed"},
                "imported_object_paths": [object_path],
                "static_meshes": [
                    {
                        "object_path": object_path,
                        "triangle_count": 12,
                        "material_count": 1,
                        "bounds_cm": {
                            "min": [-50.0, -25.0, 0.0],
                            "max": [50.0, 25.0, 200.0],
                            "size": [100.0, 50.0, 200.0],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    catalog = Catalog(settings.data_dir / "catalog.db", project_root=settings.project_root)
    catalog.finalize_import(
        AssetUpsert(
            asset_id="test_asset",
            name="Test Asset",
            source="local",
            source_id="test-asset",
            source_url="https://example.test/test-asset",
            license="CC0-1.0",
            license_tier="open",
            license_url="https://creativecommons.org/publicdomain/zero/1.0/",
            raw_path="data/raw/test_asset/model.gltf",
            sha256="a" * 64,
            status="imported",
            ue_package_path=object_path,
            tri_count=12,
            material_count=1,
        ),
        ArtifactUpsert(
            artifact_id="test_asset_import_manifest",
            asset_id="test_asset",
            kind="import_manifest",
            path=import_manifest,
            params={
                "schema_version": 2,
                "source_format": "gltf",
                "bundle_sha256": "b" * 64,
                "content_sha256": "a" * 64,
                "quality_ruleset_version": "m2_static_mesh_v2",
                "quality_policy": {
                    "require_single_static_mesh": True,
                    "require_texture_references": False,
                },
                "requested_normalization": requested_normalization,
                "import_backend": "asset_tools_auto",
                "engine_normalization": engine_normalization,
                "material_postprocess_policy": "not_applicable",
                "source_structure": source_structure.payload,
                "source_structure_sha256": source_structure.sha256,
                "ue_package_bundle": package_evidence,
            },
            sha256=hashlib.sha256(import_manifest.read_bytes()).hexdigest(),
        ),
    )

    payload = _render_asset_payload(settings, spec)

    assert payload["kind"] == "catalog"
    assert payload["mesh_path"] == object_path
    assert payload["bundle_sha256"] == "b" * 64
    assert payload["ue_package_bundle_sha256"] == package_evidence["package_bundle_sha256"]
    assert payload["preserve_materials"] is True
    assert payload["actor_scale"] == [2.0, 2.0, 2.0]
    assert payload["actor_location_cm"] == [0.0, 0.0, 0.0]
    assert payload["camera_target_cm"] == [0.0, 0.0, 200.0]

    package.write_bytes(b"tampered uasset bytes")
    with pytest.raises(RuntimeError, match="no valid import manifest/package inventory"):
        _render_asset_payload(settings, spec)


def test_model_render_busy_lock_rejects_before_resolver_or_ue(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)
    job_path.write_text(
        job_path.read_text(encoding="utf-8").replace("builtin:cube", "test_asset"),
        encoding="utf-8",
    )
    resolver_called = False
    ue_called = False

    def unexpected_resolver(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        nonlocal resolver_called
        resolver_called = True
        pytest.fail("model resolver must not run while the asset lock is busy")

    def unexpected_ue(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        nonlocal ue_called
        ue_called = True
        pytest.fail("UE must not run while the asset lock is busy")

    monkeypatch.setattr("uefactory.render.job._render_asset_payload", unexpected_resolver)
    monkeypatch.setattr("uefactory.render.job.run_ue", unexpected_ue)

    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_lock() -> None:
        try:
            with asset_lock(data_dir=settings.data_dir, asset_id="test_asset"):
                acquired.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test did not release the external asset lock")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
            acquired.set()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert acquired.wait(timeout=5)
    try:
        with pytest.raises(AssetLockError, match="another ingest or render owns"):
            render_job(settings=settings, job_path=job_path, timeout_sec=60)
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert resolver_called is False
    assert ue_called is False
    assert not (settings.project_root / "out/renders").exists()


def test_render_scene_payload_resolves_catalog_build_manifest_and_packages(
    tmp_path: Path,
) -> None:
    settings, spec, manifest_path, _, _ = _scene_render_fixture(tmp_path)

    payload = _render_asset_payload(settings, spec)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert payload["kind"] == "scene"
    assert payload["asset_id"] == "scene:test_scene"
    assert payload["scene_id"] == "test_scene"
    assert payload["scene_map_path"] == "/Game/UEF/Scenes/test_scene/L_test_scene"
    assert payload["scene_build_manifest"] == "out/scenes/test_scene/build_manifest.json"
    assert payload["build_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert payload["package_bundle_sha256"] == manifest["package_bundle_sha256"]
    assert payload["inventory_sha256"] == manifest["inventory_sha256"]
    assert payload["source"] == "local"
    assert payload["source_id"] == "test-scene"
    assert payload["source_url"] == "https://example.test/test-scene.glb"
    assert payload["source_file"] == manifest["source_file"]
    assert payload["source_sha256"] == manifest["source_sha256"]
    assert payload["scene_spec_sha256"] == manifest["scene_spec_sha256"]
    assert payload["license"] == "CC-BY-4.0"
    assert payload["license_tier"] == "open"
    assert payload["license_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert payload["attribution"] == "Example Artist"
    assert payload["export"] is True
    assert payload["actor_count"] == 2
    assert payload["static_mesh_actor_count"] == 1
    assert payload["static_mesh_component_count"] == 1
    assert payload["expected_object_stencil_ids"] == [1]
    assert payload["render_inventory"]["actors"] == manifest["inventory"]["actors"]
    assert payload["render_inventory_sha256"] == _canonical_digest(payload["render_inventory"])
    assert payload["camera_azimuth_offset_deg"] == -35.0
    assert payload["camera_elevation_deg"] == 22.5
    assert payload["lighting_intensity_multiplier"] == 1.0
    assert payload["minimum_object_stencil_coverage"] == 0.8
    assert payload["maximum_background_contamination_ratio"] == 0.001
    assert payload["preserve_materials"] is True
    assert payload["no_auto_floor"] is True
    assert payload["bounds_cm"] == {
        "min": [-100.0, -50.0, 0.0],
        "max": [300.0, 150.0, 200.0],
        "size": [400.0, 200.0, 200.0],
    }
    assert payload["camera_target_cm"] == [100.0, 50.0, 100.0]
    assert payload["camera_radius_cm"] == 795.721851


def test_render_scene_payload_rejects_missing_map_package(tmp_path: Path) -> None:
    settings, spec, _, map_file, _ = _scene_render_fixture(tmp_path)
    map_file.unlink()

    with pytest.raises(RuntimeError, match="scene map package is missing"):
        _render_asset_payload(settings, spec)


def test_render_scene_payload_rejects_missing_mesh_package(tmp_path: Path) -> None:
    settings, spec, _, _, mesh_file = _scene_render_fixture(tmp_path)
    mesh_file.unlink()

    with pytest.raises(RuntimeError, match="no valid build manifest/package inventory"):
        _render_asset_payload(settings, spec)


def test_render_scene_payload_rejects_changed_package_generation(tmp_path: Path) -> None:
    settings, spec, _, _, mesh_file = _scene_render_fixture(tmp_path)
    mesh_file.write_bytes(b"tampered package")

    with pytest.raises(RuntimeError, match="no valid build manifest/package inventory"):
        _render_asset_payload(settings, spec)


def test_render_scene_payload_rejects_changed_source_provenance(tmp_path: Path) -> None:
    settings, spec, manifest_path, _, _ = _scene_render_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    Path(manifest["source_file"]).write_bytes(b"tampered source")

    with pytest.raises(RuntimeError, match="source provenance changed"):
        _render_asset_payload(settings, spec)


def test_render_scene_payload_rejects_tampered_build_manifest(tmp_path: Path) -> None:
    settings, spec, manifest_path, _, _ = _scene_render_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["map_path"] = "/Game/UEF/Scenes/test_scene/L_tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="no valid build manifest/package inventory"):
        _render_asset_payload(settings, spec)


def test_validate_scene_sanitization_accepts_complete_catalog_evidence(
    tmp_path: Path,
) -> None:
    spec = _catalog_render_spec(tmp_path)

    _validate_scene_sanitization(
        {
            "scene_sanitization": {
                "policy": "catalog_hide_all_pawns_v2",
                "subjobs": [
                    {
                        "subjob_index": 0,
                        "hidden_pawn_count": 1,
                        "editor_hidden_pawn_count": 1,
                        "hidden_static_meshes": [
                            "/Engine/EngineMeshes/Sphere.Sphere",
                        ],
                    }
                ],
            }
        },
        spec,
    )


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (None, "missing scene sanitization evidence"),
        (
            {"policy": "not_applicable"},
            "unsupported scene sanitization policy",
        ),
        (
            {
                "policy": "catalog_hide_all_pawns_v2",
                "subjobs": [],
            },
            "must cover every MRQ subjob",
        ),
        (
            {
                "policy": "catalog_hide_all_pawns_v2",
                "subjobs": [
                    {
                        "subjob_index": 1,
                        "hidden_pawn_count": 1,
                        "editor_hidden_pawn_count": 1,
                        "hidden_static_meshes": [],
                    }
                ],
            },
            "subjob indices are incomplete",
        ),
        (
            {
                "policy": "catalog_hide_all_pawns_v2",
                "subjobs": [
                    {
                        "subjob_index": 0,
                        "hidden_pawn_count": True,
                        "editor_hidden_pawn_count": 0,
                        "hidden_static_meshes": [],
                    }
                ],
            },
            "invalid pawn count",
        ),
        (
            {
                "policy": "catalog_hide_all_pawns_v2",
                "subjobs": [
                    {
                        "subjob_index": 0,
                        "hidden_pawn_count": 1,
                        "editor_hidden_pawn_count": 1,
                        "hidden_static_meshes": [23],
                    }
                ],
            },
            "invalid mesh inventory",
        ),
    ],
)
def test_validate_scene_sanitization_rejects_invalid_catalog_evidence(
    tmp_path: Path,
    payload: object,
    error: str,
) -> None:
    spec = _catalog_render_spec(tmp_path)
    manifest = {} if payload is None else {"scene_sanitization": payload}

    with pytest.raises(RuntimeError, match=error):
        _validate_scene_sanitization(manifest, spec)


def test_validate_scene_sanitization_rejects_builtin_scene_mutation(
    tmp_path: Path,
) -> None:
    _, job_path = _local_render_fixture(tmp_path)
    spec = load_jobspec(job_path)

    with pytest.raises(RuntimeError, match="retain the M1 scene contract"):
        _validate_scene_sanitization(
            {
                "scene_sanitization": {
                    "policy": "catalog_hide_all_pawns_v2",
                    "subjobs": [],
                }
            },
            spec,
        )


def test_render_job_preserves_failed_runtime_manifest(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)
    monkeypatch.setattr(
        "uefactory.render.job.run_ue",
        _fake_run_ue(runtime_status="failed", write_frames=False),
    )

    with pytest.raises(RuntimeError, match="UE runtime reported render failure"):
        render_job(settings=settings, job_path=job_path, timeout_sec=60)

    manifest = _read_only_manifest(settings.project_root)
    assert manifest["status"] == "failed"
    assert manifest["error"] == "synthetic runtime failure"


def test_render_job_preserves_runtime_root_cause_when_process_exits_nonzero(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)
    call_count = 0

    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        nonlocal call_count
        del cwd, timeout_sec
        call_count += 1
        argv = [str(part) for part in command]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        result = UERunResult(
            command=argv,
            returncode=0 if call_count == 1 else 17,
            duration_sec=0.01,
            log_path=log_path,
            summary=LogSummary(warnings=[], errors=[], warning_count=0, error_count=0),
        )
        if call_count == 2:
            assert env is not None
            job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
            Path(job["out_dir"]).joinpath("manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "status": "failed",
                        "error": "specific runtime normalization failure",
                        "runtime_detail": {"missing_pass": "depth"},
                    }
                ),
                encoding="utf-8",
            )
            raise UERunnerError(result)
        return result

    monkeypatch.setattr("uefactory.render.job.run_ue", fake_run_ue)

    with pytest.raises(UERunnerError):
        render_job(settings=settings, job_path=job_path, timeout_sec=60)

    manifest = _read_only_manifest(settings.project_root)
    assert manifest["status"] == "failed"
    assert manifest["error"] == "specific runtime normalization failure"
    assert manifest["runtime_detail"] == {"missing_pass": "depth"}
    assert "exit code 17" in manifest["host_error"]
    assert manifest["asset_cleanup"]["status"] == "ok"


def test_render_job_records_cleanup_failure_after_setup_interrupt(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)

    def interrupt_setup(*args: Any, **kwargs: Any) -> UERunResult:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr("uefactory.render.job.run_ue", interrupt_setup)
    monkeypatch.setattr(
        "uefactory.render.job._cleanup_local_job_assets",
        lambda settings, run_id: {
            "path": f"ue/UEFBase/Content/UEF/RenderJobs/{run_id}",
            "status": "failed",
            "removed": False,
            "error_type": "PermissionError",
            "error": "synthetic cleanup denial",
        },
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        render_job(settings=settings, job_path=job_path, timeout_sec=60)

    manifest = _read_only_manifest(settings.project_root)
    assert manifest["status"] == "failed"
    assert manifest["error"] == "KeyboardInterrupt"
    assert manifest["asset_cleanup"]["status"] == "failed"
    assert manifest["cleanup_error"] == {
        "type": "PermissionError",
        "message": "synthetic cleanup denial",
    }
    assert any("cleanup also failed" in note for note in raised.value.__notes__)


def test_render_job_marks_manifest_failed_when_final_artifact_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)
    monkeypatch.setattr(
        "uefactory.render.job.run_ue",
        _fake_run_ue(runtime_status="ok", write_frames=True),
    )
    monkeypatch.setattr("uefactory.render.artifacts.shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "uefactory.render.artifacts.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            23,
            stdout="encoder setup",
            stderr="synthetic codec failure",
        ),
    )

    with pytest.raises(RuntimeError, match="synthetic codec failure"):
        render_job(settings=settings, job_path=job_path, timeout_sec=60)

    manifest = _read_only_manifest(settings.project_root)
    assert manifest["status"] == "failed"
    assert manifest["error"].startswith("Host validation/artifact failure: RuntimeError:")
    assert "synthetic codec failure" in manifest["error"]


def test_render_job_marks_manifest_failed_when_host_validation_is_interrupted(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)
    monkeypatch.setattr(
        "uefactory.render.job.run_ue",
        _fake_run_ue(runtime_status="ok", write_frames=True),
    )

    def interrupt_validation(**kwargs: Any) -> None:
        del kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "uefactory.render.job._validate_render_output",
        interrupt_validation,
    )

    with pytest.raises(KeyboardInterrupt):
        render_job(settings=settings, job_path=job_path, timeout_sec=60)

    manifest = _read_only_manifest(settings.project_root)
    assert manifest["status"] == "failed"
    assert manifest["error"] == "Host validation/artifact failure: KeyboardInterrupt"


def test_render_job_manifest_uses_relative_frame_paths(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)
    monkeypatch.setattr(
        "uefactory.render.job.run_ue",
        _fake_run_ue(runtime_status="ok", write_frames=True),
    )
    monkeypatch.setattr("uefactory.render.artifacts.shutil.which", lambda name: "/usr/bin/ffmpeg")

    def fake_ffmpeg(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(args[0][-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr("uefactory.render.artifacts.subprocess.run", fake_ffmpeg)

    result = render_job(settings=settings, job_path=job_path, timeout_sec=60)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "ok"
    assert manifest["frame_paths"] == {
        "beauty_lit": [
            "beauty_lit/frame_0000.png",
            "beauty_lit/frame_0001.png",
        ]
    }
    assert str(result.run_dir) not in json.dumps(manifest["frame_paths"])


def _local_render_fixture(tmp_path: Path) -> tuple[Settings, Path]:
    project_root = tmp_path / "project"
    script_dir = project_root / "ue/UEFBase/Content/Python"
    config_dir = project_root / "ue/UEFBase/Config"
    engine_version = tmp_path / "engine/Engine/Build/Build.version"
    script_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    engine_version.parent.mkdir(parents=True)
    project_root.joinpath("ue/UEFBase/UEFBase.uproject").write_text("{}", encoding="utf-8")
    script_dir.joinpath("uef_render_job.py").write_text("print('setup')\n", encoding="utf-8")
    script_dir.joinpath("uef_render_job_runtime.py").write_text(
        "print('runtime')\n", encoding="utf-8"
    )
    config_dir.joinpath("DefaultEngine.ini").write_text(
        "[/Script/Engine.Engine]\n", encoding="utf-8"
    )
    engine_version.write_text(
        json.dumps({"MajorVersion": 5, "MinorVersion": 5, "PatchVersion": 4}),
        encoding="utf-8",
    )
    job_path = project_root / "job.yaml"
    job_path.write_text(
        "\n".join(
            [
                "job: render",
                "assets: ['builtin:cube']",
                "camera:",
                "  rig: orbit",
                "  views: 2",
                "  elevation_deg: 20",
                "  fov: 55",
                "  resolution: [64, 64]",
                "lighting:",
                "  preset: three_point",
                "passes: ['beauty_lit']",
                "output:",
                "  dir: out/renders",
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(
        project_root=project_root,
        ue_root=tmp_path / "engine",
        ue_home=tmp_path / "ue_home",
        data_dir=project_root / "data",
        log_dir=project_root / "logs",
    )
    return settings, job_path


def _fake_run_ue(
    *,
    runtime_status: str,
    write_frames: bool,
) -> Any:
    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        del cwd, timeout_sec
        argv = [str(part) for part in command]
        assert "-ddc=InstalledNoZenLocalFallback" in argv
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        if "-game" in argv:
            assert env is not None
            job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
            assert job["beauty_sequence_path"] == job["sequence_path"]
            out_dir = Path(job["out_dir"])
            scene_sanitization = (
                {
                    "policy": "catalog_hide_all_pawns_v2",
                    "subjobs": [
                        {
                            "subjob_index": index,
                            "hidden_pawn_count": 0,
                            "editor_hidden_pawn_count": 0,
                            "hidden_static_meshes": [],
                        }
                        for index, _ in enumerate(job["passes"])
                    ],
                }
                if job["asset"]["kind"] == "catalog"
                else {"policy": "not_applicable"}
            )
            manifest: dict[str, Any] = {
                "schema_version": int(job.get("schema_version", 3)),
                "status": runtime_status,
                "render_kind": "job",
                "scene_sanitization": scene_sanitization,
            }
            if runtime_status == "failed":
                manifest["error"] = "synthetic runtime failure"
            out_dir.joinpath("manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            if write_frames:
                beauty_dir = out_dir / "beauty_lit"
                beauty_dir.mkdir(parents=True)
                for index in range(2):
                    _write_gradient_png(
                        beauty_dir / f"frame_{index:04d}.png",
                        blue=180 - index * 40,
                    )
        return UERunResult(
            command=argv,
            returncode=0,
            duration_sec=0.01,
            log_path=log_path,
            summary=LogSummary(warnings=[], errors=[], warning_count=0, error_count=0),
        )

    return fake_run_ue


def _read_only_manifest(project_root: Path) -> dict[str, Any]:
    manifests = list(project_root.joinpath("out/renders").glob("*/builtin_cube/manifest.json"))
    assert len(manifests) == 1
    return json.loads(manifests[0].read_text(encoding="utf-8"))


def _catalog_render_spec(tmp_path: Path) -> RenderJobSpec:
    _, job_path = _local_render_fixture(tmp_path)
    job_path.write_text(
        job_path.read_text(encoding="utf-8").replace("builtin:cube", "test_asset"),
        encoding="utf-8",
    )
    return load_jobspec(job_path)


def _scene_render_fixture(
    tmp_path: Path,
) -> tuple[Settings, RenderJobSpec, Path, Path, Path]:
    settings, job_path = _local_render_fixture(tmp_path)
    job_path.write_text(
        job_path.read_text(encoding="utf-8").replace("builtin:cube", "scene:test_scene"),
        encoding="utf-8",
    )
    spec = load_jobspec(job_path)
    scene_id = "test_scene"
    map_path = f"/Game/UEF/Scenes/{scene_id}/L_{scene_id}"
    mesh_path = f"/Game/UEF/Scenes/{scene_id}/Assets/SM_Diorama.SM_Diorama"
    map_file = settings.project_root / f"ue/UEFBase/Content/UEF/Scenes/{scene_id}/L_{scene_id}.umap"
    mesh_file = (
        settings.project_root / f"ue/UEFBase/Content/UEF/Scenes/{scene_id}/Assets/SM_Diorama.uasset"
    )
    map_file.parent.mkdir(parents=True)
    mesh_file.parent.mkdir(parents=True)
    map_file.write_bytes(b"umap")
    mesh_file.write_bytes(b"uasset")
    source_file = settings.project_root / f"data/raw/scenes/{scene_id}/scene.glb"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"scene source")
    source_sha256 = hashlib.sha256(source_file.read_bytes()).hexdigest()
    bounds = {
        "min": [-100.0, -50.0, 0.0],
        "max": [300.0, 150.0, 200.0],
        "size": [400.0, 200.0, 200.0],
    }
    manifest_path = settings.project_root / f"out/scenes/{scene_id}/build_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    scene_spec = {
        "schema_version": 1,
        "scene_id": scene_id,
        "name": "Test Scene",
        "kind": "interchange_scene",
        "source": {
            "path": str(source_file),
            "source": "local",
            "source_id": "test-scene",
            "source_url": "https://example.test/test-scene.glb",
            "license": "CC-BY-4.0",
            "license_tier": "open",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "Example Artist",
        },
        "camera": {
            "rig": "overview_bounds",
            "yaw": -35.0,
            "pitch": -22.5,
            "distance_multiplier": 1.25,
        },
        "build": {"map_path": map_path, "export": True},
        "render": {"no_auto_floor": True},
    }
    scene_spec_sha256 = _canonical_digest(scene_spec)
    actors = [
        {
            "object_id": "Diorama",
            "actor_name": "Diorama",
            "actor_label": "Diorama",
            "actor_class": "StaticMeshActor",
            "parent_actor_name": None,
            "transform": {
                "translation_cm": [0.0, 0.0, 0.0],
                "rotation_deg": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
            },
            "components": [
                {
                    "name": "StaticMeshComponent0",
                    "mesh_path": mesh_path,
                    "materials": [],
                    "world_bounds_cm": bounds,
                }
            ],
        },
        {
            "object_id": "KeyLight",
            "actor_name": "KeyLight",
            "actor_label": "KeyLight",
            "actor_class": "DirectionalLight",
            "parent_actor_name": None,
            "transform": {
                "translation_cm": [0.0, 0.0, 300.0],
                "rotation_deg": [-45.0, 30.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
            },
            "components": [],
        },
    ]
    inventory = {
        "schema_version": 1,
        "map_path": map_path,
        "actor_count": 2,
        "static_mesh_count": 1,
        "static_mesh_actor_count": 1,
        "static_mesh_component_count": 1,
        "triangle_count": 128,
        "material_count": 2,
        "texture_count": 0,
        "aggregate_bounds_cm": bounds,
        "actors": actors,
        "assets": [
            {"object_path": mesh_path, "class": "StaticMesh"},
            {"object_path": map_path, "class": "World"},
        ],
        "static_meshes": [
            {
                "object_path": mesh_path,
                "triangle_count": 128,
                "material_count": 2,
            }
        ],
    }
    inventory_sha256 = _canonical_digest(inventory)
    packages = [
        {
            "object_path": mesh_path,
            "class": "StaticMesh",
            "path": mesh_file.relative_to(settings.project_root).as_posix(),
            "size": mesh_file.stat().st_size,
            "sha256": hashlib.sha256(mesh_file.read_bytes()).hexdigest(),
        },
        {
            "object_path": map_path,
            "class": "World",
            "path": map_file.relative_to(settings.project_root).as_posix(),
            "size": map_file.stat().st_size,
            "sha256": hashlib.sha256(map_file.read_bytes()).hexdigest(),
        },
    ]
    package_bundle_sha256 = _canonical_digest(packages)
    manifest_path.write_text(
        json.dumps(
            {
                "status": "ok",
                "scene_id": scene_id,
                "map_path": map_path,
                "source_file": str(source_file),
                "source_sha256": source_sha256,
                "scene_spec_sha256": scene_spec_sha256,
                "scene_spec": scene_spec,
                "inventory": inventory,
                "inventory_sha256": inventory_sha256,
                "packages": packages,
                "package_bundle_sha256": package_bundle_sha256,
            }
        ),
        encoding="utf-8",
    )
    build_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    catalog = Catalog(settings.data_dir / "catalog.db", project_root=settings.project_root)
    scene = SceneUpsert(
        scene_id=scene_id,
        name="Test Scene",
        source="local",
        source_id="test-scene",
        source_url="https://example.test/test-scene.glb",
        license="CC-BY-4.0",
        license_tier="open",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution="Example Artist",
        source_path=f"examples/scenes/{scene_id}.yaml",
        source_file=source_file,
        source_sha256=source_sha256,
        spec_sha256=scene_spec_sha256,
        build_sha256=build_sha256,
        status="built",
        map_path=map_path,
        actor_count=2,
        static_mesh_count=1,
        triangle_count=128,
        material_count=2,
        texture_count=0,
        bounds=bounds,
    )
    objects = (
        SceneObjectUpsert(
            object_id="test_scene_mesh",
            scene_id=scene_id,
            actor_name="Diorama",
            actor_class="StaticMeshActor",
            mesh_path=mesh_path,
            transform={
                "location": [0.0, 0.0, 0.0],
                "rotation": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
            },
            bounds=bounds,
            triangle_count=128,
            material_count=2,
        ),
        SceneObjectUpsert(
            object_id="test_scene_light",
            scene_id=scene_id,
            actor_name="KeyLight",
            actor_class="DirectionalLight",
            transform={
                "location": [0.0, 0.0, 300.0],
                "rotation": [-45.0, 30.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
            },
        ),
    )
    common_params = {
        "source_sha256": source_sha256,
        "scene_spec_sha256": scene_spec_sha256,
        "inventory_sha256": inventory_sha256,
        "package_bundle_sha256": package_bundle_sha256,
        "build_sha256": build_sha256,
        "map_path": map_path,
        "license": "CC-BY-4.0",
        "license_tier": "open",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "Example Artist",
        "export": True,
    }
    evidence_paths = {
        "scene_primary_manifest": manifest_path.parent / "primary_manifest.json",
        "scene_reload_manifest": manifest_path.parent / "reload_manifest.json",
        "scene_finalize_manifest": manifest_path.parent / "finalize_manifest.json",
    }
    for kind, path in evidence_paths.items():
        path.write_text(json.dumps({"status": "ok", "kind": kind}), encoding="utf-8")
    artifacts = (
        SceneArtifactUpsert(
            artifact_id="test_scene_build_manifest",
            scene_id=scene_id,
            kind="scene_build_manifest",
            path=manifest_path,
            params=common_params,
            sha256=build_sha256,
        ),
        *(
            SceneArtifactUpsert(
                artifact_id=f"test_{kind}",
                scene_id=scene_id,
                kind=kind,
                path=path,
                params=common_params,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for kind, path in evidence_paths.items()
        ),
    )
    catalog.finalize_scene_build(scene, objects, artifacts)
    return settings, spec, manifest_path, map_file, mesh_file


def _write_gradient_png(path: Path, *, blue: int) -> None:
    image = Image.new("RGB", (64, 64), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index in range(64):
        draw.line((index, 0, index, 63), fill=(index * 3, 80, blue))
    image.save(path)
