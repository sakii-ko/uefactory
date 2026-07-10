from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import typer
from typer.testing import CliRunner

from uefactory.catalog import SceneRecord
from uefactory.cli.scene import scene_app
from uefactory.core.config import Settings
from uefactory.scenes import SceneBuildError, SceneSpecError


def _app(tmp_path: Path) -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def root(ctx: typer.Context) -> None:
        ctx.obj = {"settings": Settings(project_root=tmp_path, data_dir=tmp_path / "data")}

    app.add_typer(scene_app, name="scene")
    return app


def _scene_spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "fantasy_diorama.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scene_id": "fantasy_diorama",
                "name": "Low Poly Fantasy Diorama",
                "kind": "interchange_scene",
                "source": {
                    "path": "assets/fantasy-diorama.glb",
                    "source": "blackmyth_asset_library",
                    "source_id": "f3266f252ea98fcc",
                    "source_url": "https://sketchfab.com/3d-models/f3266f252ea98fcc",
                    "license": "CC-BY-4.0",
                    "license_tier": "open",
                    "license_url": "https://creativecommons.org/licenses/by/4.0/",
                    "attribution": "Mesh-Base — Low Poly Fantasy Diorama",
                },
                "expected": {
                    "mesh_count": 6,
                    "material_count": 2,
                    "texture_count": 0,
                    "triangle_count": 10216,
                },
                "build": {
                    "map_path": ("/Game/UEF/Scenes/fantasy_diorama/L_fantasy_diorama"),
                    "export": True,
                },
                "camera": {
                    "rig": "overview_bounds",
                    "yaw": -35.0,
                    "pitch": -22.5,
                    "distance_multiplier": 1.35,
                },
                "render": {"no_auto_floor": True},
            }
        ),
        encoding="utf-8",
    )
    return path


def _scene_record(*, status: str = "built") -> SceneRecord:
    return SceneRecord(
        scene_id="fantasy_diorama",
        name="Low Poly Fantasy Diorama",
        source="blackmyth_asset_library",
        source_id="f3266f252ea98fcc",
        source_url="https://sketchfab.com/3d-models/f3266f252ea98fcc",
        license="CC-BY-4.0",
        license_tier="open",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution="Mesh-Base — Low Poly Fantasy Diorama",
        source_path="fantasy_diorama.json",
        source_file="/srv/blackmyth/assets/fantasy-diorama.glb",
        source_sha256="a" * 64,
        spec_sha256="b" * 64,
        build_sha256="d" * 64,
        status=status,
        map_path="/Game/UEF/Scenes/fantasy_diorama/L_fantasy_diorama",
        actor_count=8,
        static_mesh_count=6,
        triangle_count=10216,
        material_count=2,
        texture_count=0,
        bounds={
            "min": [-100.0, -50.0, 0.0],
            "max": [100.0, 50.0, 200.0],
            "size": [200.0, 100.0, 200.0],
        },
        error=None,
        created_at="2026-07-10T12:00:00Z",
        updated_at="2026-07-10T12:00:00Z",
    )


def test_scene_validate_cli_emits_canonical_json(tmp_path: Path) -> None:
    spec_path = _scene_spec_file(tmp_path)

    result = CliRunner().invoke(
        _app(tmp_path),
        ["scene", "validate", str(spec_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["scene_id"] == "fantasy_diorama"
    assert payload["build"] == {
        "map_path": "/Game/UEF/Scenes/fantasy_diorama/L_fantasy_diorama",
        "export": True,
    }
    assert payload["render"] == {"no_auto_floor": True}
    assert len(payload["spec_sha256"]) == 64


def test_scene_build_cli_emits_json_and_forwards_options(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    spec_path = _scene_spec_file(tmp_path)
    run_dir = tmp_path / "out/custom/run/fantasy_diorama"
    scene = _scene_record()
    calls: list[dict[str, Any]] = []
    packages = (
        {
            "object_path": "/Game/UEF/Scenes/fantasy_diorama/L_fantasy_diorama",
            "path": "ue/UEFBase/Content/UEF/Scenes/fantasy_diorama/L_fantasy_diorama.umap",
            "size": 1234,
            "sha256": "e" * 64,
        },
    )

    def fake_build_scene(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(
            scene=scene,
            run_dir=run_dir,
            manifest_path=run_dir / "manifest.json",
            catalog_path=tmp_path / "data/custom.db",
            inventory_sha256="c" * 64,
            packages=packages,
            package_bundle_sha256="f" * 64,
            build_sha256=scene.build_sha256,
        )

    monkeypatch.setattr("uefactory.cli.scene.build_scene", fake_build_scene)

    result = CliRunner().invoke(
        _app(tmp_path),
        [
            "scene",
            "build",
            str(spec_path),
            "--database",
            "data/custom.db",
            "--out",
            "out/custom",
            "--timeout-sec",
            "17",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "built",
        "scene": scene.as_dict(),
        "run_dir": str(run_dir),
        "manifest": str(run_dir / "manifest.json"),
        "catalog": str(tmp_path / "data/custom.db"),
        "inventory_sha256": "c" * 64,
        "packages": list(packages),
        "package_bundle_sha256": "f" * 64,
        "build_sha256": "d" * 64,
    }
    assert calls == [
        {
            "settings": calls[0]["settings"],
            "spec_path": spec_path,
            "database_path": Path("data/custom.db"),
            "out_root": tmp_path / "out/custom",
            "timeout_sec": 17,
        }
    ]


def test_scene_thumbnail_cli_emits_json(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    scene = _scene_record(status="render_ok")
    render_dir = tmp_path / "out/scene_thumbnails/run/fantasy_diorama"
    calls: list[dict[str, Any]] = []

    def fake_thumbnail(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(
            scene=scene,
            thumbnail_path=render_dir / "thumbnail.png",
            subject_mask_path=render_dir / "subject_mask.png",
            render=SimpleNamespace(
                manifest_path=render_dir / "manifest.json",
                artifacts=SimpleNamespace(contact_sheet=render_dir / "contact_sheet.png"),
            ),
            catalog_path=tmp_path / "data/catalog.db",
            artifact_ids=("fantasy_diorama_thumb_beauty", "fantasy_diorama_thumb_mask"),
        )

    monkeypatch.setattr("uefactory.cli.scene.thumbnail_catalog_scene", fake_thumbnail)

    result = CliRunner().invoke(
        _app(tmp_path),
        [
            "scene",
            "thumbnail",
            "fantasy_diorama",
            "--timeout-sec",
            "23",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "status": "render_ok",
        "scene_id": "fantasy_diorama",
        "thumbnail": str(render_dir / "thumbnail.png"),
        "subject_mask": str(render_dir / "subject_mask.png"),
        "render_manifest": str(render_dir / "manifest.json"),
        "contact_sheet": str(render_dir / "contact_sheet.png"),
        "catalog": str(tmp_path / "data/catalog.db"),
        "artifact_ids": [
            "fantasy_diorama_thumb_beauty",
            "fantasy_diorama_thumb_mask",
        ],
    }
    assert calls[0]["scene_id"] == "fantasy_diorama"
    assert calls[0]["database_path"] is None
    assert calls[0]["timeout_sec"] == 23


def test_scene_validate_cli_maps_scene_spec_error_to_exit_two(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    spec_path = _scene_spec_file(tmp_path)

    def invalid_spec(path: Path) -> None:
        del path
        raise SceneSpecError("$.name: expected non-empty string")

    monkeypatch.setattr("uefactory.cli.scene.load_scene_spec", invalid_spec)

    result = CliRunner().invoke(_app(tmp_path), ["scene", "validate", str(spec_path)])

    assert result.exit_code == 2
    assert "Invalid scene spec" in result.output
    assert "$.name: expected non-empty string" in result.output


def test_scene_build_cli_maps_scene_spec_error_to_exit_two(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    spec_path = _scene_spec_file(tmp_path)

    def invalid_spec(**kwargs: Any) -> None:
        del kwargs
        raise SceneSpecError("$.source.path: missing source")

    monkeypatch.setattr("uefactory.cli.scene.build_scene", invalid_spec)

    result = CliRunner().invoke(_app(tmp_path), ["scene", "build", str(spec_path)])

    assert result.exit_code == 2
    assert "Invalid scene spec" in result.output
    assert "$.source.path: missing source" in result.output


def test_scene_build_cli_maps_scene_build_error_to_exit_one(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    spec_path = _scene_spec_file(tmp_path)
    failure_manifest = tmp_path / "out/scene_builds/failed/manifest.json"

    def failed_build(**kwargs: Any) -> None:
        del kwargs
        raise SceneBuildError(
            manifest_path=failure_manifest,
            cause=RuntimeError("synthetic UE failure"),
        )

    monkeypatch.setattr("uefactory.cli.scene.build_scene", failed_build)

    result = CliRunner().invoke(_app(tmp_path), ["scene", "build", str(spec_path)])

    assert result.exit_code == 1
    assert "Scene build failed (RuntimeError): synthetic UE failure" in result.output
    assert f"manifest={failure_manifest}" in result.output
