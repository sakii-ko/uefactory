from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from uefactory.core.config import Settings
from uefactory.render.job import _new_run_id, _ue_job_payload_with_lighting, render_job
from uefactory.render.jobspec import load_jobspec
from uefactory.render.ue_runner import LogSummary, UERunnerError, UERunResult


def test_new_run_id_is_unique_for_consecutive_calls_in_same_timestamp(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("uefactory.render.job.utc_timestamp", lambda: "20260710T120000Z")

    run_ids = [_new_run_id() for _ in range(8)]

    assert len(set(run_ids)) == len(run_ids)
    assert all(run_id.startswith("20260710T120000Z_") for run_id in run_ids)


def test_hdri_payload_uses_a_separate_beauty_sequence(tmp_path: Path) -> None:
    _, job_path = _local_render_fixture(tmp_path)
    job_path.write_text(
        job_path.read_text(encoding="utf-8").replace(
            "  preset: three_point",
            "  preset: hdri\n  hdri: studio_small_03_1k",
        ),
        encoding="utf-8",
    )
    spec = load_jobspec(job_path)
    run_id = "20260710T120000Z_deadbeef"
    sequence_path = f"/Game/UEF/RenderJobs/{run_id}/UEF_RenderJob_{run_id}.UEF_RenderJob_{run_id}"

    payload = _ue_job_payload_with_lighting(
        spec=spec,
        run_id=run_id,
        run_dir=tmp_path / "out",
        sequence_path=sequence_path,
        lighting={"preset": "hdri", "hdri": "studio_small_03_1k"},
    )

    assert payload["sequence_path"] == sequence_path
    assert payload["beauty_sequence_path"] == (
        f"/Game/UEF/RenderJobs/{run_id}/UEF_RenderJobBeauty_{run_id}.UEF_RenderJobBeauty_{run_id}"
    )


def test_render_job_preserves_failed_runtime_manifest(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)
    monkeypatch.setattr(
        "uefactory.render.job.run_ue",
        _fake_run_ue(runtime_status="failed", write_frames=False),
    )

    with pytest.raises(RuntimeError, match="UE runtime reported render failure"):
        render_job(settings=settings, job_path=job_path, timeout_sec=60)

    manifest = _read_only_manifest(settings.project_root)
    assert manifest["status"] == "failed"
    assert manifest["error"] == "synthetic runtime failure"


def test_render_job_preserves_runtime_root_cause_when_process_exits_nonzero(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)
    call_count = 0

    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        nonlocal call_count
        del cwd, timeout_sec
        call_count += 1
        argv = [str(part) for part in command]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        result = UERunResult(
            command=argv,
            returncode=0 if call_count == 1 else 17,
            duration_sec=0.01,
            log_path=log_path,
            summary=LogSummary(warnings=[], errors=[], warning_count=0, error_count=0),
        )
        if call_count == 2:
            assert env is not None
            job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
            Path(job["out_dir"]).joinpath("manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "status": "failed",
                        "error": "specific runtime normalization failure",
                        "runtime_detail": {"missing_pass": "depth"},
                    }
                ),
                encoding="utf-8",
            )
            raise UERunnerError(result)
        return result

    monkeypatch.setattr("uefactory.render.job.run_ue", fake_run_ue)

    with pytest.raises(UERunnerError):
        render_job(settings=settings, job_path=job_path, timeout_sec=60)

    manifest = _read_only_manifest(settings.project_root)
    assert manifest["status"] == "failed"
    assert manifest["error"] == "specific runtime normalization failure"
    assert manifest["runtime_detail"] == {"missing_pass": "depth"}
    assert "exit code 17" in manifest["host_error"]
    assert manifest["asset_cleanup"]["status"] == "ok"


def test_render_job_records_cleanup_failure_after_setup_interrupt(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)

    def interrupt_setup(*args: Any, **kwargs: Any) -> UERunResult:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr("uefactory.render.job.run_ue", interrupt_setup)
    monkeypatch.setattr(
        "uefactory.render.job._cleanup_local_job_assets",
        lambda settings, run_id: {
            "path": f"ue/UEFBase/Content/UEF/RenderJobs/{run_id}",
            "status": "failed",
            "removed": False,
            "error_type": "PermissionError",
            "error": "synthetic cleanup denial",
        },
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        render_job(settings=settings, job_path=job_path, timeout_sec=60)

    manifest = _read_only_manifest(settings.project_root)
    assert manifest["status"] == "failed"
    assert manifest["error"] == "KeyboardInterrupt"
    assert manifest["asset_cleanup"]["status"] == "failed"
    assert manifest["cleanup_error"] == {
        "type": "PermissionError",
        "message": "synthetic cleanup denial",
    }
    assert any("cleanup also failed" in note for note in raised.value.__notes__)


def test_render_job_marks_manifest_failed_when_final_artifact_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)
    monkeypatch.setattr(
        "uefactory.render.job.run_ue",
        _fake_run_ue(runtime_status="ok", write_frames=True),
    )
    monkeypatch.setattr("uefactory.render.artifacts.shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "uefactory.render.artifacts.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            23,
            stdout="encoder setup",
            stderr="synthetic codec failure",
        ),
    )

    with pytest.raises(RuntimeError, match="synthetic codec failure"):
        render_job(settings=settings, job_path=job_path, timeout_sec=60)

    manifest = _read_only_manifest(settings.project_root)
    assert manifest["status"] == "failed"
    assert manifest["error"].startswith("Host validation/artifact failure: RuntimeError:")
    assert "synthetic codec failure" in manifest["error"]


def test_render_job_marks_manifest_failed_when_host_validation_is_interrupted(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)
    monkeypatch.setattr(
        "uefactory.render.job.run_ue",
        _fake_run_ue(runtime_status="ok", write_frames=True),
    )

    def interrupt_validation(**kwargs: Any) -> None:
        del kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "uefactory.render.job._validate_render_output",
        interrupt_validation,
    )

    with pytest.raises(KeyboardInterrupt):
        render_job(settings=settings, job_path=job_path, timeout_sec=60)

    manifest = _read_only_manifest(settings.project_root)
    assert manifest["status"] == "failed"
    assert manifest["error"] == "Host validation/artifact failure: KeyboardInterrupt"


def test_render_job_manifest_uses_relative_frame_paths(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings, job_path = _local_render_fixture(tmp_path)
    monkeypatch.setattr(
        "uefactory.render.job.run_ue",
        _fake_run_ue(runtime_status="ok", write_frames=True),
    )
    monkeypatch.setattr("uefactory.render.artifacts.shutil.which", lambda name: "/usr/bin/ffmpeg")

    def fake_ffmpeg(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(args[0][-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr("uefactory.render.artifacts.subprocess.run", fake_ffmpeg)

    result = render_job(settings=settings, job_path=job_path, timeout_sec=60)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "ok"
    assert manifest["frame_paths"] == {
        "beauty_lit": [
            "beauty_lit/frame_0000.png",
            "beauty_lit/frame_0001.png",
        ]
    }
    assert str(result.run_dir) not in json.dumps(manifest["frame_paths"])


def _local_render_fixture(tmp_path: Path) -> tuple[Settings, Path]:
    project_root = tmp_path / "project"
    script_dir = project_root / "ue/UEFBase/Content/Python"
    config_dir = project_root / "ue/UEFBase/Config"
    engine_version = tmp_path / "engine/Engine/Build/Build.version"
    script_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    engine_version.parent.mkdir(parents=True)
    project_root.joinpath("ue/UEFBase/UEFBase.uproject").write_text("{}", encoding="utf-8")
    script_dir.joinpath("uef_render_job.py").write_text("print('setup')\n", encoding="utf-8")
    script_dir.joinpath("uef_render_job_runtime.py").write_text(
        "print('runtime')\n", encoding="utf-8"
    )
    config_dir.joinpath("DefaultEngine.ini").write_text(
        "[/Script/Engine.Engine]\n", encoding="utf-8"
    )
    engine_version.write_text(
        json.dumps({"MajorVersion": 5, "MinorVersion": 5, "PatchVersion": 4}),
        encoding="utf-8",
    )
    job_path = project_root / "job.yaml"
    job_path.write_text(
        "\n".join(
            [
                "job: render",
                "assets: ['builtin:cube']",
                "camera:",
                "  rig: orbit",
                "  views: 2",
                "  elevation_deg: 20",
                "  fov: 55",
                "  resolution: [64, 64]",
                "lighting:",
                "  preset: three_point",
                "passes: ['beauty_lit']",
                "output:",
                "  dir: out/renders",
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(
        project_root=project_root,
        ue_root=tmp_path / "engine",
        ue_home=tmp_path / "ue_home",
        data_dir=project_root / "data",
        log_dir=project_root / "logs",
    )
    return settings, job_path


def _fake_run_ue(
    *,
    runtime_status: str,
    write_frames: bool,
) -> Any:
    def fake_run_ue(
        command: Sequence[str | Path],
        *,
        cwd: Path,
        log_path: Path,
        timeout_sec: int,
        env: Mapping[str, str] | None = None,
    ) -> UERunResult:
        del cwd, timeout_sec
        argv = [str(part) for part in command]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        if "-game" in argv:
            assert env is not None
            job = json.loads(Path(env["UEF_JOB_FILE"]).read_text(encoding="utf-8"))
            assert job["beauty_sequence_path"] == job["sequence_path"]
            out_dir = Path(job["out_dir"])
            manifest: dict[str, Any] = {
                "schema_version": 2,
                "status": runtime_status,
                "render_kind": "job",
            }
            if runtime_status == "failed":
                manifest["error"] = "synthetic runtime failure"
            out_dir.joinpath("manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            if write_frames:
                beauty_dir = out_dir / "beauty_lit"
                beauty_dir.mkdir(parents=True)
                for index in range(2):
                    _write_gradient_png(
                        beauty_dir / f"frame_{index:04d}.png",
                        blue=180 - index * 40,
                    )
        return UERunResult(
            command=argv,
            returncode=0,
            duration_sec=0.01,
            log_path=log_path,
            summary=LogSummary(warnings=[], errors=[], warning_count=0, error_count=0),
        )

    return fake_run_ue


def _read_only_manifest(project_root: Path) -> dict[str, Any]:
    manifests = list(project_root.joinpath("out/renders").glob("*/builtin_cube/manifest.json"))
    assert len(manifests) == 1
    return json.loads(manifests[0].read_text(encoding="utf-8"))


def _write_gradient_png(path: Path, *, blue: int) -> None:
    image = Image.new("RGB", (64, 64), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index in range(64):
        draw.line((index, 0, index, 63), fill=(index * 3, 80, blue))
    image.save(path)
