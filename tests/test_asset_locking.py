from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

import pytest

import uefactory.core.asset_locking as asset_locking_module
from uefactory.core.asset_locking import AssetLockError, asset_lock


def test_asset_lock_is_non_blocking_per_asset_and_releases_cleanly(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    with asset_lock(data_dir=data_dir, asset_id="stone_arch") as first_path:
        assert first_path == data_dir / "locks/assets/stone_arch.lock"
        with asset_lock(data_dir=data_dir, asset_id="stone_arch") as nested_path:
            assert nested_path == first_path

        with asset_lock(data_dir=data_dir, asset_id="temple_bell") as other_path:
            assert other_path.name == "temple_bell.lock"

    with asset_lock(data_dir=data_dir, asset_id="stone_arch") as reacquired_path:
        assert reacquired_path == first_path


def test_asset_lock_rejects_same_asset_owned_by_another_thread(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_lock() -> None:
        try:
            with asset_lock(data_dir=data_dir, asset_id="stone_arch"):
                acquired.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test did not release the external asset lock")
        except BaseException as exc:  # pragma: no cover - surfaced in the parent thread
            errors.append(exc)
            acquired.set()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert acquired.wait(timeout=5)
    try:
        with (
            pytest.raises(AssetLockError, match="another ingest or render owns"),
            asset_lock(data_dir=data_dir, asset_id="stone_arch"),
        ):
            pytest.fail("a different thread must not re-enter the asset lock")
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []


def test_after_fork_callback_replaces_locked_guard_and_closes_inherited_handles(
    tmp_path: Path,
) -> None:
    original_guard = asset_locking_module._HELD_LOCKS_GUARD
    original_locks = asset_locking_module._HELD_LOCKS
    inherited_guard = threading.Lock()
    inherited_guard.acquire()
    handle = (tmp_path / "inherited.lock").open("a+", encoding="utf-8")
    asset_locking_module._HELD_LOCKS_GUARD = inherited_guard
    asset_locking_module._HELD_LOCKS = {
        tmp_path / "inherited.lock": asset_locking_module._HeldAssetLock(
            handle=handle,
            pid=os.getpid() - 1,
            thread_id=1,
        )
    }
    try:
        asset_locking_module._reset_asset_locks_after_fork()

        assert handle.closed
        assert asset_locking_module._HELD_LOCKS == {}
        assert asset_locking_module._HELD_LOCKS_GUARD is not inherited_guard
        assert asset_locking_module._HELD_LOCKS_GUARD.acquire(blocking=False)
        asset_locking_module._HELD_LOCKS_GUARD.release()
    finally:
        if not handle.closed:
            handle.close()
        asset_locking_module._HELD_LOCKS = original_locks
        asset_locking_module._HELD_LOCKS_GUARD = original_guard


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_does_not_inherit_a_deadlocked_registry_guard(tmp_path: Path) -> None:
    guard = asset_locking_module._HELD_LOCKS_GUARD
    guard.acquire()
    try:
        child_pid = os.fork()
    finally:
        guard.release()
    if child_pid == 0:  # pragma: no cover - assertions are observed via child status
        try:
            with asset_lock(data_dir=tmp_path / "data", asset_id="fork_asset"):
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
        pytest.fail("forked child deadlocked on the inherited asset-lock registry guard")
    assert os.waitstatus_to_exitcode(child_status) == 0


@pytest.mark.parametrize("asset_id", ["../escape", "UpperCase", "double__underscore"])
def test_asset_lock_rejects_noncanonical_ids_without_creating_a_path(
    tmp_path: Path,
    asset_id: str,
) -> None:
    with (
        pytest.raises(ValueError, match="lowercase snake_case"),
        asset_lock(data_dir=tmp_path / "data", asset_id=asset_id),
    ):
        pytest.fail("invalid asset ids cannot acquire locks")

    assert not (tmp_path / "data").exists()
