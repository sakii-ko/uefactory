from __future__ import annotations

from typing import Any

QUALITY_RULESET_VERSION = "m2_static_mesh_v2"
QUALITY_CHECK_NAMES = frozenset(
    {
        "source_structure_provenance",
        "single_static_mesh",
        "positive_lod_count",
        "positive_triangle_count",
        "positive_vertex_count",
        "bounds_finite",
        "bounds_ordered",
        "bounds_size_matches",
        "bounds_non_degenerate",
        "bounds_max_extent_cm",
        "material_references",
        "texture_references",
    }
)
QUALITY_POLICY_KEYS = frozenset(
    {
        "require_single_static_mesh",
        "require_texture_references",
    }
)
IMPORT_MANIFEST_SCHEMA_VERSION = 2
IMPORT_ARTIFACT_SCHEMA_VERSION = 2
FBX_MATERIAL_POSTPROCESS_POLICY = "fbx_filename_pbr_v2"
FBX_GLASS_OVERRIDE_POLICY = "glass_translucent_v1"
FBX_GLASS_OPACITY = 0.12


def static_mesh_quality_policy(
    *,
    require_single_static_mesh: bool,
    require_texture_references: bool,
) -> dict[str, bool]:
    return {
        "require_single_static_mesh": require_single_static_mesh,
        "require_texture_references": require_texture_references,
    }


def is_current_passed_quality(
    value: Any,
    *,
    require_single_static_mesh: bool | None = None,
    require_texture_references: bool | None = None,
) -> bool:
    if not isinstance(value, dict):
        return False
    checks = value.get("checks")
    policy = value.get("policy")
    if (
        not isinstance(policy, dict)
        or set(policy) != QUALITY_POLICY_KEYS
        or any(type(policy[key]) is not bool for key in QUALITY_POLICY_KEYS)
    ):
        return False
    if (
        require_single_static_mesh is not None
        and policy["require_single_static_mesh"] is not require_single_static_mesh
    ):
        return False
    if (
        require_texture_references is not None
        and policy["require_texture_references"] is not require_texture_references
    ):
        return False
    return (
        value.get("ruleset_version") == QUALITY_RULESET_VERSION
        and value.get("status") == "passed"
        and isinstance(checks, dict)
        and set(checks) == QUALITY_CHECK_NAMES
        and all(
            isinstance(check, dict) and check.get("status") == "passed" for check in checks.values()
        )
    )
