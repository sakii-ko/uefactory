from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
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


@node_app.command("provision")
def provision_node(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="Configured host name from [hosts.<name>].")],
    engine_zip: Annotated[
        Path,
        typer.Option("--engine-zip", help="Local UE engine zip to transfer."),
    ] = Path("/root/nas/bigdata1/cjw/Linux_Unreal_Engine_5.5.4.zip"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    settings = settings_from_context(ctx)
    remote = RemoteHost.from_settings(settings, host)
    engine_zip = engine_zip.resolve()
    if not engine_zip.exists():
        msg = f"Engine zip not found: {engine_zip}"
        raise FileNotFoundError(msg)

    job_id = f"provision_{remote.config.name}_{_utc_stamp()}"
    work_dir = PurePosixPath(str(remote.config.work_dir))
    package_dir = work_dir / "packages"
    zip_path = package_dir / engine_zip.name
    ready_path = package_dir / f"{engine_zip.name}.ready"

    check_command = remote_python_command(
        _NODE_PROVISION_CHECK_SCRIPT,
        {
            "UEF_HOST_NAME": remote.config.name,
            "UEF_WORK_DIR": str(remote.config.work_dir),
            "UEF_ENGINE_DIR": str(remote.config.engine_dir),
        },
    )
    check = parse_json_stdout(remote.run(check_command, timeout_sec=60).stdout)
    if check["status"] == "already_provisioned":
        if json_output:
            typer.echo(json.dumps(check, indent=2, sort_keys=True))
        else:
            typer.echo(f"Node {host} already provisioned: {remote.config.engine_dir}")
        return

    remote.run(
        "\n".join(
            [
                "set -euo pipefail",
                f"mkdir -p {shlex.quote(str(package_dir))}",
                f"rm -f {shlex.quote(str(ready_path))}",
            ]
        ),
        timeout_sec=60,
    )
    command = remote_python_command(
        _NODE_PROVISION_SCRIPT,
        {
            "UEF_HOST_NAME": remote.config.name,
            "UEF_WORK_DIR": str(remote.config.work_dir),
            "UEF_ENGINE_DIR": str(remote.config.engine_dir),
            "UEF_JOB_ID": job_id,
            "UEF_ENGINE_ZIP": str(zip_path),
            "UEF_ENGINE_ZIP_READY": str(ready_path),
        },
    )
    remote.tmux_start(job_id, command, timeout_sec=60)
    rsync_result = remote.rsync_push([engine_zip], f"{package_dir}/", timeout_sec=24 * 3600)
    remote.run(f"touch {shlex.quote(str(ready_path))}", timeout_sec=60)

    payload = {
        "host": remote.config.name,
        "job_id": job_id,
        "engine_dir": str(remote.config.engine_dir),
        "zip_path": str(zip_path),
        "ready_path": str(ready_path),
        "rsync_duration_sec": rsync_result.duration_sec,
        "rsync_mib_per_sec": round(
            engine_zip.stat().st_size / (1024 * 1024) / rsync_result.duration_sec,
            3,
        )
        if rsync_result.duration_sec > 0
        else None,
        "status": "extracting",
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(
            f"Provision transfer complete for {host}: job={job_id} "
            f"rsync={payload['rsync_mib_per_sec']} MiB/s"
        )


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


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


_NODE_PROVISION_CHECK_SCRIPT = r"""
import json
import os
from pathlib import Path

host_name = os.environ["UEF_HOST_NAME"]
work_dir = Path(os.environ["UEF_WORK_DIR"])
engine_dir = Path(os.environ["UEF_ENGINE_DIR"])
sentinel = work_dir / ".uef_node"
payload = json.loads(sentinel.read_text(encoding="utf-8"))
if payload.get("host") != host_name:
    raise SystemExit(f"sentinel host mismatch: {payload.get('host')} != {host_name}")
version_path = engine_dir / "Engine/Build/Build.version"
if version_path.exists():
    print(
        json.dumps(
            {
                "host": host_name,
                "status": "already_provisioned",
                "engine_dir": str(engine_dir),
                "version": json.loads(version_path.read_text(encoding="utf-8")),
            },
            sort_keys=True,
        )
    )
else:
    print(json.dumps({"host": host_name, "status": "needs_provision"}, sort_keys=True))
"""


_NODE_PROVISION_SCRIPT = r"""
import json
import os
import subprocess
import time
import traceback
from pathlib import Path

host_name = os.environ["UEF_HOST_NAME"]
work_dir = Path(os.environ["UEF_WORK_DIR"])
engine_dir = Path(os.environ["UEF_ENGINE_DIR"])
job_id = os.environ["UEF_JOB_ID"]
zip_path = Path(os.environ["UEF_ENGINE_ZIP"])
ready_path = Path(os.environ["UEF_ENGINE_ZIP_READY"])
status_path = work_dir / "jobs" / job_id / "status.json"
status_path.parent.mkdir(parents=True, exist_ok=True)


def write_status(status, phase, **extra):
    payload = {
        "job_id": job_id,
        "host": host_name,
        "status": status,
        "phase": phase,
        "work_dir": str(work_dir),
        "engine_dir": str(engine_dir),
        "zip_path": str(zip_path),
        "ready_path": str(ready_path),
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload.update(extra)
    tmp = status_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(status_path)


try:
    sentinel = work_dir / ".uef_node"
    payload = json.loads(sentinel.read_text(encoding="utf-8"))
    if payload.get("host") != host_name:
        raise RuntimeError(f"sentinel host mismatch: {payload.get('host')} != {host_name}")
    version_path = engine_dir / "Engine/Build/Build.version"
    if version_path.exists():
        version = json.loads(version_path.read_text(encoding="utf-8"))
        write_status("complete", "already_provisioned", version=version)
        raise SystemExit(0)
    write_status("running", "waiting_for_zip")
    while not ready_path.exists():
        time.sleep(10)
    if not zip_path.exists():
        raise RuntimeError(f"zip ready marker exists but zip is missing: {zip_path}")
    write_status("running", "extracting", zip_size=zip_path.stat().st_size)
    engine_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["unzip", "-oq", str(zip_path), "-d", str(engine_dir)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"unzip failed rc={result.returncode} stderr={result.stderr[-2000:]}")
    if not version_path.exists():
        raise RuntimeError(f"Build.version missing after extraction: {version_path}")
    version = json.loads(version_path.read_text(encoding="utf-8"))
    write_status("complete", "extracted", version=version)
except SystemExit:
    raise
except Exception as exc:
    write_status("failed", "error", error=str(exc), traceback=traceback.format_exc())
    raise
"""
