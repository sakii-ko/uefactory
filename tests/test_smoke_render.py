from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from uefactory.core.config import HostConfig, Settings, load_settings
from uefactory.core.remote import RemoteCommandResult
from uefactory.render.smoke import (
    _REMOTE_SMOKE_PREPARE_SCRIPT,
    _engine_version,
    _validate_image,
    render_smoke,
    render_smoke_remote,
)


def test_validate_image_rejects_black_frame(tmp_path: Path) -> None:
    frame = tmp_path / "black.png"
    Image.new("RGB", (64, 64), (0, 0, 0)).save(frame)

    with pytest.raises(RuntimeError, match="too dark"):
        _validate_image(frame)


def test_validate_image_rejects_uniform_gray_frame(tmp_path: Path) -> None:
    frame = tmp_path / "gray.png"
    Image.new("RGB", (64, 64), (32, 32, 32)).save(frame)

    with pytest.raises(RuntimeError, match="too uniform"):
        _validate_image(frame)


def test_validate_image_accepts_nonuniform_lit_frame(tmp_path: Path) -> None:
    frame = tmp_path / "gradient.png"
    image = Image.new("RGB", (64, 64), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index in range(64):
        shade = int(255 * index / 63)
        draw.line((index, 0, index, 63), fill=(shade, 80, 255 - shade))
    image.save(frame)

    info = _validate_image(frame)

    assert info.mean_luma > 5
    assert info.luma_stddev > 1


def test_engine_version_missing_file_raises(tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path,
        ue_root=tmp_path / "missing-engine",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )

    with pytest.raises(FileNotFoundError, match="Build.version"):
        _engine_version(settings)


def test_remote_smoke_prepare_includes_linux_shader_case_compat_links() -> None:
    assert "Raytracing" in _REMOTE_SMOKE_PREPARE_SCRIPT
    assert "RayTracing" in _REMOTE_SMOKE_PREPARE_SCRIPT
    assert "RaytracingSkylightRGS.usf" in _REMOTE_SMOKE_PREPARE_SCRIPT
    assert "RayTracingSkyLightRGS.usf" in _REMOTE_SMOKE_PREPARE_SCRIPT
    assert "NiagaraStatelessModule_ScaleMeshSizebySpeed.ush" in _REMOTE_SMOKE_PREPARE_SCRIPT
    assert "NiagaraStatelessModule_ScaleMeshSizeBySpeed.ush" in _REMOTE_SMOKE_PREPARE_SCRIPT


def test_remote_smoke_pulls_validated_frame_and_cleans_up(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    script_dir = project_root / "ue/UEFBase/Content/Python"
    config_dir = project_root / "ue/UEFBase/Config"
    script_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (project_root / "ue/UEFBase/UEFBase.uproject").write_text("{}", encoding="utf-8")
    (script_dir / "uef_smoke.py").write_text("print('ok')\n", encoding="utf-8")
    (config_dir / "DefaultEngine.ini").write_text("[/Script/Engine.Engine]\n", encoding="utf-8")
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
            stdout="{}",
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

    statuses = [
        {"status": "running", "tmux_live": True},
        {"status": "complete", "tmux_live": False},
    ]

    def fake_tmux_status(self: Any, job_id: str, *, timeout_sec: int = 60) -> dict[str, Any]:
        calls.append(f"status:{job_id}")
        return statuses.pop(0)

    def fake_tmux_start(
        self: Any,
        job_id: str,
        command: str,
        *,
        timeout_sec: int = 60,
    ) -> Any:
        calls.append(f"tmux:{job_id}")
        return RemoteCommandResult([], 0, "", "", 0.01)

    def fake_rsync_pull(
        self: Any,
        remote_sources: list[str | Path],
        local_dest: str | Path,
        *,
        timeout_sec: int = 3600,
        delete: bool = False,
    ) -> Any:
        calls.append(f"pull:{delete}:{local_dest}")
        frame = Path(local_dest) / "frame_0000.png"
        Image.new("RGB", (64, 64), (0, 0, 0)).save(frame)
        image = Image.open(frame)
        draw = ImageDraw.Draw(image)
        for index in range(64):
            draw.line((index, 0, index, 63), fill=(index * 4, 80, 255 - index * 2))
        image.save(frame)
        (Path(local_dest) / "manifest.json").write_text(
            json.dumps({"status": "ok"}),
            encoding="utf-8",
        )
        (Path(local_dest) / "ue.log").write_text("ok", encoding="utf-8")
        return RemoteCommandResult([], 0, "", "", 0.01)

    def fake_remove_tree(self: Any, remote_path: str | Path, *, timeout_sec: int = 60) -> Any:
        calls.append(f"remove:{remote_path}")
        return RemoteCommandResult([], 0, "", "", 0.01)

    monkeypatch.setattr("time.sleep", lambda seconds: None)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.run", fake_run)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.rsync_push", fake_rsync_push)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_start", fake_tmux_start)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.tmux_status", fake_tmux_status)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.rsync_pull", fake_rsync_pull)
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.remove_tree", fake_remove_tree)

    result = render_smoke_remote(
        settings=settings,
        host="l40s",
        out_root=project_root / "out/smoke",
        timeout_sec=60,
        poll_interval_sec=30,
    )

    assert result.frame_path.exists()
    assert result.mean_luma > 5
    assert any("UEF_REMOTE_RUN_USER=uef" in call for call in calls)
    assert any(call.startswith("tmux:smoke_l40s_") for call in calls)
    assert any(call.startswith("pull:True:") for call in calls)
    assert any(call.startswith("remove:/remote/work/jobs/smoke_l40s_") for call in calls)


@pytest.mark.ue
def test_smoke_render_end_to_end(tmp_path: Path) -> None:
    settings = load_settings()
    result = render_smoke(settings=settings, out_root=tmp_path, timeout_sec=1800)

    assert result.frame_path.exists()
    assert result.manifest_path.exists()
    assert result.mean_luma > 5

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["render_kind"] == "scene"
