from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from uefactory.core.identity import validate_asset_id
from uefactory.render.passes import SUPPORTED_PASSES


class JobSpecError(ValueError):
    """Raised when a JobSpec YAML file is syntactically valid but unsupported."""


@dataclass(frozen=True)
class CameraSpec:
    rig: str
    views: int
    elevation_deg: float
    fov: float
    resolution: tuple[int, int]


@dataclass(frozen=True)
class LightingSpec:
    preset: str
    hdri: str | None = None


@dataclass(frozen=True)
class OutputSpec:
    dir: Path


@dataclass(frozen=True)
class RenderJobSpec:
    job: str
    assets: tuple[str, ...]
    camera: CameraSpec
    lighting: LightingSpec
    passes: tuple[str, ...]
    output: OutputSpec
    source_path: Path
    raw: dict[str, Any]

    @property
    def frame_count(self) -> int:
        return self.camera.views

    @property
    def asset_id(self) -> str:
        return self.assets[0]

    @property
    def scene_id(self) -> str | None:
        if not self.asset_id.startswith("scene:"):
            return None
        return self.asset_id.removeprefix("scene:")


def load_jobspec(path: Path) -> RenderJobSpec:
    try:
        with path.open("r", encoding="utf-8") as file:
            raw = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        raise JobSpecError(f"$: invalid YAML: {exc}") from exc
    return parse_jobspec(raw, source_path=path)


def parse_jobspec(raw: Any, *, source_path: Path | None = None) -> RenderJobSpec:
    root = _mapping(raw, "$")
    _require_keys(root, {"job", "assets", "camera", "lighting", "passes", "output"}, "$")

    job = _string(root["job"], "$.job")
    if job != "render":
        raise JobSpecError(f"$.job: expected 'render', got {job!r}")

    assets = _string_list(root["assets"], "$.assets", min_len=1)
    if len(assets) != 1:
        raise JobSpecError("$.assets: expected exactly one asset")
    if assets[0].startswith("scene:"):
        scene_id = assets[0].removeprefix("scene:")
        try:
            validate_asset_id(scene_id, field="$.assets[0]")
        except ValueError as exc:
            raise JobSpecError(str(exc)) from exc
    elif assets[0] != "builtin:cube":
        try:
            validate_asset_id(assets[0], field="$.assets[0]")
        except ValueError as exc:
            raise JobSpecError(str(exc)) from exc

    camera_raw = _mapping(root["camera"], "$.camera")
    _require_keys(camera_raw, {"rig", "views", "elevation_deg", "fov", "resolution"}, "$.camera")
    camera = CameraSpec(
        rig=_enum(_string(camera_raw["rig"], "$.camera.rig"), {"orbit"}, "$.camera.rig"),
        views=_positive_int(camera_raw["views"], "$.camera.views"),
        elevation_deg=_number(camera_raw["elevation_deg"], "$.camera.elevation_deg"),
        fov=_number(camera_raw["fov"], "$.camera.fov"),
        resolution=_resolution(camera_raw["resolution"], "$.camera.resolution"),
    )
    if camera.views < 2:
        raise JobSpecError("$.camera.views: expected at least 2 for orbit render")
    if not -89.0 <= camera.elevation_deg <= 89.0:
        raise JobSpecError("$.camera.elevation_deg: expected value in [-89, 89]")
    if not 10.0 <= camera.fov <= 120.0:
        raise JobSpecError("$.camera.fov: expected value in [10, 120]")

    lighting_raw = _mapping(root["lighting"], "$.lighting")
    _require_keys(lighting_raw, {"preset"}, "$.lighting", optional={"hdri"})
    preset = _enum(
        _string(lighting_raw["preset"], "$.lighting.preset"),
        {"hdri", "three_point", "none"},
        "$.lighting.preset",
    )
    hdri = None
    if preset == "hdri":
        hdri = _string(lighting_raw.get("hdri", "studio_small_03_1k"), "$.lighting.hdri")
    elif "hdri" in lighting_raw:
        raise JobSpecError("$.lighting.hdri: only valid when preset is 'hdri'")
    lighting = LightingSpec(
        preset=preset,
        hdri=hdri,
    )

    passes = tuple(_string_list(root["passes"], "$.passes", min_len=1))
    seen_passes: set[str] = set()
    for index, pass_name in enumerate(passes):
        if pass_name not in SUPPORTED_PASSES:
            allowed_values = ", ".join(sorted(repr(item) for item in SUPPORTED_PASSES))
            raise JobSpecError(
                f"$.passes[{index}]: expected one of {allowed_values}, got {pass_name!r}"
            )
        if pass_name in seen_passes:
            raise JobSpecError(f"$.passes[{index}]: duplicate pass {pass_name!r}")
        seen_passes.add(pass_name)

    output_raw = _mapping(root["output"], "$.output")
    _require_keys(output_raw, {"dir"}, "$.output")
    output = OutputSpec(dir=Path(_string(output_raw["dir"], "$.output.dir")))

    return RenderJobSpec(
        job=job,
        assets=tuple(assets),
        camera=camera,
        lighting=lighting,
        passes=passes,
        output=output,
        source_path=source_path or Path("<memory>"),
        raw=root,
    )


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
        raise JobSpecError(f"{path}: missing required key {missing[0]!r}")
    if extra:
        raise JobSpecError(f"{path}: unknown key {extra[0]!r}")


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JobSpecError(f"{path}: expected mapping")
    if any(not isinstance(key, str) for key in value):
        raise JobSpecError(f"{path}: all keys must be strings")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise JobSpecError(f"{path}: expected non-empty string")
    return value


def _string_list(value: Any, path: str, *, min_len: int) -> list[str]:
    if not isinstance(value, list):
        raise JobSpecError(f"{path}: expected list")
    if len(value) < min_len:
        raise JobSpecError(f"{path}: expected at least {min_len} item(s)")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_string(item, f"{path}[{index}]"))
    return result


def _enum(value: str, allowed: set[str], path: str) -> str:
    if value not in allowed:
        allowed_values = ", ".join(sorted(repr(item) for item in allowed))
        raise JobSpecError(f"{path}: expected one of {allowed_values}, got {value!r}")
    return value


def _positive_int(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise JobSpecError(f"{path}: expected integer")
    if value <= 0:
        raise JobSpecError(f"{path}: expected positive integer")
    return value


def _number(value: Any, path: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise JobSpecError(f"{path}: expected number")
    return float(value)


def _resolution(value: Any, path: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise JobSpecError(f"{path}: expected [width, height]")
    width = _positive_int(value[0], f"{path}[0]")
    height = _positive_int(value[1], f"{path}[1]")
    if width < 64 or height < 64:
        raise JobSpecError(f"{path}: expected width and height >= 64")
    return width, height
