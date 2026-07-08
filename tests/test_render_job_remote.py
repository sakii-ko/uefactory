from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import typer
from PIL import Image, ImageDraw

from uefactory.cli.render import render_job_command
from uefactory.core.config import HostConfig, Settings
from uefactory.core.remote import RemoteCommandResult
from uefactory.render.job import render_job_remote


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
    assert result.artifacts.turntable_mp4.read_bytes() == b"mp4"
    assert any("UEF_REMOTE_RUN_USER=uef" in call for call in calls)
    assert any(call.startswith("tmux:render_l40s_") for call in calls)
    assert any(call.startswith("pull:True:") for call in calls)
    assert any(call.startswith("remove:/remote/work/jobs/render_l40s_") for call in calls)
    assert any("test ! -e /remote/work/jobs/render_l40s_" in call for call in calls)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["remote_host"] == "l40s"
    assert manifest["local_validation"]["status"] == "ok"
    assert manifest["cleanup"]["status"] == "ok"
    assert manifest["cleanup"]["verified"] is True


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


def _write_gradient_png(path: Path, *, blue: int) -> None:
    image = Image.new("RGB", (64, 40), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index in range(64):
        draw.line((index, 0, index, 39), fill=(index * 3, 80, blue))
    image.save(path)
