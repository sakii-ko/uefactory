from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from uefactory.acquire import hdri
from uefactory.core.config import Settings


def test_acquire_polyhaven_hdri_downloads_and_writes_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"fake-hdri"
    md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()

    def fake_urlopen(request: Any, timeout: int) -> _Response:
        url = request.full_url
        if url.endswith("/files/studio_small_03"):
            return _Response(
                json.dumps(
                    {
                        "hdri": {
                            "1k": {
                                "hdr": {
                                    "url": "https://example.test/studio_small_03_1k.hdr",
                                    "size": len(payload),
                                    "md5": md5,
                                }
                            }
                        }
                    }
                ).encode()
            )
        if url == "https://example.test/studio_small_03_1k.hdr":
            return _Response(payload)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(hdri.urllib.request, "urlopen", fake_urlopen)
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data")

    result = hdri.acquire_polyhaven_hdri(settings=settings)

    assert result.file_path.read_bytes() == payload
    assert result.skipped is False
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["license"] == "CC0"
    assert metadata["md5"] == md5


def test_acquire_polyhaven_hdri_reuses_checked_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"fake-hdri"
    md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data")
    file_path = settings.data_dir / "hdri/studio_small_03_1k.hdr"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(payload)

    def fake_urlopen(request: Any, timeout: int) -> _Response:
        assert request.full_url.endswith("/files/studio_small_03")
        return _Response(
            json.dumps(
                {
                    "hdri": {
                        "1k": {
                            "hdr": {
                                "url": "https://example.test/studio_small_03_1k.hdr",
                                "size": len(payload),
                                "md5": md5,
                            }
                        }
                    }
                }
            ).encode()
        )

    monkeypatch.setattr(hdri.urllib.request, "urlopen", fake_urlopen)

    result = hdri.acquire_polyhaven_hdri(settings=settings)

    assert result.skipped is True


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
