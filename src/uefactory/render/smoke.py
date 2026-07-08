from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat

from uefactory.core.config import Settings
from uefactory.core.paths import resolve_path, utc_timestamp
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
