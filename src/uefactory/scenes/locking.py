from __future__ import annotations

import errno
import fcntl
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from uefactory.core.identity import validate_snake_slug


class SceneLockError(RuntimeError):
    """Raised when another host-side operation owns a scene lock."""


@dataclass
class _HeldSceneLock:
    handle: TextIO
    pid: int
    thread_id: int
    depth: int = 1


_HELD_LOCKS: dict[Path, _HeldSceneLock] = {}
_HELD_LOCKS_GUARD = threading.Lock()


def _reset_scene_locks_after_fork() -> None:
    """Discard inherited process-local state without touching the parent's locks."""

    global _HELD_LOCKS_GUARD
    _HELD_LOCKS_GUARD = threading.Lock()
    inherited = tuple(_HELD_LOCKS.values())
    _HELD_LOCKS.clear()
    for held in inherited:
        with suppress(OSError):
            held.handle.close()


os.register_at_fork(after_in_child=_reset_scene_locks_after_fork)


@contextmanager
def scene_lock(*, data_dir: Path, scene_id: str) -> Iterator[Path]:
    """Hold a non-blocking scene lock, re-entering only on the owning thread."""

    canonical_id = validate_snake_slug(scene_id, field="scene_id")
    lock_path = data_dir.expanduser().resolve() / "locks/scenes" / f"{canonical_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    thread_id = threading.get_ident()
    with _HELD_LOCKS_GUARD:
        held = _HELD_LOCKS.get(lock_path)
        if held is not None and held.pid != pid:
            held.handle.close()
            del _HELD_LOCKS[lock_path]
            held = None
        if held is not None:
            if held.thread_id != thread_id:
                raise SceneLockError(
                    f"scene {canonical_id!r} is busy; another build or render owns {lock_path}"
                )
            held.depth += 1
        else:
            handle = lock_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                raise SceneLockError(
                    f"scene {canonical_id!r} is busy; another build or render owns {lock_path}"
                ) from exc
            _HELD_LOCKS[lock_path] = _HeldSceneLock(
                handle=handle,
                pid=pid,
                thread_id=thread_id,
            )
    try:
        yield lock_path
    finally:
        with _HELD_LOCKS_GUARD:
            held = _HELD_LOCKS.get(lock_path)
            if held is None or held.pid != pid or held.thread_id != thread_id:
                raise RuntimeError(f"scene lock ownership was lost: {lock_path}")
            held.depth -= 1
            if held.depth == 0:
                try:
                    fcntl.flock(held.handle.fileno(), fcntl.LOCK_UN)
                finally:
                    try:
                        held.handle.close()
                    finally:
                        del _HELD_LOCKS[lock_path]


__all__ = ["SceneLockError", "scene_lock"]
