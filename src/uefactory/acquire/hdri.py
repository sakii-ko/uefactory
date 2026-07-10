from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from uefactory import __version__
from uefactory.core.config import Settings

POLYHAVEN_FILES_URL = "https://api.polyhaven.com/files/{asset_id}"
POLYHAVEN_ASSET_URL = "https://polyhaven.com/a/{asset_id}"
DEFAULT_HDRI_ASSET = "studio_small_03"
DEFAULT_HDRI_RESOLUTION = "1k"
USER_AGENT = f"UEFactory/{__version__} research downloader"


@dataclass(frozen=True)
class HdriDownloadResult:
    asset_id: str
    resolution: str
    file_path: Path
    metadata_path: Path
    source_url: str
    license: str
    bytes: int
    md5: str
    skipped: bool


def acquire_polyhaven_hdri(
    *,
    settings: Settings,
    asset_id: str = DEFAULT_HDRI_ASSET,
    resolution: str = DEFAULT_HDRI_RESOLUTION,
    force: bool = False,
) -> HdriDownloadResult:
    hdri_dir = settings.data_dir / "hdri"
    hdri_dir.mkdir(parents=True, exist_ok=True)
    files = _fetch_json(POLYHAVEN_FILES_URL.format(asset_id=asset_id))
    entry = _hdri_entry(files, asset_id, resolution)
    source_url = _string(entry["url"], f"{asset_id}.{resolution}.url")
    expected_md5 = _string(entry["md5"], f"{asset_id}.{resolution}.md5")
    expected_size = int(entry["size"])

    file_path = hdri_dir / f"{asset_id}_{resolution}.hdr"
    metadata_path = hdri_dir / f"{asset_id}_{resolution}.json"
    skipped = False
    if file_path.exists() and not force:
        actual_md5 = _md5_file(file_path)
        if actual_md5 != expected_md5:
            raise RuntimeError(
                f"HDRI exists but md5 mismatch: {file_path} "
                f"expected={expected_md5} actual={actual_md5}"
            )
        skipped = True
    else:
        tmp_path = file_path.with_suffix(file_path.suffix + ".part")
        _download(source_url, tmp_path)
        actual_md5 = _md5_file(tmp_path)
        if actual_md5 != expected_md5:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"HDRI download md5 mismatch: {source_url} "
                f"expected={expected_md5} actual={actual_md5}"
            )
        actual_size = tmp_path.stat().st_size
        if actual_size != expected_size:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"HDRI download size mismatch: {source_url} "
                f"expected={expected_size} actual={actual_size}"
            )
        tmp_path.replace(file_path)

    metadata = {
        "asset_id": asset_id,
        "resolution": resolution,
        "file": str(file_path),
        "source_url": source_url,
        "asset_url": POLYHAVEN_ASSET_URL.format(asset_id=asset_id),
        "license": "CC0",
        "bytes": expected_size,
        "md5": expected_md5,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return HdriDownloadResult(
        asset_id=asset_id,
        resolution=resolution,
        file_path=file_path,
        metadata_path=metadata_path,
        source_url=source_url,
        license="CC0",
        bytes=expected_size,
        md5=expected_md5,
        skipped=skipped,
    )


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError(f"PolyHaven returned non-object JSON: {url}")
    return payload


def _download(url: str, path: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, path.open("wb") as file:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            file.write(chunk)


def _hdri_entry(files: dict[str, Any], asset_id: str, resolution: str) -> dict[str, Any]:
    try:
        entry = files["hdri"][resolution]["hdr"]
    except KeyError as exc:
        raise RuntimeError(f"PolyHaven HDRI {asset_id!r} has no {resolution!r} HDR file") from exc
    if not isinstance(entry, dict):
        raise RuntimeError(f"PolyHaven HDRI {asset_id!r} {resolution!r} entry is invalid")
    return entry


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{path}: expected non-empty string")
    return value


def _md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
