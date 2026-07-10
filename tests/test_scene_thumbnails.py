from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from PIL import Image

from uefactory.catalog import (
    Catalog,
    SceneArtifactUpsert,
    SceneObjectUpsert,
    SceneRecord,
    SceneUpsert,
)
from uefactory.core.config import Settings
from uefactory.render.job import _render_scene_payload
from uefactory.render.jobspec import load_jobspec
from uefactory.scenes.locking import SceneLockError, scene_lock
from uefactory.scenes.thumbnails import thumbnail_catalog_scene

SCENE_ID = "forest_scene"
MAP_PATH = f"/Game/UEF/Scenes/{SCENE_ID}/L_{SCENE_ID}"
MAP_OBJECT_PATH = f"{MAP_PATH}.L_{SCENE_ID}"
MESH_PATH = f"/Game/UEF/Scenes/{SCENE_ID}/Assets/SM_Forest.SM_Forest"
MATERIAL_PATHS = (
    f"/Game/UEF/Scenes/{SCENE_ID}/Assets/M_ForestBark.M_ForestBark",
    f"/Game/UEF/Scenes/{SCENE_ID}/Assets/M_ForestLeaves.M_ForestLeaves",
)
TEXTURE_PATH = f"/Game/UEF/Scenes/{SCENE_ID}/Assets/T_Forest.T_Forest"
SOURCE_BYTES = b"scene source fixture"
SOURCE_SHA256 = hashlib.sha256(SOURCE_BYTES).hexdigest()
BOUNDS = {
    "min": [-100.0, -50.0, 0.0],
    "max": [100.0, 50.0, 200.0],
    "size": [200.0, 100.0, 200.0],
}
SCENE_SPEC = {
    "schema_version": 1,
    "scene_id": SCENE_ID,
    "name": "Forest Scene",
    "kind": "interchange_scene",
    "source": {
        "path": f"../data/scenes/{SCENE_ID}.glb",
        "source": "blackmyth_asset_library",
        "source_id": "forest-source",
        "source_url": "https://example.test/scenes/forest",
        "license": "CC-BY-4.0",
        "license_tier": "open",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "Fixture Artist",
    },
    "expected": {
        "mesh_count": 1,
        "triangle_count": 24,
        "material_count": 2,
        "texture_count": 1,
    },
    "build": {"map_path": MAP_PATH, "export": True},
    "camera": {
        "rig": "overview_bounds",
        "yaw": -35.0,
        "pitch": -22.5,
        "distance_multiplier": 1.25,
    },
    "render": {"no_auto_floor": True},
}


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


SPEC_SHA256 = _canonical_digest(SCENE_SPEC)


def _settings(tmp_path: Path) -> Settings:
    project_root = tmp_path / "project"
    project_root.mkdir()
    return Settings(
        project_root=project_root,
        data_dir=project_root / "data",
        log_dir=project_root / "logs",
    )


def _scene(
    *,
    status: str = "built",
    source_file: Path | None = None,
    build_sha256: str | None = None,
) -> SceneUpsert:
    built = status in {"built", "render_ok"}
    return SceneUpsert(
        scene_id=SCENE_ID,
        name="Forest Scene",
        source="blackmyth_asset_library",
        source_id="forest-source",
        source_url="https://example.test/scenes/forest",
        license="CC-BY-4.0",
        license_tier="open",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution="Fixture Artist",
        source_path=f"specs/{SCENE_ID}.yaml",
        source_file=source_file if built else None,
        source_sha256=SOURCE_SHA256,
        spec_sha256=SPEC_SHA256,
        build_sha256=build_sha256 if built else None,
        status=status,
        map_path=MAP_PATH if built else None,
        actor_count=1 if built else None,
        static_mesh_count=1 if built else None,
        triangle_count=24 if built else None,
        material_count=2 if built else None,
        texture_count=1 if built else None,
        bounds=BOUNDS if built else None,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ue_package_file(project_root: Path, object_path: str, *, suffix: str) -> Path:
    package = object_path.partition(".")[0].removeprefix("/Game/")
    return project_root / "ue/UEFBase/Content" / f"{package}{suffix}"


def _built_catalog(settings: Settings) -> tuple[Catalog, SceneRecord, Path]:
    source = settings.project_root / f"data/scenes/{SCENE_ID}.glb"
    source.parent.mkdir(parents=True)
    source.write_bytes(SOURCE_BYTES)
    spec_path = settings.project_root / f"specs/{SCENE_ID}.yaml"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(yaml.safe_dump(SCENE_SPEC, sort_keys=False), encoding="utf-8")

    package_assets = sorted(
        (
            *((path, "MaterialInstanceConstant") for path in MATERIAL_PATHS),
            (MESH_PATH, "StaticMesh"),
            (TEXTURE_PATH, "Texture2D"),
            (MAP_OBJECT_PATH, "World"),
        ),
        key=lambda item: item[0],
    )
    packages: list[dict[str, object]] = []
    for object_path, class_name in package_assets:
        package_file = _ue_package_file(
            settings.project_root,
            object_path,
            suffix=".umap" if class_name == "World" else ".uasset",
        )
        package_file.parent.mkdir(parents=True, exist_ok=True)
        package_file.write_bytes(f"{class_name}:{object_path}".encode())
        packages.append(
            {
                "object_path": object_path,
                "class": class_name,
                "path": package_file.relative_to(settings.project_root).as_posix(),
                "size": package_file.stat().st_size,
                "sha256": _sha256(package_file),
            }
        )

    inventory = {
        "schema_version": 1,
        "map_path": MAP_PATH,
        "actor_count": 1,
        "static_mesh_actor_count": 1,
        "static_mesh_component_count": 1,
        "static_mesh_count": 1,
        "triangle_count": 24,
        "material_count": 2,
        "texture_count": 1,
        "aggregate_bounds_cm": BOUNDS,
        "actors": [
            {
                "object_id": "ForestActor",
                "actor_name": "ForestActor",
                "actor_label": "Forest Actor",
                "actor_class": "StaticMeshActor",
                "parent_actor_name": None,
                "transform": {
                    "translation_cm": [0.0, 0.0, 0.0],
                    "rotation_deg": [0.0, 0.0, 0.0],
                    "scale": [1.0, 1.0, 1.0],
                },
                "components": [
                    {
                        "name": "StaticMeshComponent0",
                        "mesh_path": MESH_PATH,
                        "materials": list(MATERIAL_PATHS),
                        "world_bounds_cm": BOUNDS,
                    }
                ],
            }
        ],
        "assets": [
            {"object_path": object_path, "class": class_name}
            for object_path, class_name in package_assets
        ],
        "static_meshes": [
            {
                "object_path": MESH_PATH,
                "triangle_count": 24,
                "material_count": 2,
            }
        ],
    }
    inventory_sha256 = _canonical_digest(inventory)
    package_bundle_sha256 = _canonical_digest(packages)
    build_dir = settings.project_root / f"out/scene_builds/{SCENE_ID}"
    build_dir.mkdir(parents=True)
    phase_paths = {
        "scene_primary_manifest": build_dir / "primary_manifest.json",
        "scene_reload_manifest": build_dir / "reload_manifest.json",
        "scene_finalize_manifest": build_dir / "finalize_manifest.json",
    }
    for kind, path in phase_paths.items():
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "ok",
                    "scene_id": SCENE_ID,
                    "kind": kind,
                    "inventory_sha256": inventory_sha256,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    build_manifest = build_dir / "manifest.json"
    build_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ok",
                "scene_id": SCENE_ID,
                "map_path": MAP_PATH,
                "source_file": str(source.resolve()),
                "source_sha256": SOURCE_SHA256,
                "scene_spec_sha256": SPEC_SHA256,
                "scene_spec": SCENE_SPEC,
                "inventory": inventory,
                "inventory_sha256": inventory_sha256,
                "packages": packages,
                "package_bundle_sha256": package_bundle_sha256,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    build_sha256 = _sha256(build_manifest)
    actor = SceneObjectUpsert(
        object_id=f"{SCENE_ID}_actor_0000",
        scene_id=SCENE_ID,
        actor_name="ForestActor",
        actor_class="StaticMeshActor",
        transform={
            "translation_cm": [0.0, 0.0, 0.0],
            "rotation_deg": [0.0, 0.0, 0.0],
            "scale": [1.0, 1.0, 1.0],
        },
        mesh_path=MESH_PATH,
        bounds=BOUNDS,
        triangle_count=24,
        material_count=2,
    )
    common_params = {
        "schema_version": 2,
        "source_file": str(source.resolve()),
        "source_sha256": SOURCE_SHA256,
        "scene_spec_sha256": SPEC_SHA256,
        "inventory_sha256": inventory_sha256,
        "package_bundle_sha256": package_bundle_sha256,
        "build_sha256": build_sha256,
        "map_path": MAP_PATH,
        "texture_count": 1,
        "license": "CC-BY-4.0",
        "license_tier": "open",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "Fixture Artist",
        "export": True,
    }
    artifact_paths = {
        "scene_build_manifest": build_manifest,
        **phase_paths,
    }
    artifacts = tuple(
        SceneArtifactUpsert(
            artifact_id=f"{SCENE_ID}_{kind.removeprefix('scene_')}",
            scene_id=SCENE_ID,
            kind=kind,
            path=path,
            params=common_params,
            sha256=_sha256(path),
        )
        for kind, path in artifact_paths.items()
    )
    catalog = Catalog(settings.data_dir / "catalog.db", project_root=settings.project_root)
    record, _, _ = catalog.finalize_scene_build(
        _scene(source_file=source.resolve(), build_sha256=build_sha256),
        (actor,),
        artifacts,
    )
    return catalog, record, build_manifest


def _render_fixture(
    settings: Settings,
    record: SceneRecord,
    *,
    run_name: str,
    provenance_patch: dict[str, object] | None = None,
) -> Any:
    run_dir = settings.project_root / f"out/scene_thumbnails/{run_name}/{SCENE_ID}"
    beauty_dir = run_dir / "beauty_lit"
    mask_dir = run_dir / "object_mask"
    beauty_dir.mkdir(parents=True)
    mask_dir.mkdir(parents=True)
    beauty_frames: list[Path] = []
    mask_frames: list[Path] = []
    for index in range(8):
        beauty = beauty_dir / f"frame_{index:04d}.png"
        Image.new("RGB", (8, 8), (20 + index, 80, 140)).save(beauty)
        beauty_frames.append(beauty)
        mask = mask_dir / f"frame_{index:04d}.exr"
        mask.write_bytes(f"raw-mask-{index}".encode())
        mask_frames.append(mask)
    contact_sheet = run_dir / "contact_sheet.png"
    Image.new("RGB", (16, 16), (10, 10, 10)).save(contact_sheet)
    manifest_path = run_dir / "manifest.json"
    provenance: dict[str, object] = {
        "kind": "scene",
        "scene_id": record.scene_id,
        "source": record.source,
        "source_id": record.source_id,
        "source_url": record.source_url,
        "source_file": record.source_file,
        "source_sha256": record.source_sha256,
        "scene_spec_sha256": record.spec_sha256,
        "build_sha256": record.build_sha256,
        "license": record.license,
        "license_tier": record.license_tier,
        "license_url": record.license_url,
        "attribution": record.attribution,
        "export": True,
        "static_mesh_actor_count": 1,
        "expected_object_stencil_ids": [1],
    }
    provenance.update(provenance_patch or {})
    manifest_path.write_text(
        json.dumps({"schema_version": 3, "status": "ok", "asset": provenance}),
        encoding="utf-8",
    )
    return SimpleNamespace(
        run_dir=run_dir,
        manifest_path=manifest_path,
        frame_paths={"beauty_lit": beauty_frames, "object_mask": mask_frames},
        artifacts=SimpleNamespace(contact_sheet=contact_sheet),
    )


def _consistency(
    beauty_frames: list[Path],
    mask_frames: list[Path],
    *,
    subject_stencil_ids: tuple[int, ...] = (1,),
    maximum_contamination_ratio: float = 0.001,
) -> list[dict[str, float | int | str]]:
    assert len(beauty_frames) == 8
    assert len(mask_frames) == 8
    assert subject_stencil_ids == (1,)
    assert maximum_contamination_ratio == 0.001
    ratios = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.90, 0.50]
    return [
        {
            "frame": beauty.name,
            "safe_background_pixels": 32,
            "contaminated_pixels": 0,
            "contamination_ratio": 0.0,
            "total_pixels": 64,
            "subject_pixels": round(ratio * 64),
            "subject_area_ratio": ratio,
        }
        for beauty, ratio in zip(beauty_frames, ratios, strict=True)
    ]


def _fake_mask(
    mask_path: Path,
    output_path: Path,
    *,
    subject_stencil_ids: tuple[int, ...] = (1,),
) -> None:
    assert mask_path.name == "frame_0006.exr"
    assert subject_stencil_ids == (1,)
    Image.new("L", (8, 8), 255).save(output_path)


@pytest.mark.parametrize("state", ["missing", "raw"])
def test_scene_thumbnail_rejects_missing_or_unbuilt_catalog_scene_before_render(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
) -> None:
    settings = _settings(tmp_path)
    if state == "raw":
        source = settings.project_root / f"data/scenes/{SCENE_ID}.glb"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"raw source")
        Catalog(
            settings.data_dir / "catalog.db",
            project_root=settings.project_root,
        ).upsert_scene(_scene(status="raw"))

    def unexpected_render(**kwargs: object) -> None:
        raise AssertionError(f"render should not run: {kwargs}")

    monkeypatch.setattr("uefactory.scenes.thumbnails.render_job", unexpected_render)

    with pytest.raises(ValueError, match="is not built"):
        thumbnail_catalog_scene(settings=settings, scene_id=SCENE_ID)

    assert not (settings.project_root / "out/scene_thumbnail_jobs").exists()


def test_scene_thumbnail_rejects_a_busy_build_or_render_lock_before_catalog_access(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_lock() -> None:
        try:
            with scene_lock(data_dir=settings.data_dir, scene_id=SCENE_ID):
                acquired.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test did not release the external scene lock")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
            acquired.set()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert acquired.wait(timeout=5)
    try:
        with pytest.raises(SceneLockError, match="another build or render owns"):
            thumbnail_catalog_scene(settings=settings, scene_id=SCENE_ID)
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert not (settings.data_dir / "catalog.db").exists()
    assert not (settings.project_root / "out/scene_thumbnail_jobs").exists()


def test_scene_render_payload_requires_current_build_manifest_hash(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    catalog, record, build_manifest = _built_catalog(settings)
    job_path = settings.project_root / "scene_thumbnail.yaml"
    job_path.write_text(
        yaml.safe_dump(
            {
                "job": "render",
                "assets": [f"scene:{SCENE_ID}"],
                "camera": {
                    "rig": "orbit",
                    "views": 8,
                    "elevation_deg": 22.5,
                    "fov": 45,
                    "resolution": [512, 512],
                },
                "lighting": {"preset": "three_point"},
                "passes": ["beauty_lit", "object_mask"],
                "output": {"dir": "out/scene_thumbnails"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    spec = load_jobspec(job_path)

    payload = _render_scene_payload(settings, spec, catalog)

    assert payload["kind"] == "scene"
    assert payload["scene_id"] == record.scene_id
    assert payload["scene_map_path"] == MAP_PATH
    assert payload["source_sha256"] == SOURCE_SHA256
    assert payload["scene_spec_sha256"] == SPEC_SHA256
    assert payload["build_sha256"] == record.build_sha256
    assert payload["inventory_sha256"] == _canonical_digest(
        json.loads(build_manifest.read_text(encoding="utf-8"))["inventory"]
    )
    assert payload["package_bundle_sha256"] == _canonical_digest(
        json.loads(build_manifest.read_text(encoding="utf-8"))["packages"]
    )
    assert payload["actor_count"] == 1
    assert payload["static_mesh_actor_count"] == 1
    assert payload["static_mesh_component_count"] == 1
    assert payload["expected_object_stencil_ids"] == [1]
    assert payload["render_inventory"]["actors"][0]["actor_name"] == "ForestActor"
    assert payload["render_inventory_sha256"] == _canonical_digest(payload["render_inventory"])
    assert payload["camera_azimuth_offset_deg"] == -35.0
    assert payload["camera_elevation_deg"] == 22.5
    assert payload["license"] == "CC-BY-4.0"
    assert payload["license_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert payload["attribution"] == "Fixture Artist"
    assert payload["export"] is True
    assert payload["no_auto_floor"] is True

    build_manifest.write_text(
        build_manifest.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="no valid build manifest/package inventory"):
        _render_scene_payload(settings, spec, catalog)


def test_scene_thumbnail_selects_best_of_eight_views_and_commits_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    catalog, record, _ = _built_catalog(settings)
    rendered = _render_fixture(settings, record, run_name="first")
    calls: list[dict[str, Any]] = []

    def fake_render_job(**kwargs: Any) -> Any:
        calls.append(kwargs)
        spec = load_jobspec(kwargs["job_path"])
        payload = _render_scene_payload(settings, spec, catalog)
        assert payload["kind"] == "scene"
        return rendered

    monkeypatch.setattr("uefactory.scenes.thumbnails.render_job", fake_render_job)
    monkeypatch.setattr(
        "uefactory.scenes.thumbnails._validate_black_background_consistency",
        _consistency,
    )
    monkeypatch.setattr(
        "uefactory.scenes.thumbnails._create_subject_mask_png",
        _fake_mask,
    )

    result = thumbnail_catalog_scene(settings=settings, scene_id=SCENE_ID, timeout_sec=91)

    assert calls[0]["database_path"] == settings.data_dir / "catalog.db"
    assert calls[0]["timeout_sec"] == 91
    jobspec = yaml.safe_load(calls[0]["job_path"].read_text(encoding="utf-8"))
    assert jobspec["assets"] == [f"scene:{SCENE_ID}"]
    assert jobspec["camera"]["views"] == 8
    assert jobspec["camera"]["resolution"] == [512, 512]
    assert jobspec["passes"] == ["beauty_lit", "object_mask"]
    assert result.thumbnail_path.read_bytes() == rendered.frame_paths["beauty_lit"][6].read_bytes()
    with Image.open(result.thumbnail_path) as thumbnail:
        assert thumbnail.getpixel((0, 0)) == (26, 80, 140)
    with Image.open(result.subject_mask_path) as subject_mask:
        assert subject_mask.mode == "L"
        assert subject_mask.getextrema() == (255, 255)

    updated = catalog.get_scene(SCENE_ID)
    assert updated is not None and updated.status == "render_ok"
    artifacts = catalog.list_scene_artifacts(scene_id=SCENE_ID)
    assert {item.kind for item in artifacts} == {
        "scene_build_manifest",
        "scene_primary_manifest",
        "scene_reload_manifest",
        "scene_finalize_manifest",
        "scene_thumbnail_beauty",
        "scene_thumbnail_mask",
        "scene_thumbnail_mask_raw",
        "scene_thumbnail_render_manifest",
        "scene_thumbnail_contact_sheet",
    }
    build_artifacts = [item for item in artifacts if not item.kind.startswith("scene_thumbnail_")]
    thumbnail_artifacts = [item for item in artifacts if item.kind.startswith("scene_thumbnail_")]
    assert len(build_artifacts) == 4
    assert {item.artifact_id for item in thumbnail_artifacts} == set(result.artifact_ids)
    assert all(item.sha256 == _sha256(settings.project_root / item.path) for item in artifacts)
    assert all(item.params["build_sha256"] == record.build_sha256 for item in artifacts)
    assert all(item.params["export"] is True for item in artifacts)
    assert all(item.params["license"] == "CC-BY-4.0" for item in artifacts)
    assert all(
        item.params["license_url"] == "https://creativecommons.org/licenses/by/4.0/"
        for item in artifacts
    )
    assert all(item.params["attribution"] == "Fixture Artist" for item in artifacts)
    assert all(item.params["views"] == 8 for item in thumbnail_artifacts)
    assert all(item.params["selected_view_index"] == 6 for item in thumbnail_artifacts)
    assert all(item.params["source_sha256"] == SOURCE_SHA256 for item in thumbnail_artifacts)
    assert all(item.params["subject_stencil_ids"] == [1] for item in thumbnail_artifacts)
    raw_mask = next(item for item in artifacts if item.kind == "scene_thumbnail_mask_raw")
    assert raw_mask.path.endswith("object_mask/frame_0006.exr")

    manifest = json.loads(rendered.manifest_path.read_text(encoding="utf-8"))
    assert manifest["scene_catalog_commit"] == {
        "scene_id": SCENE_ID,
        "target_status": "render_ok",
        "thumbnail_preset": "scene_thumbnail_v1",
        "selected_view_index": 6,
        "artifact_ids": list(result.artifact_ids),
        "source_sha256": SOURCE_SHA256,
        "scene_spec_sha256": SPEC_SHA256,
        "build_sha256": record.build_sha256,
        "subject_stencil_ids": [1],
        "maximum_background_contamination_ratio": 0.001,
    }
    assert manifest["scene_thumbnail_validation"]["status"] == "passed"
    assert manifest["scene_thumbnail_validation"]["subject_stencil_ids"] == [1]
    assert len(manifest["scene_thumbnail_validation"]["frames"]) == 8


@pytest.mark.parametrize(
    "provenance_patch",
    [
        {"source": "different_source"},
        {"source_id": "different-source-id"},
        {"source_url": "https://example.test/scenes/different"},
        {"source_file": "/different/forest.glb"},
        {"source_sha256": "c" * 64},
        {"scene_spec_sha256": "d" * 64},
        {"build_sha256": "e" * 64},
        {"license": "CC0-1.0"},
        {"license_tier": "nc"},
        {"license_url": "https://example.test/licenses/different"},
        {"attribution": "Different Artist"},
        {"export": False},
        {"kind": "catalog"},
    ],
)
def test_scene_thumbnail_rejects_render_provenance_mismatch_without_catalog_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provenance_patch: dict[str, object],
) -> None:
    settings = _settings(tmp_path)
    catalog, record, _ = _built_catalog(settings)
    rendered = _render_fixture(
        settings,
        record,
        run_name="mismatch",
        provenance_patch=provenance_patch,
    )

    def fake_render_job(**kwargs: Any) -> Any:
        spec = load_jobspec(kwargs["job_path"])
        _render_scene_payload(settings, spec, catalog)
        return rendered

    monkeypatch.setattr("uefactory.scenes.thumbnails.render_job", fake_render_job)
    monkeypatch.setattr(
        "uefactory.scenes.thumbnails._validate_black_background_consistency",
        _consistency,
    )
    monkeypatch.setattr(
        "uefactory.scenes.thumbnails._create_subject_mask_png",
        _fake_mask,
    )

    with pytest.raises(RuntimeError, match="scene provenance|does not match"):
        thumbnail_catalog_scene(settings=settings, scene_id=SCENE_ID)

    unchanged = catalog.get_scene(SCENE_ID)
    assert unchanged is not None and unchanged.status == "built"
    assert {item.kind for item in catalog.list_scene_artifacts(scene_id=SCENE_ID)} == {
        "scene_build_manifest",
        "scene_primary_manifest",
        "scene_reload_manifest",
        "scene_finalize_manifest",
    }


def test_scene_thumbnail_rerender_replaces_prior_artifacts_and_rechecks_build_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    catalog, record, build_manifest = _built_catalog(settings)
    renders = [
        _render_fixture(settings, record, run_name="first"),
        _render_fixture(settings, record, run_name="second"),
    ]

    def fake_render_job(**kwargs: Any) -> Any:
        spec = load_jobspec(kwargs["job_path"])
        _render_scene_payload(settings, spec, catalog)
        return renders.pop(0)

    monkeypatch.setattr("uefactory.scenes.thumbnails.render_job", fake_render_job)
    monkeypatch.setattr(
        "uefactory.scenes.thumbnails._validate_black_background_consistency",
        _consistency,
    )
    monkeypatch.setattr(
        "uefactory.scenes.thumbnails._create_subject_mask_png",
        _fake_mask,
    )

    first = thumbnail_catalog_scene(settings=settings, scene_id=SCENE_ID)
    second = thumbnail_catalog_scene(settings=settings, scene_id=SCENE_ID)

    assert first.artifact_ids == second.artifact_ids
    artifacts = catalog.list_scene_artifacts(scene_id=SCENE_ID)
    build_artifacts = [item for item in artifacts if not item.kind.startswith("scene_thumbnail_")]
    thumbnail_artifacts = [item for item in artifacts if item.kind.startswith("scene_thumbnail_")]
    assert len(build_artifacts) == 4
    assert len(thumbnail_artifacts) == 5
    assert all("/second/" in item.path for item in thumbnail_artifacts)
    before_tamper = artifacts

    build_manifest.write_text(
        build_manifest.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="no valid build manifest/package inventory"):
        thumbnail_catalog_scene(settings=settings, scene_id=SCENE_ID)

    rerendered = catalog.get_scene(SCENE_ID)
    assert rerendered is not None and rerendered.status == "render_ok"
    assert catalog.list_scene_artifacts(scene_id=SCENE_ID) == before_tamper
