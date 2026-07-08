from __future__ import annotations

import json
import os
from pathlib import Path

import unreal


def main() -> None:
    job = _load_job()
    out_dir = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    width = int(job.get("width", 640))
    height = int(job.get("height", 360))
    frames = int(job.get("frames", 8))
    unreal.log(f"[UEF-MRQ-SPIKE] start out={out_dir} frames={frames} size={width}x{height}")

    sequence = _create_sequence(frames)
    unreal.log(f"[UEF-MRQ-SPIKE] setup complete sequence={sequence.get_path_name()}")


def _load_job() -> dict:
    job_file = os.environ.get("UEF_JOB_FILE")
    if not job_file:
        raise RuntimeError("UEF_JOB_FILE is not set")
    with Path(job_file).open("r", encoding="utf-8") as file:
        return json.load(file)


def _create_sequence(frames: int):
    package_path = "/Game/UEF/MRQSpike"
    asset_name = "UEF_MRQ_Spike"
    asset_path = f"{package_path}/{asset_name}"
    materials = _create_materials(package_path)
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        unreal.EditorAssetLibrary.delete_asset(asset_path)
    sequence = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        asset_name,
        package_path,
        unreal.LevelSequence,
        unreal.LevelSequenceFactoryNew(),
    )
    if sequence is None:
        raise RuntimeError("Could not create MRQ spike level sequence")
    sequence.set_display_rate(unreal.FrameRate(24, 1))
    sequence.set_playback_start(0)
    sequence.set_playback_end(frames)

    _add_static_mesh_spawnable(
        sequence,
        frames=frames,
        label="UEF_MRQ_Cube",
        mesh_path="/Engine/BasicShapes/Cube",
        material=materials["black"],
        location=(250, 0, 0),
        rotation=(0, 0, 0),
        scale=(0.05, 2.0, 1.2),
    )
    _add_static_mesh_spawnable(
        sequence,
        frames=frames,
        label="UEF_MRQ_Backdrop",
        mesh_path="/Engine/BasicShapes/Cube",
        material=materials["white"],
        location=(500, 0, 0),
        rotation=(0, 0, 0),
        scale=(0.05, 8.0, 5.0),
    )
    _add_directional_light_spawnable(
        sequence,
        frames=frames,
        label="UEF_MRQ_DirectionalLight",
        location=(-200, -200, 800),
        rotation=(-55, 20, 0),
        intensity=20.0,
    )
    _add_sky_light_spawnable(
        sequence,
        frames=frames,
        label="UEF_MRQ_SkyLight",
        location=(0, 0, 400),
        rotation=(0, 0, 0),
        intensity=5.0,
    )
    camera_binding = _add_camera_spawnable(
        sequence,
        frames=frames,
        label="UEF_MRQ_Camera",
        location=(-300, 0, 0),
        rotation=(0, 0, 0),
        ortho_width=640.0,
    )
    camera_cut_track = (
        sequence.add_master_track(unreal.MovieSceneCameraCutTrack)
        if hasattr(sequence, "add_master_track")
        else sequence.add_track(unreal.MovieSceneCameraCutTrack)
    )
    camera_cut_section = camera_cut_track.add_section()
    camera_cut_section.set_start_frame(0)
    camera_cut_section.set_end_frame(frames)
    camera_binding_id = unreal.MovieSceneObjectBindingID()
    camera_binding_id.set_editor_property("Guid", camera_binding.get_id())
    camera_cut_section.set_editor_property("CameraBindingID", camera_binding_id)

    unreal.EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)
    unreal.log(f"[UEF-MRQ-SPIKE] sequence={asset_path}.{asset_name}")
    return sequence


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
    _add_transform_track(
        binding,
        frames=frames,
        location=location,
        rotation=rotation,
        scale=scale,
    )
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
    bound_material = component.get_material(0)
    bound_path = bound_material.get_path_name() if bound_material else "<none>"
    unreal.log(f"[UEF-MRQ-SPIKE] mesh={label} slots={material_slots} material={bound_path}")
    return binding


def _create_materials(package_path: str) -> dict[str, object]:
    return {
        "white": _create_unlit_material(
            package_path,
            "UEF_MRQ_Unlit_White",
            unreal.LinearColor(1.0, 1.0, 1.0, 1.0),
        ),
        "black": _create_unlit_material(
            package_path,
            "UEF_MRQ_Unlit_Black",
            unreal.LinearColor(0.0, 0.0, 0.0, 1.0),
        ),
    }


def _create_unlit_material(package_path: str, asset_name: str, color):
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
    material.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_OPAQUE)
    material.set_editor_property("two_sided", True)
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
    unreal.MaterialEditingLibrary.connect_material_property(
        color_node,
        "",
        unreal.MaterialProperty.MP_EMISSIVE_COLOR,
    )
    unreal.MaterialEditingLibrary.recompile_material(material)
    unreal.EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)
    return material


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
        binding,
        frames=frames,
        location=location,
        rotation=rotation,
        scale=(1, 1, 1),
    )
    _set_movable(actor)
    actor.light_component.set_editor_property("intensity", intensity)
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
        binding,
        frames=frames,
        location=location,
        rotation=rotation,
        scale=(1, 1, 1),
    )
    _set_movable(actor)
    actor.light_component.set_editor_property("intensity", intensity)
    return binding


def _add_camera_spawnable(
    sequence,
    *,
    frames: int,
    label: str,
    location: tuple[float, float, float],
    rotation: tuple[float, float, float],
    ortho_width: float,
):
    binding = sequence.add_spawnable_from_class(unreal.CineCameraActor)
    binding.set_display_name(label)
    actor = _object_template(binding)
    actor.set_actor_label(label)
    _set_transform(actor, location, rotation, (1, 1, 1))
    _configure_orthographic_camera(actor, ortho_width)
    _add_transform_track(
        binding,
        frames=frames,
        location=location,
        rotation=rotation,
        scale=(1, 1, 1),
    )
    _set_movable(actor)
    return binding


def _configure_orthographic_camera(actor, ortho_width: float) -> None:
    camera_component = getattr(actor, "camera_component", None) or getattr(
        actor,
        "cine_camera_component",
        None,
    )
    if camera_component is None:
        raise RuntimeError(f"Camera actor has no camera component: {actor}")
    camera_component.set_editor_property(
        "projection_mode",
        unreal.CameraProjectionMode.ORTHOGRAPHIC,
    )
    camera_component.set_editor_property("ortho_width", float(ortho_width))
    camera_component.set_editor_property("aspect_ratio", 16.0 / 9.0)
    camera_component.set_editor_property("constrain_aspect_ratio", True)


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
    values = (*location, *rotation, *scale)
    channels = section.get_all_channels()
    if len(channels) != len(values):
        raise RuntimeError(
            f"Expected 9 transform channels for {binding.get_display_name()}, got {len(channels)}"
        )
    for channel, value in zip(channels, values, strict=True):
        channel.set_default(float(value))


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
