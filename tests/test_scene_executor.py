from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import uefactory.scenes.executor as scene_executor
from uefactory.core.config import Settings
from uefactory.render.ue_runner import LogSummary, UERunnerError, UERunResult
from uefactory.scenes.executor import SceneBuildError, build_scene
from uefactory.scenes.locking import SceneLockError, scene_lock
from uefactory.scenes.spec import SceneSpec, load_scene_spec

SCENE_ID = "fantasy_diorama"
MAP_PATH = f"/Game/UEF/Scenes/{SCENE_ID}/L_{SCENE_ID}"


def _settings(tmp_path: Path) -> Settings:
    project_root = tmp_path / "project"
    script = project_root / "ue/UEFBase/Content/Python/uef_scene_build.py"
    script.parent.mkdir(parents=True)
    script.write_text("# host orchestration fixture\n", encoding="utf-8")
    (project_root / "ue/UEFBase/UEFBase.uproject").write_text("{}", encoding="utf-8")
    return Settings(
        project_root=project_root,
        ue_root=project_root / "engine",
        ue_home=project_root / "ue-home",
        data_dir=project_root / "data",
        log_dir=project_root / "logs",
    )


def _scene_fixture(settings: Settings) -> tuple[Path, Path, SceneSpec]:
    source = settings.project_root / "fixtures/fantasy-diorama.glb"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"multi-object glTF scene fixture")
    spec_path = settings.project_root / "specs/fantasy-diorama.json"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scene_id": SCENE_ID,
                "name": "Low Poly Fantasy Diorama",
                "kind": "interchange_scene",
                "source": {
                    "path": "../fixtures/fantasy-diorama.glb",
                    "source": "blackmyth_asset_library",
                    "source_id": "f3266f252ea98fcc",
                    "source_url": "https://sketchfab.com/3d-models/f3266f252ea98fcc",
                    "license": "CC-BY-4.0",
                    "license_tier": "open",
                    "license_url": "https://creativecommons.org/licenses/by/4.0/",
                    "attribution": "Mesh-Base — Low Poly Fantasy Diorama",
                },
                "expected": {
                    "mesh_count": 2,
                    "material_count": 2,
                    "texture_count": 1,
                    "triangle_count": 24,
                },
                "build": {"map_path": MAP_PATH, "export": True},
                "camera": {
                    "rig": "overview_bounds",
                    "yaw": -35.0,
                    "pitch": -22.5,
                    "distance_multiplier": 1.35,
                },
                "render": {"no_auto_floor": True},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    for relative_path in (
        f"ue/UEFBase/Content/UEF/Scenes/{SCENE_ID}/Assets/MeshA.uasset",
        f"ue/UEFBase/Content/UEF/Scenes/{SCENE_ID}/Assets/MeshB.uasset",
        f"ue/UEFBase/Content/UEF/Scenes/{SCENE_ID}/Assets/MaterialA.uasset",
        f"ue/UEFBase/Content/UEF/Scenes/{SCENE_ID}/Assets/MaterialB.uasset",
        f"ue/UEFBase/Content/UEF/Scenes/{SCENE_ID}/Assets/TextureA.uasset",
        f"ue/UEFBase/Content/UEF/Scenes/{SCENE_ID}/L_{SCENE_ID}.umap",
    ):
        package = settings.project_root / relative_path
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_bytes(f"package:{relative_path}".encode())
    return spec_path, source, load_scene_spec(spec_path)


def _inventory(*, triangle_count: int = 24) -> dict[str, object]:
    mesh_a = f"/Game/UEF/Scenes/{SCENE_ID}/Assets/MeshA.MeshA"
    mesh_b = f"/Game/UEF/Scenes/{SCENE_ID}/Assets/MeshB.MeshB"
    return {
        "schema_version": 1,
        "map_path": MAP_PATH,
        "actor_count": 2,
        "static_mesh_actor_count": 2,
        "static_mesh_component_count": 2,
        "static_mesh_count": 2,
        "triangle_count": triangle_count,
        "material_count": 2,
        "texture_count": 1,
        "aggregate_bounds_cm": {
            "min": [-100.0, -50.0, 0.0],
            "max": [100.0, 50.0, 200.0],
            "size": [200.0, 100.0, 200.0],
        },
        "actors": [
            {
                "actor_name": "MeshAActor",
                "actor_class": "StaticMeshActor",
                "transform": {
                    "translation_cm": [0.0, 0.0, 0.0],
                    "rotation_deg": [0.0, 0.0, 0.0],
                    "scale": [1.0, 1.0, 1.0],
                },
                "components": [
                    {
                        "mesh_path": mesh_a,
                        "world_bounds_cm": {
                            "min": [-100.0, -50.0, 0.0],
                            "max": [0.0, 50.0, 200.0],
                        },
                    }
                ],
            },
            {
                "actor_name": "MeshBActor",
                "actor_class": "StaticMeshActor",
                "transform": {
                    "translation_cm": [50.0, 0.0, 0.0],
                    "rotation_deg": [0.0, 0.0, 0.0],
                    "scale": [1.0, 1.0, 1.0],
                },
                "components": [
                    {
                        "mesh_path": mesh_b,
                        "world_bounds_cm": {
                            "min": [0.0, -50.0, 0.0],
                            "max": [100.0, 50.0, 200.0],
                        },
                    }
                ],
            },
        ],
        "assets": [
            {"class": "StaticMesh", "object_path": mesh_a},
            {"class": "StaticMesh", "object_path": mesh_b},
            {
                "class": "MaterialInstanceConstant",
                "object_path": f"/Game/UEF/Scenes/{SCENE_ID}/Assets/MaterialA.MaterialA",
            },
            {
                "class": "MaterialInstanceConstant",
                "object_path": f"/Game/UEF/Scenes/{SCENE_ID}/Assets/MaterialB.MaterialB",
            },
            {
                "class": "Texture2D",
                "object_path": f"/Game/UEF/Scenes/{SCENE_ID}/Assets/TextureA.TextureA",
            },
            {"class": "World", "object_path": f"{MAP_PATH}.L_{SCENE_ID}"},
        ],
        "static_meshes": [
            {
                "object_path": mesh_a,
                "triangle_count": 12,
                "material_count": 1,
            },
            {
                "object_path": mesh_b,
                "triangle_count": triangle_count - 12,
                "material_count": 1,
            },
        ],
    }


def _canonical_digest(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _clean_result(command: Sequence[str | Path], log_path: Path) -> UERunResult:
    log_path.write_text("LogInit: Display: clean scene phase\n", encoding="utf-8")
    return UERunResult(
        command=[str(part) for part in command],
        returncode=0,
        duration_sec=0.25,
        log_path=log_path,
        summary=LogSummary(warnings=[], errors=[], warning_count=0, error_count=0),
    )


def _failed_result(command: Sequence[str | Path], log_path: Path) -> UERunResult:
    log_path.write_text("LogPython: Error: finalize acknowledgement lost\n", encoding="utf-8")
    return UERunResult(
        command=[str(part) for part in command],
        returncode=1,
        duration_sec=0.1,
        log_path=log_path,
        summary=LogSummary(
            warnings=[],
            errors=["LogPython: Error: finalize acknowledgement lost"],
            warning_count=0,
            error_count=1,
        ),
    )


def _write_phase_manifest(
    job: Mapping[str, object],
    *,
    inventory: Mapping[str, object],
    reload_inventory: Mapping[str, object] | None = None,
) -> None:
    job_kind = str(job["job"])
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "ok",
        "job": job_kind,
        "scene_id": job["scene_id"],
        "scene_spec_sha256": job["scene_spec_sha256"],
    }
    if job_kind == "build_scene":
        payload.update(
            {
                "status": "prepared",
                "source_sha256": job["source_sha256"],
                "inventory": dict(inventory),
                "inventory_sha256": _canonical_digest(inventory),
                "transaction": {
                    "state": "promoted_pending_validation",
                    "had_existing": False,
                },
            }
        )
    elif job_kind == "reload_scene":
        selected = reload_inventory if reload_inventory is not None else inventory
        payload.update(
            {
                "inventory": dict(selected),
                "inventory_sha256": _canonical_digest(selected),
                "transaction": {"state": "validated_pending_commit"},
            }
        )
    elif job_kind == "finalize_scene":
        payload.update(
            {
                "inventory": dict(inventory),
                "inventory_sha256": _canonical_digest(inventory),
                "transaction": {
                    "state": "committed",
                    "commit_confirmation": "transaction_deleted",
                },
            }
        )
    elif job_kind == "rollback_scene":
        payload.update(
            {
                "restored_previous": False,
                "removed_new_destination": bool(job["remove_new_destination"]),
            }
        )
    else:  # pragma: no cover - each test handles inspect explicitly when needed
        raise AssertionError(f"unexpected scene job: {job_kind}")
    path = Path(str(job["manifest_path"]))
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class _CatalogStub:
    def __init__(self, path: Path, *, project_root: Path, events: list[str]) -> None:
        self.path = path
        self.project_root = project_root
        self.events = events

    def preflight_write(self) -> int:
        self.events.append("catalog_preflight")
        return 3

    def validate_scene_build(self, *args: object) -> None:
        assert len(args) == 3
        self.events.append("catalog_validate")


def _install_catalog_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[str],
) -> None:
    def catalog_factory(path: Path, *, project_root: Path) -> _CatalogStub:
        events.append("catalog_open")
        return _CatalogStub(path, project_root=project_root, events=events)

    monkeypatch.setattr(scene_executor, "Catalog", catalog_factory)


def test_source_root_environment_variable_resolves_external_library(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _, source, spec = _scene_fixture(settings)
    rooted_spec = replace(
        spec,
        source=replace(
            spec.source,
            path="fixtures/fantasy-diorama.glb",
            root_env="UEF_BLACKMYTH_ROOT",
        ),
    )
    monkeypatch.setenv("UEF_BLACKMYTH_ROOT", str(settings.project_root))

    assert scene_executor._resolve_source_file(rooted_spec) == source.resolve()


def test_source_root_environment_variable_fails_closed_when_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _, _, spec = _scene_fixture(settings)
    rooted_spec = replace(
        spec,
        source=replace(
            spec.source,
            path="fixtures/fantasy-diorama.glb",
            root_env="UEF_BLACKMYTH_ROOT",
        ),
    )
    monkeypatch.delenv("UEF_BLACKMYTH_ROOT", raising=False)

    with pytest.raises(ValueError, match="UEF_BLACKMYTH_ROOT"):
        scene_executor._resolve_source_file(rooted_spec)


def test_source_root_environment_variable_must_be_absolute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _, _, spec = _scene_fixture(settings)
    rooted_spec = replace(
        spec,
        source=replace(
            spec.source,
            path="fixtures/fantasy-diorama.glb",
            root_env="UEF_BLACKMYTH_ROOT",
        ),
    )
    monkeypatch.setenv("UEF_BLACKMYTH_ROOT", "relative/library")

    with pytest.raises(ValueError, match="must be an absolute path"):
        scene_executor._resolve_source_file(rooted_spec)


def test_scene_contract_and_digest_flow_through_primary_reload_and_finalize(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    spec_path, source, spec = _scene_fixture(settings)
    inventory = _inventory()
    jobs: list[dict[str, Any]] = []
    events: list[str] = []
    _install_catalog_stub(monkeypatch, events=events)

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
        assert "-ddc=InstalledNoZenLocalFallback" in [str(part) for part in command]
        job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
        assert isinstance(job, dict)
        jobs.append(job)
        events.append(f"ue:{job['job']}")
        _write_phase_manifest(job, inventory=inventory)
        return _clean_result(command, log_path)

    def fake_catalog_commit(**kwargs: Any) -> Any:
        events.append("catalog_write")
        assert isinstance(kwargs["catalog"], _CatalogStub)
        finalize_manifest = json.loads(
            Path(kwargs["finalize_manifest_path"]).read_text(encoding="utf-8")
        )
        assert finalize_manifest["transaction"]["state"] == "committed"
        host_manifest = json.loads(Path(kwargs["manifest_path"]).read_text(encoding="utf-8"))
        assert host_manifest["status"] == "ok"
        assert host_manifest["commit_confirmation"] == "direct"
        assert (
            kwargs["build_sha256"]
            == hashlib.sha256(Path(kwargs["manifest_path"]).read_bytes()).hexdigest()
        )
        assert kwargs["package_bundle_sha256"] == host_manifest["package_bundle_sha256"]
        return SimpleNamespace(scene_id=SCENE_ID, status="built")

    monkeypatch.setattr(scene_executor, "run_ue", fake_run_ue)
    monkeypatch.setattr(scene_executor, "_commit_catalog_scene", fake_catalog_commit)

    result = build_scene(
        settings=settings,
        spec_path=spec_path,
        database_path=settings.data_dir / "catalog.db",
        out_root=settings.project_root / "out/test_scene_builds",
        timeout_sec=90,
    )

    assert [job["job"] for job in jobs] == [
        "build_scene",
        "reload_scene",
        "finalize_scene",
    ]
    for job in jobs:
        assert job["scene_id"] == SCENE_ID
        assert job["scene_spec"] == spec.as_dict()
        assert job["scene_spec_sha256"] == spec.digest
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    assert jobs[0]["source_file"] == str(source.resolve())
    assert jobs[0]["source_sha256"] == source_sha256
    assert jobs[0]["expected"] == spec.expected.as_dict() if spec.expected else None
    for job in jobs[1:]:
        assert job["expected_inventory"] == inventory
        assert job["expected_inventory_sha256"] == _canonical_digest(inventory)
    assert events == [
        "catalog_open",
        "catalog_preflight",
        "ue:build_scene",
        "ue:reload_scene",
        "catalog_validate",
        "ue:finalize_scene",
        "catalog_write",
    ]
    assert result.scene.scene_id == SCENE_ID
    assert result.inventory == inventory
    assert result.inventory_sha256 == _canonical_digest(inventory)
    assert len(result.packages) == 6
    assert result.package_bundle_sha256 == scene_executor._canonical_digest(list(result.packages))
    assert result.build_sha256 == hashlib.sha256(result.manifest_path.read_bytes()).hexdigest()
    final_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert final_manifest["status"] == "ok"
    assert final_manifest["scene_spec"] == spec.as_dict()
    assert final_manifest["scene_spec_sha256"] == spec.digest
    assert final_manifest["commit_confirmation"] == "direct"
    assert final_manifest["packages"] == list(result.packages)
    assert final_manifest["package_bundle_sha256"] == result.package_bundle_sha256


def test_build_scene_rejects_a_busy_scene_lock_before_starting_ue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    spec_path, _, _ = _scene_fixture(settings)

    def unexpected_run(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"UE must not start while the scene lock is busy: {args}, {kwargs}")

    monkeypatch.setattr(scene_executor, "run_ue", unexpected_run)
    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_lock() -> None:
        try:
            with scene_lock(data_dir=settings.data_dir, scene_id=SCENE_ID):
                acquired.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test did not release the external scene lock")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
            acquired.set()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert acquired.wait(timeout=5)
    try:
        with pytest.raises(SceneLockError, match="another build or render owns"):
            build_scene(settings=settings, spec_path=spec_path)
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert not (settings.project_root / "out/scene_builds").exists()


@pytest.mark.parametrize(
    ("primary_evidence", "remove_new_destination"),
    [("valid", False), ("invalid", True), ("timeout", True)],
)
def test_primary_phase_failure_always_attempts_idempotent_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    primary_evidence: str,
    remove_new_destination: bool,
) -> None:
    settings = _settings(tmp_path)
    spec_path, _, _ = _scene_fixture(settings)
    inventory = _inventory()
    jobs: list[dict[str, Any]] = []
    events: list[str] = []
    _install_catalog_stub(monkeypatch, events=events)

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
        assert isinstance(job, dict)
        jobs.append(job)
        if job["job"] == "build_scene":
            manifest_path = Path(job["manifest_path"])
            if primary_evidence == "valid":
                _write_phase_manifest(job, inventory=inventory)
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                payload["transaction"]["had_existing"] = True
                manifest_path.write_text(json.dumps(payload), encoding="utf-8")
                log_path.write_text("LogTemp: Warning: force host rejection\n", encoding="utf-8")
                return UERunResult(
                    command=[str(part) for part in command],
                    returncode=0,
                    duration_sec=0.1,
                    log_path=log_path,
                    summary=LogSummary(
                        warnings=["LogTemp: Warning: force host rejection"],
                        errors=[],
                        warning_count=1,
                        error_count=0,
                    ),
                )
            if primary_evidence == "timeout":
                log_path.write_text("UE process timed out and was terminated\n", encoding="utf-8")
                raise UERunnerError(
                    UERunResult(
                        command=[str(part) for part in command],
                        returncode=-15,
                        duration_sec=1800.0,
                        log_path=log_path,
                        summary=LogSummary(
                            warnings=[],
                            errors=[],
                            warning_count=0,
                            error_count=0,
                        ),
                    )
                )
            manifest_path.write_text("{not-json", encoding="utf-8")
            return _clean_result(command, log_path)
        assert job["job"] == "rollback_scene"
        _write_phase_manifest(job, inventory=inventory)
        return _clean_result(command, log_path)

    monkeypatch.setattr(scene_executor, "run_ue", fake_run_ue)

    with pytest.raises(SceneBuildError):
        build_scene(
            settings=settings,
            spec_path=spec_path,
            database_path=settings.data_dir / "catalog.db",
            out_root=settings.project_root / "out/test_scene_builds",
        )

    assert [job["job"] for job in jobs] == ["build_scene", "rollback_scene"]
    assert jobs[-1]["remove_new_destination"] is remove_new_destination


def test_reload_inventory_mismatch_rolls_back_and_never_writes_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    spec_path, _, spec = _scene_fixture(settings)
    primary_inventory = _inventory()
    changed_inventory = _inventory(triangle_count=23)
    jobs: list[dict[str, Any]] = []
    events: list[str] = []
    _install_catalog_stub(monkeypatch, events=events)

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
        assert isinstance(job, dict)
        jobs.append(job)
        events.append(f"ue:{job['job']}")
        _write_phase_manifest(
            job,
            inventory=primary_inventory,
            reload_inventory=changed_inventory,
        )
        return _clean_result(command, log_path)

    monkeypatch.setattr(scene_executor, "run_ue", fake_run_ue)
    monkeypatch.setattr(
        scene_executor,
        "_commit_catalog_scene",
        lambda **kwargs: pytest.fail("reload mismatch must not write the scene catalog"),
    )

    with pytest.raises(SceneBuildError, match="independent UE reload returned a different"):
        build_scene(
            settings=settings,
            spec_path=spec_path,
            database_path=settings.data_dir / "catalog.db",
            out_root=settings.project_root / "out/test_scene_builds",
        )

    assert [job["job"] for job in jobs] == [
        "build_scene",
        "reload_scene",
        "rollback_scene",
    ]
    rollback = jobs[-1]
    assert rollback["scene_spec"] == spec.as_dict()
    assert rollback["scene_spec_sha256"] == spec.digest
    assert rollback["remove_new_destination"] is True
    assert "ue:finalize_scene" not in events
    assert "catalog_write" not in events
    manifest_path = next(
        (settings.project_root / "out/test_scene_builds").glob(f"*/{SCENE_ID}/manifest.json")
    )
    failure = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["phase"] == "reload"
    assert failure["scene_spec_sha256"] == spec.digest


def test_post_commit_package_mismatch_never_rolls_back_or_writes_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    spec_path, _, _ = _scene_fixture(settings)
    inventory = _inventory()
    jobs: list[dict[str, Any]] = []
    events: list[str] = []
    _install_catalog_stub(monkeypatch, events=events)
    real_collect = scene_executor.collect_scene_package_evidence
    collection_count = 0

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
        assert isinstance(job, dict)
        jobs.append(job)
        _write_phase_manifest(job, inventory=inventory)
        return _clean_result(command, log_path)

    def changed_after_finalize(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], ...]:
        nonlocal collection_count
        collection_count += 1
        if collection_count == 1:
            return real_collect(*args, **kwargs)
        raise RuntimeError("package tree changed after irreversible scene commit")

    monkeypatch.setattr(scene_executor, "run_ue", fake_run_ue)
    monkeypatch.setattr(
        scene_executor,
        "collect_scene_package_evidence",
        changed_after_finalize,
    )
    monkeypatch.setattr(
        scene_executor,
        "_commit_catalog_scene",
        lambda **kwargs: pytest.fail("post-commit package mismatch must not write the catalog"),
    )

    with pytest.raises(SceneBuildError, match="changed after irreversible scene commit") as raised:
        build_scene(
            settings=settings,
            spec_path=spec_path,
            database_path=settings.data_dir / "catalog.db",
            out_root=settings.project_root / "out/test_scene_builds",
        )

    assert [job["job"] for job in jobs] == [
        "build_scene",
        "reload_scene",
        "finalize_scene",
    ]
    failure = json.loads(raised.value.manifest_path.read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["transaction"] == {
        "state": "committed",
        "rollback": "not_attempted_after_commit",
    }


def test_catalog_is_not_written_when_finalize_commit_cannot_be_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    spec_path, _, spec = _scene_fixture(settings)
    inventory = _inventory()
    jobs: list[dict[str, Any]] = []
    events: list[str] = []
    _install_catalog_stub(monkeypatch, events=events)

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
        assert isinstance(job, dict)
        jobs.append(job)
        events.append(f"ue:{job['job']}")
        if job["job"] == "finalize_scene":
            raise UERunnerError(_failed_result(command, log_path))
        if job["job"] == "inspect_scene_transaction":
            Path(job["manifest_path"]).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "ok",
                        "job": job["job"],
                        "scene_id": SCENE_ID,
                        "scene_spec_sha256": job["scene_spec_sha256"],
                        "inspection": {
                            "state": "in_doubt",
                            "payload_matches": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
        else:
            _write_phase_manifest(job, inventory=inventory)
        return _clean_result(command, log_path)

    monkeypatch.setattr(scene_executor, "run_ue", fake_run_ue)
    monkeypatch.setattr(
        scene_executor,
        "_commit_catalog_scene",
        lambda **kwargs: pytest.fail("unconfirmed finalize must not write the scene catalog"),
    )

    with pytest.raises(SceneBuildError, match="finalize state is in doubt"):
        build_scene(
            settings=settings,
            spec_path=spec_path,
            database_path=settings.data_dir / "catalog.db",
            out_root=settings.project_root / "out/test_scene_builds",
        )

    assert [job["job"] for job in jobs] == [
        "build_scene",
        "reload_scene",
        "finalize_scene",
        "finalize_scene",
        "inspect_scene_transaction",
    ]
    for job in jobs:
        assert job["scene_spec"] == spec.as_dict()
        assert job["scene_spec_sha256"] == spec.digest
    assert "catalog_write" not in events
    assert "ue:rollback_scene" not in events
