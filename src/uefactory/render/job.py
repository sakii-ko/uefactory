from __future__ import annotations

import hashlib
import json
import math
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from uefactory.catalog import Catalog
from uefactory.core.asset_locking import asset_lock
from uefactory.core.config import Settings
from uefactory.core.ingest_contracts import (
    IMPORT_ARTIFACT_SCHEMA_VERSION,
    IMPORT_MANIFEST_SCHEMA_VERSION,
    QUALITY_RULESET_VERSION,
    is_current_passed_quality,
    static_mesh_quality_policy,
)
from uefactory.core.paths import resolve_path, utc_timestamp
from uefactory.core.remote import RemoteHost
from uefactory.render.artifacts import RenderArtifacts, create_render_artifacts
from uefactory.render.jobspec import RenderJobSpec, load_jobspec
from uefactory.render.passes import (
    PassValidation,
    assert_object_mask_visibility,
    assert_passes_distinct,
    canonicalize_png_frames,
    stable_validation_payload,
    validate_render_pass,
)
from uefactory.render.smoke import (
    _cleanup_remote_paths,
    _prepare_remote_smoke_runtime,
    _prepend_env_path,
    _record_remote_cleanup,
    _runtime_settings,
)
from uefactory.render.ue_runner import UERunnerError, run_ue


@dataclass(frozen=True)
class RenderJobResult:
    run_dir: Path
    manifest_path: Path
    ue_log_path: Path
    setup_log_path: Path
    frame_paths: dict[str, list[Path]]
    pass_validations: dict[str, PassValidation]
    spec: RenderJobSpec
    artifacts: RenderArtifacts | None = None

    @property
    def frame_luma(self) -> list[float]:
        beauty = self.pass_validations.get("beauty_lit")
        if beauty is None:
            return []
        return [
            round(frame.mean[0] * 0.2126 + frame.mean[1] * 0.7152 + frame.mean[2] * 0.0722, 3)
            for frame in beauty.frames
        ]


def render_job(
    *,
    settings: Settings,
    job_path: Path,
    timeout_sec: int = 1800,
    database_path: Path | None = None,
) -> RenderJobResult:
    spec = load_jobspec(job_path)
    if spec.asset_id != "builtin:cube" and spec.scene_id is None:
        with asset_lock(
            data_dir=resolve_path(settings.data_dir, settings.project_root),
            asset_id=spec.asset_id,
        ):
            return _render_job_for_spec(
                settings=settings,
                spec=spec,
                timeout_sec=timeout_sec,
                database_path=database_path,
            )
    return _render_job_for_spec(
        settings=settings,
        spec=spec,
        timeout_sec=timeout_sec,
        database_path=database_path,
    )


def _render_job_for_spec(
    *,
    settings: Settings,
    spec: RenderJobSpec,
    timeout_sec: int,
    database_path: Path | None,
) -> RenderJobResult:
    render_asset = resolve_render_asset(settings, spec, database_path=database_path)
    run_id = _new_run_id()
    out_root = resolve_path(spec.output.dir, settings.project_root)
    run_dir = out_root / run_id / spec.asset_id.replace(":", "_")
    run_dir.mkdir(parents=True, exist_ok=False)

    manifest_path = run_dir / "manifest.json"
    ue_log_path = run_dir / "ue.log"
    setup_log_path = run_dir / "ue_setup.log"
    ue_job_path = run_dir / "job.json"
    sequence_path = f"/Game/UEF/RenderJobs/{run_id}/UEF_RenderJob_{run_id}.UEF_RenderJob_{run_id}"

    project_path = settings.project_root / "ue/UEFBase/UEFBase.uproject"
    setup_script_path = settings.project_root / "ue/UEFBase/Content/Python/uef_render_job.py"
    runtime_script_path = (
        settings.project_root / "ue/UEFBase/Content/Python/uef_render_job_runtime.py"
    )
    for label, path in {
        "UE project": project_path,
        "UE render job setup script": setup_script_path,
        "UE render job runtime executor": runtime_script_path,
    }.items():
        if not path.exists():
            raise FileNotFoundError(f"{label} not found: {path}")

    ue_job = _ue_job_payload(
        settings,
        spec,
        run_id,
        run_dir,
        sequence_path,
        render_asset=render_asset,
    )
    ue_job_path.write_text(json.dumps(ue_job, indent=2, sort_keys=True), encoding="utf-8")

    ddc_dir = settings.ddc_dir or settings.data_dir / "ddc"
    ddc_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = settings.data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ue_home = settings.ue_home
    ue_home.mkdir(parents=True, exist_ok=True)
    runtime = _runtime_settings(settings.runtime_lib_dir)
    env = {
        "HOME": str(ue_home),
        "TMPDIR": str(tmp_dir),
        "UEF_JOB_FILE": str(ue_job_path),
        "UE-LocalDataCachePath": str(ddc_dir),
    }
    if runtime["enabled"]:
        env["LD_LIBRARY_PATH"] = _prepend_env_path(Path(str(runtime["lib_dir"])), "LD_LIBRARY_PATH")

    setup_command: list[str | Path] = [
        settings.ue_root / "Engine/Binaries/Linux/UnrealEditor-Cmd",
        project_path,
        f"-ExecutePythonScript={setup_script_path}",
        "-unattended",
        "-nopause",
        "-nosplash",
        "-NullRHI",
        "-stdout",
        "-FullStdOutLogOutput",
        "-NoSound",
        "-ddc=InstalledNoZenLocalFallback",
        f"-LocalDataCachePath={ddc_dir}",
    ]
    try:
        setup_result = run_ue(
            setup_command,
            cwd=settings.project_root,
            log_path=setup_log_path,
            timeout_sec=timeout_sec,
            env=env,
        )
    except BaseException as exc:
        setup_failure_command = (
            exc.result.command
            if isinstance(exc, UERunnerError)
            else [str(part) for part in setup_command]
        )
        _write_failure_manifest(
            manifest_path,
            ue_job,
            {"setup": setup_failure_command},
            _exception_message(exc),
            runtime,
        )
        cleanup = _cleanup_local_job_assets(settings, run_id)
        _record_local_cleanup_after_failure(manifest_path, cleanup, exc)
        raise
    try:
        _raise_on_ue_summary(
            manifest_path,
            ue_job,
            {"setup": setup_result.command},
            runtime,
            "setup",
            setup_result.summary.error_count,
            setup_result.summary.warning_count,
            setup_log_path,
        )
    except BaseException as exc:
        cleanup = _cleanup_local_job_assets(settings, run_id)
        _record_local_cleanup_after_failure(manifest_path, cleanup, exc)
        raise

    width, height = spec.camera.resolution
    render_command: list[str | Path] = [
        settings.ue_root / "Engine/Binaries/Linux/UnrealEditor-Cmd",
        project_path,
        str(ue_job["map_path"]),
        "-game",
        "-RenderOffScreen",
        "-unattended",
        "-nopause",
        "-nosplash",
        "-stdout",
        "-FullStdOutLogOutput",
        "-NoSound",
        "-NoLoadingScreen",
        "-windowed",
        f"-resx={width}",
        f"-resy={height}",
        "-ddc=InstalledNoZenLocalFallback",
        f"-LocalDataCachePath={ddc_dir}",
        "-MoviePipelineLocalExecutorClass=/Script/MovieRenderPipelineCore.MoviePipelinePythonHostExecutor",
        "-ExecutorPythonClass=/Engine/PythonTypes.UEFRenderJobRuntimeExecutor",
        f"-LevelSequence={sequence_path}",
    ]
    asset_cleanup: dict[str, Any]
    try:
        ue_result = run_ue(
            render_command,
            cwd=settings.project_root,
            log_path=ue_log_path,
            timeout_sec=timeout_sec,
            env=env,
        )
    except BaseException as exc:
        render_failure_command = (
            exc.result.command
            if isinstance(exc, UERunnerError)
            else [str(part) for part in render_command]
        )
        _write_failure_manifest(
            manifest_path,
            ue_job,
            {"setup": setup_result.command, "render": render_failure_command},
            _exception_message(exc),
            runtime,
        )
        asset_cleanup = _cleanup_local_job_assets(settings, run_id)
        _record_local_cleanup_after_failure(manifest_path, asset_cleanup, exc)
        raise
    else:
        asset_cleanup = _cleanup_local_job_assets(settings, run_id)
    try:
        _raise_on_ue_summary(
            manifest_path,
            ue_job,
            {"setup": setup_result.command, "render": ue_result.command},
            runtime,
            "render",
            ue_result.summary.error_count,
            ue_result.summary.warning_count,
            ue_log_path,
        )
    except BaseException as exc:
        _record_local_cleanup_after_failure(manifest_path, asset_cleanup, exc)
        raise

    if not manifest_path.exists():
        missing_manifest_error = RuntimeError(
            f"Render job did not produce manifest; UE log: {ue_log_path}"
        )
        _write_failure_manifest(
            manifest_path,
            ue_job,
            {"setup": setup_result.command, "render": ue_result.command},
            f"Render job manifest was not created: {manifest_path}",
            runtime,
        )
        _record_local_cleanup_after_failure(
            manifest_path,
            asset_cleanup,
            missing_manifest_error,
        )
        raise missing_manifest_error
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base_manifest = {
        "asset": ue_job["asset"],
        "commands": {"setup": setup_result.command, "render": ue_result.command},
        "runtime": runtime,
        "setup_log": setup_log_path.name,
        "ue_log": ue_log_path.name,
        "setup_summary": _summary_payload(setup_result),
        "ue_summary": _summary_payload(ue_result),
        "engine": _engine_version(settings),
        "asset_cleanup": asset_cleanup,
    }
    manifest.update(base_manifest)
    runtime_failed = manifest.get("status") != "ok"
    if runtime_failed or asset_cleanup.get("status") != "ok":
        failure_reason = (
            "UE runtime reported render failure"
            if runtime_failed
            else "Local UE asset cleanup failed"
        )
        manifest["status"] = "failed"
        manifest.setdefault("error", failure_reason)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        raise RuntimeError(f"{failure_reason}; manifest: {manifest_path}")

    manifest["status"] = "validating"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    try:
        _validate_scene_sanitization(manifest, spec)
        result = _validate_render_output(
            run_dir=run_dir,
            manifest_path=manifest_path,
            setup_log_path=setup_log_path,
            ue_log_path=ue_log_path,
            spec=spec,
            render_asset=ue_job["asset"],
        )
        manifest.update(
            {
                "status": "ok",
                "frame_luma": result.frame_luma,
                "frame_paths": _relative_frame_paths(run_dir, result.frame_paths),
                "passes": stable_validation_payload(result.pass_validations),
                "artifacts": result.artifacts.manifest_payload(run_dir=run_dir)
                if result.artifacts is not None
                else {},
            }
        )
        manifest.pop("error", None)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return result
    except BaseException as exc:
        manifest["status"] = "failed"
        detail = str(exc).strip()
        error_summary = type(exc).__name__ + (f": {detail}" if detail else "")
        manifest["error"] = f"Host validation/artifact failure: {error_summary}"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        raise


def _validate_scene_sanitization(manifest: dict[str, Any], spec: RenderJobSpec) -> None:
    payload = manifest.get("scene_sanitization")
    if spec.asset_id == "builtin:cube":
        if payload != {"policy": "not_applicable"}:
            raise RuntimeError("Builtin render must retain the M1 scene contract")
        return
    if not isinstance(payload, dict):
        raise RuntimeError("Catalog render is missing scene sanitization evidence")
    if payload.get("policy") != "catalog_hide_all_pawns_v2":
        raise RuntimeError("Catalog render used an unsupported scene sanitization policy")
    subjobs = payload.get("subjobs")
    if not isinstance(subjobs, list) or len(subjobs) != len(spec.passes):
        actual_count = 0 if not isinstance(subjobs, list) else len(subjobs)
        raise RuntimeError(
            "Catalog scene sanitization must cover every MRQ subjob: "
            f"expected={len(spec.passes)} actual={actual_count}"
        )
    expected_indices = list(range(len(spec.passes)))
    actual_indices = [item.get("subjob_index") for item in subjobs if isinstance(item, dict)]
    if actual_indices != expected_indices:
        raise RuntimeError(
            "Catalog scene sanitization subjob indices are incomplete: "
            f"expected={expected_indices} actual={actual_indices}"
        )
    for item in subjobs:
        assert isinstance(item, dict)
        count = item.get("hidden_pawn_count")
        editor_count = item.get("editor_hidden_pawn_count")
        meshes = item.get("hidden_static_meshes")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise RuntimeError("Catalog scene sanitization has an invalid pawn count")
        if (
            not isinstance(editor_count, int)
            or isinstance(editor_count, bool)
            or editor_count != count
        ):
            raise RuntimeError("Catalog scene sanitization has an invalid editor-hidden count")
        if not isinstance(meshes, list) or any(not isinstance(path, str) for path in meshes):
            raise RuntimeError("Catalog scene sanitization has an invalid mesh inventory")


def render_job_remote(
    *,
    settings: Settings,
    host: str,
    job_path: Path,
    timeout_sec: int = 1800,
    poll_interval_sec: int = 30,
) -> RenderJobResult:
    spec = load_jobspec(job_path)
    if spec.asset_id != "builtin:cube":
        raise ValueError("Remote catalog-asset rendering is not supported until M4 package sync")
    remote = RemoteHost.from_settings(settings, host)
    runtime = _prepare_remote_smoke_runtime(remote, run_user="uef")
    run_user = str(runtime.get("run_user") or "")
    run_id = _new_run_id()
    out_root = resolve_path(spec.output.dir, settings.project_root)
    run_dir = out_root / run_id / spec.asset_id.replace(":", "_")
    run_dir.mkdir(parents=True, exist_ok=False)

    job_id = f"render_{remote.config.name}_{run_id}"
    remote_run_dir = PurePosixPath(str(remote.config.work_dir)) / "jobs" / job_id
    remote_project_dir = remote_run_dir / "project"
    remote_output_dir = remote_run_dir / "out" / spec.asset_id.replace(":", "_")
    sequence_path = f"/Game/UEF/RenderJobs/{run_id}/UEF_RenderJob_{run_id}.UEF_RenderJob_{run_id}"
    manifest_path = run_dir / "manifest.json"
    result: RenderJobResult | None = None
    primary_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    stop_error: BaseException | None = None
    cleanup_needed = False
    tmux_start_attempted = False
    project_package: Path | None = None
    try:
        project_package = _package_render_project(settings, run_dir / "project_package")
        (project_package / "remote_runner.py").write_text(
            _REMOTE_RENDER_JOB_SCRIPT,
            encoding="utf-8",
        )
        cleanup_needed = True
        remote.run(
            "\n".join(
                [
                    "set -euo pipefail",
                    f"mkdir -p {shlex.quote(str(remote_run_dir))}",
                    f"mkdir -p {shlex.quote(str(remote_project_dir))}",
                    f"mkdir -p {shlex.quote(str(remote_output_dir))}",
                ]
            ),
            timeout_sec=60,
        )
        remote.rsync_push([f"{project_package}/"], f"{remote_project_dir}/", timeout_sec=3600)
        shutil.rmtree(project_package)
        project_package = None

        remote_hdri_file = _sync_remote_hdri(
            settings=settings,
            remote=remote,
            spec=spec,
            remote_run_dir=remote_run_dir,
        )
        lighting = _remote_lighting_payload(spec, remote_hdri_file)
        ue_job = _ue_job_payload_with_lighting(
            spec=spec,
            run_id=run_id,
            run_dir=Path(str(remote_output_dir)),
            sequence_path=sequence_path,
            lighting=lighting,
        )
        remote_job_path = run_dir / "remote_job.json"
        remote_job_path.write_text(json.dumps(ue_job, indent=2, sort_keys=True), encoding="utf-8")
        remote.rsync_push([remote_job_path], f"{remote_run_dir}/job.json", timeout_sec=3600)

        command = _remote_runner_command(
            remote_project_dir / "remote_runner.py",
            {
                "UEF_HOST_NAME": remote.config.name,
                "UEF_JOB_ID": job_id,
                "UEF_WORK_DIR": str(remote.config.work_dir),
                "UEF_ENGINE_DIR": str(remote.config.engine_dir),
                "UEF_REMOTE_RUN_DIR": str(remote_run_dir),
                "UEF_REMOTE_PROJECT_DIR": str(remote_project_dir),
                "UEF_REMOTE_RUN_USER": run_user,
                "UEF_TIMEOUT_SEC": str(timeout_sec),
            },
        )
        tmux_start_attempted = True
        remote.tmux_start(job_id, command, timeout_sec=60)
        status = _wait_for_remote_render_job(
            remote,
            job_id,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        remote.rsync_pull([f"{remote_output_dir}/"], run_dir, timeout_sec=3600, delete=True)
        if status.get("status") != "complete":
            raise RuntimeError(f"Remote render failed on {host}: {status}")
        if not manifest_path.exists():
            raise RuntimeError(f"Remote render did not return manifest: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "ok":
            raise RuntimeError(f"Remote UE runtime reported failure on {host}: {manifest_path}")
        manifest["status"] = "validating"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        result = _validate_render_output(
            run_dir=run_dir,
            manifest_path=manifest_path,
            ue_log_path=run_dir / "ue.log",
            setup_log_path=run_dir / "ue_setup.log",
            spec=spec,
        )
        manifest.update(
            {
                "status": "ok",
                "remote_host": remote.config.name,
                "remote_job_id": job_id,
                "frame_luma": result.frame_luma,
                "frame_paths": _relative_frame_paths(run_dir, result.frame_paths),
                "setup_log": "ue_setup.log",
                "ue_log": "ue.log",
                "passes": stable_validation_payload(result.pass_validations),
                "artifacts": result.artifacts.manifest_payload(run_dir=run_dir)
                if result.artifacts is not None
                else {},
                "local_validation": {"status": "ok", "validated_utc": utc_timestamp()},
            }
        )
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    except BaseException as exc:
        primary_error = exc
        try:
            failed_manifest = (
                json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.exists()
                else {}
            )
        except Exception as manifest_exc:
            failed_manifest = {
                "manifest_read_error": _error_payload(manifest_exc),
            }
        failed_manifest.setdefault("schema_version", 2)
        failed_manifest.setdefault("render_kind", "job")
        failed_manifest.setdefault("job", spec.raw)
        failed_manifest.setdefault("remote_host", remote.config.name)
        failed_manifest.setdefault("remote_job_id", job_id)
        failed_manifest["status"] = "failed"
        failed_manifest["orchestration_error"] = _error_payload(exc)
        if not failed_manifest.get("error"):
            failed_manifest["error"] = f"Remote render failure: {type(exc).__name__}: {exc}"
        try:
            manifest_path.write_text(
                json.dumps(failed_manifest, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except BaseException as manifest_write_exc:
            cleanup_error = cleanup_error or manifest_write_exc
    finally:
        if project_package is not None and project_package.exists():
            try:
                shutil.rmtree(project_package)
            except BaseException as exc:
                cleanup_error = exc
        safe_to_remove_remote_tree = True
        if primary_error is not None and tmux_start_attempted:
            try:
                remote.tmux_stop(job_id, timeout_sec=60)
            except BaseException as exc:
                stop_error = exc
                cleanup_error = cleanup_error or exc
                safe_to_remove_remote_tree = False
        if cleanup_needed:
            if safe_to_remove_remote_tree:
                path_cleanup_error: BaseException | None
                try:
                    cleanup, cleanup_exception = _cleanup_remote_paths(
                        remote,
                        [remote_run_dir],
                        timeout_sec=60,
                    )
                    path_cleanup_error = cleanup_exception
                except BaseException as exc:
                    path_cleanup_error = exc
                    cleanup = {
                        "status": "failed",
                        "removed_paths": [],
                        "retained_paths": [str(remote_run_dir)],
                        "verified": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                cleanup_error = cleanup_error or path_cleanup_error
            else:
                cleanup = {
                    "status": "failed",
                    "removed_paths": [],
                    "retained_paths": [str(remote_run_dir)],
                    "verified": False,
                    "error_type": type(stop_error).__name__,
                    "error": (
                        "Remote tree retained because the job process/session "
                        f"could not be stopped safely: {stop_error}"
                    ),
                }
            try:
                _record_remote_cleanup(manifest_path, cleanup)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None and manifest_path.exists():
            try:
                failed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                failed_manifest["status"] = "failed"
                failed_manifest["cleanup_error"] = _error_payload(cleanup_error)
                if not failed_manifest.get("error"):
                    failed_manifest["error"] = (
                        f"Remote cleanup failure: {type(cleanup_error).__name__}: {cleanup_error}"
                    )
                manifest_path.write_text(
                    json.dumps(failed_manifest, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
    if primary_error is not None:
        if cleanup_error is not None:
            primary_error.add_note(f"Remote cleanup also failed: {cleanup_error}")
        raise primary_error
    if cleanup_error is not None:
        raise RuntimeError(
            f"Remote render cleanup failed or could not be recorded for {remote_run_dir}"
        ) from cleanup_error
    if result is None:
        raise RuntimeError(f"Remote render produced no result for {job_id}")
    return result


def _error_payload(error: BaseException) -> dict[str, str]:
    return {
        "type": type(error).__name__,
        "message": str(error),
    }


def _exception_message(error: BaseException) -> str:
    message = str(error).strip()
    return message or type(error).__name__


def compare_job_outputs(first: RenderJobResult, second: RenderJobResult) -> dict[str, Any]:
    """Require two jobs to have identical decoded, validated pass outputs."""
    if set(first.pass_validations) != set(second.pass_validations):
        raise ValueError(
            f"Pass mismatch: {sorted(first.pass_validations)} != {sorted(second.pass_validations)}"
        )
    first_payload = stable_validation_payload(first.pass_validations)
    second_payload = stable_validation_payload(second.pass_validations)
    if first_payload != second_payload:
        raise RuntimeError("Render job decoded pass output mismatch")
    if len(first.frame_luma) != len(second.frame_luma):
        raise ValueError(
            f"Frame count mismatch: {len(first.frame_luma)} != {len(second.frame_luma)}"
        )
    return first_payload


def _validate_render_output(
    *,
    run_dir: Path,
    manifest_path: Path,
    setup_log_path: Path,
    ue_log_path: Path,
    spec: RenderJobSpec,
    render_asset: dict[str, Any] | None = None,
) -> RenderJobResult:
    frame_paths = {
        pass_name: sorted((run_dir / pass_name).glob("frame_*.*")) for pass_name in spec.passes
    }
    for paths in frame_paths.values():
        png_paths = [path for path in paths if path.suffix.lower() == ".png"]
        if png_paths:
            canonicalize_png_frames(png_paths)
    if spec.scene_id is not None:
        if not isinstance(render_asset, dict):
            raise RuntimeError("Scene render validation is missing its build-generation payload")
        stencil_payload = render_asset.get("expected_object_stencil_ids")
        static_actor_count = render_asset.get("static_mesh_actor_count")
        if (
            not isinstance(stencil_payload, list)
            or isinstance(static_actor_count, bool)
            or not isinstance(static_actor_count, int)
            or stencil_payload != list(range(1, static_actor_count + 1))
        ):
            raise RuntimeError("Scene render has an invalid object-stencil inventory")
        coverage_payload = render_asset.get("minimum_object_stencil_coverage")
        if (
            isinstance(coverage_payload, bool)
            or not isinstance(coverage_payload, int | float)
            or not math.isfinite(float(coverage_payload))
            or not 0.6 <= float(coverage_payload) <= 1.0
        ):
            raise RuntimeError("Scene render has an invalid object-stencil coverage policy")
        expected_object_stencil_ids = tuple(stencil_payload)
        object_stencil_coverage: Literal["every_frame", "sequence_union"] = "sequence_union"
        minimum_object_stencil_coverage = float(coverage_payload)
        foreground_stencil_ids = expected_object_stencil_ids
    else:
        expected_object_stencil_ids = (1, 2)
        object_stencil_coverage = "every_frame"
        minimum_object_stencil_coverage = 1.0
        foreground_stencil_ids = (1,)
    pass_validations = {
        pass_name: validate_render_pass(
            pass_name,
            paths,
            expected_frames=spec.frame_count,
            expected_resolution=spec.camera.resolution,
            lighting_preset=spec.lighting.preset,
            expected_object_stencil_ids=expected_object_stencil_ids,
            object_stencil_coverage=object_stencil_coverage,
            minimum_object_stencil_coverage=minimum_object_stencil_coverage,
        )
        for pass_name, paths in frame_paths.items()
    }
    if {"beauty_lit", "beauty_unlit"}.issubset(frame_paths):
        assert_passes_distinct(
            pass_frames=frame_paths,
            first_pass="beauty_lit",
            second_pass="beauty_unlit",
        )
    if "object_mask" in frame_paths:
        assert_object_mask_visibility(
            pass_frames=frame_paths,
            foreground_stencil_ids=foreground_stencil_ids,
        )
    artifacts = create_render_artifacts(
        run_dir=run_dir,
        frame_paths=frame_paths,
        manifest_path=manifest_path,
    )
    return RenderJobResult(
        run_dir=run_dir,
        manifest_path=manifest_path,
        ue_log_path=ue_log_path,
        setup_log_path=setup_log_path,
        frame_paths=frame_paths,
        pass_validations=pass_validations,
        spec=spec,
        artifacts=artifacts,
    )


def _relative_frame_paths(
    run_dir: Path, frame_paths: dict[str, list[Path]]
) -> dict[str, list[str]]:
    return {
        pass_name: [path.relative_to(run_dir).as_posix() for path in paths]
        for pass_name, paths in frame_paths.items()
    }


def _engine_version(settings: Settings) -> dict[str, Any]:
    version_path = settings.ue_root / "Engine/Build/Build.version"
    if not version_path.exists():
        raise FileNotFoundError(f"UE Build.version not found: {version_path}")
    payload = json.loads(version_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"UE Build.version must be an object: {version_path}")
    return payload


def _cleanup_local_job_assets(settings: Settings, run_id: str) -> dict[str, Any]:
    render_jobs_root = (settings.project_root / "ue/UEFBase/Content/UEF/RenderJobs").resolve()
    target = (render_jobs_root / run_id).resolve()
    cleanup: dict[str, Any] = {
        "path": target.relative_to(settings.project_root).as_posix(),
        "status": "ok",
        "removed": False,
    }
    if target.parent != render_jobs_root:
        raise ValueError(f"Unsafe local render asset cleanup path: {target}")
    try:
        if target.exists():
            shutil.rmtree(target)
            cleanup["removed"] = True
    except Exception as exc:
        cleanup.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
    return cleanup


def _record_local_cleanup_after_failure(
    manifest_path: Path,
    cleanup: dict[str, Any],
    primary_error: BaseException,
) -> None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("failure manifest must be a JSON object")
        manifest["asset_cleanup"] = cleanup
        if cleanup.get("status") != "ok":
            manifest["cleanup_error"] = {
                "type": str(cleanup.get("error_type") or "CleanupError"),
                "message": str(cleanup.get("error") or "Local UE asset cleanup failed"),
            }
            manifest["status"] = "failed"
            manifest.setdefault("error", "Local UE asset cleanup failed")
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as exc:
        primary_error.add_note(f"Could not record local UE asset cleanup: {exc}")
    if cleanup.get("status") != "ok":
        primary_error.add_note(
            "Local UE asset cleanup also failed: "
            f"{cleanup.get('error_type')}: {cleanup.get('error')}"
        )


def _new_run_id() -> str:
    return f"{utc_timestamp()}_{uuid4().hex[:8]}"


def _ue_job_payload(
    settings: Settings,
    spec: RenderJobSpec,
    run_id: str,
    run_dir: Path,
    sequence_path: str,
    *,
    render_asset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _ue_job_payload_with_lighting(
        spec=spec,
        run_id=run_id,
        run_dir=run_dir,
        sequence_path=sequence_path,
        lighting=_lighting_payload(settings, spec),
        render_asset=render_asset or _render_asset_payload(settings, spec),
    )
    payload["engine"] = _engine_version(settings)
    return payload


def _ue_job_payload_with_lighting(
    *,
    spec: RenderJobSpec,
    run_id: str,
    run_dir: Path,
    sequence_path: str,
    lighting: dict[str, Any],
    render_asset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    width, height = spec.camera.resolution
    beauty_sequence_name = f"UEF_RenderJobBeauty_{run_id}"
    beauty_sequence_path = (
        f"/Game/UEF/RenderJobs/{run_id}/{beauty_sequence_name}.{beauty_sequence_name}"
        if lighting["preset"] == "hdri"
        else sequence_path
    )
    return {
        "asset": render_asset or _builtin_render_asset_payload(spec),
        "asset_id": spec.asset_id,
        "camera": {
            "rig": spec.camera.rig,
            "views": spec.camera.views,
            "elevation_deg": spec.camera.elevation_deg,
            "fov": spec.camera.fov,
            "resolution": [width, height],
        },
        "frames": spec.frame_count,
        "job": spec.raw,
        "lighting": lighting,
        "out_dir": str(run_dir),
        "passes": list(spec.passes),
        "render_kind": "job",
        "run_id": run_id,
        "schema_version": 3,
        "map_path": f"/Game/UEF/RenderJobs/{run_id}/UEF_RenderWorld_{run_id}",
        "sequence_path": sequence_path,
        "beauty_sequence_path": beauty_sequence_path,
    }


def _render_asset_payload(
    settings: Settings,
    spec: RenderJobSpec,
    *,
    database_path: Path | None = None,
) -> dict[str, Any]:
    # Import lazily so importing scene specs cannot recurse through
    # ingest.pipeline -> render.thumbnails -> render.job.
    from uefactory.ingest.package_evidence import is_valid_package_bundle_evidence
    from uefactory.ingest.source_structure import is_valid_source_structure_evidence

    if spec.asset_id == "builtin:cube":
        return _builtin_render_asset_payload(spec)
    resolved_database = database_path or settings.data_dir / "catalog.db"
    if not resolved_database.is_absolute():
        resolved_database = settings.project_root / resolved_database
    catalog = Catalog(resolved_database, project_root=settings.project_root)
    if spec.scene_id is not None:
        return _render_scene_payload(settings, spec, catalog)
    record = catalog.get_asset(spec.asset_id)
    if record is None:
        raise ValueError(f"Catalog asset not found: {spec.asset_id}")
    if record.status not in {"imported", "render_ok"} or record.ue_package_path is None:
        raise ValueError(
            f"Catalog asset {spec.asset_id!r} is not renderable: status={record.status!r}"
        )
    if record.material_count is None or record.material_count <= 0:
        raise ValueError(
            f"Catalog asset {spec.asset_id!r} requires at least one material for thumbnails"
        )

    quality_policy = static_mesh_quality_policy(
        require_single_static_mesh=True,
        require_texture_references="textured" in record.tags,
    )

    artifacts = sorted(
        catalog.list_artifacts(asset_id=spec.asset_id),
        key=lambda item: (item.created_at, item.artifact_id),
        reverse=True,
    )
    for artifact in artifacts:
        if artifact.kind != "import_manifest" or artifact.sha256 is None:
            continue
        manifest_path = settings.project_root / artifact.path
        if not _regular_project_file(settings.project_root, manifest_path):
            continue
        if _file_sha256(manifest_path) != artifact.sha256:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        requested_normalization = _catalog_requested_normalization(manifest)
        if requested_normalization is None:
            continue
        material_postprocess = manifest.get("material_postprocess")
        bundle_sha256 = manifest.get("bundle_sha256")
        source_format = artifact.params.get("source_format")
        record_source_format = Path(record.raw_path).suffix.lower().removeprefix(".")
        source_structure = manifest.get("source_structure")
        source_structure_sha256 = manifest.get("source_structure_sha256")
        package_evidence = manifest.get("ue_package_bundle")
        if (
            manifest.get("schema_version") != IMPORT_MANIFEST_SCHEMA_VERSION
            or artifact.params.get("schema_version") != IMPORT_ARTIFACT_SCHEMA_VERSION
            or not isinstance(bundle_sha256, str)
            or artifact.params.get("bundle_sha256") != bundle_sha256
            or artifact.params.get("content_sha256") != record.sha256
            or artifact.params.get("quality_ruleset_version") != QUALITY_RULESET_VERSION
            or artifact.params.get("quality_policy") != quality_policy
            or artifact.params.get("requested_normalization") != requested_normalization
            or artifact.params.get("import_backend") != "asset_tools_auto"
            or artifact.params.get("engine_normalization") != manifest.get("normalization")
            or not isinstance(material_postprocess, dict)
            or artifact.params.get("material_postprocess_policy")
            != material_postprocess.get("policy")
            or artifact.params.get("source_structure") != source_structure
            or artifact.params.get("source_structure_sha256") != source_structure_sha256
            or artifact.params.get("ue_package_bundle") != package_evidence
            or not isinstance(source_format, str)
            or source_format != record_source_format
            or not is_valid_source_structure_evidence(
                source_structure,
                source_structure_sha256,
                expected_source_format=source_format,
            )
            or not is_current_passed_quality(
                manifest.get("quality"),
                require_single_static_mesh=quality_policy["require_single_static_mesh"],
                require_texture_references=quality_policy["require_texture_references"],
            )
            or not _valid_engine_normalization(manifest.get("normalization"))
        ):
            continue
        transaction = manifest.get("transaction")
        if (
            manifest.get("status") != "ok"
            or manifest.get("asset_id") != spec.asset_id
            or manifest.get("content_sha256") != record.sha256
            or not isinstance(transaction, dict)
            or transaction.get("state") != "committed"
        ):
            continue
        meshes = manifest.get("static_meshes")
        if not isinstance(meshes, list):
            continue
        mesh = next(
            (
                item
                for item in meshes
                if isinstance(item, dict) and item.get("object_path") == record.ue_package_path
            ),
            None,
        )
        if mesh is None:
            continue
        if (
            mesh.get("triangle_count") != record.tri_count
            or mesh.get("material_count") != record.material_count
            or not record.ue_package_path.startswith(f"/Game/UEF/Ingested/{spec.asset_id}/")
        ):
            continue
        imported_paths = manifest.get("imported_object_paths")
        if (
            not isinstance(imported_paths, list)
            or not imported_paths
            or any(not isinstance(path, str) for path in imported_paths)
            or not is_valid_package_bundle_evidence(
                settings.project_root,
                asset_id=spec.asset_id,
                imported_object_paths=imported_paths,
                evidence=package_evidence,
            )
        ):
            continue
        assert isinstance(package_evidence, dict)
        geometry = _catalog_geometry_payload(
            mesh.get("bounds_cm"),
            resolution=spec.camera.resolution,
            horizontal_fov_deg=spec.camera.fov,
            requested_normalization=requested_normalization,
        )
        return {
            "kind": "catalog",
            "asset_id": spec.asset_id,
            "mesh_path": record.ue_package_path,
            "bundle_sha256": bundle_sha256,
            "content_sha256": record.sha256,
            "import_manifest": artifact.path,
            "ue_package_bundle_sha256": package_evidence["package_bundle_sha256"],
            "preserve_materials": True,
            **geometry,
        }
    raise RuntimeError(
        f"Catalog asset {spec.asset_id!r} has no valid import manifest/package inventory"
    )


def resolve_render_asset(
    settings: Settings,
    spec: RenderJobSpec,
    *,
    database_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve and validate the current builtin, model, or scene generation."""

    return _render_asset_payload(settings, spec, database_path=database_path)


def _render_scene_payload(
    settings: Settings,
    spec: RenderJobSpec,
    catalog: Catalog,
) -> dict[str, Any]:
    scene_id = spec.scene_id
    if scene_id is None:  # pragma: no cover - guarded by the caller
        raise ValueError("scene render payload requires a scene reference")
    record = catalog.get_scene(scene_id)
    if record is None:
        raise ValueError(f"Catalog scene not found: {scene_id}")
    if record.status not in {"built", "render_ok"} or record.map_path is None:
        raise ValueError(f"Catalog scene {scene_id!r} is not renderable: status={record.status!r}")
    if record.build_sha256 is None:
        raise RuntimeError(f"Catalog scene {scene_id!r} has no active build generation")
    if record.source_file is None:
        raise RuntimeError(f"Catalog scene {scene_id!r} has no source-file provenance")
    source_file = Path(record.source_file)
    if not source_file.is_absolute():
        source_file = settings.project_root / source_file
    if (
        source_file.is_symlink()
        or not source_file.is_file()
        or _file_sha256(source_file) != record.source_sha256
    ):
        raise RuntimeError(f"Catalog scene source provenance changed: {record.source_file}")
    map_file = _ue_map_file(settings.project_root, record.map_path)
    if not _regular_project_file(settings.project_root, map_file):
        raise RuntimeError(f"Catalog scene map package is missing: {record.map_path}")

    artifacts = sorted(
        catalog.list_scene_artifacts(scene_id=scene_id),
        key=lambda item: (item.created_at, item.artifact_id),
        reverse=True,
    )
    for artifact in artifacts:
        if (
            artifact.kind != "scene_build_manifest"
            or artifact.sha256 != record.build_sha256
            or artifact.params.get("build_sha256") != record.build_sha256
        ):
            continue
        manifest_path = settings.project_root / artifact.path
        if not _regular_project_file(settings.project_root, manifest_path):
            continue
        if _file_sha256(manifest_path) != record.build_sha256:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict) or manifest.get("status") != "ok":
            continue
        inventory = manifest.get("inventory")
        scene_spec = manifest.get("scene_spec")
        if not isinstance(inventory, dict) or not isinstance(scene_spec, dict):
            continue
        inventory_sha256 = _canonical_digest(inventory)
        if (
            manifest.get("scene_id") != scene_id
            or manifest.get("map_path") != record.map_path
            or manifest.get("source_sha256") != record.source_sha256
            or manifest.get("scene_spec_sha256") != record.spec_sha256
            or _canonical_digest(scene_spec) != record.spec_sha256
            or manifest.get("inventory_sha256") != inventory_sha256
            or artifact.params.get("source_sha256") != record.source_sha256
            or artifact.params.get("scene_spec_sha256") != record.spec_sha256
            or artifact.params.get("inventory_sha256") != inventory_sha256
            or artifact.params.get("map_path") != record.map_path
            or inventory.get("actor_count") != record.actor_count
            or inventory.get("static_mesh_count") != record.static_mesh_count
            or inventory.get("triangle_count") != record.triangle_count
            or inventory.get("material_count") != record.material_count
            or inventory.get("texture_count") != record.texture_count
            or inventory.get("aggregate_bounds_cm") != record.bounds
        ):
            continue
        if manifest.get("source_file") != record.source_file:
            continue
        package_bundle_sha256 = _validate_scene_package_evidence(
            settings.project_root,
            inventory=inventory,
            manifest=manifest,
            artifact_params=artifact.params,
        )
        if package_bundle_sha256 is None:
            continue
        render_inventory = _scene_render_inventory_payload(inventory)
        if render_inventory is None:
            continue
        source_spec = scene_spec.get("source")
        if not isinstance(source_spec, dict) or any(
            source_spec.get(key) != expected
            for key, expected in {
                "source": record.source,
                "source_id": record.source_id,
                "source_url": record.source_url,
                "license": record.license,
                "license_tier": record.license_tier,
                "license_url": record.license_url,
                "attribution": record.attribution,
            }.items()
        ):
            continue
        camera = scene_spec.get("camera")
        if not isinstance(camera, dict):
            continue
        distance_multiplier = camera.get("distance_multiplier")
        yaw = camera.get("yaw")
        pitch = camera.get("pitch")
        if (
            isinstance(distance_multiplier, bool)
            or not isinstance(distance_multiplier, int | float)
            or not math.isfinite(float(distance_multiplier))
            or float(distance_multiplier) <= 0.0
            or isinstance(yaw, bool)
            or not isinstance(yaw, int | float)
            or not math.isfinite(float(yaw))
            or isinstance(pitch, bool)
            or not isinstance(pitch, int | float)
            or not math.isfinite(float(pitch))
            or not -89.0 <= -float(pitch) <= 89.0
        ):
            continue
        build = scene_spec.get("build")
        render_policy = scene_spec.get("render")
        lighting_intensity_multiplier = (
            render_policy.get("lighting_intensity_multiplier", 1.0)
            if isinstance(render_policy, dict)
            else None
        )
        minimum_object_stencil_coverage = (
            render_policy.get("minimum_object_stencil_coverage", 0.8)
            if isinstance(render_policy, dict)
            else None
        )
        maximum_background_contamination_ratio = (
            render_policy.get("maximum_background_contamination_ratio", 0.001)
            if isinstance(render_policy, dict)
            else None
        )
        if (
            not isinstance(build, dict)
            or build.get("map_path") != record.map_path
            or not isinstance(build.get("export"), bool)
            or not isinstance(render_policy, dict)
            or render_policy.get("no_auto_floor") is not True
            or isinstance(lighting_intensity_multiplier, bool)
            or not isinstance(lighting_intensity_multiplier, int | float)
            or not math.isfinite(float(lighting_intensity_multiplier))
            or not 0.1 <= float(lighting_intensity_multiplier) <= 100.0
            or isinstance(minimum_object_stencil_coverage, bool)
            or not isinstance(minimum_object_stencil_coverage, int | float)
            or not math.isfinite(float(minimum_object_stencil_coverage))
            or not 0.6 <= float(minimum_object_stencil_coverage) <= 1.0
            or isinstance(maximum_background_contamination_ratio, bool)
            or not isinstance(maximum_background_contamination_ratio, int | float)
            or not math.isfinite(float(maximum_background_contamination_ratio))
            or not 0.001 <= float(maximum_background_contamination_ratio) <= 0.01
            or artifact.params.get("license") != record.license
            or artifact.params.get("license_tier") != record.license_tier
            or artifact.params.get("license_url") != record.license_url
            or artifact.params.get("attribution") != record.attribution
            or artifact.params.get("export") != build.get("export")
        ):
            continue
        geometry = _scene_geometry_payload(
            record.bounds,
            resolution=spec.camera.resolution,
            horizontal_fov_deg=spec.camera.fov,
            distance_multiplier=float(distance_multiplier),
        )
        expected_stencil_ids = list(range(1, int(inventory["static_mesh_actor_count"]) + 1))
        return {
            "kind": "scene",
            "asset_id": spec.asset_id,
            "scene_id": scene_id,
            "scene_map_path": record.map_path,
            "scene_build_manifest": artifact.path,
            "build_sha256": record.build_sha256,
            "package_bundle_sha256": package_bundle_sha256,
            "inventory_sha256": inventory_sha256,
            "source": record.source,
            "source_id": record.source_id,
            "source_url": record.source_url,
            "source_file": record.source_file,
            "source_sha256": record.source_sha256,
            "scene_spec_sha256": record.spec_sha256,
            "license": record.license,
            "license_tier": record.license_tier,
            "license_url": record.license_url,
            "attribution": record.attribution,
            "export": build["export"],
            "actor_count": int(inventory["actor_count"]),
            "static_mesh_actor_count": int(inventory["static_mesh_actor_count"]),
            "static_mesh_component_count": int(inventory["static_mesh_component_count"]),
            "render_inventory": render_inventory,
            "render_inventory_sha256": _canonical_digest(render_inventory),
            "expected_object_stencil_ids": expected_stencil_ids,
            "camera_azimuth_offset_deg": float(yaw),
            "camera_elevation_deg": -float(pitch),
            "lighting_intensity_multiplier": float(lighting_intensity_multiplier),
            "minimum_object_stencil_coverage": float(minimum_object_stencil_coverage),
            "maximum_background_contamination_ratio": float(maximum_background_contamination_ratio),
            "preserve_materials": True,
            "no_auto_floor": True,
            **geometry,
        }
    raise RuntimeError(f"Catalog scene {scene_id!r} has no valid build manifest/package inventory")


def _validate_scene_package_evidence(
    project_root: Path,
    *,
    inventory: dict[str, Any],
    manifest: dict[str, Any],
    artifact_params: dict[str, Any],
) -> str | None:
    assets = inventory.get("assets")
    packages = manifest.get("packages")
    if not isinstance(assets, list) or not assets or not isinstance(packages, list):
        return None
    if len(packages) != len(assets):
        return None
    expected_assets: dict[str, str] = {}
    for item in assets:
        if not isinstance(item, dict) or set(item) != {"object_path", "class"}:
            return None
        object_path = item.get("object_path")
        class_name = item.get("class")
        if (
            not isinstance(object_path, str)
            or not isinstance(class_name, str)
            or object_path in expected_assets
        ):
            return None
        expected_assets[object_path] = class_name
    if list(expected_assets) != sorted(expected_assets):
        return None

    seen_paths: set[str] = set()
    for item in packages:
        if not isinstance(item, dict) or set(item) != {
            "object_path",
            "class",
            "path",
            "size",
            "sha256",
        }:
            return None
        object_path = item.get("object_path")
        class_name = item.get("class")
        relative_path = item.get("path")
        size = item.get("size")
        sha256 = item.get("sha256")
        if (
            not isinstance(object_path, str)
            or expected_assets.get(object_path) != class_name
            or object_path in seen_paths
            or not isinstance(relative_path, str)
            or not relative_path
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or not _is_sha256(sha256)
        ):
            return None
        seen_paths.add(object_path)
        package_file = (
            _ue_map_file(project_root, object_path)
            if class_name == "World"
            else _ue_object_file(project_root, object_path)
        )
        if not _regular_project_file(project_root, package_file):
            return None
        if package_file.resolve().relative_to(project_root.resolve()).as_posix() != relative_path:
            return None
        stat = package_file.stat()
        if stat.st_size != size or _file_sha256(package_file) != sha256:
            return None
    if seen_paths != set(expected_assets):
        return None
    if [item["object_path"] for item in packages] != sorted(seen_paths):
        return None
    bundle_sha256 = _canonical_digest(packages)
    if (
        manifest.get("package_bundle_sha256") != bundle_sha256
        or artifact_params.get("package_bundle_sha256") != bundle_sha256
    ):
        return None
    return bundle_sha256


def _scene_render_inventory_payload(inventory: dict[str, Any]) -> dict[str, Any] | None:
    actors = inventory.get("actors")
    static_meshes = inventory.get("static_meshes")
    if not isinstance(actors, list) or not actors:
        return None
    if not isinstance(static_meshes, list) or not static_meshes:
        return None
    imported_mesh_paths = {
        item.get("object_path")
        for item in static_meshes
        if isinstance(item, dict) and isinstance(item.get("object_path"), str)
    }
    if len(imported_mesh_paths) != len(static_meshes):
        return None
    expected_actor_count = inventory.get("actor_count")
    expected_static_actors = inventory.get("static_mesh_actor_count")
    expected_components = inventory.get("static_mesh_component_count")
    if (
        isinstance(expected_actor_count, bool)
        or not isinstance(expected_actor_count, int)
        or expected_actor_count != len(actors)
        or isinstance(expected_static_actors, bool)
        or not isinstance(expected_static_actors, int)
        or not 1 <= expected_static_actors <= 255
        or isinstance(expected_components, bool)
        or not isinstance(expected_components, int)
        or expected_components < expected_static_actors
    ):
        return None
    actor_names: set[str] = set()
    referenced_mesh_paths: set[str] = set()
    static_actor_count = 0
    component_count = 0
    for actor in actors:
        if not _valid_scene_actor_inventory_row(actor):
            return None
        assert isinstance(actor, dict)
        actor_name = actor.get("actor_name")
        components = actor.get("components")
        assert isinstance(actor_name, str)
        assert isinstance(components, list)
        if actor_name in actor_names or actor.get("object_id") != actor_name:
            return None
        actor_names.add(actor_name)
        if components:
            static_actor_count += 1
        referenced_mesh_paths.update(component["mesh_path"] for component in components)
        component_count += len(components)
    if (
        static_actor_count != expected_static_actors
        or component_count != expected_components
        or [actor["actor_name"] for actor in actors] != sorted(actor_names)
        or referenced_mesh_paths != imported_mesh_paths
        or inventory.get("static_mesh_count") != len(imported_mesh_paths)
    ):
        return None
    return {
        "schema_version": 1,
        "actors": actors,
        "static_mesh_actor_count": expected_static_actors,
        "static_mesh_component_count": expected_components,
    }


def _valid_scene_actor_inventory_row(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "object_id",
        "actor_name",
        "actor_label",
        "actor_class",
        "parent_actor_name",
        "transform",
        "components",
    }:
        return False
    if any(
        not isinstance(value.get(key), str) or not value[key]
        for key in ("object_id", "actor_name", "actor_label", "actor_class")
    ):
        return False
    parent = value.get("parent_actor_name")
    if parent is not None and (not isinstance(parent, str) or not parent):
        return False
    transform = value.get("transform")
    if not isinstance(transform, dict) or set(transform) != {
        "translation_cm",
        "rotation_deg",
        "scale",
    }:
        return False
    if any(not _finite_scene_vector(transform[key]) for key in transform):
        return False
    components = value.get("components")
    if not isinstance(components, list):
        return False
    component_keys = {"name", "mesh_path", "materials", "world_bounds_cm"}
    for component in components:
        if not isinstance(component, dict) or set(component) != component_keys:
            return False
        if any(
            not isinstance(component.get(key), str) or not component[key]
            for key in ("name", "mesh_path")
        ):
            return False
        materials = component.get("materials")
        if not isinstance(materials, list) or any(
            material is not None and (not isinstance(material, str) or not material)
            for material in materials
        ):
            return False
        bounds = component.get("world_bounds_cm")
        if (
            not isinstance(bounds, dict)
            or set(bounds) != {"min", "max", "size"}
            or any(not _finite_scene_vector(bounds[key]) for key in bounds)
        ):
            return False
    return components == sorted(
        components,
        key=lambda item: (item["mesh_path"], item["name"]),
    )


def _finite_scene_vector(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(
            isinstance(item, int | float)
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    )


def _scene_geometry_payload(
    bounds: Any,
    *,
    resolution: tuple[int, int],
    horizontal_fov_deg: float,
    distance_multiplier: float,
) -> dict[str, Any]:
    base = _catalog_geometry_payload(
        bounds,
        resolution=resolution,
        horizontal_fov_deg=horizontal_fov_deg,
    )
    canonical_bounds = base["bounds_cm"]
    minimum = canonical_bounds["min"]
    maximum = canonical_bounds["max"]
    target = [(float(low) + float(high)) / 2.0 for low, high in zip(minimum, maximum, strict=True)]
    return {
        "bounds_cm": canonical_bounds,
        "camera_target_cm": target,
        "camera_radius_cm": round(float(base["camera_radius_cm"]) * distance_multiplier, 6),
        "camera_near_clip_cm": 0.1,
        "normalization": {
            "engine_unit": "centimeter",
            "uniform_scale": 1.0,
            "logical_size_cm": canonical_bounds["size"],
            "pivot_policy": "preserve_scene_world",
            "translation_cm": [0.0, 0.0, 0.0],
        },
    }


def _builtin_render_asset_payload(spec: RenderJobSpec) -> dict[str, Any]:
    if spec.asset_id != "builtin:cube":
        raise ValueError(f"Catalog render asset payload is required for {spec.asset_id!r}")
    return {
        "kind": "builtin",
        "asset_id": "builtin:cube",
        "mesh_path": "/Engine/BasicShapes/Cube",
        "preserve_materials": False,
        "bounds_cm": {
            "min": [-50.0, -50.0, -50.0],
            "max": [50.0, 50.0, 50.0],
            "size": [100.0, 100.0, 100.0],
        },
        "actor_location_cm": [0.0, 0.0, 0.0],
        "camera_target_cm": [0.0, 0.0, 0.0],
        "camera_radius_cm": 420.0,
        "floor_location_z_cm": -52.5,
        "floor_scale_xy": 5.0,
    }


def _catalog_geometry_payload(
    bounds: Any,
    *,
    resolution: tuple[int, int],
    horizontal_fov_deg: float,
    requested_normalization: dict[str, str | float] | None = None,
) -> dict[str, Any]:
    normalization_request = requested_normalization or {
        "source_units": "auto",
        "source_up_axis": "auto",
        "source_handedness": "auto",
        "uniform_scale": 1.0,
        "pivot_policy": "preserve_source",
    }
    uniform_scale = normalization_request.get("uniform_scale")
    if (
        isinstance(uniform_scale, bool)
        or not isinstance(uniform_scale, int | float)
        or not math.isfinite(float(uniform_scale))
        or float(uniform_scale) <= 0.0
    ):
        raise RuntimeError("Catalog requested uniform_scale is invalid")
    scale = float(uniform_scale)
    if not isinstance(bounds, dict) or set(bounds) != {"min", "max", "size"}:
        raise RuntimeError("Catalog StaticMesh bounds payload is invalid")
    vectors: dict[str, tuple[float, float, float]] = {}
    for key in ("min", "max", "size"):
        value = bounds[key]
        if not isinstance(value, list) or len(value) != 3:
            raise RuntimeError(f"Catalog StaticMesh bounds {key} must have three values")
        if any(isinstance(item, bool) or not isinstance(item, int | float) for item in value):
            raise RuntimeError(f"Catalog StaticMesh bounds {key} contains a non-number")
        vector = tuple(float(item) for item in value)
        if not all(math.isfinite(item) for item in vector):
            raise RuntimeError(f"Catalog StaticMesh bounds {key} contains a non-finite value")
        vectors[key] = vector  # type: ignore[assignment]
    minimum, maximum, size = vectors["min"], vectors["max"], vectors["size"]
    if any(low > high for low, high in zip(minimum, maximum, strict=True)):
        raise RuntimeError("Catalog StaticMesh bounds min exceeds max")
    for axis, (low, high, reported_size) in enumerate(zip(minimum, maximum, size, strict=True)):
        actual_size = high - low
        if not math.isclose(
            reported_size,
            actual_size,
            rel_tol=1e-6,
            abs_tol=1e-5,
        ):
            raise RuntimeError(
                f"Catalog StaticMesh bounds size[{axis}] does not match max-min: "
                f"{reported_size} != {actual_size}"
            )
    if any(item < 0.0 for item in size) or sum(item > 0.0 for item in size) < 2:
        raise RuntimeError("Catalog StaticMesh bounds size is degenerate")

    scaled_size = tuple(item * scale for item in size)
    actor_location = (
        -(minimum[0] + maximum[0]) / 2.0 * scale,
        -(minimum[1] + maximum[1]) / 2.0 * scale,
        -minimum[2] * scale,
    )
    target = (0.0, 0.0, scaled_size[2] / 2.0)
    sphere_radius = math.sqrt(sum((item / 2.0) ** 2 for item in scaled_size))
    aspect = resolution[0] / resolution[1]
    horizontal_half = math.radians(horizontal_fov_deg) / 2.0
    vertical_half = math.atan(math.tan(horizontal_half) / aspect)
    limiting_half = min(horizontal_half, vertical_half)
    camera_radius = max(10.0, sphere_radius / math.sin(limiting_half) * 1.2)
    return {
        "bounds_cm": {key: list(value) for key, value in vectors.items()},
        "actor_scale": [scale, scale, scale],
        "actor_location_cm": list(actor_location),
        "camera_target_cm": list(target),
        "camera_radius_cm": round(camera_radius, 6),
        "camera_near_clip_cm": 0.1,
        "floor_location_z_cm": -2.5,
        "floor_scale_xy": max(1.0, max(scaled_size[0], scaled_size[1]) * 3.0 / 100.0),
        "normalization": {
            "engine_unit": "centimeter",
            "uniform_scale": scale,
            "request": normalization_request,
            "logical_size_cm": list(scaled_size),
            "pivot_policy": "bounds_bottom_center_to_origin",
            "translation_cm": list(actor_location),
        },
    }


def _catalog_requested_normalization(
    manifest: dict[str, Any],
) -> dict[str, str | float] | None:
    value = manifest.get("requested_normalization")
    if not isinstance(value, dict) or set(value) != {
        "source_units",
        "source_up_axis",
        "source_handedness",
        "uniform_scale",
        "pivot_policy",
    }:
        return None
    if (
        value.get("source_units") != "auto"
        or value.get("source_up_axis") != "auto"
        or value.get("source_handedness") != "auto"
        or value.get("pivot_policy") != "preserve_source"
    ):
        return None
    scale = value.get("uniform_scale")
    if (
        isinstance(scale, bool)
        or not isinstance(scale, int | float)
        or not math.isfinite(float(scale))
        or not 0.0001 <= float(scale) <= 10_000.0
    ):
        return None
    return {
        "source_units": "auto",
        "source_up_axis": "auto",
        "source_handedness": "auto",
        "uniform_scale": float(scale),
        "pivot_policy": "preserve_source",
    }


def _valid_engine_normalization(value: Any) -> bool:
    return isinstance(value, dict) and value == {
        "target_units": "centimeters",
        "target_up_axis": "Z",
        "target_handedness": "left_handed",
        "source_conversion": "delegated_to_engine_importer",
        "package_pivot_policy": "preserve",
        "uniform_scale": 1.0,
    }


def _ue_object_file(project_root: Path, object_path: str) -> Path:
    package_path = object_path.partition(".")[0]
    if not package_path.startswith("/Game/"):
        return project_root / "__invalid_ue_object__"
    return project_root / "ue/UEFBase/Content" / f"{package_path.removeprefix('/Game/')}.uasset"


def _ue_map_file(project_root: Path, object_path: str) -> Path:
    package_path = object_path.partition(".")[0]
    if not package_path.startswith("/Game/"):
        return project_root / "__invalid_ue_map__"
    return project_root / "ue/UEFBase/Content" / f"{package_path.removeprefix('/Game/')}.umap"


def _regular_project_file(project_root: Path, path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return False
    return True


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
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


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _lighting_payload(settings: Settings, spec: RenderJobSpec) -> dict[str, Any]:
    payload = {"preset": spec.lighting.preset, "hdri": spec.lighting.hdri}
    if spec.lighting.preset == "hdri":
        if spec.lighting.hdri is None:
            raise ValueError("HDRI lighting requires hdri id")
        hdri_file = settings.data_dir / "hdri" / f"{spec.lighting.hdri}.hdr"
        if not hdri_file.exists():
            raise FileNotFoundError(
                f"HDRI file not found: {hdri_file}; run `uef acquire hdri` first"
            )
        payload["hdri_file"] = str(hdri_file)
    return payload


def _remote_lighting_payload(
    spec: RenderJobSpec,
    remote_hdri_file: PurePosixPath | None,
) -> dict[str, Any]:
    payload = {"preset": spec.lighting.preset, "hdri": spec.lighting.hdri}
    if spec.lighting.preset == "hdri":
        if remote_hdri_file is None:
            raise ValueError("HDRI lighting requires remote hdri file")
        payload["hdri_file"] = str(remote_hdri_file)
    return payload


def _sync_remote_hdri(
    *,
    settings: Settings,
    remote: RemoteHost,
    spec: RenderJobSpec,
    remote_run_dir: PurePosixPath,
) -> PurePosixPath | None:
    if spec.lighting.preset != "hdri":
        return None
    if spec.lighting.hdri is None:
        raise ValueError("HDRI lighting requires hdri id")
    local_hdri = settings.data_dir / "hdri" / f"{spec.lighting.hdri}.hdr"
    if not local_hdri.exists():
        raise FileNotFoundError(f"HDRI file not found: {local_hdri}; run `uef acquire hdri` first")
    remote_hdri_dir = remote_run_dir / "data" / "hdri"
    remote.run(f"mkdir -p {shlex.quote(str(remote_hdri_dir))}", timeout_sec=60)
    remote.rsync_push([local_hdri], f"{remote_hdri_dir}/", timeout_sec=3600)
    return remote_hdri_dir / local_hdri.name


def _package_render_project(settings: Settings, package_dir: Path) -> Path:
    source = settings.project_root / "ue/UEFBase"
    project_path = source / "UEFBase.uproject"
    config_dir = source / "Config"
    python_dir = source / "Content/Python"
    if not project_path.exists():
        raise FileNotFoundError(f"UE project not found: {project_path}")
    if not python_dir.exists():
        raise FileNotFoundError(f"UE Python directory not found: {python_dir}")
    if package_dir.exists():
        shutil.rmtree(package_dir)
    (package_dir / "Config").mkdir(parents=True)
    (package_dir / "Content/Python").mkdir(parents=True)
    shutil.copy2(project_path, package_dir / "UEFBase.uproject")
    if config_dir.exists():
        for config in config_dir.glob("*.ini"):
            shutil.copy2(config, package_dir / "Config" / config.name)
    for script in python_dir.glob("*.py"):
        shutil.copy2(script, package_dir / "Content/Python" / script.name)
    return package_dir


def _remote_runner_command(
    script_path: PurePosixPath,
    env: dict[str, str],
) -> str:
    assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    return f"{assignments} python3 {shlex.quote(str(script_path))}"


def _wait_for_remote_render_job(
    remote: RemoteHost,
    job_id: str,
    *,
    timeout_sec: int,
    poll_interval_sec: int,
) -> dict[str, Any]:
    phase: str | None = None
    phase_deadline: float | None = None
    progress_key: tuple[Any, ...] | None = None
    last_progress = time.monotonic()
    phase_grace_sec = 0 if timeout_sec <= 0 else max(30, poll_interval_sec * 2)
    inactivity_sec = max(90, poll_interval_sec * 3)
    while True:
        status = remote.tmux_status(job_id, timeout_sec=60)
        now = time.monotonic()
        tmux_live = bool(status.get("tmux_live"))
        state = status.get("status")
        if not tmux_live and state in {"complete", "failed"}:
            return status
        if not tmux_live:
            raise RuntimeError(
                f"Remote render job {job_id} tmux exited before a terminal status; "
                f"last status={status}"
            )

        current_phase = str(status.get("phase") or state or "unknown")
        if current_phase != phase:
            phase = current_phase
            phase_deadline = now + timeout_sec + phase_grace_sec
            last_progress = now
            progress_key = None
        current_progress = (
            current_phase,
            status.get("updated_utc"),
            status.get("elapsed_sec"),
        )
        if current_progress != progress_key:
            progress_key = current_progress
            last_progress = now

        if phase_deadline is not None and now >= phase_deadline:
            raise TimeoutError(
                f"Remote render job {job_id} phase {current_phase!r} timed out; "
                f"last status={status}"
            )
        if now - last_progress >= inactivity_sec:
            raise TimeoutError(
                f"Remote render job {job_id} stopped heartbeating during "
                f"phase {current_phase!r}; last status={status}"
            )
        time.sleep(poll_interval_sec)


def _summary_payload(result: Any) -> dict[str, Any]:
    return {
        "warning_count": result.summary.warning_count,
        "warning_noise_count": result.summary.warning_noise_count,
        "warning_noise": result.summary.warning_noise or {},
        "error_count": result.summary.error_count,
        "error_noise_count": result.summary.error_noise_count,
        "error_noise": result.summary.error_noise or {},
        "warnings": result.summary.warnings,
        "errors": result.summary.errors,
    }


def _raise_on_ue_summary(
    manifest_path: Path,
    ue_job: dict[str, Any],
    commands: dict[str, list[str]],
    runtime: dict[str, object],
    phase: str,
    error_count: int,
    warning_count: int,
    log_path: Path,
) -> None:
    if error_count:
        error = f"Render job {phase} UE log contains {error_count} error lines"
        _write_failure_manifest(manifest_path, ue_job, commands, error, runtime)
        raise RuntimeError(f"{error}; UE log: {log_path}")
    if warning_count:
        error = f"Render job {phase} UE log contains {warning_count} warning lines"
        _write_failure_manifest(manifest_path, ue_job, commands, error, runtime)
        raise RuntimeError(f"{error}; UE log: {log_path}")


def _write_failure_manifest(
    manifest_path: Path,
    ue_job: dict[str, Any],
    commands: dict[str, list[str]],
    error: str,
    runtime: dict[str, object],
) -> None:
    payload: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload.update(existing)
            else:
                payload["manifest_read_error"] = {
                    "type": "ValueError",
                    "message": "Existing manifest is not a JSON object",
                }
        except (OSError, json.JSONDecodeError) as exc:
            payload["manifest_read_error"] = _error_payload(exc)
    payload.update(
        {
            "schema_version": 3,
            "status": "failed",
            "render_kind": "job",
            "commands": commands,
            "runtime": runtime,
            "host_error": error,
        }
    )
    payload.setdefault("job", ue_job.get("job", {}))
    payload.setdefault("asset", ue_job.get("asset", {}))
    payload.setdefault("engine", ue_job.get("engine"))
    payload.setdefault("error", error)
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


_REMOTE_RENDER_JOB_SCRIPT = r"""
import json
import os
import signal
import subprocess
import time
import traceback
from pathlib import Path

host_name = os.environ["UEF_HOST_NAME"]
job_id = os.environ["UEF_JOB_ID"]
work_dir = Path(os.environ["UEF_WORK_DIR"])
engine_dir = Path(os.environ["UEF_ENGINE_DIR"])
run_dir = Path(os.environ["UEF_REMOTE_RUN_DIR"])
project_dir = Path(os.environ["UEF_REMOTE_PROJECT_DIR"])
run_user = os.environ.get("UEF_REMOTE_RUN_USER", "")
timeout_sec = int(os.environ["UEF_TIMEOUT_SEC"])
status_path = work_dir / "jobs" / job_id / "status.json"
status_path.parent.mkdir(parents=True, exist_ok=True)
terminate_grace_sec = float(os.environ.get("UEF_TERMINATE_GRACE_SEC", "10"))

job_path = run_dir / "job.json"
project_path = project_dir / "UEFBase.uproject"
setup_script_path = project_dir / "Content/Python/uef_render_job.py"
runtime_script_path = project_dir / "Content/Python/uef_render_job_runtime.py"
ddc_dir = work_dir / "ddc" / "render_job"
ue_home = work_dir / "ue_home" / "render_job"
active_process = None
handled_signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)

WARNING_NOISE_RULES = {
    "directory_watcher": ("LogDirectoryWatcher: Warning:",),
    "unreal_trace_server_startup": (
        "LogCore: Warning: UTS: Unreal Trace Server process returned an error",
    ),
    "missing_editor_icon": ("LogStreaming: Warning: Failed to read file", ".png"),
    "usd_plugin_metadata_write_permission": ("Warning:", "USD", "plugInfo.json"),
    "engine_content_write_permission_probe": (
        "Warning:",
        "/Engine/",
        "WritePermissions.",
        "Permission denied",
    ),
    "python_types_runtime_class_probe": (
        "LogStreaming: Warning: LoadPackage: SkipPackage: /Engine/PythonTypes",
    ),
    "mrq_output_path_probe": (
        "LogCore: Warning: Unable to statfs(",
        "out/mrq_spike/",
        "errno=2 (No such file or directory)",
    ),
    "mrq_render_output_path_probe": (
        "LogCore: Warning: Unable to statfs(",
        "out/renders/",
        "errno=2 (No such file or directory)",
    ),
    "mrq_remote_output_path_probe": (
        "LogCore: Warning: Unable to statfs(",
        "/_mrq/",
        "errno=2 (No such file or directory)",
    ),
}

ERROR_NOISE_RULES = {
    "missing_optional_usd_plugin": (
        "LogUsd: Error: TF_DIAGNOSTIC_CODING_ERROR_TYPE: Failed to load plugin",
    ),
    "missing_feature_pack_screenshot": (
        "LogFeaturePack: Error: Error in Feature pack",
        "Cannot find screenshot",
    ),
}


def write_status(status, phase, **extra):
    payload = {
        "job_id": job_id,
        "host": host_name,
        "status": status,
        "phase": phase,
        "run_dir": str(run_dir),
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload.update(extra)
    tmp = status_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(status_path)


def noise_reason(line, rules):
    for reason, markers in rules.items():
        if all(marker in line for marker in markers):
            return reason
    return None


def summarize_log(path):
    warnings = []
    errors = []
    warning_count = 0
    error_count = 0
    warning_noise = {}
    error_noise = {}
    if not path.exists():
        return {
            "warnings": [],
            "errors": [],
            "warning_count": 0,
            "error_count": 0,
            "warning_noise_count": 0,
            "warning_noise": {},
            "error_noise_count": 0,
            "error_noise": {},
        }
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "Warning:" in line:
            reason = noise_reason(line, WARNING_NOISE_RULES)
            if reason is not None:
                warning_noise[reason] = warning_noise.get(reason, 0) + 1
                continue
            warning_count += 1
            if len(warnings) < 20:
                warnings.append(line)
        if "Error:" in line:
            reason = noise_reason(line, ERROR_NOISE_RULES)
            if reason is not None:
                error_noise[reason] = error_noise.get(reason, 0) + 1
                continue
            error_count += 1
            if len(errors) < 20:
                errors.append(line)
    return {
        "warnings": warnings,
        "errors": errors,
        "warning_count": warning_count,
        "error_count": error_count,
        "warning_noise_count": sum(warning_noise.values()),
        "warning_noise": warning_noise,
        "error_noise_count": sum(error_noise.values()),
        "error_noise": error_noise,
    }


def chown_for_run_user(*paths):
    if not run_user or os.geteuid() != 0:
        return
    subprocess.run(["id", "-u", run_user], check=True, stdout=subprocess.DEVNULL)
    for path in paths:
        if path.exists():
            subprocess.run(["chown", "-R", f"{run_user}:{run_user}", str(path)], check=True)


def command_for_run_user(command, env):
    if not run_user or os.geteuid() != 0:
        return command, env
    env_keys = ["HOME", "UEF_JOB_FILE", "UE-LocalDataCachePath", "LD_LIBRARY_PATH"]
    env_args = [f"{key}={env[key]}" for key in env_keys if key in env]
    return ["runuser", "-u", run_user, "--", "env", *env_args, *command], os.environ.copy()


def process_start_ticks(pid):
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    fields = stat[stat.rfind(")") + 2 :].split()
    return int(fields[19])


def process_group_is_live(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_process_group_exit(process, timeout):
    deadline = time.monotonic() + timeout
    if process.poll() is None:
        try:
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    while process_group_is_live(process.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not process_group_is_live(process.pid)


def terminate_process_group(process):
    if process is None or not process_group_is_live(process.pid):
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if wait_for_process_group_exit(process, terminate_grace_sec):
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    if not wait_for_process_group_exit(process, terminate_grace_sec):
        raise RuntimeError(f"UE process group {process.pid} did not exit")


class RunnerInterrupted(RuntimeError):
    pass


def handle_runner_signal(signum, _frame):
    terminate_process_group(active_process)
    signal_name = signal.Signals(signum).name
    raise RunnerInterrupted(f"remote render runner received {signal_name}")


for handled_signal in handled_signals:
    signal.signal(handled_signal, handle_runner_signal)


def run_ue_phase(phase, command, log_path, env):
    global active_process
    run_command, run_env = command_for_run_user(command, env)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        process = None
        try:
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)
            try:
                process = subprocess.Popen(
                    run_command,
                    cwd=project_dir,
                    env=run_env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                active_process = process
                pgid = os.getpgid(process.pid)
                if pgid != process.pid:
                    raise RuntimeError(
                        f"UE phase did not start in an independent process group: {pgid}"
                    )
                start_ticks = process_start_ticks(process.pid)
                write_status(
                    "running",
                    phase,
                    command=run_command,
                    ue_command=command,
                    run_user=run_user,
                    log=str(log_path),
                    elapsed_sec=0.0,
                    pid=process.pid,
                    pgid=pgid,
                    process_start_ticks=start_ticks,
                )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            deadline = started + timeout_sec
            next_heartbeat = started + 30
            while process.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    terminate_process_group(process)
                    raise subprocess.TimeoutExpired(run_command, timeout_sec)
                if now >= next_heartbeat:
                    write_status(
                        "running",
                        phase,
                        command=run_command,
                        ue_command=command,
                        run_user=run_user,
                        log=str(log_path),
                        elapsed_sec=round(now - started, 3),
                        pid=process.pid,
                        pgid=pgid,
                        process_start_ticks=start_ticks,
                    )
                    next_heartbeat = now + 30
                time.sleep(min(5, max(0.1, deadline - now)))
            returncode = process.returncode
        except BaseException:
            terminate_process_group(process)
            raise
        finally:
            active_process = None
    duration_sec = round(time.monotonic() - started, 3)
    return {
        "command": run_command,
        "ue_command": command,
        "returncode": returncode,
        "duration_sec": duration_sec,
        "summary": summarize_log(log_path),
        "log": str(log_path),
    }


def phase_failed(phase_result):
    summary = phase_result["summary"]
    return (
        phase_result["returncode"] != 0
        or summary["error_count"] > 0
        or summary["warning_count"] > 0
    )


def write_failure_manifest(manifest_path, job, commands, error, *, setup=None, render=None):
    payload = {
        "schema_version": 2,
        "status": "failed",
        "render_kind": "job",
        "job": job.get("job", {}),
        "commands": commands,
        "remote_host": host_name,
        "remote_job_id": job_id,
        "run_user": run_user,
        "error": error,
    }
    if setup is not None:
        payload["setup_summary"] = setup["summary"]
        payload["setup_log"] = setup["log"]
    if render is not None:
        payload["ue_summary"] = render["summary"]
        payload["ue_log"] = render["log"]
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


try:
    sentinel = work_dir / ".uef_node"
    payload = json.loads(sentinel.read_text(encoding="utf-8"))
    if payload.get("host") != host_name:
        raise RuntimeError(f"sentinel host mismatch: {payload.get('host')} != {host_name}")

    executable = engine_dir / "Engine/Binaries/Linux/UnrealEditor-Cmd"
    version_path = engine_dir / "Engine/Build/Build.version"
    for label, path in {
        "UnrealEditor-Cmd": executable,
        "Build.version": version_path,
        "UE project": project_path,
        "render setup script": setup_script_path,
        "render runtime script": runtime_script_path,
        "job file": job_path,
    }.items():
        if not path.exists():
            raise RuntimeError(f"{label} is missing: {path}")

    job = json.loads(job_path.read_text(encoding="utf-8"))
    output_dir = Path(job["out_dir"])
    manifest_path = output_dir / "manifest.json"
    setup_log_path = output_dir / "ue_setup.log"
    ue_log_path = output_dir / "ue.log"
    run_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    ddc_dir.mkdir(parents=True, exist_ok=True)
    ue_home.mkdir(parents=True, exist_ok=True)
    chown_for_run_user(run_dir, project_dir, output_dir, ddc_dir, ue_home)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(ue_home),
            "UEF_JOB_FILE": str(job_path),
            "UE-LocalDataCachePath": str(ddc_dir),
        }
    )
    width, height = job["camera"]["resolution"]
    setup_command = [
        str(executable),
        str(project_path),
        f"-ExecutePythonScript={setup_script_path}",
        "-unattended",
        "-nopause",
        "-nosplash",
        "-NullRHI",
        "-stdout",
        "-FullStdOutLogOutput",
        "-NoSound",
        "-ddc=InstalledNoZenLocalFallback",
        f"-LocalDataCachePath={ddc_dir}",
    ]
    setup = run_ue_phase("setup", setup_command, setup_log_path, env)
    if phase_failed(setup):
        write_failure_manifest(
            manifest_path,
            job,
            {"setup": setup["command"]},
            "setup phase failed",
            setup=setup,
        )
        write_status("failed", "setup_failed", manifest=str(manifest_path))
        raise RuntimeError("setup phase failed")

    render_command = [
        str(executable),
        str(project_path),
        str(job["map_path"]),
        "-game",
        "-RenderOffScreen",
        "-unattended",
        "-nopause",
        "-nosplash",
        "-stdout",
        "-FullStdOutLogOutput",
        "-NoSound",
        "-NoLoadingScreen",
        "-windowed",
        f"-resx={width}",
        f"-resy={height}",
        "-ddc=InstalledNoZenLocalFallback",
        f"-LocalDataCachePath={ddc_dir}",
        "-MoviePipelineLocalExecutorClass=/Script/MovieRenderPipelineCore.MoviePipelinePythonHostExecutor",
        "-ExecutorPythonClass=/Engine/PythonTypes.UEFRenderJobRuntimeExecutor",
        f"-LevelSequence={job['sequence_path']}",
    ]
    render = run_ue_phase("render", render_command, ue_log_path, env)

    frames_found = {}
    for pass_name in job["passes"]:
        frames_found[pass_name] = len(sorted((output_dir / pass_name).glob("frame_*.*")))
    missing = {
        pass_name: count for pass_name, count in frames_found.items() if count != int(job["frames"])
    }
    runtime_manifest_exists = manifest_path.exists()
    if runtime_manifest_exists:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {
            "schema_version": 2,
            "render_kind": "job",
            "job": job.get("job", {}),
        }
    runtime_status = manifest.get("status")
    success = (
        render["returncode"] == 0
        and render["summary"]["error_count"] == 0
        and render["summary"]["warning_count"] == 0
        and not missing
        and runtime_manifest_exists
        and runtime_status == "ok"
    )
    manifest.update(
        {
            "status": "ok" if success else "failed",
            "remote_host": host_name,
            "remote_job_id": job_id,
            "run_user": run_user,
            "commands": {
                "setup": setup["command"],
                "render": render["command"],
            },
            "ue_commands": {
                "setup": setup["ue_command"],
                "render": render["ue_command"],
            },
            "setup_log": str(setup_log_path),
            "ue_log": str(ue_log_path),
            "setup_summary": setup["summary"],
            "ue_summary": render["summary"],
            "returncode": {
                "setup": setup["returncode"],
                "render": render["returncode"],
            },
            "duration_sec": setup["duration_sec"] + render["duration_sec"],
            "engine": json.loads(version_path.read_text(encoding="utf-8")),
            "ddc_dir": str(ddc_dir),
            "ue_home": str(ue_home),
            "remote_run_dir": str(run_dir),
            "frames_found": frames_found,
        }
    )
    remote_runner_error = None
    if missing:
        remote_runner_error = f"Missing frames: {missing}"
    elif phase_failed(render):
        remote_runner_error = "render phase failed"
    elif not runtime_manifest_exists:
        remote_runner_error = f"Render manifest missing: {manifest_path}"
    elif runtime_status != "ok":
        remote_runner_error = f"UE runtime manifest status is {runtime_status!r}"
    if remote_runner_error is not None:
        manifest["remote_runner_error"] = remote_runner_error
        manifest.setdefault("error", remote_runner_error)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    if success:
        write_status(
            "complete",
            "rendered",
            duration_sec=manifest["duration_sec"],
            manifest=str(manifest_path),
            output_dir=str(output_dir),
        )
    else:
        write_status("failed", "render_failed", manifest=str(manifest_path))
        raise RuntimeError(manifest.get("error", "render phase failed"))
except subprocess.TimeoutExpired as exc:
    write_status("failed", "timeout", error=str(exc), traceback=traceback.format_exc())
    raise
except Exception as exc:
    try:
        if "manifest_path" in locals() and "job" in locals() and not manifest_path.exists():
            write_failure_manifest(manifest_path, job, {}, str(exc))
    finally:
        write_status("failed", "error", error=str(exc), traceback=traceback.format_exc())
    raise
"""
