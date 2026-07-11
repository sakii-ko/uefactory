from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import typer
from typer.testing import CliRunner

from uefactory.catalog import (
    SCHEMA_VERSION,
    ArtifactUpsert,
    AssetUpsert,
    Catalog,
    ResourceArtifactUpsert,
    ResourceBindingUpsert,
    ResourceFileUpsert,
    ResourceUpsert,
)
from uefactory.cli.catalog import catalog_app
from uefactory.core.config import Settings


def _app(tmp_path: Path) -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def root(ctx: typer.Context) -> None:
        ctx.obj = {"settings": Settings(project_root=tmp_path, data_dir=tmp_path / "data")}

    app.add_typer(catalog_app, name="catalog")
    return app


def _asset(index: int = 1) -> AssetUpsert:
    return AssetUpsert(
        asset_id=f"cli_asset_{index}",
        name=f"CLI asset {index}",
        source="local",
        source_id=f"asset-{index}",
        source_url=f"file://localhost/source/asset-{index}.glb",
        license="CC-BY-4.0",
        license_tier="open",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution="Example author",
        raw_path=f"data/raw/cli_asset_{index}.glb",
        sha256=f"{index:064x}",
        tags=("cli", "fixture"),
    )


def _ready_hdri() -> ResourceUpsert:
    return ResourceUpsert(
        resource_id="cli_hdri_studio_small_03",
        resource_kind="hdri",
        profile="radiance_hdr_v1",
        resolution="1k",
        name="CLI Studio Small 03",
        source="polyhaven",
        source_id="studio_small_03",
        source_url="https://polyhaven.com/a/studio_small_03",
        source_revision="d69ec09a43016714fd0dda163b3b0c585c968f56",
        source_revision_scheme="sha1_files_hash",
        license="CC0-1.0",
        license_tier="open",
        license_url="https://polyhaven.com/license",
        status="ready",
        tags=("cli", "studio"),
        bundle_sha256="b" * 64,
        content_sha256="c" * 64,
    )


def _finalize_ready_hdri(catalog: Catalog) -> ResourceUpsert:
    resource = _ready_hdri()
    file = ResourceFileUpsert(
        file_id="cli_hdri_studio_small_03_radiance",
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
    common = {
        "schema_version": 1,
        "resource_id": resource.resource_id,
        "resource_kind": resource.resource_kind,
        "profile": resource.profile,
        "resolution": resource.resolution,
        "bundle_sha256": resource.bundle_sha256,
        "content_sha256": resource.content_sha256,
    }
    catalog.finalize_resource(
        resource,
        (file,),
        (
            ResourceArtifactUpsert(
                artifact_id="cli_hdri_source_manifest",
                resource_id=resource.resource_id,
                kind="resource_source_manifest",
                path="out/resources/cli_hdri/source.json",
                params=common,
                sha256="4" * 64,
            ),
            ResourceArtifactUpsert(
                artifact_id="cli_hdri_validation_manifest",
                resource_id=resource.resource_id,
                kind="hdri_validation_manifest",
                path="out/resources/cli_hdri/validation.json",
                params={
                    **common,
                    "validation_status": "passed",
                    "width": 1024,
                    "height": 512,
                    "file_id": file.file_id,
                },
                sha256="5" * 64,
            ),
        ),
    )
    return resource


def _failed_pbr() -> ResourceUpsert:
    return ResourceUpsert(
        resource_id="cli_pbr_failed",
        resource_kind="pbr_texture_set",
        profile="ue_pbr_png_v1",
        resolution="2k",
        name="CLI Failed PBR",
        source="local",
        source_id="failed-pbr",
        source_url="file://localhost/failed-pbr",
        source_revision="fixture-revision",
        source_revision_scheme="fixture_revision",
        license="CC-BY-NC-4.0",
        license_tier="nc",
        license_url="https://creativecommons.org/licenses/by-nc/4.0/",
        status="failed",
        tags=("cli", "failed"),
        error={"reason": "fixture failure"},
    )


def test_catalog_cli_init_list_show_and_stats(tmp_path: Path) -> None:
    runner = CliRunner()
    app = _app(tmp_path)

    initialized = runner.invoke(app, ["catalog", "init", "--json"])
    assert initialized.exit_code == 0, initialized.output
    assert json.loads(initialized.stdout) == {
        "database": str((tmp_path / "data" / "catalog.db").resolve()),
        "schema_version": SCHEMA_VERSION,
    }

    catalog = Catalog(tmp_path / "data" / "catalog.db", project_root=tmp_path)
    catalog.upsert_assets((_asset(1), _asset(2)))
    catalog.upsert_artifact(
        ArtifactUpsert(
            artifact_id="cli_asset_1_thumbnail",
            asset_id="cli_asset_1",
            kind="thumbnail",
            path="out/thumbnails/cli_asset_1.png",
        )
    )

    listed = runner.invoke(
        app,
        ["catalog", "list", "--tag", "cli", "--license", "CC-BY-4.0", "--json"],
    )
    assert listed.exit_code == 0, listed.output
    assert [item["asset_id"] for item in json.loads(listed.stdout)] == [
        "cli_asset_1",
        "cli_asset_2",
    ]

    shown = runner.invoke(app, ["catalog", "show", "cli_asset_1", "--json"])
    assert shown.exit_code == 0, shown.output
    show_payload = json.loads(shown.stdout)
    assert show_payload["asset_id"] == "cli_asset_1"
    assert show_payload["artifacts"][0]["kind"] == "thumbnail"

    stats = runner.invoke(app, ["catalog", "stats", "--json"])
    assert stats.exit_code == 0, stats.output
    assert json.loads(stats.stdout) == {
        "by_license": {"CC-BY-4.0": 2},
        "by_license_tier": {"open": 2},
        "by_source": {"local": 2},
        "by_status": {"raw": 2},
        "total_artifacts": 1,
        "total_assets": 2,
    }


def test_catalog_cli_human_list_and_missing_show(tmp_path: Path) -> None:
    runner = CliRunner()
    app = _app(tmp_path)
    Catalog(tmp_path / "data" / "catalog.db", project_root=tmp_path).upsert_asset(_asset())

    listed = runner.invoke(app, ["catalog", "list", "--id", "cli_asset_1"])
    assert listed.exit_code == 0, listed.output
    assert "asset_id\tstatus\tsource\tlicense\tname" in listed.stdout
    assert "cli_asset_1\traw\tlocal\tCC-BY-4.0\tCLI asset 1" in listed.stdout

    missing = runner.invoke(app, ["catalog", "show", "missing_asset"])
    assert missing.exit_code == 1
    assert "Asset not found: missing_asset" in missing.output


def test_catalog_cli_accepts_explicit_database(tmp_path: Path) -> None:
    runner = CliRunner()
    database = Path("alternate/assets.sqlite3")

    result = runner.invoke(
        _app(tmp_path),
        ["catalog", "init", "--db", str(database), "--json"],
    )

    assert result.exit_code == 0, result.output
    expected = tmp_path / database
    assert expected.exists()
    assert json.loads(result.stdout)["database"] == str(expected.resolve())


def test_catalog_cli_resources_show_and_stats(tmp_path: Path) -> None:
    runner = CliRunner()
    app = _app(tmp_path)
    catalog = Catalog(tmp_path / "data" / "catalog.db", project_root=tmp_path)
    ready = _finalize_ready_hdri(catalog)
    catalog.upsert_resource(_failed_pbr())
    asset = catalog.upsert_asset(_asset())
    catalog.upsert_resource_binding(
        ResourceBindingUpsert(
            binding_id="cli_hdri_asset_lighting",
            resource_id=ready.resource_id,
            role="lighting_environment",
            asset_id=asset.asset_id,
        )
    )

    listed = runner.invoke(
        app,
        [
            "catalog",
            "resources",
            "--id",
            ready.resource_id,
            "--kind",
            "hdri",
            "--profile",
            "radiance_hdr_v1",
            "--resolution",
            "1k",
            "--status",
            "ready",
            "--source",
            "polyhaven",
            "--license",
            "CC0-1.0",
            "--license-tier",
            "open",
            "--tag",
            "studio",
            "--json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    assert [item["resource_id"] for item in json.loads(listed.stdout)] == [ready.resource_id]

    nc_listed = runner.invoke(
        app,
        ["catalog", "resources", "--license-tier", "nc", "--json"],
    )
    assert nc_listed.exit_code == 0, nc_listed.output
    assert [item["resource_id"] for item in json.loads(nc_listed.stdout)] == ["cli_pbr_failed"]

    shown = runner.invoke(app, ["catalog", "resource-show", ready.resource_id, "--json"])
    assert shown.exit_code == 0, shown.output
    show_payload = json.loads(shown.stdout)
    assert show_payload["resource_id"] == ready.resource_id
    assert show_payload["files"][0]["semantic_role"] == "environment_radiance"
    assert {item["kind"] for item in show_payload["artifacts"]} == {
        "hdri_validation_manifest",
        "resource_source_manifest",
    }
    assert show_payload["bindings"][0]["asset_id"] == asset.asset_id

    stats = runner.invoke(app, ["catalog", "resource-stats", "--json"])
    assert stats.exit_code == 0, stats.output
    assert json.loads(stats.stdout) == {
        "by_kind": {"hdri": 1, "pbr_texture_set": 1},
        "by_license": {"CC-BY-NC-4.0": 1, "CC0-1.0": 1},
        "by_license_tier": {"nc": 1, "open": 1},
        "by_source": {"local": 1, "polyhaven": 1},
        "by_status": {"failed": 1, "ready": 1},
        "total_artifacts": 2,
        "total_bindings": 1,
        "total_files": 1,
        "total_resources": 2,
    }


def test_catalog_cli_resources_human_missing_and_invalid_tier(tmp_path: Path) -> None:
    runner = CliRunner()
    app = _app(tmp_path)
    catalog = Catalog(tmp_path / "data" / "catalog.db", project_root=tmp_path)
    ready = _finalize_ready_hdri(catalog)

    listed = runner.invoke(
        app,
        ["catalog", "resources", "--license-tier", "open"],
    )
    assert listed.exit_code == 0, listed.output
    assert "resource_id\tkind\tprofile\tresolution\tstatus\tsource\tlicense\ttier\tname" in (
        listed.stdout
    )
    assert (
        f"{ready.resource_id}\thdri\tradiance_hdr_v1\t1k\tready\tpolyhaven\t"
        "CC0-1.0\topen\tCLI Studio Small 03"
    ) in listed.stdout

    human_stats = runner.invoke(app, ["catalog", "resource-stats"])
    assert human_stats.exit_code == 0, human_stats.output
    assert "Resources: 1" in human_stats.stdout
    assert "Files: 1" in human_stats.stdout
    assert 'by_kind: {"hdri": 1}' in human_stats.stdout

    missing = runner.invoke(app, ["catalog", "resource-show", "missing_resource"])
    assert missing.exit_code == 1
    assert "Resource not found: missing_resource" in missing.output

    invalid_tier = runner.invoke(
        app,
        ["catalog", "resources", "--license-tier", "restricted"],
    )
    assert invalid_tier.exit_code == 2
    assert "must be one of: nc, open, ue-only" in invalid_tier.output


def test_resource_cli_rejects_invalid_filters_without_traceback(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        _app(tmp_path),
        ["catalog", "resources", "--kind", "not_a_kind", "--json"],
    )

    assert result.exit_code == 2
    assert "resource_kind must be one of" in result.output
    assert "Traceback" not in result.output
    assert str(tmp_path) not in result.output


def test_resource_human_output_escapes_legacy_terminal_control_bytes(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    app = _app(tmp_path)
    catalog = Catalog(tmp_path / "data" / "catalog.db", project_root=tmp_path)
    resource = _failed_pbr()
    catalog.upsert_resource(resource)
    unsafe_name = "Normal\rFORGED\x07\x1b]2;TITLE\x07\u009b31mRED\u009b0m\x7ftail"
    with sqlite3.connect(catalog.database_path) as connection:
        connection.execute(
            "UPDATE resources SET name = ? WHERE resource_id = ?",
            (unsafe_name, resource.resource_id),
        )

    listed = runner.invoke(app, ["catalog", "resources", "--id", resource.resource_id])
    shown = runner.invoke(app, ["catalog", "resource-show", resource.resource_id])

    assert listed.exit_code == shown.exit_code == 0
    for output in (listed.stdout, shown.stdout):
        assert "\r" not in output
        assert "\x07" not in output
        assert "\x1b" not in output
        assert "\u009b" not in output
        assert "\x7f" not in output
        assert "\\r" in output
        assert "\\u0007" in output
        assert "\\u001b" in output
        assert "\\u009b" in output
        assert "\\u007f" in output
