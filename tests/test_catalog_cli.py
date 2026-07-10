from __future__ import annotations

import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from uefactory.catalog import SCHEMA_VERSION, ArtifactUpsert, AssetUpsert, Catalog
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
