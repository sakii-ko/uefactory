from __future__ import annotations

import hashlib
import json
import struct
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from uefactory.core.asset_locking import AssetLockError, asset_lock
from uefactory.core.config import Settings
from uefactory.ingest.executor import IngestExecutionError, ingest_asset
from uefactory.ingest.quality import IngestQualityError
from uefactory.ingest.source_structure import inspect_source_structure
from uefactory.render.ue_runner import LogSummary, UERunnerError, UERunResult


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
                if not release.wait(timeout=5):
                    raise TimeoutError("test did not release the external asset lock")
        except BaseException as exc:  # pragma: no cover - surfaced by the caller
            errors.append(exc)
            acquired.set()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert acquired.wait(timeout=5)
    return thread, release, errors


def _settings(tmp_path: Path) -> Settings:
    project_root = tmp_path / "project"
    script = project_root / "ue/UEFBase/Content/Python/uef_ingest_asset.py"
    script.parent.mkdir(parents=True)
    script.write_text("# test fixture\n", encoding="utf-8")
    (project_root / "ue/UEFBase/UEFBase.uproject").write_text("{}", encoding="utf-8")
    version = project_root / "engine/Engine/Build/Build.version"
    version.parent.mkdir(parents=True)
    version.write_text(
        json.dumps({"MajorVersion": 5, "MinorVersion": 5, "PatchVersion": 4}),
        encoding="utf-8",
    )
    return Settings(
        project_root=project_root,
        ue_root=project_root / "engine",
        ue_home=project_root / "ue-home",
        data_dir=project_root / "data",
        log_dir=project_root / "logs",
    )


def _source(settings: Settings) -> Path:
    path = settings.project_root / "fixtures/model.glb"
    path.parent.mkdir(parents=True)
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [
            {
                "children": [1],
                "matrix": [
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    -1.0,
                    0.0,
                    0.0,
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                ],
            },
            {"mesh": 0},
        ],
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
    return path


def _mesh(asset_id: str, *, triangles: int = 12) -> dict[str, object]:
    return {
        "object_path": f"/Game/UEF/Ingested/{asset_id}/SM_{asset_id}",
        "name": f"SM_{asset_id}",
        "lod_count": 1,
        "triangle_count": triangles,
        "render_fallback_triangle_count": max(1, triangles // 2),
        "vertex_count": 8,
        "material_count": 1,
        "material_slots": [
            {
                "index": 0,
                "slot_name": "body",
                "material_path": f"/Game/UEF/Ingested/{asset_id}/M_{asset_id}.M_{asset_id}",
                "texture_paths": [f"/Game/UEF/Ingested/{asset_id}/T_{asset_id}.T_{asset_id}"],
            }
        ],
        "bounds_cm": {
            "min": [-50.0, -50.0, 0.0],
            "max": [50.0, 50.0, 100.0],
            "size": [100.0, 100.0, 100.0],
        },
    }


def _clean_result(command: Sequence[str | Path], log_path: Path) -> UERunResult:
    return UERunResult(
        command=[str(part) for part in command],
        returncode=0,
        duration_sec=1.25,
        log_path=log_path,
        summary=LogSummary(warnings=[], errors=[], warning_count=0, error_count=0),
    )


def _failed_result(command: Sequence[str | Path], log_path: Path) -> UERunResult:
    return UERunResult(
        command=[str(part) for part in command],
        returncode=1,
        duration_sec=0.5,
        log_path=log_path,
        summary=LogSummary(
            warnings=[],
            errors=["LogPython: Error: import failed"],
            warning_count=0,
            error_count=1,
        ),
    )


def _write_ue_manifest(job: dict[str, object], meshes: list[dict[str, object]]) -> None:
    path = Path(str(job["manifest_path"]))
    if job["job"] == "rollback_ingested_asset":
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "ok",
                    "job": job["job"],
                    "asset_id": job["asset_id"],
                    "restored_previous": False,
                    "removed_new_destination": bool(job["remove_new_destination"]),
                }
            ),
            encoding="utf-8",
        )
        return
    asset_id = str(job["asset_id"])
    material_path = f"/Game/UEF/Ingested/{asset_id}/M_{asset_id}.M_{asset_id}"
    texture_path = f"/Game/UEF/Ingested/{asset_id}/T_{asset_id}.T_{asset_id}"
    imported_objects: list[dict[str, object]] = [
        *[{"object_path": mesh["object_path"], "class": "StaticMesh"} for mesh in meshes],
        {"object_path": material_path, "class": "MaterialInstanceConstant"},
        {"object_path": texture_path, "class": "Texture2D"},
    ]
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "ok",
        "job": job["job"],
        "asset_id": job["asset_id"],
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
        "static_mesh_count": len(meshes),
        "static_meshes": meshes,
        "imported_object_paths": [item["object_path"] for item in imported_objects],
        "imported_objects": imported_objects,
        "object_count": len(imported_objects),
        "material_count": 1,
        "texture_count": 1,
    }
    if job["job"] == "ingest_asset":
        payload.update(
            {
                "source_file": job["source_file"],
                "source_format": Path(str(job["source_file"])).suffix.lstrip("."),
                "destination_path": f"/Game/UEF/Ingested/{job['asset_id']}",
                "transaction": {
                    "state": "pending_host_validation",
                    "had_existing": False,
                },
            }
        )
        project_root = Path(str(job["source_file"])).parents[1]
        for item in imported_objects:
            object_path = str(item["object_path"])
            relative_package = object_path.removeprefix("/Game/").partition(".")[0]
            package_file = project_root / "ue/UEFBase/Content" / f"{relative_package}.uasset"
            package_file.parent.mkdir(parents=True, exist_ok=True)
            package_file.write_bytes(f"package bytes: {object_path}".encode())
    if job["job"] == "finalize_ingested_asset":
        payload.update({"transaction_state": "committed", "removed_backup": False})
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_ingest_asset_runs_import_then_independent_reload_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _source(settings)
    jobs: list[dict[str, Any]] = []
    environments: list[dict[str, str]] = []
    commands: list[list[str]] = []
    requested_normalization = {
        "source_units": "auto",
        "source_up_axis": "auto",
        "source_handedness": "auto",
        "uniform_scale": 2.0,
        "pivot_policy": "preserve_source",
    }

    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        assert cwd == settings.project_root
        assert timeout_sec == 90
        assert env is not None
        job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
        jobs.append(job)
        environments.append(dict(env))
        commands.append([str(part) for part in command])
        _write_ue_manifest(job, [_mesh("test_asset")])
        log_path.write_text("LogInit: Display: clean\n", encoding="utf-8")
        return _clean_result(command, log_path)

    monkeypatch.setattr("uefactory.ingest.executor.run_ue", fake_run_ue)

    result = ingest_asset(
        settings=settings,
        asset_id="test_asset",
        source_file=source,
        out_root=settings.project_root / "out/test_ingest",
        timeout_sec=90,
        require_texture_references=True,
        requested_normalization=requested_normalization,
    )

    assert [job["job"] for job in jobs] == [
        "ingest_asset",
        "validate_ingested_asset",
        "finalize_ingested_asset",
    ]
    assert jobs[0]["source_file"] == str(source.resolve())
    assert jobs[0]["require_texture_references"] is True
    assert jobs[0]["source_structure"]["child_edge_count"] == 1
    assert jobs[0]["source_structure"]["max_depth"] == 2
    assert jobs[0]["source_structure"]["non_identity_local_transform_count"] == 1
    assert jobs[0]["source_structure"]["ue_hierarchy_preserved"] is False
    assert len(jobs[0]["source_structure_sha256"]) == 64
    assert all(job["requested_normalization"] == requested_normalization for job in jobs)
    assert jobs[2]["expected_asset_payload"]["static_meshes"] == [_mesh("test_asset")]
    assert jobs[1]["imported_objects"] == [
        {
            "class": "StaticMesh",
            "object_path": "/Game/UEF/Ingested/test_asset/SM_test_asset",
        },
        {
            "class": "MaterialInstanceConstant",
            "object_path": "/Game/UEF/Ingested/test_asset/M_test_asset.M_test_asset",
        },
        {
            "class": "Texture2D",
            "object_path": "/Game/UEF/Ingested/test_asset/T_test_asset.T_test_asset",
        },
    ]
    assert environments[0]["HOME"] == str(settings.ue_home)
    assert environments[0]["TMPDIR"] == str(settings.data_dir / "tmp")
    assert environments[0]["UE-LocalDataCachePath"] == str(settings.data_dir / "ddc")
    assert "-NullRHI" in commands[0]
    assert "-ddc=InstalledNoZenLocalFallback" in commands[0]
    assert result.asset_id == "test_asset"
    assert result.imported_object_paths == (
        "/Game/UEF/Ingested/test_asset/SM_test_asset",
        "/Game/UEF/Ingested/test_asset/M_test_asset.M_test_asset",
        "/Game/UEF/Ingested/test_asset/T_test_asset.T_test_asset",
    )
    assert result.static_mesh_paths == ("/Game/UEF/Ingested/test_asset/SM_test_asset",)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "ok"
    assert manifest["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest["engine"] == {"MajorVersion": 5, "MinorVersion": 5, "PatchVersion": 4}
    assert manifest["ue_summary"]["warning_count"] == 0
    assert manifest["reload_validation"]["status"] == "ok"
    assert manifest["reload_validation"]["manifest"] == "reload_manifest.json"
    assert manifest["finalize_validation"]["status"] == "ok"
    assert manifest["finalize_validation"]["package_bundle_validation"] == {
        "status": "ok",
        "package_bundle_sha256": manifest["ue_package_bundle"]["package_bundle_sha256"],
    }
    assert manifest["schema_version"] == 2
    assert manifest["quality"]["ruleset_version"] == "m2_static_mesh_v2"
    assert manifest["quality"]["status"] == "passed"
    assert manifest["quality"]["checks"]["source_structure_provenance"]["status"] == "passed"
    assert manifest["source_structure"] == jobs[0]["source_structure"]
    assert manifest["source_structure_sha256"] == jobs[0]["source_structure_sha256"]
    assert manifest["requested_normalization"] == requested_normalization
    assert manifest["quality"]["checks"]["bounds_max_extent_cm"]["actual"] == 200.0
    assert manifest["ue_package_bundle"]["policy"] == "ue_ingested_package_bundle_v1"
    assert len(manifest["ue_package_bundle"]["files"]) == 3
    assert len(manifest["ue_package_bundle"]["package_bundle_sha256"]) == 64


def test_ingest_asset_busy_lock_rejects_before_ue_starts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _source(settings)
    ue_started = False

    def unexpected_run_ue(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        nonlocal ue_started
        ue_started = True
        pytest.fail("UE must not start while the model asset lock is busy")

    monkeypatch.setattr("uefactory.ingest.executor.run_ue", unexpected_run_ue)

    thread, release, errors = _start_external_asset_lock(
        data_dir=settings.data_dir,
        asset_id="test_asset",
    )
    try:
        with pytest.raises(AssetLockError, match="another ingest or render owns"):
            ingest_asset(settings=settings, asset_id="test_asset", source_file=source)
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert ue_started is False
    assert not (settings.project_root / "out/ingest").exists()


def test_ingest_asset_resolves_relative_data_dir_for_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    absolute_settings = _settings(tmp_path)
    settings = replace(absolute_settings, data_dir=Path("relative_data"))
    source = _source(settings)
    lock_data_dir = settings.project_root / settings.data_dir
    monkeypatch.setattr(
        "uefactory.ingest.executor.run_ue",
        lambda *args, **kwargs: pytest.fail("UE must not start while the resolved lock is busy"),
    )

    thread, release, errors = _start_external_asset_lock(
        data_dir=lock_data_dir,
        asset_id="test_asset",
    )
    lock_path = lock_data_dir / "locks/assets/test_asset.lock"
    try:
        with pytest.raises(AssetLockError, match="another ingest or render owns"):
            ingest_asset(settings=settings, asset_id="test_asset", source_file=source)
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert lock_path == settings.project_root / "relative_data/locks/assets/test_asset.lock"


def test_finalize_package_rewrite_records_committed_failure_without_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _source(settings)
    jobs: list[str] = []

    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        del cwd, timeout_sec
        assert env is not None
        job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
        jobs.append(str(job["job"]))
        _write_ue_manifest(job, [_mesh("test_asset")])
        if job["job"] == "finalize_ingested_asset":
            package = (
                settings.project_root
                / "ue/UEFBase/Content/UEF/Ingested/test_asset/SM_test_asset.uasset"
            )
            package.write_bytes(b"finalize unexpectedly rewrote these package bytes")
        return _clean_result(command, log_path)

    monkeypatch.setattr("uefactory.ingest.executor.run_ue", fake_run_ue)

    with pytest.raises(IngestExecutionError, match="package bundle bytes or file inventory"):
        ingest_asset(settings=settings, asset_id="test_asset", source_file=source)

    assert jobs == [
        "ingest_asset",
        "validate_ingested_asset",
        "finalize_ingested_asset",
    ]
    manifest_path = next((settings.project_root / "out/ingest").glob("*/test_asset/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["failure_phase"] == "post_commit_package_bundle_evidence"
    assert manifest["transaction"]["state"] == "committed"
    assert manifest["finalize_validation"]["status"] == "failed"
    assert manifest["finalize_validation"]["package_bundle_validation"]["status"] == "failed"
    assert manifest["asset_cleanup"] == {
        "status": "committed",
        "transaction_state": "committed",
        "rollback_attempted": False,
        "reason": (
            "UE transaction was already committed before final package-byte validation failed; "
            "rollback was not attempted"
        ),
    }


def _write_inspect_manifest(
    job: dict[str, object],
    meshes: list[dict[str, object]],
    *,
    transaction_state: str,
    payload_matches: bool,
) -> None:
    _write_ue_manifest(job, meshes)
    path = Path(str(job["manifest_path"]))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(
        {
            "transaction_state": transaction_state,
            "payload_matches": payload_matches,
            "destination_exists": True,
            "transaction_exists": transaction_state == "pre_commit",
            "candidate_exists": False,
            "backup_exists": transaction_state == "pre_commit",
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_finalize_ack_loss_is_confirmed_by_idempotent_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _source(settings)
    jobs: list[str] = []
    finalize_attempts = 0

    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        nonlocal finalize_attempts
        del cwd, timeout_sec
        assert env is not None
        job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
        jobs.append(job["job"])
        _write_ue_manifest(job, [_mesh("test_asset")])
        if job["job"] == "finalize_ingested_asset":
            finalize_attempts += 1
            if finalize_attempts == 1:
                raise UERunnerError(_failed_result(command, log_path))
        return _clean_result(command, log_path)

    monkeypatch.setattr("uefactory.ingest.executor.run_ue", fake_run_ue)

    result = ingest_asset(settings=settings, asset_id="test_asset", source_file=source)

    assert jobs == [
        "ingest_asset",
        "validate_ingested_asset",
        "finalize_ingested_asset",
        "finalize_ingested_asset",
    ]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["transaction"]["state"] == "committed"
    assert manifest["finalize_validation"]["commit_confirmation"] == "retry"
    assert [item["status"] for item in manifest["finalize_validation"]["attempts"]] == [
        "failed",
        "ok",
    ]


@pytest.mark.parametrize("inspect_state", ["pre_commit", "in_doubt"])
def test_finalize_double_failure_rolls_back_only_when_inspection_proves_pre_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    inspect_state: str,
) -> None:
    settings = _settings(tmp_path)
    source = _source(settings)
    jobs: list[str] = []

    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        del cwd, timeout_sec
        assert env is not None
        job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
        jobs.append(job["job"])
        if job["job"] == "finalize_ingested_asset":
            raise UERunnerError(_failed_result(command, log_path))
        if job["job"] == "inspect_ingest_transaction":
            _write_inspect_manifest(
                job,
                [_mesh("test_asset")],
                transaction_state=inspect_state,
                payload_matches=inspect_state == "pre_commit",
            )
        else:
            _write_ue_manifest(job, [_mesh("test_asset")])
        return _clean_result(command, log_path)

    monkeypatch.setattr("uefactory.ingest.executor.run_ue", fake_run_ue)

    with pytest.raises(IngestExecutionError):
        ingest_asset(settings=settings, asset_id="test_asset", source_file=source)

    if inspect_state == "pre_commit":
        assert jobs[-1] == "rollback_ingested_asset"
    else:
        assert "rollback_ingested_asset" not in jobs
    manifest_path = next((settings.project_root / "out/ingest").glob("*/test_asset/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["asset_cleanup"]["status"] == (
        "ok" if inspect_state == "pre_commit" else "in_doubt"
    )


def test_finalize_double_ack_loss_accepts_clean_committed_inspection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _source(settings)
    jobs: list[str] = []

    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        del cwd, timeout_sec
        assert env is not None
        job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
        jobs.append(job["job"])
        if job["job"] == "finalize_ingested_asset":
            raise UERunnerError(_failed_result(command, log_path))
        if job["job"] == "inspect_ingest_transaction":
            _write_inspect_manifest(
                job,
                [_mesh("test_asset")],
                transaction_state="committed",
                payload_matches=True,
            )
        else:
            _write_ue_manifest(job, [_mesh("test_asset")])
        return _clean_result(command, log_path)

    monkeypatch.setattr("uefactory.ingest.executor.run_ue", fake_run_ue)

    result = ingest_asset(settings=settings, asset_id="test_asset", source_file=source)

    assert jobs[-1] == "inspect_ingest_transaction"
    assert "rollback_ingested_asset" not in jobs
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["finalize_validation"]["commit_confirmation"] == "inspect"


def test_ingest_asset_rejects_invalid_requested_normalization(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(ValueError, match="uniform_scale"):
        ingest_asset(
            settings=settings,
            asset_id="test_asset",
            source_file=_source(settings),
            requested_normalization={
                "source_units": "auto",
                "source_up_axis": "auto",
                "source_handedness": "auto",
                "uniform_scale": float("nan"),
                "pivot_policy": "preserve_source",
            },
        )


def test_ingest_asset_rejects_staging_source_structure_mismatch_before_ue(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _source(settings)
    evidence = inspect_source_structure(source)
    forged = dict(evidence.payload)
    forged["ue_hierarchy_preserved"] = True

    with pytest.raises(RuntimeError, match="source_structure changed before UE import"):
        ingest_asset(
            settings=settings,
            asset_id="test_asset",
            source_file=source,
            expected_source_structure=forged,
            expected_source_structure_sha256=evidence.sha256,
        )

    assert not (settings.project_root / "out/ingest").exists()


def test_ingest_asset_persists_first_ue_failure_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _source(settings)
    jobs: list[str] = []

    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        del cwd, timeout_sec
        assert env is not None
        job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
        jobs.append(job["job"])
        if job["job"] == "rollback_ingested_asset":
            _write_ue_manifest(job, [])
            return _clean_result(command, log_path)
        Path(job["manifest_path"]).write_text(
            json.dumps(
                {
                    "status": "failed",
                    "error": "engine import exploded",
                    "engine_detail": "primary manifest survived host merge",
                }
            ),
            encoding="utf-8",
        )
        raise UERunnerError(_failed_result(command, log_path))

    monkeypatch.setattr("uefactory.ingest.executor.run_ue", fake_run_ue)
    out_root = settings.project_root / "out/test_ingest"

    with pytest.raises(IngestExecutionError, match="exit code 1") as raised:
        ingest_asset(
            settings=settings,
            asset_id="test_asset",
            source_file=source,
            out_root=out_root,
        )

    assert raised.value.cause_type == "UERunnerError"
    assert raised.value.manifest_path == next(out_root.glob("*/test_asset/manifest.json"))

    assert jobs == ["ingest_asset", "rollback_ingested_asset"]
    manifest_path = next(out_root.glob("*/test_asset/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["job"] == "ingest_asset"
    assert manifest["asset_id"] == "test_asset"
    assert manifest["source_file"] == str(source.resolve())
    assert manifest["error"] == "engine import exploded"
    assert manifest["engine_detail"] == "primary manifest survived host merge"
    assert manifest["failure_phase"] == "primary_ue_process"
    assert manifest["asset_cleanup"]["status"] == "ok"
    assert manifest["asset_cleanup"]["removed_new_destination"] is False
    assert manifest["host_error"]["type"] == "UERunnerError"
    assert "exit code 1" in manifest["host_error"]["message"]


def test_ingest_asset_rejects_reload_payload_that_differs_from_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _source(settings)
    jobs: list[str] = []

    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        del cwd, timeout_sec
        assert env is not None
        job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
        jobs.append(job["job"])
        if job["job"] == "rollback_ingested_asset":
            _write_ue_manifest(job, [])
            return _clean_result(command, log_path)
        triangles = 12 if job["job"] == "ingest_asset" else 11
        _write_ue_manifest(job, [_mesh("test_asset", triangles=triangles)])
        return _clean_result(command, log_path)

    monkeypatch.setattr("uefactory.ingest.executor.run_ue", fake_run_ue)
    out_root = settings.project_root / "out/test_ingest"

    with pytest.raises(RuntimeError, match="asset payload differs"):
        ingest_asset(
            settings=settings,
            asset_id="test_asset",
            source_file=source,
            out_root=out_root,
        )

    manifest_path = next(out_root.glob("*/test_asset/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert jobs == [
        "ingest_asset",
        "validate_ingested_asset",
        "rollback_ingested_asset",
    ]
    assert manifest["status"] == "failed"
    assert manifest["failure_phase"] == "reload_contract_validation"
    assert "asset payload differs" in manifest["host_error"]["message"]
    assert manifest["asset_cleanup"]["status"] == "ok"
    assert manifest["reload_validation"]["status"] == "failed"
    assert manifest["static_meshes"][0]["triangle_count"] == 12
    assert manifest["reload_validation"]["static_meshes"][0]["triangle_count"] == 11


def test_ingest_asset_records_reload_process_failure_in_both_manifests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _source(settings)
    jobs: list[str] = []

    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        del cwd, timeout_sec
        assert env is not None
        job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
        jobs.append(job["job"])
        if job["job"] == "ingest_asset":
            _write_ue_manifest(job, [_mesh("test_asset")])
            return _clean_result(command, log_path)
        if job["job"] == "rollback_ingested_asset":
            _write_ue_manifest(job, [])
            return _clean_result(command, log_path)
        Path(job["manifest_path"]).write_text(
            json.dumps({"status": "failed", "error": "reload process crashed"}),
            encoding="utf-8",
        )
        raise UERunnerError(_failed_result(command, log_path))

    monkeypatch.setattr("uefactory.ingest.executor.run_ue", fake_run_ue)
    out_root = settings.project_root / "out/test_ingest"

    with pytest.raises(IngestExecutionError, match="exit code 1") as raised:
        ingest_asset(
            settings=settings,
            asset_id="test_asset",
            source_file=source,
            out_root=out_root,
        )

    assert raised.value.cause_type == "UERunnerError"

    manifest_path = next(out_root.glob("*/test_asset/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reload_manifest = json.loads(
        (manifest_path.parent / "reload_manifest.json").read_text(encoding="utf-8")
    )
    assert jobs == [
        "ingest_asset",
        "validate_ingested_asset",
        "rollback_ingested_asset",
    ]
    assert manifest["status"] == "failed"
    assert manifest["failure_phase"] == "reload_ue_process"
    assert manifest["host_error"]["type"] == "UERunnerError"
    assert manifest["asset_cleanup"]["status"] == "ok"
    assert "reload_validation" not in manifest
    assert reload_manifest["status"] == "failed"
    assert reload_manifest["error"] == "reload process crashed"


def test_single_mesh_gate_runs_before_reload_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _source(settings)
    jobs: list[str] = []

    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        del cwd, timeout_sec
        assert env is not None
        job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
        jobs.append(job["job"])
        if job["job"] == "rollback_ingested_asset":
            _write_ue_manifest(job, [])
        else:
            _write_ue_manifest(
                job,
                [
                    _mesh("test_asset"),
                    {
                        **_mesh("test_asset"),
                        "object_path": ("/Game/UEF/Ingested/test_asset/SM_test_asset_extra"),
                    },
                ],
            )
        return _clean_result(command, log_path)

    monkeypatch.setattr("uefactory.ingest.executor.run_ue", fake_run_ue)

    with pytest.raises(IngestQualityError, match="single_static_mesh") as raised:
        ingest_asset(settings=settings, asset_id="test_asset", source_file=source)

    assert jobs == ["ingest_asset", "rollback_ingested_asset"]
    assert raised.value.manifest_path is not None
    assert raised.value.report["checks"]["single_static_mesh"]["status"] == "failed"
    manifest = json.loads(raised.value.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["failure_phase"] == "primary_quality_gate"
    assert manifest["quality"] == raised.value.report


def test_bundle_mutation_during_primary_ue_is_detected_and_rolled_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _source(settings)
    jobs: list[str] = []

    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        del cwd, timeout_sec
        assert env is not None
        job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
        jobs.append(job["job"])
        if job["job"] == "rollback_ingested_asset":
            _write_ue_manifest(job, [])
        else:
            _write_ue_manifest(job, [_mesh("test_asset")])
            source.write_bytes(b"mutated while UE was importing")
        return _clean_result(command, log_path)

    monkeypatch.setattr("uefactory.ingest.executor.run_ue", fake_run_ue)

    with pytest.raises(RuntimeError, match="bytes changed during UE import"):
        ingest_asset(settings=settings, asset_id="test_asset", source_file=source)

    assert jobs == ["ingest_asset", "rollback_ingested_asset"]
