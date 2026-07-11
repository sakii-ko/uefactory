from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from uefactory.acquire import resource_validation
from uefactory.acquire.resource_validation import (
    PbrMapInput,
    RadianceHdrValidationEvidence,
    ResourceValidationError,
    validate_pbr_cohort,
    validate_png,
    validate_radiance_hdr,
)


def _md5(payload: bytes) -> str:
    return hashlib.md5(payload, usedforsecurity=False).hexdigest()


def _write_payload(path: Path, payload: bytes) -> tuple[int, str]:
    path.write_bytes(payload)
    return len(payload), _md5(payload)


def _hdr_payload(
    *,
    width: int = 8,
    height: int = 4,
    magic: bytes = b"#?RADIANCE\n",
    format_line: bytes = b"FORMAT=32-bit_rle_rgbe\n",
    orientation: bytes | None = None,
) -> bytes:
    header = magic + b"EXPOSURE=1.000000\n" + format_line + b"\n"
    resolution = orientation or f"-Y {height} +X {width}\n".encode("ascii")
    scanline = b"\x02\x02" + width.to_bytes(2, "big")
    for component in range(4):
        scanline += bytes((width,)) + bytes((component + 1,)) * width
    return header + resolution + scanline * height


def _validate_hdr_file(path: Path, payload: bytes) -> RadianceHdrValidationEvidence:
    size, md5 = _write_payload(path, payload)
    return validate_radiance_hdr(path, expected_size=size, provider_md5=md5)


def _png_input(role: str, path: Path) -> PbrMapInput:
    payload = path.read_bytes()
    return PbrMapInput(
        role=role,
        path=path,
        expected_size=len(payload),
        provider_md5=_md5(payload),
    )


def _write_png(path: Path, *, mode: str = "RGB", size: tuple[int, int] = (4, 2)) -> bytes:
    color = (10, 20, 30, 255) if mode == "RGBA" else (10, 20, 30) if mode == "RGB" else 10
    Image.new(mode, size, color).save(path)
    return path.read_bytes()


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(data, zlib.crc32(kind)) & 0xFFFFFFFF)
    )


def _rgb_png_payload(
    *,
    width: int = 4,
    height: int = 2,
    idat_payloads: tuple[bytes, ...] | None = None,
    before_idat: tuple[bytes, ...] = (),
    after_idat: tuple[bytes, ...] = (),
) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_scanlines = (b"\x00" + b"\x01\x02\x03" * width) * height
    payloads = idat_payloads or (zlib.compress(raw_scanlines),)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + b"".join(before_idat)
        + b"".join(_png_chunk(b"IDAT", payload) for payload in payloads)
        + b"".join(after_idat)
        + _png_chunk(b"IEND", b"")
    )


def test_validate_radiance_hdr_returns_immutable_content_and_encoding_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "studio.hdr"
    payload = _hdr_payload()

    evidence = _validate_hdr_file(path, payload)

    assert (evidence.width, evidence.height) == (8, 4)
    assert evidence.format == "32-bit_rle_rgbe"
    assert evidence.orientation == "-Y +X"
    assert evidence.encoding == "modern_rle_rgbe"
    assert evidence.scanlines == 4
    assert evidence.file.bytes == len(payload)
    assert evidence.file.provider_md5 == _md5(payload)
    assert evidence.file.md5 == _md5(payload)
    assert evidence.file.sha256 == hashlib.sha256(payload).hexdigest()
    with pytest.raises(FrozenInstanceError):
        evidence.width = 16  # type: ignore[misc]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_hdr_payload(magic=b"#?RGBE\n"), "invalid magic"),
        (_hdr_payload(format_line=b"FORMAT=32-bit_rle_xyz\n"), "FORMAT"),
        (
            _hdr_payload(format_line=b"FORMAT=32-bit_rle_rgbe\nFORMAT=32-bit_rle_rgbe\n"),
            "duplicate FORMAT",
        ),
        (_hdr_payload(orientation=b"+Y 4 +X 8\n"), "orientation"),
        (_hdr_payload(orientation=b"-Y 4 -X 8\n"), "orientation"),
        (_hdr_payload(width=10, height=4), "2:1 ratio"),
        (_hdr_payload(width=6, height=3), "legacy Radiance encoding"),
    ],
)
def test_validate_radiance_hdr_rejects_noncanonical_header_and_dimensions(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    with pytest.raises(ResourceValidationError, match=message):
        _validate_hdr_file(tmp_path / "bad.hdr", payload)


def test_validate_radiance_hdr_rejects_legacy_scanline_encoding(tmp_path: Path) -> None:
    payload = bytearray(_hdr_payload())
    data_start = payload.index(b"-Y 4 +X 8\n") + len(b"-Y 4 +X 8\n")
    payload[data_start : data_start + 4] = b"\x01\x02\x03\x04"

    with pytest.raises(ResourceValidationError, match="legacy or invalid non-modern"):
        _validate_hdr_file(tmp_path / "legacy.hdr", bytes(payload))


def test_validate_radiance_hdr_rejects_truncated_rle(tmp_path: Path) -> None:
    payload = _hdr_payload()[:-1]

    with pytest.raises(ResourceValidationError, match="truncated"):
        _validate_hdr_file(tmp_path / "truncated.hdr", payload)


def test_validate_radiance_hdr_rejects_rle_packet_overflow(tmp_path: Path) -> None:
    payload = bytearray(_hdr_payload())
    data_start = payload.index(b"-Y 4 +X 8\n") + len(b"-Y 4 +X 8\n")
    payload[data_start + 4] = 128 + 9

    with pytest.raises(ResourceValidationError, match="overflows width"):
        _validate_hdr_file(tmp_path / "overflow.hdr", bytes(payload))


def test_validate_radiance_hdr_rejects_trailing_data(tmp_path: Path) -> None:
    with pytest.raises(ResourceValidationError, match="trailing data"):
        _validate_hdr_file(tmp_path / "trailing.hdr", _hdr_payload() + b"x")


def test_validate_radiance_hdr_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.hdr"
    payload = _hdr_payload()
    target.write_bytes(payload)
    link = tmp_path / "link.hdr"
    link.symlink_to(target)

    with pytest.raises(ResourceValidationError, match="must not be a symlink"):
        validate_radiance_hdr(link, expected_size=len(payload), provider_md5=_md5(payload))


def test_validate_png_checks_crc_and_fully_decodes_rgb_and_rgba(tmp_path: Path) -> None:
    rgb_path = tmp_path / "rgb.png"
    rgba_path = tmp_path / "rgba.png"
    rgb = _write_png(rgb_path)
    rgba = _write_png(rgba_path, mode="RGBA")

    rgb_evidence = validate_png(
        rgb_path,
        expected_size=len(rgb),
        provider_md5=_md5(rgb),
    )
    rgba_evidence = validate_png(
        rgba_path,
        expected_size=len(rgba),
        provider_md5=_md5(rgba),
    )

    assert (rgb_evidence.width, rgb_evidence.height) == (4, 2)
    assert (rgb_evidence.bit_depth, rgb_evidence.channels, rgb_evidence.mode) == (8, 3, "RGB")
    assert (rgba_evidence.bit_depth, rgba_evidence.channels, rgba_evidence.mode) == (
        8,
        4,
        "RGBA",
    )
    assert rgb_evidence.file.sha256 == hashlib.sha256(rgb).hexdigest()


def test_validate_png_rejects_corrupt_chunk_crc(tmp_path: Path) -> None:
    path = tmp_path / "crc.png"
    payload = bytearray(_write_png(path))
    idat = payload.index(b"IDAT")
    length = struct.unpack(">I", payload[idat - 4 : idat])[0]
    crc_offset = idat + 4 + length
    payload[crc_offset] ^= 1

    with pytest.raises(ResourceValidationError, match="IDAT CRC mismatch"):
        size, md5 = _write_payload(path, bytes(payload))
        validate_png(path, expected_size=size, provider_md5=md5)


def test_validate_png_reopens_and_rejects_truncated_compressed_pixels(tmp_path: Path) -> None:
    path = tmp_path / "pixels.png"
    width, height = 4, 2
    compressed = zlib.compress((b"\x00" + b"\x01\x02\x03" * width) * height)
    payload = _rgb_png_payload(width=width, height=height, idat_payloads=(compressed[:-2],))

    with pytest.raises(ResourceValidationError, match="zlib stream did not reach EOF"):
        size, md5 = _write_payload(path, payload)
        validate_png(path, expected_size=size, provider_md5=md5)


@pytest.mark.parametrize(
    "suffix",
    [
        b"smuggled",
        zlib.compress((b"\x00" + b"\x01\x02\x03" * 4) * 2),
    ],
)
def test_validate_png_rejects_bytes_after_the_single_zlib_stream(
    tmp_path: Path,
    suffix: bytes,
) -> None:
    path = tmp_path / "smuggled.png"
    raw_scanlines = (b"\x00" + b"\x01\x02\x03" * 4) * 2
    payload = _rgb_png_payload(idat_payloads=(zlib.compress(raw_scanlines) + suffix,))

    with pytest.raises(ResourceValidationError, match="bytes after its single zlib stream"):
        size, md5 = _write_payload(path, payload)
        validate_png(path, expected_size=size, provider_md5=md5)


def test_validate_png_streams_one_zlib_stream_across_idat_chunks(tmp_path: Path) -> None:
    path = tmp_path / "split.png"
    raw_scanlines = (b"\x00" + b"\x01\x02\x03" * 4) * 2
    compressed = zlib.compress(raw_scanlines)
    payload = _rgb_png_payload(idat_payloads=(compressed[:1], b"", compressed[1:7], compressed[7:]))
    size, md5 = _write_payload(path, payload)

    evidence = validate_png(path, expected_size=size, provider_md5=md5)

    assert (evidence.width, evidence.height, evidence.channels) == (4, 2, 3)


def test_validate_png_rejects_extra_inflated_scanline_bytes(tmp_path: Path) -> None:
    path = tmp_path / "extra_pixels.png"
    raw_scanlines = (b"\x00" + b"\x01\x02\x03" * 4) * 2 + b"\x00"
    payload = _rgb_png_payload(idat_payloads=(zlib.compress(raw_scanlines),))

    with pytest.raises(ResourceValidationError, match="beyond declared scanlines"):
        size, md5 = _write_payload(path, payload)
        validate_png(path, expected_size=size, provider_md5=md5)


def test_validate_png_accepts_exact_adam7_scanline_stream(tmp_path: Path) -> None:
    path = tmp_path / "adam7.png"
    width = height = 8
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 1)
    pass_dimensions = ((1, 1), (1, 1), (2, 1), (2, 2), (4, 2), (4, 4), (8, 4))
    raw_scanlines = b"".join(
        (b"\x00" + b"\x00" * (pass_width * 3)) * pass_height
        for pass_width, pass_height in pass_dimensions
    )
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw_scanlines))
        + _png_chunk(b"IEND", b"")
    )
    size, md5 = _write_payload(path, payload)

    evidence = validate_png(path, expected_size=size, provider_md5=md5)

    assert (evidence.width, evidence.height) == (8, 8)


def test_validate_png_rejects_trns_after_idat(tmp_path: Path) -> None:
    path = tmp_path / "late_trns.png"
    payload = _rgb_png_payload(after_idat=(_png_chunk(b"tRNS", b"\x00" * 6),))

    with pytest.raises(ResourceValidationError, match="tRNS must appear before IDAT"):
        size, md5 = _write_payload(path, payload)
        validate_png(path, expected_size=size, provider_md5=md5)


def test_validate_png_accepts_single_trns_before_idat(tmp_path: Path) -> None:
    path = tmp_path / "trns.png"
    payload = _rgb_png_payload(before_idat=(_png_chunk(b"tRNS", b"\x00" * 6),))
    size, md5 = _write_payload(path, payload)

    evidence = validate_png(path, expected_size=size, provider_md5=md5)

    assert evidence.mode == "RGB"


def test_validate_png_rejects_duplicate_gamma_metadata(tmp_path: Path) -> None:
    path = tmp_path / "gamma.png"
    gamma = _png_chunk(b"gAMA", struct.pack(">I", 45_455))
    payload = _rgb_png_payload(before_idat=(gamma, gamma))

    with pytest.raises(ResourceValidationError, match="duplicate gAMA"):
        size, md5 = _write_payload(path, payload)
        validate_png(path, expected_size=size, provider_md5=md5)


def test_validate_png_rejects_grayscale_mode(tmp_path: Path) -> None:
    path = tmp_path / "gray.png"
    payload = _write_png(path, mode="L")

    with pytest.raises(ResourceValidationError, match="RGB or RGBA"):
        validate_png(path, expected_size=len(payload), provider_md5=_md5(payload))


def test_validate_png_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.png"
    payload = _write_png(target)
    link = tmp_path / "link.png"
    link.symlink_to(target)

    with pytest.raises(ResourceValidationError, match="must not be a symlink"):
        validate_png(link, expected_size=len(payload), provider_md5=_md5(payload))


def test_validate_png_rejects_provider_size_and_md5_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "provider.png"
    payload = _write_png(path)

    with pytest.raises(ResourceValidationError, match="provider size mismatch"):
        validate_png(path, expected_size=len(payload) + 1, provider_md5=_md5(payload))
    with pytest.raises(ResourceValidationError, match="provider MD5 mismatch"):
        validate_png(path, expected_size=len(payload), provider_md5="0" * 32)


@pytest.mark.parametrize("validator", [validate_png, validate_radiance_hdr])
def test_public_validators_wrap_nul_path_errors(validator: Any) -> None:
    path = Path("invalid\x00resource")

    with pytest.raises(ResourceValidationError, match="cannot stat resource file"):
        validator(path, expected_size=1, provider_md5="0" * 32)


def test_public_validator_wraps_lstat_type_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "resource.png"

    def fail_lstat(self: Path) -> Any:
        raise TypeError("invalid path representation")

    monkeypatch.setattr(Path, "lstat", fail_lstat)
    with pytest.raises(ResourceValidationError, match="cannot stat resource file"):
        validate_png(path, expected_size=1, provider_md5="0" * 32)


def test_public_validator_wraps_open_value_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "resource.png"
    path.write_bytes(b"x")

    def fail_open(path: object, flags: int) -> int:
        raise ValueError("invalid path representation")

    monkeypatch.setattr(resource_validation.os, "open", fail_open)
    with pytest.raises(ResourceValidationError, match="cannot safely open resource file"):
        validate_png(path, expected_size=1, provider_md5=_md5(b"x"))


@pytest.mark.parametrize(
    ("path", "size", "md5", "message"),
    [
        ("texture.png", 1, "0" * 32, "path must be a pathlib.Path"),
        (Path("texture.png"), True, "0" * 32, "expected_size"),
        (Path("texture.png"), 1, "A" * 32, "provider_md5"),
        (Path("texture.png"), 1, "0" * 31, "provider_md5"),
    ],
)
def test_validate_png_rejects_invalid_request_types_before_io(
    path: Any,
    size: Any,
    md5: Any,
    message: str,
) -> None:
    with pytest.raises(ResourceValidationError, match=message):
        validate_png(
            path,
            expected_size=size,
            provider_md5=md5,
        )


def test_validate_pbr_cohort_returns_canonical_role_and_channel_descriptor(
    tmp_path: Path,
) -> None:
    diffuse = tmp_path / "Diffuse.png"
    normal = tmp_path / "nor_dx.png"
    arm = tmp_path / "arm.png"
    for path in (diffuse, normal, arm):
        _write_png(path)

    evidence = validate_pbr_cohort(
        maps=(
            _png_input("arm", arm),
            _png_input("Diffuse", diffuse),
            _png_input("nor_dx", normal),
        ),
        physical_size_mm=(30_000, 30_000),
    )

    assert evidence.descriptor.pixel_dimensions == (4, 2)
    assert evidence.descriptor.physical_size_mm == (30_000.0, 30_000.0)
    assert tuple(item.role for item in evidence.descriptor.maps) == (
        "Diffuse",
        "nor_dx",
        "arm",
    )
    assert tuple(item.image.file.path for item in evidence.maps) == (diffuse, normal, arm)
    diffuse_descriptor, normal_descriptor, arm_descriptor = evidence.descriptor.maps
    assert (diffuse_descriptor.color_space, diffuse_descriptor.semantic) == (
        "srgb",
        "base_color",
    )
    assert (
        normal_descriptor.color_space,
        normal_descriptor.semantic,
        normal_descriptor.normal_convention,
    ) == ("data", "normal", "directx")
    assert arm_descriptor.semantic == "packed_material"
    assert arm_descriptor.channel_semantics == (
        ("r", "ambient_occlusion"),
        ("g", "roughness"),
        ("b", "metallic"),
    )


def test_validate_pbr_cohort_rejects_dimension_mismatch(tmp_path: Path) -> None:
    diffuse = tmp_path / "Diffuse.png"
    normal = tmp_path / "nor_dx.png"
    arm = tmp_path / "arm.png"
    _write_png(diffuse)
    _write_png(normal, size=(8, 2))
    _write_png(arm)

    with pytest.raises(ResourceValidationError, match="dimensions do not match"):
        validate_pbr_cohort(
            maps=(
                _png_input("Diffuse", diffuse),
                _png_input("nor_dx", normal),
                _png_input("arm", arm),
            ),
            physical_size_mm=(30_000.0, 30_000.0),
        )


@pytest.mark.parametrize(
    "roles",
    [
        ("Diffuse", "normal", "arm"),
        ("Diffuse", "Diffuse", "arm"),
        ("Diffuse", "nor_dx"),
    ],
)
def test_validate_pbr_cohort_rejects_invalid_map_roles(
    tmp_path: Path,
    roles: tuple[str, ...],
) -> None:
    paths = tuple(tmp_path / f"{index}.png" for index in range(len(roles)))
    for path in paths:
        _write_png(path)

    with pytest.raises(ResourceValidationError, match="role|exactly three"):
        validate_pbr_cohort(
            maps=tuple(_png_input(role, path) for role, path in zip(roles, paths, strict=True)),
            physical_size_mm=(1.0, 1.0),
        )


@pytest.mark.parametrize(
    "physical_size",
    [
        (1.0,),
        [1.0, 2.0],
        (0.0, 1.0),
        (-1.0, 1.0),
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        (True, 1.0),
        (1_000_001.0, 1.0),
        (10**1000, 1.0),
    ],
)
def test_validate_pbr_cohort_rejects_invalid_physical_size(
    physical_size: Any,
) -> None:
    maps = (
        PbrMapInput("Diffuse", Path("diffuse.png"), 1, "0" * 32),
        PbrMapInput("nor_dx", Path("normal.png"), 1, "0" * 32),
        PbrMapInput("arm", Path("arm.png"), 1, "0" * 32),
    )

    with pytest.raises(ResourceValidationError, match="physical_size_mm"):
        validate_pbr_cohort(
            maps=maps,
            physical_size_mm=physical_size,
        )
