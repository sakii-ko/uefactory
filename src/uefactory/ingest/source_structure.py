from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

SOURCE_STRUCTURE_SCHEMA_VERSION = 1
GLTF_SOURCE_STRUCTURE_POLICY = "gltf_2_source_graph_v1"
FBX_SOURCE_STRUCTURE_POLICY = "fbx_not_available_delegated_to_unreal_importer_v1"
UE_OUTPUT_STRUCTURE_POLICY = "flatten_to_single_static_mesh_v1"

_SOURCE_STRUCTURE_HASH_DOMAIN = b"UEFactory source structure SHA-256 v1\0"
_GLB_HEADER = struct.Struct("<4sII")
_GLB_CHUNK_HEADER = struct.Struct("<I4s")
_GLB_MAGIC = b"glTF"
_GLB_JSON_CHUNK = b"JSON"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_MATRIX = (
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)
_IDENTITY_TRANSLATION = (0.0, 0.0, 0.0)
_IDENTITY_ROTATION = (0.0, 0.0, 0.0, 1.0)
_IDENTITY_SCALE = (1.0, 1.0, 1.0)
_QUATERNION_UNIT_TOLERANCE = 1e-6
_MATRIX_AFFINE_TOLERANCE = 1e-6
_MATRIX_ORTHOGONAL_TOLERANCE = 1e-6


class SourceStructureError(RuntimeError):
    """Raised when source graph provenance cannot be derived without guessing."""


@dataclass(frozen=True)
class SourceStructureEvidence:
    payload: dict[str, Any]
    sha256: str


def inspect_source_structure(path: Path) -> SourceStructureEvidence:
    """Inspect source graph evidence before UE import.

    glTF and GLB expose a normative JSON scene graph, so malformed or ambiguous
    graph/transform data fails closed. M2 v1 deliberately does not implement an
    independent FBX parser: FBX evidence therefore states that source structure is
    unavailable and delegated instead of inventing node counts or transforms.
    """

    source = path.expanduser().resolve()
    if not source.is_file():
        raise SourceStructureError(f"source structure path is not a regular file: {source}")
    source_format = source.suffix.lower().removeprefix(".")
    if source_format == "fbx":
        payload = _fbx_payload()
    elif source_format in {"gltf", "glb"}:
        document = read_gltf_document(source)
        payload = _gltf_payload(document, source_format=source_format, path=source)
    else:
        raise SourceStructureError(
            f"unsupported source format for structure inspection: {source.suffix}"
        )
    return SourceStructureEvidence(
        payload=payload,
        sha256=source_structure_sha256(payload),
    )


def read_gltf_document(path: Path) -> dict[str, Any]:
    """Read and minimally validate the normative JSON document from glTF or GLB."""

    source = path.expanduser().resolve()
    suffix = source.suffix.lower()
    try:
        if suffix == ".gltf":
            raw: Any = json.loads(source.read_text(encoding="utf-8"))
        elif suffix == ".glb":
            raw = _read_glb_json(source)
        else:
            raise SourceStructureError(f"expected .gltf or .glb file, got {source}")
    except SourceStructureError:
        raise
    except OSError as exc:
        raise SourceStructureError(f"cannot read glTF source {source}: {exc}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceStructureError(f"invalid glTF JSON in {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SourceStructureError(f"invalid glTF source {source}: root must be an object")
    asset = raw.get("asset")
    if not isinstance(asset, dict) or asset.get("version") != "2.0":
        raise SourceStructureError(f"invalid glTF source {source}: asset.version must be '2.0'")
    return raw


def source_structure_sha256(payload: dict[str, Any]) -> str:
    """Return the domain-separated digest of canonical source-structure JSON."""

    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SourceStructureError(f"source_structure is not canonical JSON data: {exc}") from exc
    digest = hashlib.sha256(_SOURCE_STRUCTURE_HASH_DOMAIN)
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    return digest.hexdigest()


def is_valid_source_structure_evidence(
    payload: Any,
    sha256: Any,
    *,
    expected_source_format: str | None = None,
) -> bool:
    """Validate current source-structure shape, semantics, and canonical digest."""

    if (
        not isinstance(payload, dict)
        or not isinstance(sha256, str)
        or _SHA256_PATTERN.fullmatch(sha256) is None
    ):
        return False
    try:
        if source_structure_sha256(payload) != sha256:
            return False
        if (
            not _is_canonical_nonnegative_int(payload.get("schema_version"))
            or payload.get("schema_version") != SOURCE_STRUCTURE_SCHEMA_VERSION
        ):
            return False
        source_format = payload.get("source_format")
        if expected_source_format is not None and source_format != expected_source_format:
            return False
        if source_format == "fbx":
            return payload == _fbx_payload()
        if source_format not in {"gltf", "glb"}:
            return False
        return _valid_gltf_payload(payload)
    except (SourceStructureError, TypeError, ValueError):
        return False


def _read_glb_json(path: Path) -> Any:
    size = path.stat().st_size
    with path.open("rb") as source:
        header = source.read(_GLB_HEADER.size)
        if len(header) != _GLB_HEADER.size:
            raise SourceStructureError(f"invalid GLB {path}: truncated header")
        magic, version, declared_length = _GLB_HEADER.unpack(header)
        if magic != _GLB_MAGIC:
            raise SourceStructureError(f"invalid GLB {path}: magic must be b'glTF'")
        if version != 2:
            raise SourceStructureError(f"invalid GLB {path}: version must be 2")
        if declared_length != size:
            raise SourceStructureError(
                f"invalid GLB {path}: declared length {declared_length} != file size {size}"
            )

        offset = _GLB_HEADER.size
        json_bytes: bytes | None = None
        chunk_index = 0
        while offset < declared_length:
            chunk_header = source.read(_GLB_CHUNK_HEADER.size)
            if len(chunk_header) != _GLB_CHUNK_HEADER.size:
                raise SourceStructureError(f"invalid GLB {path}: truncated chunk header")
            chunk_length, chunk_type = _GLB_CHUNK_HEADER.unpack(chunk_header)
            offset += _GLB_CHUNK_HEADER.size
            if chunk_length % 4 != 0:
                raise SourceStructureError(
                    f"invalid GLB {path}: chunk {chunk_index} length is not 4-byte aligned"
                )
            chunk_end = offset + chunk_length
            if chunk_end > declared_length:
                raise SourceStructureError(
                    f"invalid GLB {path}: chunk {chunk_index} exceeds declared length"
                )
            if chunk_index == 0 and chunk_type != _GLB_JSON_CHUNK:
                raise SourceStructureError(f"invalid GLB {path}: first chunk must be JSON")
            if chunk_type == _GLB_JSON_CHUNK:
                if json_bytes is not None:
                    raise SourceStructureError(f"invalid GLB {path}: duplicate JSON chunk")
                json_bytes = source.read(chunk_length)
                if len(json_bytes) != chunk_length:
                    raise SourceStructureError(f"invalid GLB {path}: truncated JSON chunk")
            else:
                source.seek(chunk_length, 1)
            offset = chunk_end
            chunk_index += 1
        if offset != declared_length:
            raise SourceStructureError(f"invalid GLB {path}: chunk layout is inconsistent")
        if json_bytes is None:
            raise SourceStructureError(f"invalid GLB {path}: missing JSON chunk")
    try:
        return json.loads(json_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceStructureError(f"invalid GLB JSON in {path}: {exc}") from exc


def _fbx_payload() -> dict[str, Any]:
    return {
        "schema_version": SOURCE_STRUCTURE_SCHEMA_VERSION,
        "status": "not_available",
        "source_format": "fbx",
        "inspection_policy": FBX_SOURCE_STRUCTURE_POLICY,
        "reason": (
            "M2 v1 has no independent FBX scene-graph parser; source hierarchy and local "
            "transforms are delegated to the Unreal importer and are not claimed as observed"
        ),
        "ue_output_policy": UE_OUTPUT_STRUCTURE_POLICY,
        "ue_hierarchy_preserved": False,
    }


def _gltf_payload(
    document: dict[str, Any],
    *,
    source_format: str,
    path: Path,
) -> dict[str, Any]:
    raw_nodes = _object_array(document.get("nodes", []), path=path, field="nodes")
    raw_meshes = _object_array(document.get("meshes", []), path=path, field="meshes")
    raw_scenes = _object_array(document.get("scenes", []), path=path, field="scenes")

    mesh_count = len(raw_meshes)
    nodes: list[dict[str, Any]] = []
    for index, raw_node in enumerate(raw_nodes):
        children = _index_array(
            raw_node.get("children", []),
            upper_bound=len(raw_nodes),
            path=f"{path}:nodes[{index}].children",
        )
        if index in children:
            raise SourceStructureError(f"invalid glTF graph {path}: node {index} is its own child")
        mesh: int | None = None
        if "mesh" in raw_node:
            mesh = _canonical_index(raw_node["mesh"], upper_bound=mesh_count)
            if mesh is None:
                raise SourceStructureError(
                    f"invalid glTF graph {path}: nodes[{index}].mesh is out of range"
                )
        nodes.append(
            {
                "index": index,
                "children": children,
                "mesh": mesh,
                "local_transform": _local_transform(
                    raw_node,
                    path=f"{path}:nodes[{index}]",
                ),
            }
        )

    parent_counts, root_indices, max_depth = _graph_metrics(nodes, path=path)
    if any(count > 1 for count in parent_counts):
        child = parent_counts.index(next(count for count in parent_counts if count > 1))
        raise SourceStructureError(
            f"invalid glTF graph {path}: node {child} has more than one parent"
        )

    scenes: list[dict[str, Any]] = []
    for index, raw_scene in enumerate(raw_scenes):
        roots = _index_array(
            raw_scene.get("nodes", []),
            upper_bound=len(nodes),
            path=f"{path}:scenes[{index}].nodes",
        )
        if any(parent_counts[node_index] != 0 for node_index in roots):
            raise SourceStructureError(
                f"invalid glTF graph {path}: scenes[{index}] references a non-root node"
            )
        scenes.append({"index": index, "root_nodes": roots})

    default_scene: int | None = None
    if "scene" in document:
        default_scene = _canonical_index(document["scene"], upper_bound=len(scenes))
        if default_scene is None:
            raise SourceStructureError(f"invalid glTF graph {path}: scene index is out of range")

    child_edge_count = sum(len(node["children"]) for node in nodes)
    return {
        "schema_version": SOURCE_STRUCTURE_SCHEMA_VERSION,
        "status": "available",
        "source_format": source_format,
        "inspection_policy": GLTF_SOURCE_STRUCTURE_POLICY,
        "ue_output_policy": UE_OUTPUT_STRUCTURE_POLICY,
        "ue_hierarchy_preserved": False,
        "node_count": len(nodes),
        "root_count": len(root_indices),
        "child_edge_count": child_edge_count,
        "max_depth": max_depth,
        "mesh_definition_count": mesh_count,
        "mesh_reference_count": sum(node["mesh"] is not None for node in nodes),
        "non_identity_local_transform_count": sum(
            not _is_identity_transform(node["local_transform"]) for node in nodes
        ),
        "default_scene": default_scene,
        "scenes": scenes,
        "nodes": nodes,
    }


def _object_array(value: Any, *, path: Path, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SourceStructureError(f"invalid glTF source {path}: {field} must be an array")
    if any(not isinstance(item, dict) for item in value):
        raise SourceStructureError(
            f"invalid glTF source {path}: every {field} item must be an object"
        )
    return value


def _index_array(value: Any, *, upper_bound: int, path: str) -> list[int]:
    if not isinstance(value, list):
        raise SourceStructureError(f"invalid glTF graph {path}: expected an array")
    result = [_canonical_index(item, upper_bound=upper_bound) for item in value]
    if any(item is None for item in result):
        raise SourceStructureError(f"invalid glTF graph {path}: node index is out of range")
    canonical = [item for item in result if item is not None]
    if len(canonical) != len(set(canonical)):
        raise SourceStructureError(f"invalid glTF graph {path}: duplicate node index")
    return canonical


def _local_transform(node: dict[str, Any], *, path: str) -> dict[str, Any]:
    has_matrix = "matrix" in node
    trs_keys = {key for key in ("translation", "rotation", "scale") if key in node}
    if has_matrix and trs_keys:
        raise SourceStructureError(
            f"invalid glTF transform {path}: matrix and TRS properties are mutually exclusive"
        )
    if has_matrix:
        values = _number_array(node["matrix"], length=16, path=f"{path}.matrix")
        if not _is_trs_decomposable_matrix(values):
            raise SourceStructureError(
                f"invalid glTF transform {path}.matrix: matrix must be decomposable to TRS "
                "without perspective or shear"
            )
        return {
            "representation": "matrix",
            "values": values,
        }
    rotation = _number_array(
        node.get("rotation", list(_IDENTITY_ROTATION)),
        length=4,
        path=f"{path}.rotation",
    )
    if not _is_unit_quaternion(rotation):
        raise SourceStructureError(
            f"invalid glTF transform {path}.rotation: expected a unit quaternion"
        )
    return {
        "representation": "trs",
        "translation": _number_array(
            node.get("translation", list(_IDENTITY_TRANSLATION)),
            length=3,
            path=f"{path}.translation",
        ),
        "rotation": rotation,
        "scale": _number_array(
            node.get("scale", list(_IDENTITY_SCALE)),
            length=3,
            path=f"{path}.scale",
        ),
    }


def _number_array(value: Any, *, length: int, path: str) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise SourceStructureError(
            f"invalid glTF transform {path}: expected {length} finite numbers"
        )
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise SourceStructureError(
                f"invalid glTF transform {path}: expected {length} finite numbers"
            )
        try:
            number = float(item)
        except OverflowError as exc:
            raise SourceStructureError(
                f"invalid glTF transform {path}: expected {length} finite numbers"
            ) from exc
        if not math.isfinite(number):
            raise SourceStructureError(
                f"invalid glTF transform {path}: expected {length} finite numbers"
            )
        result.append(0.0 if number == 0.0 else number)
    return result


def _graph_metrics(
    nodes: list[dict[str, Any]],
    *,
    path: Path,
) -> tuple[list[int], list[int], int]:
    parent_counts = [0] * len(nodes)
    for node in nodes:
        for child in node["children"]:
            parent_counts[child] += 1
    if any(count > 1 for count in parent_counts):
        return parent_counts, [], 0
    roots = [index for index, count in enumerate(parent_counts) if count == 0]
    if nodes and not roots:
        raise SourceStructureError(f"invalid glTF graph {path}: graph contains a cycle")

    states = [0] * len(nodes)

    def visit(index: int) -> int:
        if states[index] == 1:
            raise SourceStructureError(f"invalid glTF graph {path}: graph contains a cycle")
        if states[index] == 2:
            return 0
        states[index] = 1
        child_depth = max((visit(child) for child in nodes[index]["children"]), default=0)
        states[index] = 2
        return 1 + child_depth

    max_depth = max((visit(root) for root in roots), default=0)
    if any(state != 2 for state in states):
        raise SourceStructureError(f"invalid glTF graph {path}: graph contains a cycle")
    return parent_counts, roots, max_depth


def _is_identity_transform(value: dict[str, Any]) -> bool:
    if value["representation"] == "matrix":
        return tuple(value["values"]) == _IDENTITY_MATRIX
    return (
        tuple(value["translation"]) == _IDENTITY_TRANSLATION
        and tuple(value["rotation"]) == _IDENTITY_ROTATION
        and tuple(value["scale"]) == _IDENTITY_SCALE
    )


def _canonical_index(value: Any, *, upper_bound: int) -> int | None:
    """Return the canonical integer for a glTF JSON-Schema integer value."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        result = int(value)
    else:
        result = value
    return result if 0 <= result < upper_bound else None


def _is_canonical_index(value: Any, *, upper_bound: int) -> bool:
    return type(value) is int and 0 <= value < upper_bound


def _is_canonical_nonnegative_int(value: Any) -> TypeGuard[int]:
    return type(value) is int and value >= 0


def _is_unit_quaternion(values: list[float]) -> bool:
    if any(value < -1.0 or value > 1.0 for value in values):
        return False
    length = math.hypot(*values)
    return math.isfinite(length) and math.isclose(
        length,
        1.0,
        rel_tol=0.0,
        abs_tol=_QUATERNION_UNIT_TOLERANCE,
    )


def _is_trs_decomposable_matrix(values: list[float]) -> bool:
    """Validate a column-major affine matrix as T * R * S without shear.

    Orthogonal linear columns are exactly the matrices representable by a
    rotation and independent axis scales. Zero scales remain valid, and an
    orthogonal basis with negative determinant remains valid because glTF
    permits negative scale factors/reflections.
    """

    if not (
        math.isclose(values[3], 0.0, rel_tol=0.0, abs_tol=_MATRIX_AFFINE_TOLERANCE)
        and math.isclose(values[7], 0.0, rel_tol=0.0, abs_tol=_MATRIX_AFFINE_TOLERANCE)
        and math.isclose(values[11], 0.0, rel_tol=0.0, abs_tol=_MATRIX_AFFINE_TOLERANCE)
        and math.isclose(values[15], 1.0, rel_tol=0.0, abs_tol=_MATRIX_AFFINE_TOLERANCE)
    ):
        return False

    columns = (
        (values[0], values[1], values[2]),
        (values[4], values[5], values[6]),
        (values[8], values[9], values[10]),
    )
    unit_columns: list[tuple[float, float, float]] = []
    for column in columns:
        length = math.hypot(*column)
        if not math.isfinite(length):
            return False
        if length != 0.0:
            unit_columns.append(
                (
                    column[0] / length,
                    column[1] / length,
                    column[2] / length,
                )
            )
    for first_index, first in enumerate(unit_columns):
        for second in unit_columns[first_index + 1 :]:
            dot = sum(left * right for left, right in zip(first, second, strict=True))
            if not math.isclose(
                dot,
                0.0,
                rel_tol=0.0,
                abs_tol=_MATRIX_ORTHOGONAL_TOLERANCE,
            ):
                return False
    return True


def _valid_gltf_payload(payload: dict[str, Any]) -> bool:
    expected_keys = {
        "schema_version",
        "status",
        "source_format",
        "inspection_policy",
        "ue_output_policy",
        "ue_hierarchy_preserved",
        "node_count",
        "root_count",
        "child_edge_count",
        "max_depth",
        "mesh_definition_count",
        "mesh_reference_count",
        "non_identity_local_transform_count",
        "default_scene",
        "scenes",
        "nodes",
    }
    if (
        set(payload) != expected_keys
        or not _is_canonical_nonnegative_int(payload.get("schema_version"))
        or payload.get("schema_version") != SOURCE_STRUCTURE_SCHEMA_VERSION
        or payload.get("status") != "available"
        or payload.get("inspection_policy") != GLTF_SOURCE_STRUCTURE_POLICY
        or payload.get("ue_output_policy") != UE_OUTPUT_STRUCTURE_POLICY
        or payload.get("ue_hierarchy_preserved") is not False
    ):
        return False
    nodes = payload.get("nodes")
    scenes = payload.get("scenes")
    if not isinstance(nodes, list) or not isinstance(scenes, list):
        return False
    mesh_count = payload.get("mesh_definition_count")
    if not _is_canonical_nonnegative_int(mesh_count):
        return False
    for index, node in enumerate(nodes):
        if (
            not isinstance(node, dict)
            or set(node) != {"index", "children", "mesh", "local_transform"}
            or not _is_canonical_index(node.get("index"), upper_bound=len(nodes))
            or node.get("index") != index
        ):
            return False
        children = node.get("children")
        mesh = node.get("mesh")
        if (
            not isinstance(children, list)
            or len(children) != len(set(children))
            or any(not _is_canonical_index(child, upper_bound=len(nodes)) for child in children)
            or index in children
            or (mesh is not None and not _is_canonical_index(mesh, upper_bound=mesh_count))
            or not _valid_transform_payload(node.get("local_transform"))
        ):
            return False
    try:
        parent_counts, roots, max_depth = _graph_metrics(nodes, path=Path("<manifest>"))
    except SourceStructureError:
        return False
    if any(count > 1 for count in parent_counts):
        return False
    for index, scene in enumerate(scenes):
        if (
            not isinstance(scene, dict)
            or set(scene) != {"index", "root_nodes"}
            or not _is_canonical_index(scene.get("index"), upper_bound=len(scenes))
            or scene.get("index") != index
        ):
            return False
        scene_roots = scene.get("root_nodes")
        if (
            not isinstance(scene_roots, list)
            or len(scene_roots) != len(set(scene_roots))
            or any(not _is_canonical_index(root, upper_bound=len(nodes)) for root in scene_roots)
            or any(parent_counts[root] != 0 for root in scene_roots)
        ):
            return False
    default_scene = payload.get("default_scene")
    if default_scene is not None and not _is_canonical_index(
        default_scene, upper_bound=len(scenes)
    ):
        return False
    metric_values = {
        "node_count": len(nodes),
        "root_count": len(roots),
        "child_edge_count": sum(len(node["children"]) for node in nodes),
        "max_depth": max_depth,
        "mesh_reference_count": sum(node["mesh"] is not None for node in nodes),
        "non_identity_local_transform_count": sum(
            not _is_identity_transform(node["local_transform"]) for node in nodes
        ),
    }
    return not any(
        not _is_canonical_nonnegative_int(payload.get(key)) or payload.get(key) != value
        for key, value in metric_values.items()
    )


def _valid_transform_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    representation = value.get("representation")
    if representation == "matrix":
        matrix = value.get("values")
        return (
            set(value) == {"representation", "values"}
            and _valid_number_list(matrix, length=16)
            and _is_trs_decomposable_matrix(matrix)
        )
    if representation != "trs" or set(value) != {
        "representation",
        "translation",
        "rotation",
        "scale",
    }:
        return False
    translation = value.get("translation")
    rotation = value.get("rotation")
    scale = value.get("scale")
    return (
        _valid_number_list(translation, length=3)
        and _valid_number_list(rotation, length=4)
        and _is_unit_quaternion(rotation)
        and _valid_number_list(scale, length=3)
    )


def _valid_number_list(value: Any, *, length: int) -> TypeGuard[list[float]]:
    return (
        isinstance(value, list)
        and len(value) == length
        and all(type(item) is float and math.isfinite(float(item)) for item in value)
    )
