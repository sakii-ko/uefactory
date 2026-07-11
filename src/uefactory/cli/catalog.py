from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Annotated

import typer

from uefactory.catalog import LICENSE_TIERS, Catalog, CatalogValidationError, ResourceRecord
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
    try:
        records = catalog.list_assets(
            asset_id=asset_id,
            status=status,
            source=source,
            license=license_name,
            tag=tag,
            limit=limit,
            offset=offset,
        )
    except CatalogValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="catalog filters") from exc
    if json_output:
        typer.echo(json.dumps([record.as_dict() for record in records], indent=2, sort_keys=True))
        return
    typer.echo("asset_id\tstatus\tsource\tlicense\tname")
    for record in records:
        typer.echo(
            f"{_human_text(record.asset_id)}\t{_human_text(record.status)}\t"
            f"{_human_text(record.source)}\t{_human_text(record.license)}\t"
            f"{_human_text(record.name)}"
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
            safe_rendered = _escape_terminal_controls(rendered)
        else:
            rendered = "" if value is None else str(value)
            safe_rendered = _human_text(rendered)
        typer.echo(f"{key}: {safe_rendered}")


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


@catalog_app.command("resources")
def list_catalog_resources(
    ctx: typer.Context,
    resource_id: Annotated[
        str | None,
        typer.Option("--id", help="Filter by exact resource id."),
    ] = None,
    resource_kind: Annotated[
        str | None,
        typer.Option("--kind", help="Filter by resource kind."),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Filter by resource profile."),
    ] = None,
    resolution: Annotated[
        str | None,
        typer.Option("--resolution", help="Filter by exact resolution."),
    ] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", help="Filter by resource status."),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Filter by source."),
    ] = None,
    license_name: Annotated[
        str | None,
        typer.Option("--license", help="Filter by exact license identifier."),
    ] = None,
    license_tier: Annotated[
        str | None,
        typer.Option("--license-tier", help="Filter by license tier."),
    ] = None,
    tag: Annotated[
        str | None,
        typer.Option("--tag", help="Filter by exact tag."),
    ] = None,
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
    if license_tier is not None and license_tier not in LICENSE_TIERS:
        allowed = ", ".join(sorted(LICENSE_TIERS))
        raise typer.BadParameter(
            f"must be one of: {allowed}",
            param_hint="--license-tier",
        )
    catalog = _catalog_from_context(ctx, database)
    try:
        records = _list_resources(
            catalog,
            resource_id=resource_id,
            resource_kind=resource_kind,
            profile=profile,
            resolution=resolution,
            status=status,
            source=source,
            license_name=license_name,
            license_tier=license_tier,
            tag=tag,
            limit=limit,
            offset=offset,
        )
    except CatalogValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="resource filters") from exc
    if json_output:
        typer.echo(json.dumps([record.as_dict() for record in records], indent=2, sort_keys=True))
        return
    typer.echo("resource_id\tkind\tprofile\tresolution\tstatus\tsource\tlicense\ttier\tname")
    for record in records:
        typer.echo(
            f"{_human_text(record.resource_id)}\t{_human_text(record.resource_kind)}\t"
            f"{_human_text(record.profile)}\t{_human_text(record.resolution)}\t"
            f"{_human_text(record.status)}\t{_human_text(record.source)}\t"
            f"{_human_text(record.license)}\t{_human_text(record.license_tier)}\t"
            f"{_human_text(record.name)}"
        )


@catalog_app.command("resource-show")
def show_catalog_resource(
    ctx: typer.Context,
    resource_id: Annotated[str, typer.Argument(help="Exact catalog resource id.")],
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
    try:
        cohort = catalog.get_resource_cohort(resource_id)
    except CatalogValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="resource_id") from exc
    if cohort is None:
        typer.echo(f"Resource not found: {resource_id}", err=True)
        raise typer.Exit(code=1)
    payload = cohort.as_dict()
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    for key, value in payload.items():
        if isinstance(value, list | dict):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
            safe_rendered = _escape_terminal_controls(rendered)
        else:
            rendered = "" if value is None else str(value)
            safe_rendered = _human_text(rendered)
        typer.echo(f"{key}: {safe_rendered}")


@catalog_app.command("resource-stats")
def catalog_resource_stats(
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
    payload = catalog.resource_stats().as_dict()
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"Resources: {payload['total_resources']}")
    typer.echo(f"Files: {payload['total_files']}")
    typer.echo(f"Artifacts: {payload['total_artifacts']}")
    typer.echo(f"Bindings: {payload['total_bindings']}")
    for key in ("by_kind", "by_status", "by_source", "by_license", "by_license_tier"):
        typer.echo(f"{key}: {json.dumps(payload[key], ensure_ascii=False, sort_keys=True)}")


def _list_resources(
    catalog: Catalog,
    *,
    resource_id: str | None,
    resource_kind: str | None,
    profile: str | None,
    resolution: str | None,
    status: str | None,
    source: str | None,
    license_name: str | None,
    license_tier: str | None,
    tag: str | None,
    limit: int,
    offset: int,
) -> tuple[ResourceRecord, ...]:
    return catalog.list_resources(
        resource_id=resource_id,
        resource_kind=resource_kind,
        profile=profile,
        resolution=resolution,
        status=status,
        source=source,
        license=license_name,
        license_tier=license_tier,
        tag=tag,
        limit=limit,
        offset=offset,
    )


def _human_text(value: object) -> str:
    """Render one human-mode field without emitting terminal control bytes."""

    rendered = json.dumps(str(value), ensure_ascii=False)
    return _escape_terminal_controls(rendered[1:-1])


def _escape_terminal_controls(value: str) -> str:
    return "".join(
        f"\\u{ord(character):04x}" if unicodedata.category(character) == "Cc" else character
        for character in value
    )


def _catalog_from_context(ctx: typer.Context, database: Path | None) -> Catalog:
    settings = settings_from_context(ctx)
    if database is None:
        database_path = settings.data_dir / "catalog.db"
    elif database.is_absolute():
        database_path = database
    else:
        database_path = settings.project_root / database
    return Catalog(database_path, project_root=settings.project_root)
