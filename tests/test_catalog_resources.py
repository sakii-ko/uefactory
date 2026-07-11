from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from uefactory.catalog import (
    SCHEMA_VERSION,
    ArtifactUpsert,
    AssetUpsert,
    Catalog,
    CatalogConflictError,
    CatalogValidationError,
    ResourceArtifactUpsert,
    ResourceBindingUpsert,
    ResourceFileUpsert,
    ResourceUpsert,
    SceneArtifactUpsert,
    SceneObjectUpsert,
    SceneUpsert,
)

BUNDLE_SHA256 = "b" * 64
CONTENT_SHA256 = "c" * 64
REVISION = "d69ec09a43016714fd0dda163b3b0c585c968f56"


def _catalog(tmp_path: Path) -> Catalog:
    return Catalog(tmp_path / "data/catalog.db", project_root=tmp_path)


def _resource_params(resource: ResourceUpsert) -> dict[str, object]:
    return {
        "schema_version": 1,
        "resource_id": resource.resource_id,
        "resource_kind": resource.resource_kind,
        "profile": resource.profile,
        "resolution": resource.resolution,
        "bundle_sha256": resource.bundle_sha256,
        "content_sha256": resource.content_sha256,
    }


def _hdri_resource(*, status: str = "ready") -> ResourceUpsert:
    return ResourceUpsert(
        resource_id="polyhaven_hdri_studio_small_03_d69ec09a",
        resource_kind="hdri",
        profile="radiance_hdr_v1",
        resolution="1k",
        name="Studio Small 03",
        source="polyhaven",
        source_id="studio_small_03",
        source_url="https://polyhaven.com/a/studio_small_03",
        source_revision=REVISION,
        source_revision_scheme="sha1_files_hash",
        license="CC0-1.0",
        license_tier="open",
        license_url="https://polyhaven.com/license",
        status=status,
        tags=("hdri", "studio"),
        bundle_sha256=BUNDLE_SHA256,
        content_sha256=CONTENT_SHA256,
    )


def _hdri_file(resource: ResourceUpsert) -> ResourceFileUpsert:
    return ResourceFileUpsert(
        file_id="studio_small_03_radiance_hdr",
        resource_id=resource.resource_id,
        semantic_role="environment_radiance",
        provider_role="hdri",
        resolution="1k",
        format="hdr",
        path="data/resources/polyhaven/studio_small_03_1k.hdr",
        source_url=("https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/1k/studio_small_03_1k.hdr"),
        byte_size=1_686_299,
        provider_md5="74e6ef69ea9024c2cc25b3a7de8ec2f7",
        sha256="3" * 64,
        color_space="linear",
        width=1024,
        height=512,
        is_primary=True,
    )


def _hdri_artifacts(resource: ResourceUpsert) -> tuple[ResourceArtifactUpsert, ...]:
    common = _resource_params(resource)
    return (
        ResourceArtifactUpsert(
            artifact_id="studio_small_03_resource_source_manifest",
            resource_id=resource.resource_id,
            kind="resource_source_manifest",
            path="out/resources/studio_small_03/source.json",
            params=common,
            sha256="4" * 64,
        ),
        ResourceArtifactUpsert(
            artifact_id="studio_small_03_hdri_validation_manifest",
            resource_id=resource.resource_id,
            kind="hdri_validation_manifest",
            path="out/resources/studio_small_03/validation.json",
            params={
                **common,
                "validation_status": "passed",
                "width": 1024,
                "height": 512,
                "file_id": "studio_small_03_radiance_hdr",
            },
            sha256="5" * 64,
        ),
    )


def _pbr_resource() -> ResourceUpsert:
    return ResourceUpsert(
        resource_id="polyhaven_pbr_aerial_asphalt_01_cdf3c8f0",
        resource_kind="pbr_texture_set",
        profile="ue_pbr_png_v1",
        resolution="1k",
        name="Aerial Asphalt 01",
        source="polyhaven",
        source_id="aerial_asphalt_01",
        source_url="https://polyhaven.com/a/aerial_asphalt_01",
        source_revision="cdf3c8f091b3589407bdf0697a2deb2c6b40650d",
        source_revision_scheme="sha1_files_hash",
        license="CC0-1.0",
        license_tier="open",
        license_url="https://polyhaven.com/license",
        status="ready",
        tags=("asphalt", "pbr"),
        bundle_sha256="6" * 64,
        content_sha256="7" * 64,
        physical_size_mm=(30_000.0, 30_000.0),
    )


def _pbr_files(resource: ResourceUpsert) -> tuple[ResourceFileUpsert, ...]:
    common = {
        "resource_id": resource.resource_id,
        "resolution": "1k",
        "format": "png",
        "width": 1024,
        "height": 1024,
    }
    return (
        ResourceFileUpsert(
            file_id="aerial_asphalt_base_color",
            semantic_role="base_color",
            provider_role="Diffuse",
            path="data/resources/polyhaven/aerial_asphalt_01_diff_1k.png",
            source_url="https://dl.polyhaven.org/aerial_asphalt_01_diff_1k.png",
            byte_size=2_337_475,
            sha256="8" * 64,
            color_space="srgb",
            is_primary=True,
            **common,
        ),
        ResourceFileUpsert(
            file_id="aerial_asphalt_normal",
            semantic_role="normal",
            provider_role="nor_dx",
            path="data/resources/polyhaven/aerial_asphalt_01_nor_dx_1k.png",
            source_url="https://dl.polyhaven.org/aerial_asphalt_01_nor_dx_1k.png",
            byte_size=1_671_704,
            sha256="9" * 64,
            color_space="data",
            normal_convention="directx",
            **common,
        ),
        ResourceFileUpsert(
            file_id="aerial_asphalt_roughness",
            semantic_role="roughness",
            provider_role="Rough",
            path="data/resources/polyhaven/aerial_asphalt_01_rough_1k.png",
            source_url="https://dl.polyhaven.org/aerial_asphalt_01_rough_1k.png",
            byte_size=525_889,
            sha256="a" * 64,
            color_space="data",
            **common,
        ),
    )


def _pbr_artifacts(
    resource: ResourceUpsert, files: tuple[ResourceFileUpsert, ...]
) -> tuple[ResourceArtifactUpsert, ...]:
    common = _resource_params(resource)
    file_ids = sorted(item.file_id for item in files)
    return tuple(
        ResourceArtifactUpsert(
            artifact_id=f"aerial_asphalt_{kind}",
            resource_id=resource.resource_id,
            kind=kind,
            path=f"out/resources/aerial_asphalt_01/{kind}.json",
            params={**common, **extra},
            sha256=sha,
        )
        for kind, extra, sha in (
            ("resource_source_manifest", {}, "b" * 64),
            (
                "pbr_material_descriptor",
                {"file_ids": file_ids, "physical_size_mm": [30_000.0, 30_000.0]},
                "c" * 64,
            ),
            (
                "pbr_validation_manifest",
                {"file_ids": file_ids, "validation_status": "passed"},
                "d" * 64,
            ),
        )
    )


def test_schema_v5_migration_preserves_every_existing_table_row(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.initialize()
    asset = catalog.upsert_asset(
        AssetUpsert(
            asset_id="migration_mesh",
            name="Migration Mesh",
            source="local",
            source_id="mesh-1",
            source_url="file://localhost/mesh.glb",
            license="CC0-1.0",
            license_tier="open",
            license_url="https://creativecommons.org/publicdomain/zero/1.0/",
            raw_path="data/raw/migration_mesh.glb",
            sha256="1" * 64,
        )
    )
    catalog.upsert_artifact(
        ArtifactUpsert(
            artifact_id="migration_mesh_manifest",
            asset_id=asset.asset_id,
            kind="import_manifest",
            path="out/migration_mesh/manifest.json",
        )
    )
    scene = catalog.upsert_scene(
        SceneUpsert(
            scene_id="migration_scene",
            name="Migration Scene",
            source="local",
            source_id="scene-1",
            source_url="file://localhost/scene.glb",
            license="CC0-1.0",
            license_tier="open",
            license_url="https://creativecommons.org/publicdomain/zero/1.0/",
            source_path="examples/migration_scene.yaml",
            source_sha256="2" * 64,
            spec_sha256="3" * 64,
        )
    )
    catalog.upsert_scene_object(
        SceneObjectUpsert(
            object_id="migration_scene_root",
            scene_id=scene.scene_id,
            actor_name="Root",
            actor_class="Actor",
            transform={"translation": [0, 0, 0]},
        )
    )
    catalog.upsert_scene_artifact(
        SceneArtifactUpsert(
            artifact_id="migration_scene_manifest",
            scene_id=scene.scene_id,
            kind="source_manifest",
            path="out/migration_scene/manifest.json",
        )
    )

    connection = sqlite3.connect(catalog.database_path)
    connection.row_factory = sqlite3.Row
    tables = ("assets", "artifacts", "scenes", "scene_objects", "scene_artifacts")
    before = {
        table: tuple(dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1"))
        for table in tables
    }
    connection.executescript(
        """
        DROP TABLE resource_bindings;
        DROP TABLE resource_artifacts;
        DROP TABLE resource_files;
        DROP TABLE resources;
        PRAGMA user_version = 3;
        """
    )
    connection.commit()
    connection.close()

    assert catalog.initialize() == SCHEMA_VERSION == 5
    connection = sqlite3.connect(catalog.database_path)
    connection.row_factory = sqlite3.Row
    try:
        after = {
            table: tuple(
                dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            )
            for table in tables
        }
        assert after == before
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_ready_hdri_finalization_is_atomic_queryable_and_idempotent(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    resource = _hdri_resource()
    files = (_hdri_file(resource),)
    artifacts = _hdri_artifacts(resource)

    first, first_files, first_artifacts = catalog.finalize_resource(resource, files, artifacts)
    second, second_files, second_artifacts = catalog.finalize_resource(resource, files, artifacts)

    assert first == second
    assert first_files == second_files
    assert first_artifacts == second_artifacts
    assert first.status == "ready"
    assert first_files[0].semantic_role == "environment_radiance"
    assert catalog.list_resources(
        resource_kind="hdri", status="ready", resolution="1k", tag="studio"
    ) == (first,)
    assert catalog.list_resources(license_tier="open") == (first,)
    assert catalog.resource_stats().as_dict() == {
        "total_resources": 1,
        "total_files": 1,
        "total_artifacts": 2,
        "total_bindings": 0,
        "by_kind": {"hdri": 1},
        "by_status": {"ready": 1},
        "by_source": {"polyhaven": 1},
        "by_license": {"CC0-1.0": 1},
        "by_license_tier": {"open": 1},
    }


def test_failed_partial_evidence_can_recover_without_becoming_published_lineage(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    ready = _hdri_resource()
    failed = replace(ready, status="failed", error={"reason": "validation pending"})
    source_file = _hdri_file(failed)
    source_manifest = _hdri_artifacts(failed)[0]

    catalog.upsert_resource(failed)
    catalog.upsert_resource_file(source_file)
    catalog.upsert_resource_artifact(source_manifest)
    with sqlite3.connect(catalog.database_path) as connection:
        assert connection.execute(
            "SELECT published_once FROM resources WHERE resource_id = ?",
            (failed.resource_id,),
        ).fetchone() == (0,)

    record, files, artifacts = catalog.finalize_resource(
        ready,
        (source_file,),
        _hdri_artifacts(ready),
    )

    assert record.status == "ready"
    assert len(files) == 1
    assert {item.kind for item in artifacts} == {
        "resource_source_manifest",
        "hdri_validation_manifest",
    }
    with sqlite3.connect(catalog.database_path) as connection:
        assert connection.execute(
            "SELECT published_once FROM resources WHERE resource_id = ?",
            (ready.resource_id,),
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE resources SET published_once = 0 WHERE resource_id = ?",
                (ready.resource_id,),
            )


def test_resource_text_rejects_terminal_control_characters(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    failed = replace(
        _hdri_resource(),
        status="failed",
        bundle_sha256=None,
        content_sha256=None,
        error={"reason": "fixture"},
    )

    with pytest.raises(CatalogValidationError, match="control characters"):
        catalog.upsert_resource(replace(failed, name="Forged\nready"))
    with pytest.raises(CatalogValidationError, match="control characters"):
        catalog.upsert_resource(replace(failed, attribution="Bell\x07Author"))


def test_resource_stats_reads_one_wal_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(tmp_path)
    catalog.initialize()
    original_connect = catalog._connect
    injected = False

    def traced_connect() -> sqlite3.Connection:
        connection = original_connect()

        def trace(statement: str) -> None:
            nonlocal injected
            if not injected and statement.startswith("SELECT count(*) FROM resource_files"):
                injected = True
                writer = _catalog(tmp_path)
                writer.upsert_resource(
                    replace(
                        _hdri_resource(),
                        status="failed",
                        bundle_sha256=None,
                        content_sha256=None,
                        error={"reason": "concurrent"},
                    )
                )

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(catalog, "_connect", traced_connect)
    stats = catalog.resource_stats()

    assert injected is True
    assert stats.total_resources == 0
    assert stats.by_kind == {}
    assert len(_catalog(tmp_path).list_resources()) == 1


def test_resource_cohort_reads_parent_and_children_from_one_wal_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(tmp_path)
    ready = _hdri_resource()
    failed = replace(
        ready,
        status="failed",
        bundle_sha256=None,
        content_sha256=None,
        error={"reason": "pending"},
    )
    catalog.upsert_resource(failed)
    original_connect = catalog._connect
    injected = False

    def traced_connect() -> sqlite3.Connection:
        connection = original_connect()

        def trace(statement: str) -> None:
            nonlocal injected
            if not injected and statement.startswith("SELECT * FROM resource_files"):
                injected = True
                writer = _catalog(tmp_path)
                writer.finalize_resource(
                    ready,
                    (_hdri_file(ready),),
                    _hdri_artifacts(ready),
                )

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(catalog, "_connect", traced_connect)
    cohort = catalog.get_resource_cohort(ready.resource_id)

    assert injected is True
    assert cohort is not None
    assert cohort.resource.status == "failed"
    assert cohort.files == ()
    assert cohort.artifacts == ()
    current = _catalog(tmp_path).get_resource_cohort(ready.resource_id)
    assert current is not None and current.resource.status == "ready"
    assert len(current.files) == 1


def test_verified_evidence_is_idempotent_immutable_and_can_upgrade_to_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(tmp_path)
    resource = _hdri_resource(status="verified")
    files = (_hdri_file(resource),)
    artifacts = _hdri_artifacts(resource)[:1]

    first = catalog.finalize_resource(resource, files, artifacts)
    monkeypatch.setattr("uefactory.catalog.database._utc_now", lambda: "2099-01-01T00:00:00Z")
    second = catalog.finalize_resource(resource, files, artifacts)
    assert second == first

    with pytest.raises(CatalogConflictError, match="immutable"):
        catalog.upsert_resource_file(replace(files[0], params={"mutated": True}))
    with pytest.raises(CatalogConflictError, match="published"):
        catalog.upsert_resource_file(
            replace(
                files[0],
                file_id="studio_small_03_preview",
                semantic_role="preview",
                is_primary=False,
            )
        )
    with pytest.raises(CatalogConflictError, match="published"):
        catalog.upsert_resource_artifact(
            replace(artifacts[0], params={"mutated": True}, sha256="f" * 64)
        )

    ready = replace(resource, status="ready")
    record, ready_files, ready_artifacts = catalog.finalize_resource(
        ready, files, _hdri_artifacts(ready)
    )
    assert record.status == "ready"
    assert ready_files[0].file_id == files[0].file_id
    assert {item.kind for item in ready_artifacts} == {
        "resource_source_manifest",
        "hdri_validation_manifest",
    }


def test_verified_ready_upgrade_cannot_replace_published_lineage(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    verified = _hdri_resource(status="verified")
    source_file = _hdri_file(verified)
    source_artifact = _hdri_artifacts(verified)[:1]
    catalog.finalize_resource(verified, (source_file,), source_artifact)

    with pytest.raises(CatalogConflictError, match="file evidence is immutable"):
        catalog.finalize_resource(
            replace(verified, status="ready"),
            (
                replace(
                    source_file,
                    source_url="https://dl.polyhaven.org/replaced_same_revision.hdr",
                ),
            ),
            _hdri_artifacts(replace(verified, status="ready")),
        )

    record = catalog.get_resource(verified.resource_id)
    assert record is not None and record.status == "verified"
    assert catalog.list_resource_files(resource_id=verified.resource_id)[0].source_url == (
        source_file.source_url
    )
    with pytest.raises(CatalogValidationError, match="illegal resource status transition"):
        catalog.upsert_resource(
            replace(verified, status="failed", error={"reason": "late failure"})
        )


def test_published_resource_rejects_new_optional_artifact(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    ready = _hdri_resource()
    catalog.finalize_resource(ready, (_hdri_file(ready),), _hdri_artifacts(ready))

    with pytest.raises(CatalogConflictError, match="artifact evidence is immutable"):
        catalog.upsert_resource_artifact(
            ResourceArtifactUpsert(
                artifact_id="studio_small_03_preview_manifest",
                resource_id=ready.resource_id,
                kind="preview_manifest",
                path="out/resources/studio_small_03/preview.json",
                params={"schema_version": 1},
                sha256="f" * 64,
            )
        )


def test_legacy_downgraded_published_lineage_remains_permanently_immutable(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    verified = _hdri_resource(status="verified")
    source_file = _hdri_file(verified)
    source_artifact = _hdri_artifacts(verified)[:1]
    catalog.finalize_resource(verified, (source_file,), source_artifact)
    with sqlite3.connect(catalog.database_path) as connection:
        connection.execute(
            "UPDATE resources SET status = 'failed', error_json = ? WHERE resource_id = ?",
            ('{"legacy":"downgrade"}', verified.resource_id),
        )
        connection.commit()
    before = catalog.database_path.read_bytes()

    with pytest.raises(CatalogConflictError, match="legacy published resource lineage"):
        catalog.finalize_resource(
            replace(verified, status="ready"),
            (source_file,),
            _hdri_artifacts(replace(verified, status="ready")),
        )
    with pytest.raises(CatalogConflictError, match="published resource file evidence"):
        catalog.upsert_resource_file(
            replace(
                source_file,
                file_id="legacy_replacement_radiance",
                source_url="https://dl.polyhaven.org/legacy-replacement.hdr",
            )
        )
    with pytest.raises(CatalogConflictError, match="artifact evidence is immutable"):
        catalog.upsert_resource_artifact(
            ResourceArtifactUpsert(
                artifact_id="legacy_preview_manifest",
                resource_id=verified.resource_id,
                kind="preview_manifest",
                path="out/resources/legacy/preview.json",
                params={"schema_version": 1},
                sha256="e" * 64,
            )
        )

    assert catalog.database_path.read_bytes() == before


def test_v4_migration_marks_downgraded_verified_cohort_as_published(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    verified = _hdri_resource(status="verified")
    source_file = _hdri_file(verified)
    source_artifact = _hdri_artifacts(verified)[:1]
    catalog.finalize_resource(verified, (source_file,), source_artifact)
    with sqlite3.connect(catalog.database_path) as connection:
        connection.execute(
            "UPDATE resources SET status = 'failed', error_json = ? WHERE resource_id = ?",
            ('{"legacy":"downgrade"}', verified.resource_id),
        )
        connection.execute("DROP TRIGGER resources_published_once_insert")
        connection.execute("DROP TRIGGER resources_published_once_update")
        connection.execute("ALTER TABLE resources DROP COLUMN published_once")
        connection.execute("PRAGMA user_version = 4")

    assert catalog.initialize() == 5
    with sqlite3.connect(catalog.database_path) as connection:
        assert connection.execute(
            "SELECT status, published_once FROM resources WHERE resource_id = ?",
            (verified.resource_id,),
        ).fetchone() == ("failed", 1)

    with pytest.raises(CatalogConflictError, match="legacy published resource lineage"):
        catalog.finalize_resource(
            replace(verified, status="ready", name="Replacement"),
            (
                replace(
                    source_file,
                    source_url="https://dl.polyhaven.org/replacement.hdr",
                ),
            ),
            _hdri_artifacts(replace(verified, status="ready")),
        )


def test_resource_file_path_uniqueness_is_scoped_to_its_resource(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    first = replace(
        _hdri_resource(),
        resource_id="failed_hdri_first",
        source_id="failed-first",
        source_url="https://polyhaven.com/a/failed-first",
        source_revision="1" * 40,
        status="failed",
        bundle_sha256=None,
        content_sha256=None,
        error={"reason": "download interrupted"},
    )
    second = replace(
        first,
        resource_id="failed_hdri_second",
        source_id="failed-second",
        source_url="https://polyhaven.com/a/failed-second",
        source_revision="2" * 40,
    )
    catalog.upsert_resources((first, second))
    shared_path = "data/resources/polyhaven/shared_radiance.hdr"
    first_file = replace(
        _hdri_file(first),
        file_id="failed_hdri_first_radiance",
        path=shared_path,
        is_primary=False,
    )
    second_file = replace(
        _hdri_file(second),
        file_id="failed_hdri_second_radiance",
        path=shared_path,
        is_primary=False,
    )

    records = catalog.upsert_resource_files((first_file, second_file))

    assert tuple(item.resource_id for item in records) == (
        first.resource_id,
        second.resource_id,
    )


def test_ready_evidence_rejects_missing_or_wrong_kind_specific_proof(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    hdri = _hdri_resource()

    with pytest.raises(CatalogValidationError, match="hdri_validation_manifest"):
        catalog.finalize_resource(hdri, (_hdri_file(hdri),), _hdri_artifacts(hdri)[:1])
    assert catalog.get_resource(hdri.resource_id) is None

    pbr = _pbr_resource()
    pbr_files = _pbr_files(pbr)
    bad_files = (
        pbr_files[0],
        replace(pbr_files[1], normal_convention="opengl"),
        pbr_files[2],
    )
    with pytest.raises(CatalogValidationError, match="DirectX normal"):
        catalog.finalize_resource(pbr, bad_files, _pbr_artifacts(pbr, bad_files))
    assert catalog.get_resource(pbr.resource_id) is None

    ambiguous_packed = (
        pbr_files[0],
        pbr_files[1],
        replace(
            pbr_files[2],
            semantic_role="packed_material",
            channels={
                "r": "ambient_occlusion",
                "g": "roughness",
                "b": "metallic",
                "a": "roughness",
            },
        ),
    )
    with pytest.raises(CatalogValidationError, match="distinct"):
        catalog.finalize_resource(pbr, ambiguous_packed, _pbr_artifacts(pbr, ambiguous_packed))
    assert catalog.get_resource(pbr.resource_id) is None

    record, files, artifacts = catalog.finalize_resource(
        pbr, pbr_files, _pbr_artifacts(pbr, pbr_files)
    )
    assert record.status == "ready"
    assert {item.semantic_role for item in files} == {"base_color", "normal", "roughness"}
    assert {item.kind for item in artifacts} == {
        "resource_source_manifest",
        "pbr_material_descriptor",
        "pbr_validation_manifest",
    }


def test_resource_status_transitions_fail_closed_and_ready_evidence_is_immutable(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    quarantined = replace(
        _hdri_resource(),
        status="quarantined",
        bundle_sha256=None,
        content_sha256=None,
        error={"reason": "manual review"},
    )
    catalog.upsert_resource(quarantined)
    with pytest.raises(CatalogValidationError, match="illegal resource status transition"):
        catalog.upsert_resource(replace(quarantined, status="failed", error={"reason": "changed"}))

    ready = replace(
        _hdri_resource(),
        resource_id="polyhaven_hdri_ready_immutable",
        source_id="ready_immutable",
        source_url="https://polyhaven.com/a/ready_immutable",
        source_revision="e" * 40,
    )
    ready_file = replace(
        _hdri_file(ready),
        file_id="ready_immutable_radiance",
    )
    ready_artifacts = tuple(
        replace(
            item,
            artifact_id=item.artifact_id.replace("studio_small_03", "ready_immutable"),
            resource_id=ready.resource_id,
            params={
                **dict(item.params or {}),
                "resource_id": ready.resource_id,
                **(
                    {"file_id": ready_file.file_id}
                    if item.kind == "hdri_validation_manifest"
                    else {}
                ),
            },
        )
        for item in _hdri_artifacts(ready)
    )
    catalog.finalize_resource(ready, (ready_file,), ready_artifacts)
    with pytest.raises(CatalogConflictError, match="immutable"):
        catalog.upsert_resource_file(
            replace(ready_file, file_id="ready_immutable_extra", semantic_role="preview")
        )
    with pytest.raises(CatalogConflictError, match="params_json"):
        catalog.upsert_resource_file(replace(ready_file, params={"mutated": True}))
    with pytest.raises(CatalogConflictError, match="name"):
        catalog.finalize_resource(
            replace(ready, name="Mutated Ready Name"),
            (ready_file,),
            ready_artifacts,
        )
    with pytest.raises(CatalogValidationError, match="illegal resource status transition"):
        catalog.upsert_resource(replace(ready, status="failed", error={"reason": "late failure"}))


def test_resource_bindings_enforce_ready_fk_and_exactly_one_consumer(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    ready = _hdri_resource()
    catalog.finalize_resource(ready, (_hdri_file(ready),), _hdri_artifacts(ready))
    catalog.upsert_asset(
        AssetUpsert(
            asset_id="binding_mesh",
            name="Binding Mesh",
            source="local",
            source_id="binding-mesh",
            source_url="file://localhost/binding.glb",
            license="CC0-1.0",
            license_tier="open",
            license_url="https://creativecommons.org/publicdomain/zero/1.0/",
            raw_path="data/raw/binding.glb",
            sha256="e" * 64,
        )
    )
    binding = catalog.upsert_resource_binding(
        ResourceBindingUpsert(
            binding_id="binding_mesh_lighting",
            resource_id=ready.resource_id,
            role="lighting_environment",
            asset_id="binding_mesh",
        )
    )
    assert catalog.list_resource_bindings(asset_id="binding_mesh") == (binding,)

    with pytest.raises(CatalogValidationError, match="exactly one"):
        catalog.upsert_resource_binding(
            ResourceBindingUpsert(
                binding_id="binding_invalid_consumers",
                resource_id=ready.resource_id,
                role="lighting_environment",
                asset_id="binding_mesh",
                scene_id="missing_scene",
            )
        )
    with pytest.raises(CatalogConflictError, match="FOREIGN KEY"):
        catalog.upsert_resource_binding(
            ResourceBindingUpsert(
                binding_id="binding_missing_asset",
                resource_id=ready.resource_id,
                role="preview_environment",
                asset_id="missing_asset",
            )
        )
