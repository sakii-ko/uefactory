from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

import uefactory.scenes.package_evidence as package_evidence_module
from uefactory.scenes.package_evidence import (
    ScenePackageEvidenceError,
    collect_scene_package_evidence,
    require_valid_scene_package_evidence,
    scene_package_bundle_sha256,
)

SCENE_ID = "test_scene"


def _fixture(project_root: Path) -> tuple[dict[str, Any], Path, Path]:
    package_root = project_root / f"ue/UEFBase/Content/UEF/Scenes/{SCENE_ID}"
    map_file = package_root / "L_test_scene.umap"
    mesh_file = package_root / "Assets/SM_Diorama.uasset"
    mesh_file.parent.mkdir(parents=True)
    map_file.write_bytes(b"persistent map bytes")
    mesh_file.write_bytes(b"persistent mesh bytes")
    inventory = {
        "assets": [
            {
                "object_path": f"/Game/UEF/Scenes/{SCENE_ID}/Assets/SM_Diorama.SM_Diorama",
                "class": "StaticMesh",
            },
            {
                "object_path": f"/Game/UEF/Scenes/{SCENE_ID}/L_test_scene.L_test_scene",
                "class": "World",
            },
        ]
    }
    return inventory, map_file, mesh_file


def test_scene_package_evidence_covers_complete_tree_and_revalidates(tmp_path: Path) -> None:
    inventory, map_file, mesh_file = _fixture(tmp_path)

    evidence = collect_scene_package_evidence(
        tmp_path,
        scene_id=SCENE_ID,
        inventory=inventory,
    )

    assert [item["object_path"] for item in evidence] == [
        f"/Game/UEF/Scenes/{SCENE_ID}/Assets/SM_Diorama.SM_Diorama",
        f"/Game/UEF/Scenes/{SCENE_ID}/L_test_scene.L_test_scene",
    ]
    assert {item["path"] for item in evidence} == {
        mesh_file.relative_to(tmp_path).as_posix(),
        map_file.relative_to(tmp_path).as_posix(),
    }
    assert all(item["size"] > 0 and len(item["sha256"]) == 64 for item in evidence)
    assert (
        require_valid_scene_package_evidence(
            tmp_path,
            scene_id=SCENE_ID,
            inventory=inventory,
            packages=list(evidence),
        )
        == evidence
    )
    assert len(scene_package_bundle_sha256(evidence)) == 64


def test_scene_package_evidence_rejects_unrecorded_file(tmp_path: Path) -> None:
    inventory, map_file, _ = _fixture(tmp_path)
    (map_file.parent / "unrecorded.ubulk").write_bytes(b"extra package sidecar")

    with pytest.raises(ScenePackageEvidenceError, match="files outside its inventory.*extra="):
        collect_scene_package_evidence(tmp_path, scene_id=SCENE_ID, inventory=inventory)


def test_scene_package_evidence_includes_known_package_sidecars(tmp_path: Path) -> None:
    inventory, _, mesh_file = _fixture(tmp_path)
    sidecar = mesh_file.with_suffix(".ubulk")
    sidecar.write_bytes(b"bulk payload bytes")

    evidence = collect_scene_package_evidence(
        tmp_path,
        scene_id=SCENE_ID,
        inventory=inventory,
    )

    sidecar_evidence = [item for item in evidence if item["path"].endswith(".ubulk")]
    assert sidecar_evidence == [
        {
            "object_path": f"/Game/UEF/Scenes/{SCENE_ID}/Assets/SM_Diorama.SM_Diorama",
            "class": "StaticMesh",
            "path": sidecar.relative_to(tmp_path).as_posix(),
            "size": len(b"bulk payload bytes"),
            "sha256": hashlib.sha256(b"bulk payload bytes").hexdigest(),
            "sidecar_suffix": ".ubulk",
        }
    ]
    sidecar.write_bytes(b"changed bulk payload")
    with pytest.raises(ScenePackageEvidenceError, match="bytes or file inventory changed"):
        require_valid_scene_package_evidence(
            tmp_path,
            scene_id=SCENE_ID,
            inventory=inventory,
            packages=list(evidence),
        )


def test_scene_package_evidence_rejects_missing_or_empty_inventory_file(tmp_path: Path) -> None:
    inventory, _, mesh_file = _fixture(tmp_path)
    mesh_file.unlink()
    with pytest.raises(ScenePackageEvidenceError, match="omits required package files.*missing="):
        collect_scene_package_evidence(tmp_path, scene_id=SCENE_ID, inventory=inventory)

    mesh_file.touch()
    with pytest.raises(ScenePackageEvidenceError, match="empty or non-regular"):
        collect_scene_package_evidence(tmp_path, scene_id=SCENE_ID, inventory=inventory)


def test_scene_package_evidence_detects_changed_recorded_bytes(tmp_path: Path) -> None:
    inventory, _, mesh_file = _fixture(tmp_path)
    evidence = collect_scene_package_evidence(
        tmp_path,
        scene_id=SCENE_ID,
        inventory=inventory,
    )
    mesh_file.write_bytes(b"changed persistent mesh bytes")

    with pytest.raises(ScenePackageEvidenceError, match="bytes or file inventory changed"):
        require_valid_scene_package_evidence(
            tmp_path,
            scene_id=SCENE_ID,
            inventory=inventory,
            packages=list(evidence),
        )


def test_scene_package_evidence_rejects_file_and_directory_symlinks(tmp_path: Path) -> None:
    inventory, map_file, mesh_file = _fixture(tmp_path)
    external = tmp_path / "external.uasset"
    external.write_bytes(b"external bytes")
    mesh_file.unlink()
    mesh_file.symlink_to(external)
    with pytest.raises(ScenePackageEvidenceError, match="symbolic links"):
        collect_scene_package_evidence(tmp_path, scene_id=SCENE_ID, inventory=inventory)

    mesh_file.unlink()
    assets_dir = mesh_file.parent
    assets_dir.rmdir()
    external_dir = tmp_path / "external_assets"
    external_dir.mkdir()
    (external_dir / mesh_file.name).write_bytes(b"external mesh")
    assets_dir.symlink_to(external_dir, target_is_directory=True)
    assert map_file.is_file()
    with pytest.raises(ScenePackageEvidenceError, match="symbolic links|symbolic directory"):
        collect_scene_package_evidence(tmp_path, scene_id=SCENE_ID, inventory=inventory)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO support")
def test_scene_package_evidence_rejects_non_regular_entries(tmp_path: Path) -> None:
    inventory, map_file, _ = _fixture(tmp_path)
    os.mkfifo(map_file.parent / "unexpected.pipe")

    with pytest.raises(ScenePackageEvidenceError, match="non-regular entry"):
        collect_scene_package_evidence(tmp_path, scene_id=SCENE_ID, inventory=inventory)


def test_scene_package_evidence_detects_mutation_between_hash_passes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inventory, _, mesh_file = _fixture(tmp_path)
    original = package_evidence_module._file_evidence
    calls = 0

    def mutate_after_first_pass(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            mesh_file.write_bytes(b"mutated between evidence passes")
        return result

    monkeypatch.setattr(package_evidence_module, "_file_evidence", mutate_after_first_pass)

    with pytest.raises(ScenePackageEvidenceError, match="bytes changed"):
        collect_scene_package_evidence(tmp_path, scene_id=SCENE_ID, inventory=inventory)


def test_scene_package_evidence_rejects_inventory_path_escape(tmp_path: Path) -> None:
    inventory, _, _ = _fixture(tmp_path)
    inventory["assets"][0]["object_path"] = "/Game/UEF/Scenes/other_scene/Escape.Escape"

    with pytest.raises(ScenePackageEvidenceError, match="must stay inside"):
        collect_scene_package_evidence(tmp_path, scene_id=SCENE_ID, inventory=inventory)
