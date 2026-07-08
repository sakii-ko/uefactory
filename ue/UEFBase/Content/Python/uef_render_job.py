from __future__ import annotations

import json
import math
import os
from pathlib import Path

import unreal


def main() -> None:
    job = _load_job()
    out_dir = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    camera = job["camera"]
    width, height = camera["resolution"]
    frames = int(job["frames"])
    unreal.log(f"[UEF-RENDER-JOB] setup start out={out_dir} frames={frames} size={width}x{height}")

    sequence = _create_sequence(job)
    unreal.log(f"[UEF-RENDER-JOB] setup complete sequence={sequence.get_path_name()}")


def _load_job() -> dict:
    job_file = os.environ.get("UEF_JOB_FILE")
    if not job_file:
        raise RuntimeError("UEF_JOB_FILE is not set")
    with Path(job_file).open("r", encoding="utf-8") as file:
        return json.load(file)


def _create_sequence(job: dict):
    run_id = str(job["run_id"])
    package_path = f"/Game/UEF/RenderJobs/{run_id}"
    asset_name = f"UEF_RenderJob_{run_id}"
    asset_path = f"{package_path}/{asset_name}"
    unreal.EditorAssetLibrary.make_directory(package_path)
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        unreal.EditorAssetLibrary.delete_asset(asset_path)

    materials = _create_materials(package_path)
    sequence = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        asset_name,
        package_path,
        unreal.LevelSequence,
        unreal.LevelSequenceFactoryNew(),
    )
    if sequence is None:
        raise RuntimeError("Could not create render job level sequence")
    frames = int(job["frames"])
    sequence.set_display_rate(unreal.FrameRate(24, 1))
    sequence.set_playback_start(0)
    sequence.set_playback_end(frames)

    _add_static_mesh_spawnable(
        sequence,
        frames=frames,
        label="UEF_Job_Cube",
        mesh_path="/Engine/BasicShapes/Cube",
        material=materials["cube"],
        location=(0, 0, 0),
        rotation=(0, 0, 0),
        scale=(1.0, 1.0, 1.0),
    )
    _add_static_mesh_spawnable(
        sequence,
        frames=frames,
        label="UEF_Job_Floor",
        mesh_path="/Engine/BasicShapes/Cube",
        material=materials["floor"],
        location=(0, 0, -90),
        rotation=(0, 0, 0),
        scale=(5.0, 5.0, 0.05),
    )
    _add_directional_light_spawnable(
        sequence,
        frames=frames,
        label="UEF_Job_KeyLight",
        location=(-250, -350, 500),
        rotation=(-50, 35, 0),
        intensity=8.0,
    )
    _add_directional_light_spawnable(
        sequence,
        frames=frames,
        label="UEF_Job_FillLight",
        location=(350, 250, 350),
        rotation=(-35, -140, 0),
        intensity=2.0,
    )
    _add_sky_light_spawnable(
        sequence,
        frames=frames,
        label="UEF_Job_SkyLight",
        location=(0, 0, 250),
        rotation=(0, 0, 0),
        intensity=1.0,
    )
    _add_orbit_camera(sequence, job)

    unreal.EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)
    unreal.log(f"[UEF-RENDER-JOB] sequence={asset_path}.{asset_name}")
    return sequence


def _create_materials(package_path: str) -> dict[str, object]:
    return {
        "cube": _create_lit_material(
            package_path,
            "UEF_Job_Cube_Mat",
            unreal.LinearColor(0.8, 0.18, 0.08, 1.0),
        ),
        "floor": _create_lit_material(
            package_path,
            "UEF_Job_Floor_Mat",
            unreal.LinearColor(0.42, 0.46, 0.50, 1.0),
        ),
    }


def _create_lit_material(package_path: str, asset_name: str, color):
    asset_path = f"{package_path}/{asset_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        unreal.EditorAssetLibrary.delete_asset(asset_path)
    material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        asset_name,
        package_path,
        unreal.Material,
        unreal.MaterialFactoryNew(),
    )
    if material is None:
        raise RuntimeError(f"Could not create material: {asset_path}")
    material.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_DEFAULT_LIT)
    material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_OPAQUE)
    color_node = unreal.MaterialEditingLibrary.create_material_expression(
        material,
        unreal.MaterialExpressionConstant3Vector,
        -400,
        0,
    )
    color_node.set_editor_property("constant", color)
    unreal.MaterialEditingLibrary.connect_material_property(
        color_node,
        "",
        unreal.MaterialProperty.MP_BASE_COLOR,
    )
    unreal.MaterialEditingLibrary.recompile_material(material)
    unreal.EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)
    return material


def _add_static_mesh_spawnable(
    sequence,
    *,
    frames: int,
    label: str,
    mesh_path: str,
    material,
    location: tuple[float, float, float],
    rotation: tuple[float, float, float],
    scale: tuple[float, float, float],
):
    binding = sequence.add_spawnable_from_class(unreal.StaticMeshActor)
    binding.set_display_name(label)
    actor = _object_template(binding)
    actor.set_actor_label(label)
    _set_transform(actor, location, rotation, scale)
    _add_transform_track(binding, frames=frames, location=location, rotation=rotation, scale=scale)
    _set_movable(actor)
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    if mesh is None:
        raise RuntimeError(f"Could not load mesh: {mesh_path}")
    component = actor.static_mesh_component
    component.set_static_mesh(mesh)
    material_slots = max(1, int(component.get_num_materials()))
    for material_index in range(material_slots):
        component.set_material(material_index, material)
    component.set_editor_property("override_materials", [material] * material_slots)
    return binding


def _add_directional_light_spawnable(
    sequence,
    *,
    frames: int,
    label: str,
    location: tuple[float, float, float],
    rotation: tuple[float, float, float],
    intensity: float,
):
    binding = sequence.add_spawnable_from_class(unreal.DirectionalLight)
    binding.set_display_name(label)
    actor = _object_template(binding)
    actor.set_actor_label(label)
    _set_transform(actor, location, rotation, (1, 1, 1))
    _add_transform_track(
        binding, frames=frames, location=location, rotation=rotation, scale=(1, 1, 1)
    )
    _set_movable(actor)
    actor.light_component.set_editor_property("intensity", intensity)
    actor.light_component.set_editor_property("cast_shadows", False)
    return binding


def _add_sky_light_spawnable(
    sequence,
    *,
    frames: int,
    label: str,
    location: tuple[float, float, float],
    rotation: tuple[float, float, float],
    intensity: float,
):
    binding = sequence.add_spawnable_from_class(unreal.SkyLight)
    binding.set_display_name(label)
    actor = _object_template(binding)
    actor.set_actor_label(label)
    _set_transform(actor, location, rotation, (1, 1, 1))
    _add_transform_track(
        binding, frames=frames, location=location, rotation=rotation, scale=(1, 1, 1)
    )
    _set_movable(actor)
    actor.light_component.set_editor_property("intensity", intensity)
    return binding


def _add_orbit_camera(sequence, job: dict) -> None:
    camera = job["camera"]
    views = int(camera["views"])
    elevation = float(camera["elevation_deg"])
    fov = float(camera["fov"])
    radius = 420.0
    first_location, first_rotation = _orbit_camera_transform(radius, 0.0, elevation)
    binding = _add_camera_spawnable(
        sequence,
        frames=views,
        label="UEF_Job_Camera",
        location=first_location,
        rotation=first_rotation,
        fov=fov,
    )
    _add_orbit_transform_keys(
        binding,
        views=views,
        radius=radius,
        elevation=elevation,
    )

    camera_cut_track = (
        sequence.add_master_track(unreal.MovieSceneCameraCutTrack)
        if hasattr(sequence, "add_master_track")
        else sequence.add_track(unreal.MovieSceneCameraCutTrack)
    )
    section = camera_cut_track.add_section()
    section.set_end_frame(views)
    section.set_start_frame(0)
    camera_binding_id = unreal.MovieSceneObjectBindingID()
    camera_binding_id.set_editor_property("Guid", binding.get_id())
    section.set_editor_property("CameraBindingID", camera_binding_id)


def _add_orbit_transform_keys(
    binding,
    *,
    views: int,
    radius: float,
    elevation: float,
) -> None:
    tracks = [
        track
        for track in binding.get_tracks()
        if isinstance(track, unreal.MovieScene3DTransformTrack)
    ]
    if len(tracks) != 1:
        raise RuntimeError(f"Expected one camera transform track, got {len(tracks)}")
    sections = tracks[0].get_sections()
    if len(sections) != 1:
        raise RuntimeError(f"Expected one camera transform section, got {len(sections)}")
    section = sections[0]
    channels = section.get_all_channels()
    if len(channels) != 9:
        raise RuntimeError(f"Expected 9 camera transform channels, got {len(channels)}")
    for view_index in range(views):
        azimuth = 360.0 * view_index / views
        location, rotation = _orbit_camera_transform(radius, azimuth, elevation)
        values = (*location, *_sequencer_rotation(rotation), 1.0, 1.0, 1.0)
        frame = unreal.FrameNumber(view_index)
        for channel, value in zip(channels, values, strict=True):
            channel.add_key(
                frame,
                float(value),
                0.0,
                unreal.MovieSceneTimeUnit.DISPLAY_RATE,
                unreal.MovieSceneKeyInterpolation.CONSTANT,
            )


def _orbit_camera_transform(
    radius: float,
    azimuth_deg: float,
    elevation_deg: float,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    azimuth_rad = math.radians(azimuth_deg)
    elevation_rad = math.radians(elevation_deg)
    horizontal = radius * math.cos(elevation_rad)
    location = (
        -horizontal * math.cos(azimuth_rad),
        -horizontal * math.sin(azimuth_rad),
        radius * math.sin(elevation_rad),
    )
    look_at = unreal.MathLibrary.find_look_at_rotation(
        unreal.Vector(*location),
        unreal.Vector(0.0, 0.0, 0.0),
    )
    rotation = (look_at.pitch, look_at.yaw, look_at.roll)
    return location, rotation


def _add_camera_spawnable(
    sequence,
    *,
    frames: int,
    label: str,
    location: tuple[float, float, float],
    rotation: tuple[float, float, float],
    fov: float,
):
    binding = sequence.add_spawnable_from_class(unreal.CineCameraActor)
    binding.set_display_name(label)
    actor = _object_template(binding)
    actor.set_actor_label(label)
    _set_transform(actor, location, rotation, (1, 1, 1))
    _configure_perspective_camera(actor, fov)
    _add_transform_track(
        binding, frames=frames, location=location, rotation=rotation, scale=(1, 1, 1)
    )
    _set_movable(actor)
    return binding


def _configure_perspective_camera(actor, fov: float) -> None:
    camera_component = getattr(actor, "camera_component", None) or getattr(
        actor,
        "cine_camera_component",
        None,
    )
    if camera_component is None:
        raise RuntimeError(f"Camera actor has no camera component: {actor}")
    camera_component.set_editor_property("projection_mode", unreal.CameraProjectionMode.PERSPECTIVE)
    try:
        camera_component.set_editor_property("field_of_view", float(fov))
        return
    except Exception as field_error:
        try:
            focal_length = 36.0 / (2.0 * math.tan(math.radians(float(fov)) / 2.0))
            camera_component.set_editor_property("current_focal_length", float(focal_length))
        except Exception as focal_error:
            raise RuntimeError(
                f"Could not set camera FOV on {camera_component}: "
                f"{field_error}; fallback failed: {focal_error}"
            ) from focal_error


def _object_template(binding):
    template = binding.get_object_template()
    if template is None:
        raise RuntimeError(f"Spawnable {binding.get_display_name()} has no object template")
    return template


def _set_transform(
    actor,
    location: tuple[float, float, float],
    rotation: tuple[float, float, float],
    scale: tuple[float, float, float],
) -> None:
    actor.set_actor_location(unreal.Vector(*location), False, True)
    actor.set_actor_rotation(unreal.Rotator(*rotation), False)
    actor.set_actor_scale3d(unreal.Vector(*scale))


def _add_transform_track(
    binding,
    *,
    frames: int,
    location: tuple[float, float, float],
    rotation: tuple[float, float, float],
    scale: tuple[float, float, float],
) -> None:
    track = binding.add_track(unreal.MovieScene3DTransformTrack)
    section = track.add_section()
    section.set_start_frame(0)
    section.set_end_frame(frames)
    values = (*location, *_sequencer_rotation(rotation), *scale)
    channels = section.get_all_channels()
    if len(channels) != len(values):
        raise RuntimeError(
            f"Expected 9 transform channels for {binding.get_display_name()}, got {len(channels)}"
        )
    for channel, value in zip(channels, values, strict=True):
        channel.set_default(float(value))


def _sequencer_rotation(
    rotation: tuple[float, float, float],
) -> tuple[float, float, float]:
    pitch, yaw, roll = rotation
    return roll, pitch, yaw


def _set_movable(actor) -> None:
    components = [
        getattr(actor, "root_component", None),
        getattr(actor, "static_mesh_component", None),
        getattr(actor, "light_component", None),
        getattr(actor, "camera_component", None),
        getattr(actor, "cine_camera_component", None),
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
