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
                "LogPython: Error: traceback",
                "LogBar: Warning: second warning",
            ]
        ),
        encoding="utf-8",
    )

    summary = summarize_ue_log(log_path)

    assert summary.warning_count == 2
    assert summary.error_count == 1
    assert summary.warnings == [
        "LogFoo: Warning: first warning",
        "LogBar: Warning: second warning",
    ]
    assert summary.errors == ["LogPython: Error: traceback"]
