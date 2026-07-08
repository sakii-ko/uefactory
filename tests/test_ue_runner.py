from __future__ import annotations

from pathlib import Path

from uefactory.render.ue_runner import summarize_ue_log


def test_summarize_ue_log_counts_warnings_and_errors(tmp_path: Path) -> None:
    log_path = tmp_path / "ue.log"
    log_path.write_text(
        "\n".join(
            [
                "LogInit: Display: ok",
                "LogFoo: Warning: first warning",
                "LogDirectoryWatcher: Warning: Failed to begin reading directory changes",
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
    assert summary.warning_noise_count == 6
    assert summary.warning_noise == {
        "directory_watcher": 1,
        "missing_editor_icon": 1,
        "usd_plugin_metadata_write_permission": 1,
        "engine_content_write_permission_probe": 1,
        "python_types_runtime_class_probe": 1,
        "mrq_output_path_probe": 1,
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
