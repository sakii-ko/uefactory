from __future__ import annotations

import hashlib
import os
import struct
import tempfile
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
    pixel_sha256: str
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
            "pixel_sha256": self.pixel_sha256,
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
    resolution: tuple[int, int]
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
            "resolution": list(self.resolution),
            "frame_count": self.frame_count,
            "frames": [frame.stable_payload() for frame in self.frames],
        }


@dataclass(frozen=True)
class _InspectedFrame:
    stats: PassFrameStats
    resolution: tuple[int, int]


def validate_render_pass(
    pass_name: str,
    frame_paths: list[Path],
    *,
    expected_frames: int,
    expected_resolution: tuple[int, int] | None = None,
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

    inspected = tuple(_frame_stats(pass_name, path) for path in frame_paths)
    if not inspected:
        raise RuntimeError(f"{pass_name}: expected at least one frame")
    resolution = inspected[0].resolution
    for frame in inspected:
        if frame.resolution != resolution:
            raise RuntimeError(
                f"{pass_name}: inconsistent frame resolution; expected {resolution}, "
                f"got {frame.resolution} for {frame.stats.frame}"
            )
    if expected_resolution is not None and resolution != expected_resolution:
        raise RuntimeError(
            f"{pass_name}: expected resolution {expected_resolution}, got {resolution}"
        )

    frames = tuple(frame.stats for frame in inspected)
    _assert_pass_quality(pass_name, frames, lighting_preset=lighting_preset)
    return PassValidation(
        pass_name=pass_name,
        format=fmt,
        resolution=resolution,
        frame_count=len(frame_paths),
        frames=frames,
    )


def canonicalize_png_frames(frame_paths: list[Path]) -> None:
    """Atomically discard UE's alpha channel while preserving the RGB samples."""
    for frame_path in frame_paths:
        if frame_path.suffix.lower() != ".png":
            raise RuntimeError(f"Expected a .png frame, got {frame_path.name}")
        with Image.open(frame_path) as image:
            image.load()
            if image.format != "PNG":
                raise RuntimeError(f"Expected a PNG file, got {image.format} for {frame_path.name}")
            if image.mode == "RGB":
                continue
            if image.mode != "RGBA":
                raise RuntimeError(
                    f"Expected RGB or RGBA PNG for canonicalization, "
                    f"got {image.mode} for {frame_path.name}"
                )
            rgb = image.convert("RGB")

        original_mode = frame_path.stat().st_mode & 0o777
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{frame_path.stem}.",
                suffix=".png",
                dir=frame_path.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
            rgb.save(temporary_path, format="PNG", optimize=False, compress_level=6)
            os.chmod(temporary_path, original_mode)
            os.replace(temporary_path, frame_path)
        finally:
            rgb.close()
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


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


def assert_object_mask_visibility(
    *,
    pass_frames: dict[str, list[Path]],
    margin_fraction: float = 0.03,
) -> None:
    """Check that stencil object 1 is framed and visible in aligned RGB passes."""
    if not 0 <= margin_fraction < 0.5:
        raise ValueError("margin_fraction must be in [0, 0.5)")
    mask_paths = pass_frames.get("object_mask")
    if not mask_paths:
        raise RuntimeError("object_mask: no frames available for visibility validation")

    rgb_passes = {
        pass_name: paths
        for pass_name, paths in pass_frames.items()
        if pass_name in PASS_FORMATS and PASS_FORMATS[pass_name].extension == ".png"
    }
    for pass_name, paths in rgb_passes.items():
        if len(paths) != len(mask_paths):
            raise RuntimeError(
                f"object_mask/{pass_name}: frame count mismatch {len(mask_paths)} != {len(paths)}"
            )

    np = import_module("numpy")
    stencil_one = np.float32(1.0 / 255.0)
    for frame_index, mask_path in enumerate(mask_paths):
        mask_pixels, resolution = _read_half_rgba_exr("object_mask", mask_path)
        object_region = np.isclose(
            mask_pixels[:, :, 0].astype(np.float32),
            stencil_one,
            rtol=0.0,
            atol=5e-5,
        )
        coordinates = np.argwhere(object_region)
        if coordinates.size == 0:
            raise RuntimeError(f"object_mask: {mask_path.name} contains no stencil ID 1")

        height, width = object_region.shape
        y_min, x_min = (int(value) for value in coordinates.min(axis=0))
        y_max, x_max = (int(value) for value in coordinates.max(axis=0))
        horizontal_margin = int(np.ceil(width * margin_fraction))
        vertical_margin = int(np.ceil(height * margin_fraction))
        edge_distances = (x_min, width - 1 - x_max, y_min, height - 1 - y_max)
        if (
            edge_distances[0] < horizontal_margin
            or edge_distances[1] < horizontal_margin
            or edge_distances[2] < vertical_margin
            or edge_distances[3] < vertical_margin
        ):
            raise RuntimeError(
                f"object_mask: {mask_path.name} stencil ID 1 bbox touches frame margin; "
                f"bbox=({x_min}, {y_min}, {x_max}, {y_max}) resolution={resolution}"
            )

        for pass_name, paths in rgb_passes.items():
            rgb_path = paths[frame_index]
            with Image.open(rgb_path) as image:
                image.load()
                if image.mode != "RGB":
                    raise RuntimeError(
                        f"{pass_name}: {rgb_path.name} expected RGB for visibility validation, "
                        f"got {image.mode}"
                    )
                if image.size != resolution:
                    raise RuntimeError(
                        f"object_mask/{pass_name}: resolution mismatch "
                        f"{resolution} != {image.size} for {rgb_path.name}"
                    )
                rgb_pixels = np.asarray(image, dtype=np.float32)
            horizontal_edges = object_region[:, 1:] != object_region[:, :-1]
            vertical_edges = object_region[1:, :] != object_region[:-1, :]
            horizontal_contrast = np.max(np.abs(rgb_pixels[:, 1:] - rgb_pixels[:, :-1]), axis=2)[
                horizontal_edges
            ]
            vertical_contrast = np.max(np.abs(rgb_pixels[1:, :] - rgb_pixels[:-1, :]), axis=2)[
                vertical_edges
            ]
            boundary_contrast = np.concatenate((horizontal_contrast, vertical_contrast))
            contrast = float(np.percentile(boundary_contrast, 95))
            if contrast < 1.0:
                raise RuntimeError(
                    f"{pass_name}: {rgb_path.name} stencil ID 1 is not visibly distinct "
                    f"from adjacent non-object pixels; p95_boundary_contrast={contrast:.3f}"
                )


def stable_validation_payload(
    validations: dict[str, PassValidation],
) -> dict[str, Any]:
    return {name: validations[name].stable_payload() for name in sorted(validations)}


def _frame_stats(pass_name: str, frame_path: Path) -> _InspectedFrame:
    if PASS_FORMATS[pass_name].extension == ".exr":
        return _exr_frame_stats(pass_name, frame_path)
    return _png_frame_stats(pass_name, frame_path)


def _png_frame_stats(pass_name: str, frame_path: Path) -> _InspectedFrame:
    header_resolution, bit_depth, channel_count = _png_header(frame_path)
    with Image.open(frame_path) as image:
        image.load()
        if image.format != "PNG":
            raise RuntimeError(f"{pass_name}: {frame_path.name} is not a PNG file")
        if image.size != header_resolution:
            raise RuntimeError(
                f"{pass_name}: {frame_path.name} PNG header/image resolution mismatch "
                f"{header_resolution} != {image.size}"
            )
        if image.mode != "RGB" or bit_depth != 8 or channel_count != 3:
            raise RuntimeError(
                f"{pass_name}: {frame_path.name} expected RGB 8-bit 3-channel PNG, "
                f"got mode={image.mode} bit_depth={bit_depth} channels={channel_count}"
            )
        if image.getbands() != ("R", "G", "B"):
            raise RuntimeError(
                f"{pass_name}: {frame_path.name} expected RGB bands, got {image.getbands()}"
            )
        if _has_repeated_ocio_invalid_overlay(image):
            raise RuntimeError(
                f"{pass_name}: {frame_path.name} contains repeated yellow "
                "OCIO INVALID-style overlay"
            )
        pixel_bytes = image.tobytes()
        stat = ImageStat.Stat(image)
    return _InspectedFrame(
        stats=PassFrameStats(
            frame=frame_path.name,
            pixel_sha256=hashlib.sha256(pixel_bytes).hexdigest(),
            mean=_rounded_tuple(stat.mean),
            min=tuple(float(value[0]) for value in stat.extrema),
            max=tuple(float(value[1]) for value in stat.extrema),
            stddev=_rounded_tuple(stat.stddev),
        ),
        resolution=header_resolution,
    )


def _exr_frame_stats(pass_name: str, frame_path: Path) -> _InspectedFrame:
    pixels, resolution = _read_half_rgba_exr(pass_name, frame_path)
    np = import_module("numpy")
    pixel_sha256 = hashlib.sha256(np.ascontiguousarray(pixels).tobytes()).hexdigest()
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
    return _InspectedFrame(
        stats=PassFrameStats(
            frame=frame_path.name,
            pixel_sha256=pixel_sha256,
            mean=(round(float(np.mean(values)), 5),),
            min=(round(float(np.min(values)), 5),),
            max=(round(float(np.max(values)), 5),),
            stddev=(round(float(np.std(values)), 5),),
            unique_values=int(np.unique(rounded).size),
            unique_vectors=unique_vectors,
            scalar_vector=scalar_vector,
        ),
        resolution=resolution,
    )


def _png_header(frame_path: Path) -> tuple[tuple[int, int], int, int]:
    with frame_path.open("rb") as stream:
        header = stream.read(29)
    if len(header) != 29 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"{frame_path.name}: invalid PNG signature/header")
    chunk_length = struct.unpack(">I", header[8:12])[0]
    if chunk_length != 13 or header[12:16] != b"IHDR":
        raise RuntimeError(f"{frame_path.name}: PNG does not start with a valid IHDR chunk")
    width, height, bit_depth, color_type = struct.unpack(">IIBB", header[16:26])
    channels_by_color_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    channel_count = channels_by_color_type.get(color_type)
    if channel_count is None:
        raise RuntimeError(f"{frame_path.name}: unsupported PNG color type {color_type}")
    return (width, height), bit_depth, channel_count


def _read_half_rgba_exr(pass_name: str, frame_path: Path) -> tuple[Any, tuple[int, int]]:
    OpenEXR = import_module("OpenEXR")
    np = import_module("numpy")
    try:
        file = OpenEXR.File(str(frame_path), separate_channels=True)
        header = file.header()
        channels = file.channels()
    except Exception as exc:
        raise RuntimeError(f"{pass_name}: failed to read EXR {frame_path.name}: {exc}") from exc

    header_channels = header.get("channels")
    channel_names = [channel.name for channel in header_channels or []]
    if set(channel_names) != set("RGBA") or len(channel_names) != 4:
        raise RuntimeError(
            f"{pass_name}: {frame_path.name} expected half-float RGBA EXR, "
            f"got channels={channel_names}"
        )
    if any(channel.xSampling != 1 or channel.ySampling != 1 for channel in header_channels):
        raise RuntimeError(f"{pass_name}: {frame_path.name} expected full-resolution EXR channels")
    if set(channels) != set("RGBA"):
        raise RuntimeError(
            f"{pass_name}: {frame_path.name} EXR header/pixel channel mismatch; "
            f"got {sorted(channels)}"
        )

    arrays = [np.asarray(channels[name].pixels) for name in "RGBA"]
    shapes = {array.shape for array in arrays}
    dtypes = {array.dtype for array in arrays}
    if len(shapes) != 1 or any(array.ndim != 2 for array in arrays):
        raise RuntimeError(
            f"{pass_name}: {frame_path.name} expected four aligned 2D EXR channels, "
            f"got shapes={sorted(str(shape) for shape in shapes)}"
        )
    if dtypes != {np.dtype(np.float16)}:
        raise RuntimeError(
            f"{pass_name}: {frame_path.name} expected half-float RGBA EXR pixels, "
            f"got dtypes={sorted(str(dtype) for dtype in dtypes)}"
        )

    data_window = header.get("dataWindow")
    if not isinstance(data_window, tuple) or len(data_window) != 2:
        raise RuntimeError(f"{pass_name}: {frame_path.name} has invalid EXR dataWindow")
    minimum, maximum = data_window
    width = int(maximum[0]) - int(minimum[0]) + 1
    height = int(maximum[1]) - int(minimum[1]) + 1
    resolution = (width, height)
    if next(iter(shapes)) != (height, width):
        raise RuntimeError(
            f"{pass_name}: {frame_path.name} EXR header/pixel resolution mismatch; "
            f"dataWindow={resolution} pixels={next(iter(shapes))[::-1]}"
        )
    return np.stack(arrays, axis=2), resolution


def _has_repeated_ocio_invalid_overlay(image: Image.Image) -> bool:
    np = import_module("numpy")
    pixels = np.asarray(image)
    yellow = (
        (pixels[:, :, 0] >= 220)
        & (pixels[:, :, 1] >= 150)
        & (pixels[:, :, 1] <= 220)
        & (pixels[:, :, 2] <= 100)
        & (pixels[:, :, 0] - pixels[:, :, 1] >= 20)
        & (pixels[:, :, 1] - pixels[:, :, 2] >= 60)
    )
    yellow_count = int(np.count_nonzero(yellow))
    if yellow_count < 48 or yellow_count > 50_000 or yellow_count > yellow.size * 0.05:
        return False

    remaining = {(int(y), int(x)) for y, x in np.argwhere(yellow)}
    components: list[tuple[int, int, int, Any]] = []
    while remaining:
        first = remaining.pop()
        stack = [first]
        points = [first]
        while stack:
            y, x = stack.pop()
            for y_offset in (-1, 0, 1):
                for x_offset in (-1, 0, 1):
                    if y_offset == 0 and x_offset == 0:
                        continue
                    neighbor = (y + y_offset, x + x_offset)
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        stack.append(neighbor)
                        points.append(neighbor)
        if len(points) < 3:
            continue
        y_min = min(point[0] for point in points)
        x_min = min(point[1] for point in points)
        normalized = frozenset((y - y_min, x - x_min) for y, x in points)
        components.append((x_min, y_min, len(points), normalized))

    by_shape: dict[Any, list[tuple[int, int, int]]] = {}
    for x, y, size, shape in components:
        by_shape.setdefault(shape, []).append((x, y, size))

    offset_support: dict[tuple[int, int], tuple[int, int]] = {}
    minimum_x_offset = max(16, image.width // 8)
    minimum_y_offset = max(16, image.height // 8)
    for matches in by_shape.values():
        for index, left in enumerate(matches):
            for right in matches[index + 1 :]:
                offset = (right[0] - left[0], right[1] - left[1])
                if abs(offset[0]) < minimum_x_offset and abs(offset[1]) < minimum_y_offset:
                    continue
                component_count, pixel_count = offset_support.get(offset, (0, 0))
                offset_support[offset] = (
                    component_count + 1,
                    pixel_count + min(left[2], right[2]),
                )
    return any(
        component_count >= 4 and pixel_count >= 48
        for component_count, pixel_count in offset_support.values()
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
            if mean_luma >= 40 or min(frame.min) > 5:
                raise RuntimeError(
                    f"{pass_name}: {frame.frame} none preset background is too bright "
                    f"mean_luma={mean_luma:.3f} min={frame.min}"
                )
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
