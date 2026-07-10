from __future__ import annotations

import sys
from typing import Annotated

import typer

from uefactory import __version__
from uefactory.cli.acquire import acquire_app
from uefactory.cli.doctor import doctor_app
from uefactory.cli.node import node_app
from uefactory.cli.render import render_app
from uefactory.core.config import load_settings
from uefactory.core.log import configure_logging

app = typer.Typer(
    help="UEFactory command line tools.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, help="Print UEFactory version."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show DEBUG logs.")] = False,
) -> None:
    del version
    settings = load_settings()
    command_parts = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    command_name = "_".join(command_parts[:2]) if command_parts else "uef"
    log_path = configure_logging(
        settings=settings,
        argv=sys.argv,
        command_name=command_name,
        verbose=verbose,
    )
    ctx.obj = {"settings": settings, "log_path": log_path, "verbose": verbose}


app.add_typer(doctor_app, name="doctor")
app.add_typer(acquire_app, name="acquire")
app.add_typer(node_app, name="node")
app.add_typer(render_app, name="render")
