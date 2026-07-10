from __future__ import annotations

import signal
from pathlib import Path
from typing import Any

import pytest

from uefactory.render.ue_runner import run_ue, summarize_ue_log


def test_run_ue_kills_process_group_when_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class InterruptedProcess:
        pid = 4242

        def __init__(self) -> None:
            self.wait_calls = 0
            self.terminated = False

        def wait(self, *, timeout: int) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise KeyboardInterrupt
            assert timeout == 10
            self.terminated = True
            return -signal.SIGTERM

        def poll(self) -> int | None:
            return -signal.SIGTERM if self.terminated else None

    process = InterruptedProcess()
    popen_kwargs: dict[str, Any] = {}
    kill_calls: list[tuple[int, signal.Signals]] = []

    def fake_popen(*args: Any, **kwargs: Any) -> InterruptedProcess:
        popen_kwargs.update(kwargs)
        return process

    monkeypatch.setattr("uefactory.render.ue_runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "uefactory.render.ue_runner.os.killpg",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    with pytest.raises(KeyboardInterrupt):
        run_ue(
            ["UnrealEditor", "project.uproject"],
            cwd=tmp_path,
            log_path=tmp_path / "ue.log",
            timeout_sec=60,
        )

    assert popen_kwargs["start_new_session"] is True
    assert kill_calls == [(process.pid, signal.SIGTERM)]
    assert process.wait_calls == 2


def test_summarize_ue_log_counts_warnings_and_errors(tmp_path: Path) -> None:
    log_path = tmp_path / "ue.log"
    log_path.write_text(
        "\n".join(
            [
                "LogInit: Display: ok",
                "LogFoo: Warning: first warning",
                "LogDirectoryWatcher: Warning: Failed to begin reading directory changes",
                "LogCore: Warning: UTS: Unreal Trace Server process returned an error (0x3)",
                "LogStreaming: Warning: Failed to read file '../../../Engine/Icon128.png' error.",
                "LogUsd: Warning: Failed to update LibraryPath for USD plugInfo.json file 'x'",
                (
                    "LogUnixPlatformFile: Warning: open('/x/Engine/Content/"
                    "WritePermissions.abc.temp') failed: Permission denied"
                ),
                (
                    "LogStreaming: Warning: LoadPackage: SkipPackage: /Engine/PythonTypes "
                    "(0xB446D7D5D25A361D) - The package to load does not exist on disk"
                ),
                (
                    "LogCore: Warning: Unable to statfs('/repo/out/mrq_spike/"
                    "run/frame_0000.png'): errno=2 (No such file or directory)"
                ),
                (
                    "LogCore: Warning: Unable to statfs('/repo/out/renders/"
                    "run/builtin_cube/beauty_lit/frame_0000.png'): "
                    "errno=2 (No such file or directory)"
                ),
                (
                    "LogCore: Warning: Unable to statfs('/remote/jobs/render/out/"
                    "builtin_cube/_mrq/beauty_lit/FinalImage.frame_0000.png'): "
                    "errno=2 (No such file or directory)"
                ),
                "LogPython: Error: traceback",
                (
                    "LogUsd: Error: TF_DIAGNOSTIC_CODING_ERROR_TYPE: Failed to load plugin "
                    "'usdAbc': missing"
                ),
                (
                    "LogFeaturePack: Error: Error in Feature pack manifest.json. "
                    "Cannot find screenshot Missing.png."
                ),
                "LogBar: Warning: second warning",
            ]
        ),
        encoding="utf-8",
    )

    summary = summarize_ue_log(log_path)

    assert summary.warning_count == 2
    assert summary.warning_noise_count == 9
    assert summary.warning_noise == {
        "directory_watcher": 1,
        "unreal_trace_server_startup": 1,
        "missing_editor_icon": 1,
        "usd_plugin_metadata_write_permission": 1,
        "engine_content_write_permission_probe": 1,
        "python_types_runtime_class_probe": 1,
        "mrq_output_path_probe": 1,
        "mrq_render_output_path_probe": 1,
        "mrq_remote_output_path_probe": 1,
    }
    assert summary.error_count == 1
    assert summary.error_noise_count == 2
    assert summary.error_noise == {
        "missing_optional_usd_plugin": 1,
        "missing_feature_pack_screenshot": 1,
    }
    assert summary.warnings == [
        "LogFoo: Warning: first warning",
        "LogBar: Warning: second warning",
    ]
    assert summary.errors == ["LogPython: Error: traceback"]
