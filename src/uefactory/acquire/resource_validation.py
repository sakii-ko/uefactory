"""Fail-closed, CPU-only validation for acquired HDRI and PBR resources.

The validators in this module deliberately accept only the encodings consumed by
the resource adapters.  In particular, Radiance's legacy RGBE encoding is not
accepted: every scanline must use the modern component-wise RLE framing.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import struct
import warnings
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from PIL import Image, UnidentifiedImageError

_MD5_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_HDR_ORIENTATION_PATTERN = re.compile(rb"-Y ([1-9][0-9]*) \+X ([1-9][0-9]*)\n\Z")
_HDR_FIELD_PATTERN = re.compile(rb"[A-Za-z][A-Za-z0-9_]*=.+\Z")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_HDR_BYTES = 2 * 1024 * 1024 * 1024
_MAX_PNG_BYTES = 1024 * 1024 * 1024
_MAX_HDR_HEADER_BYTES = 1024 * 1024
_MAX_HDR_HEADER_LINES = 256
_MAX_HDR_DIMENSION = 32_767
_MAX_PNG_DIMENSION = 32_768
_MAX_PNG_PIXELS = 128 * 1024 * 1024
_MAX_PNG_CHUNK_BYTES = 256 * 1024 * 1024
_MAX_PNG_CHUNKS = 100_000
_MAX_PNG_INFLATED_BYTES = 1024 * 1024 * 1024
_MAX_PNG_INFLATE_CHUNK_BYTES = 1024 * 1024
_MAX_PNG_ANCILLARY_CHUNK_BYTES = 16 * 1024 * 1024
_MAX_PNG_ANCILLARY_BYTES = 64 * 1024 * 1024
_MAX_PHYSICAL_SIZE_MM = 1_000_000.0

_PNG_COLORSPACE_CHUNKS = frozenset({b"cHRM", b"gAMA", b"iCCP", b"sBIT", b"sRGB"})
_PNG_BEFORE_IDAT_CHUNKS = frozenset({b"bKGD", b"eXIf", b"oFFs", b"pHYs", b"tRNS"})
_PNG_SINGLETON_ANCILLARY = _PNG_COLORSPACE_CHUNKS | _PNG_BEFORE_IDAT_CHUNKS | {b"tIME"}
_PNG_REPEATABLE_ANCILLARY = frozenset({b"iTXt", b"tEXt", b"zTXt"})

PbrMapRole = Literal["Diffuse", "nor_dx", "arm"]
ColorSpace = Literal["srgb", "data"]


class ResourceValidationError(ValueError):
    """An acquired resource or its validation request is not trustworthy."""


class _ZlibDecompressor(Protocol):
    @property
    def eof(self) -> bool: ...

    @property
    def unused_data(self) -> bytes: ...

    @property
    def unconsumed_tail(self) -> bytes: ...

    def decompress(self, data: bytes, max_length: int = 0) -> bytes: ...


@dataclass(slots=True)
class _PngInflatedScanlines:
    """Streaming proof that inflated bytes are exactly the declared PNG scanlines."""

    row_bytes: tuple[int, ...]
    row_index: int = 0
    row_remaining: int = 0
    bytes_seen: int = 0
    expected_bytes: int = field(init=False)

    def __post_init__(self) -> None:
        self.expected_bytes = sum(row_bytes + 1 for row_bytes in self.row_bytes)
        if not self.row_bytes or self.expected_bytes > _MAX_PNG_INFLATED_BYTES:
            raise ResourceValidationError("PNG inflated scanlines exceed the decode safety limit")

    def consume(self, payload: bytes) -> None:
        view = memoryview(payload)
        offset = 0
        self.bytes_seen += len(payload)
        if self.bytes_seen > self.expected_bytes:
            raise ResourceValidationError("PNG zlib stream expands beyond declared scanlines")
        while offset < len(view):
            if self.row_remaining == 0:
                if self.row_index == len(self.row_bytes):
                    raise ResourceValidationError("PNG zlib stream contains extra scanline bytes")
                filter_method = view[offset]
                if filter_method > 4:
                    raise ResourceValidationError("PNG scanline uses an invalid filter method")
                offset += 1
                self.row_remaining = self.row_bytes[self.row_index]
                self.row_index += 1
            consumed = min(self.row_remaining, len(view) - offset)
            self.row_remaining -= consumed
            offset += consumed

    def finish(self) -> None:
        if (
            self.bytes_seen != self.expected_bytes
            or self.row_index != len(self.row_bytes)
            or self.row_remaining != 0
        ):
            raise ResourceValidationError(
                "PNG inflated scanline size mismatch: "
                f"expected={self.expected_bytes} actual={self.bytes_seen}"
            )


@dataclass(frozen=True, slots=True)
class FileValidationEvidence:
    """Content identity checked against the provider's byte count and MD5."""

    path: Path
    bytes: int
    provider_md5: str
    md5: str
    sha256: str


@dataclass(frozen=True, slots=True)
class RadianceHdrValidationEvidence:
    file: FileValidationEvidence
    width: int
    height: int
    format: Literal["32-bit_rle_rgbe"]
    orientation: Literal["-Y +X"]
    encoding: Literal["modern_rle_rgbe"]
    scanlines: int


@dataclass(frozen=True, slots=True)
class PngValidationEvidence:
    file: FileValidationEvidence
    width: int
    height: int
    bit_depth: Literal[8, 16]
    channels: Literal[3, 4]
    mode: Literal["RGB", "RGBA"]


@dataclass(frozen=True, slots=True)
class PbrMapInput:
    """Provider facts for one named PBR texture map.

    Runtime validation, rather than construction, checks these fields so that all
    malformed external inputs use :class:`ResourceValidationError`.
    """

    role: str
    path: Path
    expected_size: int
    provider_md5: str


@dataclass(frozen=True, slots=True)
class PbrMapDescriptor:
    role: PbrMapRole
    color_space: ColorSpace
    semantic: Literal["base_color", "normal", "packed_material"]
    normal_convention: Literal["directx"] | None
    channel_semantics: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PbrCohortDescriptor:
    """Canonical, path-independent interpretation of a PBR texture cohort."""

    pixel_dimensions: tuple[int, int]
    physical_size_mm: tuple[float, float]
    maps: tuple[PbrMapDescriptor, ...]


@dataclass(frozen=True, slots=True)
class PbrMapValidationEvidence:
    descriptor: PbrMapDescriptor
    image: PngValidationEvidence


@dataclass(frozen=True, slots=True)
class PbrCohortValidationEvidence:
    descriptor: PbrCohortDescriptor
    maps: tuple[PbrMapValidationEvidence, ...]


_PBR_DESCRIPTORS: tuple[PbrMapDescriptor, ...] = (
    PbrMapDescriptor(
        role="Diffuse",
        color_space="srgb",
        semantic="base_color",
        normal_convention=None,
        channel_semantics=(
            ("r", "base_color_r"),
            ("g", "base_color_g"),
            ("b", "base_color_b"),
        ),
    ),
    PbrMapDescriptor(
        role="nor_dx",
        color_space="data",
        semantic="normal",
        normal_convention="directx",
        channel_semantics=(("r", "normal_x"), ("g", "normal_y"), ("b", "normal_z")),
    ),
    PbrMapDescriptor(
        role="arm",
        color_space="data",
        semantic="packed_material",
        normal_convention=None,
        channel_semantics=(
            ("r", "ambient_occlusion"),
            ("g", "roughness"),
            ("b", "metallic"),
        ),
    ),
)
_PBR_ROLES = tuple(descriptor.role for descriptor in _PBR_DESCRIPTORS)


def validate_radiance_hdr(
    path: Path,
    *,
    expected_size: int,
    provider_md5: str,
) -> RadianceHdrValidationEvidence:
    """Validate one modern-RLE Radiance RGBE file and return immutable evidence.

    Files using Radiance's legacy flat/run-marker encoding are rejected even when
    a generic HDR decoder might accept them.  This keeps ingestion behavior
    deterministic and makes complete encoded-stream consumption auditable.
    """

    checked_path, checked_size, checked_md5 = _validated_request(
        path,
        expected_size=expected_size,
        provider_md5=provider_md5,
        maximum_bytes=_MAX_HDR_BYTES,
    )
    with _open_regular_snapshot(checked_path, checked_size) as file:
        actual_md5, sha256 = _hashes(file, checked_size)
        if actual_md5 != checked_md5:
            raise ResourceValidationError(
                f"provider MD5 mismatch for {checked_path}: "
                f"expected={checked_md5} actual={actual_md5}"
            )
        file.seek(0)
        width, height = _validate_hdr_stream(file)

    identity = FileValidationEvidence(
        path=checked_path,
        bytes=checked_size,
        provider_md5=checked_md5,
        md5=actual_md5,
        sha256=sha256,
    )
    return RadianceHdrValidationEvidence(
        file=identity,
        width=width,
        height=height,
        format="32-bit_rle_rgbe",
        orientation="-Y +X",
        encoding="modern_rle_rgbe",
        scanlines=height,
    )


def validate_png(
    path: Path,
    *,
    expected_size: int,
    provider_md5: str,
) -> PngValidationEvidence:
    """Validate PNG framing, CRCs, and fully decoded RGB/RGBA image content."""

    checked_path, checked_size, checked_md5 = _validated_request(
        path,
        expected_size=expected_size,
        provider_md5=provider_md5,
        maximum_bytes=_MAX_PNG_BYTES,
    )
    with _open_regular_snapshot(checked_path, checked_size) as file:
        actual_md5, sha256 = _hashes(file, checked_size)
        if actual_md5 != checked_md5:
            raise ResourceValidationError(
                f"provider MD5 mismatch for {checked_path}: "
                f"expected={checked_md5} actual={actual_md5}"
            )
        file.seek(0)
        width, height, bit_depth, channels, declared_mode = _validate_png_chunks(file, checked_size)
        _verify_and_load_png(
            file,
            path=checked_path,
            expected_dimensions=(width, height),
            expected_mode=declared_mode,
        )

    identity = FileValidationEvidence(
        path=checked_path,
        bytes=checked_size,
        provider_md5=checked_md5,
        md5=actual_md5,
        sha256=sha256,
    )
    return PngValidationEvidence(
        file=identity,
        width=width,
        height=height,
        bit_depth=bit_depth,
        channels=channels,
        mode=declared_mode,
    )


def validate_pbr_cohort(
    *,
    maps: tuple[PbrMapInput, ...],
    physical_size_mm: tuple[float, float],
) -> PbrCohortValidationEvidence:
    """Validate and canonically describe Diffuse/nor_dx/arm PBR textures."""

    if not isinstance(maps, tuple):
        raise ResourceValidationError("PBR maps must be an immutable tuple")
    if len(maps) != len(_PBR_ROLES):
        raise ResourceValidationError("PBR cohort must contain exactly three maps")

    by_role: dict[str, PbrMapInput] = {}
    for index, item in enumerate(maps):
        if type(item) is not PbrMapInput:
            raise ResourceValidationError(f"PBR map {index} must be a PbrMapInput")
        if item.role not in _PBR_ROLES:
            raise ResourceValidationError(f"unsupported PBR map role: {item.role!r}")
        if item.role in by_role:
            raise ResourceValidationError(f"duplicate PBR map role: {item.role!r}")
        by_role[item.role] = item
    missing = tuple(role for role in _PBR_ROLES if role not in by_role)
    if missing:
        raise ResourceValidationError(f"PBR cohort is missing map roles: {missing!r}")

    physical_size = _physical_size(physical_size_mm)
    validated: list[PbrMapValidationEvidence] = []
    dimensions: tuple[int, int] | None = None
    for descriptor in _PBR_DESCRIPTORS:
        item = by_role[descriptor.role]
        image = validate_png(
            item.path,
            expected_size=item.expected_size,
            provider_md5=item.provider_md5,
        )
        current_dimensions = (image.width, image.height)
        if dimensions is None:
            dimensions = current_dimensions
        elif current_dimensions != dimensions:
            raise ResourceValidationError(
                "PBR texture dimensions do not match: "
                f"expected={dimensions} actual={current_dimensions} role={descriptor.role}"
            )
        validated.append(PbrMapValidationEvidence(descriptor=descriptor, image=image))

    if dimensions is None:  # Defensive: exact cohort cardinality makes this unreachable.
        raise ResourceValidationError("PBR cohort has no texture dimensions")
    cohort_descriptor = PbrCohortDescriptor(
        pixel_dimensions=dimensions,
        physical_size_mm=physical_size,
        maps=_PBR_DESCRIPTORS,
    )
    return PbrCohortValidationEvidence(descriptor=cohort_descriptor, maps=tuple(validated))


def _validated_request(
    path: Path,
    *,
    expected_size: int,
    provider_md5: str,
    maximum_bytes: int,
) -> tuple[Path, int, str]:
    if not isinstance(path, Path):
        raise ResourceValidationError("resource path must be a pathlib.Path")
    if type(expected_size) is not int or not 0 < expected_size <= maximum_bytes:
        raise ResourceValidationError(f"expected_size must be an integer in [1, {maximum_bytes}]")
    if not isinstance(provider_md5, str) or _MD5_PATTERN.fullmatch(provider_md5) is None:
        raise ResourceValidationError("provider_md5 must be a lowercase 32-character MD5")
    return path, expected_size, provider_md5


@contextmanager
def _open_regular_snapshot(path: Path, expected_size: int) -> Iterator[BinaryIO]:
    try:
        initial = path.lstat()
    except (OSError, TypeError, ValueError) as exc:
        raise ResourceValidationError(f"cannot stat resource file {path}: {exc}") from exc
    if stat.S_ISLNK(initial.st_mode):
        raise ResourceValidationError(f"resource file must not be a symlink: {path}")
    if not stat.S_ISREG(initial.st_mode):
        raise ResourceValidationError(f"resource path is not a regular file: {path}")
    if initial.st_size != expected_size:
        raise ResourceValidationError(
            f"provider size mismatch for {path}: expected={expected_size} actual={initial.st_size}"
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, TypeError, ValueError) as exc:
        raise ResourceValidationError(f"cannot safely open resource file {path}: {exc}") from exc
    try:
        file = os.fdopen(descriptor, "rb")
    except (OSError, TypeError, ValueError) as exc:
        os.close(descriptor)
        raise ResourceValidationError(f"cannot safely open resource file {path}: {exc}") from exc
    except BaseException:
        os.close(descriptor)
        raise

    try:
        opened = os.fstat(file.fileno())
        if not _same_file_snapshot(initial, opened) or not stat.S_ISREG(opened.st_mode):
            raise ResourceValidationError(f"resource file changed while opening: {path}")
        yield file
        final = os.fstat(file.fileno())
        try:
            final_path = path.lstat()
        except (OSError, TypeError, ValueError) as exc:
            raise ResourceValidationError(
                f"resource file changed during validation: {path}"
            ) from exc
        if not _same_file_snapshot(opened, final) or not _same_file_snapshot(opened, final_path):
            raise ResourceValidationError(f"resource file changed during validation: {path}")
    except OSError as exc:
        raise ResourceValidationError(f"cannot read resource file {path}: {exc}") from exc
    finally:
        file.close()


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _hashes(file: BinaryIO, expected_size: int) -> tuple[str, str]:
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    consumed = 0
    while True:
        chunk = file.read(_HASH_CHUNK_BYTES)
        if not chunk:
            break
        consumed += len(chunk)
        if consumed > expected_size:
            raise ResourceValidationError("resource grew while hashing")
        md5.update(chunk)
        sha256.update(chunk)
    if consumed != expected_size:
        raise ResourceValidationError(
            f"resource size changed while hashing: expected={expected_size} actual={consumed}"
        )
    return md5.hexdigest(), sha256.hexdigest()


def _validate_hdr_stream(file: BinaryIO) -> tuple[int, int]:
    magic = _read_line(file, label="Radiance magic", maximum=64)
    if magic != b"#?RADIANCE\n":
        raise ResourceValidationError("Radiance HDR has invalid magic (expected #?RADIANCE)")

    header_bytes = len(magic)
    header_lines = 0
    format_seen = False
    while True:
        line = _read_line(file, label="Radiance header", maximum=_MAX_HDR_HEADER_BYTES)
        header_bytes += len(line)
        header_lines += 1
        if header_bytes > _MAX_HDR_HEADER_BYTES or header_lines > _MAX_HDR_HEADER_LINES:
            raise ResourceValidationError("Radiance HDR header exceeds safety limits")
        if line == b"\n":
            break
        body = line[:-1]
        if b"\r" in body or any(byte < 32 and byte != 9 for byte in body):
            raise ResourceValidationError("Radiance HDR header contains control characters")
        if body.startswith(b"FORMAT="):
            if format_seen:
                raise ResourceValidationError("Radiance HDR contains duplicate FORMAT fields")
            if body != b"FORMAT=32-bit_rle_rgbe":
                raise ResourceValidationError("Radiance HDR FORMAT must be 32-bit_rle_rgbe")
            format_seen = True
        elif body.startswith(b"#"):
            if any(byte > 126 for byte in body):
                raise ResourceValidationError("Radiance HDR comment is not ASCII")
        elif _HDR_FIELD_PATTERN.fullmatch(body) is None or any(byte > 126 for byte in body):
            raise ResourceValidationError("Radiance HDR contains an invalid header field")
    if not format_seen:
        raise ResourceValidationError("Radiance HDR is missing FORMAT=32-bit_rle_rgbe")

    orientation = _read_line(file, label="Radiance orientation", maximum=128)
    match = _HDR_ORIENTATION_PATTERN.fullmatch(orientation)
    if match is None:
        raise ResourceValidationError("Radiance HDR orientation must be exactly '-Y H +X W'")
    height = int(match.group(1))
    width = int(match.group(2))
    if width > _MAX_HDR_DIMENSION or height > _MAX_HDR_DIMENSION:
        raise ResourceValidationError("Radiance HDR dimensions exceed safety limits")
    if width != height * 2:
        raise ResourceValidationError("Radiance HDR dimensions must have an exact 2:1 ratio")
    if width < 8:
        raise ResourceValidationError(
            "legacy Radiance encoding is not accepted (modern RLE requires width >= 8)"
        )

    for scanline in range(height):
        prefix = _read_exact(file, 4, f"Radiance scanline {scanline} prefix")
        if prefix[0] != 2 or prefix[1] != 2 or prefix[2] & 0x80:
            raise ResourceValidationError(
                f"Radiance scanline {scanline} uses legacy or invalid non-modern RLE encoding"
            )
        encoded_width = (prefix[2] << 8) | prefix[3]
        if encoded_width != width:
            raise ResourceValidationError(
                f"Radiance scanline {scanline} width mismatch: "
                f"expected={width} actual={encoded_width}"
            )
        for component in range(4):
            _consume_hdr_component(file, width, scanline=scanline, component=component)
    if file.read(1) != b"":
        raise ResourceValidationError("Radiance HDR contains trailing data")
    return width, height


def _consume_hdr_component(
    file: BinaryIO,
    width: int,
    *,
    scanline: int,
    component: int,
) -> None:
    produced = 0
    while produced < width:
        code = _read_exact(
            file,
            1,
            f"Radiance scanline {scanline} component {component} packet",
        )[0]
        if code == 0:
            raise ResourceValidationError(
                f"Radiance scanline {scanline} component {component} has a zero-length packet"
            )
        if code > 128:
            count = code - 128
            _read_exact(
                file,
                1,
                f"Radiance scanline {scanline} component {component} run value",
            )
        else:
            count = code
            _read_exact(
                file,
                count,
                f"Radiance scanline {scanline} component {component} literal",
            )
        if count > width - produced:
            raise ResourceValidationError(
                f"Radiance scanline {scanline} component {component} packet overflows width"
            )
        produced += count


def _read_line(file: BinaryIO, *, label: str, maximum: int) -> bytes:
    line = file.readline(maximum + 1)
    if not line:
        raise ResourceValidationError(f"{label} is truncated")
    if len(line) > maximum:
        raise ResourceValidationError(f"{label} exceeds its safety limit")
    if not line.endswith(b"\n"):
        raise ResourceValidationError(f"{label} is not newline-terminated")
    return line


def _read_exact(file: BinaryIO, size: int, label: str) -> bytes:
    payload = file.read(size)
    if len(payload) != size:
        raise ResourceValidationError(f"{label} is truncated")
    return payload


def _validate_png_chunks(
    file: BinaryIO,
    file_size: int,
) -> tuple[int, int, Literal[8, 16], Literal[3, 4], Literal["RGB", "RGBA"]]:
    if _read_exact(file, len(_PNG_SIGNATURE), "PNG signature") != _PNG_SIGNATURE:
        raise ResourceValidationError("PNG has an invalid signature")

    width = height = bit_depth = color_type = 0
    saw_ihdr = saw_plte = saw_idat = saw_iend = False
    idat_ended = False
    idat_bytes = 0
    ancillary_bytes = 0
    singleton_ancillary: set[bytes] = set()
    decompressor: _ZlibDecompressor | None = None
    scanlines: _PngInflatedScanlines | None = None
    for chunk_index in range(_MAX_PNG_CHUNKS):
        header = _read_exact(file, 8, f"PNG chunk {chunk_index} header")
        length, chunk_type = struct.unpack(">I4s", header)
        if not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in chunk_type):
            raise ResourceValidationError("PNG chunk type must contain four ASCII letters")
        if not 65 <= chunk_type[2] <= 90:
            raise ResourceValidationError("PNG chunk type has a lowercase reserved bit")
        if length > _MAX_PNG_CHUNK_BYTES:
            raise ResourceValidationError("PNG chunk exceeds the per-chunk safety limit")
        remaining = file_size - file.tell()
        if length + 4 > remaining:
            raise ResourceValidationError(f"PNG chunk {chunk_type!r} is truncated")

        if chunk_type != b"IDAT" and saw_idat and not idat_ended:
            _finish_png_idat(decompressor, scanlines)
            idat_ended = True
        if chunk_type == b"IHDR":
            if chunk_index != 0 or saw_ihdr or length != 13:
                raise ResourceValidationError("PNG must begin with exactly one 13-byte IHDR")
            data = _read_exact(file, length, "PNG IHDR")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            if not 0 < width <= _MAX_PNG_DIMENSION or not 0 < height <= _MAX_PNG_DIMENSION:
                raise ResourceValidationError("PNG dimensions exceed safety limits")
            if width * height > _MAX_PNG_PIXELS:
                raise ResourceValidationError("PNG pixel count exceeds the decode safety limit")
            if color_type not in {2, 6} or bit_depth not in {8, 16}:
                raise ResourceValidationError("PNG must be 8/16-bit RGB or RGBA")
            if compression != 0 or filtering != 0 or interlace not in {0, 1}:
                raise ResourceValidationError("PNG IHDR uses unsupported encoding fields")
            saw_ihdr = True
            computed_crc = zlib.crc32(data, zlib.crc32(chunk_type))
        else:
            if not saw_ihdr:
                raise ResourceValidationError("PNG IHDR must be the first chunk")
            if chunk_type == b"PLTE":
                if (
                    saw_plte
                    or saw_idat
                    or singleton_ancillary.intersection({b"bKGD", b"tRNS"})
                    or not 0 < length <= 768
                    or length % 3
                ):
                    raise ResourceValidationError("PNG contains an invalid PLTE chunk")
                saw_plte = True
                computed_crc = _consume_png_chunk(file, length, zlib.crc32(chunk_type))
            elif chunk_type == b"IDAT":
                if idat_ended:
                    raise ResourceValidationError("PNG IDAT chunks must be consecutive")
                if decompressor is None or scanlines is None:
                    decompressor = zlib.decompressobj()
                    scanlines = _PngInflatedScanlines(
                        _png_row_bytes(
                            width=width,
                            height=height,
                            bit_depth=bit_depth,
                            color_type=color_type,
                            interlace=interlace,
                        )
                    )
                saw_idat = True
                idat_bytes += length
                computed_crc = _consume_png_idat(
                    file,
                    length,
                    zlib.crc32(chunk_type),
                    decompressor=decompressor,
                    scanlines=scanlines,
                )
            elif chunk_type == b"IEND":
                if saw_iend or length != 0:
                    raise ResourceValidationError("PNG contains an invalid IEND chunk")
                saw_iend = True
                computed_crc = _consume_png_chunk(file, length, zlib.crc32(chunk_type))
            elif 65 <= chunk_type[0] <= 90:
                raise ResourceValidationError(f"PNG contains unknown critical chunk {chunk_type!r}")
            else:
                ancillary_bytes += length
                if (
                    length > _MAX_PNG_ANCILLARY_CHUNK_BYTES
                    or ancillary_bytes > _MAX_PNG_ANCILLARY_BYTES
                ):
                    raise ResourceValidationError("PNG ancillary metadata exceeds safety limits")
                computed_crc = _validate_png_ancillary(
                    file,
                    chunk_type=chunk_type,
                    length=length,
                    color_type=color_type,
                    bit_depth=bit_depth,
                    saw_plte=saw_plte,
                    saw_idat=saw_idat,
                    singleton_seen=singleton_ancillary,
                )

        stored_crc = struct.unpack(">I", _read_exact(file, 4, "PNG chunk CRC"))[0]
        if computed_crc & 0xFFFFFFFF != stored_crc:
            name = chunk_type.decode("ascii")
            raise ResourceValidationError(f"PNG {name} CRC mismatch")
        if saw_iend:
            if not saw_idat or idat_bytes == 0:
                raise ResourceValidationError("PNG must contain non-empty IDAT data")
            if file.read(1) != b"":
                raise ResourceValidationError("PNG contains trailing data after IEND")
            break
    else:
        raise ResourceValidationError("PNG chunk count exceeds its safety limit")
    if not saw_iend:
        raise ResourceValidationError("PNG is missing IEND")

    if color_type == 2:
        return width, height, _png_depth(bit_depth), 3, "RGB"
    return width, height, _png_depth(bit_depth), 4, "RGBA"


def _consume_png_chunk(file: BinaryIO, length: int, crc: int) -> int:
    remaining = length
    while remaining:
        chunk = _read_exact(file, min(remaining, _HASH_CHUNK_BYTES), "PNG chunk payload")
        crc = zlib.crc32(chunk, crc)
        remaining -= len(chunk)
    return crc


def _consume_png_idat(
    file: BinaryIO,
    length: int,
    crc: int,
    *,
    decompressor: _ZlibDecompressor,
    scanlines: _PngInflatedScanlines,
) -> int:
    remaining = length
    while remaining:
        chunk = _read_exact(file, min(remaining, _HASH_CHUNK_BYTES), "PNG IDAT payload")
        crc = zlib.crc32(chunk, crc)
        _feed_png_zlib(decompressor, scanlines, chunk)
        remaining -= len(chunk)
    return crc


def _feed_png_zlib(
    decompressor: _ZlibDecompressor,
    scanlines: _PngInflatedScanlines,
    payload: bytes,
) -> None:
    if payload and decompressor.eof:
        raise ResourceValidationError("PNG IDAT contains bytes after its single zlib stream")
    pending = payload
    while True:
        if decompressor.eof:
            if pending:
                raise ResourceValidationError(
                    "PNG IDAT contains bytes after its single zlib stream"
                )
            break
        try:
            inflated = decompressor.decompress(pending, _MAX_PNG_INFLATE_CHUNK_BYTES)
        except zlib.error as exc:
            raise ResourceValidationError("PNG IDAT contains an invalid zlib stream") from exc
        scanlines.consume(inflated)
        if decompressor.unused_data:
            raise ResourceValidationError("PNG IDAT contains bytes after its single zlib stream")
        tail = decompressor.unconsumed_tail
        if tail:
            if len(tail) == len(pending) and not inflated:
                raise ResourceValidationError("PNG zlib decoder made no forward progress")
            pending = tail
            continue
        if len(inflated) == _MAX_PNG_INFLATE_CHUNK_BYTES:
            pending = b""
            continue
        break


def _finish_png_idat(
    decompressor: _ZlibDecompressor | None,
    scanlines: _PngInflatedScanlines | None,
) -> None:
    if decompressor is None or scanlines is None:
        raise ResourceValidationError("PNG has no zlib stream")
    if not decompressor.eof:
        raise ResourceValidationError("PNG zlib stream did not reach EOF")
    if decompressor.unused_data or decompressor.unconsumed_tail:
        raise ResourceValidationError("PNG zlib stream was not consumed exactly once")
    scanlines.finish()


def _png_row_bytes(
    *,
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
    interlace: int,
) -> tuple[int, ...]:
    channels = 3 if color_type == 2 else 4
    bits_per_pixel = channels * bit_depth
    if interlace == 0:
        row_bytes = (width * bits_per_pixel + 7) // 8
        return (row_bytes,) * height

    rows: list[int] = []
    adam7_passes = (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    )
    for x_start, y_start, x_step, y_step in adam7_passes:
        if width <= x_start or height <= y_start:
            continue
        pass_width = (width - x_start + x_step - 1) // x_step
        pass_height = (height - y_start + y_step - 1) // y_step
        row_bytes = (pass_width * bits_per_pixel + 7) // 8
        rows.extend((row_bytes,) * pass_height)
    return tuple(rows)


def _validate_png_ancillary(
    file: BinaryIO,
    *,
    chunk_type: bytes,
    length: int,
    color_type: int,
    bit_depth: int,
    saw_plte: bool,
    saw_idat: bool,
    singleton_seen: set[bytes],
) -> int:
    allowed = _PNG_SINGLETON_ANCILLARY | _PNG_REPEATABLE_ANCILLARY
    if chunk_type not in allowed:
        raise ResourceValidationError(f"PNG contains unsupported ancillary chunk {chunk_type!r}")
    if chunk_type in _PNG_SINGLETON_ANCILLARY:
        if chunk_type in singleton_seen:
            raise ResourceValidationError(f"PNG contains duplicate {chunk_type.decode('ascii')}")
        singleton_seen.add(chunk_type)
    if chunk_type in _PNG_COLORSPACE_CHUNKS and (saw_plte or saw_idat):
        raise ResourceValidationError(
            f"PNG {chunk_type.decode('ascii')} must appear before PLTE and IDAT"
        )
    if chunk_type in _PNG_BEFORE_IDAT_CHUNKS and saw_idat:
        raise ResourceValidationError(f"PNG {chunk_type.decode('ascii')} must appear before IDAT")

    payload = _read_exact(file, length, f"PNG {chunk_type.decode('ascii')} payload")
    _validate_png_ancillary_payload(
        chunk_type,
        payload,
        color_type=color_type,
        bit_depth=bit_depth,
        singleton_seen=singleton_seen,
    )
    return zlib.crc32(payload, zlib.crc32(chunk_type))


def _validate_png_ancillary_payload(
    chunk_type: bytes,
    payload: bytes,
    *,
    color_type: int,
    bit_depth: int,
    singleton_seen: set[bytes],
) -> None:
    if chunk_type == b"cHRM" and len(payload) != 32:
        raise ResourceValidationError("PNG cHRM must contain exactly 32 bytes")
    if chunk_type == b"gAMA" and (len(payload) != 4 or struct.unpack(">I", payload)[0] == 0):
        raise ResourceValidationError("PNG gAMA must contain one nonzero integer")
    if chunk_type == b"sRGB" and (len(payload) != 1 or payload[0] > 3):
        raise ResourceValidationError("PNG sRGB has an invalid rendering intent")
    if chunk_type == b"iCCP":
        separator = payload.find(b"\x00")
        if (
            not 1 <= separator <= 79
            or len(payload) <= separator + 2
            or payload[separator + 1] != 0
            or not _valid_png_keyword(payload[:separator])
        ):
            raise ResourceValidationError("PNG iCCP has invalid profile framing")
    if b"iCCP" in singleton_seen and b"sRGB" in singleton_seen:
        raise ResourceValidationError("PNG iCCP and sRGB chunks are mutually exclusive")
    if chunk_type == b"sBIT":
        channels = 3 if color_type == 2 else 4
        if len(payload) != channels or any(value == 0 or value > bit_depth for value in payload):
            raise ResourceValidationError("PNG sBIT does not match IHDR channels and depth")
    if chunk_type == b"tRNS":
        if color_type != 2 or len(payload) != 6:
            raise ResourceValidationError("PNG tRNS is valid only as 6-byte RGB transparency")
        maximum = (1 << bit_depth) - 1
        if any(value > maximum for value in struct.unpack(">HHH", payload)):
            raise ResourceValidationError("PNG tRNS sample exceeds IHDR bit depth")
    if chunk_type == b"bKGD":
        if len(payload) != 6:
            raise ResourceValidationError("PNG bKGD must contain three RGB samples")
        maximum = (1 << bit_depth) - 1
        if any(value > maximum for value in struct.unpack(">HHH", payload)):
            raise ResourceValidationError("PNG bKGD sample exceeds IHDR bit depth")
    if chunk_type in {b"oFFs", b"pHYs"} and (len(payload) != 9 or payload[8] > 1):
        raise ResourceValidationError(
            f"PNG {chunk_type.decode('ascii')} has invalid dimensions or unit"
        )
    if chunk_type == b"eXIf" and not payload:
        raise ResourceValidationError("PNG eXIf must not be empty")
    if chunk_type == b"tIME" and not _valid_png_time(payload):
        raise ResourceValidationError("PNG tIME has invalid fields")
    if chunk_type in {b"tEXt", b"zTXt", b"iTXt"}:
        _validate_png_text(chunk_type, payload)


def _valid_png_keyword(payload: bytes) -> bool:
    return (
        bool(payload)
        and len(payload) <= 79
        and all(byte == 32 or 33 <= byte <= 126 or 161 <= byte <= 255 for byte in payload)
    )


def _validate_png_text(chunk_type: bytes, payload: bytes) -> None:
    separator = payload.find(b"\x00")
    if separator < 0 or not _valid_png_keyword(payload[:separator]):
        raise ResourceValidationError(f"PNG {chunk_type.decode('ascii')} has an invalid keyword")
    remainder = payload[separator + 1 :]
    if chunk_type == b"zTXt" and (len(remainder) < 2 or remainder[0] != 0):
        raise ResourceValidationError("PNG zTXt has invalid compression framing")
    if chunk_type == b"iTXt" and (len(remainder) < 4 or remainder[0] not in {0, 1}):
        raise ResourceValidationError("PNG iTXt has invalid compression framing")
    if chunk_type == b"iTXt" and remainder[1] != 0:
        raise ResourceValidationError("PNG iTXt has an unsupported compression method")


def _valid_png_time(payload: bytes) -> bool:
    if len(payload) != 7:
        return False
    _year, month, day, hour, minute, second = struct.unpack(">HBBBBB", payload)
    return 1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59 and second <= 60


def _png_depth(value: int) -> Literal[8, 16]:
    if value == 8:
        return 8
    if value == 16:
        return 16
    raise ResourceValidationError("PNG has an unsupported bit depth")


def _verify_and_load_png(
    file: BinaryIO,
    *,
    path: Path,
    expected_dimensions: tuple[int, int],
    expected_mode: Literal["RGB", "RGBA"],
) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            file.seek(0)
            with Image.open(file) as image:
                if (
                    image.format != "PNG"
                    or image.size != expected_dimensions
                    or image.mode != expected_mode
                ):
                    raise ResourceValidationError("Pillow PNG metadata disagrees with IHDR")
                image.verify()
            file.seek(0)
            with Image.open(file) as image:
                image.load()
                if (
                    image.format != "PNG"
                    or image.size != expected_dimensions
                    or image.mode != expected_mode
                    or image.getbands() != tuple(expected_mode)
                ):
                    raise ResourceValidationError("fully decoded PNG disagrees with IHDR")
    except ResourceValidationError:
        raise
    except (OSError, SyntaxError, ValueError, UnidentifiedImageError) as exc:
        raise ResourceValidationError(f"PNG cannot be fully decoded by Pillow: {path}") from exc


def _physical_size(value: tuple[float, float]) -> tuple[float, float]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise ResourceValidationError("physical_size_mm must be a tuple of exactly two values")
    result: list[float] = []
    for index, item in enumerate(value):
        if type(item) not in {int, float}:
            raise ResourceValidationError(
                f"physical_size_mm[{index}] must be a finite positive number"
            )
        try:
            converted = float(item)
        except OverflowError as exc:
            raise ResourceValidationError(
                f"physical_size_mm[{index}] must be a finite positive number"
            ) from exc
        if not math.isfinite(converted) or not 0.0 < converted <= _MAX_PHYSICAL_SIZE_MM:
            raise ResourceValidationError(
                f"physical_size_mm[{index}] must be finite and in (0, {_MAX_PHYSICAL_SIZE_MM}]"
            )
        result.append(converted)
    return result[0], result[1]
