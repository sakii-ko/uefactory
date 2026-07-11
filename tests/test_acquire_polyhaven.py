from __future__ import annotations

import copy
import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from uefactory.acquire import polyhaven
from uefactory.acquire.polyhaven import (
    PolyHavenAcquireError,
    PolyHavenFileSpec,
    finalize_polyhaven_items,
    parse_polyhaven_model_files,
    parse_polyhaven_model_listing,
    revisioned_asset_id,
    sync_polyhaven_models,
)
from uefactory.catalog import ArtifactUpsert, AssetUpsert, Catalog
from uefactory.core.config import Settings
from uefactory.ingest.spec import load_ingest_spec
from uefactory.ingest.staging import stage_asset

REVISION = "a" * 40
SECOND_REVISION = "b" * 40


@dataclass
class _FakeClock:
    wall: datetime = datetime(2026, 7, 11, tzinfo=UTC)
    monotonic_value: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def utc_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.monotonic_value += seconds
        self.wall += timedelta(seconds=seconds)


def _md5(payload: bytes) -> str:
    return hashlib.md5(payload, usedforsecurity=False).hexdigest()


def _listing_entry(
    *,
    name: str = "Fixture Model",
    revision: str = REVISION,
    published: int = 1_700_000_000,
) -> dict[str, Any]:
    return {
        "name": name,
        "type": 2,
        "date_published": published,
        "files_hash": revision,
        "authors": {"Fixture Author": "All"},
        "categories": ["Props", "Man Made"],
        "tags": ["Painted Metal", "fixture"],
    }


def _file_entry(url: str, payload: bytes) -> dict[str, Any]:
    return {"url": url, "size": len(payload), "md5": _md5(payload)}


def _gltf_payload(source_id: str, *, binary: bytes, texture: bytes) -> bytes:
    del texture
    return json.dumps(
        {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": []}],
            "buffers": [{"uri": f"{source_id}.bin", "byteLength": len(binary)}],
            "images": [{"uri": f"textures/{source_id}_diff_1k.jpg"}],
        },
        separators=(",", ":"),
    ).encode()


def _files_payload(
    source_id: str,
    *,
    main: bytes,
    binary: bytes,
    texture: bytes,
) -> dict[str, Any]:
    base = "https://dl.polyhaven.org/file/ph-assets/Models"
    main_name = f"{source_id}_1k.gltf"
    return {
        "gltf": {
            "1k": {
                "gltf": {
                    **_file_entry(f"{base}/gltf/1k/{source_id}/{main_name}", main),
                    "include": {
                        f"{source_id}.bin": _file_entry(
                            f"{base}/gltf/8k/{source_id}/{source_id}.bin",
                            binary,
                        ),
                        f"textures/{source_id}_diff_1k.jpg": _file_entry(
                            f"{base}/jpg/1k/{source_id}/{source_id}_diff_1k.jpg",
                            texture,
                        ),
                    },
                }
            }
        }
    }


class _Response(io.BytesIO):
    def __init__(
        self,
        payload: bytes,
        *,
        url: str,
        status: int = 200,
        headers: Any | None = None,
    ) -> None:
        super().__init__(payload)
        self._url = url
        self.status = status
        self.headers = headers or {}

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class _InterruptedResponse(_Response):
    def __init__(self, payload: bytes, *, split: int, url: str) -> None:
        super().__init__(payload, url=url)
        self._split = split
        self._delivered = False

    def read(self, size: int = -1) -> bytes:
        if self._delivered:
            raise KeyboardInterrupt("fixture process interruption")
        self._delivered = True
        return super().read(min(size, self._split))


def _install_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    listing: dict[str, Any],
    file_payloads: dict[str, dict[str, Any]],
    downloads: dict[str, bytes],
) -> list[tuple[str, str | None]]:
    calls: list[tuple[str, str | None]] = []
    clock = _FakeClock()
    monkeypatch.setattr(polyhaven, "SystemClock", lambda: clock)

    def fake_open_url(
        request: Any,
        *,
        timeout: int,
        allowed_hosts: frozenset[str],
    ) -> _Response:
        url = request.full_url
        range_header = request.get_header("Range")
        calls.append((url, range_header))
        if url == polyhaven.POLYHAVEN_MODELS_URL:
            assert timeout == 60
            assert allowed_hosts == frozenset({"api.polyhaven.com"})
            return _Response(json.dumps(listing).encode(), url=url)
        prefix = "https://api.polyhaven.com/files/"
        if url.startswith(prefix):
            assert timeout == 60
            assert allowed_hosts == frozenset({"api.polyhaven.com"})
            source_id = url.removeprefix(prefix)
            return _Response(json.dumps(file_payloads[source_id]).encode(), url=url)
        assert timeout == 300
        assert allowed_hosts == frozenset({"dl.polyhaven.org"})
        payload = downloads[url]
        if range_header is None:
            return _Response(payload, url=url)
        offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
        return _Response(
            payload[offset:],
            url=url,
            status=206,
            headers={"Content-Range": f"bytes {offset}-{len(payload) - 1}/{len(payload)}"},
        )

    monkeypatch.setattr(polyhaven, "_open_url", fake_open_url)
    return calls


def _settings(tmp_path: Path) -> Settings:
    return Settings(project_root=tmp_path, data_dir=tmp_path / "data")


def _model_network_fixture(
    source_id: str = "fixture_model",
) -> tuple[bytes, bytes, bytes, dict[str, Any], dict[str, bytes]]:
    binary = b"binary-payload"
    texture = b"jpeg-payload"
    main = _gltf_payload(source_id, binary=binary, texture=texture)
    files = _files_payload(source_id, main=main, binary=binary, texture=texture)
    entries = [
        files["gltf"]["1k"]["gltf"],
        *files["gltf"]["1k"]["gltf"]["include"].values(),
    ]
    downloads = dict(
        zip(
            (entry["url"] for entry in entries),
            (main, binary, texture),
            strict=True,
        )
    )
    return main, binary, texture, files, downloads


def _sync_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Settings, Any, list[tuple[str, str | None]]]:
    source_id = "fixture_model"
    binary = b"binary-payload"
    texture = b"jpeg-payload"
    main = _gltf_payload(source_id, binary=binary, texture=texture)
    files = _files_payload(source_id, main=main, binary=binary, texture=texture)
    entries = [
        files["gltf"]["1k"]["gltf"],
        *files["gltf"]["1k"]["gltf"]["include"].values(),
    ]
    calls = _install_api(
        monkeypatch,
        listing={source_id: _listing_entry()},
        file_payloads={source_id: files},
        downloads=dict(
            zip(
                (entry["url"] for entry in entries),
                (main, binary, texture),
                strict=True,
            )
        ),
    )
    settings = _settings(tmp_path)
    return settings, sync_polyhaven_models(settings=settings), calls


def _finalized_import_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Any, Path, Path, Path, str]:
    _, result, _ = _sync_fixture(monkeypatch, tmp_path)
    assert result.generated_spec_path is not None
    asset_spec = load_ingest_spec(result.generated_spec_path).assets[0]
    staged = stage_asset(asset_spec, raw_root=tmp_path / "data/raw/local")
    item = result.items[0]
    package_evidence = {"package_bundle_sha256": "d" * 64}
    imported_object_path = f"/Game/UEFactory/{item.asset_id}/{item.asset_id}.{item.asset_id}"
    import_manifest = {
        "schema_version": polyhaven.IMPORT_MANIFEST_SCHEMA_VERSION,
        "status": "ok",
        "asset_id": item.asset_id,
        "bundle_sha256": item.source_bundle_sha256,
        "content_sha256": item.source_content_sha256,
        "requested_normalization": asset_spec.normalization.as_dict(),
        "transaction": {"state": "committed"},
        "finalize_validation": {"status": "ok"},
        "quality": {},
        "imported_object_paths": [imported_object_path],
        "ue_package_bundle": package_evidence,
        "static_meshes": [
            {
                "object_path": imported_object_path,
                "triangle_count": 12,
                "material_count": 1,
            }
        ],
    }
    import_path = tmp_path / f"out/ingest/{item.asset_id}/manifest.json"
    polyhaven._write_json_atomic(import_path, import_manifest)
    catalog_path = tmp_path / "data/catalog.db"
    catalog = Catalog(catalog_path, project_root=tmp_path)
    artifact_id = f"{item.asset_id}_import"
    catalog.finalize_import(
        AssetUpsert(
            asset_id=item.asset_id,
            name=asset_spec.name,
            source=asset_spec.source,
            source_id=asset_spec.source_id,
            source_url=asset_spec.source_url,
            license=asset_spec.license,
            license_tier=asset_spec.license_tier,
            license_url=asset_spec.license_url,
            attribution=asset_spec.attribution,
            status="imported",
            tags=asset_spec.tags,
            raw_path=staged.raw_path,
            ue_package_path=imported_object_path,
            tri_count=12,
            material_count=1,
            sha256=item.source_content_sha256,
        ),
        ArtifactUpsert(
            artifact_id=artifact_id,
            asset_id=item.asset_id,
            kind="import_manifest",
            path=import_path,
            params={
                "schema_version": 2,
                "bundle_sha256": item.source_bundle_sha256,
                "content_sha256": item.source_content_sha256,
                "source_format": "gltf",
                "requested_normalization": asset_spec.normalization.as_dict(),
                "ue_package_bundle": package_evidence,
            },
            sha256=polyhaven._sha256_file(import_path),
        ),
    )
    batch_path = tmp_path / "out/ingest_batches/fixture/manifest.json"
    polyhaven._write_json_atomic(
        batch_path,
        {
            "schema_version": 1,
            "status": "ok",
            "source_manifest": str(result.generated_spec_path),
            "catalog": str(catalog_path),
            "assets": [
                {
                    "asset_id": item.asset_id,
                    "status": "imported",
                    "bundle_sha256": item.source_bundle_sha256,
                    "content_sha256": item.source_content_sha256,
                    "raw_path": str(staged.raw_path),
                    "ingest_manifest": str(import_path),
                    "thumbnail_manifest": None,
                    "catalog_status": "imported",
                    "error": None,
                }
            ],
            "report": None,
            "report_error": None,
        },
    )
    monkeypatch.setattr(polyhaven, "is_current_passed_quality", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        polyhaven,
        "is_valid_package_bundle_evidence",
        lambda *args, **kwargs: True,
    )
    assert finalize_polyhaven_items(result=result, batch_manifest_path=batch_path) == {
        item.asset_id: "imported"
    }
    return result, batch_path, catalog_path, staged.raw_path, artifact_id


def test_listing_is_strict_sorted_and_revisioned() -> None:
    payload = {
        "new_model": _listing_entry(
            name="New Model",
            revision=SECOND_REVISION,
            published=20,
        ),
        "ArmChair_01": _listing_entry(name="Arm Chair", published=10),
    }

    models = parse_polyhaven_model_listing(payload)

    assert [item.source_id for item in models] == ["ArmChair_01", "new_model"]
    assert models[0].asset_id == f"polyhaven_armchair_01_{REVISION[:12]}"
    assert models[1].asset_id == f"polyhaven_new_model_{SECOND_REVISION[:12]}"
    assert revisioned_asset_id("ArmChair_01", REVISION) == models[0].asset_id
    assert models[0].authors == (("Fixture Author", "All"),)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "listing is empty"),
        ({"fixture": {**_listing_entry(), "type": True}}, "type must be integer 2"),
        (
            {"fixture": {**_listing_entry(), "files_hash": "A" * 40}},
            "lowercase 40-character SHA-1",
        ),
        (
            {"Chair": _listing_entry(), "chair": _listing_entry()},
            "collide after lowercase normalization",
        ),
        ({"bad-id": _listing_entry()}, "safe Poly Haven identifier"),
    ],
)
def test_listing_rejects_schema_and_identity_violations(
    payload: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(PolyHavenAcquireError, match=message):
        parse_polyhaven_model_listing(payload)


def test_files_select_exact_1k_gltf_include_closure() -> None:
    main = b"gltf"
    binary = b"binary"
    texture = b"jpeg"
    payload = _files_payload(
        "fixture_model",
        main=main,
        binary=binary,
        texture=texture,
    )

    package = parse_polyhaven_model_files("fixture_model", payload)

    assert package.main_file == Path("fixture_model_1k.gltf")
    assert package.dependencies == (
        Path("fixture_model.bin"),
        Path("textures/fixture_model_diff_1k.jpg"),
    )
    assert [item.bytes for item in package.files] == [len(main), len(binary), len(texture)]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("traversal", "safe relative path"),
        ("host", "unapproved HTTPS host"),
        ("md5", "lowercase 32-character MD5"),
        ("extra", "unsupported key"),
        ("missing_resolution", "has no 1k glTF package"),
        ("metadata", "reserved metadata/temp storage"),
        ("partial", "reserved metadata/temp storage"),
    ],
)
def test_files_reject_unsafe_or_unverifiable_entries(mutation: str, message: str) -> None:
    payload = _files_payload(
        "fixture_model",
        main=b"gltf",
        binary=b"binary",
        texture=b"jpeg",
    )
    include = payload["gltf"]["1k"]["gltf"]["include"]
    if mutation == "traversal":
        include["../escape.bin"] = include.pop("fixture_model.bin")
    elif mutation == "host":
        include["fixture_model.bin"]["url"] = "https://example.test/fixture_model.bin"
    elif mutation == "md5":
        include["fixture_model.bin"]["md5"] = "A" * 32
    elif mutation == "extra":
        include["fixture_model.bin"]["unexpected"] = True
    elif mutation == "missing_resolution":
        del payload["gltf"]["1k"]
    elif mutation == "metadata":
        include["metadata.json"] = include.pop("fixture_model.bin")
    elif mutation == "partial":
        include["fixture_model.bin.part"] = include.pop("fixture_model.bin")

    with pytest.raises(PolyHavenAcquireError, match=message):
        parse_polyhaven_model_files("fixture_model", payload)


def test_redirect_is_blocked_before_the_target_request_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded: list[str] = []

    def forwarded_redirect(
        self: Any,
        request: Any,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Any:
        del self, request, response, code, message, headers
        forwarded.append(new_url)
        return None

    monkeypatch.setattr(
        polyhaven.urllib.request.HTTPRedirectHandler,
        "redirect_request",
        forwarded_redirect,
    )
    handler = polyhaven._AllowlistRedirectHandler(frozenset({"dl.polyhaven.org"}))

    with pytest.raises(PolyHavenAcquireError, match="unapproved HTTPS host"):
        handler.redirect_request(
            polyhaven.urllib.request.Request("https://dl.polyhaven.org/model.gltf"),
            None,
            302,
            "Found",
            {},
            "https://attacker.example/model.gltf",
        )

    assert forwarded == []


def test_sync_rejects_external_data_dir_before_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}_external_data"
    settings = Settings(project_root=tmp_path, data_dir=outside)
    monkeypatch.setattr(
        polyhaven,
        "_fetch_json",
        lambda url: pytest.fail(f"unexpected network request: {url}"),
    )

    with pytest.raises(PolyHavenAcquireError, match="data_dir must be inside project_root"):
        sync_polyhaven_models(settings=settings)

    assert not outside.exists()
    (manifest_path,) = (tmp_path / "out/acquire/polyhaven").glob("*/manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"


def test_sync_writes_checked_bytes_state_manifest_and_strict_ingest_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_id = "fixture_model"
    binary = b"binary-payload"
    texture = b"jpeg-payload"
    main = _gltf_payload(source_id, binary=binary, texture=texture)
    files = _files_payload(source_id, main=main, binary=binary, texture=texture)
    listing = {source_id: _listing_entry()}
    entries = [
        files["gltf"]["1k"]["gltf"],
        *files["gltf"]["1k"]["gltf"]["include"].values(),
    ]
    downloads = dict(zip((entry["url"] for entry in entries), (main, binary, texture), strict=True))
    calls = _install_api(
        monkeypatch,
        listing=listing,
        file_payloads={source_id: files},
        downloads=downloads,
    )

    result = sync_polyhaven_models(settings=_settings(tmp_path), limit=1)

    assert result.discovered == result.selected == 1
    assert result.downloaded_files == 3
    assert result.reused_files == 0
    assert result.downloaded_bytes == len(main) + len(binary) + len(texture)
    assert result.verified_bytes == len(main) + len(binary) + len(texture)
    item = result.items[0]
    assert item.asset_id == revisioned_asset_id(source_id, REVISION)
    assert item.main_path.read_bytes() == main
    assert [path.read_bytes() for path in item.dependency_paths] == [binary, texture]
    metadata = json.loads(item.metadata_path.read_text(encoding="utf-8"))
    assert metadata["license"] == "CC0-1.0"
    assert metadata["files"][0]["sha256"] == hashlib.sha256(main).hexdigest()
    state = json.loads(result.state_path.read_text(encoding="utf-8"))
    assert state["items"][item.asset_id]["status"] == "downloaded"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "prepared"
    assert manifest["listing"]["payload_sha256"] == result.snapshot_sha256
    spec = load_ingest_spec(result.generated_spec_path)
    assert spec.assets[0].asset_id == item.asset_id
    assert spec.assets[0].path == item.main_path.resolve()
    assert spec.assets[0].dependencies == (
        Path("fixture_model.bin"),
        Path("textures/fixture_model_diff_1k.jpg"),
    )
    assert "textured" in spec.assets[0].tags
    staged = stage_asset(spec.assets[0], raw_root=tmp_path / "data/raw/polyhaven_test")
    assert staged.raw_path.read_bytes() == main
    assert len(staged.files) == 3

    # Downloaded is deliberately nonterminal: without a caller finalization,
    # the next run must prepare the same revision again for a failed ingest retry.
    retried = sync_polyhaven_models(settings=_settings(tmp_path), limit=1)
    assert retried.items[0].asset_id == item.asset_id
    assert retried.items[0].state_status == "downloaded"
    assert retried.downloaded_files == 0
    assert retried.reused_files == 3
    assert retried.items[0].acquired_at == item.acquired_at
    assert retried.items[0].verified_at > item.verified_at
    assert len([url for url, _ in calls if url.startswith("https://dl.polyhaven.org/")]) == 3


def test_v1_terminal_state_is_downgraded_and_reverified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings, first, _ = _sync_fixture(monkeypatch, tmp_path)
    item = first.items[0]
    v2_state = json.loads(first.state_path.read_text(encoding="utf-8"))
    current = v2_state["items"][item.asset_id]
    v1_state = {
        "schema_version": 1,
        "source": "polyhaven",
        "asset_type": "models",
        "updated_at": current["last_prepared_at"],
        "last_listing": {
            "snapshot_sha256": first.snapshot_sha256,
            "observed_at": current["last_prepared_at"],
            "watermark": {
                "date_published": current["date_published"],
                "source_id": current["source_id"],
            },
        },
        "items": {
            item.asset_id: {
                "asset_id": item.asset_id,
                "source_id": item.source_id,
                "revision": item.revision,
                "date_published": current["date_published"],
                "status": "ingested",
                "root_dir": current["root_dir"],
                "main_path": current["main_path"],
                "metadata_path": current["metadata_path"],
                "metadata_sha256": current["metadata_file_sha256"],
                "last_prepared_at": current["last_prepared_at"],
                "last_run_id": current["last_run_id"],
                "terminal_at": current["last_prepared_at"],
                "terminal_run_id": current["last_run_id"],
            }
        },
    }
    first.state_path.write_text(json.dumps(v1_state), encoding="utf-8")

    migrated = sync_polyhaven_models(settings=settings)

    state = json.loads(migrated.state_path.read_text(encoding="utf-8"))
    migrated_item = state["items"][item.asset_id]
    assert state["schema_version"] == 2
    assert state["migrated_from"] == 1
    assert migrated_item["status"] == "downloaded"
    assert migrated_item["migration_pending"] is False
    assert migrated_item["terminal"] is None
    assert migrated.downloaded_files == 0
    assert migrated.reused_files == 3


def test_pending_retry_rotation_does_not_starve_unseen_or_older_pending() -> None:
    models = parse_polyhaven_model_listing(
        {
            "pending_newer": _listing_entry(name="Pending Newer", published=10),
            "pending_older": _listing_entry(
                name="Pending Older",
                revision=SECOND_REVISION,
                published=20,
            ),
            "unseen": _listing_entry(
                name="Unseen",
                revision="c" * 40,
                published=30,
            ),
        }
    )
    by_source = {model.source_id: model for model in models}
    state = {
        "next_selection_class": "pending",
        "items": {
            by_source["pending_newer"].asset_id: {
                "status": "downloaded",
                "last_prepared_at": "2026-01-02T00:00:00Z",
            },
            by_source["pending_older"].asset_id: {
                "status": "downloaded",
                "last_prepared_at": "2026-01-01T00:00:00Z",
            },
        },
    }

    selected, next_class = polyhaven._select_models(models, state=state, limit=2)

    assert [model.source_id for model in selected] == ["pending_older", "unseen"]
    assert next_class == "pending"


@pytest.mark.parametrize(
    "tamper",
    [
        "spec",
        "state",
        "state_date",
        "state_acquired_at",
        "file",
        "manifest_request",
        "manifest_listing",
        "manifest_counts",
        "manifest_runtime",
    ],
)
def test_finalize_rejects_prepared_input_tampering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tamper: str,
) -> None:
    _, result, _ = _sync_fixture(monkeypatch, tmp_path)
    batch_path = tmp_path / "out/ingest_batches/fixture/manifest.json"
    batch_path.parent.mkdir(parents=True)
    batch_path.write_text('{"status":"ok"}\n', encoding="utf-8")
    if tamper == "spec":
        assert result.generated_spec_path is not None
        result.generated_spec_path.write_text(
            result.generated_spec_path.read_text(encoding="utf-8") + "# tamper\n",
            encoding="utf-8",
        )
    elif tamper in {"state", "state_date", "state_acquired_at"}:
        state = json.loads(result.state_path.read_text(encoding="utf-8"))
        state_item = state["items"][result.items[0].asset_id]
        if tamper == "state":
            state_item["prepare_token"] = "0" * 64
        elif tamper == "state_date":
            state_item["date_published"] += 1
        else:
            state_item["acquired_at"] = "2026-01-01T00:00:00Z"
        result.state_path.write_text(json.dumps(state), encoding="utf-8")
    elif tamper == "file":
        result.items[0].dependency_paths[0].write_bytes(b"X" * len(b"binary-payload"))
    else:
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        if tamper == "manifest_request":
            manifest["request"]["limit"] = 2
        elif tamper == "manifest_listing":
            manifest["listing"]["discovered"] = 2
        elif tamper == "manifest_runtime":
            manifest["runtime"]["http"]["rate_limit_wait_ms"] += 1
        else:
            manifest["counts"]["verified_bytes"] += 1
        result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        PolyHavenAcquireError,
        match="changed|CAS failed|mismatch|receipt|accounting",
    ):
        finalize_polyhaven_items(result=result, batch_manifest_path=batch_path)


def test_minimal_batch_cannot_finalize_a_prepared_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, result, _ = _sync_fixture(monkeypatch, tmp_path)
    batch_path = tmp_path / "out/ingest_batches/fixture/manifest.json"
    batch_path.parent.mkdir(parents=True)
    batch_path.write_text('{"status":"ok"}\n', encoding="utf-8")

    with pytest.raises(PolyHavenAcquireError, match="unsupported shape"):
        finalize_polyhaven_items(result=result, batch_manifest_path=batch_path)

    state = json.loads(result.state_path.read_text(encoding="utf-8"))
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert state["items"][result.items[0].asset_id]["status"] == "downloaded"
    assert manifest["status"] == "prepared"


def test_sync_retries_429_retry_after_and_503_with_monotonic_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_id = "fixture_model"
    main, binary, texture, files, downloads = _model_network_fixture(source_id)
    del main, binary, texture
    clock = _FakeClock()
    monkeypatch.setattr(polyhaven, "SystemClock", lambda: clock)
    listing_attempts = 0
    files_attempts = 0
    calls: list[tuple[float, str]] = []

    def fake_open_url(request: Any, **_kwargs: Any) -> _Response:
        nonlocal listing_attempts, files_attempts
        url = request.full_url
        calls.append((clock.monotonic(), url))
        if url == polyhaven.POLYHAVEN_MODELS_URL:
            listing_attempts += 1
            if listing_attempts == 1:
                return _Response(b"", url=url, status=429, headers={"Retry-After": "7"})
            return _Response(
                json.dumps({source_id: _listing_entry()}).encode(),
                url=url,
            )
        if url == polyhaven.POLYHAVEN_FILES_URL.format(source_id=source_id):
            files_attempts += 1
            if files_attempts == 1:
                return _Response(b"", url=url, status=503)
            return _Response(json.dumps(files).encode(), url=url)
        return _Response(downloads[url], url=url)

    monkeypatch.setattr(polyhaven, "_open_url", fake_open_url)
    result = sync_polyhaven_models(
        settings=_settings(tmp_path),
        runtime_config=polyhaven.PolyHavenRuntimeConfig(
            request_rate_per_sec=2,
            retry_max_attempts=3,
            retry_base_delay_sec=1,
            retry_max_delay_sec=1,
            max_retry_after_sec=10,
        ),
    )

    assert [timestamp for timestamp, _ in calls] == pytest.approx(
        [0.0, 7.0, 7.5, 8.5, 9.0, 9.5, 10.0]
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["runtime"]["http"] == {
        "request_attempts": 7,
        "retry_attempts": 2,
        "retry_after_honored": 1,
        "rate_limit_wait_ms": 2_000,
        "retry_wait_ms": 8_000,
        "download_body_bytes": result.downloaded_bytes,
    }


def test_malformed_retry_after_fails_closed_without_an_extra_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(polyhaven, "SystemClock", lambda: clock)
    calls = 0

    def fake_open_url(request: Any, **_kwargs: Any) -> _Response:
        nonlocal calls
        calls += 1
        return _Response(
            b"",
            url=request.full_url,
            status=429,
            headers={"Retry-After": "not-a-date"},
        )

    monkeypatch.setattr(polyhaven, "_open_url", fake_open_url)
    with pytest.raises(PolyHavenAcquireError, match="invalid Retry-After"):
        sync_polyhaven_models(settings=_settings(tmp_path))

    assert calls == 1
    manifest_path = next((tmp_path / "out/acquire/polyhaven").glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["runtime"]["http"]["request_attempts"] == 1
    assert manifest["runtime"]["http"]["retry_attempts"] == 0


def test_retry_budgets_cannot_be_reset_by_alternating_failure_categories(
    tmp_path: Path,
) -> None:
    clock = _FakeClock()
    runtime = polyhaven._AcquisitionRuntime(
        config=polyhaven.PolyHavenRuntimeConfig(
            request_rate_per_sec=1_000_000,
            retry_max_attempts=2,
            integrity_max_attempts=2,
            retry_base_delay_sec=0.001,
            retry_max_delay_sec=0.001,
        ),
        clock=clock,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
    )
    failures = [
        polyhaven.FailureKind.TRANSPORT,
        polyhaven.FailureKind.INTEGRITY,
        polyhaven.FailureKind.TRANSPORT,
        polyhaven.FailureKind.INTEGRITY,
    ]
    calls = 0

    def operation() -> None:
        nonlocal calls
        kind = failures[calls]
        calls += 1
        raise polyhaven._AttemptFailure(
            polyhaven.AcquisitionFailure(
                kind=kind,
                phase="download",
                message="alternating fixture failure",
            )
        )

    with pytest.raises(PolyHavenAcquireError, match="retry budget exhausted"):
        runtime.run_with_retries(phase="download", operation=operation)

    assert calls == 3
    assert runtime.stats.retry_attempts == 2


def test_redirect_hops_each_consume_a_rate_token_and_request_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _FakeClock()
    runtime = polyhaven._AcquisitionRuntime(
        config=polyhaven.PolyHavenRuntimeConfig(request_rate_per_sec=2),
        clock=clock,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
    )

    def forwarded_redirect(
        self: Any,
        request: Any,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Any:
        del self, request, response, code, message, headers
        return polyhaven.urllib.request.Request(new_url)

    monkeypatch.setattr(
        polyhaven.urllib.request.HTTPRedirectHandler,
        "redirect_request",
        forwarded_redirect,
    )
    handler = polyhaven._AllowlistRedirectHandler(frozenset({"dl.polyhaven.org"}))
    first = polyhaven.urllib.request.Request("https://dl.polyhaven.org/first")
    runtime.start_request()
    polyhaven._set_redirect_request_hook(first, runtime.start_request)
    second = handler.redirect_request(
        first,
        None,
        302,
        "Found",
        {},
        "https://dl.polyhaven.org/second",
    )
    assert second is not None
    third = handler.redirect_request(
        second,
        None,
        302,
        "Found",
        {},
        "https://dl.polyhaven.org/third",
    )
    assert third is not None

    assert runtime.stats.request_attempts == 3
    assert runtime.stats.rate_limit_wait_sec == pytest.approx(1.0)
    assert clock.monotonic_value == pytest.approx(1.0)


def test_daily_quota_refuses_new_reservations_after_utc_day_changes(
    tmp_path: Path,
) -> None:
    clock = _FakeClock(wall=datetime(2026, 7, 11, 23, 59, 59, tzinfo=UTC))
    config = polyhaven.PolyHavenRuntimeConfig(max_download_bytes_per_day=20)
    data_dir = tmp_path / "data"
    asset_id = revisioned_asset_id("fixture_model", REVISION)
    first_spec = PolyHavenFileSpec(
        relative_path=Path("first.bin"),
        url="https://dl.polyhaven.org/first.bin",
        bytes=10,
        md5="a" * 32,
    )
    second_spec = PolyHavenFileSpec(
        relative_path=Path("second.bin"),
        url="https://dl.polyhaven.org/second.bin",
        bytes=10,
        md5="b" * 32,
    )
    first_runtime = polyhaven._AcquisitionRuntime(
        config=config,
        clock=clock,
        project_root=tmp_path,
        data_dir=data_dir,
    )
    first_key = first_runtime.quota.begin_download(asset_id=asset_id, spec=first_spec)
    first_runtime.quota.finish_download(first_key)
    clock.sleep(2)

    with pytest.raises(PolyHavenAcquireError, match="UTC quota day changed"):
        first_runtime.quota.begin_download(asset_id=asset_id, spec=second_spec)
    assert first_runtime.quota.usage.utc_day.isoformat() == "2026-07-11"
    assert first_runtime.quota.usage.download_bytes_reserved == 10

    next_runtime = polyhaven._AcquisitionRuntime(
        config=config,
        clock=clock,
        project_root=tmp_path,
        data_dir=data_dir,
    )
    next_key = next_runtime.quota.begin_download(asset_id=asset_id, spec=second_spec)
    assert next_runtime.quota.usage.utc_day.isoformat() == "2026-07-12"
    assert next_runtime.quota.usage.download_bytes_reserved == 11
    next_runtime.quota.finish_download(next_key)
    assert next_runtime.quota.usage.download_bytes_reserved == 10


def test_exact_daily_byte_quota_downloads_valid_body_without_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"x"
    url = "https://dl.polyhaven.org/exact-quota.bin"
    spec = PolyHavenFileSpec(
        relative_path=Path("exact-quota.bin"),
        url=url,
        bytes=len(expected),
        md5=_md5(expected),
    )
    asset_id = revisioned_asset_id("fixture_model", REVISION)
    data_dir = tmp_path / "data"
    destination = data_dir / "acquire/polyhaven/models/fixture/exact-quota.bin"
    runtime = polyhaven._AcquisitionRuntime(
        config=polyhaven.PolyHavenRuntimeConfig(
            request_rate_per_sec=1_000_000,
            max_download_bytes_per_day=len(expected),
        ),
        clock=_FakeClock(),
        project_root=tmp_path,
        data_dir=data_dir,
    )
    network_calls = 0

    def exact_response(*_args: Any, **_kwargs: Any) -> _Response:
        nonlocal network_calls
        network_calls += 1
        return _Response(expected, url=url, headers={"Content-Length": "1"})

    monkeypatch.setattr(polyhaven, "_open_url", exact_response)
    result = polyhaven._acquire_file(
        spec,
        destination=destination,
        force=False,
        asset_id=asset_id,
        runtime=runtime,
    )

    assert result.downloaded_bytes == len(expected)
    assert destination.read_bytes() == expected
    assert network_calls == 1
    assert runtime.stats.download_body_bytes == len(expected)
    assert runtime.stats.download_bytes_reserved == len(expected)
    assert runtime.stats.download_probe_bytes_released == 0
    assert runtime.quota.usage.download_bytes_reserved == len(expected)
    assert runtime.quota.evidence()["open_downloads_after"] == 0


@pytest.mark.parametrize("framing", ["missing", "oversized", "duplicate", "ambiguous"])
def test_exact_daily_byte_quota_rejects_invalid_framing_without_reading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    framing: str,
) -> None:
    expected = b"x"
    url = "https://dl.polyhaven.org/known-oversized.bin"
    spec = PolyHavenFileSpec(
        relative_path=Path("known-oversized.bin"),
        url=url,
        bytes=len(expected),
        md5=_md5(expected),
    )
    asset_id = revisioned_asset_id("fixture_model", REVISION)
    data_dir = tmp_path / "data"
    destination = data_dir / "acquire/polyhaven/models/fixture/known-oversized.bin"
    runtime = polyhaven._AcquisitionRuntime(
        config=polyhaven.PolyHavenRuntimeConfig(
            request_rate_per_sec=1_000_000,
            max_download_bytes_per_day=len(expected),
        ),
        clock=_FakeClock(),
        project_root=tmp_path,
        data_dir=data_dir,
    )

    class TrackingResponse(_Response):
        read_calls = 0

        def read(self, size: int = -1) -> bytes:
            self.read_calls += 1
            return super().read(size)

    if framing == "missing":
        headers: Any = {}
    elif framing == "oversized":
        headers: Any = {"Content-Length": "2"}
    elif framing == "ambiguous":
        headers = {"Content-Length": "1", "Transfer-Encoding": "chunked"}
    else:

        class DuplicateContentLengthHeaders:
            def get_all(self, name: str) -> list[str] | None:
                return ["1", "2"] if name.casefold() == "content-length" else None

            def items(self) -> list[tuple[str, str]]:
                return [("Content-Length", "1"), ("Content-Length", "2")]

        headers = DuplicateContentLengthHeaders()
    response = TrackingResponse(b"xy", url=url, headers=headers)
    monkeypatch.setattr(polyhaven, "_open_url", lambda *_args, **_kwargs: response)

    with pytest.raises(PolyHavenAcquireError, match="Content-Length|framing"):
        polyhaven._acquire_file(
            spec,
            destination=destination,
            force=False,
            asset_id=asset_id,
            runtime=runtime,
        )

    assert response.read_calls == 0
    assert runtime.stats.download_body_bytes == 0
    assert runtime.quota.usage.download_bytes_reserved == len(expected)
    assert runtime.quota.evidence()["open_downloads_after"] == 1
    assert not destination.exists()
    assert not destination.with_name(".known-oversized.bin.part").exists()


@pytest.mark.parametrize("boundary", ["storage", "free"])
def test_exact_disk_capacity_downloads_valid_body_without_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
) -> None:
    expected = b"x"
    url = "https://dl.polyhaven.org/exact-disk.bin"
    spec = PolyHavenFileSpec(
        relative_path=Path("exact-disk.bin"),
        url=url,
        bytes=len(expected),
        md5=_md5(expected),
    )
    asset_id = revisioned_asset_id("fixture_model", REVISION)
    data_dir = tmp_path / "data"
    destination = data_dir / "acquire/polyhaven/models/fixture/exact-disk.bin"
    config = polyhaven.PolyHavenRuntimeConfig(
        request_rate_per_sec=1_000_000,
        max_storage_bytes=len(expected) if boundary == "storage" else None,
    )
    if boundary == "free":
        monkeypatch.setattr(
            polyhaven,
            "_polyhaven_disk_snapshot",
            lambda **_kwargs: polyhaven.DiskSnapshot(
                storage_bytes=0,
                free_bytes=len(expected),
            ),
        )
    runtime = polyhaven._AcquisitionRuntime(
        config=config,
        clock=_FakeClock(),
        project_root=tmp_path,
        data_dir=data_dir,
    )
    monkeypatch.setattr(
        polyhaven,
        "_open_url",
        lambda *_args, **_kwargs: _Response(
            expected,
            url=url,
            headers={"Content-Length": "1"},
        ),
    )

    result = polyhaven._acquire_file(
        spec,
        destination=destination,
        force=False,
        asset_id=asset_id,
        runtime=runtime,
    )

    assert result.downloaded_bytes == len(expected)
    assert destination.read_bytes() == expected
    assert runtime.stats.download_body_bytes == len(expected)
    assert runtime.stats.disk_checks == 1


def test_force_after_destination_replace_crash_creates_a_new_transfer_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"x"
    url = "https://dl.polyhaven.org/force-after-crash.bin"
    spec = PolyHavenFileSpec(
        relative_path=Path("force-after-crash.bin"),
        url=url,
        bytes=len(expected),
        md5=_md5(expected),
    )
    asset_id = revisioned_asset_id("fixture_model", REVISION)
    data_dir = tmp_path / "data"
    destination = data_dir / "acquire/polyhaven/models/fixture/force-after-crash.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(expected)
    config = polyhaven.PolyHavenRuntimeConfig(
        request_rate_per_sec=1_000_000,
        max_download_bytes_per_day=2,
    )
    clock = _FakeClock()
    crashed_runtime = polyhaven._AcquisitionRuntime(
        config=config,
        clock=clock,
        project_root=tmp_path,
        data_dir=data_dir,
    )
    crashed_runtime.quota.begin_download(asset_id=asset_id, spec=spec)
    assert crashed_runtime.quota.usage.download_bytes_reserved == 2
    assert crashed_runtime.quota.evidence()["open_downloads_after"] == 1

    restarted = polyhaven._AcquisitionRuntime(
        config=config,
        clock=clock,
        project_root=tmp_path,
        data_dir=data_dir,
    )
    network_calls = 0

    def forced_response(*_args: Any, **_kwargs: Any) -> _Response:
        nonlocal network_calls
        network_calls += 1
        return _Response(expected, url=url, headers={"Content-Length": "1"})

    monkeypatch.setattr(polyhaven, "_open_url", forced_response)
    result = polyhaven._acquire_file(
        spec,
        destination=destination,
        force=True,
        asset_id=asset_id,
        runtime=restarted,
    )

    assert result.downloaded_bytes == len(expected)
    assert network_calls == 1
    assert restarted.stats.download_body_bytes == len(expected)
    assert restarted.stats.download_bytes_reserved == len(expected)
    assert restarted.stats.download_probe_bytes_released == 1
    assert restarted.quota.usage.download_bytes_reserved == 2
    assert restarted.quota.evidence()["open_downloads_after"] == 0


def test_complete_partial_crash_conservatively_consumes_unconfirmed_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"x"
    url = "https://dl.polyhaven.org/complete-partial.bin"
    spec = PolyHavenFileSpec(
        relative_path=Path("complete-partial.bin"),
        url=url,
        bytes=len(expected),
        md5=_md5(expected),
    )
    asset_id = revisioned_asset_id("fixture_model", REVISION)
    data_dir = tmp_path / "data"
    destination = data_dir / "acquire/polyhaven/models/fixture/complete-partial.bin"
    destination.parent.mkdir(parents=True)
    partial = destination.with_name(".complete-partial.bin.part")
    config = polyhaven.PolyHavenRuntimeConfig(
        request_rate_per_sec=1_000_000,
        max_download_bytes_per_day=2,
    )
    clock = _FakeClock()
    crashed_runtime = polyhaven._AcquisitionRuntime(
        config=config,
        clock=clock,
        project_root=tmp_path,
        data_dir=data_dir,
    )
    crashed_runtime.quota.begin_download(asset_id=asset_id, spec=spec)
    partial.write_bytes(expected)

    restarted = polyhaven._AcquisitionRuntime(
        config=config,
        clock=clock,
        project_root=tmp_path,
        data_dir=data_dir,
    )
    monkeypatch.setattr(
        polyhaven,
        "_open_url",
        lambda *_args, **_kwargs: pytest.fail("complete partial recovery must not use network"),
    )
    result = polyhaven._acquire_file(
        spec,
        destination=destination,
        force=False,
        asset_id=asset_id,
        runtime=restarted,
    )

    assert result.downloaded_bytes == 0
    assert destination.read_bytes() == expected
    assert not partial.exists()
    assert restarted.stats.download_body_bytes == 0
    assert restarted.stats.download_bytes_reserved == 0
    assert restarted.stats.download_probe_bytes_released == 0
    assert restarted.quota.usage.download_bytes_reserved == 2
    assert restarted.quota.evidence()["open_downloads_after"] == 0


def test_probe_read_error_after_complete_body_conservatively_consumes_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"x"
    url = "https://dl.polyhaven.org/probe-read-error.bin"
    spec = PolyHavenFileSpec(
        relative_path=Path("probe-read-error.bin"),
        url=url,
        bytes=len(expected),
        md5=_md5(expected),
    )
    asset_id = revisioned_asset_id("fixture_model", REVISION)
    data_dir = tmp_path / "data"
    destination = data_dir / "acquire/polyhaven/models/fixture/probe-read-error.bin"
    runtime = polyhaven._AcquisitionRuntime(
        config=polyhaven.PolyHavenRuntimeConfig(
            request_rate_per_sec=1_000_000,
            retry_max_attempts=2,
            retry_base_delay_sec=0.001,
            retry_max_delay_sec=0.001,
            max_download_bytes_per_day=2,
        ),
        clock=_FakeClock(),
        project_root=tmp_path,
        data_dir=data_dir,
    )

    class ProbeReadErrorResponse(_Response):
        read_calls = 0

        def read(self, size: int = -1) -> bytes:
            self.read_calls += 1
            if self.read_calls == 1:
                return super().read(min(size, len(expected)))
            raise OSError("fixture EOF probe read failure")

    response = ProbeReadErrorResponse(expected, url=url)
    monkeypatch.setattr(polyhaven, "_open_url", lambda *_args, **_kwargs: response)
    result = polyhaven._acquire_file(
        spec,
        destination=destination,
        force=False,
        asset_id=asset_id,
        runtime=runtime,
    )

    assert result.downloaded_bytes == len(expected)
    assert destination.read_bytes() == expected
    assert response.read_calls == 2
    assert runtime.stats.request_attempts == 1
    assert runtime.stats.retry_attempts == 1
    assert runtime.stats.download_body_bytes == len(expected)
    assert runtime.stats.download_bytes_reserved == 2
    assert runtime.stats.download_probe_bytes_released == 0
    assert runtime.quota.usage.download_bytes_reserved == 2
    assert runtime.quota.evidence()["open_downloads_after"] == 0


def test_retry_reserves_retransmission_before_network_after_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"x"
    url = "https://dl.polyhaven.org/write-failure.bin"
    spec = PolyHavenFileSpec(
        relative_path=Path("write-failure.bin"),
        url=url,
        bytes=len(expected),
        md5=_md5(expected),
    )
    asset_id = revisioned_asset_id("fixture_model", REVISION)
    data_dir = tmp_path / "data"
    destination = data_dir / "acquire/polyhaven/models/fixture/write-failure.bin"
    partial = destination.with_name(".write-failure.bin.part")
    runtime = polyhaven._AcquisitionRuntime(
        config=polyhaven.PolyHavenRuntimeConfig(
            request_rate_per_sec=1_000_000,
            retry_max_attempts=2,
            retry_base_delay_sec=0.001,
            retry_max_delay_sec=0.001,
            max_download_bytes_per_day=len(expected),
        ),
        clock=_FakeClock(),
        project_root=tmp_path,
        data_dir=data_dir,
    )
    network_calls = 0

    def response(*_args: Any, **_kwargs: Any) -> _Response:
        nonlocal network_calls
        network_calls += 1
        return _Response(expected, url=url, headers={"Content-Length": "1"})

    monkeypatch.setattr(polyhaven, "_open_url", response)
    original_open = Path.open
    fail_write = True

    class FailingWriter:
        def __init__(self, file: Any) -> None:
            self.file = file

        def __enter__(self) -> FailingWriter:
            self.file.__enter__()
            return self

        def __exit__(self, *args: object) -> Any:
            return self.file.__exit__(*args)

        def write(self, _payload: bytes) -> int:
            nonlocal fail_write
            fail_write = False
            raise OSError("fixture write failure after response read")

        def __getattr__(self, name: str) -> Any:
            return getattr(self.file, name)

    def flaky_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        file = original_open(path, *args, **kwargs)
        if path == partial and fail_write:
            return FailingWriter(file)
        return file

    monkeypatch.setattr(Path, "open", flaky_open)
    with pytest.raises(PolyHavenAcquireError, match="download_bytes quota exceeded"):
        polyhaven._acquire_file(
            spec,
            destination=destination,
            force=False,
            asset_id=asset_id,
            runtime=runtime,
        )

    assert network_calls == 1
    assert runtime.stats.request_attempts == 1
    assert runtime.stats.retry_attempts == 1
    assert runtime.stats.download_body_bytes == len(expected)
    assert runtime.quota.usage.download_bytes_reserved == len(expected)
    assert runtime.quota.evidence()["open_downloads_after"] == 1
    assert partial.stat().st_size == 0
    assert not destination.exists()


def test_oversized_body_is_bounded_durably_accounted_and_not_free_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    declared = b"x"
    oversized = declared + b"y" * (1024 * 1024)
    url = "https://dl.polyhaven.org/oversized.bin"
    spec = PolyHavenFileSpec(
        relative_path=Path("oversized.bin"),
        url=url,
        bytes=len(declared),
        md5=_md5(declared),
    )
    asset_id = revisioned_asset_id("fixture_model", REVISION)
    data_dir = tmp_path / "data"
    destination = data_dir / "acquire/polyhaven/models/fixture/oversized.bin"
    config = polyhaven.PolyHavenRuntimeConfig(
        request_rate_per_sec=1_000_000,
        max_download_bytes_per_day=2,
    )
    runtime = polyhaven._AcquisitionRuntime(
        config=config,
        clock=_FakeClock(),
        project_root=tmp_path,
        data_dir=data_dir,
    )
    response = _Response(oversized, url=url)
    monkeypatch.setattr(polyhaven, "_open_url", lambda *args, **kwargs: response)

    with pytest.raises(PolyHavenAcquireError, match="exceeds expected size"):
        polyhaven._acquire_file(
            spec,
            destination=destination,
            force=False,
            asset_id=asset_id,
            runtime=runtime,
        )

    assert runtime.stats.download_body_bytes == 2
    assert runtime.stats.download_bytes_overage == 1
    assert runtime.quota.usage.download_bytes_reserved == 2
    assert runtime.quota.evidence()["open_downloads_after"] == 0
    assert not destination.with_name(".oversized.bin.part").exists()

    restarted = polyhaven._AcquisitionRuntime(
        config=config,
        clock=_FakeClock(),
        project_root=tmp_path,
        data_dir=data_dir,
    )
    monkeypatch.setattr(
        polyhaven,
        "_open_url",
        lambda *args, **kwargs: pytest.fail("quota debt must reject before another request"),
    )
    with pytest.raises(PolyHavenAcquireError, match="download_bytes quota exceeded"):
        polyhaven._acquire_file(
            spec,
            destination=destination,
            force=False,
            asset_id=asset_id,
            runtime=restarted,
        )


def test_oversize_ledger_close_is_atomic_and_write_failure_cannot_retry_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"x"
    url = "https://dl.polyhaven.org/oversized.bin"
    spec = PolyHavenFileSpec(
        relative_path=Path("oversized.bin"),
        url=url,
        bytes=1,
        md5=_md5(expected),
    )
    asset_id = revisioned_asset_id("fixture_model", REVISION)
    data_dir = tmp_path / "data"
    destination = data_dir / "acquire/polyhaven/models/fixture/oversized.bin"
    config = polyhaven.PolyHavenRuntimeConfig(
        request_rate_per_sec=1_000_000,
        max_download_bytes_per_day=2,
    )
    runtime = polyhaven._AcquisitionRuntime(
        config=config,
        clock=_FakeClock(),
        project_root=tmp_path,
        data_dir=data_dir,
    )
    network_calls = 0

    def oversized_response(*_args: Any, **_kwargs: Any) -> _Response:
        nonlocal network_calls
        network_calls += 1
        return _Response(b"xy", url=url)

    monkeypatch.setattr(polyhaven, "_open_url", oversized_response)
    original_write = polyhaven._write_json_atomic

    def fail_atomic_overage_close(path: Path, payload: Any) -> None:
        if (
            path.name == "quota_state.json"
            and payload.get("usage", {}).get("download_bytes_reserved") == 2
            and payload.get("open_downloads") == {}
        ):
            raise OSError("fixture quota close failure")
        original_write(path, payload)

    monkeypatch.setattr(polyhaven, "_write_json_atomic", fail_atomic_overage_close)
    with pytest.raises(PolyHavenAcquireError, match="ledger commit failed"):
        polyhaven._acquire_file(
            spec,
            destination=destination,
            force=False,
            asset_id=asset_id,
            runtime=runtime,
        )
    assert network_calls == 1
    partial = destination.with_name(".oversized.bin.part")
    assert partial.read_bytes() == b"xy"
    ledger = json.loads(
        (data_dir / "acquire/polyhaven/quota_state.json").read_text(encoding="utf-8")
    )
    assert ledger["usage"]["download_bytes_reserved"] == 2
    assert len(ledger["open_downloads"]) == 1

    monkeypatch.setattr(polyhaven, "_write_json_atomic", original_write)
    monkeypatch.setattr(
        polyhaven,
        "_open_url",
        lambda *_args, **_kwargs: pytest.fail("verified partial must prevent another request"),
    )
    restarted = polyhaven._AcquisitionRuntime(
        config=config,
        clock=_FakeClock(),
        project_root=tmp_path,
        data_dir=data_dir,
    )
    with pytest.raises(PolyHavenAcquireError, match="previously received oversized"):
        polyhaven._acquire_file(
            spec,
            destination=destination,
            force=False,
            asset_id=asset_id,
            runtime=restarted,
        )
    assert not partial.exists()
    assert not destination.exists()
    final_ledger = json.loads(
        (data_dir / "acquire/polyhaven/quota_state.json").read_text(encoding="utf-8")
    )
    assert final_ledger["open_downloads"] == {}
    assert final_ledger["usage"]["download_bytes_reserved"] == 2

    other_spec = PolyHavenFileSpec(
        relative_path=Path("other.bin"),
        url="https://dl.polyhaven.org/other.bin",
        bytes=1,
        md5=_md5(b"z"),
    )
    with pytest.raises(PolyHavenAcquireError, match="download_bytes quota exceeded"):
        polyhaven._acquire_file(
            other_spec,
            destination=destination.with_name("other.bin"),
            force=False,
            asset_id=asset_id,
            runtime=restarted,
        )


@pytest.mark.parametrize("roll_over_day", [False, True])
def test_settled_oversize_marker_recovery_is_idempotent_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    roll_over_day: bool,
) -> None:
    expected = b"x"
    url = "https://dl.polyhaven.org/oversized.bin"
    spec = PolyHavenFileSpec(
        relative_path=Path("oversized.bin"),
        url=url,
        bytes=len(expected),
        md5=_md5(expected),
    )
    asset_id = revisioned_asset_id("fixture_model", REVISION)
    data_dir = tmp_path / "data"
    destination = data_dir / "acquire/polyhaven/models/fixture/oversized.bin"
    partial = destination.with_name(".oversized.bin.part")
    config = polyhaven.PolyHavenRuntimeConfig(
        request_rate_per_sec=1_000_000,
        max_download_bytes_per_day=2,
    )
    clock = _FakeClock()
    runtime = polyhaven._AcquisitionRuntime(
        config=config,
        clock=clock,
        project_root=tmp_path,
        data_dir=data_dir,
    )
    monkeypatch.setattr(polyhaven, "_open_url", lambda *_args, **_kwargs: _Response(b"xy", url=url))
    original_unlink = Path.unlink

    def interrupt_marker_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == partial:
            raise KeyboardInterrupt("fixture crash after oversize ledger close")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupt_marker_unlink)
    with pytest.raises(KeyboardInterrupt, match="after oversize ledger close"):
        polyhaven._acquire_file(
            spec,
            destination=destination,
            force=False,
            asset_id=asset_id,
            runtime=runtime,
        )

    ledger_path = data_dir / "acquire/polyhaven/quota_state.json"
    settled = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert settled["usage"]["download_bytes_reserved"] == 2
    assert settled["open_downloads"] == {}
    assert partial.read_bytes() == b"xy"

    monkeypatch.setattr(Path, "unlink", original_unlink)
    if roll_over_day:
        clock.sleep(24 * 60 * 60)
    restarted = polyhaven._AcquisitionRuntime(
        config=config,
        clock=clock,
        project_root=tmp_path,
        data_dir=data_dir,
    )
    monkeypatch.setattr(
        polyhaven,
        "_open_url",
        lambda *_args, **_kwargs: pytest.fail("settled marker recovery must not use network"),
    )
    with pytest.raises(PolyHavenAcquireError, match="previously received oversized"):
        polyhaven._acquire_file(
            spec,
            destination=destination,
            force=False,
            asset_id=asset_id,
            runtime=restarted,
        )

    assert not partial.exists()
    recovered = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert recovered["open_downloads"] == {}
    expected_usage = 0 if roll_over_day else 2
    assert recovered["usage"]["download_bytes_reserved"] == expected_usage


def test_daily_quota_open_download_survives_restart_without_double_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_id = "fixture_model"
    main, binary, texture, files, downloads = _model_network_fixture(source_id)
    main_url = files["gltf"]["1k"]["gltf"]["url"]
    files_url = polyhaven.POLYHAVEN_FILES_URL.format(source_id=source_id)
    total_bytes = len(main) + len(binary) + len(texture)
    split = len(main) // 2
    clock = _FakeClock()
    monkeypatch.setattr(polyhaven, "SystemClock", lambda: clock)
    interrupted = False

    def first_open(request: Any, **_kwargs: Any) -> _Response:
        nonlocal interrupted
        url = request.full_url
        if url == polyhaven.POLYHAVEN_MODELS_URL:
            return _Response(json.dumps({source_id: _listing_entry()}).encode(), url=url)
        if url == files_url:
            return _Response(json.dumps(files).encode(), url=url)
        assert url == main_url
        assert interrupted is False
        interrupted = True
        return _InterruptedResponse(main, split=split, url=url)

    config = polyhaven.PolyHavenRuntimeConfig(
        request_rate_per_sec=1_000_000,
        request_burst=100,
        retry_max_attempts=1,
        integrity_max_attempts=1,
        max_new_items_per_day=1,
        max_download_bytes_per_day=total_bytes + 1,
    )
    monkeypatch.setattr(polyhaven, "_open_url", first_open)
    with pytest.raises(KeyboardInterrupt, match="fixture process interruption"):
        sync_polyhaven_models(settings=_settings(tmp_path), runtime_config=config)

    quota_path = tmp_path / "data/acquire/polyhaven/quota_state.json"
    first_quota = json.loads(quota_path.read_text(encoding="utf-8"))
    assert first_quota["usage"] == {
        "new_items_reserved": 1,
        "download_bytes_reserved": len(main) + 1,
    }
    assert len(first_quota["open_downloads"]) == 1
    assert not (tmp_path / "data/acquire/polyhaven/state.json").exists()

    def second_open(request: Any, **_kwargs: Any) -> _Response:
        url = request.full_url
        if url == polyhaven.POLYHAVEN_MODELS_URL:
            return _Response(json.dumps({source_id: _listing_entry()}).encode(), url=url)
        if url == files_url:
            return _Response(json.dumps(files).encode(), url=url)
        payload = downloads[url]
        range_header = request.get_header("Range")
        if range_header is None:
            return _Response(payload, url=url)
        offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
        return _Response(
            payload[offset:],
            url=url,
            status=206,
            headers={"Content-Range": f"bytes {offset}-{len(payload) - 1}/{len(payload)}"},
        )

    monkeypatch.setattr(polyhaven, "_open_url", second_open)
    result = sync_polyhaven_models(settings=_settings(tmp_path), runtime_config=config)

    assert result.downloaded_bytes == total_bytes - split
    daily = result.runtime_evidence["daily_quota"]
    assert daily["reserved_by_run"] == {
        "new_items": 0,
        "download_bytes": len(binary) + len(texture) + 2,
    }
    assert daily["released_probe_bytes"] == 3
    assert daily["usage_after"] == {
        "new_items_reserved": 1,
        "download_bytes_reserved": total_bytes,
    }
    assert daily["open_downloads_after"] == 0


@pytest.mark.parametrize("quota_kind", ["item", "byte", "disk"])
def test_sync_enforces_explicit_item_byte_and_disk_quotas_before_payload_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    quota_kind: str,
) -> None:
    source_id = "fixture_model"
    main, _, _, files, downloads = _model_network_fixture(source_id)
    calls = _install_api(
        monkeypatch,
        listing={source_id: _listing_entry()},
        file_payloads={source_id: files},
        downloads=downloads,
    )
    changes: dict[str, Any]
    if quota_kind == "item":
        changes = {"max_new_items_per_day": 0}
    elif quota_kind == "byte":
        changes = {"max_download_bytes_per_day": len(main) - 1}
    else:
        changes = {"max_storage_bytes": len(main) - 1}
    config = polyhaven.PolyHavenRuntimeConfig(**changes)

    if quota_kind == "item":
        result = sync_polyhaven_models(settings=_settings(tmp_path), runtime_config=config)
        assert result.items == ()
        assert result.generated_spec_path is None
        assert result.runtime_evidence["daily_quota"]["deferred_new_items"] == 1
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "noop"
        assert json.loads(result.state_path.read_text(encoding="utf-8"))["items"] == {}
    else:
        resource = "download_bytes" if quota_kind == "byte" else "storage_bytes"
        with pytest.raises(PolyHavenAcquireError, match=resource):
            sync_polyhaven_models(settings=_settings(tmp_path), runtime_config=config)

    download_calls = [url for url, _ in calls if url.startswith("https://dl.polyhaven.org/")]
    assert download_calls == []


def test_item_quota_filter_keeps_pending_rotation_eligible() -> None:
    models = parse_polyhaven_model_listing(
        {
            "pending": _listing_entry(name="Pending", published=10),
            "unseen": _listing_entry(name="Unseen", revision=SECOND_REVISION, published=20),
        }
    )
    pending, _ = models
    state = {
        "next_selection_class": "unseen",
        "items": {
            pending.asset_id: {
                "status": "downloaded",
                "last_prepared_at": "2026-01-01T00:00:00Z",
            }
        },
    }

    selected, next_class = polyhaven._select_models(
        models,
        state=state,
        limit=2,
        allowed_unseen_ids=frozenset(),
    )

    assert [model.asset_id for model in selected] == [pending.asset_id]
    assert next_class == "unseen"


def test_download_resumes_a_partial_file_and_computes_sha256(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"complete-payload"
    url = "https://dl.polyhaven.org/file/ph-assets/Models/gltf/1k/model/model.gltf"
    spec = PolyHavenFileSpec(
        relative_path=Path("model.gltf"),
        url=url,
        bytes=len(payload),
        md5=_md5(payload),
    )
    destination = tmp_path / "model.gltf"
    partial = tmp_path / ".model.gltf.part"
    partial.write_bytes(payload[:5])
    requests: list[str | None] = []

    def fake_open_url(
        request: Any,
        *,
        timeout: int,
        allowed_hosts: frozenset[str],
    ) -> _Response:
        assert timeout == 300
        assert allowed_hosts == frozenset({"dl.polyhaven.org"})
        range_header = request.get_header("Range")
        requests.append(range_header)
        return _Response(
            payload[5:],
            url=url,
            status=206,
            headers={"Content-Range": f"bytes 5-{len(payload) - 1}/{len(payload)}"},
        )

    monkeypatch.setattr(polyhaven, "_open_url", fake_open_url)

    downloaded = polyhaven._acquire_file(spec, destination=destination, force=False)

    assert requests == ["bytes=5-"]
    assert destination.read_bytes() == payload
    assert downloaded.sha256 == hashlib.sha256(payload).hexdigest()
    assert downloaded.reused is False
    assert downloaded.downloaded_bytes == len(payload) - 5
    assert not partial.exists()


def test_download_promotes_a_complete_partial_without_network_transfer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"complete-payload"
    url = "https://dl.polyhaven.org/file/ph-assets/Models/gltf/1k/model/model.gltf"
    spec = PolyHavenFileSpec(
        relative_path=Path("model.gltf"),
        url=url,
        bytes=len(payload),
        md5=_md5(payload),
    )
    destination = tmp_path / "model.gltf"
    partial = tmp_path / ".model.gltf.part"
    partial.write_bytes(payload)
    monkeypatch.setattr(
        polyhaven,
        "_open_url",
        lambda *args, **kwargs: pytest.fail("complete partial should not use the network"),
    )

    downloaded = polyhaven._acquire_file(spec, destination=destination, force=False)

    assert destination.read_bytes() == payload
    assert downloaded.reused is False
    assert downloaded.downloaded_bytes == 0
    assert not partial.exists()


def test_download_counts_full_transfer_when_server_ignores_range(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"complete-payload"
    url = "https://dl.polyhaven.org/file/ph-assets/Models/gltf/1k/model/model.gltf"
    spec = PolyHavenFileSpec(
        relative_path=Path("model.gltf"),
        url=url,
        bytes=len(payload),
        md5=_md5(payload),
    )
    destination = tmp_path / "model.gltf"
    partial = tmp_path / ".model.gltf.part"
    partial.write_bytes(payload[:5])
    requests: list[str | None] = []

    def fake_open_url(request: Any, **kwargs: Any) -> _Response:
        del kwargs
        requests.append(request.get_header("Range"))
        return _Response(payload, url=url, status=200)

    monkeypatch.setattr(polyhaven, "_open_url", fake_open_url)

    downloaded = polyhaven._acquire_file(spec, destination=destination, force=False)

    assert requests == ["bytes=5-"]
    assert destination.read_bytes() == payload
    assert downloaded.downloaded_bytes == len(payload)


def test_quota_reservation_accounts_range_200_reuse_and_force_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"complete-payload"
    source_id = "fixture_model"
    asset_id = revisioned_asset_id(source_id, REVISION)
    url = "https://dl.polyhaven.org/file/ph-assets/Models/gltf/1k/model/model.gltf"
    spec = PolyHavenFileSpec(
        relative_path=Path("model.gltf"),
        url=url,
        bytes=len(payload),
        md5=_md5(payload),
    )
    data_dir = tmp_path / "data"
    destination = data_dir / "acquire/polyhaven/models/fixture/model.gltf"
    destination.parent.mkdir(parents=True)
    partial = destination.with_name(".model.gltf.part")
    partial.write_bytes(payload[:5])
    clock = _FakeClock()
    config = polyhaven.PolyHavenRuntimeConfig(
        request_rate_per_sec=1_000_000,
        request_burst=10,
        max_download_bytes_per_day=1_000,
    )
    requests: list[str | None] = []

    def fake_open_url(request: Any, **_kwargs: Any) -> _Response:
        requests.append(request.get_header("Range"))
        return _Response(payload, url=url, status=200)

    monkeypatch.setattr(polyhaven, "_open_url", fake_open_url)
    first_runtime = polyhaven._AcquisitionRuntime(
        config=config,
        clock=clock,
        project_root=tmp_path,
        data_dir=data_dir,
    )
    first = polyhaven._acquire_file(
        spec,
        destination=destination,
        force=False,
        asset_id=asset_id,
        runtime=first_runtime,
    )
    assert first.downloaded_bytes == len(payload)
    assert first_runtime.quota.usage.download_bytes_reserved == len(payload) + 5
    assert first_runtime.quota.evidence()["open_downloads_after"] == 0

    reuse_runtime = polyhaven._AcquisitionRuntime(
        config=config,
        clock=clock,
        project_root=tmp_path,
        data_dir=data_dir,
    )
    reused = polyhaven._acquire_file(
        spec,
        destination=destination,
        force=False,
        asset_id=asset_id,
        runtime=reuse_runtime,
    )
    assert reused.reused is True
    assert reuse_runtime.stats.download_bytes_reserved == 0

    force_runtime = polyhaven._AcquisitionRuntime(
        config=config,
        clock=clock,
        project_root=tmp_path,
        data_dir=data_dir,
    )
    forced = polyhaven._acquire_file(
        spec,
        destination=destination,
        force=True,
        asset_id=asset_id,
        runtime=force_runtime,
    )
    assert forced.reused is False
    assert force_runtime.stats.download_bytes_reserved == len(payload) + 1
    assert force_runtime.quota.usage.download_bytes_reserved == len(payload) * 2 + 5
    assert requests == ["bytes=5-", None]


def test_quota_ledger_rejects_symlinks_and_source_lock_serializes_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_id = "fixture_model"
    _, _, _, files, downloads = _model_network_fixture(source_id)
    calls = _install_api(
        monkeypatch,
        listing={source_id: _listing_entry()},
        file_payloads={source_id: files},
        downloads=downloads,
    )
    settings = _settings(tmp_path)
    quota_path = tmp_path / "data/acquire/polyhaven/quota_state.json"
    quota_path.parent.mkdir(parents=True)
    outside = tmp_path / "outside-quota.json"
    outside.write_text("{}\n", encoding="utf-8")
    quota_path.symlink_to(outside)

    with pytest.raises(PolyHavenAcquireError, match="quota ledger.*regular file"):
        sync_polyhaven_models(
            settings=settings,
            runtime_config=polyhaven.PolyHavenRuntimeConfig(max_new_items_per_day=0),
        )
    assert calls == []
    quota_path.unlink()

    with (
        polyhaven._source_lock(settings.data_dir),
        pytest.raises(PolyHavenAcquireError, match="source sync is busy"),
    ):
        sync_polyhaven_models(
            settings=settings,
            runtime_config=polyhaven.PolyHavenRuntimeConfig(max_new_items_per_day=0),
        )
    assert not quota_path.exists()


def test_quota_ledger_rejects_open_reservations_larger_than_total_usage() -> None:
    asset_id = revisioned_asset_id("fixture_model", REVISION)
    spec = PolyHavenFileSpec(
        relative_path=Path("model.gltf"),
        url="https://dl.polyhaven.org/model.gltf",
        bytes=10,
        md5="a" * 32,
    )
    payload = polyhaven._empty_quota_ledger(datetime(2026, 7, 11, tzinfo=UTC).date())
    payload["updated_at"] = "2026-07-11T00:00:00Z"
    payload["usage"]["download_bytes_reserved"] = 10
    key = polyhaven._quota_download_key(asset_id=asset_id, spec=spec)
    payload["open_downloads"][key] = polyhaven._quota_download_payload(
        asset_id=asset_id,
        spec=spec,
        reserved_bytes=11,
    )

    with pytest.raises(PolyHavenAcquireError, match="open download accounting"):
        polyhaven._validate_quota_ledger(payload)


def test_quota_reservation_write_failure_leaves_no_torn_ledger_and_restarts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_id = "fixture_model"
    _, _, _, files, downloads = _model_network_fixture(source_id)
    _install_api(
        monkeypatch,
        listing={source_id: _listing_entry()},
        file_payloads={source_id: files},
        downloads=downloads,
    )
    original_write = polyhaven._write_json_atomic
    injected = False

    def interrupted_write(path: Path, payload: Any) -> None:
        nonlocal injected
        if path.name == "quota_state.json" and not injected:
            injected = True
            raise OSError("fixture quota commit interruption")
        original_write(path, payload)

    monkeypatch.setattr(polyhaven, "_write_json_atomic", interrupted_write)
    config = polyhaven.PolyHavenRuntimeConfig(max_new_items_per_day=1)
    with pytest.raises(PolyHavenAcquireError, match="quota commit interruption"):
        sync_polyhaven_models(settings=_settings(tmp_path), runtime_config=config)

    quota_path = tmp_path / "data/acquire/polyhaven/quota_state.json"
    assert not quota_path.exists()
    assert list(quota_path.parent.glob("*.tmp")) == []

    monkeypatch.setattr(polyhaven, "_write_json_atomic", original_write)
    result = sync_polyhaven_models(settings=_settings(tmp_path), runtime_config=config)
    quota = json.loads(quota_path.read_text(encoding="utf-8"))
    polyhaven._validate_quota_ledger(quota)
    assert quota["usage"]["new_items_reserved"] == 1
    assert result.selected == 1


def test_force_download_preserves_existing_file_when_new_bytes_fail_md5(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"expected"
    bad = b"bad-byte"
    url = "https://dl.polyhaven.org/file/ph-assets/Models/gltf/1k/model/model.gltf"
    spec = PolyHavenFileSpec(
        relative_path=Path("model.gltf"),
        url=url,
        bytes=len(expected),
        md5=_md5(expected),
    )
    destination = tmp_path / "model.gltf"
    destination.write_bytes(b"old")
    monkeypatch.setattr(
        polyhaven,
        "_open_url",
        lambda request, *, timeout, allowed_hosts: _Response(bad, url=url),
    )

    with pytest.raises(PolyHavenAcquireError, match="md5 mismatch"):
        polyhaven._acquire_file(spec, destination=destination, force=True)

    assert destination.read_bytes() == b"old"
    assert not (tmp_path / ".model.gltf.part").exists()


def test_terminal_finalize_updates_state_and_replay_reuses_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_id = "fixture_model"
    binary = b"binary"
    texture = b"jpeg"
    main = _gltf_payload(source_id, binary=binary, texture=texture)
    files = _files_payload(source_id, main=main, binary=binary, texture=texture)
    entries = [
        files["gltf"]["1k"]["gltf"],
        *files["gltf"]["1k"]["gltf"]["include"].values(),
    ]
    payload_by_url = dict(
        zip((entry["url"] for entry in entries), (main, binary, texture), strict=True)
    )
    calls = _install_api(
        monkeypatch,
        listing={source_id: _listing_entry()},
        file_payloads={source_id: files},
        downloads=payload_by_url,
    )
    settings = _settings(tmp_path)
    first = sync_polyhaven_models(settings=settings)
    asset_id = first.items[0].asset_id

    downstream_manifest = tmp_path / "out/ingest_batches/run/manifest.json"
    downstream_manifest.parent.mkdir(parents=True)
    downstream_manifest.write_text('{"status": "ok"}\n', encoding="utf-8")
    receipt = {"asset_id": asset_id, "fixture": True}
    evidence_sha256 = polyhaven._domain_payload_sha256(
        b"uefactory.polyhaven-terminal-evidence.v1\0", receipt
    )
    monkeypatch.setattr(
        polyhaven,
        "_derive_terminal_statuses",
        lambda **kwargs: {
            asset_id: {
                "status": "imported",
                "receipt": receipt,
                "terminal_evidence_sha256": evidence_sha256,
            }
        },
    )
    revalidated: list[str] = []
    monkeypatch.setattr(
        polyhaven,
        "_revalidate_terminal_receipt",
        lambda **kwargs: revalidated.append(str(kwargs["asset_id"])),
    )

    assert finalize_polyhaven_items(
        result=first,
        batch_manifest_path=downstream_manifest,
    ) == {asset_id: "imported"}
    state = json.loads(first.state_path.read_text(encoding="utf-8"))
    assert state["items"][asset_id]["status"] == "imported"
    first_manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert first_manifest["status"] == "finalized"
    assert first_manifest["finalization"]["terminal_statuses"] == {asset_id: "imported"}
    assert first_manifest["finalization"]["batch_manifest"] == (
        "out/ingest_batches/run/manifest.json"
    )
    assert first_manifest["state"]["after_finalization"]["file_sha256"] == polyhaven._sha256_file(
        first.state_path
    )

    other_manifest = tmp_path / "out/ingest_batches/other/manifest.json"
    other_manifest.parent.mkdir(parents=True)
    other_manifest.write_text('{"status": "ok"}\n', encoding="utf-8")
    with pytest.raises(PolyHavenAcquireError, match="different downstream evidence"):
        finalize_polyhaven_items(result=first, batch_manifest_path=other_manifest)
    unchanged_state = json.loads(first.state_path.read_text(encoding="utf-8"))
    assert unchanged_state["items"][asset_id]["status"] == "imported"

    original_downstream = downstream_manifest.read_bytes()
    downstream_manifest.write_text('{"status": "tampered"}\n', encoding="utf-8")
    with pytest.raises(PolyHavenAcquireError, match="different downstream evidence"):
        finalize_polyhaven_items(result=first, batch_manifest_path=downstream_manifest)
    downstream_manifest.write_bytes(original_downstream)

    revalidated.clear()
    assert finalize_polyhaven_items(
        result=first,
        batch_manifest_path=downstream_manifest,
    ) == {asset_id: "imported"}
    assert revalidated == [asset_id]

    state_before_noop = first.state_path.read_bytes()
    state_mtime_before_noop = first.state_path.stat().st_mtime_ns
    second = sync_polyhaven_models(settings=settings)

    assert second.items == ()
    assert second.generated_spec_path is None
    assert second.downloaded_files == 0
    assert second.reused_files == 0
    state_after_noop = json.loads(first.state_path.read_text(encoding="utf-8"))
    second_manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert first.state_path.read_bytes() != state_before_noop
    assert first.state_path.stat().st_mtime_ns >= state_mtime_before_noop
    assert (
        state_after_noop["noop_run_receipts"][second.run_dir.name]
        == (second_manifest["prepare_receipt_sha256"])
    )
    download_calls = [url for url, _ in calls if url.startswith("https://dl.polyhaven.org/")]
    assert len(download_calls) == 3


def test_terminal_receipt_replay_rederives_unchanged_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result, batch_path, _, _, _ = _finalized_import_fixture(monkeypatch, tmp_path)

    assert finalize_polyhaven_items(result=result, batch_manifest_path=batch_path) == {
        result.items[0].asset_id: "imported"
    }


def test_finalized_replay_survives_unrelated_global_state_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result, batch_path, _, _, _ = _finalized_import_fixture(monkeypatch, tmp_path)
    state = json.loads(result.state_path.read_text(encoding="utf-8"))
    state["next_selection_class"] = (
        "pending" if state["next_selection_class"] == "unseen" else "unseen"
    )
    polyhaven._write_json_atomic(result.state_path, state)

    assert finalize_polyhaven_items(result=result, batch_manifest_path=batch_path) == {
        result.items[0].asset_id: "imported"
    }


@pytest.mark.parametrize("field", ["date_published", "acquired_at"])
def test_finalized_replay_rejects_acquisition_state_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
) -> None:
    result, batch_path, _, _, _ = _finalized_import_fixture(monkeypatch, tmp_path)
    state = json.loads(result.state_path.read_text(encoding="utf-8"))
    item = state["items"][result.items[0].asset_id]
    if field == "date_published":
        item[field] += 1
    else:
        item[field] = "2026-01-01T00:00:00Z"
    polyhaven._write_json_atomic(result.state_path, state)

    with pytest.raises(PolyHavenAcquireError, match="prepare CAS failed"):
        finalize_polyhaven_items(result=result, batch_manifest_path=batch_path)


@pytest.mark.parametrize(
    "mutation",
    ["finalized_at", "terminal_receipt", "extra_key", "cohort_downgrade"],
)
def test_finalized_replay_rejects_manifest_finalization_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    result, batch_path, _, _, _ = _finalized_import_fixture(monkeypatch, tmp_path)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    if mutation == "finalized_at":
        manifest["finalized_at"] = "2099-01-01T00:00:00Z"
    elif mutation == "terminal_receipt":
        evidence = manifest["finalization"]["terminal_evidence"][result.items[0].asset_id]
        evidence["receipt"]["status"] = "corrupt"
    elif mutation == "extra_key":
        manifest["finalization"]["unexpected"] = True
    else:
        finalization = manifest["finalization"]
        finalization["terminal_statuses"] = {}
        finalization["terminal_evidence"] = {}
        finalization["nonterminal_asset_ids"] = [result.items[0].asset_id]
    polyhaven._write_json_atomic(result.manifest_path, manifest)

    with pytest.raises(
        PolyHavenAcquireError,
        match="unsupported|binding differs|receipt|declared nonterminal",
    ):
        finalize_polyhaven_items(result=result, batch_manifest_path=batch_path)


@pytest.mark.parametrize(
    "mutation",
    ["staged_raw", "asset_source", "asset_license", "artifact_delete", "artifact_params"],
)
def test_terminal_receipt_replay_rejects_evidence_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    result, batch_path, catalog_path, raw_path, artifact_id = _finalized_import_fixture(
        monkeypatch, tmp_path
    )
    asset_id = result.items[0].asset_id
    if mutation == "staged_raw":
        dependency = result.items[0].dependency_paths[0].relative_to(result.items[0].root_dir)
        staged_dependency = raw_path.parent / dependency
        staged_dependency.write_bytes(b"X" * staged_dependency.stat().st_size)
    else:
        with sqlite3.connect(catalog_path) as connection:
            if mutation == "asset_source":
                connection.execute(
                    "UPDATE assets SET source = ? WHERE asset_id = ?",
                    ("fixture_source", asset_id),
                )
            elif mutation == "asset_license":
                connection.execute(
                    "UPDATE assets SET license = ? WHERE asset_id = ?",
                    ("CC-BY-4.0", asset_id),
                )
            elif mutation == "artifact_delete":
                connection.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
            else:
                row = connection.execute(
                    "SELECT params_json FROM artifacts WHERE artifact_id = ?", (artifact_id,)
                ).fetchone()
                assert row is not None
                params = json.loads(row[0])
                params["unexamined_drift"] = True
                connection.execute(
                    "UPDATE artifacts SET params_json = ? WHERE artifact_id = ?",
                    (json.dumps(params, sort_keys=True), artifact_id),
                )

    with pytest.raises(
        PolyHavenAcquireError,
        match="changed|invalid|ambiguous|differ",
    ):
        finalize_polyhaven_items(result=result, batch_manifest_path=batch_path)


def test_failed_multi_item_sync_does_not_advance_source_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_id = "first_model"
    second_id = "second_model"
    first_binary = b"bin1"
    first_texture = b"jpg1"
    first_main = _gltf_payload(first_id, binary=first_binary, texture=first_texture)
    second_binary = b"bin2"
    second_texture = b"jpg2"
    second_main = _gltf_payload(second_id, binary=second_binary, texture=second_texture)
    first_files = _files_payload(
        first_id,
        main=first_main,
        binary=first_binary,
        texture=first_texture,
    )
    second_files = _files_payload(
        second_id,
        main=second_main,
        binary=second_binary,
        texture=second_texture,
    )
    entries = [
        first_files["gltf"]["1k"]["gltf"],
        *first_files["gltf"]["1k"]["gltf"]["include"].values(),
        second_files["gltf"]["1k"]["gltf"],
        *second_files["gltf"]["1k"]["gltf"]["include"].values(),
    ]
    good_payloads = (
        first_main,
        first_binary,
        first_texture,
        second_main,
        second_binary,
        second_texture,
    )
    downloads = dict(zip((entry["url"] for entry in entries), good_payloads, strict=True))
    downloads[second_files["gltf"]["1k"]["gltf"]["url"]] = b"broken"
    _install_api(
        monkeypatch,
        listing={
            first_id: _listing_entry(name="First", published=10),
            second_id: _listing_entry(name="Second", revision=SECOND_REVISION, published=20),
        },
        file_payloads={first_id: first_files, second_id: second_files},
        downloads=downloads,
    )

    with pytest.raises(
        PolyHavenAcquireError,
        match="size mismatch|md5 mismatch|Content-Range",
    ):
        sync_polyhaven_models(settings=_settings(tmp_path), limit=2)

    state_path = tmp_path / "data/acquire/polyhaven/state.json"
    assert not state_path.exists()
    manifests = sorted((tmp_path / "out/acquire/polyhaven").glob("*/manifest.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8"))["status"] == "failed"


def test_sync_rejects_symlinked_provider_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_id = "fixture_model"
    files = _files_payload(source_id, main=b"main", binary=b"bin", texture=b"jpg")
    entries = [
        files["gltf"]["1k"]["gltf"],
        *files["gltf"]["1k"]["gltf"]["include"].values(),
    ]
    downloads = dict(
        zip(
            (entry["url"] for entry in entries),
            (b"main", b"bin", b"jpg"),
            strict=True,
        )
    )
    _install_api(
        monkeypatch,
        listing={source_id: _listing_entry()},
        file_payloads={source_id: files},
        downloads=downloads,
    )
    provider_root = tmp_path / "data/acquire/polyhaven/models"
    provider_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (provider_root / source_id).symlink_to(outside, target_is_directory=True)

    with pytest.raises(PolyHavenAcquireError, match="directory is unsafe"):
        sync_polyhaven_models(settings=_settings(tmp_path))

    assert not (outside / REVISION).exists()


def test_parse_does_not_mutate_official_payload_fixture() -> None:
    payload = _files_payload(
        "fixture_model",
        main=b"main",
        binary=b"binary",
        texture=b"texture",
    )
    original = copy.deepcopy(payload)

    parse_polyhaven_model_files("fixture_model", payload)

    assert payload == original


def test_listing_canonicalizes_duplicate_live_metadata_tags() -> None:
    entry = _listing_entry(name="Camera 01")
    entry["tags"] = [
        "vintage",
        "photography",
        "photography",
        "classic ",
        "classic",
    ]
    payload = {"Camera_01": entry}

    (model,) = parse_polyhaven_model_listing(payload)

    assert model.tags == ("vintage", "photography", "classic")
