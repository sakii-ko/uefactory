from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from uefactory.core.identity import validate_snake_slug

SCENE_SCHEMA_VERSION = 1
SCENE_KIND = "interchange_scene"
SCENE_SOURCE_EXTENSIONS = frozenset({".fbx", ".glb", ".gltf"})
LICENSE_TIERS = frozenset({"open", "nc", "ue-only"})

_KNOWN_LICENSE_TIERS: dict[str, str] = {
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

_SOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_SOURCE_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_SOURCE_ROOT_ENV_PATTERN = re.compile(r"UEF_[A-Z0-9_]+\Z")
_ROOT_KEYS = {
    "schema_version",
    "scene_id",
    "name",
    "kind",
    "source",
    "build",
    "camera",
    "render",
}
_SOURCE_KEYS = {
    "path",
    "source",
    "source_id",
    "source_url",
    "license",
    "license_tier",
    "license_url",
    "attribution",
}
_SOURCE_OPTIONAL_KEYS = {"root_env"}
_EXPECTED_KEYS = {"mesh_count", "material_count", "texture_count", "triangle_count"}
_BUILD_KEYS = {"map_path", "export"}
_CAMERA_KEYS = {"rig", "yaw", "pitch", "distance_multiplier"}
_RENDER_KEYS = {"no_auto_floor"}
_RENDER_OPTIONAL_KEYS = {
    "lighting_intensity_multiplier",
    "maximum_background_contamination_ratio",
    "minimum_object_stencil_coverage",
}


class SceneSpecError(ValueError):
    """Raised when a scene specification violates its strict contract."""


@dataclass(frozen=True)
class SceneSourceSpec:
    path: str
    source: str
    source_id: str
    source_url: str
    license: str
    license_tier: str
    license_url: str
    attribution: str
    root_env: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {
            "path": self.path,
            "source": self.source,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "license": self.license,
            "license_tier": self.license_tier,
            "license_url": self.license_url,
            "attribution": self.attribution,
        }
        if self.root_env is not None:
            result["root_env"] = self.root_env
        return result


@dataclass(frozen=True)
class SceneExpectedSpec:
    mesh_count: int | None = None
    material_count: int | None = None
    texture_count: int | None = None
    triangle_count: int | None = None

    def as_dict(self) -> dict[str, int]:
        result: dict[str, int] = {}
        if self.mesh_count is not None:
            result["mesh_count"] = self.mesh_count
        if self.material_count is not None:
            result["material_count"] = self.material_count
        if self.texture_count is not None:
            result["texture_count"] = self.texture_count
        if self.triangle_count is not None:
            result["triangle_count"] = self.triangle_count
        return result


@dataclass(frozen=True)
class SceneBuildSpec:
    map_path: str
    export: bool

    def as_dict(self) -> dict[str, str | bool]:
        return {"map_path": self.map_path, "export": self.export}


@dataclass(frozen=True)
class SceneCameraSpec:
    rig: str
    yaw: float
    pitch: float
    distance_multiplier: float

    def as_dict(self) -> dict[str, str | float]:
        return {
            "rig": self.rig,
            "yaw": self.yaw,
            "pitch": self.pitch,
            "distance_multiplier": self.distance_multiplier,
        }


@dataclass(frozen=True)
class SceneRenderSpec:
    no_auto_floor: bool
    lighting_intensity_multiplier: float | None = None
    minimum_object_stencil_coverage: float | None = None
    maximum_background_contamination_ratio: float | None = None

    def as_dict(self) -> dict[str, bool | float]:
        result: dict[str, bool | float] = {"no_auto_floor": self.no_auto_floor}
        if self.lighting_intensity_multiplier is not None:
            result["lighting_intensity_multiplier"] = self.lighting_intensity_multiplier
        if self.minimum_object_stencil_coverage is not None:
            result["minimum_object_stencil_coverage"] = self.minimum_object_stencil_coverage
        if self.maximum_background_contamination_ratio is not None:
            result["maximum_background_contamination_ratio"] = (
                self.maximum_background_contamination_ratio
            )
        return result


@dataclass(frozen=True)
class SceneSpec:
    schema_version: int
    scene_id: str
    name: str
    kind: str
    source: SceneSourceSpec
    expected: SceneExpectedSpec | None
    build: SceneBuildSpec
    camera: SceneCameraSpec
    render: SceneRenderSpec
    source_path: Path

    def as_dict(self) -> dict[str, Any]:
        """Return the complete JSON-compatible, canonical semantic payload."""

        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "scene_id": self.scene_id,
            "name": self.name,
            "kind": self.kind,
            "source": self.source.as_dict(),
            "build": self.build.as_dict(),
            "camera": self.camera.as_dict(),
            "render": self.render.as_dict(),
        }
        if self.expected is not None:
            result["expected"] = self.expected.as_dict()
        return result

    @property
    def canonical_payload(self) -> bytes:
        """Return deterministic UTF-8 JSON bytes suitable for hashing or storage."""

        return json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        """Return the SHA-256 digest of :attr:`canonical_payload`."""

        return hashlib.sha256(self.canonical_payload).hexdigest()


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


def load_scene_spec(path: Path) -> SceneSpec:
    """Load a SceneSpec from a YAML or JSON document."""

    manifest_path = path.expanduser().resolve()
    try:
        with manifest_path.open("r", encoding="utf-8") as file:
            raw = yaml.load(file, Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError) as exc:
        raise SceneSpecError(f"cannot read scene spec {manifest_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SceneSpecError(f"$: invalid YAML or JSON: {exc}") from exc
    return parse_scene_spec(raw, source_path=manifest_path)


def parse_scene_spec(raw: Any, *, source_path: Path | None = None) -> SceneSpec:
    root = _mapping(raw, "$")
    _require_keys(root, _ROOT_KEYS, "$", optional={"expected"})

    schema_version = _integer(root["schema_version"], "$.schema_version", minimum=1)
    if schema_version != SCENE_SCHEMA_VERSION:
        raise SceneSpecError(
            f"$.schema_version: expected {SCENE_SCHEMA_VERSION}, got {schema_version!r}"
        )

    scene_id = _scene_id(root["scene_id"], "$.scene_id")
    name = _bounded_string(root["name"], "$.name", max_length=256)
    kind = _enum_string(root["kind"], "$.kind", {SCENE_KIND})
    source = _parse_source(root["source"], "$.source")
    expected = _parse_expected(root["expected"], "$.expected") if "expected" in root else None
    build = _parse_build(root["build"], "$.build", scene_id=scene_id)
    camera = _parse_camera(root["camera"], "$.camera")
    render = _parse_render(root["render"], "$.render")

    if source.license in {"LicenseRef-Research-Only", "LicenseRef-UE-Only"} and build.export:
        raise SceneSpecError(f"$.build.export: license {source.license!r} requires export=false")

    return SceneSpec(
        schema_version=schema_version,
        scene_id=scene_id,
        name=name,
        kind=kind,
        source=source,
        expected=expected,
        build=build,
        camera=camera,
        render=render,
        source_path=source_path.expanduser().resolve() if source_path else Path("<memory>"),
    )


def expected_map_path(scene_id: str) -> str:
    """Return the only allowed persistent map package path for ``scene_id``."""

    canonical_id = _scene_id(scene_id, "scene_id")
    return f"/Game/UEF/Scenes/{canonical_id}/L_{canonical_id}"


def _parse_source(value: Any, path: str) -> SceneSourceSpec:
    payload = _mapping(value, path)
    _require_keys(payload, _SOURCE_KEYS, path, optional=_SOURCE_OPTIONAL_KEYS)

    source_path = _bounded_string(payload["path"], f"{path}.path", max_length=4096)
    if urlsplit(source_path).scheme:
        raise SceneSpecError(f"{path}.path: expected a local filesystem path, not a URI")
    if Path(source_path).suffix.lower() not in SCENE_SOURCE_EXTENSIONS:
        formats = ", ".join(sorted(SCENE_SOURCE_EXTENSIONS))
        raise SceneSpecError(f"{path}.path: expected an Interchange source ({formats})")
    root_env = (
        _bounded_string(payload["root_env"], f"{path}.root_env", max_length=128)
        if "root_env" in payload
        else None
    )
    if root_env is not None:
        if _SOURCE_ROOT_ENV_PATTERN.fullmatch(root_env) is None:
            raise SceneSpecError(
                f"{path}.root_env: expected an uppercase UEF_ environment variable name"
            )
        source_parts = Path(source_path).parts
        if Path(source_path).is_absolute():
            raise SceneSpecError(f"{path}.path: expected a relative path when root_env is set")
        if ".." in source_parts:
            raise SceneSpecError(
                f"{path}.path: parent traversal is not allowed when root_env is set"
            )

    source = _bounded_string(payload["source"], f"{path}.source", max_length=64)
    if _SOURCE_PATTERN.fullmatch(source) is None:
        raise SceneSpecError(f"{path}.source: expected lowercase slug")

    source_id = _bounded_string(payload["source_id"], f"{path}.source_id", max_length=128)
    if _SOURCE_ID_PATTERN.fullmatch(source_id) is None:
        raise SceneSpecError(f"{path}.source_id: expected safe identifier")

    source_url = _provenance_uri(payload["source_url"], f"{path}.source_url")
    license_id = _bounded_string(payload["license"], f"{path}.license", max_length=64)
    if license_id not in _KNOWN_LICENSE_TIERS:
        allowed = ", ".join(sorted(repr(item) for item in _KNOWN_LICENSE_TIERS))
        raise SceneSpecError(
            f"{path}.license: expected an approved license ({allowed}), got {license_id!r}"
        )
    license_tier = _enum_string(payload["license_tier"], f"{path}.license_tier", LICENSE_TIERS)
    expected_tier = _KNOWN_LICENSE_TIERS[license_id]
    if license_tier != expected_tier:
        raise SceneSpecError(
            f"{path}.license_tier: license {license_id!r} requires {expected_tier!r}, "
            f"got {license_tier!r}"
        )
    license_url = _provenance_uri(payload["license_url"], f"{path}.license_url")
    attribution = _bounded_string(payload["attribution"], f"{path}.attribution", max_length=1024)

    return SceneSourceSpec(
        path=source_path,
        source=source,
        source_id=source_id,
        source_url=source_url,
        license=license_id,
        license_tier=license_tier,
        license_url=license_url,
        attribution=attribution,
        root_env=root_env,
    )


def _parse_expected(value: Any, path: str) -> SceneExpectedSpec:
    payload = _mapping(value, path)
    _require_keys(payload, set(), path, optional=_EXPECTED_KEYS)
    if not payload:
        raise SceneSpecError(f"{path}: expected at least one count")
    return SceneExpectedSpec(
        mesh_count=(
            _integer(payload["mesh_count"], f"{path}.mesh_count", minimum=0)
            if "mesh_count" in payload
            else None
        ),
        material_count=(
            _integer(payload["material_count"], f"{path}.material_count", minimum=0)
            if "material_count" in payload
            else None
        ),
        texture_count=(
            _integer(payload["texture_count"], f"{path}.texture_count", minimum=0)
            if "texture_count" in payload
            else None
        ),
        triangle_count=(
            _integer(payload["triangle_count"], f"{path}.triangle_count", minimum=0)
            if "triangle_count" in payload
            else None
        ),
    )


def _parse_build(value: Any, path: str, *, scene_id: str) -> SceneBuildSpec:
    payload = _mapping(value, path)
    _require_keys(payload, _BUILD_KEYS, path)
    map_path = _bounded_string(payload["map_path"], f"{path}.map_path", max_length=256)
    required_map_path = expected_map_path(scene_id)
    if map_path != required_map_path:
        raise SceneSpecError(f"{path}.map_path: expected {required_map_path!r}, got {map_path!r}")
    return SceneBuildSpec(
        map_path=map_path,
        export=_boolean(payload["export"], f"{path}.export"),
    )


def _parse_camera(value: Any, path: str) -> SceneCameraSpec:
    payload = _mapping(value, path)
    _require_keys(payload, _CAMERA_KEYS, path)
    rig = _enum_string(payload["rig"], f"{path}.rig", {"overview_bounds"})
    yaw = _finite_number(payload["yaw"], f"{path}.yaw")
    if not -180.0 <= yaw <= 180.0:
        raise SceneSpecError(f"{path}.yaw: expected value in [-180, 180]")
    pitch = _finite_number(payload["pitch"], f"{path}.pitch")
    if not -89.0 <= pitch < 0.0:
        raise SceneSpecError(f"{path}.pitch: overview_bounds requires value in [-89, 0)")
    distance_multiplier = _finite_number(
        payload["distance_multiplier"], f"{path}.distance_multiplier"
    )
    if distance_multiplier <= 0.0:
        raise SceneSpecError(f"{path}.distance_multiplier: expected positive number")
    return SceneCameraSpec(
        rig=rig,
        yaw=yaw,
        pitch=pitch,
        distance_multiplier=distance_multiplier,
    )


def _parse_render(value: Any, path: str) -> SceneRenderSpec:
    payload = _mapping(value, path)
    _require_keys(payload, _RENDER_KEYS, path, optional=_RENDER_OPTIONAL_KEYS)
    no_auto_floor = _boolean(payload["no_auto_floor"], f"{path}.no_auto_floor")
    if not no_auto_floor:
        raise SceneSpecError(f"{path}.no_auto_floor: expected true for scene renders")
    lighting_multiplier = (
        _finite_number(
            payload["lighting_intensity_multiplier"],
            f"{path}.lighting_intensity_multiplier",
        )
        if "lighting_intensity_multiplier" in payload
        else None
    )
    if lighting_multiplier is not None and not 0.1 <= lighting_multiplier <= 100.0:
        raise SceneSpecError(f"{path}.lighting_intensity_multiplier: expected value in [0.1, 100]")
    minimum_stencil_coverage = (
        _finite_number(
            payload["minimum_object_stencil_coverage"],
            f"{path}.minimum_object_stencil_coverage",
        )
        if "minimum_object_stencil_coverage" in payload
        else None
    )
    if minimum_stencil_coverage is not None and not 0.6 <= minimum_stencil_coverage <= 1.0:
        raise SceneSpecError(f"{path}.minimum_object_stencil_coverage: expected value in [0.6, 1]")
    maximum_contamination_ratio = (
        _finite_number(
            payload["maximum_background_contamination_ratio"],
            f"{path}.maximum_background_contamination_ratio",
        )
        if "maximum_background_contamination_ratio" in payload
        else None
    )
    if maximum_contamination_ratio is not None and not 0.001 <= maximum_contamination_ratio <= 0.01:
        raise SceneSpecError(
            f"{path}.maximum_background_contamination_ratio: expected value in [0.001, 0.01]"
        )
    return SceneRenderSpec(
        no_auto_floor=no_auto_floor,
        lighting_intensity_multiplier=lighting_multiplier,
        minimum_object_stencil_coverage=minimum_stencil_coverage,
        maximum_background_contamination_ratio=maximum_contamination_ratio,
    )


def _scene_id(value: Any, path: str) -> str:
    try:
        return validate_snake_slug(value, field=path, max_length=64)
    except ValueError as exc:
        raise SceneSpecError(str(exc)) from exc


def _require_keys(
    value: dict[str, Any],
    required: set[str],
    path: str,
    *,
    optional: set[str] | None = None,
) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    extra = sorted(keys - required - (optional or set()))
    if missing:
        raise SceneSpecError(f"{path}: missing required key {missing[0]!r}")
    if extra:
        raise SceneSpecError(f"{path}: unknown key {extra[0]!r}")


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SceneSpecError(f"{path}: expected mapping")
    if any(not isinstance(key, str) for key in value):
        raise SceneSpecError(f"{path}: all keys must be strings")
    return value


def _bounded_string(value: Any, path: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value:
        raise SceneSpecError(f"{path}: expected non-empty string")
    if value != value.strip():
        raise SceneSpecError(f"{path}: leading or trailing whitespace is not allowed")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise SceneSpecError(f"{path}: control characters are not allowed")
    if len(value) > max_length:
        raise SceneSpecError(f"{path}: expected at most {max_length} characters")
    return value


def _enum_string(value: Any, path: str, allowed: set[str] | frozenset[str]) -> str:
    result = _bounded_string(value, path, max_length=64)
    if result not in allowed:
        choices = ", ".join(sorted(repr(item) for item in allowed))
        raise SceneSpecError(f"{path}: expected one of {choices}, got {result!r}")
    return result


def _integer(value: Any, path: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SceneSpecError(f"{path}: expected integer")
    if value < minimum:
        raise SceneSpecError(f"{path}: expected integer >= {minimum}")
    return value


def _finite_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SceneSpecError(f"{path}: expected finite number")
    result = float(value)
    if not math.isfinite(result):
        raise SceneSpecError(f"{path}: expected finite number")
    return 0.0 if result == 0.0 else result


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise SceneSpecError(f"{path}: expected boolean")
    return value


def _provenance_uri(value: Any, path: str) -> str:
    url = _bounded_string(value, path, max_length=2048)
    if any(character.isspace() for character in url):
        raise SceneSpecError(f"{path}: whitespace is not allowed in URL")
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise SceneSpecError(f"{path}: invalid URL: {exc}") from exc
    if parsed.scheme == "urn":
        if not parsed.path or ":" not in parsed.path:
            raise SceneSpecError(f"{path}: expected namespaced URN")
    elif parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SceneSpecError(f"{path}: expected absolute http(s) URL or URN")
    if parsed.username is not None or parsed.password is not None:
        raise SceneSpecError(f"{path}: URL credentials are not allowed")
    return url
