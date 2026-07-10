from __future__ import annotations

from typing import Annotated

import typer

from uefactory.acquire.hdri import (
    DEFAULT_HDRI_ASSET,
    DEFAULT_HDRI_RESOLUTION,
    acquire_polyhaven_hdri,
)
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
