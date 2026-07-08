from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from uefactory.core.config import Settings
from uefactory.core.paths import resolve_path, utc_timestamp
from uefactory.render.jobspec import RenderJobSpec, load_jobspec
from uefactory.render.passes import (
    PassValidation,
    assert_passes_distinct,
    stable_validation_payload,
    validate_render_pass,
)
from uefactory.render.smoke import _prepend_env_path, _runtime_settings
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

    ue_job = _ue_job_payload(spec, run_id, run_dir, sequence_path)
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
    spec: RenderJobSpec,
    run_id: str,
    run_dir: Path,
    sequence_path: str,
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
        "lighting": {"preset": spec.lighting.preset},
        "out_dir": str(run_dir),
        "passes": list(spec.passes),
        "render_kind": "job",
        "run_id": run_id,
        "schema_version": 2,
        "sequence_path": sequence_path,
    }


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
