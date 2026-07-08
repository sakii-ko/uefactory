from __future__ import annotations

import json
from typing import Annotated

import typer

from uefactory.cli._common import settings_from_context
from uefactory.core.remote import RemoteHost, parse_json_stdout, remote_python_command

node_app = typer.Typer(help="Manage remote UEFactory worker nodes.")


@node_app.command("init")
def init_node(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="Configured host name from [hosts.<name>].")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    settings = settings_from_context(ctx)
    remote = RemoteHost.from_settings(settings, host)
    command = remote_python_command(
        _NODE_INIT_SCRIPT,
        {
            "UEF_HOST_NAME": remote.config.name,
            "UEF_SSH_ALIAS": remote.config.ssh_alias,
            "UEF_WORK_DIR": str(remote.config.work_dir),
            "UEF_ENGINE_DIR": str(remote.config.engine_dir),
            "UEF_GPU": remote.config.gpu or "",
        },
    )
    result = remote.run(command, timeout_sec=60)
    payload = parse_json_stdout(result.stdout)
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(
            f"Node {payload['host']} ready: {payload['work_dir']} "
            f"({payload['status']}, id={payload['node_id']})"
        )


_NODE_INIT_SCRIPT = r"""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

host_name = os.environ["UEF_HOST_NAME"]
work_dir = Path(os.environ["UEF_WORK_DIR"])
engine_dir = Path(os.environ["UEF_ENGINE_DIR"])
work_dir.mkdir(parents=True, exist_ok=True)
sentinel = work_dir / ".uef_node"

if sentinel.exists():
    payload = json.loads(sentinel.read_text(encoding="utf-8"))
    if payload.get("host") != host_name:
        raise SystemExit(
            f"refusing to reuse {sentinel}: host={payload.get('host')} expected={host_name}"
        )
    payload["status"] = "existing"
else:
    payload = {
        "schema_version": 1,
        "host": host_name,
        "ssh_alias": os.environ["UEF_SSH_ALIAS"],
        "work_dir": str(work_dir),
        "engine_dir": str(engine_dir),
        "gpu": os.environ.get("UEF_GPU") or None,
        "initialized_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "node_id": uuid.uuid4().hex,
        "status": "created",
    }
    tmp = sentinel.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(sentinel)

print(json.dumps(payload, sort_keys=True))
"""
