from __future__ import annotations

from pathlib import Path
from re import escape

import pytest

from uefactory.render.jobspec import JobSpecError, parse_jobspec
from uefactory.render.passes import PASS_ORDER


def valid_jobspec() -> dict[str, object]:
    return {
        "job": "render",
        "assets": ["builtin:cube"],
        "camera": {
            "rig": "orbit",
            "views": 8,
            "elevation_deg": 20,
            "fov": 55,
            "resolution": [640, 360],
        },
        "lighting": {"preset": "three_point"},
        "passes": ["beauty_lit"],
        "output": {"dir": "out/renders"},
    }


def test_parse_jobspec_accepts_t13_orbit_cube() -> None:
    spec = parse_jobspec(valid_jobspec(), source_path=Path("examples/orbit8.yaml"))

    assert spec.job == "render"
    assert spec.asset_id == "builtin:cube"
    assert spec.camera.views == 8
    assert spec.camera.resolution == (640, 360)
    assert spec.lighting.preset == "three_point"
    assert spec.passes == ("beauty_lit",)
    assert spec.output.dir == Path("out/renders")


def test_parse_jobspec_accepts_t14_all_passes() -> None:
    raw = valid_jobspec()
    raw["passes"] = list(PASS_ORDER)

    spec = parse_jobspec(raw, source_path=Path("examples/orbit8.yaml"))

    assert spec.passes == PASS_ORDER


def test_parse_jobspec_accepts_one_catalog_asset_id() -> None:
    raw = valid_jobspec()
    raw["assets"] = ["khronos_avocado"]

    spec = parse_jobspec(raw)

    assert spec.asset_id == "khronos_avocado"


def test_parse_jobspec_accepts_scene_reference() -> None:
    raw = valid_jobspec()
    raw["assets"] = ["scene:bm_fantasy_diorama"]

    spec = parse_jobspec(raw)

    assert spec.asset_id == "scene:bm_fantasy_diorama"
    assert spec.scene_id == "bm_fantasy_diorama"


@pytest.mark.parametrize("asset", ["scene:", "scene:Bad", "scene:two__underscores"])
def test_parse_jobspec_rejects_invalid_scene_reference(asset: str) -> None:
    raw = valid_jobspec()
    raw["assets"] = [asset]

    with pytest.raises(JobSpecError, match=r"\$\.assets\[0\]"):
        parse_jobspec(raw)


def test_parse_jobspec_accepts_t15_hdri_lighting() -> None:
    raw = valid_jobspec()
    raw["lighting"] = {"preset": "hdri", "hdri": "studio_small_03_1k"}

    spec = parse_jobspec(raw, source_path=Path("examples/orbit8_hdri.yaml"))

    assert spec.lighting.preset == "hdri"
    assert spec.lighting.hdri == "studio_small_03_1k"


def test_parse_jobspec_accepts_t15_none_lighting() -> None:
    raw = valid_jobspec()
    raw["lighting"] = {"preset": "none"}

    spec = parse_jobspec(raw, source_path=Path("examples/orbit8_none.yaml"))

    assert spec.lighting.preset == "none"
    assert spec.lighting.hdri is None


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"unknown": True}, "$: unknown key 'unknown'"),
        ({"camera": {"rig": "fixed"}}, "$.camera.rig"),
        ({"camera": {"views": 1}}, "$.camera.views"),
        ({"camera": {"elevation_deg": 90}}, "$.camera.elevation_deg"),
        ({"camera": {"fov": 9}}, "$.camera.fov"),
        ({"camera": {"resolution": [640]}}, "$.camera.resolution"),
        ({"assets": ["one", "two"]}, "$.assets"),
        ({"assets": ["Bad Asset"]}, "$.assets[0]"),
        ({"assets": ["catalog:chair"]}, "$.assets[0]"),
        ({"lighting": {"preset": "unlit"}}, "$.lighting.preset"),
        ({"lighting": {"preset": "three_point", "hdri": "studio_small_03_1k"}}, "$.lighting.hdri"),
        ({"passes": ["not_a_pass"]}, "$.passes[0]"),
        ({"passes": ["beauty_lit", "beauty_lit"]}, "$.passes[1]"),
    ],
)
def test_parse_jobspec_rejects_unsupported_or_invalid_values(
    patch: dict[str, object],
    message: str,
) -> None:
    raw = valid_jobspec()
    _deep_update(raw, patch)

    with pytest.raises(JobSpecError, match=escape(message)):
        parse_jobspec(raw)


def test_parse_jobspec_reports_missing_field_path() -> None:
    raw = valid_jobspec()
    del raw["camera"]

    with pytest.raises(JobSpecError, match=r"\$: missing required key 'camera'"):
        parse_jobspec(raw)


def _deep_update(target: dict[str, object], patch: dict[str, object]) -> None:
    for key, value in patch.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _deep_update(current, value)
        else:
            target[key] = value
