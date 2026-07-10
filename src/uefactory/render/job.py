from __future__ import annotations

import json
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from uefactory.core.config import Settings
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
) -> RenderJobResult:
    spec = load_jobspec(job_path)
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

    ue_job = _ue_job_payload(settings, spec, run_id, run_dir, sequence_path)
    ue_job_path.write_text(json.dumps(ue_job, indent=2, sort_keys=True), encoding="utf-8")

    ddc_dir = settings.ddc_dir or settings.data_dir / "ddc"
    ddc_dir.mkdir(parents=True, exist_ok=True)
    ue_home = settings.ue_home
    ue_home.mkdir(parents=True, exist_ok=True)
    runtime = _runtime_settings(settings.runtime_lib_dir)
    env = {
        "HOME": str(ue_home),
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
        result = _validate_render_output(
            run_dir=run_dir,
            manifest_path=manifest_path,
            setup_log_path=setup_log_path,
            ue_log_path=ue_log_path,
            spec=spec,
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


def render_job_remote(
    *,
    settings: Settings,
    host: str,
    job_path: Path,
    timeout_sec: int = 1800,
    poll_interval_sec: int = 30,
) -> RenderJobResult:
    spec = load_jobspec(job_path)
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
) -> RenderJobResult:
    frame_paths = {
        pass_name: sorted((run_dir / pass_name).glob("frame_*.*")) for pass_name in spec.passes
    }
    for paths in frame_paths.values():
        png_paths = [path for path in paths if path.suffix.lower() == ".png"]
        if png_paths:
            canonicalize_png_frames(png_paths)
    pass_validations = {
        pass_name: validate_render_pass(
            pass_name,
            paths,
            expected_frames=spec.frame_count,
            expected_resolution=spec.camera.resolution,
            lighting_preset=spec.lighting.preset,
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
        assert_object_mask_visibility(pass_frames=frame_paths)
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
) -> dict[str, Any]:
    payload = _ue_job_payload_with_lighting(
        spec=spec,
        run_id=run_id,
        run_dir=run_dir,
        sequence_path=sequence_path,
        lighting=_lighting_payload(settings, spec),
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
) -> dict[str, Any]:
    width, height = spec.camera.resolution
    beauty_sequence_name = f"UEF_RenderJobBeauty_{run_id}"
    beauty_sequence_path = (
        f"/Game/UEF/RenderJobs/{run_id}/{beauty_sequence_name}.{beauty_sequence_name}"
        if lighting["preset"] == "hdri"
        else sequence_path
    )
    return {
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
        "schema_version": 2,
        "map_path": f"/Game/UEF/RenderJobs/{run_id}/UEF_RenderWorld_{run_id}",
        "sequence_path": sequence_path,
        "beauty_sequence_path": beauty_sequence_path,
    }


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
            "schema_version": 2,
            "status": "failed",
            "render_kind": "job",
            "commands": commands,
            "runtime": runtime,
            "host_error": error,
        }
    )
    payload.setdefault("job", ue_job.get("job", {}))
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
