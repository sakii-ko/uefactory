import json
import math
import os
import re
import traceback
from pathlib import Path

import unreal

INGEST_ROOT = "/Game/UEF/Ingested"
TRANSACTION_ROOT = "/Game/UEF/IngestTransactions"
SUPPORTED_EXTENSIONS = {".fbx", ".gltf", ".glb"}
ASSET_ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
IMPORT_BACKEND = "asset_tools_auto"
NORMALIZATION_METADATA = {
    "target_units": "centimeters",
    "target_up_axis": "Z",
    "target_handedness": "left_handed",
    "source_conversion": "delegated_to_engine_importer",
    "package_pivot_policy": "preserve",
    "uniform_scale": 1.0,
}
FBX_PBR_POSTPROCESS_POLICY = "fbx_filename_pbr_v2"
FBX_GLASS_OVERRIDE_POLICY = "glass_translucent_v1"
FBX_GLASS_OPACITY = 0.12
ASSET_PAYLOAD_KEYS = (
    "import_backend",
    "normalization",
    "material_postprocess",
    "imported_object_paths",
    "imported_objects",
    "object_count",
    "static_mesh_count",
    "material_count",
    "texture_count",
    "static_meshes",
)


def _write_manifest(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _vector_payload(vector):
    return [float(vector.x), float(vector.y), float(vector.z)]


def _mesh_counts(mesh):
    mesh_description = mesh.get_static_mesh_description(0)
    if mesh_description is None:
        raise RuntimeError(f"could not read LOD0 mesh description: {mesh.get_path_name()}")
    return {
        "lod_count": int(mesh.get_num_lods()),
        "triangle_count": int(mesh_description.get_triangle_count()),
        "render_fallback_triangle_count": int(mesh.get_num_triangles(0)),
        "vertex_count": int(mesh_description.get_vertex_count()),
    }


def _mesh_bounds(mesh):
    if hasattr(mesh, "get_bounding_box"):
        box = mesh.get_bounding_box()
        minimum = box.min
        maximum = box.max
        return {
            "min": _vector_payload(minimum),
            "max": _vector_payload(maximum),
            "size": _vector_payload(maximum - minimum),
        }
    bounds = mesh.get_bounds()
    origin = bounds.origin
    extent = bounds.box_extent
    return {
        "min": _vector_payload(origin - extent),
        "max": _vector_payload(origin + extent),
        "size": _vector_payload(extent * 2.0),
    }


def _stable_asset_path(asset, label):
    object_path = str(asset.get_path_name())
    if not object_path.startswith("/") or "." not in object_path:
        raise RuntimeError(f"{label} has an unstable object path: {object_path!r}")
    reloaded = unreal.EditorAssetLibrary.load_asset(object_path)
    if reloaded is None:
        raise RuntimeError(f"could not reload {label}: {object_path}")
    reloaded_path = str(reloaded.get_path_name())
    if reloaded_path != object_path:
        raise RuntimeError(
            f"{label} object path changed after reload: {object_path} -> {reloaded_path}"
        )
    return object_path


def _material_properties():
    return (
        ("base_color", unreal.MaterialProperty.MP_BASE_COLOR),
        ("metallic", unreal.MaterialProperty.MP_METALLIC),
        ("roughness", unreal.MaterialProperty.MP_ROUGHNESS),
        ("normal", unreal.MaterialProperty.MP_NORMAL),
    )


def _connected_property_textures(material, material_property):
    root = unreal.MaterialEditingLibrary.get_material_property_input_node(
        material,
        material_property,
    )
    if root is None:
        return []
    textures = []
    pending = [root]
    visited = set()
    while pending:
        expression = pending.pop()
        expression_key = str(expression.get_path_name())
        if expression_key in visited:
            continue
        visited.add(expression_key)
        texture = getattr(expression, "texture", None)
        if texture is not None:
            if not isinstance(texture, unreal.Texture):
                raise RuntimeError(
                    "material expression texture property is not a Texture: "
                    f"{material.get_path_name()} {expression} {texture}"
                )
            textures.append(texture)
        inputs = unreal.MaterialEditingLibrary.get_inputs_for_material_expression(
            material,
            expression,
        )
        if inputs:
            pending.extend(item for item in inputs if item is not None)
    return textures


def _base_material_texture_bindings(material):
    if not isinstance(material, unreal.Material):
        raise RuntimeError(
            "UE 5.5 material-graph inspection requires a base Material; "
            f"got {material.get_class().get_name()}: {material.get_path_name()}"
        )
    bindings = []
    for role, material_property in _material_properties():
        for texture in _connected_property_textures(material, material_property):
            bindings.append((role, texture))
    return bindings


def _base_material_used_textures(material):
    return [texture for _, texture in _base_material_texture_bindings(material)]


def _material_instance_used_textures(material):
    base_material = material.get_base_material()
    if base_material is None:
        raise RuntimeError(
            f"material instance has no loadable base material: {material.get_path_name()}"
        )
    _stable_asset_path(base_material, "base material")
    base_textures = _base_material_used_textures(base_material)
    parameter_names = list(unreal.MaterialEditingLibrary.get_texture_parameter_names(material))

    default_parameter_paths = set()
    effective_parameter_textures = []
    for parameter_name in parameter_names:
        default_texture = (
            unreal.MaterialEditingLibrary.get_material_default_texture_parameter_value(
                base_material,
                parameter_name,
            )
        )
        if default_texture is not None:
            default_parameter_paths.add(str(default_texture.get_path_name()))
        current_texture = (
            unreal.MaterialEditingLibrary.get_material_instance_texture_parameter_value(
                material,
                parameter_name,
            )
        )
        if current_texture is not None:
            effective_parameter_textures.append(current_texture)

    non_parameter_textures = [
        texture
        for texture in base_textures
        if str(texture.get_path_name()) not in default_parameter_paths
    ]
    return [*non_parameter_textures, *effective_parameter_textures]


def _used_texture_paths(material):
    if isinstance(material, unreal.Material):
        textures = _base_material_used_textures(material)
    elif isinstance(material, unreal.MaterialInstanceConstant):
        textures = _material_instance_used_textures(material)
    else:
        raise RuntimeError(
            "cannot prove the used-texture inventory for unsupported material class "
            f"{material.get_class().get_name()}: {material.get_path_name()}"
        )
    return sorted({_stable_asset_path(texture, "used texture") for texture in textures})


def _texture_role(texture_name):
    normalized = texture_name.lower()
    markers = (
        ("normal", "opengl", ("_nor_gl_", "_normal_gl_")),
        ("normal", "directx", ("_nor_dx_", "_normal_dx_")),
        ("base_color", None, ("_diff_", "_basecolor_", "_base_color_", "_albedo_")),
        ("roughness", None, ("_rough_", "_roughness_")),
        ("metallic", None, ("_metal_", "_metallic_")),
    )
    for role, normal_space, candidates in markers:
        if any(marker in normalized for marker in candidates):
            return role, normal_space
    return None


def _referenced_base_materials(loaded_assets):
    materials = set()
    for asset in loaded_assets:
        if not isinstance(asset, unreal.StaticMesh):
            continue
        for slot in asset.get_editor_property("static_materials"):
            material = slot.get_editor_property("material_interface")
            if isinstance(material, unreal.Material):
                materials.add(material)
    return sorted(materials, key=lambda item: item.get_path_name())


def _is_glass_material(material):
    name = material.get_name().lower()
    return name == "glass" or name.endswith("_glass")


def _fbx_material_texture_map(loaded_assets):
    materials = _referenced_base_materials(loaded_assets)
    textures = [asset for asset in loaded_assets if isinstance(asset, unreal.Texture)]
    result = {material: {} for material in materials}
    for texture in textures:
        classification = _texture_role(texture.get_name())
        if classification is None:
            continue
        role, normal_space = classification
        texture_name = texture.get_name().lower()
        candidates = [
            material
            for material in materials
            if texture_name.startswith(material.get_name().lower() + "_")
        ]
        if not candidates:
            raise RuntimeError(
                f"could not map FBX {role} texture to a material: {texture.get_path_name()}"
            )
        material = max(candidates, key=lambda item: len(item.get_name()))
        previous = result[material].get(role)
        if previous is not None:
            raise RuntimeError(
                f"ambiguous FBX {role} textures for {material.get_path_name()}: "
                f"{previous.get_path_name()}, {texture.get_path_name()}"
            )
        result[material][role] = (texture, normal_space)
    return result


def _configure_pbr_texture(texture, role, normal_space):
    if role == "base_color":
        texture.set_editor_property("srgb", True)
        texture.set_editor_property(
            "compression_settings",
            unreal.TextureCompressionSettings.TC_DEFAULT,
        )
    elif role == "normal":
        texture.set_editor_property("srgb", False)
        texture.set_editor_property(
            "compression_settings",
            unreal.TextureCompressionSettings.TC_NORMALMAP,
        )
        texture.set_editor_property("flip_green_channel", normal_space == "opengl")
    elif role in {"roughness", "metallic"}:
        texture.set_editor_property("srgb", False)
        texture.set_editor_property(
            "compression_settings",
            unreal.TextureCompressionSettings.TC_MASKS,
        )
    else:
        raise RuntimeError(f"unsupported PBR texture role: {role}")


def _apply_fbx_pbr_postprocess(loaded_assets):
    material_map = _fbx_material_texture_map(loaded_assets)
    property_by_role = dict(_material_properties())
    positions = {
        "base_color": (-480, -180),
        "normal": (-480, 40),
        "roughness": (-480, 240),
        "metallic": (-480, 440),
    }
    sampler_types = {
        "base_color": unreal.MaterialSamplerType.SAMPLERTYPE_COLOR,
        "normal": unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL,
        "roughness": unreal.MaterialSamplerType.SAMPLERTYPE_MASKS,
        "metallic": unreal.MaterialSamplerType.SAMPLERTYPE_MASKS,
    }
    for material, bindings in material_map.items():
        if not bindings:
            if _is_glass_material(material):
                material.set_editor_property(
                    "blend_mode",
                    unreal.BlendMode.BLEND_TRANSLUCENT,
                )
                opacity = unreal.MaterialEditingLibrary.create_material_expression(
                    material,
                    unreal.MaterialExpressionConstant,
                    -480,
                    0,
                )
                if opacity is None:
                    raise RuntimeError(
                        f"could not create glass opacity for {material.get_path_name()}"
                    )
                opacity.set_editor_property("r", FBX_GLASS_OPACITY)
                if not unreal.MaterialEditingLibrary.connect_material_property(
                    opacity,
                    "",
                    unreal.MaterialProperty.MP_OPACITY,
                ):
                    raise RuntimeError(
                        f"could not connect glass opacity for {material.get_path_name()}"
                    )
                unreal.MaterialEditingLibrary.recompile_material(material)
            continue
        if not {"base_color", "normal", "roughness"}.issubset(bindings):
            raise RuntimeError(
                "FBX PBR material is missing a required binding: "
                f"{material.get_path_name()} roles={sorted(bindings)}"
            )
        for role in ("base_color", "normal", "roughness", "metallic"):
            binding = bindings.get(role)
            if binding is None:
                if role != "metallic":
                    continue
                expression = unreal.MaterialEditingLibrary.create_material_expression(
                    material,
                    unreal.MaterialExpressionConstant,
                    *positions[role],
                )
                if expression is None:
                    raise RuntimeError(
                        f"could not create metallic default for {material.get_path_name()}"
                    )
                expression.set_editor_property("r", 0.0)
                output_name = ""
            else:
                texture, normal_space = binding
                _configure_pbr_texture(texture, role, normal_space)
                x, y = positions[role]
                expression = unreal.MaterialEditingLibrary.create_material_expression(
                    material,
                    unreal.MaterialExpressionTextureSampleParameter2D,
                    x,
                    y,
                )
                if expression is None:
                    raise RuntimeError(
                        f"could not create {role} texture sample for {material.get_path_name()}"
                    )
                expression.set_editor_property("parameter_name", f"UEF_{role}")
                expression.set_editor_property("texture", texture)
                expression.set_editor_property("sampler_type", sampler_types[role])
                output_name = "RGB" if role in {"base_color", "normal"} else "R"
            if not unreal.MaterialEditingLibrary.connect_material_property(
                expression,
                output_name,
                property_by_role[role],
            ):
                raise RuntimeError(f"could not connect {role} for {material.get_path_name()}")
        unreal.MaterialEditingLibrary.recompile_material(material)


def _glass_override_payload(material, expected_bindings):
    if expected_bindings or not _is_glass_material(material):
        return None
    if material.get_editor_property("blend_mode") != unreal.BlendMode.BLEND_TRANSLUCENT:
        raise RuntimeError(f"FBX glass material is not translucent: {material.get_path_name()}")
    opacity_root = unreal.MaterialEditingLibrary.get_material_property_input_node(
        material,
        unreal.MaterialProperty.MP_OPACITY,
    )
    if opacity_root is None or getattr(opacity_root, "texture", None) is not None:
        raise RuntimeError(f"FBX glass material has no scalar opacity: {material.get_path_name()}")
    try:
        opacity = float(opacity_root.get_editor_property("r"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"FBX glass opacity cannot be read: {material.get_path_name()}") from exc
    if not math.isclose(opacity, FBX_GLASS_OPACITY, rel_tol=0.0, abs_tol=1e-6):
        raise RuntimeError(f"FBX glass opacity changed: {material.get_path_name()} {opacity}")
    return {
        "policy": FBX_GLASS_OVERRIDE_POLICY,
        "blend_mode": "translucent",
        "opacity": FBX_GLASS_OPACITY,
    }


def _material_postprocess_payload(loaded_assets, source_format):
    if source_format != "fbx":
        return {"policy": "not_applicable", "materials": []}
    expected_map = _fbx_material_texture_map(loaded_assets)
    materials = _referenced_base_materials(loaded_assets)
    payload = []
    connected_count = 0
    for material in materials:
        bindings = []
        actual_by_role = {}
        for role, texture in _base_material_texture_bindings(material):
            texture_path = _stable_asset_path(texture, f"{role} texture")
            actual_by_role.setdefault(role, set()).add(texture_path)
            item = {
                "role": role,
                "texture_path": texture_path,
            }
            if role == "normal":
                classification = _texture_role(texture.get_name())
                source_convention = None if classification is None else classification[1]
                item.update(
                    {
                        "source_convention": source_convention,
                        "green_channel_flipped": bool(
                            texture.get_editor_property("flip_green_channel")
                        ),
                    }
                )
            bindings.append(item)
        for role, (expected_texture, normal_space) in expected_map[material].items():
            expected_path = _stable_asset_path(expected_texture, f"expected {role} texture")
            if actual_by_role.get(role) != {expected_path}:
                raise RuntimeError(
                    "FBX effective material binding does not match its source texture: "
                    f"material={material.get_path_name()} role={role} "
                    f"expected={expected_path} actual={sorted(actual_by_role.get(role, set()))}"
                )
            if role == "normal":
                expected_flip = normal_space == "opengl"
                actual_flip = bool(expected_texture.get_editor_property("flip_green_channel"))
                if actual_flip != expected_flip:
                    raise RuntimeError(
                        "FBX normal convention was not applied: "
                        f"{expected_path} source={normal_space} flip={actual_flip}"
                    )
        bindings.sort(key=lambda item: (item["role"], item["texture_path"]))
        connected_count += len(bindings)
        payload.append(
            {
                "material_path": _stable_asset_path(material, "postprocessed material"),
                "bindings": bindings,
                "shading_override": _glass_override_payload(
                    material,
                    expected_map[material],
                ),
            }
        )
    payload.sort(key=lambda item: item["material_path"])
    texture_count = sum(isinstance(asset, unreal.Texture) for asset in loaded_assets)
    if texture_count and connected_count == 0:
        raise RuntimeError("FBX imported textures but no connected PBR material bindings")
    return {
        "policy": FBX_PBR_POSTPROCESS_POLICY,
        "materials": payload,
    }


def _material_slots_payload(mesh):
    payload = []
    for index, slot in enumerate(mesh.get_editor_property("static_materials")):
        material = slot.get_editor_property("material_interface")
        material_path = None
        texture_paths = []
        if material is not None:
            material_path = _stable_asset_path(material, "slot material")
            texture_paths = _used_texture_paths(material)
        payload.append(
            {
                "index": index,
                "slot_name": str(slot.get_editor_property("material_slot_name")),
                "material_path": material_path,
                "texture_paths": texture_paths,
            }
        )
    return payload


def _static_mesh_payload(mesh):
    material_slots = _material_slots_payload(mesh)
    payload = {
        "object_path": mesh.get_path_name(),
        "name": mesh.get_name(),
        "material_count": len(material_slots),
        "material_slots": material_slots,
        "bounds_cm": _mesh_bounds(mesh),
    }
    payload.update(_mesh_counts(mesh))
    return payload


def _validate_asset_id(asset_id):
    if ASSET_ID_PATTERN.fullmatch(asset_id) is None or len(asset_id) > 64:
        raise RuntimeError(
            f"invalid asset_id {asset_id!r}; expected lowercase snake_case starting with a letter"
        )


def _validate_bounds(bounds):
    minimum = bounds["min"]
    maximum = bounds["max"]
    size = bounds["size"]
    values = [*minimum, *maximum, *size]
    if not all(math.isfinite(value) for value in values):
        return False
    if any(low > high for low, high in zip(minimum, maximum, strict=True)):
        return False
    if any(value < 0.0 for value in size):
        return False
    return sum(value > 0.0 for value in size) >= 2


def _validate_static_meshes(static_meshes, require_single):
    if not static_meshes:
        raise RuntimeError("ingest produced no StaticMesh assets")
    if require_single and len(static_meshes) != 1:
        raise RuntimeError(
            f"M2 v1 requires exactly one StaticMesh per logical asset; got {len(static_meshes)}"
        )
    mesh_payloads = [_static_mesh_payload(mesh) for mesh in static_meshes]
    if any(mesh["triangle_count"] <= 0 for mesh in mesh_payloads):
        raise RuntimeError(f"imported StaticMesh has no triangles: {mesh_payloads}")
    if any(not _validate_bounds(mesh["bounds_cm"]) for mesh in mesh_payloads):
        raise RuntimeError(f"imported StaticMesh has invalid bounds: {mesh_payloads}")
    return mesh_payloads


def _transaction_paths(asset_id):
    destination = f"{INGEST_ROOT}/{asset_id}"
    transaction = f"{TRANSACTION_ROOT}/{asset_id}"
    return destination, transaction, f"{transaction}/candidate", f"{transaction}/backup"


def _is_safe_directory(path):
    return path.startswith(INGEST_ROOT + "/") or path.startswith(TRANSACTION_ROOT + "/")


def _delete_directory(path):
    if not _is_safe_directory(path):
        raise RuntimeError(f"unsafe ingest directory: {path}")
    existed = unreal.EditorAssetLibrary.does_directory_exist(path)
    if existed and not unreal.EditorAssetLibrary.delete_directory(path):
        raise RuntimeError(f"could not delete ingest directory: {path}")
    return bool(existed)


def _rename_directory(source, destination):
    if not _is_safe_directory(source) or not _is_safe_directory(destination):
        raise RuntimeError(f"unsafe ingest rename: {source} -> {destination}")
    if not unreal.EditorAssetLibrary.does_directory_exist(source):
        raise RuntimeError(f"ingest rename source does not exist: {source}")
    if unreal.EditorAssetLibrary.does_directory_exist(destination):
        raise RuntimeError(f"ingest rename destination already exists: {destination}")
    if not unreal.EditorAssetLibrary.rename_directory(source, destination):
        raise RuntimeError(f"could not rename ingest directory: {source} -> {destination}")


def _rollback_transaction(asset_id, remove_new_destination):
    destination, transaction, candidate, backup = _transaction_paths(asset_id)
    restored_previous = False
    removed_new_destination = False
    if unreal.EditorAssetLibrary.does_directory_exist(backup):
        removed_new_destination = _delete_directory(destination)
        _rename_directory(backup, destination)
        restored_previous = True
    elif remove_new_destination:
        removed_new_destination = _delete_directory(destination)
    _delete_directory(candidate)
    _delete_directory(transaction)
    return {
        "status": "ok",
        "restored_previous": restored_previous,
        "removed_new_destination": removed_new_destination,
    }


def _recover_stale_transaction(asset_id):
    destination, transaction, candidate, backup = _transaction_paths(asset_id)
    recovered = False
    if unreal.EditorAssetLibrary.does_directory_exist(backup):
        _delete_directory(destination)
        _rename_directory(backup, destination)
        recovered = True
    _delete_directory(candidate)
    _delete_directory(transaction)
    return recovered


def _import_asset(source_file, destination_path):
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", str(source_file))
    task.set_editor_property("destination_path", destination_path)
    task.set_editor_property("automated", True)
    task.set_editor_property("async_", False)
    task.set_editor_property("replace_existing", False)
    task.set_editor_property("replace_existing_settings", False)
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    task.get_objects()
    return _list_assets(destination_path)


def _list_assets(directory):
    return sorted(
        unreal.EditorAssetLibrary.list_assets(
            directory,
            recursive=True,
            include_folder=False,
        )
    )


def _load_assets(object_paths):
    loaded_assets = []
    for object_path in object_paths:
        asset = unreal.EditorAssetLibrary.load_asset(object_path)
        if asset is None:
            raise RuntimeError(f"could not load imported object: {object_path}")
        loaded_assets.append(asset)
    return loaded_assets


def _object_payload(asset):
    return {
        "object_path": asset.get_path_name(),
        "class": asset.get_class().get_name(),
    }


def _asset_payload(loaded_assets, require_single, source_format=None):
    static_meshes = [asset for asset in loaded_assets if isinstance(asset, unreal.StaticMesh)]
    mesh_payloads = _validate_static_meshes(static_meshes, require_single)
    return {
        "imported_object_paths": [asset.get_path_name() for asset in loaded_assets],
        "imported_objects": [_object_payload(asset) for asset in loaded_assets],
        "object_count": len(loaded_assets),
        "static_mesh_count": len(static_meshes),
        "material_count": sum(
            isinstance(asset, unreal.MaterialInterface) for asset in loaded_assets
        ),
        "texture_count": sum(isinstance(asset, unreal.Texture) for asset in loaded_assets),
        "static_meshes": mesh_payloads,
        "import_backend": IMPORT_BACKEND,
        "normalization": dict(NORMALIZATION_METADATA),
        "material_postprocess": _material_postprocess_payload(
            loaded_assets,
            source_format,
        ),
    }


def _validate_expected_objects(loaded_assets, expected_objects):
    actual = [_object_payload(asset) for asset in loaded_assets]
    if actual != expected_objects:
        raise RuntimeError(
            f"reloaded object inventory differs from import: expected={expected_objects}, "
            f"actual={actual}"
        )


def _save_directory(path):
    if not unreal.EditorAssetLibrary.save_directory(
        path,
        only_if_is_dirty=False,
        recursive=True,
    ):
        raise RuntimeError(f"could not save imported assets: {path}")


def _run_import(job, manifest, manifest_path, asset_id):
    source_file = Path(job["source_file"]).resolve()
    source_format = source_file.suffix.lower().lstrip(".")
    require_single = bool(job.get("require_single_static_mesh", True))
    manifest.update(
        {
            "source_file": str(source_file),
            "source_format": source_format,
        }
    )
    if not source_file.is_file():
        raise FileNotFoundError(f"ingest source file not found: {source_file}")
    if source_file.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise RuntimeError(f"unsupported ingest source format: {source_file.suffix}")

    destination, transaction, candidate, backup = _transaction_paths(asset_id)
    recovered = _recover_stale_transaction(asset_id)
    had_existing = unreal.EditorAssetLibrary.does_directory_exist(destination)
    manifest["transaction"] = {
        "state": "staging",
        "had_existing": bool(had_existing),
        "recovered_stale_transaction": recovered,
        "destination_path": destination,
        "candidate_path": candidate,
        "backup_path": backup,
    }
    _write_manifest(manifest_path, manifest)

    _import_asset(source_file, candidate)
    candidate_assets = _load_assets(_list_assets(candidate))
    if source_format == "fbx":
        _apply_fbx_pbr_postprocess(candidate_assets)
    _asset_payload(candidate_assets, require_single, source_format)
    _save_directory(candidate)

    manifest["transaction"]["state"] = "promoting"
    _write_manifest(manifest_path, manifest)
    if had_existing:
        _rename_directory(destination, backup)
    try:
        _rename_directory(candidate, destination)
    except Exception:
        _rollback_transaction(asset_id, remove_new_destination=not had_existing)
        raise

    final_assets = _load_assets(_list_assets(destination))
    payload = _asset_payload(final_assets, require_single, source_format)
    _save_directory(destination)
    manifest.update(payload)
    manifest["destination_path"] = destination
    manifest["transaction"]["state"] = "pending_host_validation"
    manifest["status"] = "ok"


def _run_reload(job, manifest):
    expected_objects = job["imported_objects"]
    if not isinstance(expected_objects, list) or not expected_objects:
        raise RuntimeError("reload validation requires imported_objects")
    object_paths = [str(item["object_path"]) for item in expected_objects]
    loaded_assets = _load_assets(object_paths)
    _validate_expected_objects(loaded_assets, expected_objects)
    payload = _asset_payload(
        loaded_assets,
        bool(job.get("require_single_static_mesh", True)),
        str(job["source_format"]),
    )
    manifest.update(payload)
    manifest["status"] = "ok"


def _expected_asset_payload(job):
    expected = job.get("expected_asset_payload")
    if not isinstance(expected, dict) or set(expected) != set(ASSET_PAYLOAD_KEYS):
        raise RuntimeError("finalize/inspect requires the exact host-approved asset payload")
    return expected


def _current_asset_payload(job):
    expected_objects = job["imported_objects"]
    if not isinstance(expected_objects, list) or not expected_objects:
        raise RuntimeError("finalize/inspect requires imported_objects")
    object_paths = [str(item["object_path"]) for item in expected_objects]
    loaded_assets = _load_assets(object_paths)
    _validate_expected_objects(loaded_assets, expected_objects)
    return _asset_payload(
        loaded_assets,
        bool(job.get("require_single_static_mesh", True)),
        str(job["source_format"]),
    )


def _run_finalize(job, manifest, manifest_path, asset_id):
    destination, transaction, candidate, backup = _transaction_paths(asset_id)
    expected_payload = _expected_asset_payload(job)
    payload = _current_asset_payload(job)
    if payload != expected_payload:
        raise RuntimeError("finalize destination payload differs from host-approved payload")
    manifest.update(payload)
    _save_directory(destination)
    transaction_exists = unreal.EditorAssetLibrary.does_directory_exist(transaction)
    backup_exists = unreal.EditorAssetLibrary.does_directory_exist(backup)
    candidate_exists = unreal.EditorAssetLibrary.does_directory_exist(candidate)
    expected_backup = bool(job.get("had_existing", False))
    if transaction_exists and candidate_exists:
        raise RuntimeError("finalize found an unexpected candidate directory")
    if transaction_exists and backup_exists != expected_backup:
        raise RuntimeError(
            "finalize backup state differs from the prepared transaction: "
            f"expected={expected_backup} actual={backup_exists}"
        )
    manifest.update(
        {
            "status": "prepared",
            "transaction_state": "prepared",
            "transaction_exists": transaction_exists,
            "backup_exists": backup_exists,
        }
    )
    _write_manifest(manifest_path, manifest)

    removed_transaction = _delete_directory(transaction) if transaction_exists else False
    manifest.update(
        {
            "status": "ok",
            "removed_backup": bool(backup_exists and removed_transaction),
            "removed_transaction": removed_transaction,
            "transaction_state": "committed",
            "commit_confirmation": (
                "transaction_deleted" if transaction_exists else "already_committed"
            ),
        }
    )


def _run_inspect(job, manifest, asset_id):
    destination, transaction, candidate, backup = _transaction_paths(asset_id)
    expected_payload = _expected_asset_payload(job)
    payload_matches = False
    payload_error = None
    try:
        payload = _current_asset_payload(job)
        payload_matches = payload == expected_payload
        if payload_matches:
            manifest.update(payload)
    except Exception as exc:
        payload_error = {"type": type(exc).__name__, "message": str(exc)}

    destination_exists = unreal.EditorAssetLibrary.does_directory_exist(destination)
    transaction_exists = unreal.EditorAssetLibrary.does_directory_exist(transaction)
    candidate_exists = unreal.EditorAssetLibrary.does_directory_exist(candidate)
    backup_exists = unreal.EditorAssetLibrary.does_directory_exist(backup)
    if payload_matches and transaction_exists:
        transaction_state = "pre_commit"
    elif payload_matches and not transaction_exists:
        transaction_state = "committed"
    else:
        transaction_state = "in_doubt"
    manifest.update(
        {
            "status": "ok",
            "transaction_state": transaction_state,
            "payload_matches": payload_matches,
            "payload_error": payload_error,
            "destination_exists": destination_exists,
            "transaction_exists": transaction_exists,
            "candidate_exists": candidate_exists,
            "backup_exists": backup_exists,
        }
    )


def main():
    job_path = Path(os.environ["UEF_JOB_FILE"])
    job = json.loads(job_path.read_text(encoding="utf-8"))
    manifest_path = Path(job["manifest_path"])
    job_kind = str(job["job"])
    asset_id = str(job["asset_id"])
    _validate_asset_id(asset_id)
    manifest = {
        "schema_version": 1,
        "status": "running",
        "job": job_kind,
        "asset_id": asset_id,
    }
    try:
        if job_kind == "ingest_asset":
            _run_import(job, manifest, manifest_path, asset_id)
        elif job_kind == "validate_ingested_asset":
            _run_reload(job, manifest)
        elif job_kind == "finalize_ingested_asset":
            _run_finalize(job, manifest, manifest_path, asset_id)
        elif job_kind == "inspect_ingest_transaction":
            _run_inspect(job, manifest, asset_id)
        elif job_kind == "rollback_ingested_asset":
            manifest.update(
                _rollback_transaction(
                    asset_id,
                    remove_new_destination=bool(job["remove_new_destination"]),
                )
            )
        else:
            raise RuntimeError(f"unsupported UE ingest job kind: {job_kind}")
        _write_manifest(manifest_path, manifest)
        unreal.log(f"UEF_INGEST_OK {manifest_path}")
    except Exception as exc:
        source_traceback = traceback.format_exc()
        cleanup = {"status": "not_applicable"}
        if job_kind == "ingest_asset":
            transaction = manifest.get("transaction", {})
            remove_new = bool(
                isinstance(transaction, dict) and transaction.get("had_existing") is False
            )
            try:
                cleanup = _rollback_transaction(asset_id, remove_new)
            except Exception as cleanup_exc:
                cleanup = {
                    "status": "failed",
                    "error_type": type(cleanup_exc).__name__,
                    "error": str(cleanup_exc),
                }
        elif job_kind == "finalize_ingested_asset":
            cleanup = {
                "status": "deferred_to_host",
                "reason": "finalize may have crossed the irreversible commit point",
            }
        manifest.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": source_traceback,
                "transaction_cleanup": cleanup,
            }
        )
        _write_manifest(manifest_path, manifest)
        unreal.log_error(f"UEF_INGEST_FAILED {manifest_path}: {exc}")
        raise


if __name__ == "__main__":
    main()
