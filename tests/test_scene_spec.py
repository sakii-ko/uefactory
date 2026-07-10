from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from re import escape

import pytest

from uefactory.scenes.spec import (
    SceneSpecError,
    expected_map_path,
    load_scene_spec,
    parse_scene_spec,
)


def valid_scene() -> dict[str, object]:
    return {
        "schema_version": 1,
        "scene_id": "fantasy_diorama",
        "name": "Low Poly Fantasy Diorama",
        "kind": "interchange_scene",
        "source": {
            "path": "assets/fantasy-diorama.glb",
            "source": "blackmyth_asset_library",
            "source_id": "f3266f252ea98fcc",
            "source_url": "https://sketchfab.com/3d-models/f3266f252ea98fcc",
            "license": "CC-BY-4.0",
            "license_tier": "open",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "Mesh-Base — Low Poly Fantasy Diorama",
        },
        "expected": {
            "mesh_count": 6,
            "material_count": 2,
            "texture_count": 0,
            "triangle_count": 10216,
        },
        "build": {
            "map_path": "/Game/UEF/Scenes/fantasy_diorama/L_fantasy_diorama",
            "export": True,
        },
        "camera": {
            "rig": "overview_bounds",
            "yaw": -35.0,
            "pitch": -22.5,
            "distance_multiplier": 1.35,
        },
        "render": {"no_auto_floor": True},
    }


def test_scene_spec_imports_in_a_fresh_interpreter() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from uefactory.scenes.spec import SceneSpec"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_parse_scene_spec_accepts_complete_strict_mapping() -> None:
    spec = parse_scene_spec(valid_scene())

    assert spec.schema_version == 1
    assert spec.scene_id == "fantasy_diorama"
    assert spec.name == "Low Poly Fantasy Diorama"
    assert spec.kind == "interchange_scene"
    assert spec.source.path == "assets/fantasy-diorama.glb"
    assert spec.source.source == "blackmyth_asset_library"
    assert spec.source.source_id == "f3266f252ea98fcc"
    assert spec.source.license_tier == "open"
    assert spec.expected is not None
    assert spec.expected.mesh_count == 6
    assert spec.expected.material_count == 2
    assert spec.expected.texture_count == 0
    assert spec.expected.triangle_count == 10216
    assert spec.build.map_path == expected_map_path("fantasy_diorama")
    assert spec.build.export is True
    assert spec.camera.rig == "overview_bounds"
    assert spec.camera.yaw == -35.0
    assert spec.camera.pitch == -22.5
    assert spec.camera.distance_multiplier == 1.35
    assert spec.render.no_auto_floor is True
    assert spec.render.minimum_object_stencil_coverage is None
    assert spec.render.maximum_background_contamination_ratio is None
    assert spec.source_path == Path("<memory>")


def test_parse_scene_spec_allows_expected_counts_to_be_omitted() -> None:
    raw = valid_scene()
    del raw["expected"]

    spec = parse_scene_spec(raw)

    assert spec.expected is None
    assert "expected" not in spec.as_dict()


def test_parse_scene_spec_allows_partial_and_zero_expected_counts() -> None:
    raw = valid_scene()
    raw["expected"] = {"material_count": 0}

    spec = parse_scene_spec(raw)

    assert spec.expected is not None
    assert spec.expected.as_dict() == {"material_count": 0}


def test_source_path_policy_is_left_to_caller() -> None:
    relative = parse_scene_spec(valid_scene())
    absolute_raw = valid_scene()
    source = absolute_raw["source"]
    assert isinstance(source, dict)
    source["path"] = "/mnt/library/scene.FBX"
    absolute = parse_scene_spec(absolute_raw)

    assert relative.source.path == "assets/fantasy-diorama.glb"
    assert absolute.source.path == "/mnt/library/scene.FBX"


def test_source_root_environment_variable_is_canonical_and_explicit() -> None:
    raw = valid_scene()
    source = raw["source"]
    assert isinstance(source, dict)
    source.update(
        {
            "path": "asset-library/derived/fixture/fixture.glb",
            "root_env": "UEF_BLACKMYTH_ROOT",
        }
    )

    spec = parse_scene_spec(raw)

    assert spec.source.root_env == "UEF_BLACKMYTH_ROOT"
    assert spec.source.as_dict()["root_env"] == "UEF_BLACKMYTH_ROOT"


@pytest.mark.parametrize(
    ("source_path", "root_env", "message"),
    [
        ("/mnt/library/scene.glb", "UEF_BLACKMYTH_ROOT", "expected a relative path"),
        ("../outside/scene.glb", "UEF_BLACKMYTH_ROOT", "parent traversal is not allowed"),
        ("asset-library/scene.glb", "BLACKMYTH_ROOT", "uppercase UEF_ environment"),
        ("asset-library/scene.glb", "UEF_blackmyth_ROOT", "uppercase UEF_ environment"),
    ],
)
def test_source_root_environment_contract_rejects_ambiguous_values(
    source_path: str,
    root_env: str,
    message: str,
) -> None:
    raw = valid_scene()
    source = raw["source"]
    assert isinstance(source, dict)
    source.update({"path": source_path, "root_env": root_env})

    with pytest.raises(SceneSpecError, match=escape(message)):
        parse_scene_spec(raw)


def test_load_scene_spec_reads_yaml_and_tracks_manifest_path(tmp_path: Path) -> None:
    manifest = tmp_path / "scene.yaml"
    manifest.write_text(
        """schema_version: 1
scene_id: fantasy_diorama
name: Low Poly Fantasy Diorama
kind: interchange_scene
source:
  path: assets/fantasy-diorama.glb
  source: blackmyth_asset_library
  source_id: f3266f252ea98fcc
  source_url: https://sketchfab.com/3d-models/f3266f252ea98fcc
  license: CC-BY-4.0
  license_tier: open
  license_url: https://creativecommons.org/licenses/by/4.0/
  attribution: Mesh-Base — Low Poly Fantasy Diorama
expected:
  mesh_count: 6
  texture_count: 0
build:
  map_path: /Game/UEF/Scenes/fantasy_diorama/L_fantasy_diorama
  export: true
camera:
  rig: overview_bounds
  yaw: -35
  pitch: -22.5
  distance_multiplier: 1.35
render:
  no_auto_floor: true
""",
        encoding="utf-8",
    )

    spec = load_scene_spec(manifest)

    assert spec.source_path == manifest.resolve()
    assert spec.expected is not None
    assert spec.expected.mesh_count == 6
    assert spec.expected.texture_count == 0


def test_load_scene_spec_reads_json(tmp_path: Path) -> None:
    manifest = tmp_path / "scene.json"
    manifest.write_text(json.dumps(valid_scene()), encoding="utf-8")

    spec = load_scene_spec(manifest)

    assert spec.scene_id == "fantasy_diorama"
    assert spec.source_path == manifest.resolve()


def test_load_scene_spec_rejects_duplicate_keys_in_yaml_or_json(tmp_path: Path) -> None:
    manifest = tmp_path / "scene.yaml"
    manifest.write_text("schema_version: 1\nschema_version: 1\n", encoding="utf-8")

    with pytest.raises(SceneSpecError, match="duplicate key 'schema_version'"):
        load_scene_spec(manifest)


def test_canonical_payload_and_digest_are_stable_across_mapping_order() -> None:
    raw = valid_scene()
    reversed_raw = dict(reversed(list(copy.deepcopy(raw).items())))
    first = parse_scene_spec(raw, source_path=Path("one.yaml"))
    second = parse_scene_spec(reversed_raw, source_path=Path("somewhere/else.json"))

    assert first.as_dict() == second.as_dict()
    assert first.canonical_payload == second.canonical_payload
    assert first.digest == second.digest
    assert first.digest == hashlib.sha256(first.canonical_payload).hexdigest()
    assert json.loads(first.canonical_payload) == first.as_dict()


def test_digest_changes_with_semantic_camera_change() -> None:
    first = parse_scene_spec(valid_scene())
    changed = valid_scene()
    camera = changed["camera"]
    assert isinstance(camera, dict)
    camera["yaw"] = 25
    second = parse_scene_spec(changed)

    assert first.digest != second.digest


def test_digest_changes_with_semantic_texture_count_change() -> None:
    first = parse_scene_spec(valid_scene())
    changed = valid_scene()
    expected = changed["expected"]
    assert isinstance(expected, dict)
    expected["texture_count"] = 1
    second = parse_scene_spec(changed)

    assert first.digest != second.digest


@pytest.mark.parametrize(
    ("scene_id", "message"),
    [
        ("", "expected 1 to 64 characters"),
        ("FantasyDiorama", "expected lowercase snake_case"),
        ("1fantasy", "expected lowercase snake_case"),
        ("fantasy__diorama", "expected lowercase snake_case"),
        ("fantasy_diorama_", "expected lowercase snake_case"),
    ],
)
def test_scene_id_must_be_a_stable_safe_slug(scene_id: str, message: str) -> None:
    raw = valid_scene()
    raw["scene_id"] = scene_id

    with pytest.raises(SceneSpecError, match=escape(message)):
        parse_scene_spec(raw)


@pytest.mark.parametrize("version", [True, 0, 2, 1.0, "1"])
def test_schema_version_is_exactly_integer_one(version: object) -> None:
    raw = valid_scene()
    raw["schema_version"] = version

    with pytest.raises(SceneSpecError, match=r"\$\.schema_version"):
        parse_scene_spec(raw)


def test_kind_is_exactly_interchange_scene() -> None:
    raw = valid_scene()
    raw["kind"] = "static_mesh"

    with pytest.raises(SceneSpecError, match=r"\$\.kind"):
        parse_scene_spec(raw)


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("", "expected non-empty string"),
        (" scene.glb", "leading or trailing whitespace"),
        ("scene\n.glb", "control characters"),
        ("scene\u200b.glb", "control characters"),
        ("https://example.test/scene.glb", "expected a local filesystem path, not a URI"),
        ("urn:uefactory:scene.glb", "expected a local filesystem path, not a URI"),
        ("scene.obj", "expected an Interchange source"),
    ],
)
def test_source_path_rejects_unsafe_or_unsupported_values(path: str, message: str) -> None:
    raw = valid_scene()
    source = raw["source"]
    assert isinstance(source, dict)
    source["path"] = path

    with pytest.raises(SceneSpecError, match=escape(message)):
        parse_scene_spec(raw)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"source_id": "unsafe/id"}, "expected safe identifier"),
        ({"source": "BlackMyth"}, "expected lowercase slug"),
        ({"source_url": "file:///tmp/scene.glb"}, "expected absolute http(s) URL"),
        ({"source_url": "https://user:secret@example.test/scene"}, "credentials"),
        ({"source_url": "https://example.test:99999/scene"}, "invalid URL"),
        ({"source_url": "urn:unnamespaced"}, "expected namespaced URN"),
        ({"license": "All-Rights-Reserved"}, "expected an approved license"),
        ({"license_tier": "commercial"}, "expected one of"),
        ({"license_url": "file:///license"}, "expected absolute http(s) URL"),
        ({"license": "CC-BY-NC-4.0", "license_tier": "open"}, "requires 'nc'"),
    ],
)
def test_source_provenance_is_strict(patch: dict[str, object], message: str) -> None:
    raw = valid_scene()
    source = raw["source"]
    assert isinstance(source, dict)
    source.update(patch)

    with pytest.raises(SceneSpecError, match=escape(message)):
        parse_scene_spec(raw)


@pytest.mark.parametrize(
    ("license_id", "tier", "export"),
    [
        ("CC0-1.0", "open", True),
        ("CC-BY-NC-4.0", "nc", False),
        ("LicenseRef-UE-Only", "ue-only", False),
        ("LicenseRef-Research-Only", "nc", False),
    ],
)
def test_all_scene_license_tiers_are_supported(
    license_id: str,
    tier: str,
    export: bool,
) -> None:
    raw = valid_scene()
    source = raw["source"]
    build = raw["build"]
    assert isinstance(source, dict)
    assert isinstance(build, dict)
    source.update({"license": license_id, "license_tier": tier})
    build["export"] = export

    spec = parse_scene_spec(raw)

    assert spec.source.license_tier == tier
    assert spec.build.export is export


@pytest.mark.parametrize(
    ("license_id", "tier"),
    [
        ("LicenseRef-Research-Only", "nc"),
        ("LicenseRef-UE-Only", "ue-only"),
    ],
)
def test_restricted_scene_license_cannot_be_exported(
    license_id: str,
    tier: str,
) -> None:
    raw = valid_scene()
    source = raw["source"]
    assert isinstance(source, dict)
    source.update({"license": license_id, "license_tier": tier})

    with pytest.raises(
        SceneSpecError,
        match=escape(f"license {license_id!r} requires export=false"),
    ):
        parse_scene_spec(raw)


def test_nc_creative_commons_scene_may_be_marked_for_conditional_export() -> None:
    raw = valid_scene()
    source = raw["source"]
    assert isinstance(source, dict)
    source.update({"license": "CC-BY-NC-4.0", "license_tier": "nc"})

    spec = parse_scene_spec(raw)

    assert spec.build.export is True


def test_research_only_is_not_a_fourth_catalog_license_tier() -> None:
    raw = valid_scene()
    source = raw["source"]
    assert isinstance(source, dict)
    source.update(
        {
            "license": "LicenseRef-Research-Only",
            "license_tier": "research-only",
        }
    )

    with pytest.raises(SceneSpecError, match="expected one of"):
        parse_scene_spec(raw)


@pytest.mark.parametrize(
    ("field", "count"),
    [
        ("mesh_count", -1),
        ("material_count", 1.5),
        ("texture_count", True),
        ("triangle_count", "6"),
    ],
)
def test_expected_counts_must_be_nonnegative_integers(field: str, count: object) -> None:
    raw = valid_scene()
    raw["expected"] = {field: count}

    with pytest.raises(SceneSpecError, match=escape(f"$.expected.{field}")):
        parse_scene_spec(raw)


def test_expected_mapping_cannot_be_empty() -> None:
    raw = valid_scene()
    raw["expected"] = {}

    with pytest.raises(SceneSpecError, match="expected at least one count"):
        parse_scene_spec(raw)


def test_build_map_path_is_exactly_derived_from_scene_id() -> None:
    raw = valid_scene()
    build = raw["build"]
    assert isinstance(build, dict)
    build["map_path"] = "/Game/Other/L_fantasy_diorama"

    with pytest.raises(SceneSpecError, match="/Game/UEF/Scenes/fantasy_diorama"):
        parse_scene_spec(raw)


def test_build_export_requires_a_boolean() -> None:
    raw = valid_scene()
    build = raw["build"]
    assert isinstance(build, dict)
    build["export"] = 1

    with pytest.raises(SceneSpecError, match=r"\$\.build\.export: expected boolean"):
        parse_scene_spec(raw)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("rig", "orbit", "overview_bounds"),
        ("yaw", float("nan"), "expected finite number"),
        ("yaw", float("inf"), "expected finite number"),
        ("yaw", True, "expected finite number"),
        ("yaw", -180.01, "expected value in [-180, 180]"),
        ("yaw", 180.01, "expected value in [-180, 180]"),
        ("pitch", -90, "overview_bounds requires value in [-89, 0)"),
        ("pitch", 0, "overview_bounds requires value in [-89, 0)"),
        ("pitch", 45, "overview_bounds requires value in [-89, 0)"),
        ("distance_multiplier", 0, "expected positive number"),
        ("distance_multiplier", float("-inf"), "expected finite number"),
    ],
)
def test_camera_is_strict_and_all_numbers_are_finite(
    field: str,
    value: object,
    message: str,
) -> None:
    raw = valid_scene()
    camera = raw["camera"]
    assert isinstance(camera, dict)
    camera[field] = value

    with pytest.raises(SceneSpecError, match=escape(message)):
        parse_scene_spec(raw)


@pytest.mark.parametrize("yaw", [-180, 180])
def test_camera_accepts_yaw_boundaries(yaw: int) -> None:
    raw = valid_scene()
    camera = raw["camera"]
    assert isinstance(camera, dict)
    camera["yaw"] = yaw

    assert parse_scene_spec(raw).camera.yaw == float(yaw)


@pytest.mark.parametrize("value", [False, 0, 1, "true"])
def test_render_requires_no_auto_floor_true(value: object) -> None:
    raw = valid_scene()
    raw["render"] = {"no_auto_floor": value}

    with pytest.raises(SceneSpecError, match=r"\$\.render\.no_auto_floor"):
        parse_scene_spec(raw)


def test_render_accepts_bounded_scene_lighting_multiplier() -> None:
    raw = valid_scene()
    raw["render"] = {
        "no_auto_floor": True,
        "lighting_intensity_multiplier": 4.0,
    }

    spec = parse_scene_spec(raw)

    assert spec.render.lighting_intensity_multiplier == 4.0
    assert spec.as_dict()["render"] == raw["render"]


@pytest.mark.parametrize("value", [True, 0.09, 100.01, float("inf")])
def test_render_rejects_invalid_scene_lighting_multiplier(value: object) -> None:
    raw = valid_scene()
    raw["render"] = {
        "no_auto_floor": True,
        "lighting_intensity_multiplier": value,
    }

    with pytest.raises(SceneSpecError, match="lighting_intensity_multiplier"):
        parse_scene_spec(raw)


@pytest.mark.parametrize("value", [0.6, 0.75, 0.8, 1.0])
def test_render_accepts_bounded_object_stencil_coverage(value: float) -> None:
    raw = valid_scene()
    raw["render"] = {
        "no_auto_floor": True,
        "minimum_object_stencil_coverage": value,
    }

    spec = parse_scene_spec(raw)

    assert spec.render.minimum_object_stencil_coverage == value
    assert spec.as_dict()["render"] == raw["render"]


@pytest.mark.parametrize("value", [True, 0.599, 1.001, float("inf")])
def test_render_rejects_invalid_object_stencil_coverage(value: object) -> None:
    raw = valid_scene()
    raw["render"] = {
        "no_auto_floor": True,
        "minimum_object_stencil_coverage": value,
    }

    with pytest.raises(SceneSpecError, match="minimum_object_stencil_coverage"):
        parse_scene_spec(raw)


@pytest.mark.parametrize("value", [0.001, 0.005, 0.01])
def test_render_accepts_bounded_background_contamination(value: float) -> None:
    raw = valid_scene()
    raw["render"] = {
        "no_auto_floor": True,
        "maximum_background_contamination_ratio": value,
    }

    spec = parse_scene_spec(raw)

    assert spec.render.maximum_background_contamination_ratio == value
    assert spec.as_dict()["render"] == raw["render"]


@pytest.mark.parametrize("value", [True, 0.0009, 0.0101, float("inf")])
def test_render_rejects_invalid_background_contamination(value: object) -> None:
    raw = valid_scene()
    raw["render"] = {
        "no_auto_floor": True,
        "maximum_background_contamination_ratio": value,
    }

    with pytest.raises(SceneSpecError, match="maximum_background_contamination_ratio"):
        parse_scene_spec(raw)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        (None, "extra"),
        ("source", "description"),
        ("expected", "image_count"),
        ("build", "overwrite"),
        ("camera", "fov"),
        ("render", "resolution"),
    ],
)
def test_unknown_keys_fail_closed(section: str | None, key: str) -> None:
    raw = valid_scene()
    target: dict[str, object]
    if section is None:
        target = raw
    else:
        nested = raw[section]
        assert isinstance(nested, dict)
        target = nested
    target[key] = True

    expected_path = "$" if section is None else f"$.{section}"
    with pytest.raises(SceneSpecError, match=escape(f"{expected_path}: unknown key {key!r}")):
        parse_scene_spec(raw)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {},
        {"schema_version": 1},
    ],
)
def test_invalid_roots_are_rejected(raw: object) -> None:
    with pytest.raises(SceneSpecError):
        parse_scene_spec(raw)


def test_checked_example_scene_specs_have_complete_provenance_and_expected_counts() -> None:
    examples = Path(__file__).parents[1] / "examples" / "scenes"
    expected = {
        "bm_cake_house": (127, 10, 13, 35550, "open", True),
        "bm_fantasy_diorama": (6, 2, 0, 10216, "open", True),
        "bm_lys_piandian": (1, 42, 0, 177610, "nc", False),
        "bm_old_church_ruins": (60, 16, 46, 355661, "open", True),
        "bm_player_home": (22, 10, 11, 39187, "open", True),
        "bm_rpg_lowpoly_arena": (55, 18, 49, 36551, "open", True),
        "bm_thunderclap_temple": (14, 1, 3, 437840, "open", True),
        "bm_zelda_temple_ruins": (25, 18, 13, 751016, "open", True),
        "bm_zelda_tilt_brush_forest": (14, 10, 7, 477968, "open", True),
    }

    specs = [load_scene_spec(path) for path in sorted(examples.glob("*.yaml"))]

    assert {spec.scene_id for spec in specs} == set(expected)
    for spec in specs:
        assert spec.expected is not None
        counts = expected[spec.scene_id]
        assert (
            spec.expected.mesh_count,
            spec.expected.material_count,
            spec.expected.texture_count,
            spec.expected.triangle_count,
            spec.source.license_tier,
            spec.build.export,
        ) == counts
        assert spec.source.source_url
        assert spec.source.license_url
        assert spec.source.attribution
        assert spec.source.root_env == "UEF_BLACKMYTH_ROOT"
        assert not Path(spec.source.path).is_absolute()
        assert spec.source.path.startswith("asset-library/")
        assert -180.0 <= spec.camera.yaw <= 180.0
        assert -89.0 <= spec.camera.pitch < 0.0
