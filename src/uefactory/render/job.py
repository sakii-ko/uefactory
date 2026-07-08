from __future__ import annotations

import json
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from uefactory.core.config import Settings
from uefactory.core.paths import resolve_path, utc_timestamp
from uefactory.core.remote import RemoteHost, remote_python_command
from uefactory.render.artifacts import RenderArtifacts, create_render_artifacts
from uefactory.render.jobspec import RenderJobSpec, load_jobspec
from uefactory.render.passes import (
    PassValidation,
    assert_passes_distinct,
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
    run_id = utc_timestamp()
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
    except UERunnerError as exc:
        _write_failure_manifest(
            manifest_path, ue_job, {"setup": exc.result.command}, str(exc), runtime
        )
        raise
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

    width, height = spec.camera.resolution
    render_command: list[str | Path] = [
        settings.ue_root / "Engine/Binaries/Linux/UnrealEditor-Cmd",
        project_path,
        "/Engine/Maps/Entry",
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
    try:
        ue_result = run_ue(
            render_command,
            cwd=settings.project_root,
            log_path=ue_log_path,
            timeout_sec=timeout_sec,
            env=env,
        )
    except UERunnerError as exc:
        _write_failure_manifest(
            manifest_path,
            ue_job,
            {"setup": setup_result.command, "render": exc.result.command},
            str(exc),
            runtime,
        )
        raise
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

    if not manifest_path.exists():
        _write_failure_manifest(
            manifest_path,
            ue_job,
            {"setup": setup_result.command, "render": ue_result.command},
            f"Render job manifest was not created: {manifest_path}",
            runtime,
        )
        raise RuntimeError(f"Render job did not produce manifest; UE log: {ue_log_path}")

    frame_paths = {
        pass_name: sorted((run_dir / pass_name).glob("frame_*.*")) for pass_name in spec.passes
    }
    pass_validations = {
        pass_name: validate_render_pass(
            pass_name,
            paths,
            expected_frames=spec.frame_count,
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

    result = RenderJobResult(
        run_dir=run_dir,
        manifest_path=manifest_path,
        ue_log_path=ue_log_path,
        setup_log_path=setup_log_path,
        frame_paths=frame_paths,
        pass_validations=pass_validations,
        spec=spec,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "status": "ok",
            "commands": {
                "setup": setup_result.command,
                "render": ue_result.command,
            },
            "runtime": runtime,
            "setup_summary": _summary_payload(setup_result),
            "ue_summary": _summary_payload(ue_result),
            "frame_luma": result.frame_luma,
            "passes": stable_validation_payload(pass_validations),
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    artifacts = create_render_artifacts(
        run_dir=run_dir,
        frame_paths=frame_paths,
        manifest_path=manifest_path,
    )
    manifest["artifacts"] = artifacts.manifest_payload(run_dir=run_dir)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return RenderJobResult(
        run_dir=result.run_dir,
        manifest_path=result.manifest_path,
        ue_log_path=result.ue_log_path,
        setup_log_path=result.setup_log_path,
        frame_paths=result.frame_paths,
        pass_validations=result.pass_validations,
        spec=result.spec,
        artifacts=artifacts,
    )


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
    run_id = utc_timestamp()
    out_root = resolve_path(spec.output.dir, settings.project_root)
    run_dir = out_root / run_id / spec.asset_id.replace(":", "_")
    run_dir.mkdir(parents=True, exist_ok=False)

    job_id = f"render_{remote.config.name}_{run_id}"
    remote_run_dir = PurePosixPath(str(remote.config.work_dir)) / "jobs" / job_id
    remote_project_dir = remote_run_dir / "project"
    remote_output_dir = remote_run_dir / "out" / spec.asset_id.replace(":", "_")
    sequence_path = f"/Game/UEF/RenderJobs/{run_id}/UEF_RenderJob_{run_id}.UEF_RenderJob_{run_id}"

    project_package = _package_render_project(settings, run_dir / "project_package")
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

    command = remote_python_command(
        _REMOTE_RENDER_JOB_SCRIPT,
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
    remote.tmux_start(job_id, command, timeout_sec=60)
    status = _wait_for_remote_render_job(
        remote,
        job_id,
        timeout_sec=timeout_sec,
        poll_interval_sec=poll_interval_sec,
    )
    manifest_path = run_dir / "manifest.json"

    result: RenderJobResult | None = None
    validation_error: Exception | None = None
    cleanup_error: Exception | None = None
    try:
        remote.rsync_pull([f"{remote_output_dir}/"], run_dir, timeout_sec=3600, delete=True)
        if status.get("status") != "complete":
            raise RuntimeError(f"Remote render failed on {host}: {status}")
        if not manifest_path.exists():
            raise RuntimeError(f"Remote render did not return manifest: {manifest_path}")

        frame_paths = {
            pass_name: sorted((run_dir / pass_name).glob("frame_*.*")) for pass_name in spec.passes
        }
        pass_validations = {
            pass_name: validate_render_pass(
                pass_name,
                paths,
                expected_frames=spec.frame_count,
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
        artifacts = create_render_artifacts(
            run_dir=run_dir,
            frame_paths=frame_paths,
            manifest_path=manifest_path,
        )
        result = RenderJobResult(
            run_dir=run_dir,
            manifest_path=manifest_path,
            ue_log_path=run_dir / "ue.log",
            setup_log_path=run_dir / "ue_setup.log",
            frame_paths=frame_paths,
            pass_validations=pass_validations,
            spec=spec,
            artifacts=artifacts,
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "status": "ok",
                "remote_host": remote.config.name,
                "remote_job_id": job_id,
                "frame_luma": result.frame_luma,
                "passes": stable_validation_payload(pass_validations),
                "artifacts": artifacts.manifest_payload(run_dir=run_dir),
                "local_validation": {"status": "ok", "validated_utc": utc_timestamp()},
            }
        )
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        validation_error = exc
    finally:
        cleanup, cleanup_error = _cleanup_remote_paths(remote, [remote_run_dir], timeout_sec=60)
        try:
            _record_remote_cleanup(manifest_path, cleanup)
        except Exception as exc:
            cleanup_error = cleanup_error or exc
    if validation_error is not None:
        if cleanup_error is not None:
            validation_error.add_note(f"Remote cleanup also failed: {cleanup_error}")
        raise validation_error
    if cleanup_error is not None:
        raise RuntimeError(
            f"Remote render cleanup failed or could not be recorded for {remote_run_dir}"
        ) from cleanup_error
    if result is None:
        raise RuntimeError(f"Remote render produced no result for {job_id}")
    return result


def compare_job_luma(first: RenderJobResult, second: RenderJobResult) -> dict[str, Any]:
    if set(first.pass_validations) != set(second.pass_validations):
        raise ValueError(
            f"Pass mismatch: {sorted(first.pass_validations)} != {sorted(second.pass_validations)}"
        )
    first_payload = stable_validation_payload(first.pass_validations)
    second_payload = stable_validation_payload(second.pass_validations)
    if first_payload != second_payload:
        raise RuntimeError("Render job pass validation payload mismatch")
    if len(first.frame_luma) != len(second.frame_luma):
        raise ValueError(
            f"Frame count mismatch: {len(first.frame_luma)} != {len(second.frame_luma)}"
        )
    return first_payload


def _ue_job_payload(
    settings: Settings,
    spec: RenderJobSpec,
    run_id: str,
    run_dir: Path,
    sequence_path: str,
) -> dict[str, Any]:
    width, height = spec.camera.resolution
    return _ue_job_payload_with_lighting(
        spec=spec,
        run_id=run_id,
        run_dir=run_dir,
        sequence_path=sequence_path,
        lighting=_lighting_payload(settings, spec),
    )


def _ue_job_payload_with_lighting(
    *,
    spec: RenderJobSpec,
    run_id: str,
    run_dir: Path,
    sequence_path: str,
    lighting: dict[str, Any],
) -> dict[str, Any]:
    width, height = spec.camera.resolution
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
        "sequence_path": sequence_path,
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


def _wait_for_remote_render_job(
    remote: RemoteHost,
    job_id: str,
    *,
    timeout_sec: int,
    poll_interval_sec: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    while True:
        status = remote.tmux_status(job_id, timeout_sec=60)
        if not status.get("tmux_live") and status.get("status") in {"complete", "failed"}:
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Remote render job {job_id} timed out; last status={status}")
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
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "status": "failed",
                "render_kind": "job",
                "job": ue_job.get("job", {}),
                "commands": commands,
                "runtime": runtime,
                "error": error,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


_REMOTE_RENDER_JOB_SCRIPT = r"""
import json
import os
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

job_path = run_dir / "job.json"
project_path = project_dir / "UEFBase.uproject"
setup_script_path = project_dir / "Content/Python/uef_render_job.py"
runtime_script_path = project_dir / "Content/Python/uef_render_job_runtime.py"
ddc_dir = work_dir / "ddc" / "render_job"
ue_home = work_dir / "ue_home" / "render_job"

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


def run_ue_phase(phase, command, log_path, env):
    run_command, run_env = command_for_run_user(command, env)
    write_status(
        "running",
        phase,
        command=run_command,
        ue_command=command,
        run_user=run_user,
        log=str(log_path),
    )
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        result = subprocess.run(
            run_command,
            cwd=project_dir,
            env=run_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    duration_sec = round(time.monotonic() - started, 3)
    return {
        "command": run_command,
        "ue_command": command,
        "returncode": result.returncode,
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
        "/Engine/Maps/Entry",
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
    success = (
        render["returncode"] == 0
        and render["summary"]["error_count"] == 0
        and render["summary"]["warning_count"] == 0
        and not missing
        and manifest_path.exists()
    )
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {
            "schema_version": 2,
            "render_kind": "job",
            "job": job.get("job", {}),
        }
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
    if missing:
        manifest["error"] = f"Missing frames: {missing}"
    elif phase_failed(render):
        manifest["error"] = "render phase failed"
    elif not manifest_path.exists():
        manifest["error"] = f"Render manifest missing: {manifest_path}"
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
