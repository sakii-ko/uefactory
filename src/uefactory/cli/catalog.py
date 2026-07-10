from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from uefactory.catalog import Catalog
from uefactory.cli._common import settings_from_context

catalog_app = typer.Typer(
    help="Inspect and initialize the local asset catalog.",
    no_args_is_help=True,
)


@catalog_app.command("init")
def init_catalog(
    ctx: typer.Context,
    database: Annotated[
        Path | None,
        typer.Option("--database", "--db", help="Catalog database (default: data/catalog.db)."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    catalog = _catalog_from_context(ctx, database)
    version = catalog.initialize()
    payload = {"database": str(catalog.database_path), "schema_version": version}
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(f"Catalog ready: {catalog.database_path} (schema v{version})")


@catalog_app.command("list")
def list_catalog(
    ctx: typer.Context,
    asset_id: Annotated[str | None, typer.Option("--id", help="Filter by exact asset id.")] = None,
    status: Annotated[str | None, typer.Option("--status", help="Filter by asset status.")] = None,
    source: Annotated[str | None, typer.Option("--source", help="Filter by source.")] = None,
    license_name: Annotated[
        str | None,
        typer.Option("--license", help="Filter by exact license identifier."),
    ] = None,
    tag: Annotated[str | None, typer.Option("--tag", help="Filter by exact tag.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=10_000)] = 100,
    offset: Annotated[int, typer.Option("--offset", min=0)] = 0,
    database: Annotated[
        Path | None,
        typer.Option("--database", "--db", help="Catalog database (default: data/catalog.db)."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    catalog = _catalog_from_context(ctx, database)
    records = catalog.list_assets(
        asset_id=asset_id,
        status=status,
        source=source,
        license=license_name,
        tag=tag,
        limit=limit,
        offset=offset,
    )
    if json_output:
        typer.echo(json.dumps([record.as_dict() for record in records], indent=2, sort_keys=True))
        return
    typer.echo("asset_id\tstatus\tsource\tlicense\tname")
    for record in records:
        typer.echo(
            f"{record.asset_id}\t{record.status}\t{record.source}\t{record.license}\t{record.name}"
        )


@catalog_app.command("show")
def show_catalog_asset(
    ctx: typer.Context,
    asset_id: Annotated[str, typer.Argument(help="Exact catalog asset id.")],
    database: Annotated[
        Path | None,
        typer.Option("--database", "--db", help="Catalog database (default: data/catalog.db)."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    catalog = _catalog_from_context(ctx, database)
    record = catalog.show_asset(asset_id)
    if record is None:
        typer.echo(f"Asset not found: {asset_id}", err=True)
        raise typer.Exit(code=1)
    payload = record.as_dict()
    payload["artifacts"] = [item.as_dict() for item in catalog.list_artifacts(asset_id=asset_id)]
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    for key, value in payload.items():
        if isinstance(value, list | dict):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            rendered = "" if value is None else str(value)
        typer.echo(f"{key}: {rendered}")


@catalog_app.command("stats")
def catalog_stats(
    ctx: typer.Context,
    database: Annotated[
        Path | None,
        typer.Option("--database", "--db", help="Catalog database (default: data/catalog.db)."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    catalog = _catalog_from_context(ctx, database)
    payload = catalog.stats().as_dict()
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"Assets: {payload['total_assets']}")
    typer.echo(f"Artifacts: {payload['total_artifacts']}")
    for key in ("by_status", "by_source", "by_license", "by_license_tier"):
        typer.echo(f"{key}: {json.dumps(payload[key], ensure_ascii=False, sort_keys=True)}")


def _catalog_from_context(ctx: typer.Context, database: Path | None) -> Catalog:
    settings = settings_from_context(ctx)
    if database is None:
        database_path = settings.data_dir / "catalog.db"
    elif database.is_absolute():
        database_path = database
    else:
        database_path = settings.project_root / database
    return Catalog(database_path, project_root=settings.project_root)
