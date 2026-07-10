from __future__ import annotations

from pathlib import Path
from re import escape

import pytest

from uefactory.ingest.spec import IngestSpecError, load_ingest_spec, parse_ingest_spec


def valid_asset(path: str = "models/duck.glb") -> dict[str, object]:
    return {
        "asset_id": "duck_001",
        "name": "Duck",
        "normalization": {
            "source_units": "auto",
            "source_up_axis": "auto",
            "source_handedness": "auto",
            "uniform_scale": 1.0,
            "pivot_policy": "preserve_source",
        },
        "path": path,
        "dependencies": [],
        "source": "khronos",
        "source_id": "Duck-v2.0",
        "source_url": "https://github.com/KhronosGroup/glTF-Sample-Assets/tree/main/Models/Duck",
        "license": "CC-BY-4.0",
        "license_tier": "open",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "Duck by Sony, distributed by Khronos Group.",
        "tags": ["animal", "sample-model"],
    }


def valid_manifest(path: str = "models/duck.glb") -> dict[str, object]:
    return {"assets": [valid_asset(path)]}


def test_parse_ingest_spec_accepts_strict_manifest_and_resolves_relative_path(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "manifests" / "samples.yaml"

    spec = parse_ingest_spec(valid_manifest("../models/duck.glb"), source_path=source_path)

    assert spec.source_path == source_path.resolve()
    assert len(spec.assets) == 1
    asset = spec.assets[0]
    assert asset.asset_id == "duck_001"
    assert asset.normalization.uniform_scale == 1.0
    assert asset.path == (tmp_path / "models" / "duck.glb").resolve()
    assert asset.dependencies == ()
    assert asset.format == "glb"
    assert asset.license == "CC-BY-4.0"
    assert asset.license_tier == "open"
    assert asset.tags == ("animal", "sample-model")


def test_load_ingest_spec_reads_yaml(tmp_path: Path) -> None:
    model = tmp_path / "mesh.fbx"
    manifest = tmp_path / "batch.yaml"
    manifest.write_text(
        """assets:
  - asset_id: mesh_01
    name: Mesh
    normalization:
      source_units: auto
      source_up_axis: auto
      source_handedness: auto
      uniform_scale: 1.0
      pivot_policy: preserve_source
    path: mesh.fbx
    dependencies: []
    source: local
    source_id: Mesh-01
    source_url: https://example.test/mesh
    license: CC0-1.0
    license_tier: open
    license_url: https://creativecommons.org/publicdomain/zero/1.0/
    attribution: Public domain sample by Example.
    tags: []
""",
        encoding="utf-8",
    )

    spec = load_ingest_spec(manifest)

    assert spec.assets[0].path == model.resolve()


def test_load_ingest_spec_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    manifest = tmp_path / "batch.yaml"
    manifest.write_text("assets: []\nassets: []\n", encoding="utf-8")

    with pytest.raises(IngestSpecError, match="duplicate key 'assets'"):
        load_ingest_spec(manifest)


@pytest.mark.parametrize(
    ("asset_patch", "message"),
    [
        ({"unknown": True}, "$.assets[0]: unknown key 'unknown'"),
        ({"asset_id": "Duck_001"}, "$.assets[0].asset_id"),
        ({"asset_id": "duck__001"}, "$.assets[0].asset_id"),
        ({"asset_id": "1duck"}, "$.assets[0].asset_id"),
        ({"name": " Duck"}, "$.assets[0].name"),
        ({"normalization": {}}, "$.assets[0].normalization: missing required key"),
        (
            {
                "normalization": {
                    "source_units": "meter",
                    "source_up_axis": "auto",
                    "source_handedness": "auto",
                    "uniform_scale": 1.0,
                    "pivot_policy": "preserve_source",
                }
            },
            "$.assets[0].normalization.source_units",
        ),
        (
            {
                "normalization": {
                    "source_units": "auto",
                    "source_up_axis": "auto",
                    "source_handedness": "auto",
                    "uniform_scale": 0.0,
                    "pivot_policy": "preserve_source",
                }
            },
            "$.assets[0].normalization.uniform_scale",
        ),
        ({"path": "duck.obj"}, "$.assets[0].path"),
        ({"dependencies": "texture.png"}, "$.assets[0].dependencies"),
        ({"dependencies": ["../texture.png"]}, "$.assets[0].dependencies[0]"),
        ({"dependencies": ["/tmp/texture.png"]}, "$.assets[0].dependencies[0]"),
        ({"dependencies": ["C:/texture.png"]}, "$.assets[0].dependencies[0]"),
        ({"dependencies": ["textures\\base.png"]}, "backslashes"),
        ({"dependencies": ["textures//base.png"]}, "normalized relative path"),
        ({"dependencies": ["./texture.png"]}, "normalized relative path"),
        ({"source": "Khronos"}, "$.assets[0].source"),
        ({"source": "khronos-group"}, "$.assets[0].source"),
        ({"source_id": "Duck/2"}, "$.assets[0].source_id"),
        ({"source_url": "file:///tmp/duck.glb"}, "$.assets[0].source_url"),
        ({"source_url": "https://example.test/duck model"}, "whitespace"),
        ({"source_url": "https://example.test:bad/duck"}, "invalid URL"),
        ({"source_url": "https://user:password@example.test/duck"}, "credentials"),
        ({"license": "All-Rights-Reserved"}, "$.assets[0].license"),
        ({"license": "CC-BY-NC-4.0", "license_tier": "open"}, "requires 'nc'"),
        ({"license_url": "creativecommons.org/licenses/by/4.0"}, "$.assets[0].license_url"),
        ({"attribution": ""}, "$.assets[0].attribution"),
        ({"tags": ["Animal"]}, "$.assets[0].tags[0]"),
        ({"tags": ["animal", "animal"]}, "duplicate tag 'animal'"),
    ],
)
def test_parse_ingest_spec_rejects_invalid_asset_fields(
    asset_patch: dict[str, object],
    message: str,
) -> None:
    raw = valid_manifest()
    asset = raw["assets"]
    assert isinstance(asset, list)
    assert isinstance(asset[0], dict)
    asset[0].update(asset_patch)

    with pytest.raises(IngestSpecError, match=escape(message)):
        parse_ingest_spec(raw)


def test_parse_ingest_spec_rejects_missing_field() -> None:
    raw = valid_manifest()
    assets = raw["assets"]
    assert isinstance(assets, list)
    assert isinstance(assets[0], dict)
    del assets[0]["license"]

    with pytest.raises(IngestSpecError, match=r"missing required key 'license'"):
        parse_ingest_spec(raw)


def test_parse_ingest_spec_requires_dependencies_field() -> None:
    raw = valid_manifest()
    assets = raw["assets"]
    assert isinstance(assets, list)
    assert isinstance(assets[0], dict)
    del assets[0]["dependencies"]

    with pytest.raises(IngestSpecError, match=r"missing required key 'dependencies'"):
        parse_ingest_spec(raw)


def test_dependencies_resolve_from_main_file_directory(tmp_path: Path) -> None:
    model_dir = tmp_path / "nested" / "model"
    texture = model_dir / "textures" / "base color.png"
    texture.parent.mkdir(parents=True)
    texture.write_bytes(b"texture")
    model = model_dir / "asset.fbx"
    model.write_bytes(b"fbx")
    raw = valid_manifest(str(model))
    assets = raw["assets"]
    assert isinstance(assets, list)
    assert isinstance(assets[0], dict)
    assets[0]["dependencies"] = ["textures/base color.png"]

    spec = parse_ingest_spec(raw, source_path=tmp_path / "elsewhere" / "batch.yaml")

    assert spec.assets[0].dependencies == (Path("textures/base color.png"),)


def test_dependencies_reject_duplicate_path(tmp_path: Path) -> None:
    model = tmp_path / "asset.fbx"
    model.write_bytes(b"fbx")
    (tmp_path / "texture.png").write_bytes(b"texture")
    raw = valid_manifest(str(model))
    assets = raw["assets"]
    assert isinstance(assets, list)
    assert isinstance(assets[0], dict)
    assets[0]["dependencies"] = ["texture.png", "texture.png"]

    with pytest.raises(IngestSpecError, match="duplicate dependency 'texture.png'"):
        parse_ingest_spec(raw)


def test_dependencies_reject_missing_file(tmp_path: Path) -> None:
    model = tmp_path / "asset.fbx"
    model.write_bytes(b"fbx")
    raw = valid_manifest(str(model))
    assets = raw["assets"]
    assert isinstance(assets, list)
    assert isinstance(assets[0], dict)
    assets[0]["dependencies"] = ["missing.png"]

    with pytest.raises(IngestSpecError, match="dependency file does not exist"):
        parse_ingest_spec(raw)


def test_dependencies_reject_symbolic_link(tmp_path: Path) -> None:
    model = tmp_path / "asset.fbx"
    model.write_bytes(b"fbx")
    target = tmp_path / "target.png"
    target.write_bytes(b"texture")
    (tmp_path / "texture.png").symlink_to(target)
    raw = valid_manifest(str(model))
    assets = raw["assets"]
    assert isinstance(assets, list)
    assert isinstance(assets[0], dict)
    assets[0]["dependencies"] = ["texture.png"]

    with pytest.raises(IngestSpecError, match="symbolic links are not allowed"):
        parse_ingest_spec(raw)


def test_main_asset_rejects_symbolic_link_before_resolving(tmp_path: Path) -> None:
    target = tmp_path / "target.glb"
    target.write_bytes(b"target")
    source = tmp_path / "source.glb"
    source.symlink_to(target)

    with pytest.raises(IngestSpecError, match="symbolic links are not allowed"):
        parse_ingest_spec(
            valid_manifest("source.glb"),
            source_path=tmp_path / "batch.yaml",
        )


def test_main_asset_rejects_symlinked_ancestor_before_resolving(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "source.glb").write_bytes(b"target")
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(IngestSpecError, match="symbolic links are not allowed"):
        parse_ingest_spec(
            valid_manifest("linked/source.glb"),
            source_path=tmp_path / "batch.yaml",
        )


def test_dependencies_reject_main_asset_path(tmp_path: Path) -> None:
    model = tmp_path / "asset.fbx"
    model.write_bytes(b"fbx")
    raw = valid_manifest(str(model))
    assets = raw["assets"]
    assert isinstance(assets, list)
    assert isinstance(assets[0], dict)
    assets[0]["dependencies"] = ["asset.fbx"]

    with pytest.raises(IngestSpecError, match="main asset file is not a dependency"):
        parse_ingest_spec(raw)


def test_dependencies_reject_directory(tmp_path: Path) -> None:
    model = tmp_path / "asset.fbx"
    model.write_bytes(b"fbx")
    (tmp_path / "textures").mkdir()
    raw = valid_manifest(str(model))
    assets = raw["assets"]
    assert isinstance(assets, list)
    assert isinstance(assets[0], dict)
    assets[0]["dependencies"] = ["textures"]

    with pytest.raises(IngestSpecError, match="dependency file does not exist"):
        parse_ingest_spec(raw)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {},
        {"assets": "not-a-list"},
        {"assets": []},
        {"assets": ["not-a-mapping"]},
        {"assets": [valid_asset()], "version": 1},
    ],
)
def test_parse_ingest_spec_rejects_invalid_root(raw: object) -> None:
    with pytest.raises(IngestSpecError):
        parse_ingest_spec(raw)


def test_parse_ingest_spec_rejects_duplicate_asset_id() -> None:
    first = valid_asset()
    second = valid_asset("models/other.fbx")
    second["name"] = "Other"

    with pytest.raises(IngestSpecError, match="duplicate asset_id 'duck_001'"):
        parse_ingest_spec({"assets": [first, second]})


@pytest.mark.parametrize("extension", [".fbx", ".FBX", ".gltf", ".GLTF", ".glb", ".GLB"])
def test_parse_ingest_spec_supports_required_formats(extension: str) -> None:
    spec = parse_ingest_spec(valid_manifest(f"asset{extension}"))

    assert spec.assets[0].format == extension.lower().removeprefix(".")


def test_parse_ingest_spec_accepts_all_license_tiers() -> None:
    values = [
        ("CC0-1.0", "open"),
        ("MIT", "open"),
        ("CC-BY-NC-4.0", "nc"),
        ("LicenseRef-Research-Only", "nc"),
        ("LicenseRef-UE-Only", "ue-only"),
    ]
    assets: list[dict[str, object]] = []
    for index, (license_id, tier) in enumerate(values):
        asset = valid_asset(f"asset_{index}.glb")
        asset["asset_id"] = f"asset_{index}"
        asset["license"] = license_id
        asset["license_tier"] = tier
        assets.append(asset)

    spec = parse_ingest_spec({"assets": assets})

    assert [(asset.license, asset.license_tier) for asset in spec.assets] == values
