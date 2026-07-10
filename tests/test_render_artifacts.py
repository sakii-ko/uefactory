from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from uefactory.render.artifacts import create_render_artifacts


def test_create_render_artifacts_writes_sheet_html_and_video(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    beauty_dir = run_dir / "beauty_lit"
    normal_dir = run_dir / "normal"
    beauty_dir.mkdir(parents=True)
    normal_dir.mkdir()
    for index in range(2):
        Image.new("RGB", (32, 18), (40 + index * 20, 80, 160)).save(
            beauty_dir / f"frame_{index:04d}.png"
        )
        Image.new("RGB", (32, 18), (120, 120 + index * 20, 200)).save(
            normal_dir / f"frame_{index:04d}.png"
        )
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")

    command: list[str] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command.extend(args[0])
        output_path = Path(args[0][-1])
        output_path.write_bytes(b"mp4")
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
    )
    monkeypatch.setattr("subprocess.run", fake_run)

    artifacts = create_render_artifacts(
        run_dir=run_dir,
        frame_paths={
            "beauty_lit": sorted(beauty_dir.glob("frame_*.png")),
            "normal": sorted(normal_dir.glob("frame_*.png")),
        },
        manifest_path=manifest_path,
    )

    assert artifacts.contact_sheet.exists()
    assert artifacts.index_html.exists()
    assert artifacts.turntable_mp4 is not None
    assert artifacts.turntable_mp4.read_bytes() == b"mp4"
    assert artifacts.manifest_payload(run_dir=run_dir) == {
        "contact_sheet": "contact_sheet.png",
        "index_html": "index.html",
        "turntable_mp4": "turntable.mp4",
    }
    html = artifacts.index_html.read_text(encoding="utf-8")
    assert "beauty_lit" in html
    assert "turntable.mp4" in html
    assert str(run_dir) not in html
    assert command[command.index("-stream_loop") + 1] == "-1"
    assert float(command[command.index("-framerate") + 1]) == pytest.approx(0.5)
    output_framerate = int(command[command.index("-r") + 1])
    output_frame_count = int(command[command.index("-frames:v") + 1])
    assert output_framerate == 12
    assert output_frame_count / output_framerate >= 4.0


def test_create_render_artifacts_without_beauty_skips_turntable(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    normal_dir = run_dir / "normal"
    normal_dir.mkdir(parents=True)
    frame = normal_dir / "frame_0000.png"
    Image.new("RGB", (32, 18), (120, 140, 200)).save(frame)
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")

    def unexpected_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("ffmpeg must not run without beauty_lit frames")

    monkeypatch.setattr("subprocess.run", unexpected_run)

    artifacts = create_render_artifacts(
        run_dir=run_dir,
        frame_paths={"normal": [frame]},
        manifest_path=manifest_path,
    )

    assert artifacts.contact_sheet.exists()
    assert artifacts.index_html.exists()
    assert artifacts.turntable_mp4 is None
    assert artifacts.manifest_payload(run_dir=run_dir) == {
        "contact_sheet": "contact_sheet.png",
        "index_html": "index.html",
        "turntable_mp4": None,
    }
    html = artifacts.index_html.read_text(encoding="utf-8")
    assert "Turntable skipped: beauty_lit not rendered." in html
    assert "<video" not in html
    assert 'href="normal/frame_0000.png"' in html
    assert str(run_dir) not in html


def test_create_render_artifacts_reports_ffmpeg_failure_context(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    beauty_dir = run_dir / "beauty_lit"
    beauty_dir.mkdir(parents=True)
    frame = beauty_dir / "frame_0000.png"
    Image.new("RGB", (32, 18), (40, 80, 160)).save(frame)
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 23, stdout="encoder setup", stderr="synthetic codec failure"
        ),
    )

    with pytest.raises(RuntimeError) as error:
        create_render_artifacts(
            run_dir=run_dir,
            frame_paths={"beauty_lit": [frame]},
            manifest_path=manifest_path,
        )

    message = str(error.value)
    assert "turntable.mp4" in message
    assert "exit code 23" in message
    assert "synthetic codec failure" in message
    assert "/usr/bin/ffmpeg" in message
