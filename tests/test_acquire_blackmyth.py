from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from uefactory.acquire.blackmyth import (
    BlackMythLibraryError,
    SceneLibraryScan,
    research_only_external_glb,
    scan_blackmyth_scene_library,
)
from uefactory.cli.acquire import acquire_app


def _cli_app() -> typer.Typer:
    app = typer.Typer()
    app.add_typer(acquire_app, name="acquire")
    return app


def _glb(document: dict[str, Any] | None = None) -> bytes:
    value = document or {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{}]}
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    encoded += b" " * (-len(encoded) % 4)
    chunk = struct.pack("<II", len(encoded), 0x4E4F534A) + encoded
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunk)) + chunk


def _library(root: Path) -> None:
    (root / "asset-library/manifests").mkdir(parents=True)
    (root / "asset-library/derived").mkdir()


def _scene(
    root: Path,
    *,
    uid: str,
    license: str = "CC-BY-4.0",
    glb: bytes | None = None,
    glb_path: str | None = None,
    redistributable: bool = True,
    category: str = "scene",
) -> tuple[Path, Path]:
    relative_glb = glb_path or f"derived/{uid}/{uid}.glb"
    manifest_path = root / f"asset-library/manifests/{uid}.meta.json"
    manifest_path.write_text(
        json.dumps(
            {
                "uid": uid,
                "source_id": f"source-{uid}",
                "source_url": f"https://example.test/scenes/{uid}",
                "author": "Fixture Artist",
                "asset_name": f"Fixture {uid}",
                "category": category,
                "license": license,
                "is_redistributable": redistributable,
                "normalized_formats": {"glb": relative_glb},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    destination = root / "asset-library" / relative_glb
    if glb is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(glb)
    return manifest_path, destination


def test_blackmyth_scan_cli_requires_an_explicit_root() -> None:
    result = CliRunner().invoke(_cli_app(), ["acquire", "blackmyth"])

    assert result.exit_code == 2
    assert "ROOT" in result.output


def test_blackmyth_scan_cli_forwards_the_explicit_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "blackmyth"
    root.mkdir()
    calls: list[Path] = []

    def fake_scan(value: Path) -> SceneLibraryScan:
        calls.append(value)
        return SceneLibraryScan(root=root.resolve(), records=(), quarantined=())

    monkeypatch.setattr("uefactory.cli.acquire.scan_blackmyth_scene_library", fake_scan)

    result = CliRunner().invoke(
        _cli_app(),
        ["acquire", "blackmyth", str(root), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [root]
    assert json.loads(result.stdout)["root"] == str(root.resolve())


def test_scan_returns_stable_checked_records_for_supported_license_tiers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    _library(root)
    expected = {
        "a_cc0": ("CC0-1.0", "open", True),
        "b_by": ("CC-BY-4.0", "open", True),
        "c_share_alike": ("CC-BY-SA-4.0", "open", True),
        "d_noncommercial": ("CC-BY-NC-SA-4.0", "nc", False),
    }
    glb = _glb()
    for uid, (license_id, _, redistributable) in expected.items():
        source_license = "CC0" if license_id == "CC0-1.0" else license_id
        _scene(
            root,
            uid=uid,
            license=source_license,
            glb=glb,
            redistributable=redistributable,
        )

    result = scan_blackmyth_scene_library(root)

    assert result.root == root.resolve()
    assert not result.quarantined
    assert [record.library_uid for record in result.records] == list(expected)
    for record in result.records:
        license_id, tier, redistributable = expected[record.library_uid]
        assert record.source == "blackmyth_asset_library"
        assert record.license == license_id
        assert record.license_tier == tier
        assert record.redistributable is redistributable
        assert record.sha256 == hashlib.sha256(glb).hexdigest()
        assert record.bytes == len(glb)
        assert record.glb_path.is_absolute()
        assert record.manifest_path is not None
        assert record.manifest_path.is_absolute()
        assert len(record.canonical_digest) == 64
        assert record.as_dict()["glb_path"] == str(record.glb_path)


def test_record_digest_is_independent_of_library_location(tmp_path: Path) -> None:
    digests: list[str] = []
    for directory in ("first", "second"):
        root = tmp_path / directory
        _library(root)
        _scene(root, uid="stable_scene", glb=_glb())
        digests.append(scan_blackmyth_scene_library(root).records[0].canonical_digest)

    assert digests[0] == digests[1]


def test_scan_quarantines_unsupported_missing_invalid_and_external_glbs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    _library(root)
    _scene(root, uid="a_unknown", license="Unknown-Proprietary", glb=_glb())
    _scene(root, uid="b_store_terms", license="Sketchfab-Free-Standard", glb=_glb())
    _scene(root, uid="c_missing", glb=None)
    _scene(root, uid="d_invalid", glb=b"not-a-glb")
    _scene(
        root,
        uid="e_external",
        glb=_glb(
            {
                "asset": {"version": "2.0"},
                "images": [{"uri": "textures/albedo.png"}],
            }
        ),
    )

    result = scan_blackmyth_scene_library(root)

    assert not result.records
    assert [(item.library_uid, item.reason) for item in result.quarantined] == [
        ("a_unknown", "unsupported_license"),
        ("b_store_terms", "unsupported_license"),
        ("c_missing", "missing_glb"),
        ("d_invalid", "invalid_glb"),
        ("e_external", "external_glb"),
    ]


def test_scan_ignores_non_scene_manifests(tmp_path: Path) -> None:
    root = tmp_path / "library"
    _library(root)
    _scene(
        root,
        uid="character_fixture",
        category="character",
        license="Unknown-Proprietary",
        glb=_glb(),
    )

    result = scan_blackmyth_scene_library(root)

    assert not result.records
    assert not result.quarantined


def test_scan_rejects_derived_path_escape_without_opening_it(tmp_path: Path) -> None:
    root = tmp_path / "library"
    _library(root)
    _scene(root, uid="escape_scene", glb_path="../outside.glb")

    with pytest.raises(BlackMythLibraryError, match="escapes its approved root"):
        scan_blackmyth_scene_library(root)


def test_scan_rejects_symlinked_glb(tmp_path: Path) -> None:
    root = tmp_path / "library"
    _library(root)
    _, glb_path = _scene(root, uid="linked_scene")
    target = tmp_path / "target.glb"
    target.write_bytes(_glb())
    glb_path.parent.mkdir(parents=True)
    glb_path.symlink_to(target)

    with pytest.raises(BlackMythLibraryError, match="symlink"):
        scan_blackmyth_scene_library(root)


def test_scan_rejects_symlinked_manifest(tmp_path: Path) -> None:
    root = tmp_path / "library"
    _library(root)
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    (root / "asset-library/manifests/linked.meta.json").symlink_to(target)

    with pytest.raises(BlackMythLibraryError, match="symlink"):
        scan_blackmyth_scene_library(root)


@pytest.mark.parametrize(
    "component",
    [
        ".env",
        "bmw_key.txt",
        "mapping.usmap",
    ],
)
def test_scan_rejects_sensitive_path_without_disclosing_it(
    tmp_path: Path,
    component: str,
) -> None:
    root = tmp_path / "library"
    _library(root)
    _scene(
        root,
        uid="sensitive_scene",
        glb_path=f"derived/sensitive_scene/{component}/scene.glb",
    )

    with pytest.raises(BlackMythLibraryError) as caught:
        scan_blackmyth_scene_library(root)

    assert component not in str(caught.value)
    assert "sensitive" in str(caught.value)


def test_scan_rejects_duplicate_manifest_keys(tmp_path: Path) -> None:
    root = tmp_path / "library"
    _library(root)
    manifest = root / "asset-library/manifests/duplicate.meta.json"
    manifest.write_text(
        '{"uid":"duplicate","uid":"duplicate","category":"scene"}',
        encoding="utf-8",
    )

    with pytest.raises(BlackMythLibraryError, match="strict JSON"):
        scan_blackmyth_scene_library(root)


def test_research_only_external_glb_is_checked_and_never_redistributable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extracted"
    root.mkdir()
    payload = _glb()
    source = root / "temple/component.glb"
    source.parent.mkdir()
    source.write_bytes(payload)

    record = research_only_external_glb(
        root=root,
        glb_path=Path("temple/component.glb"),
        source_id="bmw-temple-component",
        title="Temple Component",
        source_url="https://example.test/research-provenance",
    )

    assert record.source == "blackmyth_research"
    assert record.license == "LicenseRef-Research-Only"
    assert record.license_tier == "nc"
    assert record.redistributable is False
    assert record.manifest_path is None
    assert record.sha256 == hashlib.sha256(payload).hexdigest()
    assert record.bytes == len(payload)
    assert record.glb_path == source.resolve()
    assert len(record.canonical_digest) == 64


def test_research_only_digest_is_independent_of_root_location(tmp_path: Path) -> None:
    digests: list[str] = []
    for directory in ("first", "second"):
        root = tmp_path / directory
        root.mkdir()
        (root / "same.glb").write_bytes(_glb())
        digests.append(
            research_only_external_glb(
                root=root,
                glb_path=Path("same.glb"),
                source_id="same-source",
                title="Same Source",
            ).canonical_digest
        )

    assert digests[0] == digests[1]


def test_research_only_external_glb_rejects_escape_symlink_and_external_data(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.glb"
    outside.write_bytes(_glb())

    with pytest.raises(BlackMythLibraryError, match="escapes its approved root"):
        research_only_external_glb(
            root=root,
            glb_path=outside,
            source_id="outside",
            title="Outside",
        )

    link = root / "linked.glb"
    os.symlink(outside, link)
    with pytest.raises(BlackMythLibraryError, match="symlink"):
        research_only_external_glb(
            root=root,
            glb_path=link,
            source_id="linked",
            title="Linked",
        )

    external = root / "external.glb"
    external.write_bytes(
        _glb(
            {
                "asset": {"version": "2.0"},
                "buffers": [{"byteLength": 4, "uri": "mesh.bin"}],
            }
        )
    )
    with pytest.raises(BlackMythLibraryError, match="external dependencies"):
        research_only_external_glb(
            root=root,
            glb_path=external,
            source_id="external",
            title="External",
        )
