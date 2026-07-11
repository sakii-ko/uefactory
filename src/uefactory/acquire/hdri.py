from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from uefactory.acquire.polyhaven_resource_sync import sync_polyhaven_resources
from uefactory.core.config import Settings

DEFAULT_HDRI_ASSET = "studio_small_03"
DEFAULT_HDRI_RESOLUTION = "1k"


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
    try:
        result = sync_polyhaven_resources(
            settings=settings,
            kind="hdri",
            limit=1,
            resolution=resolution,
            source_ids=(asset_id,),
            force=force,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Poly Haven HDRI acquisition failed for {asset_id!r} at {resolution!r}: {exc}"
        ) from exc
    if len(result.items) != 1:
        raise RuntimeError(
            f"Poly Haven HDRI acquisition returned {len(result.items)} items for {asset_id!r}"
        )
    item = result.items[0]
    if item.source_id != asset_id or item.kind != "hdri":
        raise RuntimeError(f"Poly Haven HDRI acquisition returned the wrong item for {asset_id!r}")
    if item.status not in {"ready", "skipped"}:
        detail = ""
        if item.error is not None:
            message = item.error.get("message")
            failure = item.error.get("failure")
            if not isinstance(message, str) and isinstance(failure, Mapping):
                message = failure.get("message")
            if isinstance(message, str) and message:
                detail = f": {message}"
        raise RuntimeError(
            f"Poly Haven HDRI acquisition ended with status {item.status!r} for "
            f"{asset_id!r}{detail}"
        )
    file_path = item.compatibility_path
    metadata_path = item.compatibility_metadata_path
    if file_path is None or not file_path.is_file() or file_path.is_symlink():
        raise RuntimeError(f"Poly Haven HDRI compatibility file is unavailable for {asset_id!r}")
    if metadata_path is None or not metadata_path.is_file() or metadata_path.is_symlink():
        raise RuntimeError(f"Poly Haven HDRI metadata is unavailable for {asset_id!r}")
    metadata = _read_compatibility_metadata(metadata_path, asset_id, resolution)
    return HdriDownloadResult(
        asset_id=asset_id,
        resolution=resolution,
        file_path=file_path,
        metadata_path=metadata_path,
        source_url=_metadata_string(metadata, "source_url", metadata_path),
        license="CC0",
        bytes=_metadata_positive_int(metadata, "bytes", metadata_path),
        md5=_metadata_string(metadata, "md5", metadata_path),
        skipped=item.status == "skipped" or item.downloaded_files == 0,
    )


def _read_compatibility_metadata(
    path: Path,
    asset_id: str,
    resolution: str,
) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read Poly Haven HDRI compatibility metadata: {path}") from exc
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise RuntimeError(f"Poly Haven HDRI compatibility metadata is invalid: {path}")
    if payload.get("asset_id") != asset_id or payload.get("resolution") != resolution:
        raise RuntimeError(f"Poly Haven HDRI compatibility metadata identity mismatch: {path}")
    if payload.get("license") != "CC0":
        raise RuntimeError(f"Poly Haven HDRI compatibility metadata license mismatch: {path}")
    return payload


def _metadata_string(payload: dict[str, object], field: str, path: Path) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Poly Haven HDRI metadata {field!r} is invalid: {path}")
    return value


def _metadata_positive_int(payload: dict[str, object], field: str, path: Path) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError(f"Poly Haven HDRI metadata {field!r} is invalid: {path}")
    return value
