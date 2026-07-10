from __future__ import annotations

from pathlib import Path

import pytest

from uefactory.scenes.locking import SceneLockError, scene_lock


def test_scene_lock_is_non_blocking_per_scene_and_releases_cleanly(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    with scene_lock(data_dir=data_dir, scene_id="forest_scene") as first_path:
        assert first_path == data_dir / "locks/scenes/forest_scene.lock"
        with (
            pytest.raises(SceneLockError, match="another build or render owns"),
            scene_lock(data_dir=data_dir, scene_id="forest_scene"),
        ):
            pytest.fail("the same scene lock must not be re-entrant")

        with scene_lock(data_dir=data_dir, scene_id="church_scene") as other_path:
            assert other_path.name == "church_scene.lock"

    with scene_lock(data_dir=data_dir, scene_id="forest_scene") as reacquired_path:
        assert reacquired_path == first_path


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
