from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import uefactory.ingest.package_evidence as package_evidence_module
from uefactory.ingest.package_evidence import (
    PACKAGE_BUNDLE_EVIDENCE_POLICY,
    PackageBundleEvidenceError,
    collect_package_bundle_evidence,
    is_valid_package_bundle_evidence,
    package_bundle_sha256,
    require_valid_package_bundle_evidence,
)


def _bundle(tmp_path: Path) -> tuple[Path, str, list[str]]:
    project_root = tmp_path / "project"
    asset_id = "test_asset"
    package_root = project_root / f"ue/UEFBase/Content/UEF/Ingested/{asset_id}"
    package_root.mkdir(parents=True)
    (package_root / "SM_Test.uasset").write_bytes(b"static mesh package")
    (package_root / "SM_Test.uexp").write_bytes(b"static mesh exports")
    nested = package_root / "Materials"
    nested.mkdir()
    (nested / "M_Test.uasset").write_bytes(b"material package")
    imported_paths = [
        f"/Game/UEF/Ingested/{asset_id}/SM_Test.SM_Test",
        f"/Game/UEF/Ingested/{asset_id}/Materials/M_Test.M_Test",
    ]
    return project_root, asset_id, imported_paths


def test_collect_package_bundle_evidence_covers_complete_recursive_file_set(
    tmp_path: Path,
) -> None:
    project_root, asset_id, imported_paths = _bundle(tmp_path)

    evidence = collect_package_bundle_evidence(
        project_root,
        asset_id=asset_id,
        imported_object_paths=imported_paths,
    )

    assert evidence["policy"] == PACKAGE_BUNDLE_EVIDENCE_POLICY
    assert evidence["root"] == "ue/UEFBase/Content/UEF/Ingested/test_asset"
    assert [item["path"] for item in evidence["files"]] == [
        "ue/UEFBase/Content/UEF/Ingested/test_asset/Materials/M_Test.uasset",
        "ue/UEFBase/Content/UEF/Ingested/test_asset/SM_Test.uasset",
        "ue/UEFBase/Content/UEF/Ingested/test_asset/SM_Test.uexp",
    ]
    assert all(type(item["size"]) is int and item["size"] > 0 for item in evidence["files"])
    assert all(len(str(item["sha256"])) == 64 for item in evidence["files"])
    canonical = {key: evidence[key] for key in ("policy", "root", "files")}
    assert evidence["package_bundle_sha256"] == package_bundle_sha256(canonical)
    undomained = hashlib.sha256(
        json.dumps(
            canonical,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert evidence["package_bundle_sha256"] != undomained
    assert is_valid_package_bundle_evidence(
        project_root,
        asset_id=asset_id,
        imported_object_paths=imported_paths,
        evidence=evidence,
    )


@pytest.mark.parametrize("mutation", ["changed", "missing", "additional"])
def test_package_bundle_validation_rejects_any_file_set_or_byte_change(
    tmp_path: Path,
    mutation: str,
) -> None:
    project_root, asset_id, imported_paths = _bundle(tmp_path)
    evidence = collect_package_bundle_evidence(
        project_root,
        asset_id=asset_id,
        imported_object_paths=imported_paths,
    )
    package_root = project_root / evidence["root"]
    if mutation == "changed":
        (package_root / "SM_Test.uasset").write_bytes(b"tampered package bytes")
    elif mutation == "missing":
        (package_root / "SM_Test.uexp").unlink()
    else:
        (package_root / "new.ubulk").write_bytes(b"unexpected bulk data")

    assert not is_valid_package_bundle_evidence(
        project_root,
        asset_id=asset_id,
        imported_object_paths=imported_paths,
        evidence=evidence,
    )
    with pytest.raises(PackageBundleEvidenceError, match="changed"):
        require_valid_package_bundle_evidence(
            project_root,
            asset_id=asset_id,
            imported_object_paths=imported_paths,
            evidence=evidence,
        )


@pytest.mark.parametrize("link_kind", ["file", "directory"])
def test_package_bundle_collection_rejects_every_symlink(
    tmp_path: Path,
    link_kind: str,
) -> None:
    project_root, asset_id, imported_paths = _bundle(tmp_path)
    package_root = project_root / f"ue/UEFBase/Content/UEF/Ingested/{asset_id}"
    outside = tmp_path / "outside"
    if link_kind == "file":
        outside.write_bytes(b"outside file")
        (package_root / "outside.uasset").symlink_to(outside)
    else:
        outside.mkdir()
        (outside / "outside.uasset").write_bytes(b"outside file")
        (package_root / "Outside").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PackageBundleEvidenceError, match="symbolic"):
        collect_package_bundle_evidence(
            project_root,
            asset_id=asset_id,
            imported_object_paths=imported_paths,
        )


def test_package_bundle_collection_rejects_symlink_in_directory_chain(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    outside = tmp_path / "outside"
    outside.mkdir()
    (project_root / "ue/UEFBase/Content").mkdir(parents=True)
    (project_root / "ue/UEFBase/Content/UEF").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PackageBundleEvidenceError, match="symbolic directory"):
        collect_package_bundle_evidence(
            project_root,
            asset_id="test_asset",
            imported_object_paths=["/Game/UEF/Ingested/test_asset/SM_Test.SM_Test"],
        )


def test_package_bundle_collection_detects_same_path_rewrite_between_hash_passes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, asset_id, imported_paths = _bundle(tmp_path)
    package = project_root / "ue/UEFBase/Content/UEF/Ingested/test_asset/SM_Test.uasset"
    original = package_evidence_module._file_evidence
    call_count = 0

    def mutate_after_first_pass(root: Path, path: Path) -> dict[str, str | int]:
        nonlocal call_count
        result = original(root, path)
        call_count += 1
        if call_count == 3:
            package.write_bytes(b"same path, different inode generation")
        return result

    monkeypatch.setattr(package_evidence_module, "_file_evidence", mutate_after_first_pass)

    with pytest.raises(PackageBundleEvidenceError, match="bytes changed"):
        collect_package_bundle_evidence(
            project_root,
            asset_id=asset_id,
            imported_object_paths=imported_paths,
        )


def test_package_bundle_collection_wraps_safe_open_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, asset_id, imported_paths = _bundle(tmp_path)

    def failed_open(path: Path, flags: int) -> int:
        del path, flags
        raise OSError("synthetic O_NOFOLLOW failure")

    monkeypatch.setattr(package_evidence_module.os, "open", failed_open)

    with pytest.raises(PackageBundleEvidenceError, match="could not safely open"):
        collect_package_bundle_evidence(
            project_root,
            asset_id=asset_id,
            imported_object_paths=imported_paths,
        )


def test_package_bundle_evidence_requires_every_imported_object_uasset(tmp_path: Path) -> None:
    project_root, asset_id, imported_paths = _bundle(tmp_path)
    (project_root / "ue/UEFBase/Content/UEF/Ingested/test_asset/Materials/M_Test.uasset").unlink()

    with pytest.raises(PackageBundleEvidenceError, match="omits imported-object"):
        collect_package_bundle_evidence(
            project_root,
            asset_id=asset_id,
            imported_object_paths=imported_paths,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"unexpected": True}),
        lambda value: value.__setitem__("root", "../escape"),
        lambda value: value["files"][0].__setitem__("size", True),
        lambda value: value["files"].reverse(),
        lambda value: value.__setitem__("package_bundle_sha256", "0" * 64),
    ],
)
def test_package_bundle_evidence_rejects_noncanonical_or_forged_payload(
    tmp_path: Path,
    mutate: Any,
) -> None:
    project_root, asset_id, imported_paths = _bundle(tmp_path)
    evidence = collect_package_bundle_evidence(
        project_root,
        asset_id=asset_id,
        imported_object_paths=imported_paths,
    )
    forged = json.loads(json.dumps(evidence))
    mutate(forged)

    assert not is_valid_package_bundle_evidence(
        project_root,
        asset_id=asset_id,
        imported_object_paths=imported_paths,
        evidence=forged,
    )
