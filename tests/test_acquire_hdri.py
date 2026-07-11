from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from uefactory.acquire import hdri
from uefactory.acquire.polyhaven_resource_sync import (
    PolyHavenResourceSyncItem,
    PolyHavenResourceSyncResult,
)
from uefactory.core.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(project_root=tmp_path, data_dir=tmp_path / "data")


def _sync_result(
    tmp_path: Path,
    *,
    status: str = "ready",
    downloaded_files: int = 1,
    error: dict[str, object] | None = None,
) -> PolyHavenResourceSyncResult:
    compatibility_path = tmp_path / "data/hdri/studio_small_03_1k.hdr"
    metadata_path = compatibility_path.with_suffix(".json")
    compatibility_path.parent.mkdir(parents=True, exist_ok=True)
    compatibility_path.write_bytes(b"fake-hdri")
    metadata_path.write_text(
        json.dumps(
            {
                "asset_id": "studio_small_03",
                "resolution": "1k",
                "file": str(compatibility_path),
                "source_url": "https://dl.polyhaven.org/file/studio_small_03_1k.hdr",
                "license": "CC0",
                "bytes": len(b"fake-hdri"),
                "md5": "a" * 32,
            }
        ),
        encoding="utf-8",
    )
    item = PolyHavenResourceSyncItem(
        resource_id="polyhaven_hdri_studio_small_03_" + "a" * 32,
        kind="hdri",
        source_id="studio_small_03",
        revision="b" * 40,
        resolution="1k",
        status=status,  # type: ignore[arg-type]
        compatibility_path=compatibility_path,
        compatibility_metadata_path=metadata_path,
        downloaded_files=downloaded_files,
        reused_files=0 if downloaded_files else 1,
        downloaded_bytes=len(b"fake-hdri") if downloaded_files else 0,
        verified_bytes=len(b"fake-hdri"),
        error=error,
    )
    return PolyHavenResourceSyncResult(
        kind="hdri",
        resolution="1k",
        run_id="20260711T120000Z_fixture",
        status="failed" if status == "failed" else "ready",
        manifest_path=tmp_path / "out/acquire/polyhaven-resources/hdri/run/manifest.json",
        state_path=tmp_path / "data/acquire/polyhaven/resources/hdri/state.json",
        failure_journal_path=(
            tmp_path / "data/acquire/polyhaven/resources/hdri/failure_journal.json"
        ),
        catalog_path=tmp_path / "data/catalog.db",
        listing_sha256="c" * 64,
        items=(item,),
    )


def test_acquire_polyhaven_hdri_is_targeted_facade_and_preserves_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []
    sync_result = _sync_result(tmp_path)

    def fake_sync(**kwargs: Any) -> PolyHavenResourceSyncResult:
        calls.append(kwargs)
        return sync_result

    monkeypatch.setattr(hdri, "sync_polyhaven_resources", fake_sync)
    settings = _settings(tmp_path)

    result = hdri.acquire_polyhaven_hdri(settings=settings, force=True)

    assert calls == [
        {
            "settings": settings,
            "kind": "hdri",
            "limit": 1,
            "resolution": "1k",
            "source_ids": ("studio_small_03",),
            "force": True,
        }
    ]
    assert result.file_path.read_bytes() == b"fake-hdri"
    assert result.file_path.is_symlink() is False
    assert result.metadata_path == sync_result.items[0].compatibility_metadata_path
    assert result.source_url.endswith("studio_small_03_1k.hdr")
    assert result.license == "CC0"
    assert result.bytes == len(b"fake-hdri")
    assert result.md5 == "a" * 32
    assert result.skipped is False


@pytest.mark.parametrize(
    ("status", "downloaded_files"),
    [("skipped", 1), ("ready", 0)],
)
def test_acquire_polyhaven_hdri_reports_reuse_from_item_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
    downloaded_files: int,
) -> None:
    monkeypatch.setattr(
        hdri,
        "sync_polyhaven_resources",
        lambda **_kwargs: _sync_result(
            tmp_path,
            status=status,
            downloaded_files=downloaded_files,
        ),
    )

    result = hdri.acquire_polyhaven_hdri(settings=_settings(tmp_path))

    assert result.skipped is True


def test_acquire_polyhaven_hdri_turns_sync_exception_into_clear_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail(**_kwargs: Any) -> None:
        raise ValueError("fixture listing drift")

    monkeypatch.setattr(hdri, "sync_polyhaven_resources", fail)

    with pytest.raises(
        RuntimeError,
        match="Poly Haven HDRI acquisition failed.*fixture listing drift",
    ):
        hdri.acquire_polyhaven_hdri(settings=_settings(tmp_path))


def test_acquire_polyhaven_hdri_rejects_failed_sync_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        hdri,
        "sync_polyhaven_resources",
        lambda **_kwargs: _sync_result(
            tmp_path,
            status="failed",
            error={"failure": {"message": "provider checksum mismatch"}},
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="status 'failed'.*provider checksum mismatch",
    ):
        hdri.acquire_polyhaven_hdri(settings=_settings(tmp_path))


def test_acquire_polyhaven_hdri_requires_regular_compatibility_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sync_result = _sync_result(tmp_path)
    alias = sync_result.items[0].compatibility_path
    assert alias is not None
    alias.unlink()
    alias.symlink_to(tmp_path / "outside.hdr")
    monkeypatch.setattr(hdri, "sync_polyhaven_resources", lambda **_kwargs: sync_result)

    with pytest.raises(RuntimeError, match="compatibility file is unavailable"):
        hdri.acquire_polyhaven_hdri(settings=_settings(tmp_path))
