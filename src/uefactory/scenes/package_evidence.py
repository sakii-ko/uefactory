from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from uefactory.core.identity import validate_snake_slug

SCENE_PACKAGE_EVIDENCE_POLICY = "ue_scene_package_bundle_v1"


class ScenePackageEvidenceError(RuntimeError):
    """The persistent scene package tree is unsafe, incomplete, or changed."""


@dataclass(frozen=True)
class _InventoryPackage:
    object_path: str
    class_name: str
    relative_path: str


@dataclass(frozen=True)
class _PackageFile:
    package: _InventoryPackage
    relative_path: str
    sidecar_suffix: str | None = None


_PACKAGE_SIDECAR_SUFFIXES = frozenset({".ubulk", ".uexp", ".uptnl"})


def collect_scene_package_evidence(
    project_root: Path,
    *,
    scene_id: str,
    inventory: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Hash the complete regular-file tree for one persistent scene generation."""

    canonical_id = validate_snake_slug(scene_id, field="scene_id")
    project = _canonical_project_root(project_root)
    root_relative = _scene_root_relative(canonical_id)
    root = project / Path(root_relative)
    _require_safe_directory_chain(project, root)
    expected = _inventory_packages(canonical_id, inventory)
    expected_by_path = {item.relative_path: item for item in expected}

    first_paths = _scan_regular_files(project, root)
    first_inventory = _complete_package_files(
        expected_by_path,
        first_paths,
        project_root=project,
    )
    first_files = tuple(
        _file_evidence(project, project / Path(item.relative_path), item=item)
        for item in first_inventory
    )

    second_paths = _scan_regular_files(project, root)
    if second_paths != first_paths:
        raise ScenePackageEvidenceError("scene package tree changed while evidence was collected")
    second_inventory = _complete_package_files(
        expected_by_path,
        second_paths,
        project_root=project,
    )
    if second_inventory != first_inventory:
        raise ScenePackageEvidenceError(
            "scene package inventory changed while evidence was collected"
        )
    second_files = tuple(
        _file_evidence(project, project / Path(item.relative_path), item=item)
        for item in second_inventory
    )
    if second_files != first_files:
        raise ScenePackageEvidenceError("scene package bytes changed while evidence was collected")
    return first_files


def require_valid_scene_package_evidence(
    project_root: Path,
    *,
    scene_id: str,
    inventory: Mapping[str, Any],
    packages: Any,
) -> tuple[dict[str, Any], ...]:
    """Require exact agreement between recorded evidence and the complete current tree."""

    if not isinstance(packages, list | tuple) or not packages:
        raise ScenePackageEvidenceError("scene package evidence requires a non-empty list")
    if any(not isinstance(item, Mapping) for item in packages):
        raise ScenePackageEvidenceError("scene package evidence entries must be objects")
    recorded = [dict(item) for item in packages]
    actual = collect_scene_package_evidence(
        project_root,
        scene_id=scene_id,
        inventory=inventory,
    )
    if recorded != list(actual):
        raise ScenePackageEvidenceError(
            f"scene package bytes or file inventory changed for {scene_id!r}"
        )
    return actual


def is_valid_scene_package_evidence(
    project_root: Path,
    *,
    scene_id: str,
    inventory: Mapping[str, Any],
    packages: Any,
) -> bool:
    try:
        require_valid_scene_package_evidence(
            project_root,
            scene_id=scene_id,
            inventory=inventory,
            packages=packages,
        )
    except (OSError, ScenePackageEvidenceError, TypeError, ValueError):
        return False
    return True


def scene_package_bundle_sha256(packages: Sequence[Mapping[str, Any]]) -> str:
    """Return the canonical digest used by scene manifests and catalog artifacts."""

    encoded = json.dumps(
        list(packages),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _inventory_packages(
    scene_id: str,
    inventory: Mapping[str, Any],
) -> tuple[_InventoryPackage, ...]:
    assets = inventory.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ScenePackageEvidenceError("scene inventory requires a non-empty package asset list")
    expected_prefix = f"/Game/UEF/Scenes/{scene_id}/"
    result: list[_InventoryPackage] = []
    object_paths: set[str] = set()
    relative_paths: set[str] = set()
    for index, value in enumerate(assets):
        if not isinstance(value, Mapping) or set(value) != {"object_path", "class"}:
            raise ScenePackageEvidenceError(
                f"scene inventory package {index} requires exactly object_path and class"
            )
        object_path = value.get("object_path")
        class_name = value.get("class")
        if (
            not isinstance(object_path, str)
            or object_path in object_paths
            or not object_path.startswith(expected_prefix)
            or "\\" in object_path
            or "//" in object_path
        ):
            raise ScenePackageEvidenceError(
                f"scene package object_path must stay inside {expected_prefix}: {object_path!r}"
            )
        if not isinstance(class_name, str) or not class_name:
            raise ScenePackageEvidenceError(
                f"scene package {object_path!r} requires a non-empty class"
            )
        package_name, separator, object_name = object_path.partition(".")
        if (
            separator != "."
            or not object_name
            or "." in object_name
            or "/" in object_name
            or package_name.rsplit("/", 1)[-1] != object_name
        ):
            raise ScenePackageEvidenceError(
                f"scene package has a non-canonical object path: {object_path!r}"
            )
        relative_package = package_name.removeprefix("/Game/")
        pure = PurePosixPath(relative_package)
        if (
            pure.is_absolute()
            or str(pure) != relative_package
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ScenePackageEvidenceError(
                f"scene package has a non-canonical package path: {object_path!r}"
            )
        suffix = ".umap" if class_name == "World" else ".uasset"
        relative_path = f"ue/UEFBase/Content/{relative_package}{suffix}"
        if (
            not relative_path.startswith(_scene_root_relative(scene_id) + "/")
            or relative_path in relative_paths
        ):
            raise ScenePackageEvidenceError(
                f"scene package path is duplicated or escapes its root: {relative_path!r}"
            )
        object_paths.add(object_path)
        relative_paths.add(relative_path)
        result.append(
            _InventoryPackage(
                object_path=object_path,
                class_name=class_name,
                relative_path=relative_path,
            )
        )
    return tuple(sorted(result, key=lambda item: item.object_path))


def _canonical_project_root(project_root: Path) -> Path:
    supplied = project_root.expanduser()
    if supplied.is_symlink():
        raise ScenePackageEvidenceError(f"project root may not be symbolic: {supplied}")
    try:
        return supplied.resolve(strict=True)
    except OSError as exc:
        raise ScenePackageEvidenceError(f"project root is unavailable: {supplied}") from exc


def _scene_root_relative(scene_id: str) -> str:
    return f"ue/UEFBase/Content/UEF/Scenes/{scene_id}"


def _require_safe_directory_chain(project_root: Path, root: Path) -> None:
    try:
        relative = root.relative_to(project_root)
    except ValueError as exc:  # pragma: no cover - constructed from the project root
        raise ScenePackageEvidenceError(f"scene package root escapes the project: {root}") from exc
    current = project_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ScenePackageEvidenceError(
                f"scene package root contains a symbolic directory: {current}"
            )
    if not root.is_dir():
        raise ScenePackageEvidenceError(f"scene package root is missing or not a directory: {root}")
    try:
        root.resolve(strict=True).relative_to(project_root)
    except (OSError, ValueError) as exc:
        raise ScenePackageEvidenceError(
            f"scene package root resolves outside the project: {root}"
        ) from exc


def _scan_regular_files(project_root: Path, root: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    pending = [root]
    resolved_root = root.resolve(strict=True)
    while pending:
        directory = pending.pop()
        if directory.is_symlink():
            raise ScenePackageEvidenceError(
                f"scene package tree contains a symbolic directory: {directory}"
            )
        try:
            entries = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise ScenePackageEvidenceError(
                f"could not enumerate scene package directory: {directory}"
            ) from exc
        for entry in entries:
            if entry.is_symlink():
                raise ScenePackageEvidenceError(
                    f"scene package tree may not contain symbolic links: {entry}"
                )
            try:
                resolved = entry.resolve(strict=True)
                resolved.relative_to(project_root)
                resolved.relative_to(resolved_root)
            except (OSError, ValueError) as exc:
                raise ScenePackageEvidenceError(
                    f"scene package entry resolves outside its root: {entry}"
                ) from exc
            if entry.is_dir():
                pending.append(entry)
            elif entry.is_file():
                result.append(entry)
            else:
                raise ScenePackageEvidenceError(
                    f"scene package tree contains a non-regular entry: {entry}"
                )
    if not result:
        raise ScenePackageEvidenceError(f"scene package tree contains no files: {root}")
    return tuple(sorted(result, key=lambda path: _project_relative(project_root, path)))


def _complete_package_files(
    expected_by_path: Mapping[str, _InventoryPackage],
    paths: Sequence[Path],
    *,
    project_root: Path,
) -> tuple[_PackageFile, ...]:
    actual = {_project_relative(project_root, path) for path in paths}
    expected = set(expected_by_path)
    missing = sorted(expected - actual)
    if missing:
        raise ScenePackageEvidenceError(
            "scene package inventory omits required package files: missing=" + ",".join(missing)
        )

    result = [
        _PackageFile(package=item, relative_path=item.relative_path)
        for item in expected_by_path.values()
    ]
    unexpected: list[str] = []
    for relative_path in sorted(actual - expected):
        sidecar_path = Path(relative_path)
        suffix = sidecar_path.suffix.lower()
        matches = [
            item
            for item in expected_by_path.values()
            if suffix in _PACKAGE_SIDECAR_SUFFIXES
            and Path(item.relative_path).parent == sidecar_path.parent
            and Path(item.relative_path).stem == sidecar_path.stem
        ]
        if len(matches) != 1:
            unexpected.append(relative_path)
            continue
        result.append(
            _PackageFile(
                package=matches[0],
                relative_path=relative_path,
                sidecar_suffix=suffix,
            )
        )
    if unexpected:
        raise ScenePackageEvidenceError(
            "scene package tree contains files outside its inventory: extra=" + ",".join(unexpected)
        )
    return tuple(
        sorted(
            result,
            key=lambda item: (item.package.object_path, item.relative_path),
        )
    )


def _file_evidence(
    project_root: Path,
    path: Path,
    *,
    item: _PackageFile,
) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ScenePackageEvidenceError(
            f"could not safely open scene package file: {path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise ScenePackageEvidenceError(f"scene package file is empty or non-regular: {path}")
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
            raise ScenePackageEvidenceError(
                f"scene package file changed while it was hashed: {path}"
            )
    finally:
        os.close(descriptor)
    if path.is_symlink():
        raise ScenePackageEvidenceError(f"scene package file became symbolic: {path}")
    result = {
        "object_path": item.package.object_path,
        "class": item.package.class_name,
        "path": _project_relative(project_root, path),
        "size": before.st_size,
        "sha256": digest.hexdigest(),
    }
    if item.sidecar_suffix is not None:
        result["sidecar_suffix"] = item.sidecar_suffix
    return result


def _project_relative(project_root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=True).relative_to(project_root).as_posix()
    except (OSError, ValueError) as exc:
        raise ScenePackageEvidenceError(
            f"scene package path escapes the project root: {path}"
        ) from exc


__all__ = [
    "SCENE_PACKAGE_EVIDENCE_POLICY",
    "ScenePackageEvidenceError",
    "collect_scene_package_evidence",
    "is_valid_scene_package_evidence",
    "require_valid_scene_package_evidence",
    "scene_package_bundle_sha256",
]
