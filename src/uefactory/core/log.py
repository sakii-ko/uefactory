from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

from uefactory import __version__
from uefactory.core.config import Settings
from uefactory.core.paths import utc_timestamp

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class UtcFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        del datefmt
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created))


def configure_logging(
    *,
    settings: Settings,
    argv: Sequence[str],
    command_name: str,
    verbose: bool = False,
) -> Path:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.log_dir / f"{utc_timestamp()}_{_sanitize_command(command_name)}.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()

    formatter = UtcFormatter(LOG_FORMAT)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    logger = logging.getLogger("uefactory")
    logger.debug("argv=%s", list(argv))
    logger.debug("cwd=%s", Path.cwd())
    logger.debug("git_commit=%s", _git_commit(settings.project_root))
    logger.debug("uefactory_version=%s", __version__)
    logger.debug("log_path=%s", log_path)
    return log_path


def _sanitize_command(command_name: str) -> str:
    value = command_name.strip().replace(" ", "_").replace("/", "_")
    return value or "uef"


def _git_commit(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_root,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


def log_path_from_context() -> Path | None:
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            return Path(handler.baseFilename)
    return None


def log_unhandled_exception(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
) -> None:
    logging.getLogger("uefactory").critical("Unhandled exception", exc_info=(exc_type, exc, tb))
    sys.__excepthook__(exc_type, exc, tb)
