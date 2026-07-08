from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from uefactory.cli._common import settings_from_context
from uefactory.render.mrq_spike import compare_spike_luma, render_mrq_spike
from uefactory.render.smoke import render_smoke, render_smoke_remote

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
    host: Annotated[
        str | None,
        typer.Option("--host", help="Run smoke render on a configured remote host."),
    ] = None,
) -> None:
    settings = settings_from_context(ctx)
    result = (
        render_smoke(settings=settings, out_root=out, timeout_sec=timeout_sec)
        if host is None
        else render_smoke_remote(
            settings=settings,
            host=host,
            out_root=out,
            timeout_sec=timeout_sec,
        )
    )
    typer.echo(f"Smoke render OK: {result.frame_path}")
    typer.echo(f"Manifest: {result.manifest_path}")


@render_app.command("mrq-spike")
def mrq_spike(
    ctx: typer.Context,
    out: Annotated[
        Path,
        typer.Option("--out", help="Output directory for MRQ spike runs."),
    ] = Path("out/mrq_spike"),
    timeout_sec: Annotated[
        int,
        typer.Option("--timeout-sec", help="UE process timeout in seconds."),
    ] = 1800,
    verify_twice: Annotated[
        bool,
        typer.Option("--verify-twice", help="Run twice and require identical frame luma."),
    ] = False,
) -> None:
    settings = settings_from_context(ctx)
    first = render_mrq_spike(settings=settings, out_root=out, timeout_sec=timeout_sec)
    typer.echo(f"MRQ spike OK: {first.run_dir}")
    typer.echo(f"Frames: {len(first.frame_paths)}")
    typer.echo(f"Manifest: {first.manifest_path}")
    if verify_twice:
        second = render_mrq_spike(settings=settings, out_root=out, timeout_sec=timeout_sec)
        compare_spike_luma(first, second)
        typer.echo(f"MRQ spike repeat OK: {second.run_dir}")
