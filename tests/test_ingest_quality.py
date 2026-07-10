from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from uefactory.ingest.quality import (
    QUALITY_RULESET_VERSION,
    IngestQualityError,
    evaluate_static_mesh_quality,
    is_current_passed_quality,
    require_static_mesh_quality,
)
from uefactory.ingest.source_structure import source_structure_sha256


def _source_structure() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "available",
        "source_format": "glb",
        "inspection_policy": "gltf_2_source_graph_v1",
        "ue_output_policy": "flatten_to_single_static_mesh_v1",
        "ue_hierarchy_preserved": False,
        "node_count": 2,
        "root_count": 1,
        "child_edge_count": 1,
        "max_depth": 2,
        "mesh_definition_count": 1,
        "mesh_reference_count": 1,
        "non_identity_local_transform_count": 1,
        "default_scene": 0,
        "scenes": [{"index": 0, "root_nodes": [0]}],
        "nodes": [
            {
                "index": 0,
                "children": [1],
                "mesh": None,
                "local_transform": {
                    "representation": "trs",
                    "translation": [0.0, 0.0, 1.0],
                    "rotation": [0.0, 0.0, 0.0, 1.0],
                    "scale": [1.0, 1.0, 1.0],
                },
            },
            {
                "index": 1,
                "children": [],
                "mesh": 0,
                "local_transform": {
                    "representation": "trs",
                    "translation": [0.0, 0.0, 0.0],
                    "rotation": [0.0, 0.0, 0.0, 1.0],
                    "scale": [1.0, 1.0, 1.0],
                },
            },
        ],
    }


def _valid_manifest() -> dict[str, Any]:
    source_structure = _source_structure()
    return {
        "source_format": "glb",
        "source_structure": source_structure,
        "source_structure_sha256": source_structure_sha256(source_structure),
        "requested_normalization": {"uniform_scale": 1.0},
        "static_meshes": [
            {
                "object_path": "/Game/UEF/Ingested/chair/chair.chair",
                "lod_count": 1,
                "triangle_count": 12,
                "vertex_count": 8,
                "material_count": 1,
                "material_slots": [
                    {
                        "index": 0,
                        "slot_name": "body",
                        "material_path": "/Game/UEF/Ingested/chair/chair_mat.chair_mat",
                        "texture_paths": ["/Game/UEF/Ingested/chair/chair_base.chair_base"],
                    }
                ],
                "bounds_cm": {
                    "min": [-50.0, -25.0, 0.0],
                    "max": [50.0, 25.0, 100.0],
                    "size": [100.0, 50.0, 100.0],
                },
            }
        ],
        "texture_count": 1,
        "imported_objects": [
            {
                "object_path": "/Game/UEF/Ingested/chair/chair.chair",
                "class": "StaticMesh",
            },
            {
                "object_path": "/Game/UEF/Ingested/chair/chair_mat.chair_mat",
                "class": "MaterialInstanceConstant",
            },
            {
                "object_path": "/Game/UEF/Ingested/chair/chair_base.chair_base",
                "class": "Texture2D",
            },
        ],
    }


def test_m2_quality_report_passes_every_current_check() -> None:
    report = require_static_mesh_quality(
        _valid_manifest(),
        require_texture_references=True,
    )

    assert report["ruleset_version"] == QUALITY_RULESET_VERSION
    assert report["status"] == "passed"
    assert report["checks"]
    assert {check["status"] for check in report["checks"].values()} == {"passed"}
    assert is_current_passed_quality(report) is True


@pytest.mark.parametrize(
    ("check_name", "mutate"),
    [
        (
            "source_structure_provenance",
            lambda payload: payload.__setitem__("source_structure_sha256", "0" * 64),
        ),
        (
            "source_structure_provenance",
            lambda payload: payload["source_structure"].__setitem__("ue_hierarchy_preserved", True),
        ),
        (
            "single_static_mesh",
            lambda payload: payload["static_meshes"].append(deepcopy(payload["static_meshes"][0])),
        ),
        (
            "positive_lod_count",
            lambda payload: payload["static_meshes"][0].__setitem__("lod_count", 0),
        ),
        (
            "positive_triangle_count",
            lambda payload: payload["static_meshes"][0].__setitem__("triangle_count", 0),
        ),
        (
            "positive_vertex_count",
            lambda payload: payload["static_meshes"][0].__setitem__("vertex_count", 0),
        ),
        (
            "bounds_finite",
            lambda payload: payload["static_meshes"][0]["bounds_cm"]["max"].__setitem__(
                0, float("inf")
            ),
        ),
        (
            "bounds_ordered",
            lambda payload: payload["static_meshes"][0]["bounds_cm"]["min"].__setitem__(0, 51.0),
        ),
        (
            "bounds_size_matches",
            lambda payload: payload["static_meshes"][0]["bounds_cm"]["size"].__setitem__(0, 99.0),
        ),
        (
            "bounds_non_degenerate",
            lambda payload: payload["static_meshes"][0].__setitem__(
                "bounds_cm",
                {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 5.0], "size": [0.0, 0.0, 5.0]},
            ),
        ),
        (
            "bounds_max_extent_cm",
            lambda payload: payload["static_meshes"][0].__setitem__(
                "bounds_cm",
                {
                    "min": [0.0, 0.0, 0.0],
                    "max": [0.05, 0.05, 0.0],
                    "size": [0.05, 0.05, 0.0],
                },
            ),
        ),
        (
            "bounds_max_extent_cm",
            lambda payload: payload["static_meshes"][0].__setitem__(
                "bounds_cm",
                {
                    "min": [0.0, 0.0, 0.0],
                    "max": [100_001.0, 1.0, 0.0],
                    "size": [100_001.0, 1.0, 0.0],
                },
            ),
        ),
        (
            "material_references",
            lambda payload: payload["static_meshes"][0].__setitem__("material_count", 0),
        ),
        (
            "material_references",
            lambda payload: payload["static_meshes"][0]["material_slots"][0].__setitem__(
                "material_path", ""
            ),
        ),
        (
            "texture_references",
            lambda payload: payload["static_meshes"][0]["material_slots"][0].__setitem__(
                "texture_paths", []
            ),
        ),
    ],
)
def test_each_m2_quality_gate_reports_failure(
    check_name: str,
    mutate: Any,
) -> None:
    manifest = _valid_manifest()
    mutate(manifest)

    report = evaluate_static_mesh_quality(
        manifest,
        require_texture_references=True,
    )

    assert report["status"] == "failed"
    assert report["checks"][check_name]["status"] == "failed"
    with pytest.raises(IngestQualityError) as raised:
        require_static_mesh_quality(manifest, require_texture_references=True)
    assert raised.value.report == report


def test_texture_reference_gate_is_explicitly_optional() -> None:
    manifest = _valid_manifest()
    manifest["texture_count"] = 0
    manifest["static_meshes"][0]["material_slots"][0]["texture_paths"] = []

    report = require_static_mesh_quality(
        manifest,
        require_texture_references=False,
    )

    assert report["checks"]["texture_references"] == {
        "status": "passed",
        "required_for_asset": False,
        "declared_texture_count": 0,
        "references": [],
        "required": "not required",
    }


def test_fbx_delegated_structure_policy_passes_without_invented_graph_metrics() -> None:
    manifest = _valid_manifest()
    source_structure = {
        "schema_version": 1,
        "status": "not_available",
        "source_format": "fbx",
        "inspection_policy": "fbx_not_available_delegated_to_unreal_importer_v1",
        "reason": (
            "M2 v1 has no independent FBX scene-graph parser; source hierarchy and local "
            "transforms are delegated to the Unreal importer and are not claimed as observed"
        ),
        "ue_output_policy": "flatten_to_single_static_mesh_v1",
        "ue_hierarchy_preserved": False,
    }
    manifest["source_format"] = "fbx"
    manifest["source_structure"] = source_structure
    manifest["source_structure_sha256"] = source_structure_sha256(source_structure)

    report = require_static_mesh_quality(manifest)

    provenance = report["checks"]["source_structure_provenance"]
    assert provenance["status"] == "passed"
    assert provenance["availability"] == "not_available"
    assert "node_count" not in source_structure


def test_max_extent_gate_uses_requested_uniform_scale() -> None:
    manifest = _valid_manifest()
    manifest["static_meshes"][0]["bounds_cm"] = {
        "min": [0.0, 0.0, 0.0],
        "max": [60_000.0, 1.0, 0.0],
        "size": [60_000.0, 1.0, 0.0],
    }
    manifest["requested_normalization"]["uniform_scale"] = 2.0

    report = evaluate_static_mesh_quality(manifest)

    extent = report["checks"]["bounds_max_extent_cm"]
    assert extent["status"] == "failed"
    assert extent["source_max_extent_cm"] == 60_000.0
    assert extent["requested_uniform_scale"] == 2.0
    assert extent["actual"] == 120_000.0


def test_only_exact_current_passed_report_is_reusable() -> None:
    report = require_static_mesh_quality(_valid_manifest())
    stale = deepcopy(report)
    stale["ruleset_version"] = "m2_static_mesh_v0"
    failed = deepcopy(report)
    failed["checks"]["positive_lod_count"]["status"] = "failed"
    incomplete = deepcopy(report)
    del incomplete["checks"]["positive_lod_count"]
    wrong_policy = deepcopy(report)
    wrong_policy["policy"]["require_texture_references"] = True

    assert is_current_passed_quality(stale) is False
    assert is_current_passed_quality(failed) is False
    assert is_current_passed_quality(incomplete) is False
    assert (
        is_current_passed_quality(
            wrong_policy,
            require_single_static_mesh=True,
            require_texture_references=False,
        )
        is False
    )
    assert is_current_passed_quality(None) is False
