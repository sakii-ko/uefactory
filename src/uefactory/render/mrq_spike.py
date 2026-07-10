from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageStat

from uefactory.core.config import Settings
from uefactory.core.paths import resolve_path, utc_timestamp
from uefactory.render.smoke import _prepend_env_path, _runtime_settings
from uefactory.render.ue_runner import UERunnerError, run_ue


@dataclass(frozen=True)
class MRQSpikeResult:
    run_dir: Path
    manifest_path: Path
    ue_log_path: Path
    setup_log_path: Path
    frame_paths: list[Path]
    frame_luma: list[float]


def render_mrq_spike(
    settings: Settings,
    out_root: Path,
    *,
    timeout_sec: int = 1800,
    frames: int = 8,
) -> MRQSpikeResult:
    run_dir = resolve_path(out_root, settings.project_root) / utc_timestamp()
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = run_dir / "manifest.json"
    ue_log_path = run_dir / "ue.log"
    setup_log_path = run_dir / "ue_setup.log"
    job_path = run_dir / "job.json"

    project_path = settings.project_root / "ue/UEFBase/UEFBase.uproject"
    script_path = settings.project_root / "ue/UEFBase/Content/Python/uef_mrq_spike.py"
    runtime_script_path = (
        settings.project_root / "ue/UEFBase/Content/Python/uef_mrq_spike_runtime.py"
    )
    if not project_path.exists():
        msg = f"UE project not found: {project_path}"
        raise FileNotFoundError(msg)
    if not script_path.exists():
        msg = f"UE MRQ spike script not found: {script_path}"
        raise FileNotFoundError(msg)
    if not runtime_script_path.exists():
        msg = f"UE MRQ spike runtime executor not found: {runtime_script_path}"
        raise FileNotFoundError(msg)

    job = {
        "out_dir": str(run_dir),
        "render_kind": "mrq_spike",
        "width": 640,
        "height": 360,
        "frames": frames,
        "sequence_path": "/Game/UEF/MRQSpike/UEF_MRQ_Spike.UEF_MRQ_Spike",
    }
    job_path.write_text(json.dumps(job, indent=2, sort_keys=True), encoding="utf-8")

    ddc_dir = settings.ddc_dir or settings.data_dir / "ddc"
    ddc_dir.mkdir(parents=True, exist_ok=True)
    ue_home = settings.ue_home
    ue_home.mkdir(parents=True, exist_ok=True)
    runtime = _runtime_settings(settings.runtime_lib_dir)

    setup_command: list[str | Path] = [
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
        "-ddc=InstalledNoZenLocalFallback",
        f"-LocalDataCachePath={ddc_dir}",
    ]
    env = {
        "HOME": str(ue_home),
        "UEF_JOB_FILE": str(job_path),
        "UE-LocalDataCachePath": str(ddc_dir),
    }
    if runtime["enabled"]:
        env["LD_LIBRARY_PATH"] = _prepend_env_path(Path(str(runtime["lib_dir"])), "LD_LIBRARY_PATH")

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
            manifest_path,
            job,
            {"setup": exc.result.command},
            str(exc),
            runtime,
        )
        raise
    if setup_result.summary.error_count:
        _write_failure_manifest(
            manifest_path,
            job,
            {"setup": setup_result.command},
            f"MRQ spike setup UE log contains {setup_result.summary.error_count} error lines",
            runtime,
        )
        msg = f"MRQ spike setup UE log contains errors; UE log: {setup_log_path}"
        raise RuntimeError(msg)
    if setup_result.summary.warning_count:
        _write_failure_manifest(
            manifest_path,
            job,
            {"setup": setup_result.command},
            f"MRQ spike setup UE log contains {setup_result.summary.warning_count} warning lines",
            runtime,
        )
        msg = f"MRQ spike setup UE log contains warnings; UE log: {setup_log_path}"
        raise RuntimeError(msg)

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
        "-resx=640",
        "-resy=360",
        "-ddc=InstalledNoZenLocalFallback",
        f"-LocalDataCachePath={ddc_dir}",
        "-MoviePipelineLocalExecutorClass=/Script/MovieRenderPipelineCore.MoviePipelinePythonHostExecutor",
        "-ExecutorPythonClass=/Engine/PythonTypes.UEFMRQSpikeRuntimeExecutor",
        f"-LevelSequence={job['sequence_path']}",
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
            job,
            {"setup": setup_result.command, "render": exc.result.command},
            str(exc),
            runtime,
        )
        raise
    if ue_result.summary.error_count:
        _write_failure_manifest(
            manifest_path,
            job,
            {"setup": setup_result.command, "render": ue_result.command},
            f"MRQ spike render UE log contains {ue_result.summary.error_count} error lines",
            runtime,
        )
        msg = f"MRQ spike render UE log contains errors; UE log: {ue_log_path}"
        raise RuntimeError(msg)
    if ue_result.summary.warning_count:
        _write_failure_manifest(
            manifest_path,
            job,
            {"setup": setup_result.command, "render": ue_result.command},
            f"MRQ spike render UE log contains {ue_result.summary.warning_count} warning lines",
            runtime,
        )
        msg = f"MRQ spike render UE log contains warnings; UE log: {ue_log_path}"
        raise RuntimeError(msg)

    if not manifest_path.exists():
        _write_failure_manifest(
            manifest_path,
            job,
            {"setup": setup_result.command, "render": ue_result.command},
            f"MRQ spike manifest was not created: {manifest_path}",
            runtime,
        )
        msg = f"MRQ spike did not produce manifest; UE log: {ue_log_path}"
        raise RuntimeError(msg)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    frame_paths = sorted(run_dir.glob("*.png"))
    if len(frame_paths) != frames:
        msg = f"MRQ spike expected {frames} frames, found {len(frame_paths)}; run={run_dir}"
        raise RuntimeError(msg)
    frame_luma = [_mean_luma(path) for path in frame_paths]
    if any(luma <= 5 for luma in frame_luma):
        msg = f"MRQ spike produced dark frame luma={frame_luma}; run={run_dir}"
        raise RuntimeError(msg)

    manifest.update(
        {
            "status": "ok",
            "commands": {
                "setup": setup_result.command,
                "render": ue_result.command,
            },
            "runtime": runtime,
            "setup_summary": {
                "warning_count": setup_result.summary.warning_count,
                "warning_noise_count": setup_result.summary.warning_noise_count,
                "warning_noise": setup_result.summary.warning_noise or {},
                "error_count": setup_result.summary.error_count,
                "error_noise_count": setup_result.summary.error_noise_count,
                "error_noise": setup_result.summary.error_noise or {},
                "warnings": setup_result.summary.warnings,
                "errors": setup_result.summary.errors,
            },
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
            "frame_luma": frame_luma,
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return MRQSpikeResult(
        run_dir=run_dir,
        manifest_path=manifest_path,
        ue_log_path=ue_log_path,
        setup_log_path=setup_log_path,
        frame_paths=frame_paths,
        frame_luma=frame_luma,
    )


def compare_spike_luma(first: MRQSpikeResult, second: MRQSpikeResult) -> list[tuple[float, float]]:
    if len(first.frame_luma) != len(second.frame_luma):
        msg = f"Frame count mismatch: {len(first.frame_luma)} != {len(second.frame_luma)}"
        raise ValueError(msg)
    pairs = list(zip(first.frame_luma, second.frame_luma, strict=True))
    mismatches = [(left, right) for left, right in pairs if left != right]
    if mismatches:
        msg = f"MRQ spike luma mismatch: {mismatches}"
        raise RuntimeError(msg)
    return pairs


def _mean_luma(frame_path: Path) -> float:
    with Image.open(frame_path) as image:
        image.load()
        stat = ImageStat.Stat(image.convert("RGB").convert("L"))
    return round(float(stat.mean[0]), 3)


def _write_failure_manifest(
    manifest_path: Path,
    job: dict[str, object],
    commands: dict[str, list[str]],
    error: str,
    runtime: dict[str, object],
) -> None:
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "failed",
                "render_kind": "mrq_spike",
                "job": job,
                "commands": commands,
                "runtime": runtime,
                "error": error,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
