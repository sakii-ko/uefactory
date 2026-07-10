from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "ue/UEFBase/Content/Python/uef_scene_build.py"
SCRIPT_SOURCE = SCRIPT_PATH.read_text(encoding="utf-8")
SCRIPT_TREE = ast.parse(SCRIPT_SOURCE, filename=str(SCRIPT_PATH))


def _function_source(name: str) -> str:
    for node in SCRIPT_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            source = ast.get_source_segment(SCRIPT_SOURCE, node)
            assert source is not None
            return source
    raise AssertionError(f"missing function {name!r} in {SCRIPT_PATH}")


def _load_scene_script(monkeypatch: pytest.MonkeyPatch) -> Any:
    unreal = ModuleType("unreal")
    monkeypatch.setitem(sys.modules, "unreal", unreal)
    spec = importlib.util.spec_from_file_location("test_uef_scene_build", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_transaction_and_final_map_namespaces_are_exact_and_escape_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_scene_script(monkeypatch)

    destination, transaction, candidate, backup = script._paths("fantasy_diorama")

    assert destination == "/Game/UEF/Scenes/fantasy_diorama"
    assert transaction == "/Game/UEF/SceneTransactions/fantasy_diorama"
    assert candidate == "/Game/UEF/SceneTransactions/fantasy_diorama/candidate"
    assert backup == "/Game/UEF/SceneTransactions/fantasy_diorama/backup"
    assert (
        script._map_path(destination, "fantasy_diorama")
        == "/Game/UEF/Scenes/fantasy_diorama/L_fantasy_diorama"
    )
    assert (
        script._map_path(candidate, "fantasy_diorama")
        == "/Game/UEF/SceneTransactions/fantasy_diorama/candidate/L_fantasy_diorama"
    )
    assert script._is_safe_directory(destination) is True
    assert script._is_safe_directory(transaction) is True
    assert script._is_safe_directory("/Game/UEF/ScenesEscape/fantasy_diorama") is False
    assert script._is_safe_directory("/Game/Other/fantasy_diorama") is False


@pytest.mark.parametrize(
    "scene_id",
    ["../escape", "Fantasy", "1scene", "scene__id", "scene_id_", "a" * 65],
)
def test_scene_id_guard_rejects_package_escape_values(
    monkeypatch: pytest.MonkeyPatch,
    scene_id: str,
) -> None:
    script = _load_scene_script(monkeypatch)

    with pytest.raises(RuntimeError, match="invalid scene_id"):
        script._validate_scene_id(scene_id)


class _RenameAsset:
    def __init__(self, object_path: str) -> None:
        self.object_path = object_path

    def get_path_name(self) -> str:
        return self.object_path

    def get_name(self) -> str:
        return self.object_path.rsplit(".", 1)[-1]


class _RenameWorld(_RenameAsset):
    pass


class _RenameData:
    def __init__(self, *, asset: _RenameAsset, new_package_path: str, new_name: str) -> None:
        self.asset = asset
        self.new_package_path = new_package_path
        self.new_name = new_name


def test_asset_tools_renames_non_world_assets_and_clones_map_without_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_scene_script(monkeypatch)
    source = "/Game/UEF/SceneTransactions/fantasy_diorama/candidate"
    destination = "/Game/UEF/Scenes/fantasy_diorama"
    assets = [
        _RenameWorld(f"{source}/L_fantasy_diorama.L_fantasy_diorama"),
        _RenameAsset(f"{source}/Assets/Nested/SM_Castle.SM_Castle"),
    ]
    directories = {source}
    existing_packages: set[str] = set()
    rename_batches: list[list[_RenameData]] = []
    deleted_directories: list[str] = []
    level_events: list[tuple[str, ...]] = []

    class AssetLibrary:
        @staticmethod
        def does_directory_exist(path: str) -> bool:
            return path in directories

        @staticmethod
        def list_assets(path: str, *, recursive: bool, include_folder: bool) -> list[str]:
            assert path == source
            assert recursive is True
            assert include_folder is False
            return [asset.object_path for asset in assets]

        @staticmethod
        def load_asset(path: str) -> _RenameAsset | None:
            return next((asset for asset in assets if asset.object_path == path), None)

        @staticmethod
        def make_directory(path: str) -> bool:
            directories.add(path)
            return True

        @staticmethod
        def does_asset_exist(path: str) -> bool:
            return path == f"{source}/L_fantasy_diorama" or path in existing_packages

        @staticmethod
        def delete_directory(path: str) -> bool:
            deleted_directories.append(path)
            directories.discard(path)
            return True

    class AssetTools:
        @staticmethod
        def rename_assets(rows: list[_RenameData]) -> bool:
            rename_batches.append(rows)
            for row in rows:
                existing_packages.add(f"{row.new_package_path}/{row.new_name}")
            return True

    class LevelEditor:
        @staticmethod
        def save_current_level() -> bool:
            level_events.append(("save",))
            return True

        @staticmethod
        def new_level_from_template(destination_map: str, source_map: str) -> bool:
            level_events.append(("clone", source_map, destination_map))
            return True

    script.unreal = SimpleNamespace(
        EditorAssetLibrary=AssetLibrary,
        AssetRenameData=_RenameData,
        AssetToolsHelpers=SimpleNamespace(get_asset_tools=lambda: AssetTools()),
        LevelEditorSubsystem=object,
        World=_RenameWorld,
        get_editor_subsystem=lambda subsystem: LevelEditor(),
    )
    source_inventory = {
        "map_path": f"{source}/L_fantasy_diorama",
        "assets": [f"{source}/Assets/Nested/SM_Castle.SM_Castle"],
    }
    monkeypatch.setattr(script, "_load_level", lambda path: None)
    monkeypatch.setattr(script, "_load_neutral_level", lambda: None)
    monkeypatch.setattr(script, "_save_scene_assets", lambda root: None)
    monkeypatch.setattr(
        script,
        "_inventory",
        lambda root, map_path: (
            source_inventory
            if root == source
            else script._remap_paths(source_inventory, source, destination)
        ),
    )

    def delete_directory(path: str) -> bool:
        deleted_directories.append(path)
        return True

    monkeypatch.setattr(script, "_delete_directory", delete_directory)

    script._rename_directory(source, destination, "fantasy_diorama")

    assert len(rename_batches) == 1
    assert [(row.new_package_path, row.new_name) for row in rename_batches[0]] == [
        (f"{destination}/Assets/Nested", "SM_Castle"),
    ]
    assert level_events == [
        ("save",),
        (
            "clone",
            f"{source}/L_fantasy_diorama",
            f"{destination}/L_fantasy_diorama",
        ),
        ("save",),
    ]
    assert deleted_directories == [source]
    rename_source = _function_source("_rename_directory")
    assert ".rename_assets(rename_data)" in rename_source
    assert "rename_assets_with_dialog" not in rename_source
    assert "rename_asset_with_dialog" not in rename_source


def test_interchange_scene_import_is_automated_and_cannot_open_dialogs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_scene_script(monkeypatch)
    imported: list[tuple[str, object, object]] = []

    class Parameters:
        def __init__(self) -> None:
            self.properties: dict[str, object] = {}

        def set_editor_property(self, name: str, value: object) -> None:
            self.properties[name] = value

    source_data = object()

    def import_scene(root: str, source: object, params: object) -> bool:
        imported.append((root, source, params))
        return True

    manager = SimpleNamespace(
        create_source_data=lambda path: source_data,
        import_scene=import_scene,
    )
    script.unreal = SimpleNamespace(
        InterchangeManager=SimpleNamespace(
            get_interchange_manager_scripted=lambda: manager,
        ),
        ImportAssetParameters=Parameters,
    )

    script._import_scene(Path("/library/fantasy.glb"), "/Game/Transaction/Assets", "fantasy")

    assert len(imported) == 1
    assert imported[0][:2] == ("/Game/Transaction/Assets", source_data)
    params = cast(Parameters, imported[0][2])
    assert params.properties == {
        "is_automated": True,
        "follow_redirectors": False,
        "destination_name": "fantasy",
        "replace_existing": False,
        "force_show_dialog": False,
    }


def test_source_hash_gates_build_before_transaction_recovery_and_after_import() -> None:
    build_source = _function_source("_run_build")
    first_source_check = build_source.index("_sha256(source_file) != expected_source_sha256")
    recovery = build_source.index("_recover_stale_transaction(scene_id)")
    import_scene = build_source.index("_import_scene(source_file, asset_root, scene_id)")
    last_source_check = build_source.rindex("_sha256(source_file) != expected_source_sha256")

    assert build_source.count("_sha256(source_file) != expected_source_sha256") == 2
    assert first_source_check < recovery < import_scene < last_source_check
    assert '"source_sha256": expected_source_sha256' in build_source


def test_scene_spec_payload_and_hash_are_validated_before_any_mode_dispatch() -> None:
    gate_source = _function_source("_require_scene_spec")
    main_source = _function_source("main")

    assert 'job.get("scene_spec")' in gate_source
    assert 'job.get("scene_spec_sha256"' in gate_source
    assert "_canonical_digest" in gate_source
    assert "scene_id" in gate_source
    assert "map_path" in gate_source
    gate_call = main_source.index("_require_scene_spec(job, scene_id)")
    first_dispatch = main_source.index('if job_kind == "build_scene":')
    assert gate_call < first_dispatch
    assert '"scene_spec_sha256"' in main_source


def test_inventory_gate_requires_exact_payload_digest_and_final_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_scene_script(monkeypatch)
    inventory = {
        "schema_version": 1,
        "map_path": "/Game/UEF/Scenes/fantasy/L_fantasy",
        "static_mesh_count": 2,
    }
    digest = script._canonical_digest(inventory)
    loaded: list[str] = []
    inventoried: list[tuple[str, str]] = []
    monkeypatch.setattr(script, "_load_level", loaded.append)

    def inventory_probe(root: str, map_path: str) -> dict[str, object]:
        inventoried.append((root, map_path))
        return dict(inventory)

    monkeypatch.setattr(script, "_inventory", inventory_probe)
    job = {
        "expected_inventory": dict(inventory),
        "expected_inventory_sha256": digest,
    }

    actual, actual_digest = script._require_inventory(job, "fantasy")

    assert actual == inventory
    assert actual_digest == digest
    assert loaded == ["/Game/UEF/Scenes/fantasy/L_fantasy"]
    assert inventoried == [("/Game/UEF/Scenes/fantasy", "/Game/UEF/Scenes/fantasy/L_fantasy")]

    with pytest.raises(RuntimeError, match="scene inventory changed after build"):
        script._require_inventory(
            {**job, "expected_inventory_sha256": "0" * 64},
            "fantasy",
        )
    with pytest.raises(RuntimeError, match="missing its approved inventory"):
        script._require_inventory(
            {"expected_inventory": inventory, "expected_inventory_sha256": "short"},
            "fantasy",
        )


def test_expected_import_counts_are_exact_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_scene_script(monkeypatch)
    inventory = {
        "static_mesh_count": 6,
        "material_count": 2,
        "texture_count": 0,
        "triangle_count": 10216,
    }

    script._validate_expected(
        inventory,
        {
            "mesh_count": 6,
            "material_count": 2,
            "texture_count": 0,
            "triangle_count": 10216,
        },
    )

    with pytest.raises(RuntimeError, match="scene triangle_count mismatch"):
        script._validate_expected(inventory, {"triangle_count": 10215})
    with pytest.raises(RuntimeError, match="scene texture_count mismatch"):
        script._validate_expected(inventory, {"texture_count": 1})


def test_reload_finalize_and_inspect_all_use_the_same_inventory_gate() -> None:
    assert "_require_inventory(job, scene_id)" in _function_source("_run_reload")
    assert "_require_inventory(job, scene_id)" in _function_source("_run_finalize")
    assert "_require_inventory(job, scene_id)" in _function_source("_run_inspect")


def test_candidate_promotion_reloads_and_exactly_compares_remapped_inventory() -> None:
    build_source = _function_source("_run_build")
    load_neutral = build_source.index("_load_neutral_level()")
    backup_move = build_source.index("_rename_directory(destination, backup, scene_id)")
    candidate_move = build_source.index(
        "_rename_directory(candidate, destination, scene_id, delete_source=False)"
    )
    load_final = build_source.index("_load_level(final_map)")
    remap = build_source.index("_remap_paths(candidate_inventory, candidate, destination)")
    equality_gate = build_source.index("if inventory != expected_after_move:")

    assert load_neutral < backup_move < candidate_move < load_final < remap < equality_gate
    assert (
        "_rename_directory(candidate, destination, scene_id, delete_source=False)" in build_source
    )
    assert '"status": "prepared"' in build_source
    assert '"state": "promoted_pending_validation"' in build_source


def test_finalize_deletes_deferred_candidate_before_transaction_commit() -> None:
    finalize_source = _function_source("_run_finalize")

    candidate_probe = finalize_source.index("does_directory_exist(candidate)")
    candidate_delete = finalize_source.index("_delete_directory(candidate)")
    backup_delete = finalize_source.index("_delete_directory(backup)")
    transaction_delete = finalize_source.index("_delete_directory(transaction)")

    assert candidate_probe < candidate_delete < backup_delete < transaction_delete


@pytest.mark.parametrize("had_backup", [False, True])
def test_rollback_restores_backup_or_removes_only_an_explicitly_new_destination(
    monkeypatch: pytest.MonkeyPatch,
    had_backup: bool,
) -> None:
    script = _load_scene_script(monkeypatch)
    destination, transaction, candidate, backup = script._paths("fantasy")
    existing = {destination, transaction, candidate}
    if had_backup:
        existing.add(backup)
    events: list[tuple[str, ...]] = []
    script.unreal = SimpleNamespace(
        EditorAssetLibrary=SimpleNamespace(
            does_directory_exist=lambda path: path in existing,
        )
    )
    monkeypatch.setattr(script, "_load_neutral_level", lambda: events.append(("neutral",)))

    def delete(path: str) -> bool:
        events.append(("delete", path))
        existing.discard(path)
        return True

    def rename(source: str, target: str, scene_id: str) -> None:
        assert scene_id == "fantasy"
        events.append(("rename", source, target))
        existing.discard(source)
        existing.add(target)

    monkeypatch.setattr(script, "_delete_directory", delete)
    monkeypatch.setattr(script, "_rename_directory", rename)

    result = script._rollback("fantasy", remove_new_destination=True)

    if had_backup:
        assert events[:3] == [
            ("neutral",),
            ("delete", destination),
            ("rename", backup, destination),
        ]
        assert result["restored_previous"] is True
    else:
        assert events[:2] == [("neutral",), ("delete", destination)]
        assert result["restored_previous"] is False
    assert events[-2:] == [("delete", candidate), ("delete", transaction)]
    assert result["removed_new_destination"] is True


def test_finalize_writes_prepared_evidence_before_irreversible_deletes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _load_scene_script(monkeypatch)
    destination, transaction, candidate, backup = script._paths("fantasy")
    inventory = {"map_path": f"{destination}/L_fantasy"}
    digest = script._canonical_digest(inventory)
    existing = {destination, transaction, backup}
    events: list[tuple[str, object]] = []
    script.unreal = SimpleNamespace(
        EditorAssetLibrary=SimpleNamespace(
            does_directory_exist=lambda path: path in existing,
        )
    )
    monkeypatch.setattr(script, "_require_inventory", lambda job, scene_id: (inventory, digest))
    monkeypatch.setattr(
        script,
        "_write_manifest",
        lambda path, payload: events.append(("write", dict(payload))),
    )

    def delete(path: str) -> bool:
        events.append(("delete", path))
        existing.discard(path)
        return True

    monkeypatch.setattr(script, "_delete_directory", delete)
    manifest: dict[str, object] = {"status": "running"}

    script._run_finalize({}, manifest, tmp_path / "manifest.json", "fantasy")

    assert events[0][0] == "write"
    prepared = cast(dict[str, Any], events[0][1])
    assert prepared["status"] == "prepared"
    assert prepared["transaction"]["state"] == "prepared"
    assert events[1:] == [("delete", backup), ("delete", transaction)]
    assert manifest["status"] == "ok"
    assert cast(dict[str, object], manifest["transaction"]) == {
        "state": "committed",
        "commit_confirmation": "transaction_deleted",
        "destination": destination,
        "removed_backup": True,
        "removed_transaction": True,
    }
    assert candidate not in existing


def test_finalize_is_idempotent_after_transaction_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_scene_script(monkeypatch)
    destination, _, _, _ = script._paths("fantasy")
    inventory = {"map_path": f"{destination}/L_fantasy"}
    digest = script._canonical_digest(inventory)
    script.unreal = SimpleNamespace(
        EditorAssetLibrary=SimpleNamespace(does_directory_exist=lambda path: False)
    )
    monkeypatch.setattr(script, "_require_inventory", lambda job, scene_id: (inventory, digest))
    monkeypatch.setattr(
        script,
        "_write_manifest",
        lambda path, payload: pytest.fail("already committed finalize must not prepare deletion"),
    )
    monkeypatch.setattr(
        script,
        "_delete_directory",
        lambda path: pytest.fail("already committed finalize must not delete anything"),
    )
    manifest: dict[str, object] = {}

    script._run_finalize({}, manifest, Path("unused.json"), "fantasy")

    assert manifest["status"] == "ok"
    assert cast(dict[str, object], manifest["transaction"])["commit_confirmation"] == (
        "already_committed"
    )


def test_main_dispatches_all_transaction_modes_and_defers_mutating_cleanup() -> None:
    main_source = _function_source("main")
    for job_kind in (
        "build_scene",
        "reload_scene",
        "finalize_scene",
        "inspect_scene_transaction",
        "rollback_scene",
    ):
        assert f'job_kind == "{job_kind}"' in main_source
    assert 'if job_kind == "build_scene":' in main_source
    assert "build may retain live Interchange/LevelInstance objects" in main_source
    assert "cleanup = _rollback(scene_id, remove_new)" not in main_source
    assert 'elif job_kind == "finalize_scene":' in main_source
    assert '"status": "deferred_to_host"' in main_source
    assert '"finalize may have crossed the commit point"' in main_source
