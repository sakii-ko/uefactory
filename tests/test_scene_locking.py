from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

import pytest

import uefactory.scenes.locking as scene_locking_module
from uefactory.scenes.locking import SceneLockError, scene_lock


def test_scene_lock_is_non_blocking_per_scene_and_releases_cleanly(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    with scene_lock(data_dir=data_dir, scene_id="forest_scene") as first_path:
        assert first_path == data_dir / "locks/scenes/forest_scene.lock"
        with scene_lock(data_dir=data_dir, scene_id="forest_scene") as nested_path:
            assert nested_path == first_path

        with scene_lock(data_dir=data_dir, scene_id="church_scene") as other_path:
            assert other_path.name == "church_scene.lock"

    with scene_lock(data_dir=data_dir, scene_id="forest_scene") as reacquired_path:
        assert reacquired_path == first_path


def test_scene_lock_rejects_same_scene_owned_by_another_thread(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_lock() -> None:
        try:
            with scene_lock(data_dir=data_dir, scene_id="forest_scene"):
                acquired.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test did not release the external scene lock")
        except BaseException as exc:  # pragma: no cover - surfaced in the parent thread
            errors.append(exc)
            acquired.set()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert acquired.wait(timeout=5)
    try:
        with (
            pytest.raises(SceneLockError, match="another build or render owns"),
            scene_lock(data_dir=data_dir, scene_id="forest_scene"),
        ):
            pytest.fail("a different thread must not re-enter the scene lock")
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []


def test_after_fork_callback_replaces_scene_guard_and_closes_inherited_handles(
    tmp_path: Path,
) -> None:
    original_guard = scene_locking_module._HELD_LOCKS_GUARD
    original_locks = scene_locking_module._HELD_LOCKS
    inherited_guard = threading.Lock()
    inherited_guard.acquire()
    handle = (tmp_path / "inherited.lock").open("a+", encoding="utf-8")
    scene_locking_module._HELD_LOCKS_GUARD = inherited_guard
    scene_locking_module._HELD_LOCKS = {
        tmp_path / "inherited.lock": scene_locking_module._HeldSceneLock(
            handle=handle,
            pid=os.getpid() - 1,
            thread_id=1,
        )
    }
    try:
        scene_locking_module._reset_scene_locks_after_fork()

        assert handle.closed
        assert scene_locking_module._HELD_LOCKS == {}
        assert scene_locking_module._HELD_LOCKS_GUARD is not inherited_guard
        assert scene_locking_module._HELD_LOCKS_GUARD.acquire(blocking=False)
        scene_locking_module._HELD_LOCKS_GUARD.release()
    finally:
        if not handle.closed:
            handle.close()
        scene_locking_module._HELD_LOCKS = original_locks
        scene_locking_module._HELD_LOCKS_GUARD = original_guard


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_does_not_inherit_a_deadlocked_scene_guard(tmp_path: Path) -> None:
    guard = scene_locking_module._HELD_LOCKS_GUARD
    guard.acquire()
    try:
        child_pid = os.fork()
    finally:
        guard.release()
    if child_pid == 0:  # pragma: no cover - assertions are observed via child status
        try:
            with scene_lock(data_dir=tmp_path / "data", scene_id="fork_scene"):
                os._exit(0)
        except BaseException:
            os._exit(1)

    deadline = time.monotonic() + 5.0
    child_status: int | None = None
    while time.monotonic() < deadline:
        completed_pid, status = os.waitpid(child_pid, os.WNOHANG)
        if completed_pid == child_pid:
            child_status = status
            break
        time.sleep(0.01)
    if child_status is None:
        os.kill(child_pid, signal.SIGKILL)
        os.waitpid(child_pid, 0)
        pytest.fail("forked child deadlocked on the inherited scene-lock registry guard")
    assert os.waitstatus_to_exitcode(child_status) == 0


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_cannot_take_parent_scene_lock(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    with scene_lock(data_dir=data_dir, scene_id="fork_scene"):
        child_pid = os.fork()
        if child_pid == 0:  # pragma: no cover - assertions are observed via child status
            try:
                with scene_lock(data_dir=data_dir, scene_id="fork_scene"):
                    os._exit(1)
            except SceneLockError:
                os._exit(0)
            except BaseException:
                os._exit(2)
        deadline = time.monotonic() + 5.0
        child_status: int | None = None
        while time.monotonic() < deadline:
            completed_pid, status = os.waitpid(child_pid, os.WNOHANG)
            if completed_pid == child_pid:
                child_status = status
                break
            time.sleep(0.01)
        if child_status is None:
            os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)
            pytest.fail("forked child blocked instead of observing the parent's scene lock")
        assert os.waitstatus_to_exitcode(child_status) == 0

    with scene_lock(data_dir=data_dir, scene_id="fork_scene"):
        pass


@pytest.mark.parametrize("scene_id", ["../escape", "UpperCase", "double__underscore"])
def test_scene_lock_rejects_noncanonical_ids_without_creating_a_path(
    tmp_path: Path,
    scene_id: str,
) -> None:
    with (
        pytest.raises(ValueError, match="lowercase snake_case"),
        scene_lock(data_dir=tmp_path / "data", scene_id=scene_id),
    ):
        pytest.fail("invalid scene ids cannot acquire locks")

    assert not (tmp_path / "data").exists()
