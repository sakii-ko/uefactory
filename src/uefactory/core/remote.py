from __future__ import annotations

import json
import logging
import posixpath
import shlex
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from uefactory.core.config import HostConfig, Settings

LOGGER = logging.getLogger(__name__)

SSH_CONTROL_OPTIONS = [
    "-o",
    "ControlMaster=auto",
    "-o",
    "ControlPath=~/.ssh/uef_cm_%r@%h-%p",
    "-o",
    "ControlPersist=900",
    "-o",
    "BatchMode=yes",
]


@dataclass(frozen=True)
class RemoteCommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_sec: float


class RemoteCommandError(RuntimeError):
    def __init__(self, result: RemoteCommandResult) -> None:
        self.result = result
        message = (
            f"Remote command failed with exit code {result.returncode}; "
            f"command={' '.join(result.command)}; stderr={result.stderr.strip()}"
        )
        super().__init__(message)


class RemoteHost:
    def __init__(
        self,
        config: HostConfig,
        *,
        local_delete_roots: Sequence[Path] | None = None,
    ) -> None:
        self.config = config
        self._local_delete_roots = tuple(root.resolve() for root in local_delete_roots or ())

    @classmethod
    def from_settings(cls, settings: Settings, name: str) -> RemoteHost:
        try:
            config = settings.hosts[name]
        except KeyError as exc:
            available = ", ".join(sorted(settings.hosts)) or "(none)"
            msg = f"Unknown host {name!r}; configured hosts: {available}"
            raise KeyError(msg) from exc
        return cls(
            config,
            local_delete_roots=(
                settings.data_dir,
                settings.project_root / "out",
            ),
        )

    def run(
        self, command: str, *, timeout_sec: int = 60, check: bool = True
    ) -> RemoteCommandResult:
        argv = ["ssh", *SSH_CONTROL_OPTIONS, self.config.ssh_alias, command]
        return _run_local(argv, timeout_sec=timeout_sec, check=check)

    def rsync_push(
        self,
        sources: Sequence[Path | str],
        remote_dest: Path | str,
        *,
        timeout_sec: int = 3600,
        delete: bool = False,
    ) -> RemoteCommandResult:
        if delete:
            self._validate_delete_target(remote_dest)
            self.verify_sentinel(timeout_sec=60)
        argv = ["rsync", "-a", "-z", "--partial", "-e", self._rsync_ssh_transport()]
        if delete:
            argv.append("--delete")
        argv.extend(str(source) for source in sources)
        argv.append(f"{self.config.ssh_alias}:{remote_dest}")
        return _run_local(argv, timeout_sec=timeout_sec, check=True)

    def rsync_pull(
        self,
        remote_sources: Sequence[Path | str],
        local_dest: Path | str,
        *,
        timeout_sec: int = 3600,
        delete: bool = False,
    ) -> RemoteCommandResult:
        if delete:
            self._validate_local_delete_target(local_dest)
            LOGGER.debug(
                "Pull --delete guarded by local destination check: %s",
                local_dest,
            )
        argv = ["rsync", "-a", "-z", "--partial", "-e", self._rsync_ssh_transport()]
        if delete:
            argv.append("--delete")
        argv.extend(f"{self.config.ssh_alias}:{source}" for source in remote_sources)
        argv.append(str(local_dest))
        return _run_local(argv, timeout_sec=timeout_sec, check=True)

    def verify_sentinel(self, *, timeout_sec: int = 60) -> dict[str, Any]:
        command = remote_python_command(
            _VERIFY_SENTINEL_SCRIPT,
            {
                "UEF_HOST_NAME": self.config.name,
                "UEF_WORK_DIR": str(self.config.work_dir),
            },
        )
        result = self.run(command, timeout_sec=timeout_sec)
        return parse_json_stdout(result.stdout)

    def tmux_start(
        self, job_id: str, command: str, *, timeout_sec: int = 60
    ) -> RemoteCommandResult:
        _validate_job_id(job_id)
        session = _tmux_session_name(job_id)
        status_path = PurePosixPath(str(self.config.work_dir)) / "jobs" / job_id / "status.json"
        remote_command = "\n".join(
            [
                "set -euo pipefail",
                f"mkdir -p {shlex.quote(posixpath.dirname(str(status_path)))}",
                f"cat > {shlex.quote(str(status_path))} <<'JSON'",
                json.dumps({"status": "running", "job_id": job_id}, sort_keys=True),
                "JSON",
                (
                    f"tmux new-session -d -s {shlex.quote(session)} -- "
                    f"bash -lc {shlex.quote(command)}"
                ),
            ]
        )
        return self.run(remote_command, timeout_sec=timeout_sec)

    def tmux_status(self, job_id: str, *, timeout_sec: int = 60) -> dict[str, Any]:
        _validate_job_id(job_id)
        session = _tmux_session_name(job_id)
        status_path = PurePosixPath(str(self.config.work_dir)) / "jobs" / job_id / "status.json"
        remote_command = "\n".join(
            [
                "set -euo pipefail",
                (
                    f"if tmux has-session -t {shlex.quote(session)} 2>/dev/null; "
                    "then live=true; else live=false; fi"
                ),
                (
                    f"if [[ -f {shlex.quote(str(status_path))} ]]; then "
                    f"cat {shlex.quote(str(status_path))}; else echo '{{}}'; fi"
                ),
                'echo ""',
                'echo "__UEF_TMUX_LIVE__${live}"',
            ]
        )
        result = self.run(remote_command, timeout_sec=timeout_sec)
        payload = parse_json_stdout(result.stdout)
        payload["tmux_live"] = "__UEF_TMUX_LIVE__true" in result.stdout.splitlines()
        return payload

    def tmux_stop(self, job_id: str, *, timeout_sec: int = 60) -> RemoteCommandResult:
        _validate_job_id(job_id)
        session = _tmux_session_name(job_id)
        command = remote_python_command(
            _STOP_REMOTE_JOB_SCRIPT,
            {
                "UEF_HOST_NAME": self.config.name,
                "UEF_WORK_DIR": str(self.config.work_dir),
                "UEF_JOB_ID": job_id,
                "UEF_TMUX_SESSION": session,
            },
        )
        return self.run(command, timeout_sec=timeout_sec)

    def remove_tree(self, remote_path: Path | str, *, timeout_sec: int = 60) -> RemoteCommandResult:
        self._validate_delete_target(remote_path)
        self.verify_sentinel(timeout_sec=60)
        command = f"rm -rf -- {shlex.quote(str(remote_path))}"
        return self.run(command, timeout_sec=timeout_sec)

    def _rsync_ssh_transport(self) -> str:
        return " ".join(shlex.quote(part) for part in ["ssh", *SSH_CONTROL_OPTIONS])

    def _validate_delete_target(self, remote_path: Path | str) -> None:
        work_dir = PurePosixPath(str(self.config.work_dir))
        target = PurePosixPath(str(remote_path))
        if not target.is_absolute():
            msg = f"Refusing --delete on relative remote path: {remote_path}"
            raise ValueError(msg)
        if target != work_dir and work_dir not in target.parents:
            msg = f"Refusing --delete outside sentinel work_dir {work_dir}: {remote_path}"
            raise ValueError(msg)

    def _validate_local_delete_target(self, local_path: Path | str) -> None:
        if not self._local_delete_roots:
            msg = "Refusing pull --delete without configured local delete roots"
            raise ValueError(msg)
        target = Path(local_path).resolve()
        if any(target == root or root in target.parents for root in self._local_delete_roots):
            return
        roots = ", ".join(str(root) for root in self._local_delete_roots)
        msg = f"Refusing pull --delete outside local roots ({roots}): {target}"
        raise ValueError(msg)


def remote_python_command(script: str, env: Mapping[str, str]) -> str:
    assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    prefix = f"{assignments} " if assignments else ""
    return f"{prefix}python3 - <<'PY'\n{script.rstrip()}\nPY"


def parse_json_stdout(stdout: str) -> dict[str, Any]:
    stripped = stdout.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        msg = f"Remote command did not emit JSON: {stdout[:500]}"
        raise ValueError(msg)
    value = json.loads(stripped[start : end + 1])
    if not isinstance(value, dict):
        msg = f"Remote JSON payload is not an object: {type(value).__name__}"
        raise ValueError(msg)
    return value


def _run_local(
    argv: list[str],
    *,
    timeout_sec: int,
    check: bool,
) -> RemoteCommandResult:
    LOGGER.info("Starting remote command: %s", " ".join(shlex.quote(part) for part in argv))
    start = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_sec = round(time.monotonic() - start, 3)
        stdout = _decode_timeout_output(exc.stdout)
        stderr = _decode_timeout_output(exc.stderr)
        remote_result = RemoteCommandResult(
            command=argv,
            returncode=124,
            stdout=stdout,
            stderr=stderr or f"timed out after {timeout_sec}s",
            duration_sec=duration_sec,
        )
        LOGGER.info(
            "Remote command finished: returncode=%s duration=%.3fs",
            remote_result.returncode,
            remote_result.duration_sec,
        )
        LOGGER.debug("Remote timeout after %ss", timeout_sec)
        if check:
            raise RemoteCommandError(remote_result) from exc
        return remote_result
    duration_sec = round(time.monotonic() - start, 3)
    remote_result = RemoteCommandResult(
        command=argv,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_sec=duration_sec,
    )
    LOGGER.info(
        "Remote command finished: returncode=%s duration=%.3fs",
        remote_result.returncode,
        remote_result.duration_sec,
    )
    if result.stdout:
        LOGGER.debug("Remote stdout: %s", result.stdout)
    if result.stderr:
        LOGGER.debug("Remote stderr: %s", result.stderr)
    if check and result.returncode != 0:
        raise RemoteCommandError(remote_result)
    return remote_result


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _tmux_session_name(job_id: str) -> str:
    _validate_job_id(job_id)
    return f"uef_{job_id}"


def _validate_job_id(job_id: str) -> None:
    if not job_id or any(
        not (character.isascii() and (character.isalnum() or character in {"_", "-"}))
        for character in job_id
    ):
        raise ValueError(f"Unsafe remote job id: {job_id!r}")


_VERIFY_SENTINEL_SCRIPT = r"""
import json
import os
from pathlib import Path

host_name = os.environ["UEF_HOST_NAME"]
work_dir = Path(os.environ["UEF_WORK_DIR"])
sentinel = work_dir / ".uef_node"
if not sentinel.exists():
    raise SystemExit(f"missing sentinel: {sentinel}")
payload = json.loads(sentinel.read_text(encoding="utf-8"))
if payload.get("host") != host_name:
    raise SystemExit(f"sentinel host mismatch: {payload.get('host')} != {host_name}")
print(json.dumps(payload, sort_keys=True))
"""


_STOP_REMOTE_JOB_SCRIPT = r"""
import json
import os
import signal
import subprocess
import time
from pathlib import Path

host_name = os.environ["UEF_HOST_NAME"]
work_dir = Path(os.environ["UEF_WORK_DIR"])
job_id = os.environ["UEF_JOB_ID"]
session = os.environ["UEF_TMUX_SESSION"]
status_path = work_dir / "jobs" / job_id / "status.json"
sentinel = work_dir / ".uef_node"
stop_grace_sec = float(os.environ.get("UEF_STOP_GRACE_SEC", "10"))


def process_identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = stat[stat.rfind(")") + 2 :].split()
    return {
        "pgrp": int(fields[2]),
        "session": int(fields[3]),
        "start_ticks": int(fields[19]),
    }


def group_is_live(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_group_exit(pgid, timeout):
    deadline = time.monotonic() + timeout
    while group_is_live(pgid) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not group_is_live(pgid)


if not sentinel.exists():
    raise SystemExit(f"missing sentinel: {sentinel}")
sentinel_payload = json.loads(sentinel.read_text(encoding="utf-8"))
if sentinel_payload.get("host") != host_name:
    raise SystemExit(
        f"sentinel host mismatch: {sentinel_payload.get('host')} != {host_name}"
    )
if sentinel_payload.get("work_dir") != str(work_dir):
    raise SystemExit(
        "sentinel work_dir mismatch: "
        f"{sentinel_payload.get('work_dir')} != {work_dir}"
    )

status = None
if status_path.exists():
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if status.get("job_id") != job_id:
        raise SystemExit(
            f"job status mismatch: {status.get('job_id')} != {job_id}"
        )

session_exists = (
    subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode
    == 0
)
if status is None and session_exists:
    raise RuntimeError(
        f"refusing to kill live tmux session {session}: job status is missing"
    )

killed_pgid = None
status_is_terminal = status is not None and status.get("status") in {"complete", "failed"}
if status is not None and not status_is_terminal:
    pid = status.get("pid")
    pgid = status.get("pgid")
    start_ticks = status.get("process_start_ticks")
    if not all(type(value) is int for value in (pid, pgid, start_ticks)):
        raise RuntimeError(
            f"refusing to report nonterminal remote job {job_id} stopped: "
            "PID/PGID/start-ticks identity is missing or invalid; "
            f"status={status}; tmux_live={session_exists}"
        )
    if pid <= 1 or pid != pgid:
        raise RuntimeError(
            f"refusing to report nonterminal remote job {job_id} stopped: "
            f"unsafe recorded process identity pid={pid} pgid={pgid}; "
            f"tmux_live={session_exists}"
        )
    identity = process_identity(pid)
    if identity is None:
        raise RuntimeError(
            f"refusing to report nonterminal remote job {job_id} stopped: "
            f"recorded PID {pid} is no longer available, so UE process exit "
            f"cannot be verified; tmux_live={session_exists}"
        )
    if not (
        identity["pgrp"] == pgid
        and identity["session"] == pgid
        and identity["start_ticks"] == start_ticks
    ):
        raise RuntimeError(
            f"refusing to signal recorded PID {pid} for nonterminal remote job {job_id}: "
            "process identity mismatch (possible PID reuse); "
            f"expected pgid/session/start_ticks={pgid}/{pgid}/{start_ticks}, "
            f"actual={identity['pgrp']}/{identity['session']}/{identity['start_ticks']}; "
            f"tmux_live={session_exists}"
        )

    killed_pgid = pgid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if not wait_for_group_exit(pgid, stop_grace_sec):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not wait_for_group_exit(pgid, stop_grace_sec):
            raise RuntimeError(f"UE process group {pgid} did not exit")

if status is not None and session_exists:
    subprocess.run(["tmux", "kill-session", "-t", session], check=True)
    deadline = time.monotonic() + stop_grace_sec
    while time.monotonic() < deadline:
        if (
            subprocess.run(
                ["tmux", "has-session", "-t", session],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            != 0
        ):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"tmux session {session} did not exit")

print(
    json.dumps(
        {
            "job_id": job_id,
            "killed_pgid": killed_pgid,
            "status_found": status is not None,
            "status_was_terminal": status_is_terminal,
            "tmux_session": session,
            "tmux_was_live": session_exists,
        },
        sort_keys=True,
    )
)
"""
