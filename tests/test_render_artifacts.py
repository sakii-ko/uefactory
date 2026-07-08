from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

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

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
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
    assert artifacts.turntable_mp4.read_bytes() == b"mp4"
    html = artifacts.index_html.read_text(encoding="utf-8")
    assert "beauty_lit" in html
    assert "turntable.mp4" in html
