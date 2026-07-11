from __future__ import annotations

import copy
import hashlib
import io
import json
import sqlite3
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
        headers: dict[str, str] | None = None,
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


def _install_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    listing: dict[str, Any],
    file_payloads: dict[str, dict[str, Any]],
    downloads: dict[str, bytes],
) -> list[tuple[str, str | None]]:
    calls: list[tuple[str, str | None]] = []

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
    assert first.state_path.read_bytes() == state_before_noop
    assert first.state_path.stat().st_mtime_ns == state_mtime_before_noop
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

    with pytest.raises(PolyHavenAcquireError, match="size mismatch|md5 mismatch"):
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
