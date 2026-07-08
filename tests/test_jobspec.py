from __future__ import annotations

from pathlib import Path
from re import escape

import pytest

from uefactory.render.jobspec import JobSpecError, parse_jobspec


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


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"unknown": True}, "$: unknown key 'unknown'"),
        ({"camera": {"rig": "fixed"}}, "$.camera.rig"),
        ({"camera": {"views": 1}}, "$.camera.views"),
        ({"camera": {"elevation_deg": 90}}, "$.camera.elevation_deg"),
        ({"camera": {"fov": 9}}, "$.camera.fov"),
        ({"camera": {"resolution": [640]}}, "$.camera.resolution"),
        ({"assets": ["chair_001"]}, "$.assets"),
        ({"lighting": {"preset": "hdri"}}, "$.lighting.preset"),
        ({"passes": ["beauty_unlit"]}, "$.passes"),
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
