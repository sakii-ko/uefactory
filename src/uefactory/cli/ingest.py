from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from uefactory.cli._common import settings_from_context
from uefactory.ingest.executor import ingest_asset
from uefactory.ingest.pipeline import ingest_batch
from uefactory.ingest.spec import IngestSpecError
from uefactory.render.thumbnails import thumbnail_catalog_asset

ingest_app = typer.Typer(
    help="Stage and import local FBX/glTF assets into Unreal Engine.",
    no_args_is_help=True,
)


@ingest_app.command("asset")
def ingest_one_asset(
    ctx: typer.Context,
    source_file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Local FBX, glTF, or GLB file.",
        ),
    ],
    asset_id: Annotated[
        str,
        typer.Option("--asset-id", "--id", help="Stable lowercase snake_case asset id."),
    ],
    timeout_sec: Annotated[
        int,
        typer.Option("--timeout-sec", min=1, help="Timeout for each UE process."),
    ] = 1800,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Optional ingest run root (default: out/ingest)."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    settings = settings_from_context(ctx)
    out_root = None
    if out is not None:
        out_root = out if out.is_absolute() else settings.project_root / out
    result = ingest_asset(
        settings=settings,
        asset_id=asset_id,
        source_file=source_file,
        out_root=out_root,
        timeout_sec=timeout_sec,
    )
    payload = {
        "asset_id": result.asset_id,
        "run_dir": str(result.run_dir),
        "manifest": str(result.manifest_path),
        "ue_log": str(result.ue_log_path),
        "reload_log": str(result.reload_log_path),
        "finalize_log": str(result.finalize_log_path),
        "imported_object_paths": list(result.imported_object_paths),
        "static_mesh_paths": list(result.static_mesh_paths),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"Asset imported: {result.asset_id}")
    typer.echo(f"StaticMesh: {', '.join(result.static_mesh_paths)}")
    typer.echo(f"Manifest: {result.manifest_path}")


@ingest_app.command("batch")
def ingest_asset_batch(
    ctx: typer.Context,
    manifest_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Strict ingest YAML manifest.",
        ),
    ],
    database: Annotated[
        Path | None,
        typer.Option("--database", "--db", help="Catalog database (default: data/catalog.db)."),
    ] = None,
    timeout_sec: Annotated[
        int,
        typer.Option("--timeout-sec", min=1, help="Timeout for each UE process."),
    ] = 1800,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
    thumbnails: Annotated[
        bool,
        typer.Option(
            "--thumbnails/--no-thumbnails",
            help="Render and catalog the standard beauty/mask thumbnail set.",
        ),
    ] = True,
) -> None:
    settings = settings_from_context(ctx)
    try:
        result = ingest_batch(
            settings=settings,
            manifest_path=manifest_path,
            database_path=database,
            timeout_sec=timeout_sec,
            render_thumbnails=thumbnails,
        )
    except IngestSpecError as exc:
        typer.echo(f"Invalid ingest manifest {manifest_path}: {exc}", err=True)
        raise typer.Exit(2) from exc

    payload = {
        "status": result.status,
        "run_dir": str(result.run_dir),
        "manifest": str(result.manifest_path),
        "catalog": str(result.catalog_path),
        "assets": [
            {
                "asset_id": asset.asset_id,
                "status": asset.status,
                "bundle_sha256": asset.bundle_sha256,
                "content_sha256": asset.content_sha256,
                "raw_path": None if asset.raw_path is None else str(asset.raw_path),
                "ingest_manifest": None
                if asset.ingest_manifest is None
                else str(asset.ingest_manifest),
                "thumbnail_manifest": None
                if asset.thumbnail_manifest is None
                else str(asset.thumbnail_manifest),
                "catalog_status": asset.catalog_status,
                "error": asset.error,
            }
            for asset in result.assets
        ],
        "report": None
        if result.report is None
        else {
            "contact_sheet": str(result.report.contact_sheet),
            "index_html": str(result.report.index_html),
            "thumbnails": [
                {
                    "asset_id": item.asset_id,
                    "path": str(item.path),
                    "sha256": item.sha256,
                }
                for item in result.report.thumbnails
            ],
        },
        "report_error": result.report_error,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(f"Batch status: {result.status}")
        for asset in result.assets:
            typer.echo(f"{asset.asset_id}: {asset.status}")
        typer.echo(f"Catalog: {result.catalog_path}")
        typer.echo(f"Manifest: {result.manifest_path}")
        if result.report is not None:
            typer.echo(f"Contact sheet: {result.report.contact_sheet}")
            typer.echo(f"Index: {result.report.index_html}")
        if result.report_error is not None:
            typer.echo(
                "Report failed: "
                f"{result.report_error.get('type', 'Error')}: "
                f"{result.report_error.get('message', '<no message>')}"
            )
    if result.status != "ok":
        raise typer.Exit(1)


@ingest_app.command("thumbnail")
def thumbnail_asset(
    ctx: typer.Context,
    asset_id: Annotated[str, typer.Argument(help="Imported catalog asset id.")],
    database: Annotated[
        Path | None,
        typer.Option("--database", "--db", help="Catalog database (default: data/catalog.db)."),
    ] = None,
    timeout_sec: Annotated[
        int,
        typer.Option("--timeout-sec", min=1, help="Timeout for each UE process."),
    ] = 1800,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    settings = settings_from_context(ctx)
    result = thumbnail_catalog_asset(
        settings=settings,
        asset_id=asset_id,
        database_path=database,
        timeout_sec=timeout_sec,
    )
    payload = {
        "asset_id": result.asset_id,
        "status": "render_ok",
        "thumbnail": str(result.thumbnail_path),
        "subject_mask": str(result.subject_mask_path),
        "render_manifest": str(result.render.manifest_path),
        "contact_sheet": str(result.render.artifacts.contact_sheet)
        if result.render.artifacts is not None
        else None,
        "catalog": str(result.catalog_path),
        "artifact_ids": list(result.artifact_ids),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"Thumbnail ready: {result.thumbnail_path}")
    typer.echo(f"Subject mask: {result.subject_mask_path}")
    typer.echo(f"Catalog: {result.catalog_path}")
