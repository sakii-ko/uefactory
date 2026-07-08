from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageChops, ImageStat

PASS_ORDER = (
    "beauty_lit",
    "beauty_unlit",
    "depth",
    "normal",
    "basecolor",
    "object_mask",
)
SUPPORTED_PASSES = frozenset(PASS_ORDER)


@dataclass(frozen=True)
class PassFormat:
    extension: str
    bit_depth: int
    pixel_type: Literal["uint8", "float16"]
    channels: int


PASS_FORMATS: dict[str, PassFormat] = {
    "beauty_lit": PassFormat(extension=".png", bit_depth=8, pixel_type="uint8", channels=3),
    "beauty_unlit": PassFormat(extension=".png", bit_depth=8, pixel_type="uint8", channels=3),
    "depth": PassFormat(extension=".exr", bit_depth=16, pixel_type="float16", channels=4),
    "normal": PassFormat(extension=".png", bit_depth=8, pixel_type="uint8", channels=3),
    "basecolor": PassFormat(extension=".png", bit_depth=8, pixel_type="uint8", channels=3),
    "object_mask": PassFormat(extension=".exr", bit_depth=16, pixel_type="float16", channels=4),
}


@dataclass(frozen=True)
class PassFrameStats:
    frame: str
    mean: tuple[float, ...]
    min: tuple[float, ...]
    max: tuple[float, ...]
    stddev: tuple[float, ...]
    unique_values: int | None = None
    unique_vectors: int | None = None
    scalar_vector: bool | None = None

    def stable_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "frame": self.frame,
            "mean": list(self.mean),
            "min": list(self.min),
            "max": list(self.max),
            "stddev": list(self.stddev),
        }
        if self.unique_values is not None:
            payload["unique_values"] = self.unique_values
        if self.unique_vectors is not None:
            payload["unique_vectors"] = self.unique_vectors
        if self.scalar_vector is not None:
            payload["scalar_vector"] = self.scalar_vector
        return payload


@dataclass(frozen=True)
class PassValidation:
    pass_name: str
    format: PassFormat
    frame_count: int
    frames: tuple[PassFrameStats, ...]

    def stable_payload(self) -> dict[str, Any]:
        return {
            "format": {
                "extension": self.format.extension,
                "bit_depth": self.format.bit_depth,
                "pixel_type": self.format.pixel_type,
                "channels": self.format.channels,
            },
            "frame_count": self.frame_count,
            "frames": [frame.stable_payload() for frame in self.frames],
        }


def validate_render_pass(
    pass_name: str,
    frame_paths: list[Path],
    *,
    expected_frames: int,
    lighting_preset: str = "three_point",
) -> PassValidation:
    if pass_name not in SUPPORTED_PASSES:
        raise ValueError(f"Unsupported render pass: {pass_name}")
    if len(frame_paths) != expected_frames:
        raise RuntimeError(
            f"{pass_name}: expected {expected_frames} frames, found {len(frame_paths)}"
        )
    fmt = PASS_FORMATS[pass_name]
    bad_extension = [path for path in frame_paths if path.suffix.lower() != fmt.extension]
    if bad_extension:
        raise RuntimeError(
            f"{pass_name}: expected {fmt.extension} frames, got {bad_extension[0].name}"
        )

    frames = tuple(_frame_stats(pass_name, path) for path in frame_paths)
    _assert_pass_quality(pass_name, frames, lighting_preset=lighting_preset)
    return PassValidation(
        pass_name=pass_name,
        format=fmt,
        frame_count=len(frame_paths),
        frames=frames,
    )


def assert_passes_distinct(
    *,
    pass_frames: dict[str, list[Path]],
    first_pass: str,
    second_pass: str,
) -> None:
    left_paths = pass_frames.get(first_pass, [])
    right_paths = pass_frames.get(second_pass, [])
    if len(left_paths) != len(right_paths):
        raise RuntimeError(
            f"{first_pass}/{second_pass}: frame count mismatch "
            f"{len(left_paths)} != {len(right_paths)}"
        )
    identical = 0
    for left_path, right_path in zip(left_paths, right_paths, strict=True):
        with Image.open(left_path) as left_image, Image.open(right_path) as right_image:
            diff = ImageChops.difference(
                left_image.convert("RGB"),
                right_image.convert("RGB"),
            )
            if diff.getbbox() is None:
                identical += 1
    if identical == len(left_paths):
        raise RuntimeError(f"{first_pass} and {second_pass} are pixel-identical")


def stable_validation_payload(
    validations: dict[str, PassValidation],
) -> dict[str, Any]:
    return {name: validations[name].stable_payload() for name in sorted(validations)}


def _frame_stats(pass_name: str, frame_path: Path) -> PassFrameStats:
    if PASS_FORMATS[pass_name].extension == ".exr":
        return _exr_frame_stats(pass_name, frame_path)
    return _png_frame_stats(frame_path)


def _png_frame_stats(frame_path: Path) -> PassFrameStats:
    with Image.open(frame_path) as image:
        image.load()
        rgb = image.convert("RGB")
        stat = ImageStat.Stat(rgb)
    return PassFrameStats(
        frame=frame_path.name,
        mean=_rounded_tuple(stat.mean),
        min=tuple(float(value[0]) for value in stat.extrema),
        max=tuple(float(value[1]) for value in stat.extrema),
        stddev=_rounded_tuple(stat.stddev),
    )


def _exr_frame_stats(pass_name: str, frame_path: Path) -> PassFrameStats:
    OpenEXR = import_module("OpenEXR")
    file = OpenEXR.File(str(frame_path), separate_channels=False)
    channels = file.channels()
    pixels = next(iter(channels.values())).pixels
    np = import_module("numpy")
    raw_values = np.asarray(pixels, dtype=np.float32)
    if pass_name == "object_mask" and raw_values.ndim == 3:
        finite_pixels = raw_values[np.all(np.isfinite(raw_values), axis=2)]
        if finite_pixels.size == 0:
            raise RuntimeError(f"{pass_name}: {frame_path} contains no finite pixels")
        rounded_vectors = np.round(finite_pixels, 5)
        if rounded_vectors.shape[1] >= 3:
            unique_vectors = int(np.unique(rounded_vectors[:, :3], axis=0).shape[0])
            channel_spread = np.max(rounded_vectors[:, :3], axis=1) - np.min(
                rounded_vectors[:, :3], axis=1
            )
            scalar_vector = float(np.max(channel_spread)) <= 1e-4
            values = finite_pixels[:, 0]
        else:
            unique_vectors = int(np.unique(rounded_vectors, axis=0).shape[0])
            scalar_vector = True
            values = finite_pixels[:, 0]
    else:
        values = raw_values[:, :, 0] if raw_values.ndim == 3 else raw_values
        unique_vectors = None
        scalar_vector = None
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise RuntimeError(f"{pass_name}: {frame_path} contains no finite pixels")
    rounded = np.round(values, 5)
    return PassFrameStats(
        frame=frame_path.name,
        mean=(round(float(np.mean(values)), 5),),
        min=(round(float(np.min(values)), 5),),
        max=(round(float(np.max(values)), 5),),
        stddev=(round(float(np.std(values)), 5),),
        unique_values=int(np.unique(rounded).size),
        unique_vectors=unique_vectors,
        scalar_vector=scalar_vector,
    )


def _assert_pass_quality(
    pass_name: str,
    frames: tuple[PassFrameStats, ...],
    *,
    lighting_preset: str,
) -> None:
    if pass_name in {"beauty_lit", "beauty_unlit", "basecolor"}:
        _assert_rgb_non_dark_non_uniform(pass_name, frames, lighting_preset=lighting_preset)
    elif pass_name == "depth":
        _assert_depth_gradient(frames)
    elif pass_name == "normal":
        _assert_normal_reasonable(frames)
    elif pass_name == "object_mask":
        _assert_object_mask_values(frames)


def _assert_rgb_non_dark_non_uniform(
    pass_name: str,
    frames: tuple[PassFrameStats, ...],
    *,
    lighting_preset: str,
) -> None:
    for frame in frames:
        mean_luma = 0.2126 * frame.mean[0] + 0.7152 * frame.mean[1] + 0.0722 * frame.mean[2]
        if lighting_preset == "none" and pass_name == "beauty_lit":
            if max(frame.max) <= 32:
                raise RuntimeError(f"{pass_name}: {frame.frame} lacks emissive highlights")
        elif mean_luma <= 5:
            raise RuntimeError(f"{pass_name}: {frame.frame} is too dark mean_luma={mean_luma:.3f}")
        if max(frame.stddev) <= 1:
            raise RuntimeError(f"{pass_name}: {frame.frame} is too uniform stddev={frame.stddev}")


def _assert_depth_gradient(frames: tuple[PassFrameStats, ...]) -> None:
    for frame in frames:
        value_range = frame.max[0] - frame.min[0]
        if value_range <= 1e-3 or frame.stddev[0] <= 1e-4:
            raise RuntimeError(
                f"depth: {frame.frame} lacks gradient "
                f"range={value_range:.5f} stddev={frame.stddev[0]:.5f}"
            )


def _assert_normal_reasonable(frames: tuple[PassFrameStats, ...]) -> None:
    channel_span = tuple(
        max(frame.mean[index] for frame in frames) - min(frame.mean[index] for frame in frames)
        for index in range(3)
    )
    if max(channel_span) <= 25:
        raise RuntimeError(f"normal: expected orbit-varying channel means, got span={channel_span}")
    for frame in frames:
        if not 64 <= frame.mean[2] <= 192:
            raise RuntimeError(f"normal: {frame.frame} has implausible blue mean {frame.mean[2]}")
        if max(frame.stddev) <= 1 and max(frame.max) - min(frame.min) <= 5:
            raise RuntimeError(f"normal: {frame.frame} is too uniform stddev={frame.stddev}")


def _assert_object_mask_values(frames: tuple[PassFrameStats, ...]) -> None:
    for frame in frames:
        if frame.scalar_vector is False:
            raise RuntimeError(
                f"object_mask: {frame.frame} expected scalar stencil IDs, "
                f"got {frame.unique_vectors} unique color vectors"
            )
        if frame.unique_values != 3:
            raise RuntimeError(
                f"object_mask: {frame.frame} expected 3 unique values "
                f"(background + 2 objects), got {frame.unique_values}"
            )
        if frame.min[0] < -1e-5:
            raise RuntimeError(f"object_mask: {frame.frame} has negative value {frame.min[0]}")
        if frame.max[0] > (2.0 / 255.0) + 1e-4:
            raise RuntimeError(
                f"object_mask: {frame.frame} max value is not a stencil id {frame.max[0]}"
            )


def _rounded_tuple(values: list[float]) -> tuple[float, ...]:
    return tuple(round(float(value), 3) for value in values)
