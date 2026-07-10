from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, ImageStat

from uefactory.core.config import Settings
from uefactory.core.paths import resolve_path, utc_timestamp
from uefactory.core.remote import RemoteHost, parse_json_stdout, remote_python_command
from uefactory.render.ue_runner import UERunnerError, run_ue

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmokeRenderResult:
    run_dir: Path
    frame_path: Path
    manifest_path: Path
    ue_log_path: Path
    mean_luma: float


@dataclass(frozen=True)
class ImageInfo:
    width: int
    height: int
    mean_luma: float
    luma_stddev: float
    luma_min: int
    luma_max: int


def render_smoke(settings: Settings, out_root: Path, timeout_sec: int = 1800) -> SmokeRenderResult:
    run_dir = resolve_path(out_root, settings.project_root) / utc_timestamp()
    run_dir.mkdir(parents=True, exist_ok=False)
    raw_frame = run_dir / "smoke_frame.png"
    frame_path = run_dir / "frame_0000.png"
    ue_log_path = run_dir / "ue.log"
    manifest_path = run_dir / "manifest.json"
    job_path = run_dir / "job.json"

    project_path = settings.project_root / "ue/UEFBase/UEFBase.uproject"
    script_path = settings.project_root / "ue/UEFBase/Content/Python/uef_smoke.py"
    if not project_path.exists():
        msg = f"UE project not found: {project_path}"
        raise FileNotFoundError(msg)
    if not script_path.exists():
        msg = f"UE smoke script not found: {script_path}"
        raise FileNotFoundError(msg)

    job = {
        "out_dir": str(run_dir),
        "filename": raw_frame.name,
        "render_kind": "scene",
        "width": 1280,
        "height": 720,
    }
    job_path.write_text(json.dumps(job, indent=2, sort_keys=True), encoding="utf-8")

    ddc_dir = settings.ddc_dir or settings.data_dir / "ddc"
    ddc_dir.mkdir(parents=True, exist_ok=True)
    ue_home = settings.ue_home
    ue_home.mkdir(parents=True, exist_ok=True)
    runtime_lib_dir = settings.runtime_lib_dir
    runtime = _runtime_settings(runtime_lib_dir)

    command: list[str | Path] = [
        settings.ue_root / "Engine/Binaries/Linux/UnrealEditor-Cmd",
        project_path,
        f"-ExecutePythonScript={script_path}",
        "-unattended",
        "-nopause",
        "-nosplash",
        "-RenderOffScreen",
        "-stdout",
        "-FullStdOutLogOutput",
        "-NoSound",
        "-ddc=InstalledNoZenLocalFallback",
        f"-LocalDataCachePath={ddc_dir}",
    ]
    env = {
        "HOME": str(ue_home),
        "UEF_JOB_FILE": str(job_path),
        "UE-LocalDataCachePath": str(ddc_dir),
    }
    if runtime["enabled"]:
        env["LD_LIBRARY_PATH"] = _prepend_env_path(
            Path(str(runtime["lib_dir"])),
            "LD_LIBRARY_PATH",
        )

    try:
        ue_result = run_ue(
            command,
            cwd=settings.project_root,
            log_path=ue_log_path,
            timeout_sec=timeout_sec,
            env=env,
        )
    except UERunnerError as exc:
        _finalize_manifest(
            manifest_path,
            job,
            exc.result.command,
            exc.result.duration_sec,
            exc.result,
            status="failed",
            runtime=runtime,
        )
        raise

    if ue_result.summary.error_count:
        _finalize_manifest(
            manifest_path,
            job,
            ue_result.command,
            ue_result.duration_sec,
            ue_result,
            status="failed",
            runtime=runtime,
            error=f"UE log contains {ue_result.summary.error_count} error lines",
        )
        msg = f"Smoke render UE log contains errors; UE log: {ue_log_path}"
        raise RuntimeError(msg)

    if not raw_frame.exists():
        _finalize_manifest(
            manifest_path,
            job,
            ue_result.command,
            ue_result.duration_sec,
            ue_result,
            status="failed",
            runtime=runtime,
            error=f"Expected frame was not created: {raw_frame}",
        )
        msg = f"Smoke render did not produce {raw_frame}; UE log: {ue_log_path}"
        raise RuntimeError(msg)
    shutil.move(raw_frame, frame_path)

    image_info = _validate_image(frame_path)
    _finalize_manifest(
        manifest_path,
        job,
        ue_result.command,
        ue_result.duration_sec,
        ue_result,
        status="ok",
        runtime=runtime,
        image_info=image_info,
        settings=settings,
        ddc_dir=ddc_dir,
        ue_home=ue_home,
    )
    LOGGER.info(
        "Smoke render produced %s (%sx%s mean_luma=%.3f)",
        frame_path,
        image_info.width,
        image_info.height,
        image_info.mean_luma,
    )
    return SmokeRenderResult(
        run_dir=run_dir,
        frame_path=frame_path,
        manifest_path=manifest_path,
        ue_log_path=ue_log_path,
        mean_luma=image_info.mean_luma,
    )


def render_smoke_remote(
    *,
    settings: Settings,
    host: str,
    out_root: Path,
    timeout_sec: int = 1800,
    poll_interval_sec: int = 30,
) -> SmokeRenderResult:
    remote = RemoteHost.from_settings(settings, host)
    requested_run_user = "uef"
    runtime = _prepare_remote_smoke_runtime(remote, run_user=requested_run_user)
    run_user = str(runtime.get("run_user") or "")
    timestamp = utc_timestamp()
    job_id = f"smoke_{remote.config.name}_{timestamp}"
    run_dir = resolve_path(out_root, settings.project_root) / timestamp
    run_dir.mkdir(parents=True, exist_ok=False)

    project_package = _package_smoke_project(settings, run_dir / "project_package")
    remote_run_dir = PurePosixPath(str(remote.config.work_dir)) / "jobs" / job_id
    remote_project_dir = remote_run_dir / "project"

    remote.run(
        "\n".join(
            [
                "set -euo pipefail",
                f"mkdir -p {shlex.quote(str(remote_run_dir))}",
                f"mkdir -p {shlex.quote(str(remote_project_dir))}",
            ]
        ),
        timeout_sec=60,
    )
    remote.rsync_push([f"{project_package}/"], f"{remote_project_dir}/", timeout_sec=3600)
    command = remote_python_command(
        _REMOTE_SMOKE_SCRIPT,
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
    status = _wait_for_remote_job(
        remote,
        job_id,
        timeout_sec=timeout_sec,
        poll_interval_sec=poll_interval_sec,
    )

    remote.rsync_pull([f"{remote_run_dir}/"], run_dir, timeout_sec=3600, delete=True)
    frame_path = run_dir / "frame_0000.png"
    manifest_path = run_dir / "manifest.json"
    ue_log_path = run_dir / "ue.log"
    cleanup_error: Exception | None = None
    try:
        if status.get("status") != "complete":
            msg = f"Remote smoke failed on {host}: {status}"
            raise RuntimeError(msg)
        if not manifest_path.exists():
            msg = f"Remote smoke did not return manifest: {manifest_path}"
            raise RuntimeError(msg)
        image_info = _validate_image(frame_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["image"] = {
            "path": str(frame_path),
            "width": image_info.width,
            "height": image_info.height,
            "mean_luma": image_info.mean_luma,
            "luma_stddev": image_info.luma_stddev,
            "luma_min": image_info.luma_min,
            "luma_max": image_info.luma_max,
        }
        manifest["local_validation"] = {"status": "ok", "validated_utc": utc_timestamp()}
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    finally:
        cleanup, cleanup_error = _cleanup_remote_paths(remote, [remote_run_dir], timeout_sec=60)
        try:
            _record_remote_cleanup(manifest_path, cleanup)
        except Exception as exc:
            cleanup_error = cleanup_error or exc

    if cleanup_error is not None:
        msg = f"Remote smoke cleanup failed or could not be recorded for {remote_run_dir}"
        raise RuntimeError(msg) from cleanup_error

    LOGGER.info(
        "Remote smoke render produced %s via %s (mean_luma=%.3f)",
        frame_path,
        host,
        image_info.mean_luma,
    )
    return SmokeRenderResult(
        run_dir=run_dir,
        frame_path=frame_path,
        manifest_path=manifest_path,
        ue_log_path=ue_log_path,
        mean_luma=image_info.mean_luma,
    )


def _cleanup_remote_paths(
    remote: RemoteHost,
    remote_paths: list[PurePosixPath],
    *,
    timeout_sec: int,
) -> tuple[dict[str, Any], Exception | None]:
    cleanup: dict[str, Any] = {
        "status": "ok",
        "removed_paths": [str(path) for path in remote_paths],
        "verified": False,
        "cleaned_utc": utc_timestamp(),
    }
    try:
        for remote_path in remote_paths:
            remote.remove_tree(str(remote_path), timeout_sec=timeout_sec)
        verify_command = "\n".join(
            ["set -euo pipefail"]
            + [f"test ! -e {shlex.quote(str(remote_path))}" for remote_path in remote_paths]
        )
        verify = remote.run(verify_command, timeout_sec=timeout_sec, check=False)
        cleanup["verify_returncode"] = verify.returncode
        cleanup["verified"] = verify.returncode == 0
        if verify.returncode != 0:
            cleanup["status"] = "failed"
            cleanup["verify_stderr"] = verify.stderr[-2000:]
            msg = f"Remote cleanup verification failed for {cleanup['removed_paths']}"
            return cleanup, RuntimeError(msg)
    except Exception as exc:
        cleanup["status"] = "failed"
        cleanup["error_type"] = type(exc).__name__
        cleanup["error"] = str(exc)
        return cleanup, exc
    return cleanup, None


def _record_remote_cleanup(manifest_path: Path, cleanup: dict[str, Any]) -> None:
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cleanup"] = cleanup
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def _prepare_remote_smoke_runtime(remote: RemoteHost, *, run_user: str) -> dict[str, Any]:
    result = remote.run(
        remote_python_command(
            _REMOTE_SMOKE_PREPARE_SCRIPT,
            {
                "UEF_HOST_NAME": remote.config.name,
                "UEF_WORK_DIR": str(remote.config.work_dir),
                "UEF_ENGINE_DIR": str(remote.config.engine_dir),
                "UEF_REMOTE_RUN_USER": run_user,
            },
        ),
        timeout_sec=120,
    )
    payload = parse_json_stdout(result.stdout)
    LOGGER.info(
        "Prepared remote smoke runtime on %s: mode=%s run_user=%s",
        remote.config.name,
        payload.get("mode"),
        payload.get("run_user"),
    )
    return payload


def _package_smoke_project(settings: Settings, package_dir: Path) -> Path:
    source = settings.project_root / "ue/UEFBase"
    project_path = source / "UEFBase.uproject"
    config_dir = source / "Config"
    python_dir = source / "Content/Python"
    if not project_path.exists():
        msg = f"UE project not found: {project_path}"
        raise FileNotFoundError(msg)
    if not python_dir.exists():
        msg = f"UE Python directory not found: {python_dir}"
        raise FileNotFoundError(msg)
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


def _wait_for_remote_job(
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
            msg = f"Remote job {job_id} timed out; last status={status}"
            raise TimeoutError(msg)
        time.sleep(poll_interval_sec)


def _validate_image(frame_path: Path) -> ImageInfo:
    with Image.open(frame_path) as image:
        image.load()
        rgb = image.convert("RGB")
        luma = rgb.convert("L")
        stat = ImageStat.Stat(luma)
        mean_luma = float(stat.mean[0])
        luma_stddev = float(stat.stddev[0])
        extrema = luma.getextrema()
        if not isinstance(extrema[0], int) or not isinstance(extrema[1], int):
            msg = f"Unexpected luma extrema for {frame_path}: {extrema}"
            raise RuntimeError(msg)
        luma_min, luma_max = extrema
        width, height = rgb.size
    if mean_luma <= 5:
        msg = f"Smoke render image is too dark: mean_luma={mean_luma:.3f}; path={frame_path}"
        raise RuntimeError(msg)
    if luma_stddev <= 1 or (luma_max - luma_min) <= 5:
        msg = (
            "Smoke render image is too uniform: "
            f"stddev={luma_stddev:.3f} range={luma_min}-{luma_max}; path={frame_path}"
        )
        raise RuntimeError(msg)
    return ImageInfo(
        width=width,
        height=height,
        mean_luma=round(mean_luma, 3),
        luma_stddev=round(luma_stddev, 3),
        luma_min=int(luma_min),
        luma_max=int(luma_max),
    )


def _manifest(
    job: dict[str, Any],
    command: list[str],
    duration_sec: float,
    ue_result: Any,
    *,
    status: str,
    runtime: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "render_kind": str(job.get("render_kind", "unknown")),
        "job": job,
        "command": command,
        "duration_sec": duration_sec,
        "ue_log": str(ue_result.log_path),
        "runtime": runtime,
        "ue_summary": {
            "warning_count": ue_result.summary.warning_count,
            "warning_noise_count": ue_result.summary.warning_noise_count,
            "warning_noise": ue_result.summary.warning_noise or {},
            "error_count": ue_result.summary.error_count,
            "error_noise_count": ue_result.summary.error_noise_count,
            "error_noise": ue_result.summary.error_noise or {},
            "warnings": ue_result.summary.warnings,
            "errors": ue_result.summary.errors,
        },
    }


def _finalize_manifest(
    manifest_path: Path,
    job: dict[str, Any],
    command: list[str],
    duration_sec: float,
    ue_result: Any,
    *,
    status: str,
    runtime: dict[str, Any],
    error: str | None = None,
    image_info: ImageInfo | None = None,
    settings: Settings | None = None,
    ddc_dir: Path | None = None,
    ue_home: Path | None = None,
) -> dict[str, Any]:
    manifest = _manifest(job, command, duration_sec, ue_result, status=status, runtime=runtime)
    if error is not None:
        manifest["error"] = error
    if image_info is not None:
        frame_path = Path(str(job["out_dir"])) / "frame_0000.png"
        manifest["image"] = {
            "path": str(frame_path),
            "width": image_info.width,
            "height": image_info.height,
            "mean_luma": image_info.mean_luma,
            "luma_stddev": image_info.luma_stddev,
            "luma_min": image_info.luma_min,
            "luma_max": image_info.luma_max,
        }
    if settings is not None:
        manifest["engine"] = _engine_version(settings)
    if ddc_dir is not None:
        manifest["ddc_dir"] = str(ddc_dir)
    if ue_home is not None:
        manifest["ue_home"] = str(ue_home)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def _runtime_settings(runtime_lib_dir: Path | None) -> dict[str, Any]:
    if runtime_lib_dir is None:
        return {"enabled": False, "lib_dir": None, "reason": "runtime_lib_dir not configured"}
    loader = runtime_lib_dir / "libvulkan.so.1"
    if not loader.exists():
        msg = f"Configured runtime_lib_dir does not contain libvulkan.so.1: {runtime_lib_dir}"
        raise FileNotFoundError(msg)
    return {
        "enabled": True,
        "lib_dir": str(runtime_lib_dir),
        "reason": "configured runtime_lib_dir",
        "libvulkan": str(loader),
    }


def _prepend_env_path(path: Path, env_var: str) -> str:
    current = os.environ.get(env_var)
    if current:
        return f"{path}:{current}"
    return str(path)


def _engine_version(settings: Settings) -> dict[str, Any]:
    version_path = settings.ue_root / "Engine/Build/Build.version"
    try:
        return json.loads(version_path.read_text(encoding="utf-8"))
    except OSError as exc:
        msg = f"Could not read UE Build.version: {version_path}"
        raise FileNotFoundError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"Could not parse UE Build.version: {version_path}"
        raise ValueError(msg) from exc


_REMOTE_SMOKE_PREPARE_SCRIPT = r"""
import json
import os
import pwd
import shutil
import subprocess
import time
from pathlib import Path

host_name = os.environ["UEF_HOST_NAME"]
work_dir = Path(os.environ["UEF_WORK_DIR"])
engine_dir = Path(os.environ["UEF_ENGINE_DIR"])
run_user = os.environ["UEF_REMOTE_RUN_USER"]


def run(command):
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=True,
    )


def user_exists(name):
    try:
        pwd.getpwnam(name)
    except KeyError:
        return False
    return True


def ensure_symlink(link_path, target_path, *, relative_target=None):
    if link_path.exists() or link_path.is_symlink():
        if link_path.resolve() != target_path.resolve():
            raise RuntimeError(f"Unexpected shader compatibility path: {link_path}")
        return False
    if os.geteuid() != 0:
        raise RuntimeError(f"Missing shader compatibility link and not root: {link_path}")
    link_path.symlink_to(relative_target or target_path)
    return True


actions = []
executable = engine_dir / "Engine/Binaries/Linux/UnrealEditor-Cmd"
version_path = engine_dir / "Engine/Build/Build.version"
if not executable.exists():
    raise RuntimeError(f"UnrealEditor-Cmd is missing: {executable}")
if not version_path.exists():
    raise RuntimeError(f"Build.version is missing: {version_path}")

shader_private_dir = engine_dir / "Engine/Shaders/Private"
ray_tracing_dir = shader_private_dir / "RayTracing"
raytracing_dir = shader_private_dir / "Raytracing"
sky_light_shader = ray_tracing_dir / "RayTracingSkyLightRGS.usf"
sky_light_compat_shader = ray_tracing_dir / "RaytracingSkylightRGS.usf"
niagara_modules_dir = (
    engine_dir / "Engine/Plugins/FX/Niagara/Shaders/Private/Stateless/Modules"
)
niagara_scale_mesh_shader = niagara_modules_dir / "NiagaraStatelessModule_ScaleMeshSizeBySpeed.ush"
niagara_scale_mesh_compat_shader = (
    niagara_modules_dir / "NiagaraStatelessModule_ScaleMeshSizebySpeed.ush"
)
if not ray_tracing_dir.exists():
    raise RuntimeError(f"RayTracing shader directory is missing: {ray_tracing_dir}")
if not sky_light_shader.exists():
    raise RuntimeError(f"RayTracing skylight shader is missing: {sky_light_shader}")
if not niagara_scale_mesh_shader.exists():
    raise RuntimeError(f"Niagara scale mesh shader is missing: {niagara_scale_mesh_shader}")
if ensure_symlink(raytracing_dir, ray_tracing_dir, relative_target=Path("RayTracing")):
    actions.append("created shader directory compatibility link Raytracing -> RayTracing")
if ensure_symlink(
    sky_light_compat_shader,
    sky_light_shader,
    relative_target=Path("RayTracingSkyLightRGS.usf"),
):
    actions.append(
        "created shader file compatibility link RaytracingSkylightRGS.usf -> "
        "RayTracingSkyLightRGS.usf"
    )
if ensure_symlink(
    niagara_scale_mesh_compat_shader,
    niagara_scale_mesh_shader,
    relative_target=Path("NiagaraStatelessModule_ScaleMeshSizeBySpeed.ush"),
):
    actions.append(
        "created Niagara shader compatibility link "
        "NiagaraStatelessModule_ScaleMeshSizebySpeed.ush -> "
        "NiagaraStatelessModule_ScaleMeshSizeBySpeed.ush"
    )

if os.geteuid() == 0:
    if shutil.which("runuser") is None:
        raise RuntimeError("runuser is required to launch Unreal as a non-root user")
    if not user_exists(run_user):
        run(["useradd", "-m", "-s", "/bin/bash", run_user])
        actions.append(f"created user {run_user}")
    if work_dir.parts[:2] == ("/", "root") or engine_dir.parts[:2] == ("/", "root"):
        setfacl = shutil.which("setfacl")
        if setfacl is None:
            raise RuntimeError("setfacl is required for /root-backed remote paths; install acl")
        run([setfacl, "-m", f"u:{run_user}:--x", "/root"])
        actions.append(f"granted {run_user} execute ACL on /root")
    run(["runuser", "-u", run_user, "--", "test", "-x", str(executable)])
    run(["runuser", "-u", run_user, "--", "test", "-r", str(version_path)])
    mode = "root-orchestrated"
else:
    run_user = ""
    mode = "current-user"

print(
    json.dumps(
        {
            "status": "ok",
            "host": host_name,
            "mode": mode,
            "run_user": run_user,
            "actions": actions,
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        sort_keys=True,
    )
)
"""


_REMOTE_SMOKE_SCRIPT = r"""
import json
import os
import shutil
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

frame_path = run_dir / "frame_0000.png"
raw_frame_path = run_dir / "smoke_frame.png"
manifest_path = run_dir / "manifest.json"
ue_log_path = run_dir / "ue.log"
job_path = run_dir / "job.json"
project_path = project_dir / "UEFBase.uproject"
script_path = project_dir / "Content/Python/uef_smoke.py"
ddc_dir = work_dir / "ddc" / "smoke"
ue_home = work_dir / "ue_home" / "smoke"


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
            if "LogDirectoryWatcher: Warning:" in line:
                reason = "directory_watcher"
                warning_noise[reason] = warning_noise.get(reason, 0) + 1
                continue
            if "LogStreaming: Warning: Failed to read file" in line and ".png" in line:
                reason = "missing_editor_icon"
                warning_noise[reason] = warning_noise.get(reason, 0) + 1
                continue
            if "USD" in line and "plugInfo.json" in line:
                reason = "usd_plugin_metadata_write_permission"
                warning_noise[reason] = warning_noise.get(reason, 0) + 1
                continue
            if "/Engine/" in line and "WritePermissions." in line and "Permission denied" in line:
                reason = "engine_content_write_permission_probe"
                warning_noise[reason] = warning_noise.get(reason, 0) + 1
                continue
            warning_count += 1
            if len(warnings) < 20:
                warnings.append(line)
        if "Error:" in line:
            if "LogUsd: Error: TF_DIAGNOSTIC_CODING_ERROR_TYPE: Failed to load plugin" in line:
                reason = "missing_optional_usd_plugin"
                error_noise[reason] = error_noise.get(reason, 0) + 1
                continue
            if (
                "LogFeaturePack: Error: Error in Feature pack" in line
                and "Cannot find screenshot" in line
            ):
                reason = "missing_feature_pack_screenshot"
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
        subprocess.run(["chown", "-R", f"{run_user}:{run_user}", str(path)], check=True)


def command_for_run_user(command, env):
    if not run_user or os.geteuid() != 0:
        return command, env
    env_keys = ["HOME", "UEF_JOB_FILE", "UE-LocalDataCachePath", "LD_LIBRARY_PATH"]
    env_args = [f"{key}={env[key]}" for key in env_keys if key in env]
    return ["runuser", "-u", run_user, "--", "env", *env_args, *command], os.environ.copy()


try:
    sentinel = work_dir / ".uef_node"
    payload = json.loads(sentinel.read_text(encoding="utf-8"))
    if payload.get("host") != host_name:
        raise RuntimeError(f"sentinel host mismatch: {payload.get('host')} != {host_name}")
    executable = engine_dir / "Engine/Binaries/Linux/UnrealEditor-Cmd"
    version_path = engine_dir / "Engine/Build/Build.version"
    if not executable.exists():
        raise RuntimeError(f"UnrealEditor-Cmd is missing: {executable}")
    if not version_path.exists():
        raise RuntimeError(f"Build.version is missing: {version_path}")
    run_dir.mkdir(parents=True, exist_ok=True)
    ddc_dir.mkdir(parents=True, exist_ok=True)
    ue_home.mkdir(parents=True, exist_ok=True)
    job = {
        "out_dir": str(run_dir),
        "filename": raw_frame_path.name,
        "render_kind": "scene",
        "width": 1280,
        "height": 720,
        "remote_host": host_name,
    }
    job_path.write_text(json.dumps(job, indent=2, sort_keys=True), encoding="utf-8")
    chown_for_run_user(run_dir, ddc_dir, ue_home)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(ue_home),
            "UEF_JOB_FILE": str(job_path),
            "UE-LocalDataCachePath": str(ddc_dir),
        }
    )
    command = [
        str(executable),
        str(project_path),
        f"-ExecutePythonScript={script_path}",
        "-unattended",
        "-nopause",
        "-nosplash",
        "-RenderOffScreen",
        "-stdout",
        "-FullStdOutLogOutput",
        "-NoSound",
        "-ddc=InstalledNoZenLocalFallback",
        f"-LocalDataCachePath={ddc_dir}",
    ]
    run_command, run_env = command_for_run_user(command, env)
    write_status("running", "rendering", command=run_command, ue_command=command, run_user=run_user)
    started = time.monotonic()
    with ue_log_path.open("w", encoding="utf-8", errors="replace") as log_file:
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
    summary = summarize_log(ue_log_path)
    if raw_frame_path.exists():
        raw_frame_path.replace(frame_path)
    manifest = {
        "schema_version": 1,
        "status": "ok" if result.returncode == 0 and frame_path.exists() else "failed",
        "render_kind": "scene",
        "job": job,
        "remote_host": host_name,
        "job_id": job_id,
        "command": run_command,
        "ue_command": command,
        "run_user": run_user,
        "duration_sec": duration_sec,
        "ue_log": str(ue_log_path),
        "frame": str(frame_path),
        "engine": json.loads(version_path.read_text(encoding="utf-8")),
        "ddc_dir": str(ddc_dir),
        "ue_summary": summary,
        "returncode": result.returncode,
    }
    if result.returncode != 0:
        manifest["error"] = f"UE exited with {result.returncode}"
    elif not frame_path.exists():
        manifest["error"] = f"Expected frame missing: {frame_path}"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    if manifest["status"] == "ok":
        write_status("complete", "rendered", duration_sec=duration_sec, frame=str(frame_path))
    else:
        write_status("failed", "render_failed", manifest=str(manifest_path))
except subprocess.TimeoutExpired as exc:
    write_status("failed", "timeout", error=str(exc), traceback=traceback.format_exc())
    raise
except Exception as exc:
    write_status("failed", "error", error=str(exc), traceback=traceback.format_exc())
    raise
"""
