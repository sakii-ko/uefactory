from __future__ import annotations

from pathlib import Path

import numpy as np
import OpenEXR
import pytest
from PIL import Image, ImageDraw

from uefactory.render.passes import assert_passes_distinct, validate_render_pass


def test_validate_depth_rejects_constant_exr(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.exr"
    _write_scalar_exr(frame, np.ones((8, 8), dtype=np.float32))

    with pytest.raises(RuntimeError, match="depth: .* lacks gradient"):
        validate_render_pass("depth", [frame], expected_frames=1)


def test_validate_depth_accepts_gradient_exr(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.exr"
    values = np.tile(np.linspace(0.0, 10.0, 8, dtype=np.float32), (8, 1))
    _write_scalar_exr(frame, values)

    result = validate_render_pass("depth", [frame], expected_frames=1)

    assert result.frames[0].max[0] > result.frames[0].min[0]


def test_validate_normal_rejects_non_varying_frames(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.png"
    Image.new("RGB", (8, 8), (128, 128, 128)).save(frame)

    with pytest.raises(RuntimeError, match="normal: expected orbit-varying"):
        validate_render_pass("normal", [frame], expected_frames=1)


def test_validate_object_mask_rejects_wrong_unique_count(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.exr"
    values = np.zeros((8, 8), dtype=np.float32)
    values[:, 4:] = 1.0
    _write_scalar_exr(frame, values)

    with pytest.raises(RuntimeError, match="object_mask: .* expected 3 unique values"):
        validate_render_pass("object_mask", [frame], expected_frames=1)


def test_validate_object_mask_accepts_background_and_two_objects(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.exr"
    values = np.zeros((8, 8), dtype=np.float32)
    values[1:4, 1:4] = 1.0 / 255.0
    values[4:7, 4:7] = 2.0 / 255.0
    _write_scalar_exr(frame, values)

    result = validate_render_pass("object_mask", [frame], expected_frames=1)

    assert result.frames[0].unique_values == 3


def test_validate_object_mask_rejects_palette_vectors(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.exr"
    values = np.zeros((8, 8, 3), dtype=np.float32)
    values[1:4, 1:4, :] = (0.03434, 0.60383, 0.65141)
    values[4:7, 4:7, :] = (0.55834, 0.43966, 0.00605)
    _write_rgb_exr(frame, values)

    with pytest.raises(RuntimeError, match="expected scalar stencil IDs"):
        validate_render_pass("object_mask", [frame], expected_frames=1)


def test_assert_passes_distinct_rejects_pixel_identical_pngs(tmp_path: Path) -> None:
    left = tmp_path / "lit.png"
    right = tmp_path / "unlit.png"
    _write_rgb_gradient(left)
    _write_rgb_gradient(right)

    with pytest.raises(RuntimeError, match="pixel-identical"):
        assert_passes_distinct(
            pass_frames={"beauty_lit": [left], "beauty_unlit": [right]},
            first_pass="beauty_lit",
            second_pass="beauty_unlit",
        )


def _write_rgb_gradient(path: Path) -> None:
    image = Image.new("RGB", (8, 8), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index in range(8):
        draw.line((index, 0, index, 7), fill=(index * 20, 80, 180))
    image.save(path)


def _write_scalar_exr(path: Path, values: np.ndarray) -> None:
    OpenEXR.File(
        {"compression": OpenEXR.ZIP_COMPRESSION},
        {"R": values.astype(np.float32)},
    ).write(str(path))


def _write_rgb_exr(path: Path, values: np.ndarray) -> None:
    OpenEXR.File(
        {"compression": OpenEXR.ZIP_COMPRESSION},
        {"RGB": values.astype(np.float32)},
    ).write(str(path))
