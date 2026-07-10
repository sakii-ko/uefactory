from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import OpenEXR
import pytest
from PIL import Image, ImageDraw

from uefactory.render.passes import (
    assert_object_mask_visibility,
    assert_passes_distinct,
    canonicalize_png_frames,
    validate_render_pass,
)


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
    assert len(result.frames[0].pixel_sha256) == 64
    assert result.resolution == (8, 8)


def test_validate_exr_rejects_float32_rgba(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.exr"
    values = np.tile(np.linspace(0.0, 10.0, 8, dtype=np.float32), (8, 1))
    _write_scalar_exr(frame, values, dtype=np.float32)

    with pytest.raises(RuntimeError, match="expected half-float RGBA EXR pixels"):
        validate_render_pass("depth", [frame], expected_frames=1)


def test_validate_exr_rejects_half_float_rgb_without_alpha(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.exr"
    values = np.ones((8, 8, 3), dtype=np.float16)
    OpenEXR.File(
        {"compression": OpenEXR.ZIP_COMPRESSION},
        {"RGB": values},
    ).write(str(frame))

    with pytest.raises(RuntimeError, match="expected half-float RGBA EXR"):
        validate_render_pass("depth", [frame], expected_frames=1)


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


def test_validate_png_records_decoded_pixel_sha256(tmp_path: Path) -> None:
    first = tmp_path / "frame_0000.png"
    second = tmp_path / "frame_0001.png"
    image = _rgb_gradient_image((16, 8))
    image.save(first, compress_level=0)
    image.save(second, compress_level=9)

    result = validate_render_pass("beauty_lit", [first, second], expected_frames=2)

    expected = hashlib.sha256(image.tobytes()).hexdigest()
    assert result.frames[0].pixel_sha256 == expected
    assert result.frames[1].pixel_sha256 == expected
    assert result.stable_payload()["frames"][0]["pixel_sha256"] == expected


def test_validate_png_rejects_rgba_until_canonicalized(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.png"
    Image.new("RGBA", (8, 8), (120, 80, 40, 255)).save(frame)

    with pytest.raises(RuntimeError, match="expected RGB 8-bit 3-channel PNG"):
        validate_render_pass("beauty_lit", [frame], expected_frames=1)


def test_validate_png_rejects_grayscale_mode(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.png"
    Image.new("L", (8, 8), 128).save(frame)

    with pytest.raises(RuntimeError, match="expected RGB 8-bit 3-channel PNG"):
        validate_render_pass("beauty_lit", [frame], expected_frames=1)


def test_canonicalize_png_frames_discards_alpha_without_compositing(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.png"
    image = Image.new("RGBA", (2, 1))
    image.putdata([(12, 34, 56, 0), (210, 180, 70, 255)])
    image.save(frame)

    canonicalize_png_frames([frame])

    with Image.open(frame) as canonical:
        canonical.load()
        assert canonical.mode == "RGB"
        assert canonical.size == (2, 1)
        assert canonical.getpixel((0, 0)) == (12, 34, 56)
        assert canonical.getpixel((1, 0)) == (210, 180, 70)


def test_validate_png_rejects_declared_resolution_mismatch(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.png"
    _rgb_gradient_image((16, 8)).save(frame)

    with pytest.raises(RuntimeError, match="expected resolution .* got"):
        validate_render_pass(
            "beauty_lit",
            [frame],
            expected_frames=1,
            expected_resolution=(32, 16),
        )


def test_validate_png_rejects_inconsistent_frame_resolutions(tmp_path: Path) -> None:
    first = tmp_path / "frame_0000.png"
    second = tmp_path / "frame_0001.png"
    _rgb_gradient_image((16, 8)).save(first)
    _rgb_gradient_image((20, 8)).save(second)

    with pytest.raises(RuntimeError, match="inconsistent frame resolution"):
        validate_render_pass("beauty_lit", [first, second], expected_frames=2)


def test_validate_png_rejects_repeated_ocio_invalid_style_overlay(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.png"
    image = Image.new("RGB", (640, 360), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.text((128, 128), "OCIO INVALID", fill=(255, 191, 64))
    draw.text((384, 128), "OCIO INVALID", fill=(255, 191, 64))
    image.save(frame)

    with pytest.raises(RuntimeError, match="OCIO INVALID-style overlay"):
        validate_render_pass("beauty_lit", [frame], expected_frames=1)


def test_validate_png_accepts_single_solid_yellow_asset(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.png"
    image = Image.new("RGB", (64, 64), (10, 10, 10))
    ImageDraw.Draw(image).rectangle((20, 20, 43, 43), fill=(255, 191, 64))
    image.save(frame)

    result = validate_render_pass("beauty_lit", [frame], expected_frames=1)

    assert result.frame_count == 1


def test_validate_beauty_lit_none_accepts_small_emissive_region(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.png"
    image = Image.new("RGB", (16, 16), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((7, 7, 8, 8), fill=(255, 191, 64))
    image.save(frame)

    result = validate_render_pass(
        "beauty_lit",
        [frame],
        expected_frames=1,
        lighting_preset="none",
    )

    assert result.frames[0].max == (255.0, 191.0, 64.0)


def test_validate_beauty_lit_none_rejects_no_emissive_highlight(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.png"
    Image.new("RGB", (16, 16), (8, 8, 8)).save(frame)

    with pytest.raises(RuntimeError, match="lacks emissive highlights"):
        validate_render_pass(
            "beauty_lit",
            [frame],
            expected_frames=1,
            lighting_preset="none",
        )


def test_validate_beauty_lit_none_rejects_bright_background(tmp_path: Path) -> None:
    frame = tmp_path / "frame_0000.png"
    image = Image.new("RGB", (16, 16), (96, 96, 96))
    ImageDraw.Draw(image).rectangle((7, 7, 8, 8), fill=(255, 191, 64))
    image.save(frame)

    with pytest.raises(RuntimeError, match="none preset background is too bright"):
        validate_render_pass(
            "beauty_lit",
            [frame],
            expected_frames=1,
            lighting_preset="none",
        )


def test_assert_object_mask_visibility_rejects_edge_touching_bbox(tmp_path: Path) -> None:
    mask = tmp_path / "mask.exr"
    beauty = tmp_path / "beauty.png"
    values = np.zeros((24, 40), dtype=np.float32)
    values[0:10, 12:28] = 1.0 / 255.0
    _write_scalar_exr(mask, values)
    _write_visibility_png(beauty, values)

    with pytest.raises(RuntimeError, match="bbox touches frame margin"):
        assert_object_mask_visibility(pass_frames={"object_mask": [mask], "beauty_lit": [beauty]})


def test_assert_object_mask_visibility_rejects_invisible_object(tmp_path: Path) -> None:
    mask = tmp_path / "mask.exr"
    beauty = tmp_path / "beauty.png"
    values = np.zeros((24, 40), dtype=np.float32)
    values[6:18, 12:28] = 1.0 / 255.0
    values[20:, :] = 2.0 / 255.0
    _write_scalar_exr(mask, values)
    Image.new("RGB", (40, 24), (48, 48, 48)).save(beauty)

    with pytest.raises(RuntimeError, match="not visibly distinct"):
        assert_object_mask_visibility(pass_frames={"object_mask": [mask], "beauty_lit": [beauty]})


def test_assert_object_mask_visibility_accepts_centered_visible_object(tmp_path: Path) -> None:
    mask = tmp_path / "mask.exr"
    beauty = tmp_path / "beauty.png"
    values = np.zeros((24, 40), dtype=np.float32)
    values[6:18, 12:28] = 1.0 / 255.0
    values[20:, :] = 2.0 / 255.0
    _write_scalar_exr(mask, values)
    _write_visibility_png(beauty, values)

    assert_object_mask_visibility(pass_frames={"object_mask": [mask], "beauty_lit": [beauty]})


def _write_rgb_gradient(path: Path) -> None:
    _rgb_gradient_image((8, 8)).save(path)


def _rgb_gradient_image(size: tuple[int, int]) -> Image.Image:
    image = Image.new("RGB", size, (0, 0, 0))
    draw = ImageDraw.Draw(image)
    width, height = size
    for index in range(width):
        draw.line(
            (index, 0, index, height - 1),
            fill=(min(index * 12, 255), 80, 180),
        )
    return image


def _write_scalar_exr(
    path: Path,
    values: np.ndarray,
    *,
    dtype: type[np.float16] | type[np.float32] = np.float16,
) -> None:
    rgba = np.repeat(values[:, :, np.newaxis], 4, axis=2).astype(dtype)
    OpenEXR.File(
        {"compression": OpenEXR.ZIP_COMPRESSION},
        {"RGBA": rgba},
    ).write(str(path))


def _write_rgb_exr(path: Path, values: np.ndarray) -> None:
    alpha = np.ones((*values.shape[:2], 1), dtype=np.float16)
    rgba = np.concatenate((values.astype(np.float16), alpha), axis=2)
    OpenEXR.File(
        {"compression": OpenEXR.ZIP_COMPRESSION},
        {"RGBA": rgba},
    ).write(str(path))


def _write_visibility_png(path: Path, stencil: np.ndarray) -> None:
    image = np.full((*stencil.shape, 3), (12, 18, 24), dtype=np.uint8)
    image[np.isclose(stencil, 1.0 / 255.0, rtol=0.0, atol=5e-5)] = (210, 80, 40)
    Image.fromarray(image, mode="RGB").save(path)
