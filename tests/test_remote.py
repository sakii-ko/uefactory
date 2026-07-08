from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from uefactory.core.config import HostConfig, Settings
from uefactory.core.remote import (
    SSH_CONTROL_OPTIONS,
    RemoteCommandError,
    RemoteCommandResult,
    RemoteHost,
)


def _result(command: list[str], stdout: str = "{}") -> RemoteCommandResult:
    return RemoteCommandResult(
        command=command,
        returncode=0,
        stdout=stdout,
        stderr="",
        duration_sec=0.01,
    )


def test_remote_run_uses_controlmaster_options(monkeypatch: Any) -> None:
    calls: list[list[str]] = []

    def fake_run_local(
        argv: list[str],
        *,
        timeout_sec: int,
        check: bool,
    ) -> RemoteCommandResult:
        calls.append(argv)
        return _result(argv)

    monkeypatch.setattr("uefactory.core.remote._run_local", fake_run_local)
    remote = RemoteHost(
        HostConfig(
            name="l40s",
            ssh_alias="l40s",
            work_dir=Path("/remote/work"),
            engine_dir=Path("/remote/engine"),
        )
    )

    remote.run("echo ok", timeout_sec=10)

    assert calls == [["ssh", *SSH_CONTROL_OPTIONS, "l40s", "echo ok"]]


def test_rsync_delete_requires_sentinel_and_workdir(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    sentinel = {
        "host": "l40s",
        "work_dir": "/remote/work",
        "node_id": "abc",
    }

    def fake_run_local(
        argv: list[str],
        *,
        timeout_sec: int,
        check: bool,
    ) -> RemoteCommandResult:
        calls.append(argv)
        stdout = json.dumps(sentinel) if argv[0] == "ssh" else "{}"
        return _result(argv, stdout=stdout)

    monkeypatch.setattr("uefactory.core.remote._run_local", fake_run_local)
    remote = RemoteHost(
        HostConfig(
            name="l40s",
            ssh_alias="l40s",
            work_dir=Path("/remote/work"),
            engine_dir=Path("/remote/engine"),
        )
    )

    with pytest.raises(ValueError, match="outside sentinel work_dir"):
        remote.rsync_push([tmp_path], "/tmp/other", delete=True)

    remote.rsync_push([tmp_path], "/remote/work/job", delete=True)

    assert calls[0][0] == "ssh"
    assert calls[1][0] == "rsync"
    assert "--delete" in calls[1]
    assert "-z" in calls[1]
    assert "--partial" in calls[1]
    assert any("ControlMaster=auto" in part for part in calls[1])


def test_rsync_pull_delete_requires_local_safe_destination(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    project_root = tmp_path / "project"
    data_dir = project_root / "data"
    out_dir = project_root / "out"
    project_root.mkdir()
    data_dir.mkdir()
    out_dir.mkdir()

    def fake_run_local(
        argv: list[str],
        *,
        timeout_sec: int,
        check: bool,
    ) -> RemoteCommandResult:
        calls.append(argv)
        return _result(argv)

    monkeypatch.setattr("uefactory.core.remote._run_local", fake_run_local)
    remote = RemoteHost(
        HostConfig(
            name="l40s",
            ssh_alias="l40s",
            work_dir=Path("/remote/work"),
            engine_dir=Path("/remote/engine"),
        ),
        local_delete_roots=[data_dir, out_dir],
    )

    with pytest.raises(ValueError, match="outside local roots"):
        remote.rsync_pull(["/remote/work/job/"], project_root, delete=True)

    remote.rsync_pull(["/remote/work/job/"], out_dir / "job", delete=True)

    assert len(calls) == 1
    assert calls[0][0] == "rsync"
    assert "--delete" in calls[0]
    assert str(out_dir / "job") == calls[0][-1]


def test_remote_from_settings_configures_local_delete_roots(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        hosts={
            "l40s": HostConfig(
                name="l40s",
                ssh_alias="l40s",
                work_dir=Path("/remote/work"),
                engine_dir=Path("/remote/engine"),
            )
        },
    )

    def fake_run_local(
        argv: list[str],
        *,
        timeout_sec: int,
        check: bool,
    ) -> RemoteCommandResult:
        calls.append(argv)
        return _result(argv)

    monkeypatch.setattr("uefactory.core.remote._run_local", fake_run_local)
    remote = RemoteHost.from_settings(settings, "l40s")

    with pytest.raises(ValueError, match="outside local roots"):
        remote.rsync_pull(["/remote/work/job/"], tmp_path, delete=True)

    remote.rsync_pull(["/remote/work/job/"], tmp_path / "data" / "job", delete=True)

    assert len(calls) == 1
    assert calls[0][0] == "rsync"


def test_tmux_status_requires_exact_live_marker(monkeypatch: Any) -> None:
    def fake_run_local(
        argv: list[str],
        *,
        timeout_sec: int,
        check: bool,
    ) -> RemoteCommandResult:
        stdout = '{"status": "complete", "note": "__UEF_TMUX_LIVE__true"}\n__UEF_TMUX_LIVE__false\n'
        return _result(argv, stdout=stdout)

    monkeypatch.setattr("uefactory.core.remote._run_local", fake_run_local)
    remote = RemoteHost(
        HostConfig(
            name="l40s",
            ssh_alias="l40s",
            work_dir=Path("/remote/work"),
            engine_dir=Path("/remote/engine"),
        )
    )

    status = remote.tmux_status("job-1")

    assert status["status"] == "complete"
    assert status["tmux_live"] is False


def test_remote_run_wraps_timeout(monkeypatch: Any) -> None:
    def fake_subprocess_run(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b"partial")

    monkeypatch.setattr("subprocess.run", fake_subprocess_run)
    remote = RemoteHost(
        HostConfig(
            name="l40s",
            ssh_alias="l40s",
            work_dir=Path("/remote/work"),
            engine_dir=Path("/remote/engine"),
        )
    )

    with pytest.raises(RemoteCommandError) as exc_info:
        remote.run("sleep 10", timeout_sec=1)

    assert exc_info.value.result.returncode == 124
    assert exc_info.value.result.stdout == "partial"
    assert "timed out after 1s" in exc_info.value.result.stderr
