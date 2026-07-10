from __future__ import annotations

import hashlib
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

    map_path = _create_empty_level(job)
    sequence, beauty_sequence = _create_sequences(job)
    _save_current_level(map_path)
    unreal.log(
        f"[UEF-RENDER-JOB] setup complete map={map_path} "
        f"sequence={sequence.get_path_name()} beauty_sequence={beauty_sequence.get_path_name()}"
    )


def _load_job() -> dict:
    job_file = os.environ.get("UEF_JOB_FILE")
    if not job_file:
        raise RuntimeError("UEF_JOB_FILE is not set")
    with Path(job_file).open("r", encoding="utf-8") as file:
        return json.load(file)


def _create_empty_level(job: dict) -> str:
    map_path = str(job["map_path"])
    if unreal.EditorAssetLibrary.does_asset_exist(
        map_path
    ) and not unreal.EditorAssetLibrary.delete_asset(map_path):
        raise RuntimeError(f"Could not replace render level: {map_path}")
    level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if level_editor is None:
        raise RuntimeError("LevelEditorSubsystem is unavailable")
    render_asset = job["asset"]
    if str(render_asset["kind"]) == "scene":
        source_map = str(render_asset["scene_map_path"])
        if not unreal.EditorAssetLibrary.does_asset_exist(source_map):
            raise RuntimeError(f"Could not load persistent scene map: {source_map}")
        unreal.EditorAssetLibrary.make_directory(map_path.rpartition("/")[0])
        if not level_editor.new_level_from_template(map_path, source_map):
            raise RuntimeError(f"Could not clone scene render level: {source_map} -> {map_path}")
        if not level_editor.load_level(map_path):
            raise RuntimeError(f"Could not load cloned scene render level: {map_path}")
        unreal.log(f"[UEF-RENDER-JOB] cloned scene level={source_map} -> {map_path}")
    else:
        if not level_editor.new_level(map_path, False):
            raise RuntimeError(f"Could not create empty render level: {map_path}")
        unreal.log(f"[UEF-RENDER-JOB] empty level={map_path}")
    return map_path


def _save_current_level(map_path: str) -> None:
    level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if level_editor is None or not level_editor.save_current_level():
        raise RuntimeError(f"Could not save render level: {map_path}")


def _save_asset(asset_path: str) -> None:
    if not unreal.EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False):
        raise RuntimeError(f"Could not save render asset: {asset_path}")


def _create_sequences(job: dict):
    run_id = str(job["run_id"])
    package_path = f"/Game/UEF/RenderJobs/{run_id}"
    unreal.EditorAssetLibrary.make_directory(package_path)
    lighting = job["lighting"]
    preset = str(lighting["preset"])
    materials = _create_materials(package_path, lighting_preset=preset)
    _create_object_mask_material(package_path)

    if str(job["asset"]["kind"]) == "scene":
        _prepare_scene_level(job)

    lighting_assets: dict[str, object] = {}
    if preset == "three_point":
        _add_three_point_lighting(job)
    elif preset == "hdri":
        texture = _import_hdri_texture(package_path, str(lighting["hdri_file"]))
        lighting_assets = {
            "texture": texture,
            "backdrop_material": _create_hdri_backdrop_material(package_path, texture),
        }
    elif preset == "none":
        unreal.log("[UEF-RENDER-JOB] lighting preset none: no lights spawned")
    else:
        raise RuntimeError(f"Unsupported lighting preset: {preset}")

    sequence = _create_sequence_asset(
        job,
        package_path=package_path,
        asset_name=f"UEF_RenderJob_{run_id}",
        materials=materials,
        lighting_assets=lighting_assets,
        include_hdri_backdrop=False,
    )
    if preset == "hdri":
        beauty_sequence = _create_sequence_asset(
            job,
            package_path=package_path,
            asset_name=f"UEF_RenderJobBeauty_{run_id}",
            materials=materials,
            lighting_assets=lighting_assets,
            include_hdri_backdrop=True,
        )
    else:
        beauty_sequence = sequence
    expected_paths = {
        "data": str(job["sequence_path"]),
        "beauty": str(job.get("beauty_sequence_path", job["sequence_path"])),
    }
    actual_paths = {
        "data": sequence.get_path_name(),
        "beauty": beauty_sequence.get_path_name(),
    }
    if actual_paths != expected_paths:
        raise RuntimeError(
            f"Render sequence path mismatch: expected={expected_paths} actual={actual_paths}"
        )
    return sequence, beauty_sequence


def _create_sequence_asset(
    job: dict,
    *,
    package_path: str,
    asset_name: str,
    materials: dict[str, object],
    lighting_assets: dict[str, object],
    include_hdri_backdrop: bool,
):
    asset_path = f"{package_path}/{asset_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        unreal.EditorAssetLibrary.delete_asset(asset_path)
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

    render_asset = job["asset"]
    if str(render_asset["kind"]) != "scene":
        is_builtin = str(render_asset["kind"]) == "builtin"
        _add_static_mesh_spawnable(
            sequence,
            frames=frames,
            label="UEF_Job_Cube" if is_builtin else "UEF_Job_Asset",
            mesh_path=str(render_asset["mesh_path"]),
            material=materials["cube"] if not render_asset["preserve_materials"] else None,
            location=tuple(float(value) for value in render_asset["actor_location_cm"]),
            rotation=(0, 0, 0),
            scale=tuple(float(value) for value in render_asset.get("actor_scale", [1.0] * 3)),
            stencil_value=1,
            expected_bounds=render_asset["bounds_cm"],
        )
        _add_static_mesh_spawnable(
            sequence,
            frames=frames,
            label="UEF_Job_Floor",
            mesh_path="/Engine/BasicShapes/Cube",
            material=materials["floor"],
            location=(0, 0, float(render_asset["floor_location_z_cm"])),
            rotation=(0, 0, 0),
            scale=(
                float(render_asset["floor_scale_xy"]),
                float(render_asset["floor_scale_xy"]),
                0.05,
            ),
            stencil_value=2,
            expected_bounds=None,
        )
    _configure_sequence_lighting(
        sequence,
        job,
        lighting_assets=lighting_assets,
        include_hdri_backdrop=include_hdri_backdrop,
    )
    _add_orbit_camera(sequence, job)

    _save_asset(asset_path)
    unreal.log(f"[UEF-RENDER-JOB] sequence={asset_path}.{asset_name}")
    return sequence


def _prepare_scene_level(job: dict) -> None:
    render_asset = job["asset"]
    if render_asset.get("no_auto_floor") is not True:
        raise RuntimeError("Scene render requires no_auto_floor=true")
    expected_inventory = render_asset.get("render_inventory")
    expected_digest = render_asset.get("render_inventory_sha256")
    if not isinstance(expected_inventory, dict) or not isinstance(expected_digest, str):
        raise RuntimeError("Scene render is missing its approved actor/component inventory")
    if _canonical_digest(expected_inventory) != expected_digest:
        raise RuntimeError("Scene render actor/component inventory digest mismatch")
    actual_inventory, actors_by_name = _current_scene_render_inventory()
    if actual_inventory != expected_inventory:
        raise RuntimeError("Persistent scene actor/component inventory changed before render")

    expected_actors = int(render_asset["static_mesh_actor_count"])
    expected_components = int(render_asset["static_mesh_component_count"])
    expected_stencil_ids = render_asset.get("expected_object_stencil_ids")
    if (
        expected_inventory.get("static_mesh_actor_count") != expected_actors
        or expected_inventory.get("static_mesh_component_count") != expected_components
        or not 1 <= expected_actors <= 255
        or expected_stencil_ids != list(range(1, expected_actors + 1))
    ):
        raise RuntimeError("Scene render has an invalid actor/component stencil contract")
    stencil_assignments = []
    static_actor_rows = sorted(
        (row for row in actual_inventory["actors"] if row["components"]),
        key=lambda row: row["actor_name"],
    )
    for stencil_id, row in enumerate(static_actor_rows, start=1):
        actor_name = row["actor_name"]
        actor = actors_by_name[actor_name]
        assigned_components = 0
        for component in actor.get_components_by_class(unreal.StaticMeshComponent):
            if component.get_editor_property("static_mesh") is None:
                continue
            component.set_render_custom_depth(True)
            component.set_custom_depth_stencil_value(stencil_id)
            assigned_components += 1
        stencil_assignments.append(
            {
                "actor_name": actor_name,
                "stencil_id": stencil_id,
                "component_count": assigned_components,
            }
        )
    unreal.log(
        f"[UEF-RENDER-JOB] scene foreground actors={expected_actors} "
        f"components={expected_components} auto_floor=false stencils="
        + json.dumps(stencil_assignments, sort_keys=True)
    )


def _canonical_digest(value) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_number(value, digits=6):
    result = float(value)
    if not math.isfinite(result):
        raise RuntimeError(f"scene render inventory contains a non-finite value: {result}")
    result = round(result, digits)
    return 0.0 if result == 0.0 else result


def _vector(value, digits=6):
    return [
        _stable_number(value.x, digits),
        _stable_number(value.y, digits),
        _stable_number(value.z, digits),
    ]


def _scene_actor_transform(actor):
    value = actor.get_actor_transform()
    rotation = value.rotation.rotator()
    return {
        "translation_cm": _vector(value.translation),
        "rotation_deg": [
            _stable_number(rotation.roll),
            _stable_number(rotation.pitch),
            _stable_number(rotation.yaw),
        ],
        "scale": _vector(value.scale3d),
    }


def _scene_component_payload(component):
    mesh = component.get_editor_property("static_mesh")
    if mesh is None:
        return None
    origin, extent, _ = unreal.SystemLibrary.get_component_bounds(component)
    low = origin - extent
    high = origin + extent
    materials = []
    for index in range(int(component.get_num_materials())):
        material = component.get_material(index)
        materials.append(None if material is None else str(material.get_path_name()))
    return {
        "name": str(component.get_name()),
        "mesh_path": str(mesh.get_path_name()),
        "materials": materials,
        "world_bounds_cm": {
            "min": _vector(low),
            "max": _vector(high),
            "size": _vector(high - low),
        },
    }


def _current_scene_render_inventory():
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    if actor_subsystem is None:
        raise RuntimeError("EditorActorSubsystem is unavailable")
    actors = []
    actors_by_name = {}
    static_mesh_actor_count = 0
    static_mesh_component_count = 0
    for actor in actor_subsystem.get_all_level_actors():
        actor_name = str(actor.get_name())
        if actor_name in actors_by_name:
            raise RuntimeError(f"Persistent scene has duplicate actor name: {actor_name}")
        actors_by_name[actor_name] = actor
        component_rows = []
        for component in actor.get_components_by_class(unreal.StaticMeshComponent):
            component_row = _scene_component_payload(component)
            if component_row is None:
                continue
            component_rows.append(component_row)
            static_mesh_component_count += 1
        if component_rows:
            static_mesh_actor_count += 1
        parent = actor.get_attach_parent_actor()
        actors.append(
            {
                "object_id": actor_name,
                "actor_name": actor_name,
                "actor_label": str(actor.get_actor_label()),
                "actor_class": str(actor.get_class().get_name()),
                "parent_actor_name": None if parent is None else str(parent.get_name()),
                "transform": _scene_actor_transform(actor),
                "components": sorted(
                    component_rows,
                    key=lambda item: (item["mesh_path"], item["name"]),
                ),
            }
        )
    actors.sort(key=lambda item: item["object_id"])
    return (
        {
            "schema_version": 1,
            "actors": actors,
            "static_mesh_actor_count": static_mesh_actor_count,
            "static_mesh_component_count": static_mesh_component_count,
        },
        actors_by_name,
    )


def _create_materials(package_path: str, *, lighting_preset: str) -> dict[str, object]:
    cube_emissive = 10.0 if lighting_preset == "none" else 0.0
    return {
        "cube": _create_lit_material(
            package_path,
            "UEF_Job_Cube_Mat",
            (0.8, 0.18, 0.08),
            emissive_strength=cube_emissive,
        ),
        "floor": _create_lit_material(
            package_path,
            "UEF_Job_Floor_Mat",
            (0.42, 0.46, 0.50),
            emissive_strength=0.0,
        ),
    }


def _create_lit_material(
    package_path: str,
    asset_name: str,
    color: tuple[float, float, float],
    *,
    emissive_strength: float,
):
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
    material.set_editor_property(
        "shading_model",
        unreal.MaterialShadingModel.MSM_UNLIT
        if emissive_strength > 0
        else unreal.MaterialShadingModel.MSM_DEFAULT_LIT,
    )
    material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_OPAQUE)
    color_node = unreal.MaterialEditingLibrary.create_material_expression(
        material,
        unreal.MaterialExpressionConstant3Vector,
        -400,
        0,
    )
    color_node.set_editor_property("constant", unreal.LinearColor(*color, 1.0))
    if emissive_strength > 0:
        emissive_color = unreal.MaterialEditingLibrary.create_material_expression(
            material,
            unreal.MaterialExpressionConstant3Vector,
            -400,
            180,
        )
        emissive_color.set_editor_property(
            "constant",
            unreal.LinearColor(
                color[0] * emissive_strength,
                color[1] * emissive_strength,
                color[2] * emissive_strength,
                1.0,
            ),
        )
        unreal.MaterialEditingLibrary.connect_material_property(
            emissive_color,
            "",
            unreal.MaterialProperty.MP_EMISSIVE_COLOR,
        )
    else:
        unreal.MaterialEditingLibrary.connect_material_property(
            color_node,
            "",
            unreal.MaterialProperty.MP_BASE_COLOR,
        )
    unreal.MaterialEditingLibrary.recompile_material(material)
    _save_asset(asset_path)
    return material


def _configure_sequence_lighting(
    sequence,
    job: dict,
    *,
    lighting_assets: dict[str, object],
    include_hdri_backdrop: bool,
) -> None:
    preset = str(job["lighting"]["preset"])
    frames = int(job["frames"])
    if preset == "hdri":
        texture = lighting_assets["texture"]
        _add_hdri_lighting(sequence, frames=frames, texture=texture)
        if include_hdri_backdrop:
            _add_static_mesh_spawnable(
                sequence,
                frames=frames,
                label="UEF_Job_HDRIBackdrop",
                mesh_path="/HDRIBackdrop/Meshes/EnviroDome",
                material=lighting_assets["backdrop_material"],
                location=(0, 0, 0),
                rotation=(0, 0, 0),
                scale=(100.0, 100.0, 100.0),
                stencil_value=None,
                cast_shadow=False,
                expected_bounds=None,
            )


def _add_three_point_lighting(job: dict) -> None:
    # Fixed lights belong to the level instead of the sequence. Sequencer assigns
    # spawnables random binding GUIDs, which can change equal-type light registration
    # order and produce one-to-three-code-value FP16 accumulation differences.
    multiplier = float(job.get("asset", {}).get("lighting_intensity_multiplier", 1.0))
    if not math.isfinite(multiplier) or not 0.1 <= multiplier <= 100.0:
        raise RuntimeError(f"Invalid three-point lighting intensity multiplier: {multiplier}")
    _add_directional_light_actor(
        label="UEF_Job_KeyLight",
        location=(-250, -350, 500),
        rotation=(-50, 35, 0),
        intensity=8.0 * multiplier,
    )
    _add_directional_light_actor(
        label="UEF_Job_FillLight",
        location=(350, 250, 350),
        rotation=(-35, -85, 0),
        intensity=2.0 * multiplier,
    )
    _add_directional_light_actor(
        label="UEF_Job_RimLight",
        location=(250, 350, 450),
        rotation=(-25, 155, 0),
        intensity=3.0 * multiplier,
    )


def _add_hdri_lighting(sequence, *, frames: int, texture) -> None:
    _add_sky_light_spawnable(
        sequence,
        frames=frames,
        label="UEF_Job_HDRISkyLight",
        location=(0, 0, 250),
        rotation=(0, 0, 0),
        intensity=2.5,
        cubemap=texture,
    )


def _import_hdri_texture(package_path: str, hdri_file: str):
    source = Path(hdri_file)
    if not source.exists():
        raise RuntimeError(f"HDRI file not found: {source}")
    destination_path = f"{package_path}/Lighting"
    asset_name = source.stem
    asset_path = f"{destination_path}/{asset_name}"
    unreal.EditorAssetLibrary.make_directory(destination_path)
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        unreal.EditorAssetLibrary.delete_asset(asset_path)
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", str(source))
    task.set_editor_property("destination_path", destination_path)
    task.set_editor_property("destination_name", asset_name)
    task.set_editor_property("automated", True)
    task.set_editor_property("save", True)
    task.set_editor_property("replace_existing", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    texture = unreal.load_asset(asset_path)
    if texture is None:
        raise RuntimeError(f"Could not import HDRI texture: {source}")
    if texture.get_class().get_name() != "TextureCube":
        raise RuntimeError(f"HDRI import did not produce TextureCube: {texture.get_path_name()}")
    _save_asset(asset_path)
    return texture


def _create_hdri_backdrop_material(package_path: str, texture):
    asset_name = "UEF_HDRIBackdrop_Mat"
    asset_path = f"{package_path}/{asset_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        unreal.EditorAssetLibrary.delete_asset(asset_path)
    material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        asset_name,
        package_path,
        unreal.MaterialInstanceConstant,
        unreal.MaterialInstanceConstantFactoryNew(),
    )
    if material is None:
        raise RuntimeError(f"Could not create HDRI backdrop material: {asset_path}")
    parent = unreal.load_asset("/HDRIBackdrop/Materials/MI_HDRI_Sky.MI_HDRI_Sky")
    if parent is None:
        raise RuntimeError("Could not load HDRIBackdrop sky material")
    unreal.MaterialEditingLibrary.set_material_instance_parent(material, parent)
    # UE 5.5's MaterialEditingLibrary setters mutate correctly but always return false.
    # Verify the readback instead of trusting that broken return value.
    unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(
        material,
        unreal.Name("HDRI_Map"),
        texture,
    )
    unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(
        material,
        unreal.Name("Intensity"),
        1.0,
    )
    unreal.MaterialEditingLibrary.update_material_instance(material)
    actual_texture = unreal.MaterialEditingLibrary.get_material_instance_texture_parameter_value(
        material,
        unreal.Name("HDRI_Map"),
    )
    actual_intensity = unreal.MaterialEditingLibrary.get_material_instance_scalar_parameter_value(
        material,
        unreal.Name("Intensity"),
    )
    if actual_texture is None or actual_texture.get_path_name() != texture.get_path_name():
        raise RuntimeError("Could not set HDRI_Map on backdrop material")
    if not math.isclose(float(actual_intensity), 1.0, rel_tol=0.0, abs_tol=1e-6):
        raise RuntimeError("Could not set Intensity on backdrop material")
    _save_asset(asset_path)
    return material


def _create_object_mask_material(package_path: str):
    asset_name = "UEF_ObjectMask_Mat"
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
        raise RuntimeError(f"Could not create object mask material: {asset_path}")
    material.set_editor_property("material_domain", unreal.MaterialDomain.MD_POST_PROCESS)
    material.set_editor_property(
        "blendable_location",
        unreal.BlendableLocation.BL_REPLACING_TONEMAPPER,
    )
    stencil_node = unreal.MaterialEditingLibrary.create_material_expression(
        material,
        unreal.MaterialExpressionSceneTexture,
        -400,
        0,
    )
    stencil_node.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_CUSTOM_STENCIL)
    divide_node = unreal.MaterialEditingLibrary.create_material_expression(
        material,
        unreal.MaterialExpressionDivide,
        -180,
        0,
    )
    divisor = unreal.MaterialEditingLibrary.create_material_expression(
        material,
        unreal.MaterialExpressionConstant,
        -360,
        180,
    )
    divisor.set_editor_property("r", 255.0)
    unreal.MaterialEditingLibrary.connect_material_expressions(stencil_node, "", divide_node, "A")
    unreal.MaterialEditingLibrary.connect_material_expressions(divisor, "", divide_node, "B")
    unreal.MaterialEditingLibrary.connect_material_property(
        divide_node,
        "",
        unreal.MaterialProperty.MP_EMISSIVE_COLOR,
    )
    unreal.MaterialEditingLibrary.recompile_material(material)
    _save_asset(asset_path)
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
    stencil_value: int | None,
    cast_shadow: bool = True,
    tags: tuple[str, ...] = (),
    expected_bounds=None,
):
    binding = sequence.add_spawnable_from_class(unreal.StaticMeshActor)
    binding.set_display_name(label)
    actor = _object_template(binding)
    actor.set_actor_label(label)
    actor.set_editor_property("tags", [unreal.Name(tag) for tag in tags])
    _set_transform(actor, location, rotation, scale)
    _add_transform_track(binding, frames=frames, location=location, rotation=rotation, scale=scale)
    _set_movable(actor)
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    if mesh is None or not isinstance(mesh, unreal.StaticMesh):
        raise RuntimeError(f"Could not load mesh: {mesh_path}")
    if expected_bounds is not None:
        _validate_mesh_bounds(mesh, expected_bounds)
    component = actor.static_mesh_component
    component.set_static_mesh(mesh)
    component.set_editor_property("cast_shadow", bool(cast_shadow))
    if stencil_value is None:
        component.set_render_custom_depth(False)
    else:
        component.set_render_custom_depth(True)
        component.set_custom_depth_stencil_value(int(stencil_value))
    if material is not None:
        material_slots = max(1, int(component.get_num_materials()))
        for material_index in range(material_slots):
            component.set_material(material_index, material)
        component.set_editor_property("override_materials", [material] * material_slots)
    return binding


def _validate_mesh_bounds(mesh, expected_bounds) -> None:
    box = mesh.get_bounding_box()
    actual = {
        "min": [float(box.min.x), float(box.min.y), float(box.min.z)],
        "max": [float(box.max.x), float(box.max.y), float(box.max.z)],
        "size": [
            float(box.max.x - box.min.x),
            float(box.max.y - box.min.y),
            float(box.max.z - box.min.z),
        ],
    }
    for key in ("min", "max", "size"):
        expected = expected_bounds[key]
        if len(expected) != 3:
            raise RuntimeError(f"Expected mesh bounds {key} must have three values")
        for axis, (actual_value, expected_value) in enumerate(
            zip(actual[key], expected, strict=True)
        ):
            if not math.isclose(
                actual_value,
                float(expected_value),
                rel_tol=1e-6,
                abs_tol=1e-4,
            ):
                raise RuntimeError(
                    f"StaticMesh bounds changed for {mesh.get_path_name()}: "
                    f"{key}[{axis}] {actual_value} != {expected_value}"
                )


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


def _add_directional_light_actor(
    *,
    label: str,
    location: tuple[float, float, float],
    rotation: tuple[float, float, float],
    intensity: float,
):
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    if actor_subsystem is None:
        raise RuntimeError("EditorActorSubsystem is unavailable")
    actor = actor_subsystem.spawn_actor_from_class(
        actor_class=unreal.DirectionalLight,
        location=unreal.Vector(*location),
        rotation=unreal.Rotator(
            pitch=rotation[0],
            yaw=rotation[1],
            roll=rotation[2],
        ),
        transient=False,
    )
    if actor is None:
        raise RuntimeError(f"Could not spawn persistent directional light: {label}")
    actor.set_actor_label(label)
    _set_transform(actor, location, rotation, (1, 1, 1))
    _set_movable(actor)
    actor.light_component.set_editor_property("intensity", intensity)
    actor.light_component.set_editor_property("cast_shadows", False)
    return actor


def _add_sky_light_spawnable(
    sequence,
    *,
    frames: int,
    label: str,
    location: tuple[float, float, float],
    rotation: tuple[float, float, float],
    intensity: float,
    cubemap=None,
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
    if cubemap is not None:
        actor.light_component.set_editor_property(
            "source_type",
            unreal.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP,
        )
        actor.light_component.set_editor_property("cubemap", cubemap)
        actor.light_component.set_editor_property("lower_hemisphere_is_black", False)
        actor.light_component.set_editor_property("real_time_capture", False)
    return binding


def _add_orbit_camera(sequence, job: dict) -> None:
    camera = job["camera"]
    render_asset = job["asset"]
    views = int(camera["views"])
    elevation = float(render_asset.get("camera_elevation_deg", camera["elevation_deg"]))
    azimuth_offset = float(render_asset.get("camera_azimuth_offset_deg", 0.0))
    if not math.isfinite(elevation) or not -89.0 <= elevation <= 89.0:
        raise RuntimeError(f"Invalid orbit camera elevation: {elevation}")
    if not math.isfinite(azimuth_offset):
        raise RuntimeError(f"Invalid orbit camera azimuth offset: {azimuth_offset}")
    fov = float(camera["fov"])
    radius = float(render_asset["camera_radius_cm"])
    target = tuple(float(value) for value in render_asset["camera_target_cm"])
    custom_near_clip_cm = render_asset.get("camera_near_clip_cm")
    first_location, first_rotation = _orbit_camera_transform(
        radius,
        azimuth_offset,
        elevation,
        target,
    )
    binding = _add_camera_spawnable(
        sequence,
        frames=views,
        label="UEF_Job_Camera",
        location=first_location,
        rotation=first_rotation,
        fov=fov,
        aspect_ratio=float(camera["resolution"][0]) / float(camera["resolution"][1]),
        custom_near_clip_cm=(None if custom_near_clip_cm is None else float(custom_near_clip_cm)),
    )
    _add_orbit_transform_keys(
        binding,
        views=views,
        radius=radius,
        elevation=elevation,
        azimuth_offset=azimuth_offset,
        target=target,
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
    azimuth_offset: float,
    target: tuple[float, float, float],
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
        azimuth = azimuth_offset + 360.0 * view_index / views
        location, rotation = _orbit_camera_transform(radius, azimuth, elevation, target)
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
    target: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    azimuth_rad = math.radians(azimuth_deg)
    elevation_rad = math.radians(elevation_deg)
    horizontal = radius * math.cos(elevation_rad)
    location = (
        target[0] - horizontal * math.cos(azimuth_rad),
        target[1] - horizontal * math.sin(azimuth_rad),
        target[2] + radius * math.sin(elevation_rad),
    )
    look_at = unreal.MathLibrary.find_look_at_rotation(
        unreal.Vector(*location),
        unreal.Vector(*target),
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
    aspect_ratio: float,
    custom_near_clip_cm: float | None,
):
    binding = sequence.add_spawnable_from_class(unreal.CineCameraActor)
    binding.set_display_name(label)
    actor = _object_template(binding)
    actor.set_actor_label(label)
    _set_transform(actor, location, rotation, (1, 1, 1))
    _configure_perspective_camera(actor, fov, aspect_ratio, custom_near_clip_cm)
    _add_transform_track(
        binding, frames=frames, location=location, rotation=rotation, scale=(1, 1, 1)
    )
    _set_movable(actor)
    return binding


def _configure_perspective_camera(
    actor,
    fov: float,
    aspect_ratio: float,
    custom_near_clip_cm: float | None,
) -> None:
    camera_component = getattr(actor, "camera_component", None) or getattr(
        actor,
        "cine_camera_component",
        None,
    )
    if camera_component is None:
        raise RuntimeError(f"Camera actor has no camera component: {actor}")
    camera_component.set_editor_property("projection_mode", unreal.CameraProjectionMode.PERSPECTIVE)
    if custom_near_clip_cm is not None:
        if not math.isfinite(custom_near_clip_cm) or custom_near_clip_cm <= 0.0:
            raise RuntimeError(f"Invalid custom camera near clip: {custom_near_clip_cm}")
        camera_component.set_editor_property("override_custom_near_clipping_plane", True)
        camera_component.set_editor_property(
            "custom_near_clipping_plane",
            float(custom_near_clip_cm),
        )
        actual_override = bool(
            camera_component.get_editor_property("override_custom_near_clipping_plane")
        )
        actual_near_clip = float(camera_component.get_editor_property("custom_near_clipping_plane"))
        if not actual_override or not math.isclose(
            actual_near_clip,
            custom_near_clip_cm,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise RuntimeError(
                "Could not apply catalog camera custom near clip: "
                f"requested={custom_near_clip_cm} actual={actual_near_clip} "
                f"override={actual_override}"
            )
    filmback = camera_component.get_editor_property("filmback")
    current_aspect = float(filmback.get_editor_property("sensor_aspect_ratio"))
    if not math.isclose(current_aspect, aspect_ratio, rel_tol=0.0, abs_tol=1e-6):
        sensor_width = float(filmback.get_editor_property("sensor_width"))
        filmback.set_editor_property("sensor_height", sensor_width / aspect_ratio)
        camera_component.set_editor_property("filmback", filmback)
        camera_component.set_editor_property("aspect_ratio", float(aspect_ratio))
        camera_component.set_editor_property("constrain_aspect_ratio", True)
    try:
        camera_component.set_field_of_view(float(fov))
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
    actor.set_actor_rotation(
        unreal.Rotator(
            pitch=rotation[0],
            yaw=rotation[1],
            roll=rotation[2],
        ),
        False,
    )
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
