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
    _draw_smoke_pattern(world, render_target, width, height)
    time.sleep(0.25)

    unreal.RenderingLibrary.export_render_target(
        world,
        render_target,
        str(out_dir),
        filename,
    )
    unreal.log(f"[UEF-SMOKE] out_dir files={sorted(os.listdir(out_dir))}")
    unreal.log(f"[UEF-SMOKE] wrote {out_dir / filename}")


def _draw_smoke_pattern(world, render_target, width: int, height: int) -> None:
    canvas, _, context = unreal.RenderingLibrary.begin_draw_canvas_to_render_target(
        world,
        render_target,
    )
    canvas.draw_box(
        unreal.Vector2D(width * 0.08, height * 0.12),
        unreal.Vector2D(width * 0.36, height * 0.68),
        80.0,
        unreal.LinearColor(0.95, 0.25, 0.16, 1.0),
    )
    canvas.draw_box(
        unreal.Vector2D(width * 0.44, height * 0.18),
        unreal.Vector2D(width * 0.42, height * 0.58),
        120.0,
        unreal.LinearColor(0.12, 0.75, 0.95, 1.0),
    )
    canvas.draw_line(
        unreal.Vector2D(width * 0.12, height * 0.82),
        unreal.Vector2D(width * 0.88, height * 0.22),
        18.0,
        unreal.LinearColor(1.0, 0.92, 0.18, 1.0),
    )
    canvas.draw_line(
        unreal.Vector2D(width * 0.14, height * 0.24),
        unreal.Vector2D(width * 0.92, height * 0.78),
        10.0,
        unreal.LinearColor(0.3, 1.0, 0.42, 1.0),
    )
    unreal.RenderingLibrary.end_draw_canvas_to_render_target(world, context)
    unreal.log("[UEF-SMOKE] canvas smoke pattern rendered")


def _load_job() -> dict:
    job_file = os.environ.get("UEF_JOB_FILE")
    if not job_file:
        raise RuntimeError("UEF_JOB_FILE is not set")
    with Path(job_file).open("r", encoding="utf-8") as file:
        return json.load(file)


def _clear_existing(editor_actor_subsystem) -> None:
    for actor in list(editor_actor_subsystem.get_all_level_actors()):
        if unreal.Name("UEF_SMOKE") in actor.tags:
            editor_actor_subsystem.destroy_actor(actor)


def _build_scene(editor_actor_subsystem) -> None:
    _spawn_mesh(
        editor_actor_subsystem,
        "/Engine/BasicShapes/Plane",
        "UEF_Smoke_Plane",
        (250, 0, 0),
        (0, 0, 0),
        (16, 16, 1),
        "/Engine/EngineMaterials/EmissiveMeshMaterial",
    )
    _spawn_mesh(
        editor_actor_subsystem,
        "/Engine/BasicShapes/Cube",
        "UEF_Smoke_Cube",
        (180, 0, 220),
        (0, 0, 0),
        (4, 4, 4),
        "/Engine/EngineMaterials/EmissiveMeshMaterial",
    )
    directional = editor_actor_subsystem.spawn_actor_from_class(
        unreal.DirectionalLight,
        unreal.Vector(-200, -200, 800),
        unreal.Rotator(-55, 20, 0),
    )
    directional.set_actor_label("UEF_Smoke_DirectionalLight")
    directional.tags = [unreal.Name("UEF_SMOKE")]
    directional.light_component.set_editor_property("intensity", 20.0)

    skylight = editor_actor_subsystem.spawn_actor_from_class(
        unreal.SkyLight,
        unreal.Vector(0, 0, 400),
        unreal.Rotator(),
    )
    skylight.set_actor_label("UEF_Smoke_SkyLight")
    skylight.tags = [unreal.Name("UEF_SMOKE")]
    skylight.light_component.set_editor_property("intensity", 5.0)


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


if __name__ == "__main__":
    main()
