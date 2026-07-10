from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from uefactory.catalog import (
    Catalog,
    SceneArtifactUpsert,
    SceneObjectUpsert,
    SceneRecord,
    SceneUpsert,
)
from uefactory.core.config import Settings
from uefactory.core.paths import utc_timestamp
from uefactory.render.smoke import _prepend_env_path, _runtime_settings
from uefactory.render.ue_runner import UERunResult, run_ue
from uefactory.scenes.locking import scene_lock
from uefactory.scenes.package_evidence import (
    collect_scene_package_evidence,
    scene_package_bundle_sha256,
)
from uefactory.scenes.spec import SceneSpec, load_scene_spec


@dataclass(frozen=True)
class SceneBuildResult:
    scene: SceneRecord
    run_dir: Path
    manifest_path: Path
    primary_log_path: Path
    reload_log_path: Path
    finalize_log_path: Path
    inventory: dict[str, Any]
    inventory_sha256: str
    packages: tuple[dict[str, Any], ...]
    package_bundle_sha256: str
    build_sha256: str
    catalog_path: Path


class SceneBuildError(RuntimeError):
    def __init__(self, *, manifest_path: Path, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.manifest_path = manifest_path
        self.cause_type = type(cause).__name__


def build_scene(
    *,
    settings: Settings,
    spec_path: Path,
    database_path: Path | None = None,
    out_root: Path | None = None,
    timeout_sec: int = 1800,
) -> SceneBuildResult:
    spec = load_scene_spec(spec_path)
    data_dir = settings.data_dir
    if not data_dir.is_absolute():
        data_dir = settings.project_root / data_dir
    with scene_lock(data_dir=data_dir, scene_id=spec.scene_id):
        return _build_scene_locked(
            settings=settings,
            spec=spec,
            database_path=database_path,
            out_root=out_root,
            timeout_sec=timeout_sec,
        )


def _build_scene_locked(
    *,
    settings: Settings,
    spec: SceneSpec,
    database_path: Path | None,
    out_root: Path | None,
    timeout_sec: int,
) -> SceneBuildResult:
    source_file = _resolve_source_file(spec)
    source_sha256 = _sha256(source_file)
    if timeout_sec <= 0:
        raise ValueError("timeout_sec must be positive")

    catalog_path = database_path or settings.data_dir / "catalog.db"
    if not catalog_path.is_absolute():
        catalog_path = settings.project_root / catalog_path
    catalog = Catalog(catalog_path, project_root=settings.project_root)
    catalog.preflight_write()
    _project_relative(settings.project_root, catalog_path)

    run_id = f"{utc_timestamp()}_{uuid4().hex[:8]}"
    root = out_root or settings.project_root / "out/scene_builds"
    run_dir = root / run_id / spec.scene_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _project_relative(settings.project_root, spec.source_path)
    _project_relative(settings.project_root, run_dir)
    manifest_path = run_dir / "manifest.json"
    primary_manifest_path = run_dir / "primary_manifest.json"
    reload_manifest_path = run_dir / "reload_manifest.json"
    finalize_manifest_path = run_dir / "finalize_manifest.json"
    retry_manifest_path = run_dir / "finalize_retry_manifest.json"
    inspect_manifest_path = run_dir / "inspect_manifest.json"
    rollback_manifest_path = run_dir / "rollback_manifest.json"
    primary_log_path = run_dir / "ue_primary.log"
    reload_log_path = run_dir / "ue_reload.log"
    finalize_log_path = run_dir / "ue_finalize.log"
    retry_log_path = run_dir / "ue_finalize_retry.log"
    inspect_log_path = run_dir / "ue_inspect.log"
    rollback_log_path = run_dir / "ue_rollback.log"

    runtime, command, base_env = _ue_runtime(settings)
    phase_results: dict[str, UERunResult] = {}
    primary: dict[str, Any] | None = None
    primary_attempted = False
    reload_payload: dict[str, Any] | None = None
    finalize_payload: dict[str, Any] | None = None
    finalize_evidence_path: Path | None = None
    commit_confirmation: str | None = None
    committed = False
    phase = "primary"
    try:
        scene_contract = {
            "scene_spec": spec.as_dict(),
            "scene_spec_sha256": spec.digest,
        }
        primary_job = {
            "schema_version": 1,
            "job": "build_scene",
            "scene_id": spec.scene_id,
            **scene_contract,
            "source_file": str(source_file),
            "source_sha256": source_sha256,
            "expected": None if spec.expected is None else spec.expected.as_dict(),
            "manifest_path": str(primary_manifest_path),
        }
        primary_attempted = True
        phase_results[phase] = _run_scene_phase(
            settings=settings,
            command=command,
            env=base_env,
            job=primary_job,
            job_path=run_dir / "primary_job.json",
            manifest_path=primary_manifest_path,
            log_path=primary_log_path,
            timeout_sec=timeout_sec,
            expected_status="prepared",
        )
        primary = _read_object(primary_manifest_path)
        inventory = _require_inventory(primary)
        inventory_sha256 = _require_digest(primary, "inventory_sha256")
        _assert_digest(inventory, inventory_sha256, "primary scene inventory")
        _assert_source_unchanged(source_file, source_sha256)

        approved_job = {
            "schema_version": 1,
            "scene_id": spec.scene_id,
            **scene_contract,
            "expected_inventory": inventory,
            "expected_inventory_sha256": inventory_sha256,
        }
        phase = "reload"
        phase_results[phase] = _run_scene_phase(
            settings=settings,
            command=command,
            env=base_env,
            job={
                **approved_job,
                "job": "reload_scene",
                "manifest_path": str(reload_manifest_path),
            },
            job_path=run_dir / "reload_job.json",
            manifest_path=reload_manifest_path,
            log_path=reload_log_path,
            timeout_sec=timeout_sec,
            expected_status="ok",
        )
        reload_payload = _read_object(reload_manifest_path)
        if reload_payload.get("inventory") != inventory:
            raise RuntimeError("independent UE reload returned a different scene inventory")
        _assert_source_unchanged(source_file, source_sha256)
        packages = collect_scene_package_evidence(
            settings.project_root,
            scene_id=spec.scene_id,
            inventory=inventory,
        )
        package_bundle_sha256 = scene_package_bundle_sha256(packages)

        # Exercise the exact catalog replacement while UE's previous-map backup is
        # still available.  Finalize evidence does not exist yet, so only its two
        # file digests use a valid sentinel; every identity/path/inventory field is
        # the real final value.
        preflight_sha256 = "0" * 64
        preflight_scene, preflight_objects, preflight_artifacts = _catalog_scene_items(
            settings=settings,
            spec=spec,
            source_file=source_file,
            source_sha256=source_sha256,
            inventory=inventory,
            inventory_sha256=inventory_sha256,
            package_bundle_sha256=package_bundle_sha256,
            build_sha256=preflight_sha256,
            manifest_path=manifest_path,
            primary_manifest_path=primary_manifest_path,
            reload_manifest_path=reload_manifest_path,
            finalize_manifest_path=finalize_manifest_path,
            artifact_sha256={
                "scene_build_manifest": preflight_sha256,
                "scene_primary_manifest": _sha256(primary_manifest_path),
                "scene_reload_manifest": _sha256(reload_manifest_path),
                "scene_finalize_manifest": preflight_sha256,
            },
        )
        catalog.validate_scene_build(
            preflight_scene,
            preflight_objects,
            preflight_artifacts,
        )

        finalize_job = {
            **approved_job,
            "job": "finalize_scene",
            "manifest_path": str(finalize_manifest_path),
        }
        phase = "finalize"
        finalize_errors: list[BaseException] = []
        for attempt, (job_path, phase_manifest, phase_log) in enumerate(
            (
                (run_dir / "finalize_job.json", finalize_manifest_path, finalize_log_path),
                (run_dir / "finalize_retry_job.json", retry_manifest_path, retry_log_path),
            ),
            start=1,
        ):
            try:
                job = {**finalize_job, "manifest_path": str(phase_manifest)}
                result = _run_scene_phase(
                    settings=settings,
                    command=command,
                    env=base_env,
                    job=job,
                    job_path=job_path,
                    manifest_path=phase_manifest,
                    log_path=phase_log,
                    timeout_sec=timeout_sec,
                    expected_status="ok",
                )
                phase_results[f"finalize_{attempt}"] = result
                finalize_payload = _read_object(phase_manifest)
                transaction = finalize_payload.get("transaction")
                if not isinstance(transaction, dict) or transaction.get("state") != "committed":
                    raise RuntimeError("UE scene finalize did not report committed state")
                commit_confirmation = "direct" if attempt == 1 else "retry"
                finalize_log_path = phase_log
                finalize_evidence_path = phase_manifest
                break
            except BaseException as exc:
                finalize_errors.append(exc)
        if finalize_payload is None:
            phase = "inspect"
            try:
                phase_results[phase] = _run_scene_phase(
                    settings=settings,
                    command=command,
                    env=base_env,
                    job={
                        **approved_job,
                        "job": "inspect_scene_transaction",
                        "manifest_path": str(inspect_manifest_path),
                    },
                    job_path=run_dir / "inspect_job.json",
                    manifest_path=inspect_manifest_path,
                    log_path=inspect_log_path,
                    timeout_sec=timeout_sec,
                    expected_status="ok",
                )
                inspection_payload = _read_object(inspect_manifest_path)
                inspection = inspection_payload.get("inspection")
                state = inspection.get("state") if isinstance(inspection, dict) else None
                payload_matches = (
                    inspection.get("payload_matches") if isinstance(inspection, dict) else None
                )
                if state == "committed" and payload_matches is True:
                    finalize_payload = inspection_payload
                    commit_confirmation = "inspect"
                    finalize_log_path = inspect_log_path
                    finalize_evidence_path = inspect_manifest_path
                elif state == "pre_commit":
                    _rollback_scene(
                        settings=settings,
                        command=command,
                        env=base_env,
                        scene_id=spec.scene_id,
                        scene_spec=spec,
                        had_existing=_had_existing(primary),
                        job_path=run_dir / "rollback_job.json",
                        manifest_path=rollback_manifest_path,
                        log_path=rollback_log_path,
                        timeout_sec=timeout_sec,
                    )
                    raise RuntimeError(
                        "scene finalize failed before commit; transaction rolled back"
                    )
                else:
                    raise RuntimeError(
                        "scene finalize state is in doubt; transaction was preserved for inspection"
                    )
            except BaseException as inspect_exc:
                for finalize_error in finalize_errors:
                    inspect_exc.add_note(
                        f"Finalize attempt failed: {type(finalize_error).__name__}: "
                        f"{finalize_error}"
                    )
                raise

        assert finalize_payload is not None
        assert finalize_evidence_path is not None
        assert commit_confirmation is not None
        committed = True
        _assert_source_unchanged(source_file, source_sha256)
        finalized_packages = collect_scene_package_evidence(
            settings.project_root,
            scene_id=spec.scene_id,
            inventory=inventory,
        )
        if finalized_packages != packages:
            raise RuntimeError("scene package files changed between reload validation and finalize")
        phase = "catalog"
        final_payload = {
            "schema_version": 2,
            "status": "ok",
            "scene_id": spec.scene_id,
            "name": spec.name,
            "scene_spec": spec.as_dict(),
            "scene_spec_sha256": spec.digest,
            "source_file": str(source_file),
            "source_sha256": source_sha256,
            "map_path": spec.build.map_path,
            "inventory": inventory,
            "inventory_sha256": inventory_sha256,
            "packages": list(packages),
            "package_bundle_sha256": package_bundle_sha256,
            "commit_confirmation": commit_confirmation,
            "runtime": runtime,
            "phases": {
                key: _result_payload(value, run_dir) for key, value in phase_results.items()
            },
            "evidence": {
                "primary_manifest": primary_manifest_path.name,
                "reload_manifest": reload_manifest_path.name,
                "finalize_manifest": finalize_evidence_path.name,
            },
            "catalog_commit": {
                "database": _project_relative(settings.project_root, catalog_path),
                "target_status": "built",
                "scene_id": spec.scene_id,
            },
        }
        _write_json(manifest_path, final_payload)
        build_sha256 = _sha256(manifest_path)
        scene_record = _commit_catalog_scene(
            settings=settings,
            catalog=catalog,
            catalog_path=catalog_path,
            spec=spec,
            source_file=source_file,
            source_sha256=source_sha256,
            inventory=inventory,
            inventory_sha256=inventory_sha256,
            package_bundle_sha256=package_bundle_sha256,
            build_sha256=build_sha256,
            manifest_path=manifest_path,
            primary_manifest_path=primary_manifest_path,
            reload_manifest_path=reload_manifest_path,
            finalize_manifest_path=finalize_evidence_path,
        )
        return SceneBuildResult(
            scene=scene_record,
            run_dir=run_dir,
            manifest_path=manifest_path,
            primary_log_path=primary_log_path,
            reload_log_path=reload_log_path,
            finalize_log_path=finalize_log_path,
            inventory=inventory,
            inventory_sha256=inventory_sha256,
            packages=packages,
            package_bundle_sha256=package_bundle_sha256,
            build_sha256=build_sha256,
            catalog_path=catalog_path,
        )
    except BaseException as exc:
        if committed:
            rollback_disposition = "not_attempted_after_commit"
        elif phase == "inspect":
            rollback_disposition = "preserved_for_inspection"
        else:
            rollback_disposition = "attempted_if_safe"
        if primary_attempted and not committed and phase != "inspect":
            try:
                rollback_primary = primary
                if rollback_primary is None and primary_manifest_path.is_file():
                    try:
                        rollback_primary = _read_object(primary_manifest_path)
                    except RuntimeError:
                        rollback_primary = None
                _rollback_scene(
                    settings=settings,
                    command=command,
                    env=base_env,
                    scene_id=spec.scene_id,
                    scene_spec=spec,
                    had_existing=_best_effort_had_existing(rollback_primary),
                    job_path=run_dir / "rollback_job.json",
                    manifest_path=rollback_manifest_path,
                    log_path=rollback_log_path,
                    timeout_sec=timeout_sec,
                )
            except BaseException as rollback_exc:
                exc.add_note(
                    f"Scene rollback also failed: {type(rollback_exc).__name__}: {rollback_exc}"
                )
        failure = {
            "schema_version": 2,
            "status": "failed",
            "scene_id": spec.scene_id,
            "phase": phase,
            "scene_spec": spec.as_dict(),
            "scene_spec_sha256": spec.digest,
            "source_file": str(source_file),
            "source_sha256": source_sha256,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "transaction": {
                "state": "committed" if committed else "pre_commit_or_in_doubt",
                "rollback": rollback_disposition,
            },
            "runtime": runtime,
            "phases": {
                key: _result_payload(value, run_dir) for key, value in phase_results.items()
            },
        }
        try:
            _write_json(manifest_path, failure)
        except BaseException as write_exc:
            exc.add_note(f"Could not write scene failure manifest: {write_exc}")
        raise SceneBuildError(manifest_path=manifest_path, cause=exc) from exc


def _ue_runtime(settings: Settings) -> tuple[dict[str, Any], list[str | Path], dict[str, str]]:
    project_path = settings.project_root / "ue/UEFBase/UEFBase.uproject"
    script_path = settings.project_root / "ue/UEFBase/Content/Python/uef_scene_build.py"
    for label, path in {"UE project": project_path, "UE scene script": script_path}.items():
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
    ddc_dir = settings.ddc_dir or settings.data_dir / "ddc"
    tmp_dir = settings.data_dir / "tmp"
    for directory in (ddc_dir, tmp_dir, settings.ue_home):
        directory.mkdir(parents=True, exist_ok=True)
    runtime = _runtime_settings(settings.runtime_lib_dir)
    env = {
        "HOME": str(settings.ue_home),
        "TMPDIR": str(tmp_dir),
        "UE-LocalDataCachePath": str(ddc_dir),
    }
    if runtime["enabled"]:
        env["LD_LIBRARY_PATH"] = _prepend_env_path(
            Path(str(runtime["lib_dir"])),
            "LD_LIBRARY_PATH",
        )
    command: list[str | Path] = [
        settings.ue_root / "Engine/Binaries/Linux/UnrealEditor-Cmd",
        project_path,
        f"-ExecutePythonScript={script_path}",
        "-unattended",
        "-nopause",
        "-nosplash",
        "-NullRHI",
        "-stdout",
        "-FullStdOutLogOutput",
        "-NoSound",
        "-trace=none",
        "-ddc=InstalledNoZenLocalFallback",
        f"-LocalDataCachePath={ddc_dir}",
    ]
    return runtime, command, env


def _run_scene_phase(
    *,
    settings: Settings,
    command: list[str | Path],
    env: dict[str, str],
    job: dict[str, Any],
    job_path: Path,
    manifest_path: Path,
    log_path: Path,
    timeout_sec: int,
    expected_status: str,
) -> UERunResult:
    _write_json(job_path, job)
    phase_env = {**env, "UEF_JOB_FILE": str(job_path)}
    result = run_ue(
        command,
        cwd=settings.project_root,
        log_path=log_path,
        timeout_sec=timeout_sec,
        env=phase_env,
    )
    if result.summary.error_count or result.summary.warning_count:
        raise RuntimeError(
            f"UE scene phase produced unfiltered diagnostics: "
            f"errors={result.summary.error_count} warnings={result.summary.warning_count}; "
            f"log={log_path}"
        )
    payload = _read_object(manifest_path)
    if payload.get("status") != expected_status:
        raise RuntimeError(
            f"UE scene phase manifest status is {payload.get('status')!r}, "
            f"expected {expected_status!r}: {manifest_path}"
        )
    return result


def _rollback_scene(
    *,
    settings: Settings,
    command: list[str | Path],
    env: dict[str, str],
    scene_id: str,
    scene_spec: SceneSpec,
    had_existing: bool,
    job_path: Path,
    manifest_path: Path,
    log_path: Path,
    timeout_sec: int,
) -> None:
    _run_scene_phase(
        settings=settings,
        command=command,
        env=env,
        job={
            "schema_version": 1,
            "job": "rollback_scene",
            "scene_id": scene_id,
            "scene_spec": scene_spec.as_dict(),
            "scene_spec_sha256": scene_spec.digest,
            "remove_new_destination": not had_existing,
            "manifest_path": str(manifest_path),
        },
        job_path=job_path,
        manifest_path=manifest_path,
        log_path=log_path,
        timeout_sec=timeout_sec,
        expected_status="ok",
    )


def _commit_catalog_scene(
    *,
    settings: Settings,
    catalog: Catalog,
    catalog_path: Path,
    spec: SceneSpec,
    source_file: Path,
    source_sha256: str,
    inventory: dict[str, Any],
    inventory_sha256: str,
    package_bundle_sha256: str,
    build_sha256: str,
    manifest_path: Path,
    primary_manifest_path: Path,
    reload_manifest_path: Path,
    finalize_manifest_path: Path,
) -> SceneRecord:
    del catalog_path
    scene, objects, artifacts = _catalog_scene_items(
        settings=settings,
        spec=spec,
        source_file=source_file,
        source_sha256=source_sha256,
        inventory=inventory,
        inventory_sha256=inventory_sha256,
        package_bundle_sha256=package_bundle_sha256,
        build_sha256=build_sha256,
        manifest_path=manifest_path,
        primary_manifest_path=primary_manifest_path,
        reload_manifest_path=reload_manifest_path,
        finalize_manifest_path=finalize_manifest_path,
        artifact_sha256={
            "scene_build_manifest": build_sha256,
            "scene_primary_manifest": _sha256(primary_manifest_path),
            "scene_reload_manifest": _sha256(reload_manifest_path),
            "scene_finalize_manifest": _sha256(finalize_manifest_path),
        },
    )
    record, _, _ = catalog.finalize_scene_build(scene, objects, artifacts)
    return record


def _catalog_scene_items(
    *,
    settings: Settings,
    spec: SceneSpec,
    source_file: Path,
    source_sha256: str,
    inventory: dict[str, Any],
    inventory_sha256: str,
    package_bundle_sha256: str,
    build_sha256: str,
    manifest_path: Path,
    primary_manifest_path: Path,
    reload_manifest_path: Path,
    finalize_manifest_path: Path,
    artifact_sha256: dict[str, str],
) -> tuple[SceneUpsert, tuple[SceneObjectUpsert, ...], tuple[SceneArtifactUpsert, ...]]:
    actors = inventory.get("actors")
    meshes = inventory.get("static_meshes")
    if not isinstance(actors, list) or not actors:
        raise RuntimeError("scene catalog commit requires a non-empty actor inventory")
    if not isinstance(meshes, list):
        raise RuntimeError("scene catalog commit requires a mesh inventory")
    mesh_by_path = {
        item["object_path"]: item
        for item in meshes
        if isinstance(item, dict) and isinstance(item.get("object_path"), str)
    }
    objects: list[SceneObjectUpsert] = []
    distinct_meshes: set[str] = set()
    for index, actor in enumerate(actors):
        if not isinstance(actor, dict):
            raise RuntimeError("scene actor inventory entry is invalid")
        components = actor.get("components")
        if not isinstance(components, list) or len(components) > 1:
            raise RuntimeError(
                "scene catalog v2 requires at most one StaticMesh component per actor"
            )
        mesh_path = None
        bounds = None
        triangle_count = None
        material_count = None
        if components:
            component = components[0]
            if not isinstance(component, dict):
                raise RuntimeError("scene component inventory entry is invalid")
            mesh_path = component.get("mesh_path")
            if not isinstance(mesh_path, str) or mesh_path not in mesh_by_path:
                raise RuntimeError("scene component references an unknown StaticMesh")
            distinct_meshes.add(mesh_path)
            bounds = component.get("world_bounds_cm")
            triangle_count = int(mesh_by_path[mesh_path]["triangle_count"])
            material_count = int(mesh_by_path[mesh_path]["material_count"])
        objects.append(
            SceneObjectUpsert(
                object_id=f"{spec.scene_id}_actor_{index:04d}",
                scene_id=spec.scene_id,
                actor_name=str(actor["actor_name"]),
                actor_class=str(actor["actor_class"]),
                transform=actor["transform"],
                mesh_path=mesh_path,
                bounds=bounds,
                triangle_count=triangle_count,
                material_count=material_count,
            )
        )
    static_mesh_count = int(inventory["static_mesh_count"])
    if len(distinct_meshes) != static_mesh_count:
        raise RuntimeError(
            "scene actor inventory does not reference every imported StaticMesh exactly by identity"
        )
    scene = SceneUpsert(
        scene_id=spec.scene_id,
        name=spec.name,
        source=spec.source.source,
        source_id=spec.source.source_id,
        source_url=spec.source.source_url,
        license=spec.source.license,
        license_tier=spec.source.license_tier,
        license_url=spec.source.license_url,
        attribution=spec.source.attribution,
        source_path=spec.source_path,
        source_file=source_file,
        source_sha256=source_sha256,
        spec_sha256=spec.digest,
        build_sha256=build_sha256,
        status="built",
        map_path=spec.build.map_path,
        actor_count=len(objects),
        static_mesh_count=static_mesh_count,
        triangle_count=int(inventory["triangle_count"]),
        material_count=int(inventory["material_count"]),
        texture_count=int(inventory["texture_count"]),
        bounds=inventory["aggregate_bounds_cm"],
    )
    common_params = {
        "schema_version": 2,
        "scene_spec_sha256": spec.digest,
        "source_sha256": source_sha256,
        "inventory_sha256": inventory_sha256,
        "package_bundle_sha256": package_bundle_sha256,
        "build_sha256": build_sha256,
        "source_file": str(source_file),
        "map_path": spec.build.map_path,
        "texture_count": int(inventory["texture_count"]),
        "license": spec.source.license,
        "license_tier": spec.source.license_tier,
        "license_url": spec.source.license_url,
        "attribution": spec.source.attribution,
        "export": spec.build.export,
    }
    artifacts = tuple(
        SceneArtifactUpsert(
            artifact_id=f"{spec.scene_id}_{suffix}",
            scene_id=spec.scene_id,
            kind=kind,
            path=path,
            params=common_params,
            sha256=artifact_sha256[kind],
        )
        for suffix, kind, path in (
            ("build_manifest", "scene_build_manifest", manifest_path),
            ("primary_manifest", "scene_primary_manifest", primary_manifest_path),
            ("reload_manifest", "scene_reload_manifest", reload_manifest_path),
            ("finalize_manifest", "scene_finalize_manifest", finalize_manifest_path),
        )
    )
    return scene, tuple(objects), artifacts


def _resolve_source_file(spec: SceneSpec) -> Path:
    source = Path(spec.source.path).expanduser()
    source_root: Path | None = None
    if spec.source.root_env is not None:
        root_value = os.environ.get(spec.source.root_env)
        if not root_value:
            raise ValueError(
                f"scene source root environment variable is not set: {spec.source.root_env}"
            )
        source_root = Path(root_value).expanduser()
        if not source_root.is_absolute():
            raise ValueError(
                f"scene source root environment variable must be an absolute path: "
                f"{spec.source.root_env}"
            )
        source_root = source_root.resolve()
        if not source_root.is_dir():
            raise FileNotFoundError(
                f"scene source root directory not found: {source_root} ({spec.source.root_env})"
            )
        source = source_root / source
    elif not source.is_absolute():
        source = spec.source_path.parent / source
    if source.is_symlink():
        raise ValueError(f"scene source file may not be a symbolic link: {source}")
    source = source.resolve()
    if source_root is not None:
        try:
            source.relative_to(source_root)
        except ValueError as exc:
            raise ValueError(
                f"scene source file escapes configured root {spec.source.root_env}: {source}"
            ) from exc
    if not source.is_file():
        raise FileNotFoundError(f"scene source file not found: {source}")
    if source.suffix.lower() not in {".fbx", ".glb", ".gltf"}:
        raise ValueError(f"unsupported scene source format: {source.suffix}")
    return source


def _require_inventory(payload: dict[str, Any]) -> dict[str, Any]:
    inventory = payload.get("inventory")
    if not isinstance(inventory, dict):
        raise RuntimeError("UE scene manifest is missing its inventory")
    return inventory


def _require_digest(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or len(value) != 64:
        raise RuntimeError(f"UE scene manifest has an invalid {key}")
    return value


def _assert_digest(value: dict[str, Any], expected: str, label: str) -> None:
    actual = _canonical_digest(value)
    if actual != expected:
        raise RuntimeError(f"{label} digest mismatch: expected={expected} actual={actual}")


def _had_existing(primary: dict[str, Any]) -> bool:
    transaction = primary.get("transaction")
    if not isinstance(transaction, dict) or not isinstance(transaction.get("had_existing"), bool):
        raise RuntimeError("primary scene manifest is missing had_existing evidence")
    return bool(transaction["had_existing"])


def _best_effort_had_existing(primary: dict[str, Any] | None) -> bool:
    if primary is None:
        return False
    transaction = primary.get("transaction")
    if not isinstance(transaction, dict) or not isinstance(transaction.get("had_existing"), bool):
        return False
    return bool(transaction["had_existing"])


def _assert_source_unchanged(path: Path, expected_sha256: str) -> None:
    actual = _sha256(path)
    if actual != expected_sha256:
        raise RuntimeError(
            f"scene source changed during build: expected={expected_sha256} actual={actual}"
        )


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read scene manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"scene manifest must be a JSON object: {path}")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _project_relative(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"path is outside project root: {path}") from exc


def _result_payload(result: UERunResult, run_dir: Path) -> dict[str, Any]:
    try:
        log = result.log_path.resolve().relative_to(run_dir.resolve()).as_posix()
    except ValueError:
        log = str(result.log_path)
    return {
        "command": result.command,
        "returncode": result.returncode,
        "duration_sec": result.duration_sec,
        "log": log,
        "summary": {
            "warning_count": result.summary.warning_count,
            "error_count": result.summary.error_count,
            "filtered_warning_noise_count": result.summary.warning_noise_count,
            "filtered_error_noise_count": result.summary.error_noise_count,
        },
    }


__all__ = ["SceneBuildError", "SceneBuildResult", "build_scene"]
