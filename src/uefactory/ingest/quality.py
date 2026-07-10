from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from uefactory.core.ingest_contracts import (
    QUALITY_RULESET_VERSION,
    is_current_passed_quality,
    static_mesh_quality_policy,
)
from uefactory.ingest.source_structure import is_valid_source_structure_evidence

__all__ = [
    "QUALITY_RULESET_VERSION",
    "is_current_passed_quality",
]

_MIN_MAX_EXTENT_CM = 0.1
_MAX_MAX_EXTENT_CM = 100_000.0
_BOUNDS_REL_TOLERANCE = 1e-6
_BOUNDS_ABS_TOLERANCE_CM = 1e-6


class IngestQualityError(RuntimeError):
    """Raised when an imported asset does not pass the active quality ruleset."""

    def __init__(self, report: dict[str, Any]) -> None:
        failed = [
            name
            for name, check in report.get("checks", {}).items()
            if isinstance(check, dict) and check.get("status") == "failed"
        ]
        detail = ", ".join(failed) if failed else "unknown check"
        super().__init__(
            f"ingest quality gate {report.get('ruleset_version', 'unknown')} failed: {detail}"
        )
        self.report = report
        self.manifest_path: Path | None = None


def evaluate_static_mesh_quality(
    manifest: dict[str, Any],
    *,
    require_single_static_mesh: bool = True,
    require_texture_references: bool = False,
) -> dict[str, Any]:
    """Evaluate the versioned M2 StaticMesh host-side quality contract."""

    meshes_value = manifest.get("static_meshes")
    meshes = meshes_value if isinstance(meshes_value, list) else []
    checks: dict[str, dict[str, Any]] = {}

    source_structure = manifest.get("source_structure")
    source_structure_sha256 = manifest.get("source_structure_sha256")
    source_format = manifest.get("source_format")
    source_structure_valid = is_valid_source_structure_evidence(
        source_structure,
        source_structure_sha256,
        expected_source_format=source_format if isinstance(source_format, str) else None,
    )
    checks["source_structure_provenance"] = _check(
        source_structure_valid and isinstance(source_format, str),
        source_format=source_format,
        availability=(
            source_structure.get("status") if isinstance(source_structure, dict) else None
        ),
        canonical_sha256=source_structure_sha256,
        ue_output_policy=(
            source_structure.get("ue_output_policy") if isinstance(source_structure, dict) else None
        ),
        ue_hierarchy_preserved=(
            source_structure.get("ue_hierarchy_preserved")
            if isinstance(source_structure, dict)
            else None
        ),
        required=(
            "canonical current source graph evidence; glTF/GLB available, FBX explicitly "
            "not_available/delegated; UE output flattening must not claim hierarchy preservation"
        ),
    )

    expected_mesh_count = 1 if require_single_static_mesh else "at_least_one"
    mesh_count_passed = len(meshes) == 1 if require_single_static_mesh else len(meshes) >= 1
    checks["single_static_mesh"] = _check(
        mesh_count_passed,
        actual=len(meshes),
        required=expected_mesh_count,
    )

    mesh = meshes[0] if len(meshes) == 1 and isinstance(meshes[0], dict) else None
    for field, check_name in (
        ("lod_count", "positive_lod_count"),
        ("triangle_count", "positive_triangle_count"),
        ("vertex_count", "positive_vertex_count"),
    ):
        value = None if mesh is None else mesh.get(field)
        checks[check_name] = _check(
            _is_positive_int(value),
            actual=value,
            required="> 0",
        )

    bounds = None if mesh is None else mesh.get("bounds_cm")
    vectors = _bounds_vectors(bounds)
    finite = bool(
        vectors is not None
        and all(math.isfinite(component) for vector in vectors.values() for component in vector)
    )
    checks["bounds_finite"] = _check(
        finite,
        required="all min/max/size components finite",
    )

    ordered = bool(
        vectors is not None
        and finite
        and all(low <= high for low, high in zip(vectors["min"], vectors["max"], strict=True))
    )
    checks["bounds_ordered"] = _check(
        ordered,
        required="min <= max on every axis",
    )

    expected_size = (
        tuple(high - low for low, high in zip(vectors["min"], vectors["max"], strict=True))
        if ordered and vectors is not None
        else None
    )
    size_matches = bool(
        vectors is not None
        and expected_size is not None
        and all(
            math.isclose(
                actual,
                expected,
                rel_tol=_BOUNDS_REL_TOLERANCE,
                abs_tol=_BOUNDS_ABS_TOLERANCE_CM,
            )
            for actual, expected in zip(vectors["size"], expected_size, strict=True)
        )
    )
    checks["bounds_size_matches"] = _check(
        size_matches,
        actual=None if vectors is None else list(vectors["size"]),
        expected=None if expected_size is None else list(expected_size),
    )

    non_degenerate_axes = (
        0 if expected_size is None else sum(component > 0.0 for component in expected_size)
    )
    checks["bounds_non_degenerate"] = _check(
        non_degenerate_axes >= 2,
        actual_axes=non_degenerate_axes,
        required_axes=2,
    )

    source_max_extent = (
        None
        if expected_size is None or not all(math.isfinite(value) for value in expected_size)
        else max(expected_size)
    )
    uniform_scale = _requested_uniform_scale(manifest)
    max_extent = (
        None
        if source_max_extent is None or uniform_scale is None
        else source_max_extent * uniform_scale
    )
    checks["bounds_max_extent_cm"] = _check(
        max_extent is not None and _MIN_MAX_EXTENT_CM <= max_extent <= _MAX_MAX_EXTENT_CM,
        actual=max_extent,
        source_max_extent_cm=source_max_extent,
        requested_uniform_scale=uniform_scale,
        minimum=_MIN_MAX_EXTENT_CM,
        maximum=_MAX_MAX_EXTENT_CM,
    )

    material_count = None if mesh is None else mesh.get("material_count")
    material_slots = None if mesh is None else mesh.get("material_slots")
    slot_payloads = material_slots if isinstance(material_slots, list) else []
    material_references = _material_references(slot_payloads)
    material_slots_valid = (
        _is_positive_int(material_count)
        and len(slot_payloads) == material_count
        and len(material_references) == material_count
        and _valid_material_slot_indices(slot_payloads)
    )
    checks["material_references"] = _check(
        material_slots_valid,
        material_slot_count=material_count,
        material_slot_payload_count=len(slot_payloads),
        references=material_references,
        required="one non-empty material_path for every indexed material slot",
    )

    texture_references = _texture_references(slot_payloads)
    texture_count = manifest.get("texture_count")
    texture_passed = not require_texture_references or (
        _is_positive_int(texture_count) and bool(texture_references)
    )
    checks["texture_references"] = _check(
        texture_passed,
        required_for_asset=require_texture_references,
        declared_texture_count=texture_count,
        references=texture_references,
        required=(
            "declared positive texture_count and non-empty material-slot texture_paths"
            if require_texture_references
            else "not required"
        ),
    )

    status = (
        "passed" if all(check.get("status") == "passed" for check in checks.values()) else "failed"
    )
    return {
        "ruleset_version": QUALITY_RULESET_VERSION,
        "policy": static_mesh_quality_policy(
            require_single_static_mesh=require_single_static_mesh,
            require_texture_references=require_texture_references,
        ),
        "status": status,
        "checks": checks,
    }


def require_static_mesh_quality(
    manifest: dict[str, Any],
    *,
    require_single_static_mesh: bool = True,
    require_texture_references: bool = False,
) -> dict[str, Any]:
    report = evaluate_static_mesh_quality(
        manifest,
        require_single_static_mesh=require_single_static_mesh,
        require_texture_references=require_texture_references,
    )
    if report["status"] != "passed":
        raise IngestQualityError(report)
    return report


def _bounds_vectors(value: Any) -> dict[str, tuple[float, float, float]] | None:
    if not isinstance(value, dict) or set(value) != {"min", "max", "size"}:
        return None
    vectors: dict[str, tuple[float, float, float]] = {}
    for key in ("min", "max", "size"):
        vector = value[key]
        if not isinstance(vector, list) or len(vector) != 3:
            return None
        if any(
            isinstance(component, bool) or not isinstance(component, int | float)
            for component in vector
        ):
            return None
        vectors[key] = (float(vector[0]), float(vector[1]), float(vector[2]))
    return vectors


def _material_references(slots: list[Any]) -> list[str]:
    references: list[str] = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        material_path = slot.get("material_path")
        if isinstance(material_path, str) and material_path.strip():
            references.append(material_path)
    return references


def _texture_references(slots: list[Any]) -> list[str]:
    references: set[str] = set()
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        texture_paths = slot.get("texture_paths")
        if not isinstance(texture_paths, list):
            continue
        references.update(path for path in texture_paths if isinstance(path, str) and path.strip())
    return sorted(references)


def _valid_material_slot_indices(slots: list[Any]) -> bool:
    indices = [slot.get("index") for slot in slots if isinstance(slot, dict)]
    return (
        len(indices) == len(slots)
        and all(isinstance(index, int) and not isinstance(index, bool) for index in indices)
        and indices == list(range(len(slots)))
    )


def _requested_uniform_scale(manifest: dict[str, Any]) -> float | None:
    normalization = manifest.get("requested_normalization")
    if normalization is None:
        return 1.0
    if not isinstance(normalization, dict):
        return None
    value = normalization.get("uniform_scale")
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or value <= 0
    ):
        return None
    return float(value)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _check(passed: bool, **evidence: Any) -> dict[str, Any]:
    return {"status": "passed" if passed else "failed", **evidence}
