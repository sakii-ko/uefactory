from __future__ import annotations

import hashlib
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from uefactory.ingest.source_structure import (
    SourceStructureError,
    inspect_source_structure,
    read_gltf_document,
)
from uefactory.ingest.spec import (
    IngestAssetSpec,
    IngestBatchSpec,
    IngestSpecError,
    validate_asset_id,
)

_BUNDLE_HASH_DOMAIN = b"UEFactory asset bundle SHA-256 v1\0"
_CONTENT_HASH_DOMAIN = b"UEFactory asset content multiset SHA-256 v1\0"
_CONTENT_FILE_HASH_DOMAIN = b"UEFactory asset content file SHA-256 v1\0"


class StagingError(RuntimeError):
    """Raised when a local asset bundle cannot be staged safely."""


@dataclass(frozen=True)
class StagedAsset:
    asset_id: str
    raw_dir: Path
    raw_path: Path
    files: tuple[Path, ...]
    bundle_sha256: str
    content_sha256: str
    source_structure: dict[str, object]
    source_structure_sha256: str
    changed: bool

    @property
    def sha256(self) -> str:
        """Backward-compatible alias for the path-sensitive bundle hash."""

        return self.bundle_sha256


def stage_batch(
    spec: IngestBatchSpec,
    *,
    raw_root: Path = Path("data/raw/local"),
) -> tuple[StagedAsset, ...]:
    return tuple(stage_asset(asset, raw_root=raw_root) for asset in spec.assets)


def stage_asset(
    spec: IngestAssetSpec,
    *,
    raw_root: Path = Path("data/raw/local"),
) -> StagedAsset:
    try:
        validate_asset_id(spec.asset_id)
    except IngestSpecError as exc:
        raise StagingError(f"invalid staged asset_id: {exc}") from exc
    source_root, source_files = _source_bundle(spec)
    expected_bundle_hash, expected_content_hash = _bundle_hashes(source_root, source_files)
    expected_source_structure, expected_source_structure_hash = _source_structure(spec.path)

    root = raw_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / spec.asset_id).resolve()
    if destination.parent != root:
        raise StagingError(f"staging destination escapes raw_root: {destination}")
    main_relative = Path(spec.path.expanduser().resolve().name)

    if destination.exists() or destination.is_symlink():
        return _existing_result(
            spec=spec,
            destination=destination,
            main_relative=main_relative,
            expected_files=source_files,
            expected_bundle_hash=expected_bundle_hash,
            expected_content_hash=expected_content_hash,
            expected_source_structure=expected_source_structure,
            expected_source_structure_hash=expected_source_structure_hash,
        )

    temporary = Path(tempfile.mkdtemp(prefix=f".{spec.asset_id}.tmp-", dir=root))
    try:
        for relative_path in source_files:
            source = source_root / relative_path
            target = temporary / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)

        copied_bundle_hash, copied_content_hash = _bundle_hashes(temporary, source_files)
        if (
            copied_bundle_hash != expected_bundle_hash
            or copied_content_hash != expected_content_hash
        ):
            raise StagingError(
                f"asset {spec.asset_id!r} changed while it was being copied; staging aborted"
            )
        copied_source_structure, copied_source_structure_hash = _source_structure(
            temporary / main_relative
        )
        if (
            copied_source_structure != expected_source_structure
            or copied_source_structure_hash != expected_source_structure_hash
        ):
            raise StagingError(
                f"asset {spec.asset_id!r} source structure changed while it was being copied; "
                "staging aborted"
            )
        try:
            temporary.rename(destination)
        except OSError as exc:
            if destination.exists() or destination.is_symlink():
                result = _existing_result(
                    spec=spec,
                    destination=destination,
                    main_relative=main_relative,
                    expected_files=source_files,
                    expected_bundle_hash=expected_bundle_hash,
                    expected_content_hash=expected_content_hash,
                    expected_source_structure=expected_source_structure,
                    expected_source_structure_hash=expected_source_structure_hash,
                )
                return result
            raise StagingError(
                f"cannot atomically publish staged asset {spec.asset_id!r}: {exc}"
            ) from exc
    except StagingError:
        raise
    except OSError as exc:
        raise StagingError(f"cannot stage asset {spec.asset_id!r}: {exc}") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    return _result(
        spec=spec,
        destination=destination,
        main_relative=main_relative,
        relative_files=source_files,
        bundle_hash=expected_bundle_hash,
        content_hash=expected_content_hash,
        source_structure=expected_source_structure,
        source_structure_hash=expected_source_structure_hash,
        changed=True,
    )


def bundle_sha256(root: Path, relative_files: tuple[Path, ...]) -> str:
    """Hash a bundle's paths and contents using a stable, ambiguity-free encoding."""

    return _bundle_hashes(root, relative_files)[0]


def content_sha256(root: Path, relative_files: tuple[Path, ...]) -> str:
    """Hash the multiset of file contents independently of file paths."""

    return _bundle_hashes(root, relative_files)[1]


def _bundle_hashes(root: Path, relative_files: tuple[Path, ...]) -> tuple[str, str]:
    bundle_digest = hashlib.sha256(_BUNDLE_HASH_DOMAIN)
    normalized = tuple(sorted({_normalize_relative_path(path) for path in relative_files}))
    if not normalized:
        raise StagingError("asset bundle contains no files")
    content_entries: list[bytes] = []
    for relative_path in normalized:
        full_path = root / relative_path
        if full_path.is_symlink() or not full_path.is_file():
            raise StagingError(f"bundle member is not a regular file: {full_path}")
        path_bytes = relative_path.as_posix().encode("utf-8")
        bundle_digest.update(len(path_bytes).to_bytes(8, "big"))
        bundle_digest.update(path_bytes)
        size = full_path.stat().st_size
        size_bytes = size.to_bytes(8, "big")
        bundle_digest.update(size_bytes)
        file_digest = hashlib.sha256(_CONTENT_FILE_HASH_DOMAIN)
        file_digest.update(size_bytes)
        with full_path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                bundle_digest.update(chunk)
                file_digest.update(chunk)
        content_entries.append(size_bytes + file_digest.digest())

    content_digest = hashlib.sha256(_CONTENT_HASH_DOMAIN)
    content_digest.update(len(content_entries).to_bytes(8, "big"))
    for entry in sorted(content_entries):
        content_digest.update(entry)
    return bundle_digest.hexdigest(), content_digest.hexdigest()


def gltf_dependency_paths(gltf_path: Path) -> tuple[Path, ...]:
    """Return validated local paths referenced by a JSON glTF document."""

    unresolved = gltf_path.expanduser()
    _reject_symlink_traversal(unresolved)
    path = unresolved.resolve()
    if path.suffix.lower() != ".gltf":
        raise StagingError(f"expected .gltf file, got {path}")
    try:
        raw = read_gltf_document(path)
    except SourceStructureError as exc:
        raise StagingError(str(exc)) from exc

    dependencies: dict[str, Path] = {}
    for collection_name in ("buffers", "images"):
        collection = raw.get(collection_name, [])
        if not isinstance(collection, list):
            raise StagingError(f"invalid glTF file {path}: {collection_name} must be an array")
        for index, item in enumerate(collection):
            if not isinstance(item, dict):
                raise StagingError(
                    f"invalid glTF file {path}: {collection_name}[{index}] must be an object"
                )
            uri = item.get("uri")
            if uri is None:
                if collection_name == "buffers":
                    raise StagingError(
                        f"invalid glTF file {path}: buffers[{index}].uri is required "
                        "for a .gltf document"
                    )
                continue
            if not isinstance(uri, str) or not uri:
                raise StagingError(
                    f"invalid glTF file {path}: {collection_name}[{index}].uri must be a "
                    "non-empty string"
                )
            relative_path = _local_gltf_uri(
                uri,
                context=f"{path}:{collection_name}[{index}].uri",
            )
            if relative_path == Path(path.name):
                raise StagingError(f"glTF dependency URI aliases the main file: {uri!r}")
            unresolved = path.parent / relative_path
            if unresolved.is_symlink():
                raise StagingError(f"glTF dependency may not be a symbolic link: {uri!r}")
            try:
                candidate = unresolved.resolve()
            except (OSError, RuntimeError) as exc:
                raise StagingError(f"cannot resolve glTF dependency {uri!r}: {exc}") from exc
            try:
                safe_relative = candidate.relative_to(path.parent)
            except ValueError as exc:
                raise StagingError(
                    f"glTF dependency escapes its bundle directory: {uri!r}"
                ) from exc
            if candidate != unresolved.absolute():
                raise StagingError(f"glTF dependency may not traverse a symbolic link: {uri!r}")
            if not candidate.is_file():
                raise StagingError(f"missing glTF dependency: {candidate}")
            dependencies[safe_relative.as_posix()] = safe_relative
    return tuple(dependencies[key] for key in sorted(dependencies))


def _source_bundle(spec: IngestAssetSpec) -> tuple[Path, tuple[Path, ...]]:
    unresolved = spec.path.expanduser()
    _reject_symlink_traversal(unresolved)
    path = unresolved.resolve()
    if not path.is_file():
        raise StagingError(f"asset source is not a regular file: {path}")
    source_root = path.parent
    declared_dependencies = _declared_dependency_paths(
        source_root,
        spec.dependencies,
        main_path=path,
    )
    files = [Path(path.name), *declared_dependencies]
    if path.suffix.lower() == ".gltf":
        referenced_dependencies = gltf_dependency_paths(path)
        declared_names = {item.as_posix() for item in declared_dependencies}
        referenced_names = {item.as_posix() for item in referenced_dependencies}
        if declared_names != referenced_names:
            missing = sorted(referenced_names - declared_names)
            extra = sorted(declared_names - referenced_names)
            details: list[str] = []
            if missing:
                details.append(f"missing from manifest: {', '.join(missing)}")
            if extra:
                details.append(f"not referenced by glTF: {', '.join(extra)}")
            raise StagingError(
                "manifest dependencies do not match glTF external references ("
                + "; ".join(details)
                + ")"
            )
        files.extend(referenced_dependencies)
    return source_root, tuple(sorted(set(files), key=lambda item: item.as_posix()))


def _declared_dependency_paths(
    source_root: Path,
    dependencies: tuple[Path, ...],
    *,
    main_path: Path,
) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for dependency in dependencies:
        relative_path = _normalize_relative_path(dependency)
        name = relative_path.as_posix()
        if name in seen:
            raise StagingError(f"duplicate declared dependency: {name!r}")
        seen.add(name)
        unresolved = source_root / relative_path
        if unresolved.is_symlink():
            raise StagingError(f"declared dependency may not be a symbolic link: {name!r}")
        try:
            candidate = unresolved.resolve()
        except (OSError, RuntimeError) as exc:
            raise StagingError(f"cannot resolve declared dependency {name!r}: {exc}") from exc
        try:
            candidate.relative_to(source_root)
        except ValueError as exc:
            raise StagingError(f"declared dependency escapes bundle: {name!r}") from exc
        if candidate != unresolved.absolute():
            raise StagingError(f"declared dependency may not be a symbolic link: {name!r}")
        if candidate == main_path:
            raise StagingError(f"main asset file is not a dependency: {name!r}")
        if not candidate.is_file():
            raise StagingError(f"missing declared dependency: {candidate}")
        result.append(relative_path)
    return tuple(result)


def _local_gltf_uri(uri: str, *, context: str) -> Path:
    if any(character.isspace() or ord(character) == 127 for character in uri):
        raise StagingError(f"{context}: whitespace and control characters are not allowed")
    if re.search(r"%(?![0-9A-Fa-f]{2})", uri):
        raise StagingError(f"{context}: invalid percent encoding")
    parsed = urlsplit(uri)
    if parsed.scheme or parsed.netloc:
        kind = "data URI" if parsed.scheme.lower() == "data" else "remote/absolute URI"
        raise StagingError(f"{context}: {kind} is not allowed")
    if parsed.query or parsed.fragment:
        raise StagingError(f"{context}: query strings and fragments are not allowed")
    try:
        decoded = unquote(parsed.path, errors="strict")
    except UnicodeDecodeError as exc:
        raise StagingError(f"{context}: invalid percent-encoded UTF-8") from exc
    if not decoded or "\\" in decoded or "\x00" in decoded:
        raise StagingError(f"{context}: invalid local URI path")
    if re.match(r"[A-Za-z]:", decoded):
        raise StagingError(f"{context}: absolute drive path is not allowed")
    pure_path = PurePosixPath(decoded)
    if pure_path.is_absolute() or any(part in {"", ".", ".."} for part in pure_path.parts):
        raise StagingError(f"{context}: path must stay within the glTF bundle")
    return Path(*pure_path.parts)


def _existing_result(
    *,
    spec: IngestAssetSpec,
    destination: Path,
    main_relative: Path,
    expected_files: tuple[Path, ...],
    expected_bundle_hash: str,
    expected_content_hash: str,
    expected_source_structure: dict[str, object],
    expected_source_structure_hash: str,
) -> StagedAsset:
    if destination.is_symlink() or not destination.is_dir():
        raise StagingError(f"staging destination is not a regular directory: {destination}")
    actual_files = _tree_files(destination)
    actual_bundle_hash, actual_content_hash = _bundle_hashes(destination, actual_files)
    actual_source_structure, actual_source_structure_hash = _source_structure(
        destination / main_relative
    )
    expected_names = tuple(path.as_posix() for path in expected_files)
    actual_names = tuple(path.as_posix() for path in actual_files)
    if (
        actual_names != expected_names
        or actual_bundle_hash != expected_bundle_hash
        or actual_content_hash != expected_content_hash
        or actual_source_structure != expected_source_structure
        or actual_source_structure_hash != expected_source_structure_hash
    ):
        raise StagingError(
            f"asset_id conflict for {spec.asset_id!r}: staged content differs from source"
        )
    return _result(
        spec=spec,
        destination=destination,
        main_relative=main_relative,
        relative_files=actual_files,
        bundle_hash=actual_bundle_hash,
        content_hash=actual_content_hash,
        source_structure=actual_source_structure,
        source_structure_hash=actual_source_structure_hash,
        changed=False,
    )


def _tree_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    try:
        entries = sorted(root.rglob("*"), key=lambda path: path.as_posix())
    except OSError as exc:
        raise StagingError(f"cannot inspect staged asset {root}: {exc}") from exc
    for entry in entries:
        if entry.is_symlink():
            raise StagingError(f"staged bundle contains a symbolic link: {entry}")
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise StagingError(f"staged bundle contains a non-regular file: {entry}")
        files.append(entry.relative_to(root))
    return tuple(files)


def _normalize_relative_path(path: Path) -> Path:
    raw = path.as_posix()
    if (
        path.is_absolute()
        or "\\" in raw
        or "//" in raw
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and path.parts[0].endswith(":"))
    ):
        raise StagingError(f"bundle path must be a normalized relative path: {path}")
    return path


def _reject_symlink_traversal(value: Path) -> None:
    candidate = value if value.is_absolute() else Path.cwd() / value
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        if part == ".":
            continue
        if part == "..":
            current = current.parent
            continue
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:  # pragma: no cover - platform/filesystem-specific failure
            raise StagingError(
                f"cannot inspect asset source path component {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(mode):
            raise StagingError(f"asset source may not traverse a symbolic link: {current}")


def _result(
    *,
    spec: IngestAssetSpec,
    destination: Path,
    main_relative: Path,
    relative_files: tuple[Path, ...],
    bundle_hash: str,
    content_hash: str,
    source_structure: dict[str, object],
    source_structure_hash: str,
    changed: bool,
) -> StagedAsset:
    return StagedAsset(
        asset_id=spec.asset_id,
        raw_dir=destination,
        raw_path=destination / main_relative,
        files=tuple(destination / path for path in relative_files),
        bundle_sha256=bundle_hash,
        content_sha256=content_hash,
        source_structure=source_structure,
        source_structure_sha256=source_structure_hash,
        changed=changed,
    )


def _source_structure(path: Path) -> tuple[dict[str, object], str]:
    try:
        evidence = inspect_source_structure(path)
    except SourceStructureError as exc:
        raise StagingError(str(exc)) from exc
    return evidence.payload, evidence.sha256
