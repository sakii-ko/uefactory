from __future__ import annotations

import json
import os
import time
from pathlib import Path

import unreal


def main() -> None:
    job = _load_job()
    out_dir = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = str(job.get("filename", "smoke_frame"))
    width = int(job.get("width", 1280))
    height = int(job.get("height", 720))
    unreal.log(f"[UEF-SMOKE] start out={out_dir} size={width}x{height}")

    editor_actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    editor_subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    world = editor_subsystem.get_editor_world()
    if world is None:
        raise RuntimeError("Could not get editor world")

    unreal.SystemLibrary.execute_console_command(world, "DisableAllScreenMessages")
    _clear_existing(editor_actor_subsystem)
    _build_scene(editor_actor_subsystem)

    render_target = unreal.RenderingLibrary.create_render_target2d(
        world,
        width,
        height,
        unreal.TextureRenderTargetFormat.RTF_RGBA8,
    )
    unreal.RenderingLibrary.clear_render_target2d(
        world,
        render_target,
        unreal.LinearColor(0, 0, 0, 1),
    )
    _capture_scene(editor_actor_subsystem, render_target)

    unreal.RenderingLibrary.export_render_target(
        world,
        render_target,
        str(out_dir),
        filename,
    )
    unreal.log(f"[UEF-SMOKE] out_dir files={sorted(os.listdir(out_dir))}")
    unreal.log(f"[UEF-SMOKE] wrote {out_dir / filename}")


def _load_job() -> dict:
    job_file = os.environ.get("UEF_JOB_FILE")
    if not job_file:
        raise RuntimeError("UEF_JOB_FILE is not set")
    with Path(job_file).open("r", encoding="utf-8") as file:
        return json.load(file)


def _clear_existing(editor_actor_subsystem) -> None:
    for actor in list(editor_actor_subsystem.get_all_level_actors()):
        if unreal.Name("UEF_SMOKE") in actor.tags:
            destroyed = editor_actor_subsystem.destroy_actor(actor)
            if destroyed is False:
                raise RuntimeError(f"Could not destroy actor {actor.get_actor_label()}")


def _build_scene(editor_actor_subsystem) -> None:
    # Template_Default can expose fallback skydome warning text in offscreen captures.
    _spawn_mesh(
        editor_actor_subsystem,
        "/Engine/BasicShapes/Cube",
        "UEF_Smoke_Backdrop",
        (1200, 0, 520),
        (0, 0, 0),
        (0.2, 60, 30),
    )
    _spawn_mesh(
        editor_actor_subsystem,
        "/Engine/BasicShapes/Plane",
        "UEF_Smoke_Plane",
        (250, 0, 0),
        (0, 0, 0),
        (16, 16, 1),
    )
    _spawn_mesh(
        editor_actor_subsystem,
        "/Engine/BasicShapes/Cube",
        "UEF_Smoke_Cube",
        (180, 0, 100),
        (0, 0, 0),
        (2, 2, 2),
    )
    directional = editor_actor_subsystem.spawn_actor_from_class(
        unreal.DirectionalLight,
        unreal.Vector(-200, -200, 800),
        unreal.Rotator(-55, 20, 0),
    )
    directional.set_actor_label("UEF_Smoke_DirectionalLight")
    directional.tags = [unreal.Name("UEF_SMOKE")]
    _set_movable(directional)
    directional.light_component.set_editor_property("intensity", 20.0)

    skylight = editor_actor_subsystem.spawn_actor_from_class(
        unreal.SkyLight,
        unreal.Vector(0, 0, 400),
        unreal.Rotator(),
    )
    skylight.set_actor_label("UEF_Smoke_SkyLight")
    skylight.tags = [unreal.Name("UEF_SMOKE")]
    _set_movable(skylight)
    skylight.light_component.set_editor_property("intensity", 5.0)
    if hasattr(skylight.light_component, "recapture_sky"):
        skylight.light_component.recapture_sky()


def _capture_scene(editor_actor_subsystem, render_target) -> None:
    capture = editor_actor_subsystem.spawn_actor_from_class(
        unreal.SceneCapture2D,
        unreal.Vector(-800, 0, 320),
        unreal.Rotator(-12, 0, 0),
    )
    capture.set_actor_label("UEF_Smoke_SceneCapture2D")
    capture.tags = [unreal.Name("UEF_SMOKE")]
    _set_movable(capture)

    component = getattr(capture, "capture_component2d", None) or getattr(
        capture,
        "scene_capture_component2d",
        None,
    )
    if component is None:
        raise RuntimeError("SceneCapture2D component is unavailable")
    component.set_editor_property("texture_target", render_target)
    component.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
    component.set_editor_property("fov_angle", 55.0)
    component.set_editor_property("capture_every_frame", False)
    component.set_editor_property("capture_on_movement", False)
    component.set_editor_property("always_persist_rendering_state", True)

    # A single offscreen capture can run before exposure/rendering state settles in headless editor.
    component.capture_scene()
    time.sleep(0.25)
    component.capture_scene()
    time.sleep(0.25)
    unreal.log("[UEF-SMOKE] scene capture rendered")


def _spawn_mesh(
    editor_actor_subsystem,
    mesh_path: str,
    label: str,
    location: tuple[float, float, float],
    rotation: tuple[float, float, float],
    scale: tuple[float, float, float],
    material_path: str | None = None,
):
    actor = editor_actor_subsystem.spawn_actor_from_class(
        unreal.StaticMeshActor,
        unreal.Vector(*location),
        unreal.Rotator(*rotation),
    )
    actor.set_actor_label(label)
    actor.tags = [unreal.Name("UEF_SMOKE")]
    _set_movable(actor)
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    if mesh is None:
        raise RuntimeError(f"Could not load mesh: {mesh_path}")
    actor.static_mesh_component.set_static_mesh(mesh)
    if material_path:
        material = unreal.EditorAssetLibrary.load_asset(material_path)
        if material is None:
            raise RuntimeError(f"Could not load material: {material_path}")
        actor.static_mesh_component.set_material(0, material)
    actor.set_actor_scale3d(unreal.Vector(*scale))
    return actor


def _set_movable(actor) -> None:
    components = [
        getattr(actor, "root_component", None),
        getattr(actor, "static_mesh_component", None),
        getattr(actor, "light_component", None),
        getattr(actor, "capture_component2d", None),
        getattr(actor, "scene_capture_component2d", None),
    ]
    seen = set()
    for component in components:
        if component is None or id(component) in seen:
            continue
        seen.add(id(component))
        try:
            component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
        except Exception as exc:
            raise RuntimeError(f"Could not set movable on {component}: {exc}") from exc


if __name__ == "__main__":
    main()
