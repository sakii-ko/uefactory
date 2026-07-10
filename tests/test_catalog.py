from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from uefactory.catalog import (
    SCHEMA_VERSION,
    ArtifactUpsert,
    AssetUpsert,
    Catalog,
    CatalogConflictError,
    CatalogSchemaError,
    CatalogValidationError,
    SceneArtifactUpsert,
    SceneObjectUpsert,
    SceneUpsert,
)


def _asset(index: int = 1, **overrides: object) -> AssetUpsert:
    values: dict[str, object] = {
        "asset_id": f"sample_{index:02d}",
        "name": f"Sample {index}",
        "source": "khronos",
        "source_id": f"sample-{index}",
        "source_url": f"https://example.test/models/sample-{index}.glb",
        "license": "CC0-1.0",
        "license_tier": "open",
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "attribution": "Khronos sample",
        "status": "raw",
        "tags": ("fixture", f"group-{index % 2}"),
        "raw_path": f"data/raw/khronos/sample_{index:02d}/model.glb",
        "sha256": f"{index:064x}",
    }
    values.update(overrides)
    return AssetUpsert(**values)  # type: ignore[arg-type]


def _catalog(tmp_path: Path) -> Catalog:
    return Catalog(tmp_path / "data" / "catalog.db", project_root=tmp_path)


def _scene(index: int = 1, **overrides: object) -> SceneUpsert:
    scene_id = f"fantasy_scene_{index:02d}"
    values: dict[str, object] = {
        "scene_id": scene_id,
        "name": f"Fantasy Scene {index}",
        "source": "sketchfab",
        "source_id": f"fantasy-scene-{index}",
        "source_url": f"https://example.test/scenes/fantasy-scene-{index}.glb",
        "license": "CC-BY-4.0",
        "license_tier": "open",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "Example Artist",
        "source_path": f"data/raw/scenes/{scene_id}/scene.glb",
        "source_sha256": f"{index + 100:064x}",
        "spec_sha256": f"{index + 200:064x}",
    }
    values.update(overrides)
    return SceneUpsert(**values)  # type: ignore[arg-type]


def _built_scene(index: int = 1, **overrides: object) -> SceneUpsert:
    scene = _scene(index)
    values: dict[str, object] = {
        "status": "built",
        "source_file": f"/external/scenes/{scene.scene_id}/scene.glb",
        "build_sha256": "d" * 64,
        "map_path": f"/Game/UEF/Scenes/{scene.scene_id}/L_{scene.scene_id}",
        "actor_count": 2,
        "static_mesh_count": 1,
        "triangle_count": 128,
        "material_count": 2,
        "texture_count": 3,
        "bounds": {"min": [-100.0, -50.0, 0.0], "max": [100.0, 50.0, 200.0]},
    }
    values.update(overrides)
    return replace(scene, **values)  # type: ignore[arg-type]


def _scene_objects(index: int = 1) -> tuple[SceneObjectUpsert, ...]:
    scene_id = _scene(index).scene_id
    return (
        SceneObjectUpsert(
            object_id=f"fantasy_scene_{index:02d}_mesh",
            scene_id=scene_id,
            actor_name="DioramaMesh",
            actor_class="StaticMeshActor",
            mesh_path=f"/Game/UEF/Scenes/{scene_id}/Assets/SM_Diorama",
            transform={
                "location": [0.0, 0.0, 0.0],
                "rotation": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
            },
            bounds={"min": [-100.0, -50.0, 0.0], "max": [100.0, 50.0, 200.0]},
            triangle_count=128,
            material_count=2,
        ),
        SceneObjectUpsert(
            object_id=f"fantasy_scene_{index:02d}_light",
            scene_id=scene_id,
            actor_name="KeyLight",
            actor_class="DirectionalLight",
            transform={
                "location": [0.0, 0.0, 300.0],
                "rotation": [-45.0, 30.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
            },
        ),
    )


def _scene_artifact(
    index: int = 1,
    *,
    artifact_id: str | None = None,
    kind: str = "scene_build_manifest",
    path: str | None = None,
) -> SceneArtifactUpsert:
    scene_id = _scene(index).scene_id
    return SceneArtifactUpsert(
        artifact_id=artifact_id or f"fantasy_scene_{index:02d}_{kind}",
        scene_id=scene_id,
        kind=kind,
        path=path or f"out/scenes/{scene_id}/{kind}.json",
        params={"schema_version": 3, "build_sha256": "d" * 64},
        sha256="d" * 64,
    )


def _scene_artifacts(
    index: int = 1,
    *,
    root: str = "out/scenes",
) -> tuple[SceneArtifactUpsert, ...]:
    scene_id = _scene(index).scene_id
    return tuple(
        _scene_artifact(
            index,
            artifact_id=f"{scene_id}_{suffix}",
            kind=kind,
            path=f"{root}/{scene_id}/{suffix}.json",
        )
        for suffix, kind in (
            ("build_manifest", "scene_build_manifest"),
            ("primary_manifest", "scene_primary_manifest"),
            ("reload_manifest", "scene_reload_manifest"),
            ("finalize_manifest", "scene_finalize_manifest"),
        )
    )


def _scene_render_artifacts(
    index: int = 1, *, generation: str = "a"
) -> tuple[SceneArtifactUpsert, ...]:
    scene_id = _scene(index).scene_id
    return tuple(
        _scene_artifact(
            index,
            artifact_id=f"{scene_id}_{suffix}_{generation}",
            kind=kind,
            path=f"out/scenes/{scene_id}/{generation}/{suffix}{extension}",
        )
        for suffix, kind, extension in (
            ("thumb_beauty", "scene_thumbnail_beauty", ".png"),
            ("thumb_mask", "scene_thumbnail_mask", ".png"),
            ("thumb_mask_raw", "scene_thumbnail_mask_raw", ".exr"),
            ("thumb_manifest", "scene_thumbnail_render_manifest", ".json"),
            ("thumb_contact", "scene_thumbnail_contact_sheet", ".png"),
        )
    )


def test_initialize_is_idempotent_and_applies_connection_pragmas(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)

    assert catalog.schema_version() == 0
    assert catalog.initialize() == SCHEMA_VERSION
    assert catalog.initialize() == SCHEMA_VERSION
    assert catalog.connection_settings() == {
        "foreign_keys": 1,
        "journal_mode": "wal",
        "busy_timeout_ms": 5000,
        "user_version": SCHEMA_VERSION,
    }

    connection = sqlite3.connect(catalog.database_path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
        assert {"assets", "artifacts"}.issubset(tables)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_initialize_rejects_newer_schema(tmp_path: Path) -> None:
    database = tmp_path / "future.db"
    connection = sqlite3.connect(database)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(CatalogSchemaError, match="newer than supported"):
        Catalog(database, project_root=tmp_path).initialize()


def test_initialize_is_safe_when_multiple_clients_race(tmp_path: Path) -> None:
    database = tmp_path / "data" / "catalog.db"

    with ThreadPoolExecutor(max_workers=8) as executor:
        versions = tuple(
            executor.map(
                lambda _: Catalog(database, project_root=tmp_path).initialize(),
                range(16),
            )
        )

    assert versions == (SCHEMA_VERSION,) * 16
    assert Catalog(database, project_root=tmp_path).schema_version() == SCHEMA_VERSION


def test_failed_migration_rolls_back_schema_and_version(tmp_path: Path) -> None:
    database = tmp_path / "catalog.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE artifacts (sentinel TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(CatalogSchemaError, match="could not initialize"):
        Catalog(database, project_root=tmp_path).initialize()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert tables == {"artifacts"}
    finally:
        connection.close()


def test_v1_catalog_migrates_to_latest_without_changing_asset_rows(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    asset = catalog.upsert_asset(_asset())
    artifact = catalog.upsert_artifact(
        ArtifactUpsert(
            artifact_id="sample_01_manifest",
            asset_id=asset.asset_id,
            kind="import_manifest",
            path="out/sample_01/manifest.json",
        )
    )

    connection = sqlite3.connect(catalog.database_path)
    try:
        connection.execute("DROP TABLE scene_artifacts")
        connection.execute("DROP TABLE scene_objects")
        connection.execute("DROP TABLE scenes")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    assert catalog.initialize() == SCHEMA_VERSION
    assert catalog.get_asset(asset.asset_id) == asset
    assert catalog.get_artifact(artifact.artifact_id) == artifact
    assert catalog.list_scenes() == ()
    connection = sqlite3.connect(catalog.database_path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
        assert {"assets", "artifacts", "scenes", "scene_objects", "scene_artifacts"} <= tables
    finally:
        connection.close()


def test_ten_raw_assets_are_upserted_and_queryable(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    records = catalog.upsert_assets(_asset(index) for index in range(1, 11))

    assert len(records) == 10
    assert records[0].asset_id == "sample_01"
    assert records[0].tags == ("fixture", "group-1")
    assert [item.asset_id for item in catalog.list_assets(status="raw", source="khronos")] == [
        f"sample_{index:02d}" for index in range(1, 11)
    ]
    assert [item.asset_id for item in catalog.list_assets(tag="group-0")] == [
        "sample_02",
        "sample_04",
        "sample_06",
        "sample_08",
        "sample_10",
    ]
    assert catalog.list_assets(asset_id="sample_04")[0].source_id == "sample-4"
    assert catalog.show_asset("sample_04") == catalog.get_asset("sample_04")
    assert catalog.get_asset_by_sha256(f"{4:064x}") == catalog.get_asset("sample_04")
    assert catalog.get_asset_by_sha256("f" * 64) is None
    assert len(catalog.list_assets(limit=3, offset=2)) == 3

    stats = catalog.stats()
    assert stats.as_dict() == {
        "total_assets": 10,
        "total_artifacts": 0,
        "by_status": {"raw": 10},
        "by_source": {"khronos": 10},
        "by_license": {"CC0-1.0": 10},
        "by_license_tier": {"open": 10},
    }


def test_asset_upsert_updates_mutable_data_but_preserves_identity(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    initial = catalog.upsert_asset(_asset())
    imported = catalog.upsert_asset(
        _asset(
            name="Imported sample",
            status="imported",
            ue_package_path="/Game/UEF/Ingested/sample_01/SM_Sample",
            tri_count=128,
            material_count=2,
        )
    )

    assert imported.name == "Imported sample"
    assert imported.status == "imported"
    assert imported.tri_count == 128
    assert imported.material_count == 2
    assert imported.created_at == initial.created_at
    assert imported.sha256 == initial.sha256

    with pytest.raises(CatalogConflictError, match="immutable provenance conflicts: sha256"):
        catalog.upsert_asset(_asset(sha256="f" * 64))
    assert catalog.get_asset("sample_01") == imported


def test_asset_upsert_rejects_silent_provenance_rewrite(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    original = catalog.upsert_asset(_asset())

    with pytest.raises(CatalogConflictError, match="source_url, license"):
        catalog.upsert_asset(
            _asset(
                source_url="https://example.test/reassigned/model.glb",
                license="MIT",
            )
        )

    assert catalog.get_asset("sample_01") == original


def test_finalize_import_atomically_commits_asset_and_artifact(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    raw = catalog.upsert_asset(_asset())
    imported = replace(
        _asset(),
        status="imported",
        ue_package_path="/Game/UEF/Ingested/sample_01/SM_Sample",
        tri_count=128,
        material_count=2,
    )
    artifact = ArtifactUpsert(
        artifact_id="sample_01_import_manifest",
        asset_id="sample_01",
        kind="import_manifest",
        path="out/ingest/sample_01/manifest.json",
        params={"schema_version": 1},
        sha256="a" * 64,
    )

    asset_record, artifact_record = catalog.finalize_import(imported, artifact)

    assert raw.status == "raw"
    assert asset_record.status == "imported"
    assert asset_record.tri_count == 128
    assert artifact_record.asset_id == asset_record.asset_id
    assert catalog.get_asset("sample_01") == asset_record
    assert catalog.get_artifact("sample_01_import_manifest") == artifact_record


def test_finalize_render_atomically_marks_asset_and_adds_artifacts(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    imported = replace(
        _asset(),
        status="imported",
        ue_package_path="/Game/UEF/Ingested/sample_01/SM_Sample",
        tri_count=128,
        material_count=2,
    )
    catalog.upsert_asset(imported)
    rendered = replace(imported, status="render_ok")
    artifacts = (
        ArtifactUpsert(
            artifact_id="sample_01_thumbnail_beauty",
            asset_id="sample_01",
            kind="thumbnail_beauty",
            path="out/thumbnails/sample_01/beauty.png",
        ),
        ArtifactUpsert(
            artifact_id="sample_01_thumbnail_mask",
            asset_id="sample_01",
            kind="thumbnail_mask",
            path="out/thumbnails/sample_01/mask.png",
        ),
    )

    asset_record, artifact_records = catalog.finalize_render(rendered, artifacts)

    assert asset_record.status == "render_ok"
    assert tuple(item.kind for item in artifact_records) == (
        "thumbnail_beauty",
        "thumbnail_mask",
    )


def test_finalize_render_rolls_back_when_an_artifact_conflicts(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    imported = replace(
        _asset(),
        status="imported",
        ue_package_path="/Game/UEF/Ingested/sample_01/SM_Sample",
        tri_count=128,
    )
    before = catalog.upsert_asset(imported)
    catalog.upsert_artifact(
        ArtifactUpsert(
            artifact_id="sample_01_thumbnail",
            asset_id="sample_01",
            kind="thumbnail_beauty",
            path="out/old.png",
        )
    )

    with pytest.raises(CatalogConflictError, match="another artifact"):
        catalog.finalize_render(
            replace(imported, status="render_ok"),
            (
                ArtifactUpsert(
                    artifact_id="sample_01_thumbnail",
                    asset_id="sample_01",
                    kind="thumbnail_beauty",
                    path="out/new.png",
                ),
            ),
        )

    assert catalog.get_asset("sample_01") == before


def test_finalize_import_rejects_nonfinal_status_and_mismatched_asset_id(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    artifact = ArtifactUpsert(
        artifact_id="sample_01_import_manifest",
        asset_id="sample_01",
        kind="import_manifest",
        path="out/ingest/sample_01/manifest.json",
    )

    with pytest.raises(CatalogValidationError, match="status 'imported' or 'render_ok'"):
        catalog.finalize_import(_asset(), artifact)

    imported = replace(
        _asset(),
        status="imported",
        ue_package_path="/Game/UEF/Ingested/sample_01/SM_Sample",
        tri_count=128,
    )
    with pytest.raises(CatalogValidationError, match="artifact.asset_id to match"):
        catalog.finalize_import(imported, replace(artifact, asset_id="sample_02"))

    assert catalog.schema_version() == 0


def test_finalize_import_rolls_back_asset_when_artifact_step_crashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    raw = catalog.upsert_asset(_asset())
    imported = replace(
        _asset(),
        status="imported",
        ue_package_path="/Game/UEF/Ingested/sample_01/SM_Sample",
        tri_count=128,
        material_count=2,
    )
    artifact = ArtifactUpsert(
        artifact_id="sample_01_import_manifest",
        asset_id="sample_01",
        kind="import_manifest",
        path="out/ingest/sample_01/manifest.json",
    )

    def crash_after_asset(*args: object) -> None:
        del args
        raise RuntimeError("simulated crash before artifact write")

    monkeypatch.setattr(
        "uefactory.catalog.database._upsert_prepared_artifact",
        crash_after_asset,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        catalog.finalize_import(imported, artifact)

    assert catalog.get_asset("sample_01") == raw
    assert catalog.list_artifacts() == ()


def test_finalize_import_atomically_supersedes_prior_asset_artifacts(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    raw = catalog.upsert_asset(_asset())
    existing_artifact = catalog.upsert_artifact(
        ArtifactUpsert(
            artifact_id="sample_01_import_manifest",
            asset_id="sample_01",
            kind="import_manifest",
            path="out/ingest/old/manifest.json",
        )
    )
    stale_thumbnail = catalog.upsert_artifact(
        ArtifactUpsert(
            artifact_id="sample_01_thumbnail",
            asset_id="sample_01",
            kind="thumbnail_beauty",
            path="out/thumbnails/old.png",
        )
    )
    imported = replace(
        _asset(),
        status="imported",
        ue_package_path="/Game/UEF/Ingested/sample_01/SM_Sample",
        tri_count=128,
        material_count=2,
    )
    conflicting_artifact = ArtifactUpsert(
        artifact_id="sample_01_import_manifest",
        asset_id="sample_01",
        kind="import_manifest",
        path="out/ingest/new/manifest.json",
    )

    asset_record, artifact_record = catalog.finalize_import(imported, conflicting_artifact)

    assert raw.status == "raw"
    assert asset_record.status == "imported"
    assert artifact_record.path == "out/ingest/new/manifest.json"
    assert catalog.get_artifact(existing_artifact.artifact_id) == artifact_record
    assert catalog.get_artifact(stale_thumbnail.artifact_id) is None
    assert catalog.list_artifacts(asset_id="sample_01") == (artifact_record,)


def test_absolute_path_inside_project_is_stored_relative(tmp_path: Path) -> None:
    raw_path = tmp_path / "data" / "raw" / "sample.glb"
    record = _catalog(tmp_path).upsert_asset(_asset(raw_path=raw_path))

    assert record.raw_path == "data/raw/sample.glb"


def test_batch_upsert_rolls_back_every_row_on_hash_conflict(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.upsert_asset(_asset(1))

    with pytest.raises(CatalogConflictError, match="UNIQUE constraint failed: assets.sha256"):
        catalog.upsert_assets((_asset(2), _asset(3, sha256=f"{1:064x}")))

    assert catalog.get_asset("sample_02") is None
    assert catalog.get_asset("sample_03") is None
    assert catalog.stats().total_assets == 1


def test_batch_rejects_duplicate_ids_before_writing(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)

    with pytest.raises(CatalogConflictError, match="duplicate asset_id"):
        catalog.upsert_assets((_asset(1), _asset(2, asset_id="sample_01")))

    assert catalog.stats().total_assets == 0


def test_failed_asset_requires_and_round_trips_structured_error(tmp_path: Path) -> None:
    error = {"code": "ue_import_failed", "messages": ["zero meshes"], "retryable": True}
    record = _catalog(tmp_path).upsert_asset(_asset(status="failed", error=error))

    assert record.status == "failed"
    assert record.error == error
    assert record.as_dict()["error"] == error


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"asset_id": "Bad-ID"}, "asset_id"),
        ({"source": "Khronos"}, "source"),
        ({"source_url": "example.test/model.glb"}, "source_url"),
        ({"license": "proprietary"}, "license must be"),
        ({"license_tier": "nc"}, "requires license_tier"),
        ({"status": "ready"}, "status must be"),
        ({"sha256": "A" * 64}, "SHA-256"),
        ({"sha256": 42}, "SHA-256"),
        ({"raw_path": "../outside.glb"}, "raw_path"),
        ({"raw_path": 42}, "raw_path"),
        ({"raw_path": "data\\raw\\model.glb"}, "raw_path"),
        ({"tags": "fixture"}, "tags must be"),
        ({"tags": ("fixture", "fixture")}, "duplicate tag"),
        ({"attribution": " author "}, "attribution"),
        ({"material_count": -1}, "material_count"),
        ({"tri_count": True}, "tri_count"),
        ({"ue_package_path": "Game/Model"}, "ue_package_path"),
        ({"ue_package_path": 42}, "ue_package_path"),
        ({"error": {"code": "bad"}}, "only failed"),
        ({"status": "failed", "error": {}}, "non-empty structured error"),
        ({"status": "imported"}, "require ue_package_path"),
        ({"ue_package_path": "/Game/Model", "tri_count": 1}, "raw assets may not"),
        (
            {"status": "imported", "ue_package_path": "/Game/Model", "tri_count": 0},
            "tri_count > 0",
        ),
    ],
)
def test_asset_validation_rejects_invalid_contract(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    invalid = replace(_asset(), **changes)  # type: ignore[arg-type]
    with pytest.raises(CatalogValidationError, match=message):
        _catalog(tmp_path).upsert_asset(invalid)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("asset_id", "1sample"),
        ("asset_id", "sample__01"),
        ("asset_id", "sample_"),
        ("asset_id", "a" * 65),
        ("source", "1source"),
        ("source", "source-name"),
        ("source", "source__name"),
        ("source", "source_"),
        ("source", "s" * 65),
    ],
)
def test_asset_and_source_slugs_match_ingest_contract(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    invalid = replace(_asset(), **{field: value})  # type: ignore[arg-type]

    with pytest.raises(CatalogValidationError, match=field):
        _catalog(tmp_path).upsert_asset(invalid)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_id", "1artifact"),
        ("artifact_id", "artifact__manifest"),
        ("artifact_id", "artifact_"),
        ("artifact_id", "a" * 97),
        ("kind", "1manifest"),
        ("kind", "import__manifest"),
        ("kind", "import_manifest_"),
        ("kind", "k" * 97),
    ],
)
def test_artifact_slugs_use_the_same_snake_case_contract(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    artifact = ArtifactUpsert(
        artifact_id="sample_01_import_manifest",
        asset_id="sample_01",
        kind="import_manifest",
        path="out/ingest/sample_01/manifest.json",
    )
    invalid = replace(artifact, **{field: value})  # type: ignore[arg-type]

    with pytest.raises(CatalogValidationError, match=field):
        _catalog(tmp_path).upsert_artifact(invalid)


def test_path_outside_project_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CatalogValidationError, match="inside project_root"):
        _catalog(tmp_path).upsert_asset(_asset(raw_path=tmp_path.parent / "outside.glb"))


def test_catalog_path_may_not_traverse_project_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CatalogValidationError, match="symbolic link"):
        _catalog(tmp_path).upsert_asset(_asset(raw_path="linked/model.glb"))


def test_database_checks_reject_invalid_status_license_hash_and_path(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.upsert_asset(_asset())
    invalid_updates = (
        ("UPDATE assets SET status = 'ready'",),
        ("UPDATE assets SET source = 'Bad Source'",),
        ("UPDATE assets SET license = 'unknown'",),
        ("UPDATE assets SET license_tier = 'nc'",),
        ("UPDATE assets SET raw_path = '/absolute/model.glb'",),
        ("UPDATE assets SET raw_path = '../escape.glb'",),
        ("UPDATE assets SET sha256 = ?", "z" * 64),
        ("UPDATE assets SET tri_count = 1.5",),
        ("UPDATE assets SET tri_count = 1 WHERE status = 'raw'",),
        ("UPDATE assets SET created_at = 'not-a-time'",),
        ("UPDATE assets SET created_at = '2026-99-99T99:99:99Z'",),
        ("UPDATE assets SET status = 'failed', error_json = NULL",),
    )

    connection = sqlite3.connect(catalog.database_path)
    try:
        for update in invalid_updates:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(update[0], update[1:])
            connection.rollback()
    finally:
        connection.close()


def test_database_checks_enforce_public_slug_contract(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.upsert_asset(_asset())
    catalog.upsert_artifact(
        ArtifactUpsert(
            artifact_id="sample_01_import_manifest",
            asset_id="sample_01",
            kind="import_manifest",
            path="out/ingest/sample_01/manifest.json",
        )
    )
    invalid_updates = (
        ("UPDATE assets SET asset_id = ?", "1sample"),
        ("UPDATE assets SET asset_id = ?", "sample__01"),
        ("UPDATE assets SET asset_id = ?", "sample_"),
        ("UPDATE assets SET asset_id = ?", "a" * 65),
        ("UPDATE assets SET source = ?", "1source"),
        ("UPDATE assets SET source = ?", "source__name"),
        ("UPDATE assets SET source = ?", "source_"),
        ("UPDATE assets SET source = ?", "s" * 65),
        ("UPDATE artifacts SET artifact_id = ?", "1artifact"),
        ("UPDATE artifacts SET artifact_id = ?", "artifact__manifest"),
        ("UPDATE artifacts SET artifact_id = ?", "artifact_"),
        ("UPDATE artifacts SET artifact_id = ?", "a" * 97),
        ("UPDATE artifacts SET kind = ?", "1manifest"),
        ("UPDATE artifacts SET kind = ?", "import__manifest"),
        ("UPDATE artifacts SET kind = ?", "import_manifest_"),
        ("UPDATE artifacts SET kind = ?", "k" * 97),
    )

    connection = sqlite3.connect(catalog.database_path)
    try:
        for statement, value in invalid_updates:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, (value,))
            connection.rollback()
    finally:
        connection.close()


def test_artifacts_enforce_foreign_key_relative_path_and_identity(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.upsert_asset(_asset())
    artifact = ArtifactUpsert(
        artifact_id="sample_01_thumbnail",
        asset_id="sample_01",
        kind="thumbnail",
        path="out/thumbnails/sample_01.png",
        params={"resolution": [512, 512]},
        sha256="a" * 64,
    )

    first = catalog.upsert_artifact(artifact)
    updated = catalog.upsert_artifact(replace(artifact, params={"resolution": [256, 256]}))
    assert first.path == "out/thumbnails/sample_01.png"
    assert updated.params == {"resolution": [256, 256]}
    assert catalog.list_artifacts(asset_id="sample_01") == (updated,)
    assert catalog.stats().total_artifacts == 1

    with pytest.raises(CatalogValidationError, match="path"):
        catalog.upsert_artifact(replace(artifact, artifact_id="bad_path", path="/tmp/a.png"))
    with pytest.raises(CatalogConflictError, match="another artifact"):
        catalog.upsert_artifact(replace(artifact, kind="object_mask"))
    with pytest.raises(CatalogConflictError, match="FOREIGN KEY"):
        catalog.upsert_artifact(
            replace(artifact, artifact_id="missing_asset", asset_id="sample_99")
        )


def test_artifact_batch_is_transactional(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.upsert_asset(_asset())
    good = ArtifactUpsert(
        artifact_id="sample_01_thumbnail",
        asset_id="sample_01",
        kind="thumbnail",
        path="out/sample_01.png",
    )
    missing_parent = replace(good, artifact_id="sample_02_thumbnail", asset_id="sample_02")

    with pytest.raises(CatalogConflictError, match="FOREIGN KEY"):
        catalog.upsert_artifacts((good, missing_parent))

    assert catalog.list_artifacts() == ()


def test_query_validation_is_strict(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)

    with pytest.raises(CatalogValidationError, match="status"):
        catalog.list_assets(status="unknown")
    with pytest.raises(CatalogValidationError, match="license"):
        catalog.list_assets(license="unknown")
    with pytest.raises(CatalogValidationError, match="limit"):
        catalog.list_assets(limit=0)
    with pytest.raises(CatalogValidationError, match="offset"):
        catalog.list_assets(offset=-1)


def test_scene_build_round_trips_map_inventory_and_artifacts(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    raw = catalog.upsert_scene(_scene())
    built = _built_scene()

    scene, objects, artifacts = catalog.finalize_scene_build(
        built, _scene_objects(), _scene_artifacts()
    )

    assert raw.status == "raw"
    assert scene.status == "built"
    assert scene.map_path == "/Game/UEF/Scenes/fantasy_scene_01/L_fantasy_scene_01"
    assert scene.bounds == {"min": [-100.0, -50.0, 0.0], "max": [100.0, 50.0, 200.0]}
    assert scene.as_dict()["static_mesh_count"] == 1
    assert [item.object_id for item in objects] == [
        "fantasy_scene_01_light",
        "fantasy_scene_01_mesh",
    ]
    assert objects[1].transform["scale"] == [1.0, 1.0, 1.0]
    assert {item.kind for item in artifacts} == {
        "scene_build_manifest",
        "scene_primary_manifest",
        "scene_reload_manifest",
        "scene_finalize_manifest",
    }
    assert catalog.get_scene(scene.scene_id) == scene
    assert catalog.get_scene_object("fantasy_scene_01_mesh") == objects[1]
    assert catalog.get_scene_artifact(artifacts[0].artifact_id) == artifacts[0]
    assert catalog.list_scenes(status="built", source="sketchfab") == (scene,)


def test_scene_build_preflight_executes_full_transaction_without_publishing(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)

    catalog.validate_scene_build(_built_scene(), _scene_objects(), _scene_artifacts())

    assert catalog.list_scenes() == ()
    assert catalog.list_scene_objects() == ()
    assert catalog.list_scene_artifacts() == ()
    scene, objects, artifacts = catalog.finalize_scene_build(
        _built_scene(), _scene_objects(), _scene_artifacts()
    )
    assert scene.status == "built"
    assert len(objects) == 2
    assert len(artifacts) == 4


def test_scene_build_requires_exact_hashed_generation_cohort(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    cohort = _scene_artifacts()

    with pytest.raises(CatalogValidationError, match="complete scene build artifact cohort"):
        catalog.finalize_scene_build(_built_scene(), _scene_objects(), cohort[:-1])
    with pytest.raises(CatalogValidationError, match="active build_sha256"):
        catalog.finalize_scene_build(
            _built_scene(),
            _scene_objects(),
            (replace(cohort[0], params={"build_sha256": "e" * 64}), *cohort[1:]),
        )
    with pytest.raises(CatalogValidationError, match="manifest sha256"):
        catalog.finalize_scene_build(
            _built_scene(),
            _scene_objects(),
            (replace(cohort[0], sha256="e" * 64), *cohort[1:]),
        )

    assert catalog.list_scenes() == ()


def test_scene_rebuild_fully_replaces_objects_and_build_artifacts(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    first_scene, first_objects, first_artifacts = catalog.finalize_scene_build(
        _built_scene(), _scene_objects(), _scene_artifacts()
    )
    replacement_objects = (
        replace(
            _scene_objects()[0],
            object_id="fantasy_scene_01_replacement",
            actor_name="ReplacementMesh",
        ),
        _scene_objects()[1],
    )
    replacement_scene = replace(_built_scene(), spec_sha256="e" * 64)
    replacement_artifacts = tuple(
        replace(
            item,
            path=f"out/rebuilt/{Path(item.path).name}",
        )
        for item in _scene_artifacts()
    )

    scene, objects, artifacts = catalog.finalize_scene_build(
        replacement_scene, replacement_objects, replacement_artifacts
    )

    assert scene.created_at == first_scene.created_at
    assert scene.spec_sha256 == "e" * 64
    assert catalog.get_scene_object(first_objects[1].object_id) is None
    assert [item.object_id for item in objects] == [
        "fantasy_scene_01_light",
        "fantasy_scene_01_replacement",
    ]
    assert all(item.path.startswith("out/rebuilt/") for item in artifacts)
    assert catalog.get_scene_artifact(first_artifacts[0].artifact_id) == artifacts[0]


def test_scene_build_failure_rolls_back_scene_objects_and_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path)
    before_scene, before_objects, before_artifacts = catalog.finalize_scene_build(
        _built_scene(), _scene_objects(), _scene_artifacts()
    )

    def crash_before_artifact(*args: object) -> None:
        del args
        raise RuntimeError("simulated scene artifact crash")

    monkeypatch.setattr(
        "uefactory.catalog.database._upsert_prepared_scene_artifact", crash_before_artifact
    )
    replacement_objects = (
        replace(_scene_objects()[0], object_id="fantasy_scene_01_replacement"),
        _scene_objects()[1],
    )
    with pytest.raises(RuntimeError, match="simulated scene artifact crash"):
        catalog.finalize_scene_build(
            replace(_built_scene(), spec_sha256="e" * 64),
            replacement_objects,
            tuple(
                replace(item, path=f"out/rebuilt/{Path(item.path).name}")
                for item in _scene_artifacts()
            ),
        )

    assert catalog.get_scene(before_scene.scene_id) == before_scene
    assert catalog.list_scene_objects(scene_id=before_scene.scene_id) == before_objects
    assert catalog.list_scene_artifacts(scene_id=before_scene.scene_id) == before_artifacts


def test_scene_render_replaces_complete_render_cohort_and_preserves_build(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    built, _, build_artifacts = catalog.finalize_scene_build(
        _built_scene(), _scene_objects(), _scene_artifacts()
    )
    rendered = replace(_built_scene(), status="render_ok")
    scene, first_render = catalog.finalize_scene_render(
        rendered, _scene_render_artifacts(generation="a")
    )

    scene, replacement = catalog.finalize_scene_render(
        rendered, _scene_render_artifacts(generation="b")
    )

    assert built.status == "built"
    assert scene.status == "render_ok"
    assert all("/b/" in item.path for item in replacement)
    assert all(catalog.get_scene_artifact(item.artifact_id) is None for item in first_render)
    all_artifacts = catalog.list_scene_artifacts(scene_id=scene.scene_id)
    assert {item.artifact_id for item in build_artifacts} <= {
        item.artifact_id for item in all_artifacts
    }
    assert len(all_artifacts) == len(build_artifacts) + len(replacement)


def test_scene_render_failure_rolls_back_status_and_kind_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path)
    built, _, build_artifacts = catalog.finalize_scene_build(
        _built_scene(), _scene_objects(), _scene_artifacts()
    )

    def crash_after_kind_delete(*args: object) -> None:
        del args
        raise RuntimeError("simulated render artifact crash")

    monkeypatch.setattr(
        "uefactory.catalog.database._upsert_prepared_scene_artifact", crash_after_kind_delete
    )
    with pytest.raises(RuntimeError, match="simulated render artifact crash"):
        catalog.finalize_scene_render(
            replace(_built_scene(), status="render_ok"),
            _scene_render_artifacts(),
        )

    assert catalog.get_scene(built.scene_id) == built
    assert catalog.list_scene_artifacts(scene_id=built.scene_id) == build_artifacts


def test_scene_render_rejects_partial_or_stale_generation_cohort(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.finalize_scene_build(_built_scene(), _scene_objects(), _scene_artifacts())
    rendered = replace(_built_scene(), status="render_ok")
    cohort = _scene_render_artifacts()

    with pytest.raises(CatalogValidationError, match="complete scene thumbnail artifact cohort"):
        catalog.finalize_scene_render(rendered, cohort[:-1])
    with pytest.raises(CatalogValidationError, match="active build_sha256"):
        catalog.finalize_scene_render(
            rendered,
            (replace(cohort[0], params={"build_sha256": "e" * 64}), *cohort[1:]),
        )

    assert catalog.get_scene(rendered.scene_id).status == "built"  # type: ignore[union-attr]


def test_scene_research_only_license_is_nc_and_quarantine_is_queryable(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    quarantined = _scene(
        source="blackmyth",
        source_url="file:///home/chijw/workspace/projs/blackmyth/model.glb",
        license="LicenseRef-Research-Only",
        license_tier="nc",
        license_url="urn:license:blackmyth-research-only",
        attribution="Research-only extracted game asset",
        status="quarantined",
        error={"code": "redistribution_prohibited", "retryable": False},
    )

    record = catalog.upsert_scene(quarantined)

    assert record.license_tier == "nc"
    assert record.error == {"code": "redistribution_prohibited", "retryable": False}
    assert catalog.list_scenes(status="quarantined") == (record,)
    with pytest.raises(CatalogValidationError, match="requires license_tier 'nc'"):
        catalog.upsert_scene(replace(quarantined, scene_id="wrong_tier", license_tier="open"))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"status": "ready"}, "scene status"),
        ({"source_path": "../scene.glb"}, "source_path"),
        ({"source_sha256": "A" * 64}, "source_sha256"),
        ({"spec_sha256": "bad"}, "spec_sha256"),
        ({"map_path": "/Game/UEF/Scenes/wrong/L_wrong"}, "map_path"),
        ({"bounds": {"max": [float("nan"), 1.0, 2.0]}}, "finite"),
        ({"status": "quarantined"}, "non-empty structured error"),
        ({"status": "failed", "error": {}}, "non-empty"),
    ],
)
def test_scene_validation_rejects_invalid_contract(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    base = _built_scene() if "map_path" in changes or "bounds" in changes else _scene()
    with pytest.raises(CatalogValidationError, match=message):
        _catalog(tmp_path).upsert_scene(replace(base, **changes))  # type: ignore[arg-type]


def test_scene_object_artifact_json_and_foreign_keys_are_strict(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    scene = catalog.upsert_scene(_scene())
    object_item = replace(_scene_objects()[1], scene_id=scene.scene_id)
    artifact_item = _scene_artifact()

    catalog.upsert_scene_object(object_item)
    catalog.upsert_scene_artifact(artifact_item)
    with pytest.raises(CatalogValidationError, match="finite"):
        catalog.upsert_scene_object(
            replace(object_item, object_id="bad_transform", transform={"x": float("inf")})
        )
    with pytest.raises(CatalogValidationError, match="keys must be strings"):
        catalog.upsert_scene_artifact(
            replace(artifact_item, artifact_id="bad_params", params={1: "bad"})  # type: ignore[dict-item]
        )
    with pytest.raises(CatalogConflictError, match="FOREIGN KEY"):
        catalog.upsert_scene_object(
            replace(object_item, object_id="missing_scene_object", scene_id="missing_scene")
        )

    connection = sqlite3.connect(catalog.database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM scenes WHERE scene_id = ?", (scene.scene_id,))
        connection.commit()
    finally:
        connection.close()
    assert catalog.get_scene_object(object_item.object_id) is None
    assert catalog.get_scene_artifact(artifact_item.artifact_id) is None


def test_scene_database_checks_reject_invalid_status_license_map_and_json(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    scene, _, _ = catalog.finalize_scene_build(_built_scene(), _scene_objects(), _scene_artifacts())
    invalid_updates = (
        ("UPDATE scenes SET status = 'ready' WHERE scene_id = ?", scene.scene_id),
        (
            "UPDATE scenes SET license_tier = 'nc' WHERE scene_id = ?",
            scene.scene_id,
        ),
        (
            "UPDATE scenes SET map_path = '/Game/UEF/Scenes/wrong/L_wrong' WHERE scene_id = ?",
            scene.scene_id,
        ),
        (
            "UPDATE scenes SET bounds_json = '[]' WHERE scene_id = ?",
            scene.scene_id,
        ),
        (
            "UPDATE scenes SET build_sha256 = NULL WHERE scene_id = ?",
            scene.scene_id,
        ),
        (
            "UPDATE scenes SET texture_count = -1 WHERE scene_id = ?",
            scene.scene_id,
        ),
        (
            "UPDATE scene_objects SET transform_json = '[]' WHERE scene_id = ?",
            scene.scene_id,
        ),
    )

    connection = sqlite3.connect(catalog.database_path)
    try:
        for statement, scene_id in invalid_updates:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, (scene_id,))
            connection.rollback()
    finally:
        connection.close()


def test_scene_finalize_validates_complete_inventory_and_existing_build(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    with pytest.raises(CatalogValidationError, match="actor_count"):
        catalog.finalize_scene_build(
            replace(_built_scene(), actor_count=3), _scene_objects(), _scene_artifacts()
        )
    with pytest.raises(CatalogValidationError, match="existing built scene"):
        catalog.finalize_scene_render(
            replace(_built_scene(), status="render_ok"),
            _scene_render_artifacts(),
        )
    assert catalog.list_scenes() == ()
