from __future__ import annotations

import typer

from uefactory.core.config import Settings


def settings_from_context(ctx: typer.Context) -> Settings:
    obj = ctx.find_root().obj or {}
    settings = obj.get("settings")
    if not isinstance(settings, Settings):
        msg = "CLI settings were not initialized"
        raise RuntimeError(msg)
    return settings
