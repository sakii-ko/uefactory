from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

_RECORD_DIGEST_DOMAIN = b"uefactory.external-scene-record.v1\0"
_GLB_MAGIC = b"glTF"
_GLB_JSON_CHUNK = 0x4E4F534A
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_GLB_JSON_BYTES = 64 * 1024 * 1024
_SOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_UID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

_OPEN_LICENSES = frozenset(
    {
        "CC0-1.0",
        "CC-BY-3.0",
        "CC-BY-4.0",
        "CC-BY-SA-3.0",
        "CC-BY-SA-4.0",
    }
)
_NC_LICENSES = frozenset(
    {
        "CC-BY-NC-3.0",
        "CC-BY-NC-4.0",
        "CC-BY-NC-ND-3.0",
        "CC-BY-NC-ND-4.0",
        "CC-BY-NC-SA-3.0",
        "CC-BY-NC-SA-4.0",
    }
)
_LICENSE_ALIASES = {
    "cc0": "CC0-1.0",
    "cc0-1.0": "CC0-1.0",
    **{value.casefold(): value for value in _OPEN_LICENSES | _NC_LICENSES},
}


class BlackMythLibraryError(RuntimeError):
    """The external scene library cannot be trusted or read safely."""


@dataclass(frozen=True, slots=True)
class ExternalSceneRecord:
    """A checked, read-only GLB candidate with explicit provenance."""

    library_uid: str
    source: str
    source_id: str
    title: str
    source_url: str
    author: str
    license: str
    license_tier: Literal["open", "nc"]
    glb_path: Path
    sha256: str
    bytes: int
    manifest_path: Path | None
    redistributable: bool
    canonical_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "library_uid": self.library_uid,
            "source": self.source,
            "source_id": self.source_id,
            "title": self.title,
            "source_url": self.source_url,
            "author": self.author,
            "license": self.license,
            "license_tier": self.license_tier,
            "glb_path": str(self.glb_path),
            "sha256": self.sha256,
            "bytes": self.bytes,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "redistributable": self.redistributable,
            "canonical_digest": self.canonical_digest,
        }


@dataclass(frozen=True, slots=True)
class QuarantinedScene:
    """A scene manifest deliberately excluded from build candidates."""

    library_uid: str
    source_id: str
    title: str
    license: str
    manifest_path: Path
    reason: Literal["unsupported_license", "missing_glb", "invalid_glb", "external_glb"]

    def as_dict(self) -> dict[str, str]:
        return {
            "library_uid": self.library_uid,
            "source_id": self.source_id,
            "title": self.title,
            "license": self.license,
            "manifest_path": str(self.manifest_path),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SceneLibraryScan:
    root: Path
    records: tuple[ExternalSceneRecord, ...]
    quarantined: tuple[QuarantinedScene, ...]


def scan_blackmyth_scene_library(root: Path) -> SceneLibraryScan:
    """Scan asset-library scene manifests without modifying the source library.

    Only regular, non-symlink GLB files under asset-library/derived/<uid> can
    become records. Unsupported licenses and non-self-contained GLBs are reported
    as quarantined; structural and path-safety violations fail the complete scan.
    """

    canonical_root = _checked_root(root)
    library_dir = _checked_directory(canonical_root, canonical_root / "asset-library")
    manifests_dir = _checked_directory(library_dir, library_dir / "manifests")
    derived_dir = _checked_directory(library_dir, library_dir / "derived")

    records: list[ExternalSceneRecord] = []
    quarantined: list[QuarantinedScene] = []
    for manifest_path in sorted(manifests_dir.iterdir(), key=lambda item: item.name):
        _reject_sensitive_path(manifest_path)
        if manifest_path.is_symlink():
            raise BlackMythLibraryError("manifest directory contains a symlink")
        if not manifest_path.name.endswith(".meta.json"):
            continue
        _require_regular_file(manifests_dir, manifest_path, label="manifest")
        manifest = _read_json_object(manifest_path)
        category = manifest.get("category")
        if category != "scene":
            continue

        uid = _manifest_uid(manifest, manifest_path)
        source_id = _required_string(manifest, "source_id", max_length=128)
        if not _SOURCE_ID_PATTERN.fullmatch(source_id):
            raise BlackMythLibraryError(f"scene manifest {uid}: invalid source_id")
        title = _required_string(manifest, "asset_name", max_length=512)
        license_value = _required_string(manifest, "license", max_length=128)
        source_url = _optional_string(manifest, "source_url", max_length=2_048)
        author = _optional_string(manifest, "author", max_length=512)
        redistributable = manifest.get("is_redistributable")
        if not isinstance(redistributable, bool):
            raise BlackMythLibraryError(
                f"scene manifest {uid}: is_redistributable must be a boolean"
            )

        glb_path = _manifest_glb_path(
            manifest=manifest,
            uid=uid,
            library_dir=library_dir,
            derived_dir=derived_dir,
        )
        license_class = _classify_license(license_value)
        if license_class is None:
            quarantined.append(
                QuarantinedScene(
                    library_uid=uid,
                    source_id=source_id,
                    title=title,
                    license=license_value,
                    manifest_path=manifest_path,
                    reason="unsupported_license",
                )
            )
            continue
        license_id, license_tier = license_class

        if not glb_path.exists():
            quarantined.append(
                QuarantinedScene(
                    library_uid=uid,
                    source_id=source_id,
                    title=title,
                    license=license_id,
                    manifest_path=manifest_path,
                    reason="missing_glb",
                )
            )
            continue
        _require_regular_file(derived_dir, glb_path, label=f"derived GLB for {uid}")
        glb_status = _validate_self_contained_glb(glb_path)
        if glb_status is not None:
            quarantined.append(
                QuarantinedScene(
                    library_uid=uid,
                    source_id=source_id,
                    title=title,
                    license=license_id,
                    manifest_path=manifest_path,
                    reason=glb_status,
                )
            )
            continue

        glb_sha256, glb_bytes = _sha256_and_size(glb_path)
        relative_glb = glb_path.relative_to(library_dir).as_posix()
        relative_manifest = manifest_path.relative_to(library_dir).as_posix()
        canonical_digest = _canonical_record_digest(
            {
                "author": author,
                "bytes": glb_bytes,
                "glb_path": relative_glb,
                "library_uid": uid,
                "license": license_id,
                "license_tier": license_tier,
                "manifest_path": relative_manifest,
                "redistributable": redistributable,
                "sha256": glb_sha256,
                "source": "blackmyth_asset_library",
                "source_id": source_id,
                "source_url": source_url,
                "title": title,
            }
        )
        records.append(
            ExternalSceneRecord(
                library_uid=uid,
                source="blackmyth_asset_library",
                source_id=source_id,
                title=title,
                source_url=source_url,
                author=author,
                license=license_id,
                license_tier=license_tier,
                glb_path=glb_path,
                sha256=glb_sha256,
                bytes=glb_bytes,
                manifest_path=manifest_path,
                redistributable=redistributable,
                canonical_digest=canonical_digest,
            )
        )

    return SceneLibraryScan(
        root=canonical_root,
        records=tuple(sorted(records, key=lambda item: (item.library_uid, item.source_id))),
        quarantined=tuple(sorted(quarantined, key=lambda item: (item.library_uid, item.source_id))),
    )


def research_only_external_glb(
    *,
    root: Path,
    glb_path: Path,
    source_id: str,
    title: str,
    source_url: str = "",
    author: str = "",
) -> ExternalSceneRecord:
    """Create an explicit non-redistributable record for an extracted GLB.

    root is the caller-approved read-only boundary. The GLB must be a regular,
    non-symlink, self-contained file inside it. The function never copies or writes
    source data, and the resulting license cannot be promoted by the caller.
    """

    canonical_root = _checked_root(root)
    checked_source_id = _checked_value(source_id, "source_id", max_length=128)
    if not _SOURCE_ID_PATTERN.fullmatch(checked_source_id):
        raise BlackMythLibraryError("research-only source_id is invalid")
    checked_title = _checked_value(title, "title", max_length=512)
    checked_source_url = _checked_optional_value(source_url, "source_url", max_length=2_048)
    checked_author = _checked_optional_value(author, "author", max_length=512)
    candidate = glb_path.expanduser()
    if not candidate.is_absolute():
        candidate = canonical_root / candidate
    candidate = _safe_descendant(canonical_root, candidate, label="research-only GLB")
    _reject_sensitive_path(candidate)
    if candidate.suffix.casefold() != ".glb":
        raise BlackMythLibraryError("research-only scene source must be a GLB")
    _require_regular_file(canonical_root, candidate, label="research-only GLB")
    glb_status = _validate_self_contained_glb(candidate)
    if glb_status is not None:
        raise BlackMythLibraryError(
            "research-only GLB is invalid"
            if glb_status == "invalid_glb"
            else "research-only GLB has external dependencies"
        )

    sha256, size = _sha256_and_size(candidate)
    relative_glb = candidate.relative_to(canonical_root).as_posix()
    payload: dict[str, Any] = {
        "author": checked_author,
        "bytes": size,
        "glb_path": relative_glb,
        "library_uid": checked_source_id,
        "license": "LicenseRef-Research-Only",
        "license_tier": "nc",
        "manifest_path": None,
        "redistributable": False,
        "sha256": sha256,
        "source": "blackmyth_research",
        "source_id": checked_source_id,
        "source_url": checked_source_url,
        "title": checked_title,
    }
    return ExternalSceneRecord(
        library_uid=checked_source_id,
        source="blackmyth_research",
        source_id=checked_source_id,
        title=checked_title,
        source_url=checked_source_url,
        author=checked_author,
        license="LicenseRef-Research-Only",
        license_tier="nc",
        glb_path=candidate,
        sha256=sha256,
        bytes=size,
        manifest_path=None,
        redistributable=False,
        canonical_digest=_canonical_record_digest(payload),
    )


def _checked_root(root: Path) -> Path:
    requested = Path(os.path.abspath(root.expanduser()))
    _reject_sensitive_path(requested)
    try:
        info = requested.lstat()
    except OSError as exc:
        raise BlackMythLibraryError("scene library root is not accessible") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BlackMythLibraryError("scene library root must be a non-symlink directory")
    return requested.resolve(strict=True)


def _checked_directory(scope: Path, path: Path) -> Path:
    checked = _safe_descendant(scope, path, label="library directory")
    try:
        info = checked.lstat()
    except OSError as exc:
        raise BlackMythLibraryError("required scene library directory is not accessible") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BlackMythLibraryError("required scene library path must be a non-symlink directory")
    return checked


def _manifest_uid(manifest: dict[str, Any], manifest_path: Path) -> str:
    uid = _required_string(manifest, "uid", max_length=128)
    if not _UID_PATTERN.fullmatch(uid):
        raise BlackMythLibraryError("scene manifest has an invalid uid")
    if manifest_path.name != f"{uid}.meta.json":
        raise BlackMythLibraryError(f"scene manifest {uid}: uid does not match filename")
    return uid


def _manifest_glb_path(
    *,
    manifest: dict[str, Any],
    uid: str,
    library_dir: Path,
    derived_dir: Path,
) -> Path:
    formats = manifest.get("normalized_formats")
    if not isinstance(formats, dict):
        raise BlackMythLibraryError(f"scene manifest {uid}: normalized_formats must be an object")
    raw_glb = formats.get("glb")
    if raw_glb is None:
        conventional_path = derived_dir / uid / f"{uid}.glb"
        _reject_sensitive_path(conventional_path)
        return conventional_path
    if not isinstance(raw_glb, str) or not raw_glb or "\x00" in raw_glb:
        raise BlackMythLibraryError(f"scene manifest {uid}: invalid derived GLB path")
    raw_path = Path(raw_glb).expanduser()
    if not raw_path.is_absolute():
        raw_path = library_dir / raw_path
    glb_path = _safe_descendant(derived_dir, raw_path, label=f"derived GLB for {uid}")
    _reject_sensitive_path(glb_path)
    if glb_path.suffix.casefold() != ".glb":
        raise BlackMythLibraryError(f"scene manifest {uid}: derived scene is not a GLB")
    relative = glb_path.relative_to(derived_dir)
    if not relative.parts or relative.parts[0] != uid:
        raise BlackMythLibraryError(
            f"scene manifest {uid}: derived GLB is outside its uid directory"
        )
    return glb_path


def _safe_descendant(scope: Path, path: Path, *, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(scope)
    except ValueError as exc:
        raise BlackMythLibraryError(f"{label} escapes its approved root") from exc
    current = scope
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BlackMythLibraryError(f"{label} is not accessible") from exc
        if stat.S_ISLNK(info.st_mode):
            raise BlackMythLibraryError(f"{label} contains a symlink")
    resolved = absolute.resolve(strict=False)
    try:
        resolved.relative_to(scope)
    except ValueError as exc:
        raise BlackMythLibraryError(f"{label} resolves outside its approved root") from exc
    return resolved


def _require_regular_file(scope: Path, path: Path, *, label: str) -> None:
    checked = _safe_descendant(scope, path, label=label)
    _reject_sensitive_path(checked)
    try:
        info = checked.lstat()
    except OSError as exc:
        raise BlackMythLibraryError(f"{label} is not accessible") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BlackMythLibraryError(f"{label} must be a regular non-symlink file")


def _reject_sensitive_path(path: Path) -> None:
    for part in path.parts:
        name = part.casefold()
        if name in {".env", "bmw_key.txt"} or name.endswith(".usmap"):
            raise BlackMythLibraryError("sensitive paths are outside the scene scanner contract")


def _open_regular(path: Path) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BlackMythLibraryError("scene source changed or cannot be opened safely") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BlackMythLibraryError("scene source is not a regular file")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _read_json_object(path: Path) -> dict[str, Any]:
    with _open_regular(path) as file:
        payload = file.read(_MAX_MANIFEST_BYTES + 1)
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise BlackMythLibraryError("scene manifest exceeds the size limit")
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, BlackMythLibraryError) as exc:
        raise BlackMythLibraryError("scene manifest is not valid strict JSON") from exc
    if not isinstance(value, dict):
        raise BlackMythLibraryError("scene manifest root must be an object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BlackMythLibraryError("scene manifest contains a duplicate key")
        result[key] = value
    return result


def _validate_self_contained_glb(
    path: Path,
) -> Literal["invalid_glb", "external_glb"] | None:
    try:
        with _open_regular(path) as file:
            header = file.read(12)
            if len(header) != 12:
                return "invalid_glb"
            magic, version, declared_size = struct.unpack("<4sII", header)
            actual_size = os.fstat(file.fileno()).st_size
            if magic != _GLB_MAGIC or version != 2 or declared_size != actual_size:
                return "invalid_glb"
            chunk_header = file.read(8)
            if len(chunk_header) != 8:
                return "invalid_glb"
            json_size, chunk_type = struct.unpack("<II", chunk_header)
            if chunk_type != _GLB_JSON_CHUNK or json_size % 4 or json_size > _MAX_GLB_JSON_BYTES:
                return "invalid_glb"
            json_payload = file.read(json_size)
            if len(json_payload) != json_size:
                return "invalid_glb"
            while file.tell() < actual_size:
                if actual_size - file.tell() < 8:
                    return "invalid_glb"
                trailing_header = file.read(8)
                trailing_size, _ = struct.unpack("<II", trailing_header)
                if trailing_size % 4 or trailing_size > actual_size - file.tell():
                    return "invalid_glb"
                file.seek(trailing_size, os.SEEK_CUR)
            if file.tell() != actual_size:
                return "invalid_glb"
    except BlackMythLibraryError:
        raise
    try:
        document = json.loads(json_payload.rstrip(b" \t\r\n\x00"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid_glb"
    asset = document.get("asset") if isinstance(document, dict) else None
    if not isinstance(asset, dict) or asset.get("version") != "2.0":
        return "invalid_glb"
    for collection in ("buffers", "images"):
        entries = document.get(collection, [])
        if not isinstance(entries, list):
            return "invalid_glb"
        for entry in entries:
            if not isinstance(entry, dict):
                return "invalid_glb"
            uri = entry.get("uri")
            if uri is not None and (
                not isinstance(uri, str) or not uri.casefold().startswith("data:")
            ):
                return "external_glb"
    return None


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with _open_regular(path) as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _classify_license(value: str) -> tuple[str, Literal["open", "nc"]] | None:
    canonical = _LICENSE_ALIASES.get(value.casefold())
    if canonical in _OPEN_LICENSES:
        return canonical, "open"
    if canonical in _NC_LICENSES:
        return canonical, "nc"
    return None


def _canonical_record_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"schema_version": 1, **payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(_RECORD_DIGEST_DOMAIN + encoded).hexdigest()


def _required_string(manifest: dict[str, Any], key: str, *, max_length: int) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise BlackMythLibraryError(f"scene manifest field {key} must be a non-empty string")
    if len(value) > max_length or "\x00" in value:
        raise BlackMythLibraryError(f"scene manifest field {key} is invalid")
    return value


def _optional_string(manifest: dict[str, Any], key: str, *, max_length: int) -> str:
    value = manifest.get(key)
    if value is None:
        return ""
    if not isinstance(value, str) or value != value.strip():
        raise BlackMythLibraryError(f"scene manifest field {key} must be a string")
    if len(value) > max_length or "\x00" in value:
        raise BlackMythLibraryError(f"scene manifest field {key} is invalid")
    return value


def _checked_value(value: str, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BlackMythLibraryError(f"{label} must be a non-empty string")
    if len(value) > max_length or "\x00" in value:
        raise BlackMythLibraryError(f"{label} is invalid")
    return value


def _checked_optional_value(value: str, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise BlackMythLibraryError(f"{label} must be a string")
    if len(value) > max_length or "\x00" in value:
        raise BlackMythLibraryError(f"{label} is invalid")
    return value
