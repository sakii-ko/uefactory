from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from uefactory.core.config import HostConfig, Settings
from uefactory.core.remote import (
    _STOP_REMOTE_JOB_SCRIPT,
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


def test_tmux_stop_targets_only_canonical_session(monkeypatch: Any) -> None:
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

    with pytest.raises(ValueError, match="Unsafe remote job id"):
        remote.tmux_stop("job;kill-server")
    assert calls == []

    remote.tmux_stop("job-kill-server")

    assert len(calls) == 1
    assert calls[0][:-1] == ["ssh", *SSH_CONTROL_OPTIONS, "l40s"]
    command = calls[0][-1]
    assert "UEF_JOB_ID=job-kill-server" in command
    assert "UEF_TMUX_SESSION=uef_job-kill-server" in command
    assert 'status.get("job_id") != job_id' in command
    assert 'identity["session"] == pgid' in command
    assert 'identity["start_ticks"] == start_ticks' in command
    assert "os.killpg(pgid, signal.SIGTERM)" in command
    assert '["tmux", "kill-session", "-t", session]' in command


def test_stop_remote_job_kills_only_recorded_independent_group_then_tmux(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "work"
    job_id = "render_l40s_test"
    status_path = work_dir / "jobs" / job_id / "status.json"
    status_path.parent.mkdir(parents=True)
    work_dir.joinpath(".uef_node").write_text(
        json.dumps({"host": "l40s", "work_dir": str(work_dir)}),
        encoding="utf-8",
    )
    supervisor_script = "\n".join(
        [
            "import json, subprocess, sys",
            "from pathlib import Path",
            "status_path = Path(sys.argv[1])",
            "job_id = sys.argv[2]",
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(60)'], start_new_session=True)",
            "stat = Path(f'/proc/{child.pid}/stat').read_text()",
            "fields = stat[stat.rfind(')') + 2:].split()",
            "status_path.write_text(json.dumps({"
            "'job_id': job_id, 'status': 'running', 'pid': child.pid, "
            "'pgid': child.pid, 'process_start_ticks': int(fields[19])}))",
            "print(child.pid, flush=True)",
            "raise SystemExit(child.wait())",
        ]
    )
    supervisor = subprocess.Popen(
        [sys.executable, "-c", supervisor_script, str(status_path), job_id],
        text=True,
        stdout=subprocess.PIPE,
    )
    assert supervisor.stdout is not None
    target_pid = int(supervisor.stdout.readline().strip())
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux_log = tmp_path / "tmux.log"
    tmux_state = tmp_path / "tmux.live"
    tmux_state.touch()
    tmux = bin_dir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$UEF_TEST_TMUX_LOG"\n'
        'if [ "$1" = has-session ]; then test -e "$UEF_TEST_TMUX_STATE"; exit $?; fi\n'
        'if [ "$1" = kill-session ]; then rm -f "$UEF_TEST_TMUX_STATE"; exit 0; fi\n'
        "exit 2\n",
        encoding="utf-8",
    )
    tmux.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "UEF_HOST_NAME": "l40s",
            "UEF_WORK_DIR": str(work_dir),
            "UEF_JOB_ID": job_id,
            "UEF_TMUX_SESSION": f"uef_{job_id}",
            "UEF_STOP_GRACE_SEC": "0.1",
            "UEF_TEST_TMUX_LOG": str(tmux_log),
            "UEF_TEST_TMUX_STATE": str(tmux_state),
        }
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", _STOP_REMOTE_JOB_SCRIPT],
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["killed_pgid"] == target_pid
        assert supervisor.wait(timeout=5) != 0
        assert unrelated.poll() is None
        assert not tmux_state.exists()
        assert "kill-session -t uef_render_l40s_test" in tmux_log.read_text(encoding="utf-8")
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=5)
        if unrelated.poll() is None:
            unrelated.terminate()
            unrelated.wait(timeout=5)


def test_stop_remote_job_refuses_live_tmux_without_matching_status(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    work_dir.joinpath(".uef_node").write_text(
        json.dumps({"host": "l40s", "work_dir": str(work_dir)}),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux_log = tmp_path / "tmux.log"
    tmux = bin_dir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$UEF_TEST_TMUX_LOG"\n'
        'if [ "$1" = has-session ]; then exit 0; fi\n'
        "exit 97\n",
        encoding="utf-8",
    )
    tmux.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "UEF_HOST_NAME": "l40s",
            "UEF_WORK_DIR": str(work_dir),
            "UEF_JOB_ID": "render_l40s_missing",
            "UEF_TMUX_SESSION": "uef_render_l40s_missing",
            "UEF_STOP_GRACE_SEC": "0.1",
            "UEF_TEST_TMUX_LOG": str(tmux_log),
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", _STOP_REMOTE_JOB_SCRIPT],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert "job status is missing" in result.stderr
    assert "kill-session" not in tmux_log.read_text(encoding="utf-8")


def test_stop_remote_job_fails_closed_for_dead_tmux_with_nonterminal_status_without_pid(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "work"
    job_id = "render_l40s_orphan_gap"
    status_path = work_dir / "jobs" / job_id / "status.json"
    status_path.parent.mkdir(parents=True)
    work_dir.joinpath(".uef_node").write_text(
        json.dumps({"host": "l40s", "work_dir": str(work_dir)}),
        encoding="utf-8",
    )
    status_path.write_text(
        json.dumps({"job_id": job_id, "status": "running"}),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux_log = tmp_path / "tmux.log"
    tmux = bin_dir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$UEF_TEST_TMUX_LOG"\n'
        'if [ "$1" = has-session ]; then exit 1; fi\n'
        "exit 97\n",
        encoding="utf-8",
    )
    tmux.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "UEF_HOST_NAME": "l40s",
            "UEF_WORK_DIR": str(work_dir),
            "UEF_JOB_ID": job_id,
            "UEF_TMUX_SESSION": f"uef_{job_id}",
            "UEF_STOP_GRACE_SEC": "0.1",
            "UEF_TEST_TMUX_LOG": str(tmux_log),
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", _STOP_REMOTE_JOB_SCRIPT],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert "PID/PGID/start-ticks identity is missing or invalid" in result.stderr
    assert "tmux_live=False" in result.stderr
    assert "kill-session" not in tmux_log.read_text(encoding="utf-8")


def test_stop_remote_job_refuses_pid_reuse_without_signaling_process(
    tmp_path: Path,
) -> None:
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        work_dir = tmp_path / "work"
        job_id = "render_l40s_reused_pid"
        status_path = work_dir / "jobs" / job_id / "status.json"
        status_path.parent.mkdir(parents=True)
        work_dir.joinpath(".uef_node").write_text(
            json.dumps({"host": "l40s", "work_dir": str(work_dir)}),
            encoding="utf-8",
        )
        stat = Path(f"/proc/{unrelated.pid}/stat").read_text(encoding="utf-8")
        fields = stat[stat.rfind(")") + 2 :].split()
        actual_start_ticks = int(fields[19])
        status_path.write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "status": "running",
                    "pid": unrelated.pid,
                    "pgid": unrelated.pid,
                    "process_start_ticks": actual_start_ticks - 1,
                }
            ),
            encoding="utf-8",
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        tmux_log = tmp_path / "tmux.log"
        tmux = bin_dir / "tmux"
        tmux.write_text(
            "#!/bin/sh\n"
            'echo "$@" >> "$UEF_TEST_TMUX_LOG"\n'
            'if [ "$1" = has-session ]; then exit 1; fi\n'
            "exit 97\n",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "UEF_HOST_NAME": "l40s",
                "UEF_WORK_DIR": str(work_dir),
                "UEF_JOB_ID": job_id,
                "UEF_TMUX_SESSION": f"uef_{job_id}",
                "UEF_STOP_GRACE_SEC": "0.1",
                "UEF_TEST_TMUX_LOG": str(tmux_log),
            }
        )

        result = subprocess.run(
            [sys.executable, "-c", _STOP_REMOTE_JOB_SCRIPT],
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )

        assert result.returncode != 0
        assert "process identity mismatch (possible PID reuse)" in result.stderr
        assert unrelated.poll() is None
        assert "kill-session" not in tmux_log.read_text(encoding="utf-8")
    finally:
        if unrelated.poll() is None:
            unrelated.terminate()
            unrelated.wait(timeout=5)


def test_stop_remote_job_accepts_terminal_status_without_pid(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    job_id = "render_l40s_complete"
    status_path = work_dir / "jobs" / job_id / "status.json"
    status_path.parent.mkdir(parents=True)
    work_dir.joinpath(".uef_node").write_text(
        json.dumps({"host": "l40s", "work_dir": str(work_dir)}),
        encoding="utf-8",
    )
    status_path.write_text(
        json.dumps({"job_id": job_id, "status": "complete"}),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text(
        '#!/bin/sh\nif [ "$1" = has-session ]; then exit 1; fi\nexit 97\n',
        encoding="utf-8",
    )
    tmux.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "UEF_HOST_NAME": "l40s",
            "UEF_WORK_DIR": str(work_dir),
            "UEF_JOB_ID": job_id,
            "UEF_TMUX_SESSION": f"uef_{job_id}",
            "UEF_STOP_GRACE_SEC": "0.1",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", _STOP_REMOTE_JOB_SCRIPT],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["killed_pgid"] is None
    assert payload["status_was_terminal"] is True


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
