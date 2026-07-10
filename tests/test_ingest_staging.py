from __future__ import annotations

import json
import struct
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from uefactory.ingest.source_structure import (
    FBX_SOURCE_STRUCTURE_POLICY,
    GLTF_SOURCE_STRUCTURE_POLICY,
    UE_OUTPUT_STRUCTURE_POLICY,
    SourceStructureError,
    inspect_source_structure,
    is_valid_source_structure_evidence,
    source_structure_sha256,
)
from uefactory.ingest.spec import IngestAssetSpec, parse_ingest_spec
from uefactory.ingest.staging import (
    StagingError,
    bundle_sha256,
    content_sha256,
    gltf_dependency_paths,
    stage_asset,
    stage_batch,
)


def asset_spec(
    path: Path,
    *,
    asset_id: str = "test_asset",
    dependencies: list[str] | None = None,
) -> IngestAssetSpec:
    raw = {
        "assets": [
            {
                "asset_id": asset_id,
                "name": "Test Asset",
                "normalization": {
                    "source_units": "auto",
                    "source_up_axis": "auto",
                    "source_handedness": "auto",
                    "uniform_scale": 1.0,
                    "pivot_policy": "preserve_source",
                },
                "path": str(path),
                "dependencies": dependencies or [],
                "source": "local",
                "source_id": "test-asset",
                "source_url": "https://example.test/assets/test-asset",
                "license": "CC0-1.0",
                "license_tier": "open",
                "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                "attribution": "Public domain test fixture.",
                "tags": ["test"],
            }
        ]
    }
    return parse_ingest_spec(raw).assets[0]


def write_gltf(path: Path, *, buffers: list[object], images: list[object] | None = None) -> None:
    path.write_text(
        json.dumps({"asset": {"version": "2.0"}, "buffers": buffers, "images": images or []}),
        encoding="utf-8",
    )


def write_glb(path: Path, document: dict[str, object] | None = None) -> None:
    payload = document or {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{}],
    }
    json_chunk = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * (-len(json_chunk) % 4)
    length = 12 + 8 + len(json_chunk)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, length)
        + struct.pack("<I4s", len(json_chunk), b"JSON")
        + json_chunk
    )


def box_document() -> dict[str, Any]:
    """The source graph and transform layout from Khronos Box.glb."""

    return {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [
            {
                "children": [1],
                "matrix": [
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    -1.0,
                    0.0,
                    0.0,
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                ],
            },
            {"mesh": 0},
        ],
        "meshes": [{}],
    }


def test_stage_glb_atomically_and_repeat_same_hash_is_noop(tmp_path: Path) -> None:
    source = tmp_path / "source" / "model.glb"
    source.parent.mkdir()
    write_glb(source)
    raw_root = tmp_path / "raw"

    first = stage_asset(asset_spec(source), raw_root=raw_root)
    second = stage_asset(asset_spec(source), raw_root=raw_root)

    assert first.changed is True
    assert second.changed is False
    assert first.sha256 == second.sha256
    assert first.sha256 == first.bundle_sha256
    assert first.bundle_sha256 == second.bundle_sha256
    assert first.content_sha256 == second.content_sha256
    assert first.source_structure == second.source_structure
    assert first.source_structure_sha256 == second.source_structure_sha256
    assert first.source_structure["status"] == "available"
    assert first.source_structure["ue_output_policy"] == UE_OUTPUT_STRUCTURE_POLICY
    assert first.source_structure["ue_hierarchy_preserved"] is False
    assert len(first.sha256) == 64
    assert len(first.content_sha256) == 64
    assert first.raw_dir == raw_root / "test_asset"
    assert first.raw_path == raw_root / "test_asset" / "model.glb"
    assert first.raw_path.read_bytes() == source.read_bytes()
    assert first.files == (first.raw_path,)
    assert not list(raw_root.glob(".test_asset.tmp-*"))


def test_khronos_box_glb_proves_parent_child_depth_and_non_identity_transform(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Box.glb"
    write_glb(source, box_document())

    evidence = inspect_source_structure(source)
    payload = evidence.payload

    assert payload["inspection_policy"] == GLTF_SOURCE_STRUCTURE_POLICY
    assert payload["node_count"] == 2
    assert payload["root_count"] == 1
    assert payload["child_edge_count"] == 1
    assert payload["max_depth"] == 2
    assert payload["mesh_definition_count"] == 1
    assert payload["mesh_reference_count"] == 1
    assert payload["non_identity_local_transform_count"] == 1
    assert payload["nodes"][0]["children"] == [1]
    assert payload["nodes"][0]["local_transform"]["representation"] == "matrix"
    assert payload["ue_output_policy"] == "flatten_to_single_static_mesh_v1"
    assert payload["ue_hierarchy_preserved"] is False
    assert is_valid_source_structure_evidence(
        payload,
        evidence.sha256,
        expected_source_format="glb",
    )


def test_source_structure_digest_is_canonical_across_json_formatting(tmp_path: Path) -> None:
    first = tmp_path / "first.gltf"
    second = tmp_path / "second.gltf"
    document = box_document()
    first.write_text(json.dumps(document, indent=2), encoding="utf-8")
    second.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")

    first_evidence = inspect_source_structure(first)
    second_evidence = inspect_source_structure(second)

    assert first_evidence.payload == second_evidence.payload
    assert first_evidence.sha256 == second_evidence.sha256


def test_gltf_integer_valued_numbers_are_canonicalized_as_indices(tmp_path: Path) -> None:
    source = tmp_path / "integer-valued-indices.glb"
    document = box_document()
    document["scene"] = -0.0
    document["scenes"][0]["nodes"] = [0.0]
    document["nodes"][0]["children"] = [1e0]
    document["nodes"][1]["mesh"] = 0.0
    write_glb(source, document)

    evidence = inspect_source_structure(source)

    assert evidence.payload["default_scene"] == 0
    assert type(evidence.payload["default_scene"]) is int
    assert evidence.payload["scenes"][0]["root_nodes"] == [0]
    assert evidence.payload["nodes"][0]["children"] == [1]
    assert evidence.payload["nodes"][1]["mesh"] == 0
    assert all(
        type(value) is int
        for value in (
            evidence.payload["scenes"][0]["root_nodes"][0],
            evidence.payload["nodes"][0]["children"][0],
            evidence.payload["nodes"][1]["mesh"],
        )
    )
    assert is_valid_source_structure_evidence(
        evidence.payload,
        evidence.sha256,
        expected_source_format="glb",
    )


@pytest.mark.parametrize("invalid_index", [True, 0.5, float("inf")])
def test_gltf_rejects_non_integer_or_non_finite_indices(
    tmp_path: Path,
    invalid_index: object,
) -> None:
    source = tmp_path / "invalid-index.glb"
    document = box_document()
    document["nodes"][1]["mesh"] = invalid_index
    write_glb(source, document)

    with pytest.raises(SourceStructureError, match="mesh is out of range"):
        inspect_source_structure(source)


@pytest.mark.parametrize(
    "rotation",
    [
        [0.0, 0.0, 0.0, 0.0],
        [2.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.99],
    ],
)
def test_gltf_rejects_non_unit_node_rotation(
    tmp_path: Path,
    rotation: list[float],
) -> None:
    source = tmp_path / "invalid-rotation.glb"
    document: dict[str, Any] = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "rotation": rotation}],
        "meshes": [{}],
    }
    write_glb(source, document)

    with pytest.raises(SourceStructureError, match="unit quaternion"):
        inspect_source_structure(source)


def test_gltf_accepts_unit_quaternion_with_exporter_precision(tmp_path: Path) -> None:
    source = tmp_path / "valid-rotation.glb"
    document: dict[str, Any] = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [
            {
                "mesh": 0,
                "rotation": [0.0, 0.70710678118, 0.0, 0.70710678118],
            }
        ],
        "meshes": [{}],
    }
    write_glb(source, document)

    evidence = inspect_source_structure(source)

    assert is_valid_source_structure_evidence(evidence.payload, evidence.sha256)


@pytest.mark.parametrize(
    "matrix",
    [
        [
            1.0,
            0.0,
            0.0,
            0.0,
            0.25,
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
        ],
        [
            1.0,
            0.0,
            0.0,
            0.25,
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
        ],
    ],
)
def test_gltf_rejects_shear_or_perspective_matrix(
    tmp_path: Path,
    matrix: list[float],
) -> None:
    source = tmp_path / "invalid-matrix.glb"
    document: dict[str, Any] = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "matrix": matrix}],
        "meshes": [{}],
    }
    write_glb(source, document)

    with pytest.raises(SourceStructureError, match="decomposable to TRS"):
        inspect_source_structure(source)


@pytest.mark.parametrize(
    "matrix",
    [
        [
            -2.0,
            0.0,
            0.0,
            0.0,
            0.0,
            3.0,
            0.0,
            0.0,
            0.0,
            0.0,
            4.0,
            0.0,
            5.0,
            6.0,
            7.0,
            1.0,
        ],
        [
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            -3.0,
            0.0,
            0.0,
            0.0,
            0.0,
            4.0,
            0.0,
            5.0,
            6.0,
            7.0,
            1.0,
        ],
    ],
)
def test_gltf_accepts_reflection_and_zero_scale_trs_matrices(
    tmp_path: Path,
    matrix: list[float],
) -> None:
    source = tmp_path / "valid-matrix.glb"
    document: dict[str, Any] = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "matrix": matrix}],
        "meshes": [{}],
    }
    write_glb(source, document)

    evidence = inspect_source_structure(source)

    assert evidence.payload["nodes"][0]["local_transform"] == {
        "representation": "matrix",
        "values": matrix,
    }
    assert is_valid_source_structure_evidence(evidence.payload, evidence.sha256)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("schema_version", True),
        lambda payload: payload["nodes"][0].__setitem__("index", False),
        lambda payload: payload["scenes"][0].__setitem__("index", False),
        lambda payload: payload.__setitem__("root_count", True),
        lambda payload: payload["nodes"][1].__setitem__("mesh", 0.0),
        lambda payload: payload["nodes"][1]["local_transform"].__setitem__(
            "rotation", [0.0, 0.0, 0.0, 0.0]
        ),
        lambda payload: payload["nodes"][0]["local_transform"]["values"].__setitem__(4, 0.25),
    ],
)
def test_source_structure_validator_rejects_noncanonical_or_forged_payload(
    tmp_path: Path,
    mutate: object,
) -> None:
    source = tmp_path / "Box.glb"
    write_glb(source, box_document())
    evidence = inspect_source_structure(source)
    payload = deepcopy(evidence.payload)
    assert callable(mutate)
    mutate(payload)

    assert not is_valid_source_structure_evidence(
        payload,
        source_structure_sha256(payload),
        expected_source_format="glb",
    )


def test_fbx_structure_is_explicitly_not_available_without_fake_metrics(
    tmp_path: Path,
) -> None:
    source = tmp_path / "model.fbx"
    source.write_bytes(b"Kaydara FBX Binary")

    evidence = inspect_source_structure(source)

    assert evidence.payload == {
        "schema_version": 1,
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
    assert "node_count" not in evidence.payload
    assert "nodes" not in evidence.payload
    assert is_valid_source_structure_evidence(
        evidence.payload,
        evidence.sha256,
        expected_source_format="fbx",
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda document: document["nodes"][1].__setitem__("children", [0]),
            "cycle",
        ),
        (
            lambda document: document["nodes"][0].__setitem__("translation", [0, 0, 0]),
            "mutually exclusive",
        ),
        (
            lambda document: document["nodes"][0].__setitem__("matrix", [1.0]),
            "finite numbers",
        ),
    ],
)
def test_gltf_source_structure_fails_closed_on_invalid_graph_or_transform(
    tmp_path: Path,
    mutate: object,
    message: str,
) -> None:
    document = box_document()
    assert callable(mutate)
    mutate(document)
    source = tmp_path / "invalid.glb"
    write_glb(source, document)

    with pytest.raises(SourceStructureError, match=message):
        inspect_source_structure(source)


def test_glb_source_structure_fails_closed_on_declared_length_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "invalid.glb"
    write_glb(source, box_document())
    source.write_bytes(source.read_bytes() + b"extra")

    with pytest.raises(SourceStructureError, match="declared length"):
        inspect_source_structure(source)
    with pytest.raises(StagingError, match="declared length"):
        stage_asset(asset_spec(source), raw_root=tmp_path / "raw")

    assert not (tmp_path / "raw/test_asset").exists()


def test_stage_fbx_without_dependencies_copies_only_main_file(tmp_path: Path) -> None:
    source = tmp_path / "model.fbx"
    source.write_bytes(b"Kaydara FBX Binary")

    result = stage_asset(asset_spec(source), raw_root=tmp_path / "raw")

    assert [path.name for path in result.files] == ["model.fbx"]


@pytest.mark.parametrize("extension", ["fbx", "glb"])
def test_stage_binary_formats_copy_explicit_texture_dependencies(
    tmp_path: Path, extension: str
) -> None:
    source_dir = tmp_path / "source"
    texture = source_dir / "textures" / "basecolor.jpg"
    texture.parent.mkdir(parents=True)
    texture.write_bytes(b"jpeg texture")
    source = source_dir / f"model.{extension}"
    if extension == "glb":
        write_glb(source)
    else:
        source.write_bytes(b"binary model")

    result = stage_asset(
        asset_spec(source, dependencies=["textures/basecolor.jpg"]),
        raw_root=tmp_path / "raw",
    )

    assert [path.relative_to(result.raw_dir).as_posix() for path in result.files] == [
        f"model.{extension}",
        "textures/basecolor.jpg",
    ]
    assert (result.raw_dir / "textures" / "basecolor.jpg").read_bytes() == b"jpeg texture"


def test_stage_gltf_copies_local_buffers_and_images_preserving_paths(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    (source_dir / "buffers").mkdir(parents=True)
    (source_dir / "textures").mkdir()
    (source_dir / "buffers" / "mesh.bin").write_bytes(b"vertices")
    (source_dir / "textures" / "base color.png").write_bytes(b"png")
    gltf = source_dir / "scene.gltf"
    write_gltf(
        gltf,
        buffers=[{"uri": "buffers/mesh.bin", "byteLength": 8}],
        images=[
            {"uri": "textures/base%20color.png"},
            {"bufferView": 0, "mimeType": "image/png"},
        ],
    )

    assert gltf_dependency_paths(gltf) == (
        Path("buffers/mesh.bin"),
        Path("textures/base color.png"),
    )
    result = stage_asset(
        asset_spec(
            gltf,
            dependencies=["buffers/mesh.bin", "textures/base color.png"],
        ),
        raw_root=tmp_path / "raw",
    )

    assert [path.relative_to(result.raw_dir).as_posix() for path in result.files] == [
        "buffers/mesh.bin",
        "scene.gltf",
        "textures/base color.png",
    ]
    assert (result.raw_dir / "buffers" / "mesh.bin").read_bytes() == b"vertices"
    assert (result.raw_dir / "textures" / "base color.png").read_bytes() == b"png"


def test_stage_gltf_rejects_reference_missing_from_manifest(tmp_path: Path) -> None:
    (tmp_path / "mesh.bin").write_bytes(b"mesh")
    gltf = tmp_path / "scene.gltf"
    write_gltf(gltf, buffers=[{"uri": "mesh.bin"}])

    with pytest.raises(StagingError, match="missing from manifest: mesh.bin"):
        stage_asset(asset_spec(gltf), raw_root=tmp_path / "raw")


def test_stage_gltf_rejects_declared_file_not_referenced_by_document(tmp_path: Path) -> None:
    (tmp_path / "mesh.bin").write_bytes(b"mesh")
    (tmp_path / "unused.png").write_bytes(b"unused")
    gltf = tmp_path / "scene.gltf"
    write_gltf(gltf, buffers=[{"uri": "mesh.bin"}])

    with pytest.raises(StagingError, match="not referenced by glTF: unused.png"):
        stage_asset(
            asset_spec(gltf, dependencies=["mesh.bin", "unused.png"]),
            raw_root=tmp_path / "raw",
        )


def test_stage_conflicting_asset_id_fails_closed_without_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source.fbx"
    source.write_bytes(b"first")
    raw_root = tmp_path / "raw"
    first = stage_asset(asset_spec(source), raw_root=raw_root)
    source.write_bytes(b"second")

    with pytest.raises(StagingError, match="asset_id conflict"):
        stage_asset(asset_spec(source), raw_root=raw_root)

    assert first.raw_path.read_bytes() == b"first"


@pytest.mark.parametrize("asset_id", ["../escape", "/tmp/escape", "nested/escape", "_escape"])
def test_stage_asset_revalidates_public_spec_asset_id(
    tmp_path: Path,
    asset_id: str,
) -> None:
    source = tmp_path / "source.glb"
    source.write_bytes(b"asset")
    unsafe = replace(asset_spec(source), asset_id=asset_id)

    with pytest.raises(StagingError, match="invalid staged asset_id"):
        stage_asset(unsafe, raw_root=tmp_path / "raw")

    assert not (tmp_path / "escape").exists()


def test_extra_file_in_existing_destination_is_a_conflict(tmp_path: Path) -> None:
    source = tmp_path / "source.fbx"
    source.write_bytes(b"asset")
    raw_root = tmp_path / "raw"
    result = stage_asset(asset_spec(source), raw_root=raw_root)
    (result.raw_dir / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(StagingError, match="asset_id conflict"):
        stage_asset(asset_spec(source), raw_root=raw_root)


def test_changed_dependency_conflicts_without_overwriting_staged_bundle(tmp_path: Path) -> None:
    source = tmp_path / "source.fbx"
    source.write_bytes(b"fbx")
    dependency = tmp_path / "texture.png"
    dependency.write_bytes(b"first")
    raw_root = tmp_path / "raw"
    first = stage_asset(
        asset_spec(source, dependencies=["texture.png"]),
        raw_root=raw_root,
    )
    dependency.write_bytes(b"second")

    with pytest.raises(StagingError, match="asset_id conflict"):
        stage_asset(
            asset_spec(source, dependencies=["texture.png"]),
            raw_root=raw_root,
        )

    assert (first.raw_dir / "texture.png").read_bytes() == b"first"


@pytest.mark.parametrize(
    ("uri", "message"),
    [
        ("https://example.test/mesh.bin", "remote/absolute URI"),
        ("//example.test/mesh.bin", "remote/absolute URI"),
        ("data:application/octet-stream;base64,AA==", "data URI"),
        ("file:///tmp/mesh.bin", "remote/absolute URI"),
        ("../mesh.bin", "stay within"),
        ("%2e%2e/mesh.bin", "stay within"),
        ("/tmp/mesh.bin", "stay within"),
        ("textures\\base.png", "invalid local URI path"),
        ("mesh.bin?version=1", "query strings"),
        ("mesh.bin#part", "query strings"),
        ("mesh file.bin", "whitespace"),
        ("mesh%ZZ.bin", "invalid percent encoding"),
        ("C:/mesh.bin", "remote/absolute URI"),
        ("C%3A/mesh.bin", "absolute drive path"),
    ],
)
def test_gltf_rejects_unsafe_dependency_uri(tmp_path: Path, uri: str, message: str) -> None:
    gltf = tmp_path / "scene.gltf"
    write_gltf(gltf, buffers=[{"uri": uri}])

    with pytest.raises(StagingError, match=message):
        gltf_dependency_paths(gltf)


def test_gltf_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "mesh.bin").symlink_to(outside)
    gltf = bundle / "scene.gltf"
    write_gltf(gltf, buffers=[{"uri": "mesh.bin"}])

    with pytest.raises(StagingError, match="symbolic link"):
        gltf_dependency_paths(gltf)


def test_gltf_rejects_symlink_even_when_target_stays_in_bundle(tmp_path: Path) -> None:
    target = tmp_path / "actual.bin"
    target.write_bytes(b"actual")
    (tmp_path / "mesh.bin").symlink_to(target)
    gltf = tmp_path / "scene.gltf"
    write_gltf(gltf, buffers=[{"uri": "mesh.bin"}])

    with pytest.raises(StagingError, match="symbolic link"):
        gltf_dependency_paths(gltf)


def test_gltf_rejects_symbolic_link_loop(tmp_path: Path) -> None:
    (tmp_path / "mesh.bin").symlink_to("mesh.bin")
    gltf = tmp_path / "scene.gltf"
    write_gltf(gltf, buffers=[{"uri": "mesh.bin"}])

    with pytest.raises(StagingError, match="symbolic link"):
        gltf_dependency_paths(gltf)


def test_gltf_rejects_missing_dependency(tmp_path: Path) -> None:
    gltf = tmp_path / "scene.gltf"
    write_gltf(gltf, buffers=[{"uri": "missing.bin"}])

    with pytest.raises(StagingError, match="missing glTF dependency"):
        stage_asset(asset_spec(gltf), raw_root=tmp_path / "raw")


@pytest.mark.parametrize(
    "payload",
    [
        "not JSON",
        "[]",
        "{}",
        '{"asset": {"version": "1.0"}}',
        '{"buffers": {}}',
        '{"asset": {"version": "2.0"}, "buffers": {}}',
        '{"asset": {"version": "2.0"}, "buffers": [false]}',
        '{"asset": {"version": "2.0"}, "buffers": [{"uri": 1}]}',
        '{"asset": {"version": "2.0"}, "buffers": [{}]}',
    ],
)
def test_gltf_rejects_invalid_document(tmp_path: Path, payload: str) -> None:
    gltf = tmp_path / "scene.gltf"
    gltf.write_text(payload, encoding="utf-8")

    with pytest.raises(StagingError, match="invalid"):
        gltf_dependency_paths(gltf)


def test_stage_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(StagingError, match="asset source is not a regular file"):
        stage_asset(asset_spec(tmp_path / "missing.glb"), raw_root=tmp_path / "raw")


def test_stage_revalidates_dependency_removed_after_manifest_parse(tmp_path: Path) -> None:
    source = tmp_path / "source.fbx"
    source.write_bytes(b"fbx")
    dependency = tmp_path / "texture.png"
    dependency.write_bytes(b"texture")
    spec = asset_spec(source, dependencies=["texture.png"])
    dependency.unlink()

    with pytest.raises(StagingError, match="missing declared dependency"):
        stage_asset(spec, raw_root=tmp_path / "raw")


def test_stage_revalidates_dependency_replaced_by_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.fbx"
    source.write_bytes(b"fbx")
    dependency = tmp_path / "texture.png"
    dependency.write_bytes(b"texture")
    spec = asset_spec(source, dependencies=["texture.png"])
    dependency.unlink()
    target = tmp_path / "target.png"
    target.write_bytes(b"target")
    dependency.symlink_to(target)

    with pytest.raises(StagingError, match="may not be a symbolic link"):
        stage_asset(spec, raw_root=tmp_path / "raw")


def test_stage_rejects_main_source_symlink_on_direct_spec(tmp_path: Path) -> None:
    target = tmp_path / "target.glb"
    target.write_bytes(b"target")
    source = tmp_path / "source.glb"
    source.symlink_to(target)
    spec = replace(asset_spec(target), path=source)

    with pytest.raises(StagingError, match="may not traverse a symbolic link"):
        stage_asset(spec, raw_root=tmp_path / "raw")


def test_stage_rejects_main_source_with_symlinked_ancestor_on_direct_spec(
    tmp_path: Path,
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    target = actual / "source.glb"
    target.write_bytes(b"target")
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    spec = replace(asset_spec(target), path=linked / "source.glb")

    with pytest.raises(StagingError, match="may not traverse a symbolic link"):
        stage_asset(spec, raw_root=tmp_path / "raw")


def test_stage_rejects_duplicate_dependencies_on_direct_spec(tmp_path: Path) -> None:
    source = tmp_path / "source.fbx"
    source.write_bytes(b"fbx")
    dependency = tmp_path / "texture.png"
    dependency.write_bytes(b"texture")
    spec = asset_spec(source, dependencies=["texture.png"])
    duplicate = replace(spec, dependencies=(Path("texture.png"), Path("texture.png")))

    with pytest.raises(StagingError, match="duplicate declared dependency"):
        stage_asset(duplicate, raw_root=tmp_path / "raw")


def test_stage_rejects_non_directory_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.glb"
    write_glb(source)
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    (raw_root / "test_asset").write_text("collision", encoding="utf-8")

    with pytest.raises(StagingError, match="not a regular directory"):
        stage_asset(asset_spec(source), raw_root=raw_root)


def test_copy_failure_does_not_publish_partial_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.glb"
    write_glb(source)
    raw_root = tmp_path / "raw"

    def fail_copy(_source: Path, _target: Path, *, follow_symlinks: bool) -> None:
        assert follow_symlinks is False
        raise OSError("simulated copy failure")

    monkeypatch.setattr("uefactory.ingest.staging.shutil.copy2", fail_copy)

    with pytest.raises(StagingError, match="simulated copy failure"):
        stage_asset(asset_spec(source), raw_root=raw_root)

    assert not (raw_root / "test_asset").exists()
    assert not list(raw_root.glob(".test_asset.tmp-*"))


def test_bundle_hash_includes_relative_path_and_content(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.bin").write_bytes(b"same")
    (second / "b.bin").write_bytes(b"same")

    first_hash = bundle_sha256(first, (Path("a.bin"),))
    second_hash = bundle_sha256(second, (Path("b.bin"),))
    (first / "a.bin").write_bytes(b"changed")
    changed_hash = bundle_sha256(first, (Path("a.bin"),))

    assert first_hash != second_hash
    assert first_hash != changed_hash


def test_content_hash_ignores_file_renames(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "model.glb").write_bytes(b"identical model bytes")
    (second / "renamed.glb").write_bytes(b"identical model bytes")

    assert content_sha256(first, (Path("model.glb"),)) == content_sha256(
        second,
        (Path("renamed.glb"),),
    )
    assert bundle_sha256(first, (Path("model.glb"),)) != bundle_sha256(
        second,
        (Path("renamed.glb"),),
    )


def test_content_hash_changes_when_file_content_changes(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    source = bundle / "model.glb"
    source.write_bytes(b"first content")
    first_hash = content_sha256(bundle, (Path("model.glb"),))

    source.write_bytes(b"different content")

    assert content_sha256(bundle, (Path("model.glb"),)) != first_hash


def test_content_hash_counts_duplicate_file_contents(tmp_path: Path) -> None:
    single = tmp_path / "single"
    duplicate = tmp_path / "duplicate"
    renamed_duplicate = tmp_path / "renamed_duplicate"
    single.mkdir()
    duplicate.mkdir()
    renamed_duplicate.mkdir()
    (single / "one.bin").write_bytes(b"same")
    (duplicate / "one.bin").write_bytes(b"same")
    (duplicate / "two.bin").write_bytes(b"same")
    (renamed_duplicate / "alpha.bin").write_bytes(b"same")
    (renamed_duplicate / "beta.bin").write_bytes(b"same")

    single_hash = content_sha256(single, (Path("one.bin"),))
    duplicate_hash = content_sha256(
        duplicate,
        (Path("one.bin"), Path("two.bin")),
    )
    renamed_duplicate_hash = content_sha256(
        renamed_duplicate,
        (Path("alpha.bin"), Path("beta.bin")),
    )

    assert duplicate_hash != single_hash
    assert duplicate_hash == renamed_duplicate_hash


def test_stage_batch_stages_every_asset(tmp_path: Path) -> None:
    first = tmp_path / "first.glb"
    second = tmp_path / "second.fbx"
    write_glb(first)
    second.write_bytes(b"second")
    first_raw = {
        "asset_id": "first_asset",
        "name": "First",
        "normalization": {
            "source_units": "auto",
            "source_up_axis": "auto",
            "source_handedness": "auto",
            "uniform_scale": 1.0,
            "pivot_policy": "preserve_source",
        },
        "path": str(first),
        "dependencies": [],
        "source": "local",
        "source_id": "first",
        "source_url": "https://example.test/first",
        "license": "CC0-1.0",
        "license_tier": "open",
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "attribution": "Test fixture.",
        "tags": [],
    }
    second_raw = dict(first_raw, asset_id="second_asset", name="Second", path=str(second))
    batch = parse_ingest_spec({"assets": [first_raw, second_raw]})

    results = stage_batch(batch, raw_root=tmp_path / "raw")

    assert [result.asset_id for result in results] == ["first_asset", "second_asset"]
    assert all(result.raw_path.is_file() for result in results)
