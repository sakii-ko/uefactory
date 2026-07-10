from __future__ import annotations

import math
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml

from uefactory.core.identity import validate_asset_id as validate_core_asset_id

SUPPORTED_ASSET_EXTENSIONS = frozenset({".fbx", ".gltf", ".glb"})

LICENSE_TIERS: dict[str, str] = {
    "0BSD": "open",
    "Apache-2.0": "open",
    "BSD-2-Clause": "open",
    "BSD-3-Clause": "open",
    "CC0-1.0": "open",
    "CC-BY-3.0": "open",
    "CC-BY-4.0": "open",
    "CC-BY-SA-3.0": "open",
    "CC-BY-SA-4.0": "open",
    "CC-BY-NC-3.0": "nc",
    "CC-BY-NC-4.0": "nc",
    "CC-BY-NC-ND-3.0": "nc",
    "CC-BY-NC-ND-4.0": "nc",
    "CC-BY-NC-SA-3.0": "nc",
    "CC-BY-NC-SA-4.0": "nc",
    "GPL-2.0-only": "open",
    "GPL-3.0-only": "open",
    "MIT": "open",
    "Unlicense": "open",
    "LicenseRef-Research-Only": "nc",
    "LicenseRef-UE-Only": "ue-only",
}

_SOURCE_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_SOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_TAG_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_ASSET_KEYS = {
    "asset_id",
    "name",
    "normalization",
    "path",
    "dependencies",
    "source",
    "source_id",
    "source_url",
    "license",
    "license_tier",
    "license_url",
    "attribution",
    "tags",
}
_NORMALIZATION_KEYS = {
    "source_units",
    "source_up_axis",
    "source_handedness",
    "uniform_scale",
    "pivot_policy",
}


class IngestSpecError(ValueError):
    """Raised when an ingest batch manifest violates its strict contract."""


@dataclass(frozen=True)
class IngestNormalizationSpec:
    source_units: str
    source_up_axis: str
    source_handedness: str
    uniform_scale: float
    pivot_policy: str

    def as_dict(self) -> dict[str, str | float]:
        return {
            "source_units": self.source_units,
            "source_up_axis": self.source_up_axis,
            "source_handedness": self.source_handedness,
            "uniform_scale": self.uniform_scale,
            "pivot_policy": self.pivot_policy,
        }


@dataclass(frozen=True)
class IngestAssetSpec:
    asset_id: str
    name: str
    normalization: IngestNormalizationSpec
    path: Path
    dependencies: tuple[Path, ...]
    source: str
    source_id: str
    source_url: str
    license: str
    license_tier: str
    license_url: str
    attribution: str
    tags: tuple[str, ...]

    @property
    def format(self) -> str:
        return self.path.suffix.lower().removeprefix(".")


@dataclass(frozen=True)
class IngestBatchSpec:
    assets: tuple[IngestAssetSpec, ...]
    source_path: Path
    raw: dict[str, Any]


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_ingest_spec(path: Path) -> IngestBatchSpec:
    manifest_path = path.expanduser().resolve()
    try:
        with manifest_path.open("r", encoding="utf-8") as file:
            raw = yaml.load(file, Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError) as exc:
        raise IngestSpecError(f"cannot read ingest manifest {manifest_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise IngestSpecError(f"$: invalid YAML: {exc}") from exc
    return parse_ingest_spec(raw, source_path=manifest_path)


def validate_asset_id(value: Any, *, path: str = "asset_id") -> str:
    """Validate and return the canonical logical asset identifier."""

    try:
        return validate_core_asset_id(value, field=path)
    except ValueError as exc:
        raise IngestSpecError(str(exc)) from exc


def parse_ingest_spec(raw: Any, *, source_path: Path | None = None) -> IngestBatchSpec:
    root = _mapping(raw, "$")
    _require_keys(root, {"assets"}, "$")
    assets_raw = root["assets"]
    if not isinstance(assets_raw, list):
        raise IngestSpecError("$.assets: expected list")
    if not assets_raw:
        raise IngestSpecError("$.assets: expected at least 1 item(s)")

    manifest_path = (source_path or Path("<memory>")).expanduser()
    if source_path is None or manifest_path.name == "<memory>":
        base_dir = Path.cwd()
    else:
        base_dir = manifest_path.resolve().parent

    assets: list[IngestAssetSpec] = []
    seen_ids: set[str] = set()
    for index, raw_asset in enumerate(assets_raw):
        path = f"$.assets[{index}]"
        asset = _parse_asset(raw_asset, path=path, base_dir=base_dir)
        if asset.asset_id in seen_ids:
            raise IngestSpecError(f"{path}.asset_id: duplicate asset_id {asset.asset_id!r}")
        seen_ids.add(asset.asset_id)
        assets.append(asset)

    return IngestBatchSpec(
        assets=tuple(assets),
        source_path=manifest_path.resolve() if source_path is not None else Path("<memory>"),
        raw=root,
    )


def _parse_asset(raw: Any, *, path: str, base_dir: Path) -> IngestAssetSpec:
    value = _mapping(raw, path)
    _require_keys(value, _ASSET_KEYS, path)

    asset_id = validate_asset_id(value["asset_id"], path=f"{path}.asset_id")

    name = _bounded_string(value["name"], f"{path}.name", max_length=256)
    normalization = _normalization(value["normalization"], f"{path}.normalization")
    raw_path = _bounded_string(value["path"], f"{path}.path", max_length=4096)
    asset_path = Path(raw_path).expanduser()
    if not asset_path.is_absolute():
        asset_path = base_dir / asset_path
    _reject_symlink_traversal(asset_path, f"{path}.path")
    try:
        asset_path = asset_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise IngestSpecError(f"{path}.path: cannot resolve path: {exc}") from exc
    if asset_path.suffix.lower() not in SUPPORTED_ASSET_EXTENSIONS:
        formats = ", ".join(sorted(SUPPORTED_ASSET_EXTENSIONS))
        raise IngestSpecError(f"{path}.path: expected one of {formats}")
    dependencies = _dependencies(
        value["dependencies"],
        f"{path}.dependencies",
        asset_path=asset_path,
    )

    source = _bounded_string(value["source"], f"{path}.source", max_length=64)
    if _SOURCE_PATTERN.fullmatch(source) is None:
        raise IngestSpecError(f"{path}.source: expected lowercase slug")

    source_id = _bounded_string(value["source_id"], f"{path}.source_id", max_length=128)
    if _SOURCE_ID_PATTERN.fullmatch(source_id) is None:
        raise IngestSpecError(f"{path}.source_id: expected safe identifier")

    source_url = _web_url(value["source_url"], f"{path}.source_url")
    license_id = _bounded_string(value["license"], f"{path}.license", max_length=64)
    if license_id not in LICENSE_TIERS:
        allowed = ", ".join(sorted(repr(item) for item in LICENSE_TIERS))
        raise IngestSpecError(
            f"{path}.license: expected an approved license ({allowed}), got {license_id!r}"
        )
    license_tier = _bounded_string(value["license_tier"], f"{path}.license_tier", max_length=16)
    expected_tier = LICENSE_TIERS[license_id]
    if license_tier != expected_tier:
        raise IngestSpecError(
            f"{path}.license_tier: license {license_id!r} requires {expected_tier!r}, "
            f"got {license_tier!r}"
        )
    license_url = _web_url(value["license_url"], f"{path}.license_url")
    attribution = _bounded_string(value["attribution"], f"{path}.attribution", max_length=1024)
    tags = _tags(value["tags"], f"{path}.tags")

    return IngestAssetSpec(
        asset_id=asset_id,
        name=name,
        normalization=normalization,
        path=asset_path,
        dependencies=dependencies,
        source=source,
        source_id=source_id,
        source_url=source_url,
        license=license_id,
        license_tier=license_tier,
        license_url=license_url,
        attribution=attribution,
        tags=tags,
    )


def _normalization(value: Any, path: str) -> IngestNormalizationSpec:
    payload = _mapping(value, path)
    _require_keys(payload, _NORMALIZATION_KEYS, path)
    source_units = _enum_string(
        payload["source_units"],
        f"{path}.source_units",
        {"auto"},
    )
    source_up_axis = _enum_string(
        payload["source_up_axis"],
        f"{path}.source_up_axis",
        {"auto"},
    )
    source_handedness = _enum_string(
        payload["source_handedness"],
        f"{path}.source_handedness",
        {"auto"},
    )
    pivot_policy = _enum_string(
        payload["pivot_policy"],
        f"{path}.pivot_policy",
        {"preserve_source"},
    )
    raw_scale = payload["uniform_scale"]
    if (
        isinstance(raw_scale, bool)
        or not isinstance(raw_scale, int | float)
        or not math.isfinite(float(raw_scale))
        or not 0.0001 <= float(raw_scale) <= 10_000.0
    ):
        raise IngestSpecError(f"{path}.uniform_scale: expected finite number in [0.0001, 10000.0]")
    return IngestNormalizationSpec(
        source_units=source_units,
        source_up_axis=source_up_axis,
        source_handedness=source_handedness,
        uniform_scale=float(raw_scale),
        pivot_policy=pivot_policy,
    )


def _enum_string(value: Any, path: str, allowed: set[str]) -> str:
    result = _bounded_string(value, path, max_length=64)
    if result not in allowed:
        choices = ", ".join(sorted(repr(item) for item in allowed))
        raise IngestSpecError(f"{path}: expected one of {choices}")
    return result


def _require_keys(value: dict[str, Any], required: set[str], path: str) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    extra = sorted(keys - required)
    if missing:
        raise IngestSpecError(f"{path}: missing required key {missing[0]!r}")
    if extra:
        raise IngestSpecError(f"{path}: unknown key {extra[0]!r}")


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IngestSpecError(f"{path}: expected mapping")
    if any(not isinstance(key, str) for key in value):
        raise IngestSpecError(f"{path}: all keys must be strings")
    return value


def _bounded_string(value: Any, path: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value:
        raise IngestSpecError(f"{path}: expected non-empty string")
    if value != value.strip():
        raise IngestSpecError(f"{path}: leading or trailing whitespace is not allowed")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise IngestSpecError(f"{path}: control characters are not allowed")
    if len(value) > max_length:
        raise IngestSpecError(f"{path}: expected at most {max_length} characters")
    return value


def _reject_symlink_traversal(value: Path, path: str) -> None:
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
            raise IngestSpecError(
                f"{path}: cannot inspect path component {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(mode):
            raise IngestSpecError(
                f"{path}: symbolic links are not allowed in asset source paths: {current}"
            )


def _web_url(value: Any, path: str) -> str:
    url = _bounded_string(value, path, max_length=2048)
    if any(character.isspace() for character in url):
        raise IngestSpecError(f"{path}: whitespace is not allowed in URL")
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise IngestSpecError(f"{path}: invalid URL: {exc}") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IngestSpecError(f"{path}: expected absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise IngestSpecError(f"{path}: URL credentials are not allowed")
    return url


def _tags(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise IngestSpecError(f"{path}: expected list")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        tag = _bounded_string(item, item_path, max_length=64)
        if _TAG_PATTERN.fullmatch(tag) is None:
            raise IngestSpecError(f"{item_path}: expected lowercase tag slug")
        if tag in seen:
            raise IngestSpecError(f"{item_path}: duplicate tag {tag!r}")
        seen.add(tag)
        result.append(tag)
    return tuple(result)


def _dependencies(value: Any, path: str, *, asset_path: Path) -> tuple[Path, ...]:
    if not isinstance(value, list):
        raise IngestSpecError(f"{path}: expected list")
    result: list[Path] = []
    seen: set[str] = set()
    bundle_root = asset_path.parent
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        raw_dependency = _bounded_string(item, item_path, max_length=4096)
        if "\\" in raw_dependency:
            raise IngestSpecError(f"{item_path}: backslashes are not allowed")
        if "//" in raw_dependency:
            raise IngestSpecError(f"{item_path}: expected normalized relative path")
        parts = raw_dependency.split("/")
        pure_path = PurePosixPath(raw_dependency)
        if (
            pure_path.is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
            or (pure_path.parts and pure_path.parts[0].endswith(":"))
        ):
            raise IngestSpecError(f"{item_path}: expected normalized relative path")
        normalized = pure_path.as_posix()
        if normalized in seen:
            raise IngestSpecError(f"{item_path}: duplicate dependency {normalized!r}")
        seen.add(normalized)

        unresolved = bundle_root / Path(*pure_path.parts)
        if unresolved.is_symlink():
            raise IngestSpecError(f"{item_path}: symbolic links are not allowed")
        try:
            dependency_path = unresolved.resolve()
        except (OSError, RuntimeError) as exc:
            raise IngestSpecError(f"{item_path}: cannot resolve dependency: {exc}") from exc
        if dependency_path != unresolved.absolute():
            raise IngestSpecError(f"{item_path}: symbolic links are not allowed")
        if dependency_path == asset_path:
            raise IngestSpecError(f"{item_path}: main asset file is not a dependency")
        if not dependency_path.is_file():
            raise IngestSpecError(f"{item_path}: dependency file does not exist: {dependency_path}")
        result.append(Path(*pure_path.parts))
    return tuple(result)
