from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import stat
import struct
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from uefactory.acquire import polyhaven_resource_sync
from uefactory.acquire.polyhaven_provider import ProviderFileResult
from uefactory.acquire.polyhaven_resource_sync import (
    POLYHAVEN_RESOURCE_LISTING_URLS,
    sync_polyhaven_resources,
)
from uefactory.acquire.polyhaven_resources import revisioned_resource_id
from uefactory.catalog import Catalog
from uefactory.core.config import Settings

HDR_SOURCE_ID = "studio_small_03"
HDR_REVISION = "d69ec09a43016714fd0dda163b3b0c585c968f56"
HDR_REVISION_2 = "e69ec09a43016714fd0dda163b3b0c585c968f57"
PBR_SOURCE_ID = "aerial_asphalt_01"
PBR_REVISION = "cdf3c8f091b3589407bdf0697a2deb2c6b40650d"


def _md5(payload: bytes) -> str:
    return hashlib.md5(payload, usedforsecurity=False).hexdigest()


def _hdr_payload(*, seed: int = 1, width: int = 8, height: int = 4) -> bytes:
    header = b"#?RADIANCE\nEXPOSURE=1.000000\nFORMAT=32-bit_rle_rgbe\n\n"
    resolution = f"-Y {height} +X {width}\n".encode("ascii")
    scanline = b"\x02\x02" + width.to_bytes(2, "big")
    for component in range(4):
        value = ((seed + component - 1) % 254) + 1
        scanline += bytes((128 + width, value))
    return header + resolution + scanline * height


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF)
    )


def _png_payload(color: tuple[int, int, int], *, size: int = 4) -> bytes:
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    scanlines = (b"\x00" + bytes(color) * size) * size
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )


def _listing_payload(
    *,
    kind: str,
    source_id: str,
    revision: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": 0 if kind == "hdri" else 1,
        "name": "Studio Small 03" if kind == "hdri" else "Aerial Asphalt 01",
        "date_published": 1_700_000_000,
        "files_hash": revision,
        "authors": {"Fixture Author": "fixture-author"},
        "categories": ["studio"] if kind == "hdri" else ["floor", "asphalt"],
        "tags": ["indoor"] if kind == "hdri" else ["road"],
    }
    if kind == "pbr_texture_set":
        payload["dimensions"] = [30_000, 30_000]
    return {source_id: payload}


def _download_entry(filename: str, payload: bytes) -> dict[str, Any]:
    return {
        "url": f"https://dl.polyhaven.org/file/ph-assets/fixtures/{filename}",
        "md5": _md5(payload),
        "size": len(payload),
    }


def _hdri_files_payload(
    payloads: dict[str, bytes],
    *,
    source_id: str = HDR_SOURCE_ID,
) -> dict[str, Any]:
    return {
        "hdri": {
            resolution: {"hdr": _download_entry(f"{source_id}_{resolution}.hdr", payload)}
            for resolution, payload in payloads.items()
        }
    }


def _pbr_files_payload(
    payloads: dict[str, bytes],
    *,
    source_id: str = PBR_SOURCE_ID,
    resolution: str = "1k",
) -> dict[str, Any]:
    suffixes = {"Diffuse": "diff", "nor_dx": "nor_dx", "arm": "arm"}
    return {
        role: {
            resolution: {
                "png": _download_entry(f"{source_id}_{suffixes[role]}_{resolution}.png", payload)
            }
        }
        for role, payload in payloads.items()
    }


class _FakeProviderSession:
    def __init__(
        self,
        *,
        responses: dict[str, dict[str, Any]],
        downloads: dict[str, bytes],
    ) -> None:
        self.responses = responses
        self.downloads = downloads
        self.fetch_urls: list[str] = []
        self.file_calls: list[tuple[str, Path, bool, str]] = []
        self.reservations: list[tuple[str, ...]] = []

    @contextmanager
    def source_lock(self) -> Iterator[Path]:
        yield Path("fake-polyhaven-source.lock")

    def fetch_json(self, url: str) -> dict[str, Any]:
        self.fetch_urls.append(url)
        try:
            return copy.deepcopy(self.responses[url])
        except KeyError as exc:  # pragma: no cover - improves fixture diagnostics
            raise AssertionError(f"unexpected provider JSON request: {url}") from exc

    def acquire_file(
        self,
        spec: Any,
        *,
        destination: Path,
        force: bool,
        item_id: str,
    ) -> ProviderFileResult:
        self.file_calls.append((spec.url, destination, force, item_id))
        try:
            payload = self.downloads[spec.url]
        except KeyError as exc:  # pragma: no cover - improves fixture diagnostics
            raise AssertionError(f"unexpected provider file request: {spec.url}") from exc
        assert spec.bytes == len(payload)
        assert spec.md5 == _md5(payload)
        destination.parent.mkdir(parents=True, exist_ok=True)
        reused = destination.is_file() and not force and destination.read_bytes() == payload
        if not reused:
            destination.write_bytes(payload)
        return ProviderFileResult(
            path=destination,
            sha256=hashlib.sha256(payload).hexdigest(),
            reused=reused,
            downloaded_bytes=0 if reused else len(payload),
        )

    def reserve_items(self, item_ids: tuple[str, ...]) -> None:
        self.reservations.append(item_ids)

    def reserve_items_bounded(
        self, item_ids: tuple[str, ...]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        self.reserve_items(item_ids)
        return item_ids, ()

    def check_disk_growth(self, _growth_bytes: int) -> None:
        return None

    def runtime_evidence(self) -> dict[str, Any]:
        return {
            "fixture": {
                "json_requests": len(self.fetch_urls),
                "file_requests": len(self.file_calls),
                "reservations": sum(len(items) for items in self.reservations),
            }
        }


def _settings(tmp_path: Path) -> Settings:
    return Settings(project_root=tmp_path, data_dir=tmp_path / "data")


def _provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    listing_url: str,
    listing_payload: dict[str, Any],
    source_id: str,
    files_payload: dict[str, Any],
    file_payloads: dict[str, bytes],
) -> _FakeProviderSession:
    files_url = f"https://api.polyhaven.com/files/{source_id}"
    downloads: dict[str, bytes] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if set(value) == {"url", "md5", "size"}:
                downloads[value["url"]] = file_payloads[Path(value["url"]).name]
                return
            for child in value.values():
                visit(child)

    visit(files_payload)
    provider = _FakeProviderSession(
        responses={listing_url: listing_payload, files_url: files_payload},
        downloads=downloads,
    )
    monkeypatch.setattr(
        polyhaven_resource_sync,
        "PolyHavenProviderSession",
        lambda **_kwargs: provider,
    )
    return provider


def _hdri_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    revision: str = HDR_REVISION,
    payloads: dict[str, bytes] | None = None,
) -> _FakeProviderSession:
    checked_payloads = {"1k": _hdr_payload()} if payloads is None else payloads
    files_payload = _hdri_files_payload(checked_payloads)
    return _provider(
        monkeypatch,
        listing_url=POLYHAVEN_RESOURCE_LISTING_URLS["hdri"],
        listing_payload=_listing_payload(kind="hdri", source_id=HDR_SOURCE_ID, revision=revision),
        source_id=HDR_SOURCE_ID,
        files_payload=files_payload,
        file_payloads={
            f"{HDR_SOURCE_ID}_{resolution}.hdr": payload
            for resolution, payload in checked_payloads.items()
        },
    )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fresh_hdri_is_strictly_validated_and_atomically_published_with_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)

    result = sync_polyhaven_resources(
        settings=settings,
        kind="hdri",
        source_ids=(HDR_SOURCE_ID,),
    )

    assert (result.status, result.ready, result.failed) == ("ready", 1, 0)
    item = result.items[0]
    assert item.status == "ready"
    assert (item.downloaded_files, item.reused_files) == (1, 0)
    assert item.verified_bytes == len(_hdr_payload())
    assert item.root_dir is not None and item.root_dir.is_dir()
    assert len(item.file_paths) == 1 and item.file_paths[0].read_bytes() == _hdr_payload()

    resource = Catalog(result.catalog_path, project_root=tmp_path).get_resource(item.resource_id)
    assert resource is not None
    assert (
        resource.status,
        resource.resource_kind,
        resource.profile,
        resource.resolution,
        resource.source_revision,
        resource.license,
    ) == (
        "ready",
        "hdri",
        "radiance_hdr_v1",
        "1k",
        HDR_REVISION,
        "CC0-1.0",
    )
    catalog = Catalog(result.catalog_path, project_root=tmp_path)
    files = catalog.list_resource_files(resource_id=item.resource_id)
    assert len(files) == 1
    assert (
        files[0].semantic_role,
        files[0].provider_role,
        files[0].color_space,
        files[0].width,
        files[0].height,
        files[0].is_primary,
    ) == ("environment_radiance", "hdri", "linear", 8, 4, True)
    artifacts = catalog.list_resource_artifacts(resource_id=item.resource_id)
    assert {artifact.kind for artifact in artifacts} == {
        "resource_source_manifest",
        "hdri_validation_manifest",
    }
    validation_artifact = next(
        artifact for artifact in artifacts if artifact.kind == "hdri_validation_manifest"
    )
    validation = _read_json(tmp_path / validation_artifact.path)
    assert (
        validation["validation_status"],
        validation["width"],
        validation["height"],
        validation["encoding"],
        validation["scanlines"],
    ) == ("passed", 8, 4, "modern_rle_rgbe", 4)

    assert item.compatibility_path is not None
    assert item.compatibility_metadata_path is not None
    assert stat.S_ISREG(item.compatibility_path.lstat().st_mode)
    assert not item.compatibility_path.is_symlink()
    assert item.compatibility_path.read_bytes() == item.file_paths[0].read_bytes()
    canonical_stat = item.file_paths[0].stat()
    compatibility_stat = item.compatibility_path.stat()
    assert (canonical_stat.st_dev, canonical_stat.st_ino) != (
        compatibility_stat.st_dev,
        compatibility_stat.st_ino,
    )
    metadata = _read_json(item.compatibility_metadata_path)
    assert metadata["schema_version"] == 2
    assert metadata["resource_id"] == item.resource_id
    assert metadata["canonical_file"] == item.file_paths[0].relative_to(tmp_path).as_posix()

    state = _read_json(result.state_path)
    assert set(state["items"]) == {item.resource_id}
    assert state["items"][item.resource_id]["catalog"]["projection_sha256"]
    manifest = _read_json(result.manifest_path)
    assert manifest["status"] == "ready"
    assert manifest["active_attempt"] is None
    assert manifest["items"][0]["status"] == "ready"
    assert provider.fetch_urls == [
        POLYHAVEN_RESOURCE_LISTING_URLS["hdri"],
        f"https://api.polyhaven.com/files/{HDR_SOURCE_ID}",
    ]
    assert len(provider.file_calls) == 1


def test_exact_hdri_replay_skips_files_endpoint_and_download_and_preserves_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)
    first = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    catalog_before = _sha256(first.catalog_path)
    record_before = Catalog(first.catalog_path, project_root=tmp_path).get_resource(
        first.items[0].resource_id
    )
    provider.fetch_urls.clear()
    provider.file_calls.clear()

    replay = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))

    assert replay.status == "ready"
    assert (replay.ready, replay.skipped, replay.failed) == (0, 1, 0)
    assert replay.items[0].status == "skipped"
    assert replay.items[0].downloaded_files == 0
    assert provider.fetch_urls == [POLYHAVEN_RESOURCE_LISTING_URLS["hdri"]]
    assert provider.file_calls == []
    assert _sha256(replay.catalog_path) == catalog_before
    assert (
        Catalog(replay.catalog_path, project_root=tmp_path).get_resource(
            replay.items[0].resource_id
        )
        == record_before
    )


def test_hdri_alias_mutation_cannot_change_canonical_and_replay_repairs_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)
    first = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    canonical = first.items[0].file_paths[0]
    alias = first.items[0].compatibility_path
    assert alias is not None
    canonical_before = canonical.read_bytes()

    alias.write_bytes(b"mutated compatibility copy")

    assert canonical.read_bytes() == canonical_before
    replay = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    assert replay.items[0].status == "skipped"
    assert alias.read_bytes() == canonical_before


def test_hdri_alias_parent_symlink_cannot_write_outside_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)
    external = tmp_path.parent / f"{tmp_path.name}_external"
    external.mkdir()
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "hdri").symlink_to(external, target_is_directory=True)

    result = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))

    assert (result.status, result.failed) == ("failed", 1)
    assert list(external.iterdir()) == []
    assert Catalog(result.catalog_path, project_root=tmp_path).list_resources() == ()


def test_fresh_pbr_maps_have_exact_ue_roles_channels_dimensions_and_ready_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payloads = {
        "Diffuse": _png_payload((160, 120, 80)),
        "nor_dx": _png_payload((128, 128, 255)),
        "arm": _png_payload((255, 100, 0)),
    }
    files_payload = _pbr_files_payload(payloads)
    provider = _provider(
        monkeypatch,
        listing_url=POLYHAVEN_RESOURCE_LISTING_URLS["pbr_texture_set"],
        listing_payload=_listing_payload(
            kind="pbr_texture_set",
            source_id=PBR_SOURCE_ID,
            revision=PBR_REVISION,
        ),
        source_id=PBR_SOURCE_ID,
        files_payload=files_payload,
        file_payloads={
            f"{PBR_SOURCE_ID}_diff_1k.png": payloads["Diffuse"],
            f"{PBR_SOURCE_ID}_nor_dx_1k.png": payloads["nor_dx"],
            f"{PBR_SOURCE_ID}_arm_1k.png": payloads["arm"],
        },
    )

    result = sync_polyhaven_resources(
        settings=_settings(tmp_path),
        kind="pbr_texture_set",
        source_ids=(PBR_SOURCE_ID,),
    )

    assert (result.status, result.ready) == ("ready", 1)
    item = result.items[0]
    assert item.downloaded_files == 3
    assert item.compatibility_path is None
    catalog = Catalog(result.catalog_path, project_root=tmp_path)
    resource = catalog.get_resource(item.resource_id)
    assert resource is not None
    assert (
        resource.status,
        resource.profile,
        resource.physical_size_mm,
        resource.source_revision,
    ) == ("ready", "ue_pbr_png_v1", (30_000.0, 30_000.0), PBR_REVISION)
    by_semantic = {
        file.semantic_role: file
        for file in catalog.list_resource_files(resource_id=item.resource_id)
    }
    assert set(by_semantic) == {"base_color", "normal", "packed_material"}
    assert (
        by_semantic["base_color"].provider_role,
        by_semantic["base_color"].color_space,
        by_semantic["base_color"].is_primary,
    ) == ("Diffuse", "srgb", True)
    assert (
        by_semantic["normal"].provider_role,
        by_semantic["normal"].color_space,
        by_semantic["normal"].normal_convention,
    ) == ("nor_dx", "data", "directx")
    assert by_semantic["packed_material"].provider_role == "arm"
    assert by_semantic["packed_material"].channels == {
        "r": "ambient_occlusion",
        "g": "roughness",
        "b": "metallic",
    }
    assert {(file.width, file.height) for file in by_semantic.values()} == {(4, 4)}
    artifacts = catalog.list_resource_artifacts(resource_id=item.resource_id)
    assert {artifact.kind for artifact in artifacts} == {
        "resource_source_manifest",
        "pbr_material_descriptor",
        "pbr_validation_manifest",
    }
    descriptor_record = next(
        artifact for artifact in artifacts if artifact.kind == "pbr_material_descriptor"
    )
    descriptor = _read_json(tmp_path / descriptor_record.path)
    assert descriptor["pixel_dimensions"] == [4, 4]
    assert descriptor["physical_size_mm"] == [30_000.0, 30_000.0]
    assert [entry["role"] for entry in descriptor["maps"]] == [
        "Diffuse",
        "nor_dx",
        "arm",
    ]
    assert len(provider.file_calls) == 3


def test_quality_invalid_resource_is_permanently_quarantined_without_catalog_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _hdri_provider(monkeypatch, payloads={"1k": b"not-a-radiance-hdr"})

    result = sync_polyhaven_resources(
        settings=_settings(tmp_path),
        kind="hdri",
        source_ids=(HDR_SOURCE_ID,),
    )

    assert (result.status, result.failed, result.ready) == ("failed", 1, 0)
    assert result.items[0].status == "failed"
    journal = _read_json(result.failure_journal_path)
    assert len(journal["events"]) == 1
    event = journal["events"][0]
    assert event["failure"]["kind"] == "quality"
    assert event["failure"]["category"] == "permanent"
    assert event["disposition"] == "quarantined"
    assert event["next_eligible_at"] is None
    state = _read_json(result.state_path)
    assert state["items"] == {}
    resource_id = revisioned_resource_id("hdri", HDR_SOURCE_ID, HDR_REVISION, "1k")
    assert Catalog(result.catalog_path, project_root=tmp_path).get_resource(resource_id) is None
    assert len(provider.file_calls) == 1


def test_catalog_finalization_exception_leaves_no_terminal_state_and_failed_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _hdri_provider(monkeypatch)

    def fail_finalize(_self: Catalog, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("injected catalog commit failure")

    monkeypatch.setattr(Catalog, "finalize_resource", fail_finalize)
    with pytest.raises(RuntimeError, match="injected catalog commit failure"):
        sync_polyhaven_resources(
            settings=_settings(tmp_path),
            kind="hdri",
            source_ids=(HDR_SOURCE_ID,),
        )

    state_path = tmp_path / "data/acquire/polyhaven/resources/hdri/state.json"
    state = _read_json(state_path)
    assert state["items"] == {}
    assert state["run_receipts"] == {}
    manifests = tuple((tmp_path / "out/acquire/polyhaven-resources/hdri").glob("*/manifest.json"))
    assert len(manifests) == 1
    manifest = _read_json(manifests[0])
    assert manifest["status"] == "failed"
    assert manifest["active_attempt"] is None
    assert manifest["error"] == {
        "type": "RuntimeError",
        "message": "injected catalog commit failure",
    }
    assert not (tmp_path / "data/catalog.db").exists()


@pytest.mark.parametrize("tamper", ["state", "file", "artifact", "catalog"])
def test_terminal_replay_fails_closed_on_state_file_artifact_or_catalog_tamper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tamper: str,
) -> None:
    provider = _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)
    first = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    item = first.items[0]
    state = _read_json(first.state_path)
    ready_at = state["items"][item.resource_id]["ready_at"]
    if tamper == "state":
        state["items"][item.resource_id]["bundle_sha256"] = "0" * 64
        first.state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif tamper == "file":
        item.file_paths[0].write_bytes(b"tampered provider file")
    elif tamper == "artifact":
        item.artifact_paths[0].write_text("{}\n", encoding="utf-8")
    else:
        with sqlite3.connect(first.catalog_path) as connection:
            connection.execute(
                "UPDATE resources SET name = ? WHERE resource_id = ?",
                ("Tampered Resource", item.resource_id),
            )
            connection.commit()
    provider.fetch_urls.clear()
    provider.file_calls.clear()

    replay = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))

    assert (replay.status, replay.failed, replay.skipped, replay.ready) == (
        "failed",
        1,
        0,
        0,
    )
    assert replay.items[0].status == "failed"
    assert provider.fetch_urls == [POLYHAVEN_RESOURCE_LISTING_URLS["hdri"]]
    assert provider.file_calls == []
    after_state = _read_json(replay.state_path)
    assert set(after_state["items"]) == {item.resource_id}
    assert after_state["items"][item.resource_id]["ready_at"] == ready_at
    assert len(Catalog(replay.catalog_path, project_root=tmp_path).list_resources()) == 1
    journal = _read_json(replay.failure_journal_path)
    assert journal["events"][-1]["disposition"] == "quarantined"


def test_force_redownload_verifies_isolated_candidate_without_mutating_ready_cohort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)
    first = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    canonical = first.items[0].file_paths[0]
    catalog_before = _sha256(first.catalog_path)
    canonical_before = _sha256(canonical)
    provider.storage_root = settings.data_dir / "acquire/polyhaven"
    provider.project_root = settings.project_root
    provider.file_calls.clear()

    forced = sync_polyhaven_resources(
        settings=settings,
        kind="hdri",
        source_ids=(HDR_SOURCE_ID,),
        force=True,
    )

    item = forced.items[0]
    assert (item.status, item.downloaded_files, item.reused_files) == ("ready", 1, 0)
    assert item.downloaded_bytes == len(_hdr_payload())
    assert _sha256(canonical) == canonical_before
    assert _sha256(forced.catalog_path) == catalog_before
    assert not (
        settings.data_dir / "acquire/polyhaven/resources/.force_verify" / item.resource_id
    ).exists()


def test_revision_and_resolution_have_isolated_ids_roots_state_and_catalog_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload_1k_v1 = _hdr_payload(seed=1)
    payload_2k_v1 = _hdr_payload(seed=20)
    provider = _hdri_provider(
        monkeypatch,
        payloads={"1k": payload_1k_v1, "2k": payload_2k_v1},
    )
    settings = _settings(tmp_path)
    first = sync_polyhaven_resources(
        settings=settings,
        kind="hdri",
        source_ids=(HDR_SOURCE_ID,),
        resolution="1k",
    )
    second = sync_polyhaven_resources(
        settings=settings,
        kind="hdri",
        source_ids=(HDR_SOURCE_ID,),
        resolution="2k",
    )

    payload_1k_v2 = _hdr_payload(seed=40)
    provider.responses[POLYHAVEN_RESOURCE_LISTING_URLS["hdri"]] = _listing_payload(
        kind="hdri", source_id=HDR_SOURCE_ID, revision=HDR_REVISION_2
    )
    files_v2 = _hdri_files_payload({"1k": payload_1k_v2})
    provider.responses[f"https://api.polyhaven.com/files/{HDR_SOURCE_ID}"] = files_v2
    file_entry = files_v2["hdri"]["1k"]["hdr"]
    provider.downloads[file_entry["url"]] = payload_1k_v2
    third = sync_polyhaven_resources(
        settings=settings,
        kind="hdri",
        source_ids=(HDR_SOURCE_ID,),
        resolution="1k",
    )

    items = (first.items[0], second.items[0], third.items[0])
    assert all(item.status == "ready" for item in items)
    assert len({item.resource_id for item in items}) == 3
    assert len({item.root_dir for item in items}) == 3
    assert all(item.root_dir is not None and item.root_dir.is_dir() for item in items)
    assert items[0].file_paths[0].read_bytes() == payload_1k_v1
    assert items[1].file_paths[0].read_bytes() == payload_2k_v1
    assert items[2].file_paths[0].read_bytes() == payload_1k_v2
    state = _read_json(third.state_path)
    assert set(state["items"]) == {item.resource_id for item in items}
    catalog = Catalog(third.catalog_path, project_root=tmp_path)
    records = catalog.list_resources(resource_kind="hdri")
    assert {record.resource_id for record in records} == {item.resource_id for item in items}
    assert {(record.source_revision, record.resolution) for record in records} == {
        (HDR_REVISION, "1k"),
        (HDR_REVISION, "2k"),
        (HDR_REVISION_2, "1k"),
    }


def test_item_quota_deferral_is_manifested_without_files_or_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _hdri_provider(monkeypatch)
    monkeypatch.setattr(
        provider,
        "reserve_items_bounded",
        lambda item_ids: ((), item_ids),
    )

    result = sync_polyhaven_resources(
        settings=_settings(tmp_path),
        kind="hdri",
        source_ids=(HDR_SOURCE_ID,),
    )

    assert (result.status, result.deferred, result.ready) == ("deferred", 1, 0)
    assert result.items[0].status == "deferred"
    assert provider.fetch_urls == [POLYHAVEN_RESOURCE_LISTING_URLS["hdri"]]
    assert provider.file_calls == []
    assert not result.catalog_path.exists()
    assert _read_json(result.manifest_path)["items"][0]["status"] == "deferred"


def test_post_catalog_compatibility_intent_recovers_without_failure_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)
    original_commit = polyhaven_resource_sync._commit_prepared_hdri_compatibility

    def fail_commit(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("injected compatibility rename failure")

    monkeypatch.setattr(
        polyhaven_resource_sync,
        "_commit_prepared_hdri_compatibility",
        fail_commit,
    )
    with pytest.raises(
        polyhaven_resource_sync.PolyHavenResourceCommitPendingError,
        match="pending HDRI compatibility commit",
    ):
        sync_polyhaven_resources(
            settings=settings,
            kind="hdri",
            source_ids=(HDR_SOURCE_ID,),
        )

    resource_id = revisioned_resource_id("hdri", HDR_SOURCE_ID, HDR_REVISION, "1k")
    assert (
        Catalog(settings.data_dir / "catalog.db", project_root=tmp_path).get_resource(resource_id)
        is not None
    )
    state_path = settings.data_dir / "acquire/polyhaven/resources/hdri/state.json"
    assert _read_json(state_path)["items"] == {}
    journal_path = settings.data_dir / "acquire/polyhaven/resources/hdri/failure_journal.json"
    assert _read_json(journal_path)["events"] == []
    intent_path = (
        settings.data_dir
        / "acquire/polyhaven/resources/hdri/compatibility_intents"
        / f"{resource_id}.json"
    )
    assert _read_json(intent_path)["status"] == "catalog_committed"
    failed_manifest = next(
        (tmp_path / "out/acquire/polyhaven-resources/hdri").glob("*/manifest.json")
    )
    assert _read_json(failed_manifest)["status"] == "interrupted"

    monkeypatch.setattr(
        polyhaven_resource_sync,
        "_commit_prepared_hdri_compatibility",
        original_commit,
    )
    recovered = sync_polyhaven_resources(
        settings=settings,
        kind="hdri",
        source_ids=(HDR_SOURCE_ID,),
    )

    assert recovered.items[0].status == "skipped"
    assert resource_id in _read_json(state_path)["items"]
    assert not intent_path.exists()
    assert recovered.items[0].compatibility_path is not None
    assert recovered.items[0].compatibility_path.is_file()


def test_catalog_ready_state_gap_is_recovered_before_listing_revision_moves(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)
    first = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    first_id = first.items[0].resource_id
    state = _read_json(first.state_path)
    state["items"].pop(first_id)
    first.state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    revision_two_payload = _hdr_payload(seed=77)
    provider.responses[POLYHAVEN_RESOURCE_LISTING_URLS["hdri"]] = _listing_payload(
        kind="hdri", source_id=HDR_SOURCE_ID, revision=HDR_REVISION_2
    )
    files_two = _hdri_files_payload({"1k": revision_two_payload})
    provider.responses[f"https://api.polyhaven.com/files/{HDR_SOURCE_ID}"] = files_two
    entry = files_two["hdri"]["1k"]["hdr"]
    provider.downloads[entry["url"]] = revision_two_payload

    second = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))

    state_after = _read_json(second.state_path)
    assert set(state_after["items"]) == {first_id, second.items[0].resource_id}
    assert len(Catalog(second.catalog_path, project_root=tmp_path).list_resources()) == 2


def test_catalog_gap_recovery_of_historical_revision_does_not_roll_back_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)
    first = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    first_id = first.items[0].resource_id

    revision_two_payload = _hdr_payload(seed=77)
    provider.responses[POLYHAVEN_RESOURCE_LISTING_URLS["hdri"]] = _listing_payload(
        kind="hdri", source_id=HDR_SOURCE_ID, revision=HDR_REVISION_2
    )
    files_two = _hdri_files_payload({"1k": revision_two_payload})
    provider.responses[f"https://api.polyhaven.com/files/{HDR_SOURCE_ID}"] = files_two
    entry = files_two["hdri"]["1k"]["hdr"]
    provider.downloads[entry["url"]] = revision_two_payload
    second = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    second_id = second.items[0].resource_id
    alias = settings.data_dir / "hdri" / f"{HDR_SOURCE_ID}_1k.hdr"
    metadata = alias.with_suffix(".json")
    assert alias.read_bytes() == revision_two_payload
    assert _read_json(metadata)["resource_id"] == second_id

    state = _read_json(second.state_path)
    state["items"].pop(first_id)
    second.state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    replay = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))

    assert replay.items[0].resource_id == second_id
    assert alias.read_bytes() == revision_two_payload
    assert _read_json(metadata)["resource_id"] == second_id
    assert first_id in _read_json(replay.state_path)["items"]


def test_catalog_gap_recovery_rejects_tampered_file_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)
    first = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    resource_id = first.items[0].resource_id
    state = _read_json(first.state_path)
    state["items"].pop(resource_id)
    first.state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with sqlite3.connect(first.catalog_path) as connection:
        connection.execute(
            "UPDATE resource_files SET width = 999 WHERE resource_id = ?",
            (resource_id,),
        )

    with pytest.raises(
        polyhaven_resource_sync.PolyHavenResourceSyncError,
        match="file semantics changed",
    ):
        sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    assert resource_id not in _read_json(first.state_path)["items"]


def test_listing_evidence_mode_is_cryptographically_bound_and_live_drift_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)
    first = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    resource_id = first.items[0].resource_id
    state = _read_json(first.state_path)
    state["items"][resource_id]["listing_evidence"]["mode"] = "catalog_recovery"
    first.state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(
        polyhaven_resource_sync.PolyHavenResourceSyncError,
        match="listing evidence is invalid",
    ):
        sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))

    state["items"][resource_id]["listing_evidence"] = polyhaven_resource_sync._listing_evidence(
        polyhaven_resource_sync.parse_polyhaven_resource_listing(
            provider.responses[POLYHAVEN_RESOURCE_LISTING_URLS["hdri"]], "hdri"
        )[0],
        mode="live",
    )
    first.state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    drifted = copy.deepcopy(provider.responses[POLYHAVEN_RESOURCE_LISTING_URLS["hdri"]])
    drifted[HDR_SOURCE_ID]["authors"] = {"Fixture Author": "changed-credit"}
    provider.responses[POLYHAVEN_RESOURCE_LISTING_URLS["hdri"]] = drifted
    result = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    assert (result.status, result.failed) == ("failed", 1)
    assert result.items[0].error is not None
    assert "files_hash revision" in result.items[0].error["failure"]["message"]


def test_catalog_recovery_evidence_is_upgraded_on_first_matching_live_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)
    first = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    resource_id = first.items[0].resource_id
    state = _read_json(first.state_path)
    state["items"].pop(resource_id)
    first.state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    replay = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))

    evidence = _read_json(replay.state_path)["items"][resource_id]["listing_evidence"]
    assert evidence["mode"] == "live"
    assert evidence["sha256"] == polyhaven_resource_sync._domain_sha256(
        polyhaven_resource_sync._LISTING_EVIDENCE_DOMAIN,
        {"mode": "live", "projection": evidence["projection"]},
    )


@pytest.mark.parametrize("drift", ["author_credit", "date_published"])
def test_catalog_recovery_compares_immutable_source_listing_before_live_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drift: str,
) -> None:
    provider = _hdri_provider(monkeypatch)
    settings = _settings(tmp_path)
    first = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    resource_id = first.items[0].resource_id
    state = _read_json(first.state_path)
    state["items"].pop(resource_id)
    first.state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    listing = copy.deepcopy(provider.responses[POLYHAVEN_RESOURCE_LISTING_URLS["hdri"]])
    if drift == "author_credit":
        listing[HDR_SOURCE_ID]["authors"] = {"Fixture Author": "changed-credit"}
    else:
        listing[HDR_SOURCE_ID]["date_published"] += 1
    provider.responses[POLYHAVEN_RESOURCE_LISTING_URLS["hdri"]] = listing

    result = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))

    assert (result.status, result.failed) == ("failed", 1)
    assert result.items[0].error is not None
    assert "immutable source manifest" in result.items[0].error["failure"]["message"]
    evidence = _read_json(result.state_path)["items"][resource_id]["listing_evidence"]
    assert evidence["mode"] == "catalog_recovery"


def test_stale_running_manifest_recovers_missing_durable_journal_reference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _hdri_provider(monkeypatch, payloads={"1k": b"not-a-radiance-hdr"})
    settings = _settings(tmp_path)
    failed = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))
    manifest = _read_json(failed.manifest_path)
    assert len(manifest["journal_event_refs"]) == 1
    expected_ref = manifest["journal_event_refs"][0]
    manifest.update(
        {
            "status": "running",
            "completed_at": None,
            "journal_event_refs": [],
            "runtime": None,
            "error": None,
        }
    )
    failed.manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    state_before = _read_json(failed.state_path)
    state_before["run_receipts"].pop(failed.run_id)
    failed.state_path.write_text(
        json.dumps(state_before, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    replay = sync_polyhaven_resources(settings=settings, kind="hdri", source_ids=(HDR_SOURCE_ID,))

    reconciled = _read_json(failed.manifest_path)
    assert reconciled["status"] == "interrupted"
    assert reconciled["journal_event_refs"] == [expected_ref]
    assert replay.status == "deferred"
    state = _read_json(replay.state_path)
    assert state["run_receipts"][failed.run_id]["status"] == "interrupted"
