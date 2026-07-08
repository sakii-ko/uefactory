from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)

KNOWN_WARNING_NOISE_RULES: dict[str, tuple[str, ...]] = {
    "directory_watcher": ("LogDirectoryWatcher: Warning:",),
    "missing_editor_icon": ("LogStreaming: Warning: Failed to read file", ".png"),
    "usd_plugin_metadata_write_permission": (
        "Warning:",
        "USD",
        "plugInfo.json",
    ),
    "engine_content_write_permission_probe": (
        "Warning:",
        "/Engine/",
        "WritePermissions.",
        "Permission denied",
    ),
    "python_types_runtime_class_probe": (
        "LogStreaming: Warning: LoadPackage: SkipPackage: /Engine/PythonTypes",
    ),
    "mrq_output_path_probe": (
        "LogCore: Warning: Unable to statfs(",
        "out/mrq_spike/",
        "errno=2 (No such file or directory)",
    ),
}

KNOWN_ERROR_NOISE_RULES: dict[str, tuple[str, ...]] = {
    "missing_optional_usd_plugin": (
        "LogUsd: Error: TF_DIAGNOSTIC_CODING_ERROR_TYPE: Failed to load plugin",
    ),
    "missing_feature_pack_screenshot": (
        "LogFeaturePack: Error: Error in Feature pack",
        "Cannot find screenshot",
    ),
}


@dataclass(frozen=True)
class LogSummary:
    warnings: list[str]
    errors: list[str]
    warning_count: int
    error_count: int
    warning_noise_count: int = 0
    warning_noise: dict[str, int] | None = None
    error_noise_count: int = 0
    error_noise: dict[str, int] | None = None


@dataclass(frozen=True)
class UERunResult:
    command: list[str]
    returncode: int
    duration_sec: float
    log_path: Path
    summary: LogSummary


class UERunnerError(RuntimeError):
    def __init__(self, result: UERunResult) -> None:
        self.result = result
        message = (
            f"Unreal Engine command failed with exit code {result.returncode}; "
            f"log={result.log_path}; errors={result.summary.error_count}; "
            f"warnings={result.summary.warning_count}"
        )
        super().__init__(message)


def run_ue(
    command: Sequence[str | Path],
    *,
    cwd: Path,
    log_path: Path,
    timeout_sec: int,
    env: Mapping[str, str] | None = None,
) -> UERunResult:
    argv = [str(part) for part in command]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Starting UE process: %s", " ".join(argv))
    LOGGER.debug("UE cwd=%s log=%s timeout=%s", cwd, log_path, timeout_sec)
    start = time.monotonic()
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=merged_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            LOGGER.error(
                "UE process timed out after %s seconds; killing process group",
                timeout_sec,
            )
            _kill_process_group(process)
            returncode = process.wait(timeout=30)
    duration_sec = time.monotonic() - start
    summary = summarize_ue_log(log_path)
    result = UERunResult(
        command=argv,
        returncode=returncode,
        duration_sec=round(duration_sec, 3),
        log_path=log_path,
        summary=summary,
    )
    _log_summary(result)
    if returncode != 0:
        raise UERunnerError(result)
    return result


def summarize_ue_log(log_path: Path, *, limit: int = 20) -> LogSummary:
    warnings: list[str] = []
    errors: list[str] = []
    warning_count = 0
    error_count = 0
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        LOGGER.warning("Could not read UE log %s: %s", log_path, exc)
        return LogSummary(warnings=[], errors=[], warning_count=0, error_count=0)
    warning_noise: dict[str, int] = {}
    error_noise: dict[str, int] = {}
    for line in lines:
        if "Warning:" in line:
            noise_reason = _warning_noise_reason(line)
            if noise_reason is not None:
                warning_noise[noise_reason] = warning_noise.get(noise_reason, 0) + 1
                continue
            warning_count += 1
            if len(warnings) < limit:
                warnings.append(line)
        if "Error:" in line:
            noise_reason = _error_noise_reason(line)
            if noise_reason is not None:
                error_noise[noise_reason] = error_noise.get(noise_reason, 0) + 1
                continue
            error_count += 1
            if len(errors) < limit:
                errors.append(line)
    return LogSummary(
        warnings=warnings,
        errors=errors,
        warning_count=warning_count,
        error_count=error_count,
        warning_noise_count=sum(warning_noise.values()),
        warning_noise=warning_noise,
        error_noise_count=sum(error_noise.values()),
        error_noise=error_noise,
    )


def _warning_noise_reason(line: str) -> str | None:
    for reason, markers in KNOWN_WARNING_NOISE_RULES.items():
        if all(marker in line for marker in markers):
            return reason
    return None


def _error_noise_reason(line: str) -> str | None:
    for reason, markers in KNOWN_ERROR_NOISE_RULES.items():
        if all(marker in line for marker in markers):
            return reason
    return None


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)


def _log_summary(result: UERunResult) -> None:
    LOGGER.info(
        "UE process finished: returncode=%s duration=%.3fs log=%s warnings=%s "
        "filtered_warning_noise=%s errors=%s filtered_error_noise=%s",
        result.returncode,
        result.duration_sec,
        result.log_path,
        result.summary.warning_count,
        result.summary.warning_noise_count,
        result.summary.error_count,
        result.summary.error_noise_count,
    )
    if result.summary.warning_noise_count:
        LOGGER.info("UE warning noise filtered: %s", result.summary.warning_noise)
    if result.summary.error_noise_count:
        LOGGER.info("UE error noise filtered: %s", result.summary.error_noise)
    for line in result.summary.errors:
        LOGGER.error("UE error summary: %s", line)
    for line in result.summary.warnings:
        LOGGER.warning("UE warning summary: %s", line)
