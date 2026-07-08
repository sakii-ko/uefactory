from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from uefactory.cli._common import settings_from_context
from uefactory.render.smoke import render_smoke

render_app = typer.Typer(help="Render UEFactory jobs.")


@render_app.command()
def smoke(
    ctx: typer.Context,
    out: Annotated[
        Path,
        typer.Option("--out", help="Output directory for smoke render runs."),
    ] = Path("out/smoke"),
    timeout_sec: Annotated[
        int,
        typer.Option("--timeout-sec", help="UE process timeout in seconds."),
    ] = 1800,
) -> None:
    settings = settings_from_context(ctx)
    result = render_smoke(settings=settings, out_root=out, timeout_sec=timeout_sec)
    typer.echo(f"Smoke render OK: {result.frame_path}")
    typer.echo(f"Manifest: {result.manifest_path}")
