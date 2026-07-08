from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from uefactory.core.config import load_settings
from uefactory.render.smoke import _validate_image, render_smoke


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


@pytest.mark.ue
def test_smoke_render_end_to_end(tmp_path: Path) -> None:
    settings = load_settings()
    result = render_smoke(settings=settings, out_root=tmp_path, timeout_sec=1800)

    assert result.frame_path.exists()
    assert result.manifest_path.exists()
    assert result.mean_luma > 5
