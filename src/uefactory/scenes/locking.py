from __future__ import annotations

import errno
import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from uefactory.core.identity import validate_snake_slug


class SceneLockError(RuntimeError):
    """Raised when another host-side operation owns a scene lock."""


@contextmanager
def scene_lock(*, data_dir: Path, scene_id: str) -> Iterator[Path]:
    """Hold the non-blocking, cross-process lock for one scene operation."""

    canonical_id = validate_snake_slug(scene_id, field="scene_id")
    lock_path = data_dir.expanduser().resolve() / "locks/scenes" / f"{canonical_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle: TextIO = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise SceneLockError(
                f"scene {canonical_id!r} is busy; another build or render owns {lock_path}"
            ) from exc
        try:
            yield lock_path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


__all__ = ["SceneLockError", "scene_lock"]
