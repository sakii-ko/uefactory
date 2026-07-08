from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from uefactory.core.config import Settings
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
    settings = _settings_from_context(ctx)
    result = render_smoke(settings=settings, out_root=out, timeout_sec=timeout_sec)
    typer.echo(f"Smoke render OK: {result.frame_path}")
    typer.echo(f"Manifest: {result.manifest_path}")


def _settings_from_context(ctx: typer.Context) -> Settings:
    obj = ctx.find_root().obj or {}
    settings = obj.get("settings")
    if not isinstance(settings, Settings):
        msg = "CLI settings were not initialized"
        raise RuntimeError(msg)
    return settings
