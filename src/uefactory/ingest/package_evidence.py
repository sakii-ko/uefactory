from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, TypeGuard

from uefactory.core.identity import validate_asset_id

PACKAGE_BUNDLE_EVIDENCE_POLICY = "ue_ingested_package_bundle_v1"
_DIGEST_DOMAIN = b"uefactory:ue-ingested-package-bundle:v1\x00"
_FILE_KEYS = {"path", "size", "sha256"}
_EVIDENCE_KEYS = {"policy", "root", "files", "package_bundle_sha256"}


class PackageBundleEvidenceError(RuntimeError):
    """The on-disk UE package bundle is unsafe, incomplete, or has changed."""


def collect_package_bundle_evidence(
    project_root: Path,
    *,
    asset_id: str,
    imported_object_paths: Sequence[str],
) -> dict[str, Any]:
    """Hash the complete regular-file bundle for one imported UE asset."""

    validate_asset_id(asset_id)
    root_relative = _bundle_root_relative(asset_id)
    root = project_root / Path(root_relative)
    _require_safe_directory_chain(project_root, root, label="UE package bundle root")

    first_paths = _scan_regular_files(project_root, root)
    if not first_paths:
        raise PackageBundleEvidenceError(f"UE package bundle contains no files: {root}")
    files = [_file_evidence(project_root, path) for path in first_paths]
    second_paths = _scan_regular_files(project_root, root)
    if second_paths != first_paths:
        raise PackageBundleEvidenceError("UE package bundle changed while evidence was collected")
    second_files = [_file_evidence(project_root, path) for path in second_paths]
    if second_files != files:
        raise PackageBundleEvidenceError(
            "UE package bundle bytes changed while evidence was collected"
        )

    required_uassets = _required_uasset_paths(asset_id, imported_object_paths)
    actual_paths = {str(item["path"]) for item in files}
    missing = sorted(required_uassets - actual_paths)
    if missing:
        raise PackageBundleEvidenceError(
            "UE package bundle omits imported-object .uasset files: " + ", ".join(missing)
        )

    canonical = {
        "policy": PACKAGE_BUNDLE_EVIDENCE_POLICY,
        "root": root_relative,
        "files": files,
    }
    return {
        **canonical,
        "package_bundle_sha256": package_bundle_sha256(canonical),
    }


def require_valid_package_bundle_evidence(
    project_root: Path,
    *,
    asset_id: str,
    imported_object_paths: Sequence[str],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Require canonical evidence and exact agreement with the current package tree."""

    canonical = _canonical_evidence_payload(
        asset_id=asset_id,
        imported_object_paths=imported_object_paths,
        evidence=evidence,
    )
    actual = collect_package_bundle_evidence(
        project_root,
        asset_id=asset_id,
        imported_object_paths=imported_object_paths,
    )
    if actual != canonical:
        raise PackageBundleEvidenceError(
            f"UE package bundle bytes or file inventory changed for {asset_id!r}"
        )
    return actual


def is_valid_package_bundle_evidence(
    project_root: Path,
    *,
    asset_id: str,
    imported_object_paths: Sequence[str],
    evidence: Any,
) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    try:
        require_valid_package_bundle_evidence(
            project_root,
            asset_id=asset_id,
            imported_object_paths=imported_object_paths,
            evidence=evidence,
        )
    except (OSError, PackageBundleEvidenceError, TypeError, ValueError):
        return False
    return True


def package_bundle_sha256(value: Mapping[str, Any]) -> str:
    """Return the domain-separated digest for a canonical package inventory."""

    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(_DIGEST_DOMAIN + encoded).hexdigest()


def _canonical_evidence_payload(
    *,
    asset_id: str,
    imported_object_paths: Sequence[str],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    validate_asset_id(asset_id)
    if set(evidence) != _EVIDENCE_KEYS:
        raise PackageBundleEvidenceError(
            "UE package evidence requires exactly: " + ", ".join(sorted(_EVIDENCE_KEYS))
        )
    root_relative = _bundle_root_relative(asset_id)
    if (
        evidence.get("policy") != PACKAGE_BUNDLE_EVIDENCE_POLICY
        or evidence.get("root") != root_relative
    ):
        raise PackageBundleEvidenceError("UE package evidence policy or root is invalid")
    raw_files = evidence.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise PackageBundleEvidenceError("UE package evidence requires a non-empty files list")

    files: list[dict[str, str | int]] = []
    previous_path: str | None = None
    for index, item in enumerate(raw_files):
        if not isinstance(item, Mapping) or set(item) != _FILE_KEYS:
            raise PackageBundleEvidenceError(
                f"UE package evidence file {index} has an invalid shape"
            )
        path = item.get("path")
        size = item.get("size")
        sha256 = item.get("sha256")
        if not isinstance(path, str) or not _is_canonical_bundle_file_path(path, root_relative):
            raise PackageBundleEvidenceError(
                f"UE package evidence file {index} has an invalid repo-relative path"
            )
        if previous_path is not None and path <= previous_path:
            raise PackageBundleEvidenceError(
                "UE package evidence file paths must be unique and sorted"
            )
        if type(size) is not int or size <= 0:
            raise PackageBundleEvidenceError(
                f"UE package evidence file {index} requires an exact positive integer size"
            )
        if not _is_sha256(sha256):
            raise PackageBundleEvidenceError(
                f"UE package evidence file {index} requires a lowercase SHA-256"
            )
        files.append({"path": path, "size": size, "sha256": sha256})
        previous_path = path

    required_uassets = _required_uasset_paths(asset_id, imported_object_paths)
    missing = sorted(required_uassets - {str(item["path"]) for item in files})
    if missing:
        raise PackageBundleEvidenceError(
            "UE package evidence omits imported-object .uasset files: " + ", ".join(missing)
        )

    canonical = {
        "policy": PACKAGE_BUNDLE_EVIDENCE_POLICY,
        "root": root_relative,
        "files": files,
    }
    digest = evidence.get("package_bundle_sha256")
    if not _is_sha256(digest) or digest != package_bundle_sha256(canonical):
        raise PackageBundleEvidenceError("UE package evidence bundle digest is invalid")
    return {**canonical, "package_bundle_sha256": digest}


def _bundle_root_relative(asset_id: str) -> str:
    return f"ue/UEFBase/Content/UEF/Ingested/{asset_id}"


def _required_uasset_paths(asset_id: str, imported_object_paths: Sequence[str]) -> set[str]:
    if isinstance(imported_object_paths, str) or not imported_object_paths:
        raise PackageBundleEvidenceError(
            "UE package evidence requires non-empty imported_object_paths"
        )
    expected_prefix = f"/Game/UEF/Ingested/{asset_id}/"
    result: set[str] = set()
    seen: set[str] = set()
    for index, object_path in enumerate(imported_object_paths):
        if not isinstance(object_path, str) or object_path in seen:
            raise PackageBundleEvidenceError(
                f"imported_object_paths entry {index} is invalid or duplicated"
            )
        seen.add(object_path)
        if (
            not object_path.startswith(expected_prefix)
            or "\\" in object_path
            or "//" in object_path
        ):
            raise PackageBundleEvidenceError(
                f"imported object path must stay inside {expected_prefix}: {object_path!r}"
            )
        package_path, separator, object_name = object_path.partition(".")
        if separator and (not object_name or "." in object_name or "/" in object_name):
            raise PackageBundleEvidenceError(
                f"imported object path is not canonical: {object_path!r}"
            )
        relative_package = package_path.removeprefix("/Game/")
        pure = PurePosixPath(relative_package)
        if (
            pure.is_absolute()
            or str(pure) != relative_package
            or any(part in {"", ".", ".."} for part in pure.parts)
            or not package_path.startswith(expected_prefix)
        ):
            raise PackageBundleEvidenceError(
                f"imported object package is not canonical: {object_path!r}"
            )
        result.add(f"ue/UEFBase/Content/{relative_package}.uasset")
    return result


def _require_safe_directory_chain(project_root: Path, directory: Path, *, label: str) -> None:
    root = project_root.resolve()
    try:
        relative = directory.relative_to(project_root)
    except ValueError as exc:
        raise PackageBundleEvidenceError(f"{label} escapes the project root: {directory}") from exc
    current = project_root
    if current.is_symlink():
        raise PackageBundleEvidenceError(f"project root may not be symbolic: {current}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PackageBundleEvidenceError(f"{label} contains a symbolic directory: {current}")
    if not directory.is_dir():
        raise PackageBundleEvidenceError(f"{label} is missing or not a directory: {directory}")
    try:
        directory.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise PackageBundleEvidenceError(f"{label} resolves outside the project root") from exc


def _scan_regular_files(project_root: Path, root: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    pending = [root]
    resolved_project = project_root.resolve()
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise PackageBundleEvidenceError(
                f"could not enumerate UE package bundle directory: {directory}"
            ) from exc
        for entry in entries:
            if entry.is_symlink():
                raise PackageBundleEvidenceError(
                    f"UE package bundle may not contain symbolic links: {entry}"
                )
            try:
                resolved = entry.resolve(strict=True)
                resolved.relative_to(resolved_project)
                resolved.relative_to(root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise PackageBundleEvidenceError(
                    f"UE package bundle entry resolves outside its root: {entry}"
                ) from exc
            if entry.is_dir():
                pending.append(entry)
            elif entry.is_file():
                result.append(entry)
            else:
                raise PackageBundleEvidenceError(
                    f"UE package bundle contains a non-regular entry: {entry}"
                )
    return tuple(sorted(result, key=lambda path: _project_relative(project_root, path)))


def _file_evidence(project_root: Path, path: Path) -> dict[str, str | int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PackageBundleEvidenceError(
            f"could not safely open UE package bundle file: {path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise PackageBundleEvidenceError(
                f"UE package bundle file is empty or non-regular: {path}"
            )
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PackageBundleEvidenceError(
                f"UE package bundle file changed while it was hashed: {path}"
            )
    finally:
        os.close(descriptor)
    if path.is_symlink():
        raise PackageBundleEvidenceError(f"UE package bundle file became symbolic: {path}")
    return {
        "path": _project_relative(project_root, path),
        "size": before.st_size,
        "sha256": digest.hexdigest(),
    }


def _project_relative(project_root: Path, path: Path) -> str:
    try:
        relative = path.resolve(strict=True).relative_to(project_root.resolve())
    except (OSError, ValueError) as exc:
        raise PackageBundleEvidenceError(
            f"UE package path escapes the project root: {path}"
        ) from exc
    return relative.as_posix()


def _is_canonical_bundle_file_path(path: str, root_relative: str) -> bool:
    if "\\" in path or not path.startswith(root_relative + "/"):
        return False
    pure = PurePosixPath(path)
    return (
        not pure.is_absolute()
        and str(pure) == path
        and all(part not in {"", ".", ".."} for part in pure.parts)
    )


def _is_sha256(value: Any) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
