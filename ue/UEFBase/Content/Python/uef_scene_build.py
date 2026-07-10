import hashlib
import json
import math
import os
import re
import traceback
from pathlib import Path

import unreal

SCENES_ROOT = "/Game/UEF/Scenes"
TRANSACTIONS_ROOT = "/Game/UEF/SceneTransactions"
SCENE_ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
SUPPORTED_EXTENSIONS = {".fbx", ".glb", ".gltf"}
NEUTRAL_MAP = "/Engine/Maps/Templates/Template_Default"
INVENTORY_SCHEMA_VERSION = 1


def _write_manifest(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _canonical_digest(value):
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_number(value, digits=6):
    result = float(value)
    if not math.isfinite(result):
        raise RuntimeError(f"scene inventory contains a non-finite value: {result}")
    result = round(result, digits)
    return 0.0 if result == 0.0 else result


def _vector(value, digits=6):
    return [
        _stable_number(value.x, digits),
        _stable_number(value.y, digits),
        _stable_number(value.z, digits),
    ]


def _transform(actor):
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


def _mesh_bounds(mesh):
    box = mesh.get_bounding_box()
    minimum = box.min
    maximum = box.max
    return {
        "min": _vector(minimum),
        "max": _vector(maximum),
        "size": _vector(maximum - minimum),
    }


def _mesh_payload(mesh):
    description = mesh.get_static_mesh_description(0)
    if description is None:
        raise RuntimeError(f"missing LOD0 mesh description: {mesh.get_path_name()}")
    material_paths = []
    for slot in mesh.get_editor_property("static_materials"):
        material = slot.get_editor_property("material_interface")
        material_paths.append(None if material is None else str(material.get_path_name()))
    triangle_count = int(description.get_triangle_count())
    if triangle_count <= 0:
        raise RuntimeError(f"StaticMesh has no triangles: {mesh.get_path_name()}")
    return {
        "object_path": str(mesh.get_path_name()),
        "name": str(mesh.get_name()),
        "lod_count": int(mesh.get_num_lods()),
        "triangle_count": triangle_count,
        "vertex_count": int(description.get_vertex_count()),
        "material_count": len(material_paths),
        "material_paths": material_paths,
        "bounds_cm": _mesh_bounds(mesh),
    }


def _component_payload(component):
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


def _inventory(scene_root, map_path):
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    if actor_subsystem is None:
        raise RuntimeError("EditorActorSubsystem is unavailable")
    actors = []
    aggregate_min = [math.inf, math.inf, math.inf]
    aggregate_max = [-math.inf, -math.inf, -math.inf]
    static_component_count = 0
    static_mesh_actor_count = 0
    for actor in actor_subsystem.get_all_level_actors():
        component_rows = []
        for component in actor.get_components_by_class(unreal.StaticMeshComponent):
            component_row = _component_payload(component)
            if component_row is None:
                continue
            static_component_count += 1
            component_rows.append(component_row)
            bounds = component_row["world_bounds_cm"]
            for index in range(3):
                aggregate_min[index] = min(aggregate_min[index], bounds["min"][index])
                aggregate_max[index] = max(aggregate_max[index], bounds["max"][index])
        parent = actor.get_attach_parent_actor()
        if component_rows:
            static_mesh_actor_count += 1
        actors.append(
            {
                "object_id": str(actor.get_name()),
                "actor_name": str(actor.get_name()),
                "actor_label": str(actor.get_actor_label()),
                "actor_class": str(actor.get_class().get_name()),
                "parent_actor_name": None if parent is None else str(parent.get_name()),
                "transform": _transform(actor),
                "components": sorted(
                    component_rows,
                    key=lambda item: (item["mesh_path"], item["name"]),
                ),
            }
        )

    asset_rows = []
    meshes = []
    material_count = 0
    texture_count = 0
    for object_path in unreal.EditorAssetLibrary.list_assets(
        scene_root,
        recursive=True,
        include_folder=False,
    ):
        asset = unreal.EditorAssetLibrary.load_asset(object_path)
        if asset is None:
            raise RuntimeError(f"could not load scene asset: {object_path}")
        class_name = str(asset.get_class().get_name())
        asset_rows.append({"object_path": str(asset.get_path_name()), "class": class_name})
        if isinstance(asset, unreal.StaticMesh):
            meshes.append(_mesh_payload(asset))
        if isinstance(asset, unreal.MaterialInterface):
            material_count += 1
        if isinstance(asset, unreal.Texture):
            texture_count += 1

    if not meshes:
        raise RuntimeError("scene import produced no StaticMesh assets")
    if static_component_count <= 0:
        raise RuntimeError("scene level contains no StaticMesh components")
    aggregate_size = [
        _stable_number(high - low) for low, high in zip(aggregate_min, aggregate_max, strict=True)
    ]
    aggregate_min = [_stable_number(value) for value in aggregate_min]
    aggregate_max = [_stable_number(value) for value in aggregate_max]
    if sum(value > 0.0 for value in aggregate_size) < 2:
        raise RuntimeError(f"scene aggregate bounds are degenerate: {aggregate_size}")
    meshes.sort(key=lambda item: item["object_path"])
    actors.sort(key=lambda item: item["object_id"])
    asset_rows.sort(key=lambda item: item["object_path"])
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "map_path": map_path,
        "actor_count": len(actors),
        "static_mesh_actor_count": static_mesh_actor_count,
        "static_mesh_component_count": static_component_count,
        "static_mesh_count": len(meshes),
        "triangle_count": sum(item["triangle_count"] for item in meshes),
        "material_count": material_count,
        "texture_count": texture_count,
        "aggregate_bounds_cm": {
            "min": aggregate_min,
            "max": aggregate_max,
            "size": aggregate_size,
        },
        "actors": actors,
        "assets": asset_rows,
        "static_meshes": meshes,
    }


def _validate_expected(inventory, expected):
    if expected is None:
        return
    fields = {
        "mesh_count": "static_mesh_count",
        "material_count": "material_count",
        "texture_count": "texture_count",
        "triangle_count": "triangle_count",
    }
    for expected_key, inventory_key in fields.items():
        if expected_key not in expected:
            continue
        expected_value = int(expected[expected_key])
        actual_value = int(inventory[inventory_key])
        if actual_value != expected_value:
            raise RuntimeError(
                f"scene {expected_key} mismatch: expected={expected_value} actual={actual_value}"
            )


def _validate_scene_id(scene_id):
    if len(scene_id) > 64 or SCENE_ID_PATTERN.fullmatch(scene_id) is None:
        raise RuntimeError(f"invalid scene_id: {scene_id!r}")


def _paths(scene_id):
    destination = f"{SCENES_ROOT}/{scene_id}"
    transaction = f"{TRANSACTIONS_ROOT}/{scene_id}"
    candidate = f"{transaction}/candidate"
    backup = f"{transaction}/backup"
    return destination, transaction, candidate, backup


def _map_path(root, scene_id):
    return f"{root}/L_{scene_id}"


def _is_safe_directory(path):
    return path.startswith(SCENES_ROOT + "/") or path.startswith(TRANSACTIONS_ROOT + "/")


def _delete_directory(path):
    if not _is_safe_directory(path):
        raise RuntimeError(f"unsafe scene directory: {path}")
    existed = unreal.EditorAssetLibrary.does_directory_exist(path)
    if existed and not unreal.EditorAssetLibrary.delete_directory(path):
        raise RuntimeError(f"could not delete scene directory: {path}")
    return bool(existed)


def _rename_directory(source, destination, scene_id, delete_source=True):
    """Move a complete scene tree without renaming its World asset.

    UE 5.5's AssetRenameManager always opens an Ok/Cancel CDO-reference
    warning when a World is part of a rename batch, even when RenameAssets is
    invoked through its nominally non-dialog API.  Unattended runs answer that
    dialog with Cancel.  Move every non-World asset in one batch, save the
    source map so its references are fixed up, then clone the map through the
    LevelEditorSubsystem's unattended NewLevelFromTemplate path.
    """
    if not _is_safe_directory(source) or not _is_safe_directory(destination):
        raise RuntimeError(f"unsafe scene rename: {source} -> {destination}")
    if not unreal.EditorAssetLibrary.does_directory_exist(source):
        raise RuntimeError(f"scene rename source does not exist: {source}")
    if unreal.EditorAssetLibrary.does_directory_exist(destination):
        raise RuntimeError(f"scene rename destination already exists: {destination}")
    object_paths = unreal.EditorAssetLibrary.list_assets(
        source,
        recursive=True,
        include_folder=False,
    )
    if not object_paths:
        raise RuntimeError(f"scene rename source contains no assets: {source}")
    source_map = _map_path(source, scene_id)
    destination_map = _map_path(destination, scene_id)
    if not unreal.EditorAssetLibrary.does_asset_exist(source_map):
        raise RuntimeError(f"scene rename source has no persistent map: {source_map}")
    _load_level(source_map)
    source_inventory = _inventory(source, source_map)
    rename_data = []
    expected_destinations = []
    for object_path in object_paths:
        asset = unreal.EditorAssetLibrary.load_asset(object_path)
        if asset is None:
            raise RuntimeError(f"could not load scene asset for rename: {object_path}")
        package_path = str(asset.get_path_name()).partition(".")[0]
        if not package_path.startswith(source + "/"):
            raise RuntimeError(f"scene rename asset escaped its source root: {package_path}")
        if package_path == source_map:
            if not isinstance(asset, unreal.World):
                raise RuntimeError(f"scene map package is not a World: {object_path}")
            continue
        if isinstance(asset, unreal.World):
            raise RuntimeError(f"scene tree contains an unexpected World: {object_path}")
        relative_package = package_path[len(source) + 1 :]
        relative_parent = relative_package.rpartition("/")[0]
        new_package_path = (
            destination if not relative_parent else f"{destination}/{relative_parent}"
        )
        expected_destinations.append(f"{new_package_path}/{asset.get_name()}")
        rename_data.append(
            unreal.AssetRenameData(
                asset=asset,
                new_package_path=new_package_path,
                new_name=str(asset.get_name()),
            )
        )
    if not rename_data:
        raise RuntimeError(f"scene rename source contains no non-World assets: {source}")
    unreal.EditorAssetLibrary.make_directory(destination)
    if not unreal.AssetToolsHelpers.get_asset_tools().rename_assets(rename_data):
        raise RuntimeError(f"could not rename scene assets: {source} -> {destination}")
    missing = [
        path
        for path in expected_destinations
        if not unreal.EditorAssetLibrary.does_asset_exist(path)
    ]
    if missing:
        raise RuntimeError(f"scene asset rename lost {len(missing)} destination packages")
    level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if level_editor is None or not level_editor.save_current_level():
        raise RuntimeError(f"could not save remapped scene map: {source_map}")
    if not level_editor.new_level_from_template(destination_map, source_map):
        raise RuntimeError(
            f"could not clone scene map without a dialog: {source_map} -> {destination_map}"
        )
    if not level_editor.save_current_level():
        raise RuntimeError(f"could not save promoted scene map: {destination_map}")
    _save_scene_assets(destination)
    destination_inventory = _inventory(destination, destination_map)
    expected_inventory = _remap_paths(source_inventory, source, destination)
    if destination_inventory != expected_inventory:
        raise RuntimeError("scene inventory changed during no-dialog scene promotion")
    _load_neutral_level()
    if delete_source:
        _delete_directory(source)


def _load_neutral_level():
    level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if level_editor is None or not level_editor.load_level(NEUTRAL_MAP):
        raise RuntimeError(f"could not load neutral editor map: {NEUTRAL_MAP}")


def _load_level(map_path):
    level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if level_editor is None or not level_editor.load_level(map_path):
        raise RuntimeError(f"could not load scene map: {map_path}")


def _save_scene_assets(scene_root):
    for object_path in unreal.EditorAssetLibrary.list_assets(
        scene_root,
        recursive=True,
        include_folder=False,
    ):
        if not unreal.EditorAssetLibrary.save_asset(object_path, only_if_is_dirty=False):
            raise RuntimeError(f"could not save scene asset: {object_path}")


def _recover_stale_transaction(scene_id):
    destination, transaction, candidate, backup = _paths(scene_id)
    transaction_exists = unreal.EditorAssetLibrary.does_directory_exist(transaction)
    candidate_exists = unreal.EditorAssetLibrary.does_directory_exist(candidate)
    backup_exists = unreal.EditorAssetLibrary.does_directory_exist(backup)
    recovered = False
    if backup_exists:
        _delete_directory(destination)
        _rename_directory(backup, destination, scene_id)
        recovered = True
    elif transaction_exists and not candidate_exists:
        _delete_directory(destination)
        recovered = True
    _delete_directory(candidate)
    _delete_directory(transaction)
    return recovered


def _rollback(scene_id, remove_new_destination):
    destination, transaction, candidate, backup = _paths(scene_id)
    restored_previous = False
    removed_new_destination = False
    if unreal.EditorAssetLibrary.does_directory_exist(backup):
        _load_neutral_level()
        removed_new_destination = _delete_directory(destination)
        _rename_directory(backup, destination, scene_id)
        restored_previous = True
    elif remove_new_destination and unreal.EditorAssetLibrary.does_directory_exist(transaction):
        _load_neutral_level()
        removed_new_destination = _delete_directory(destination)
    _delete_directory(candidate)
    _delete_directory(transaction)
    return {
        "status": "ok",
        "restored_previous": restored_previous,
        "removed_new_destination": removed_new_destination,
    }


def _import_scene(source_file, asset_root, scene_id):
    manager = unreal.InterchangeManager.get_interchange_manager_scripted()
    source_data = manager.create_source_data(str(source_file))
    if source_data is None:
        raise RuntimeError(f"could not create Interchange source data: {source_file}")
    params = unreal.ImportAssetParameters()
    params.set_editor_property("is_automated", True)
    params.set_editor_property("follow_redirectors", False)
    params.set_editor_property("destination_name", scene_id)
    params.set_editor_property("replace_existing", False)
    params.set_editor_property("force_show_dialog", False)
    if not manager.import_scene(asset_root, source_data, params):
        raise RuntimeError(f"Interchange scene import failed: {source_file}")


def _remap_paths(value, old_root, new_root):
    if isinstance(value, str):
        return new_root + value[len(old_root) :] if value.startswith(old_root) else value
    if isinstance(value, list):
        return [_remap_paths(item, old_root, new_root) for item in value]
    if isinstance(value, dict):
        return {key: _remap_paths(item, old_root, new_root) for key, item in value.items()}
    return value


def _require_inventory(job, scene_id):
    expected = job.get("expected_inventory")
    expected_digest = str(job.get("expected_inventory_sha256", ""))
    if not isinstance(expected, dict) or len(expected_digest) != 64:
        raise RuntimeError("scene validation job is missing its approved inventory")
    destination, _, _, _ = _paths(scene_id)
    final_map = _map_path(destination, scene_id)
    _load_level(final_map)
    actual = _inventory(destination, final_map)
    actual_digest = _canonical_digest(actual)
    if actual != expected or actual_digest != expected_digest:
        raise RuntimeError(
            "scene inventory changed after build: "
            f"expected={expected_digest} actual={actual_digest}"
        )
    return actual, actual_digest


def _require_scene_spec(job, scene_id):
    scene_spec = job.get("scene_spec")
    expected_digest = str(job.get("scene_spec_sha256", ""))
    if not isinstance(scene_spec, dict) or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        raise RuntimeError("scene job is missing its canonical SceneSpec")
    actual_digest = _canonical_digest(scene_spec)
    if actual_digest != expected_digest:
        raise RuntimeError(
            "scene spec digest changed before UE execution: "
            f"expected={expected_digest} actual={actual_digest}"
        )
    if scene_spec.get("scene_id") != scene_id or scene_spec.get("kind") != "interchange_scene":
        raise RuntimeError("scene spec identity does not match the UE job")
    build = scene_spec.get("build")
    destination, _, _, _ = _paths(scene_id)
    if not isinstance(build, dict) or build.get("map_path") != _map_path(destination, scene_id):
        raise RuntimeError("scene spec map_path does not match its fixed persistent map")
    return expected_digest


def _run_build(job, manifest, scene_id):
    source_file = Path(job["source_file"]).resolve()
    if source_file.suffix.lower() not in SUPPORTED_EXTENSIONS or not source_file.is_file():
        raise RuntimeError(f"invalid scene source: {source_file}")
    expected_source_sha256 = str(job["source_sha256"])
    if _sha256(source_file) != expected_source_sha256:
        raise RuntimeError("scene source hash changed before UE import")

    destination, transaction, candidate, backup = _paths(scene_id)
    recovered_stale = _recover_stale_transaction(scene_id)
    unreal.EditorAssetLibrary.make_directory(candidate)
    candidate_map = _map_path(candidate, scene_id)
    level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if level_editor is None or not level_editor.new_level(candidate_map, False):
        raise RuntimeError(f"could not create candidate scene level: {candidate_map}")
    asset_root = f"{candidate}/Assets"
    unreal.EditorAssetLibrary.make_directory(asset_root)
    _import_scene(source_file, asset_root, scene_id)
    if not level_editor.save_current_level():
        raise RuntimeError(f"could not save candidate scene map: {candidate_map}")
    _save_scene_assets(asset_root)

    candidate_inventory = _inventory(candidate, candidate_map)
    _validate_expected(candidate_inventory, job.get("expected"))
    if _sha256(source_file) != expected_source_sha256:
        raise RuntimeError("scene source hash changed during UE import")

    _load_neutral_level()
    had_existing = unreal.EditorAssetLibrary.does_directory_exist(destination)
    manifest["transaction"] = {
        "state": "promoting",
        "destination": destination,
        "transaction": transaction,
        "backup": backup,
        "had_existing": had_existing,
        "recovered_stale_transaction": recovered_stale,
    }
    if had_existing:
        _rename_directory(destination, backup, scene_id)
    try:
        # Deleting a just-imported World in this same process can crash UE 5.5's
        # LevelInstanceSubsystem for large Interchange scenes.  Keep the now
        # asset-empty candidate World as rollback evidence and delete it in the
        # independent finalize process after reload validation.
        _rename_directory(candidate, destination, scene_id, delete_source=False)
    except Exception:
        if had_existing and unreal.EditorAssetLibrary.does_directory_exist(backup):
            _delete_directory(destination)
            _rename_directory(backup, destination, scene_id)
        raise

    final_map = _map_path(destination, scene_id)
    _load_level(final_map)
    inventory = _inventory(destination, final_map)
    expected_after_move = _remap_paths(candidate_inventory, candidate, destination)
    if inventory != expected_after_move:
        raise RuntimeError("scene inventory changed while promoting the candidate package")
    inventory_digest = _canonical_digest(inventory)
    manifest.update(
        {
            "status": "prepared",
            "source_file": str(source_file),
            "source_sha256": expected_source_sha256,
            "import_backend": "interchange_scene",
            "inventory": inventory,
            "inventory_sha256": inventory_digest,
            "transaction": {
                "state": "promoted_pending_validation",
                "destination": destination,
                "transaction": transaction,
                "backup": backup,
                "had_existing": had_existing,
                "recovered_stale_transaction": recovered_stale,
            },
        }
    )


def _run_reload(job, manifest, scene_id):
    inventory, inventory_digest = _require_inventory(job, scene_id)
    manifest.update(
        {
            "status": "ok",
            "inventory": inventory,
            "inventory_sha256": inventory_digest,
            "transaction": {"state": "validated_pending_commit"},
        }
    )


def _run_finalize(job, manifest, manifest_path, scene_id):
    destination, transaction, candidate, backup = _paths(scene_id)
    inventory, inventory_digest = _require_inventory(job, scene_id)
    transaction_exists = unreal.EditorAssetLibrary.does_directory_exist(transaction)
    candidate_exists = unreal.EditorAssetLibrary.does_directory_exist(candidate)
    backup_exists = unreal.EditorAssetLibrary.does_directory_exist(backup)
    if candidate_exists:
        _delete_directory(candidate)
        candidate_exists = unreal.EditorAssetLibrary.does_directory_exist(candidate)
    if candidate_exists:
        raise RuntimeError("scene finalize could not delete the validated candidate directory")
    if not transaction_exists:
        manifest.update(
            {
                "status": "ok",
                "inventory": inventory,
                "inventory_sha256": inventory_digest,
                "transaction": {
                    "state": "committed",
                    "commit_confirmation": "already_committed",
                    "destination": destination,
                },
            }
        )
        return

    prepared = {
        **manifest,
        "status": "prepared",
        "inventory": inventory,
        "inventory_sha256": inventory_digest,
        "transaction": {
            "state": "prepared",
            "destination": destination,
            "transaction": transaction,
            "backup": backup,
            "backup_exists": backup_exists,
        },
    }
    _write_manifest(manifest_path, prepared)
    removed_backup = _delete_directory(backup) if backup_exists else False
    removed_transaction = _delete_directory(transaction)
    manifest.update(
        {
            "status": "ok",
            "inventory": inventory,
            "inventory_sha256": inventory_digest,
            "transaction": {
                "state": "committed",
                "commit_confirmation": "transaction_deleted",
                "destination": destination,
                "removed_backup": removed_backup,
                "removed_transaction": removed_transaction,
            },
        }
    )


def _run_inspect(job, manifest, scene_id):
    destination, transaction, candidate, backup = _paths(scene_id)
    flags = {
        "destination_exists": unreal.EditorAssetLibrary.does_directory_exist(destination),
        "transaction_exists": unreal.EditorAssetLibrary.does_directory_exist(transaction),
        "candidate_exists": unreal.EditorAssetLibrary.does_directory_exist(candidate),
        "backup_exists": unreal.EditorAssetLibrary.does_directory_exist(backup),
    }
    payload_matches = False
    payload_error = None
    if flags["destination_exists"]:
        try:
            _, digest = _require_inventory(job, scene_id)
            payload_matches = digest == str(job["expected_inventory_sha256"])
        except Exception as exc:
            payload_error = f"{type(exc).__name__}: {exc}"
    if not flags["transaction_exists"] and payload_matches:
        state = "committed"
    elif flags["transaction_exists"] and (
        flags["backup_exists"] or not payload_matches or flags["candidate_exists"]
    ):
        state = "pre_commit"
    else:
        state = "in_doubt"
    manifest.update(
        {
            "status": "ok",
            "inspection": {
                "state": state,
                "payload_matches": payload_matches,
                "payload_error": payload_error,
                **flags,
            },
        }
    )


def main():
    job_path = Path(os.environ["UEF_JOB_FILE"])
    job = json.loads(job_path.read_text(encoding="utf-8"))
    manifest_path = Path(job["manifest_path"])
    job_kind = str(job["job"])
    scene_id = str(job["scene_id"])
    _validate_scene_id(scene_id)
    scene_spec_sha256 = _require_scene_spec(job, scene_id)
    manifest = {
        "schema_version": 1,
        "status": "running",
        "job": job_kind,
        "scene_id": scene_id,
        "scene_spec_sha256": scene_spec_sha256,
    }
    try:
        if job_kind == "build_scene":
            _run_build(job, manifest, scene_id)
        elif job_kind == "reload_scene":
            _run_reload(job, manifest, scene_id)
        elif job_kind == "finalize_scene":
            _run_finalize(job, manifest, manifest_path, scene_id)
        elif job_kind == "inspect_scene_transaction":
            _run_inspect(job, manifest, scene_id)
        elif job_kind == "rollback_scene":
            manifest.update(_rollback(scene_id, bool(job["remove_new_destination"])))
        else:
            raise RuntimeError(f"unsupported scene job kind: {job_kind}")
        _write_manifest(manifest_path, manifest)
        unreal.log(f"UEF_SCENE_OK {manifest_path}")
    except Exception as exc:
        cleanup = {"status": "not_applicable"}
        if job_kind == "build_scene":
            cleanup = {
                "status": "deferred_to_host",
                "reason": (
                    "build may retain live Interchange/LevelInstance objects; "
                    "independent host rollback is required"
                ),
            }
        elif job_kind == "finalize_scene":
            cleanup = {
                "status": "deferred_to_host",
                "reason": "finalize may have crossed the commit point",
            }
        manifest.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "transaction_cleanup": cleanup,
            }
        )
        _write_manifest(manifest_path, manifest)
        unreal.log_error(f"UEF_SCENE_FAILED {manifest_path}: {exc}")
        raise


if __name__ == "__main__":
    main()
