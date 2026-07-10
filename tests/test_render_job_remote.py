from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest
import typer
from PIL import Image, ImageDraw

from uefactory.cli.render import render_job_command
from uefactory.core.config import HostConfig, Settings
from uefactory.core.remote import RemoteCommandResult
from uefactory.render.job import (
    _REMOTE_RENDER_JOB_SCRIPT,
    _remote_runner_command,
    _wait_for_remote_render_job,
    render_job,
    render_job_remote,
)


def test_render_job_remote_pulls_validates_artifacts_and_cleans_up(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_minimal_ue_project(project_root)
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
                "  resolution: [128, 72]",
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
        data_dir=project_root / "data",
        log_dir=project_root / "logs",
        hosts={
            "l40s": HostConfig(
                name="l40s",
                ssh_alias="l40s",
                work_dir=Path("/remote/work"),
                engine_dir=Path("/remote/work/engine"),
            )
        },
    )
    calls: list[str] = []

    def fake_run(self: Any, command: str, *, timeout_sec: int = 60, check: bool = True) -> Any:
        calls.append(f"run:{command}")
        return RemoteCommandResult(
            command=["ssh", "l40s", command],
            returncode=0,
            stdout=json.dumps({"status": "ok", "mode": "current-user", "run_user": "uef"}),
            stderr="",
            duration_sec=0.01,
        )

    def fake_rsync_push(
        self: Any,
        sources: list[str | Path],
        remote_dest: str | Path,
        *,
        timeout_sec: int = 3600,
        delete: bool = False,
    ) -> Any:
        calls.append(f"push:{remote_dest}")
        if str(remote_dest).endswith("/project/"):
            package_dir = Path(str(sources[0]).removesuffix("/"))
            assert (
                package_dir.joinpath("remote_runner.py").read_text(encoding="utf-8")
                == _REMOTE_RENDER_JOB_SCRIPT
            )
        return RemoteCommandResult([], 0, "", "", 0.01)

    def fake_tmux_start(
        self: Any,
        job_id: str,
        command: str,
        *,
        timeout_sec: int = 60,
    ) -> Any:
        calls.append(f"tmux:{job_id}:{command}")
        return RemoteCommandResult([], 0, "", "", 0.01)

    statuses = [
        {"status": "running", "tmux_live": True},
        {"status": "complete", "tmux_live": False},
    ]

    def fake_tmux_status(self: Any, job_id: str, *, timeout_sec: int = 60) -> dict[str, Any]:
        calls.append(f"status:{job_id}")
        return statuses.pop(0)

    def fake_rsync_pull(
        self: Any,
        remote_sources: list[str | Path],
        local_dest: str | Path,
        *,
        timeout_sec: int = 3600,
        delete: bool = False,
    ) -> Any:
        calls.append(f"pull:{delete}:{local_dest}")
        beauty_dir = Path(local_dest) / "beauty_lit"
        beauty_dir.mkdir(parents=True)
        for index in range(2):
            _write_gradient_png(beauty_dir / f"frame_{index:04d}.png", blue=180 - index * 40)
        (Path(local_dest) / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "status": "ok",
                    "render_kind": "job",
                    "frames_found": {"beauty_lit": 2},
                }
            ),
            encoding="utf-8",
        )
        (Path(local_dest) / "ue.log").write_text("ok", encoding="utf-8")
        (Path(local_dest) / "ue_setup.log").write_text("ok", encoding="utf-8")
        return RemoteCommandResult([], 0, "", "", 0.01)

    def fake_remove_tree(self: Any, remote_path: str | Path, *, timeout_sec: int = 60) -> Any:
        calls.append(f"remove:{remote_path}")
        return RemoteCommandResult([], 0, "", "", 0.01)

    def fake_subprocess_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        output_path = Path(args[0][-1])
        output_path.write_bytes(b"mp4")
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr("time.sleep", lambda seconds: None)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.run", fake_run)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.rsync_push", fake_rsync_push)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_start", fake_tmux_start)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_status", fake_tmux_status)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.rsync_pull", fake_rsync_pull)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.remove_tree", fake_remove_tree)
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
    )
    monkeypatch.setattr("subprocess.run", fake_subprocess_run)

    result = render_job_remote(
        settings=settings,
        host="l40s",
        job_path=job_path,
        timeout_sec=60,
        poll_interval_sec=30,
    )

    assert result.artifacts is not None
    assert result.artifacts.contact_sheet.exists()
    assert result.artifacts.index_html.exists()
    assert result.artifacts.turntable_mp4 is not None
    assert result.artifacts.turntable_mp4.read_bytes() == b"mp4"
    assert any("UEF_REMOTE_RUN_USER=uef" in call for call in calls)
    assert any(call.startswith("tmux:render_l40s_") for call in calls)
    assert any(call.startswith("pull:True:") for call in calls)
    assert any(call.startswith("remove:/remote/work/jobs/render_l40s_") for call in calls)
    assert any("test ! -e /remote/work/jobs/render_l40s_" in call for call in calls)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["remote_host"] == "l40s"
    assert manifest["local_validation"]["status"] == "ok"
    assert manifest["frame_paths"] == {
        "beauty_lit": [
            "beauty_lit/frame_0000.png",
            "beauty_lit/frame_0001.png",
        ]
    }
    assert manifest["cleanup"]["status"] == "ok"
    assert manifest["cleanup"]["verified"] is True


def test_remote_runner_command_uses_uploaded_script_instead_of_inline_source() -> None:
    command = _remote_runner_command(
        PurePosixPath("/remote/work/jobs/job-1/project/remote_runner.py"),
        {
            "UEF_JOB_ID": "job-1",
            "UEF_REMOTE_RUN_USER": "uef worker",
        },
    )

    assert command == (
        "UEF_JOB_ID=job-1 UEF_REMOTE_RUN_USER='uef worker' "
        "python3 /remote/work/jobs/job-1/project/remote_runner.py"
    )
    assert _REMOTE_RENDER_JOB_SCRIPT not in command


def test_render_job_remote_timeout_stops_tmux_cleans_up_and_records_failure(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_minimal_ue_project(project_root)
    job_path = _write_render_job(project_root)
    settings = _remote_settings(project_root)
    calls: list[str] = []

    def fake_run(self: Any, command: str, **kwargs: Any) -> RemoteCommandResult:
        calls.append(f"run:{command}")
        return RemoteCommandResult(
            [],
            0,
            json.dumps({"status": "ok", "mode": "current-user", "run_user": "uef"}),
            "",
            0.01,
        )

    def fake_rsync_push(self: Any, *args: Any, **kwargs: Any) -> RemoteCommandResult:
        calls.append("push")
        return RemoteCommandResult([], 0, "", "", 0.01)

    def fake_tmux_start(
        self: Any,
        job_id: str,
        command: str,
        **kwargs: Any,
    ) -> RemoteCommandResult:
        calls.append(f"start:{job_id}")
        return RemoteCommandResult([], 0, "", "", 0.01)

    def fake_tmux_status(self: Any, job_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(f"status:{job_id}")
        return {"status": "running", "tmux_live": True}

    def fake_tmux_stop(self: Any, job_id: str, **kwargs: Any) -> RemoteCommandResult:
        calls.append(f"stop:{job_id}")
        return RemoteCommandResult([], 0, "", "", 0.01)

    def fake_remove_tree(
        self: Any,
        remote_path: str | Path,
        **kwargs: Any,
    ) -> RemoteCommandResult:
        calls.append(f"remove:{remote_path}")
        return RemoteCommandResult([], 0, "", "", 0.01)

    monkeypatch.setattr("uefactory.core.remote.RemoteHost.run", fake_run)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.rsync_push", fake_rsync_push)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_start", fake_tmux_start)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_status", fake_tmux_status)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_stop", fake_tmux_stop)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.remove_tree", fake_remove_tree)

    with pytest.raises(TimeoutError, match="timed out"):
        render_job_remote(
            settings=settings,
            host="l40s",
            job_path=job_path,
            timeout_sec=0,
            poll_interval_sec=1,
        )

    stop_index = next(index for index, call in enumerate(calls) if call.startswith("stop:"))
    remove_index = next(index for index, call in enumerate(calls) if call.startswith("remove:"))
    assert stop_index < remove_index
    manifests = list((project_root / "out/renders").glob("*/builtin_cube/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert "TimeoutError" in manifest["error"]
    assert manifest["cleanup"]["status"] == "ok"
    assert manifest["cleanup"]["verified"] is True


def test_render_job_remote_ambiguous_tmux_start_failure_still_stops_before_delete(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_minimal_ue_project(project_root)
    job_path = _write_render_job(project_root)
    settings = _remote_settings(project_root)
    calls: list[str] = []

    def fake_run(self: Any, command: str, **kwargs: Any) -> RemoteCommandResult:
        calls.append("run")
        return RemoteCommandResult(
            [],
            0,
            json.dumps({"status": "ok", "mode": "current-user", "run_user": "uef"}),
            "",
            0.01,
        )

    def fake_start(self: Any, job_id: str, command: str, **kwargs: Any) -> None:
        calls.append(f"start:{job_id}")
        raise RuntimeError("SSH failed after the remote command may have started")

    def fake_stop(self: Any, job_id: str, **kwargs: Any) -> RemoteCommandResult:
        calls.append(f"stop:{job_id}")
        return RemoteCommandResult([], 0, "{}", "", 0.01)

    def fake_remove(self: Any, path: str | Path, **kwargs: Any) -> RemoteCommandResult:
        calls.append(f"remove:{path}")
        return RemoteCommandResult([], 0, "", "", 0.01)

    monkeypatch.setattr("uefactory.core.remote.RemoteHost.run", fake_run)
    monkeypatch.setattr(
        "uefactory.core.remote.RemoteHost.rsync_push",
        lambda *args, **kwargs: RemoteCommandResult([], 0, "", "", 0.01),
    )
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_start", fake_start)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_stop", fake_stop)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.remove_tree", fake_remove)

    with pytest.raises(RuntimeError, match="may have started"):
        render_job_remote(
            settings=settings,
            host="l40s",
            job_path=job_path,
            timeout_sec=60,
            poll_interval_sec=1,
        )

    start_index = next(index for index, call in enumerate(calls) if call.startswith("start:"))
    stop_index = next(index for index, call in enumerate(calls) if call.startswith("stop:"))
    remove_index = next(index for index, call in enumerate(calls) if call.startswith("remove:"))
    assert start_index < stop_index < remove_index
    manifest = json.loads(
        next((project_root / "out/renders").glob("*/builtin_cube/manifest.json")).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["orchestration_error"] == {
        "type": "RuntimeError",
        "message": "SSH failed after the remote command may have started",
    }


def test_render_job_remote_keyboard_interrupt_stops_job_and_cleans_up(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_minimal_ue_project(project_root)
    settings = _remote_settings(project_root)
    calls: list[str] = []

    def fake_run(self: Any, command: str, **kwargs: Any) -> RemoteCommandResult:
        return RemoteCommandResult(
            [],
            0,
            json.dumps({"status": "ok", "mode": "current-user", "run_user": "uef"}),
            "",
            0.01,
        )

    def success(*args: Any, **kwargs: Any) -> RemoteCommandResult:
        return RemoteCommandResult([], 0, "", "", 0.01)

    def interrupted_status(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise KeyboardInterrupt

    def fake_stop(self: Any, job_id: str, **kwargs: Any) -> RemoteCommandResult:
        calls.append(f"stop:{job_id}")
        return RemoteCommandResult([], 0, "{}", "", 0.01)

    def fake_remove(self: Any, path: str | Path, **kwargs: Any) -> RemoteCommandResult:
        calls.append(f"remove:{path}")
        return RemoteCommandResult([], 0, "", "", 0.01)

    monkeypatch.setattr("uefactory.core.remote.RemoteHost.run", fake_run)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.rsync_push", success)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_start", success)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_status", interrupted_status)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_stop", fake_stop)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.remove_tree", fake_remove)

    with pytest.raises(KeyboardInterrupt):
        render_job_remote(
            settings=settings,
            host="l40s",
            job_path=_write_render_job(project_root),
            timeout_sec=60,
            poll_interval_sec=1,
        )

    stop_index = next(index for index, call in enumerate(calls) if call.startswith("stop:"))
    remove_index = next(index for index, call in enumerate(calls) if call.startswith("remove:"))
    assert stop_index < remove_index
    manifest = json.loads(
        next((project_root / "out/renders").glob("*/builtin_cube/manifest.json")).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["orchestration_error"]["type"] == "KeyboardInterrupt"
    assert manifest["cleanup"]["status"] == "ok"


def test_render_job_remote_does_not_delete_tree_when_stop_cannot_be_confirmed(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_minimal_ue_project(project_root)
    settings = _remote_settings(project_root)
    remove_called = False

    def fake_run(self: Any, command: str, **kwargs: Any) -> RemoteCommandResult:
        return RemoteCommandResult(
            [],
            0,
            json.dumps({"status": "ok", "mode": "current-user", "run_user": "uef"}),
            "",
            0.01,
        )

    def success(*args: Any, **kwargs: Any) -> RemoteCommandResult:
        return RemoteCommandResult([], 0, "", "", 0.01)

    def failed_stop(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("cannot verify remote process termination")

    def forbidden_remove(*args: Any, **kwargs: Any) -> None:
        nonlocal remove_called
        remove_called = True
        raise AssertionError("unsafe remove_tree call")

    monkeypatch.setattr("uefactory.core.remote.RemoteHost.run", fake_run)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.rsync_push", success)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_start", success)
    monkeypatch.setattr(
        "uefactory.core.remote.RemoteHost.tmux_status",
        lambda *args, **kwargs: {"status": "running", "phase": "render", "tmux_live": True},
    )
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_stop", failed_stop)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.remove_tree", forbidden_remove)

    with pytest.raises(TimeoutError, match="phase 'render' timed out"):
        render_job_remote(
            settings=settings,
            host="l40s",
            job_path=_write_render_job(project_root),
            timeout_sec=0,
            poll_interval_sec=1,
        )

    assert remove_called is False
    manifest = json.loads(
        next((project_root / "out/renders").glob("*/builtin_cube/manifest.json")).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["cleanup_error"]["message"] == "cannot verify remote process termination"
    assert manifest["cleanup"]["removed_paths"] == []
    assert manifest["cleanup"]["retained_paths"][0].startswith("/remote/work/jobs/")


def test_render_job_remote_rejects_failed_runtime_manifest(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_minimal_ue_project(project_root)
    job_path = _write_render_job(project_root)
    settings = _remote_settings(project_root)
    validation_called = False

    def fake_run(self: Any, command: str, **kwargs: Any) -> RemoteCommandResult:
        return RemoteCommandResult(
            [],
            0,
            json.dumps({"status": "ok", "mode": "current-user", "run_user": "uef"}),
            "",
            0.01,
        )

    def fake_rsync_pull(
        self: Any,
        remote_sources: list[str | Path],
        local_dest: str | Path,
        **kwargs: Any,
    ) -> RemoteCommandResult:
        (Path(local_dest) / "manifest.json").write_text(
            json.dumps({"schema_version": 2, "status": "failed", "error": "runtime failed"}),
            encoding="utf-8",
        )
        return RemoteCommandResult([], 0, "", "", 0.01)

    def unexpected_validation(**kwargs: Any) -> None:
        nonlocal validation_called
        validation_called = True
        raise AssertionError("failed runtime output must not be validated as successful")

    def success(*args: Any, **kwargs: Any) -> RemoteCommandResult:
        return RemoteCommandResult([], 0, "", "", 0.01)

    monkeypatch.setattr("uefactory.core.remote.RemoteHost.run", fake_run)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.rsync_push", success)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.rsync_pull", fake_rsync_pull)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_start", success)
    monkeypatch.setattr(
        "uefactory.core.remote.RemoteHost.tmux_status",
        lambda *args, **kwargs: {"status": "complete", "tmux_live": False},
    )
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_stop", success)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.remove_tree", success)
    monkeypatch.setattr("uefactory.render.job._validate_render_output", unexpected_validation)

    with pytest.raises(RuntimeError, match="Remote UE runtime reported failure"):
        render_job_remote(
            settings=settings,
            host="l40s",
            job_path=job_path,
            timeout_sec=60,
            poll_interval_sec=1,
        )

    assert validation_called is False
    manifests = list((project_root / "out/renders").glob("*/builtin_cube/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["error"] == "runtime failed"
    assert manifest["orchestration_error"]["type"] == "RuntimeError"
    assert "Remote UE runtime reported failure" in manifest["orchestration_error"]["message"]


def test_render_job_remote_preserves_runtime_root_cause_when_cleanup_also_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_minimal_ue_project(project_root)

    def fake_run(self: Any, command: str, **kwargs: Any) -> RemoteCommandResult:
        return RemoteCommandResult(
            [],
            0,
            json.dumps({"status": "ok", "mode": "current-user", "run_user": "uef"}),
            "",
            0.01,
        )

    def success(*args: Any, **kwargs: Any) -> RemoteCommandResult:
        return RemoteCommandResult([], 0, "", "", 0.01)

    def fake_pull(
        self: Any,
        remote_sources: list[str | Path],
        local_dest: str | Path,
        **kwargs: Any,
    ) -> RemoteCommandResult:
        Path(local_dest, "manifest.json").write_text(
            json.dumps({"schema_version": 2, "status": "failed", "error": "runtime root"}),
            encoding="utf-8",
        )
        return RemoteCommandResult([], 0, "", "", 0.01)

    def failed_remove(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("remote filesystem unavailable")

    monkeypatch.setattr("uefactory.core.remote.RemoteHost.run", fake_run)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.rsync_push", success)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.rsync_pull", fake_pull)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_start", success)
    monkeypatch.setattr(
        "uefactory.core.remote.RemoteHost.tmux_status",
        lambda *args, **kwargs: {"status": "complete", "tmux_live": False},
    )
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_stop", success)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.remove_tree", failed_remove)

    with pytest.raises(RuntimeError, match="Remote UE runtime reported failure"):
        render_job_remote(
            settings=_remote_settings(project_root),
            host="l40s",
            job_path=_write_render_job(project_root),
            timeout_sec=60,
            poll_interval_sec=1,
        )

    manifest = json.loads(
        next((project_root / "out/renders").glob("*/builtin_cube/manifest.json")).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["error"] == "runtime root"
    assert manifest["orchestration_error"]["type"] == "RuntimeError"
    assert manifest["cleanup_error"] == {
        "type": "RuntimeError",
        "message": "remote filesystem unavailable",
    }


def test_remote_runner_preserves_failed_runtime_manifest(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "work"
    engine_dir = tmp_path / "engine"
    project_dir = tmp_path / "project"
    run_dir = work_dir / "jobs/render_l40s_test"
    output_dir = run_dir / "out/builtin_cube"
    executable = engine_dir / "Engine/Binaries/Linux/UnrealEditor-Cmd"
    version_path = engine_dir / "Engine/Build/Build.version"
    executable.parent.mkdir(parents=True)
    version_path.parent.mkdir(parents=True)
    project_dir.joinpath("Content/Python").mkdir(parents=True)
    run_dir.mkdir(parents=True)
    work_dir.joinpath(".uef_node").write_text(
        json.dumps({"host": "l40s", "work_dir": str(work_dir)}),
        encoding="utf-8",
    )
    version_path.write_text(json.dumps({"MajorVersion": 5}), encoding="utf-8")
    project_dir.joinpath("UEFBase.uproject").write_text("{}", encoding="utf-8")
    project_dir.joinpath("Content/Python/uef_render_job.py").write_text(
        "print('setup')\n", encoding="utf-8"
    )
    project_dir.joinpath("Content/Python/uef_render_job_runtime.py").write_text(
        "print('runtime')\n", encoding="utf-8"
    )
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if not any(arg.startswith('-ExecutePythonScript=') for arg in sys.argv[1:]):\n"
        "    job = json.loads(Path(os.environ['UEF_JOB_FILE']).read_text())\n"
        "    out_dir = Path(job['out_dir'])\n"
        "    pass_dir = out_dir / 'beauty_lit'\n"
        "    pass_dir.mkdir(parents=True, exist_ok=True)\n"
        "    for index in range(job['frames']):\n"
        "        (pass_dir / f'frame_{index:04d}.png').write_bytes(b'frame')\n"
        "    (out_dir / 'manifest.json').write_text(\n"
        "        json.dumps({'schema_version': 2, 'status': 'failed', "
        "'error': 'runtime failed'}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    run_dir.joinpath("job.json").write_text(
        json.dumps(
            {
                "camera": {"resolution": [64, 40]},
                "frames": 2,
                "job": {"job": "render"},
                "map_path": "/Game/Test/Map",
                "out_dir": str(output_dir),
                "passes": ["beauty_lit"],
                "sequence_path": "/Game/Test/Sequence.Sequence",
            }
        ),
        encoding="utf-8",
    )
    for key, value in {
        "UEF_HOST_NAME": "l40s",
        "UEF_JOB_ID": "render_l40s_test",
        "UEF_WORK_DIR": str(work_dir),
        "UEF_ENGINE_DIR": str(engine_dir),
        "UEF_REMOTE_RUN_DIR": str(run_dir),
        "UEF_REMOTE_PROJECT_DIR": str(project_dir),
        "UEF_REMOTE_RUN_USER": "",
        "UEF_TIMEOUT_SEC": "10",
    }.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match="runtime failed"):
        exec(_REMOTE_RENDER_JOB_SCRIPT, {})

    manifest = json.loads(output_dir.joinpath("manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["error"] == "runtime failed"
    assert manifest["remote_runner_error"] == "UE runtime manifest status is 'failed'"


def test_wait_for_remote_render_job_gives_setup_and_render_separate_budgets(
    monkeypatch: Any,
) -> None:
    clock = [0.0]
    statuses = iter(
        [
            {
                "status": "running",
                "phase": "setup",
                "updated_utc": "2026-07-10T00:00:00Z",
                "tmux_live": True,
            },
            {
                "status": "running",
                "phase": "render",
                "updated_utc": "2026-07-10T00:00:06Z",
                "tmux_live": True,
            },
            {
                "status": "running",
                "phase": "render",
                "updated_utc": "2026-07-10T00:00:12Z",
                "tmux_live": True,
            },
            {
                "status": "complete",
                "phase": "rendered",
                "updated_utc": "2026-07-10T00:00:18Z",
                "tmux_live": False,
            },
        ]
    )

    class FakeRemote:
        def tmux_status(self, job_id: str, *, timeout_sec: int) -> dict[str, Any]:
            return next(statuses)

    monkeypatch.setattr("uefactory.render.job.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "uefactory.render.job.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    status = _wait_for_remote_render_job(
        cast(Any, FakeRemote()),
        "job-1",
        timeout_sec=10,
        poll_interval_sec=6,
    )

    assert clock[0] == 18
    assert status["status"] == "complete"


def test_wait_for_remote_render_job_fails_immediately_if_tmux_dies_nonterminal() -> None:
    class FakeRemote:
        def tmux_status(self, job_id: str, *, timeout_sec: int) -> dict[str, Any]:
            return {"status": "running", "phase": "render", "tmux_live": False}

    with pytest.raises(RuntimeError, match="tmux exited before a terminal status"):
        _wait_for_remote_render_job(
            cast(Any, FakeRemote()),
            "job-1",
            timeout_sec=60,
            poll_interval_sec=1,
        )


def test_wait_for_remote_render_job_detects_stale_heartbeat(monkeypatch: Any) -> None:
    clock = [0.0]

    class FakeRemote:
        def tmux_status(self, job_id: str, *, timeout_sec: int) -> dict[str, Any]:
            return {
                "status": "running",
                "phase": "render",
                "updated_utc": "2026-07-10T00:00:00Z",
                "elapsed_sec": 0,
                "tmux_live": True,
            }

    monkeypatch.setattr("uefactory.render.job.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "uefactory.render.job.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    with pytest.raises(TimeoutError, match="stopped heartbeating"):
        _wait_for_remote_render_job(
            cast(Any, FakeRemote()),
            "job-1",
            timeout_sec=600,
            poll_interval_sec=30,
        )


def test_remote_runner_signal_kills_active_ue_group_and_records_pid(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    engine_dir = tmp_path / "engine"
    project_dir = tmp_path / "project"
    job_id = "render_l40s_signal"
    run_dir = work_dir / "jobs" / job_id
    output_dir = run_dir / "out/builtin_cube"
    executable = engine_dir / "Engine/Binaries/Linux/UnrealEditor-Cmd"
    executable.parent.mkdir(parents=True)
    engine_dir.joinpath("Engine/Build").mkdir(parents=True)
    project_dir.joinpath("Content/Python").mkdir(parents=True)
    run_dir.mkdir(parents=True)
    work_dir.joinpath(".uef_node").write_text(
        json.dumps({"host": "l40s", "work_dir": str(work_dir)}),
        encoding="utf-8",
    )
    engine_dir.joinpath("Engine/Build/Build.version").write_text(
        json.dumps({"MajorVersion": 5}),
        encoding="utf-8",
    )
    project_dir.joinpath("UEFBase.uproject").write_text("{}", encoding="utf-8")
    project_dir.joinpath("Content/Python/uef_render_job.py").write_text(
        "print('setup')\n", encoding="utf-8"
    )
    project_dir.joinpath("Content/Python/uef_render_job_runtime.py").write_text(
        "print('runtime')\n", encoding="utf-8"
    )
    executable.write_text(
        f"#!{sys.executable}\n"
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    run_dir.joinpath("job.json").write_text(
        json.dumps(
            {
                "camera": {"resolution": [64, 40]},
                "frames": 2,
                "job": {"job": "render"},
                "map_path": "/Game/Test/Map",
                "out_dir": str(output_dir),
                "passes": ["beauty_lit"],
                "sequence_path": "/Game/Test/Sequence.Sequence",
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "UEF_HOST_NAME": "l40s",
            "UEF_JOB_ID": job_id,
            "UEF_WORK_DIR": str(work_dir),
            "UEF_ENGINE_DIR": str(engine_dir),
            "UEF_REMOTE_RUN_DIR": str(run_dir),
            "UEF_REMOTE_PROJECT_DIR": str(project_dir),
            "UEF_REMOTE_RUN_USER": "",
            "UEF_TIMEOUT_SEC": "60",
            "UEF_TERMINATE_GRACE_SEC": "0.1",
        }
    )
    runner = subprocess.Popen(
        [sys.executable, "-c", _REMOTE_RENDER_JOB_SCRIPT],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    status_path = run_dir / "status.json"
    active_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if status_path.exists():
                status = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(status.get("pid"), int):
                    active_pid = status["pid"]
                    assert status["pgid"] == active_pid
                    assert isinstance(status["process_start_ticks"], int)
                    break
            time.sleep(0.02)
        assert active_pid is not None, "runner did not publish the active UE pid"

        runner.send_signal(signal.SIGTERM)
        stdout, stderr = runner.communicate(timeout=5)

        assert runner.returncode != 0, (stdout, stderr)
        final_status = json.loads(status_path.read_text(encoding="utf-8"))
        assert final_status["status"] == "failed"
        assert "SIGTERM" in final_status["error"]
        with pytest.raises(ProcessLookupError):
            os.kill(active_pid, 0)
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=5)
        if active_pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(active_pid, signal.SIGKILL)


def test_local_setup_keyboard_interrupt_removes_generated_assets(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_minimal_ue_project(project_root)
    engine_dir = tmp_path / "engine"
    engine_dir.joinpath("Engine/Build").mkdir(parents=True)
    engine_dir.joinpath("Engine/Build/Build.version").write_text(
        json.dumps({"MajorVersion": 5}),
        encoding="utf-8",
    )
    settings = Settings(
        project_root=project_root,
        ue_root=engine_dir,
        ue_home=tmp_path / "ue_home",
        data_dir=project_root / "data",
        log_dir=project_root / "logs",
    )
    generated = project_root / "ue/UEFBase/Content/UEF/RenderJobs/test_run"

    def interrupted_setup(*args: Any, **kwargs: Any) -> None:
        generated.mkdir(parents=True)
        generated.joinpath("partial.uasset").write_bytes(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr("uefactory.render.job._new_run_id", lambda: "test_run")
    monkeypatch.setattr("uefactory.render.job.run_ue", interrupted_setup)

    with pytest.raises(KeyboardInterrupt):
        render_job(
            settings=settings,
            job_path=_write_render_job(project_root),
            timeout_sec=60,
        )

    assert not generated.exists()


def test_render_job_command_rejects_remote_verify_twice(tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    job_path = tmp_path / "job.yaml"
    job_path.write_text("job: render\n", encoding="utf-8")

    with pytest.raises(typer.Exit) as exc_info:
        render_job_command(
            cast(Any, _DummyContext(settings)),
            job_path,
            timeout_sec=60,
            verify_twice=True,
            host="l40s",
        )

    assert exc_info.value.exit_code == 2


class _DummyContext:
    def __init__(self, settings: Settings) -> None:
        self.obj = {"settings": settings}

    def find_root(self) -> _DummyContext:
        return self


def _write_minimal_ue_project(project_root: Path) -> None:
    script_dir = project_root / "ue/UEFBase/Content/Python"
    config_dir = project_root / "ue/UEFBase/Config"
    script_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (project_root / "ue/UEFBase/UEFBase.uproject").write_text("{}", encoding="utf-8")
    (script_dir / "uef_render_job.py").write_text("print('setup')\n", encoding="utf-8")
    (script_dir / "uef_render_job_runtime.py").write_text("print('runtime')\n", encoding="utf-8")
    (config_dir / "DefaultEngine.ini").write_text("[/Script/Engine.Engine]\n", encoding="utf-8")


def _write_render_job(project_root: Path) -> Path:
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
                "  resolution: [128, 72]",
                "lighting:",
                "  preset: three_point",
                "passes: ['beauty_lit']",
                "output:",
                "  dir: out/renders",
            ]
        ),
        encoding="utf-8",
    )
    return job_path


def _remote_settings(project_root: Path) -> Settings:
    return Settings(
        project_root=project_root,
        data_dir=project_root / "data",
        log_dir=project_root / "logs",
        hosts={
            "l40s": HostConfig(
                name="l40s",
                ssh_alias="l40s",
                work_dir=Path("/remote/work"),
                engine_dir=Path("/remote/work/engine"),
            )
        },
    )


def _write_gradient_png(path: Path, *, blue: int) -> None:
    image = Image.new("RGB", (128, 72), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index in range(128):
        draw.line((index, 0, index, 71), fill=(index * 2, 80, blue))
    image.save(path)
