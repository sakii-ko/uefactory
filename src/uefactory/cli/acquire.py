from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from uefactory.acquire.blackmyth import BlackMythLibraryError, scan_blackmyth_scene_library
from uefactory.acquire.hdri import (
    DEFAULT_HDRI_ASSET,
    DEFAULT_HDRI_RESOLUTION,
    acquire_polyhaven_hdri,
)
from uefactory.acquire.models import ModelAcquireError, acquire_m2_models
from uefactory.cli._common import settings_from_context

acquire_app = typer.Typer(help="Acquire runtime assets.")


@acquire_app.command("hdri")
def acquire_hdri(
    ctx: typer.Context,
    asset_id: Annotated[
        str,
        typer.Option("--asset-id", help="PolyHaven HDRI asset id."),
    ] = DEFAULT_HDRI_ASSET,
    resolution: Annotated[
        str,
        typer.Option("--resolution", help="PolyHaven HDRI resolution, e.g. 1k or 2k."),
    ] = DEFAULT_HDRI_RESOLUTION,
    force: Annotated[
        bool,
        typer.Option("--force", help="Redownload even when the checked file already exists."),
    ] = False,
) -> None:
    settings = settings_from_context(ctx)
    result = acquire_polyhaven_hdri(
        settings=settings,
        asset_id=asset_id,
        resolution=resolution,
        force=force,
    )
    action = "reused" if result.skipped else "downloaded"
    typer.echo(f"HDRI {action}: {result.file_path}")
    typer.echo(f"Metadata: {result.metadata_path}")
    typer.echo(f"License: {result.license}")


@acquire_app.command("models")
def acquire_models(
    ctx: typer.Context,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Redownload all model files and replace them only after exact verification.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a machine-readable acquisition summary."),
    ] = False,
) -> None:
    """Acquire the pinned M2 open-license sample set."""

    settings = settings_from_context(ctx)
    try:
        result = acquire_m2_models(settings=settings, force=force)
    except ModelAcquireError as exc:
        typer.echo(f"Model acquisition failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    payload = {
        "root_dir": str(result.root_dir),
        "inventory_path": str(result.inventory_path),
        "models": len(result.models),
        "downloaded_files": result.downloaded_files,
        "reused_files": result.reused_files,
        "bytes": result.bytes,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"Models ready: {len(result.models)} in {result.root_dir}")
    typer.echo(
        f"Files: {result.downloaded_files} downloaded, {result.reused_files} reused; "
        f"{result.bytes} bytes verified"
    )
    typer.echo(f"Inventory: {result.inventory_path}")


@acquire_app.command("blackmyth")
def scan_blackmyth(
    root: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            help="Approved BlackMyth/external library root to scan read-only.",
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit checked records and quarantined entries."),
    ] = False,
) -> None:
    """Safely discover scene GLBs without copying or modifying the source library."""

    try:
        result = scan_blackmyth_scene_library(root)
    except BlackMythLibraryError as exc:
        typer.echo(f"BlackMyth scene scan failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    open_count = sum(item.license_tier == "open" for item in result.records)
    nc_count = sum(item.license_tier == "nc" for item in result.records)
    payload = {
        "root": str(result.root),
        "records": [item.as_dict() for item in result.records],
        "quarantined": [item.as_dict() for item in result.quarantined],
        "counts": {
            "records": len(result.records),
            "open": open_count,
            "nc": nc_count,
            "quarantined": len(result.quarantined),
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"Scene GLBs: {len(result.records)} checked")
    typer.echo(
        f"License tiers: {open_count} open, {nc_count} nc; {len(result.quarantined)} quarantined"
    )
    for record in result.records:
        typer.echo(
            f"{record.library_uid}: {record.title} [{record.license}] "
            f"{record.bytes} bytes {record.sha256[:12]}"
        )
