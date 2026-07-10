from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = PROJECT_ROOT / "ue/UEFBase/Content/Python/uef_render_job.py"
RUNTIME_SCRIPT = PROJECT_ROOT / "ue/UEFBase/Content/Python/uef_render_job_runtime.py"


def _load_render_script(monkeypatch: pytest.MonkeyPatch) -> Any:
    unreal = ModuleType("unreal")
    unreal_api = cast(Any, unreal)
    unreal_api.log = lambda message: None
    monkeypatch.setitem(sys.modules, "unreal", unreal)
    spec = importlib.util.spec_from_file_location("test_uef_render_job_scene", SETUP_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _function_calls(path: Path, function_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    return {
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_scene_render_level_uses_template_clone_without_asset_duplication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_render_script(monkeypatch)
    target_map = "/Game/UEF/RenderJobs/run_01/UEF_RenderWorld_run_01"
    source_map = "/Game/UEF/Scenes/forest_scene/L_forest_scene"

    class FakeAssetLibrary:
        made_directories: list[str] = []

        @classmethod
        def does_asset_exist(cls, path: str) -> bool:
            return path == source_map

        @classmethod
        def delete_asset(cls, path: str) -> bool:
            raise AssertionError(f"unexpected render-map delete: {path}")

        @classmethod
        def make_directory(cls, path: str) -> None:
            cls.made_directories.append(path)

        @classmethod
        def duplicate_asset(cls, *args: object) -> None:
            raise AssertionError(f"scene maps must not use duplicate_asset: {args}")

    class FakeLevelEditor:
        template_calls: list[tuple[str, str]] = []
        loaded: list[str] = []

        def new_level_from_template(self, target: str, template: str) -> bool:
            self.template_calls.append((target, template))
            return True

        def new_level(self, *args: object) -> bool:
            raise AssertionError(f"scene render must not create an empty level: {args}")

        def load_level(self, path: str) -> bool:
            self.loaded.append(path)
            return True

    level_editor = FakeLevelEditor()
    script.unreal.EditorAssetLibrary = FakeAssetLibrary
    script.unreal.LevelEditorSubsystem = object()
    script.unreal.get_editor_subsystem = lambda subsystem: level_editor
    job = {
        "map_path": target_map,
        "asset": {
            "kind": "scene",
            "scene_map_path": source_map,
        },
    }

    result = script._create_empty_level(job)

    assert result == target_map
    assert level_editor.template_calls == [(target_map, source_map)]
    assert level_editor.loaded == [target_map]
    assert FakeAssetLibrary.made_directories == ["/Game/UEF/RenderJobs/run_01"]
    calls = _function_calls(SETUP_SCRIPT, "_create_empty_level")
    assert "new_level_from_template" in calls
    assert "duplicate_asset" not in calls


def test_scene_sequence_does_not_spawn_model_or_automatic_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_render_script(monkeypatch)

    class FakeSequence:
        def __init__(self) -> None:
            self.display_rate: object | None = None
            self.playback: tuple[int | None, int | None] = (None, None)

        def set_display_rate(self, value: object) -> None:
            self.display_rate = value

        def set_playback_start(self, value: int) -> None:
            self.playback = (value, self.playback[1])

        def set_playback_end(self, value: int) -> None:
            self.playback = (self.playback[0], value)

    sequence = FakeSequence()

    class FakeAssetTools:
        def create_asset(self, *args: object) -> FakeSequence:
            assert args[:2] == ("SceneSequence", "/Game/UEF/RenderJobs/run_01")
            return sequence

    script.unreal.FrameRate = lambda numerator, denominator: (numerator, denominator)
    script.unreal.LevelSequence = object()
    script.unreal.LevelSequenceFactoryNew = lambda: object()
    script.unreal.EditorAssetLibrary = SimpleNamespace(
        does_asset_exist=lambda path: False,
    )
    script.unreal.AssetToolsHelpers = SimpleNamespace(get_asset_tools=lambda: FakeAssetTools())
    lighting_calls: list[dict[str, object]] = []
    camera_calls: list[dict[str, object]] = []
    script._configure_sequence_lighting = lambda value, job, **kwargs: lighting_calls.append(
        {"sequence": value, "job": job, **kwargs}
    )
    script._add_orbit_camera = lambda value, job: camera_calls.append(
        {"sequence": value, "job": job}
    )
    script._save_asset = lambda path: None

    def unexpected_mesh_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"scene sequence spawned model/floor: args={args} kwargs={kwargs}")

    script._add_static_mesh_spawnable = unexpected_mesh_spawn
    job = {
        "frames": 8,
        "asset": {
            "kind": "scene",
            # Intentionally omit mesh_path and floor fields. A spawn regression
            # must fail this test instead of silently using synthetic geometry.
        },
    }

    result = script._create_sequence_asset(
        job,
        package_path="/Game/UEF/RenderJobs/run_01",
        asset_name="SceneSequence",
        materials={"cube": object(), "floor": object()},
        lighting_assets={},
        include_hdri_backdrop=False,
    )

    assert result is sequence
    assert sequence.display_rate == (24, 1)
    assert sequence.playback == (0, 8)
    assert len(lighting_calls) == 1
    assert len(camera_calls) == 1


class _FakeStaticMesh:
    def __init__(self, path: str) -> None:
        self.path = path

    def get_path_name(self) -> str:
        return self.path


class _FakeStaticMeshComponent:
    def __init__(self, mesh_path: str) -> None:
        self.mesh = _FakeStaticMesh(mesh_path)
        self.custom_depth_calls: list[bool] = []
        self.stencil_calls: list[int] = []

    def get_editor_property(self, name: str) -> _FakeStaticMesh:
        assert name == "static_mesh"
        return self.mesh

    def set_render_custom_depth(self, enabled: bool) -> None:
        self.custom_depth_calls.append(enabled)

    def set_custom_depth_stencil_value(self, value: int) -> None:
        self.stencil_calls.append(value)


class _FakeActor:
    def __init__(self, name: str, components: list[_FakeStaticMeshComponent]) -> None:
        self.name = name
        self.components = components

    def get_components_by_class(
        self,
        component_class: type[_FakeStaticMeshComponent],
    ) -> list[_FakeStaticMeshComponent]:
        assert component_class is _FakeStaticMeshComponent
        return self.components

    def get_name(self) -> str:
        return self.name


def _install_scene_actor_subsystem(
    script: Any,
    actors: list[_FakeActor],
) -> list[_FakeStaticMeshComponent]:
    components = [component for actor in actors for component in actor.components]
    subsystem = SimpleNamespace(get_all_level_actors=lambda: actors)
    script.unreal.StaticMeshComponent = _FakeStaticMeshComponent
    script.unreal.EditorActorSubsystem = object()
    script.unreal.get_editor_subsystem = lambda subsystem_type: subsystem
    return components


def _install_render_inventory(script: Any, actors: list[_FakeActor]) -> dict[str, object]:
    actor_rows = [
        {
            "actor_name": actor.name,
            "components": [{"mesh_path": component.mesh.path} for component in actor.components],
        }
        for actor in actors
    ]
    inventory: dict[str, object] = {
        "schema_version": 1,
        "actors": actor_rows,
        "static_mesh_actor_count": sum(bool(actor.components) for actor in actors),
        "static_mesh_component_count": sum(len(actor.components) for actor in actors),
    }
    script._current_scene_render_inventory = lambda: (
        inventory,
        {actor.name: actor for actor in actors},
    )
    return inventory


def test_scene_preparation_assigns_one_unique_stencil_per_static_mesh_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_render_script(monkeypatch)
    actors = [
        _FakeActor(
            "TreeActor",
            [
                _FakeStaticMeshComponent("/Game/Scene/SM_Tree.SM_Tree"),
                _FakeStaticMeshComponent("/Game/Scene/SM_Rock.SM_Rock"),
            ],
        ),
        _FakeActor(
            "GroundActor",
            [_FakeStaticMeshComponent("/Game/Scene/SM_Ground.SM_Ground")],
        ),
    ]
    components = _install_scene_actor_subsystem(script, actors)
    inventory = _install_render_inventory(script, actors)

    script._prepare_scene_level(
        {
            "asset": {
                "kind": "scene",
                "no_auto_floor": True,
                "static_mesh_actor_count": 2,
                "static_mesh_component_count": 3,
                "render_inventory": inventory,
                "render_inventory_sha256": script._canonical_digest(inventory),
                "expected_object_stencil_ids": [1, 2],
            }
        }
    )

    assert all(component.custom_depth_calls == [True] for component in components)
    assert [component.stencil_calls for component in actors[0].components] == [[2], [2]]
    assert [component.stencil_calls for component in actors[1].components] == [[1]]


@pytest.mark.parametrize(
    ("expected_actors", "expected_components"),
    [
        (3, 3),
        (2, 1),
    ],
)
def test_scene_preparation_requires_exact_actor_and_component_inventory(
    monkeypatch: pytest.MonkeyPatch,
    expected_actors: int,
    expected_components: int,
) -> None:
    script = _load_render_script(monkeypatch)
    actors = [
        _FakeActor("ActorA", [_FakeStaticMeshComponent("/Game/Scene/SM_A.SM_A")]),
        _FakeActor("ActorB", [_FakeStaticMeshComponent("/Game/Scene/SM_B.SM_B")]),
    ]
    _install_scene_actor_subsystem(script, actors)
    actual_inventory = _install_render_inventory(script, actors)
    expected_inventory = {
        **actual_inventory,
        "static_mesh_actor_count": expected_actors,
        "static_mesh_component_count": expected_components,
    }

    with pytest.raises(
        RuntimeError,
        match="actor/component inventory changed before render",
    ):
        script._prepare_scene_level(
            {
                "asset": {
                    "kind": "scene",
                    "no_auto_floor": True,
                    "static_mesh_actor_count": expected_actors,
                    "static_mesh_component_count": expected_components,
                    "render_inventory": expected_inventory,
                    "render_inventory_sha256": script._canonical_digest(expected_inventory),
                    "expected_object_stencil_ids": list(range(1, expected_actors + 1)),
                }
            }
        )


@pytest.mark.parametrize("no_auto_floor", [False, None])
def test_scene_preparation_requires_no_automatic_floor_policy(
    monkeypatch: pytest.MonkeyPatch,
    no_auto_floor: bool | None,
) -> None:
    script = _load_render_script(monkeypatch)
    job = {
        "asset": {
            "kind": "scene",
            "static_mesh_actor_count": 1,
            "static_mesh_component_count": 1,
        }
    }
    if no_auto_floor is not None:
        job["asset"]["no_auto_floor"] = no_auto_floor

    with pytest.raises(RuntimeError, match="requires no_auto_floor=true"):
        script._prepare_scene_level(job)


def test_scene_job_entry_prepares_persistent_level_before_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_render_script(monkeypatch)
    prepared: list[dict[str, object]] = []
    created: list[dict[str, object]] = []
    events: list[str] = []
    sequence = SimpleNamespace(
        get_path_name=lambda: "/Game/UEF/RenderJobs/run_01/Sequence.Sequence"
    )
    script.unreal.EditorAssetLibrary = SimpleNamespace(make_directory=lambda path: None)
    script._create_materials = lambda package, lighting_preset: {
        "cube": object(),
        "floor": object(),
    }
    script._create_object_mask_material = lambda package: object()

    def fake_prepare_scene(job: dict[str, object]) -> None:
        events.append("prepare_scene")
        prepared.append(job)

    script._prepare_scene_level = fake_prepare_scene

    def fake_create_sequence(job: dict[str, object], **kwargs: object) -> object:
        events.append("create_sequence")
        created.append({"job": job, **kwargs})
        return sequence

    script._create_sequence_asset = fake_create_sequence
    job = {
        "run_id": "run_01",
        "frames": 8,
        "asset": {"kind": "scene"},
        "lighting": {"preset": "none"},
        "sequence_path": "/Game/UEF/RenderJobs/run_01/Sequence.Sequence",
    }

    data_sequence, beauty_sequence = script._create_sequences(job)

    assert prepared == [job]
    assert events == ["prepare_scene", "create_sequence"]
    assert len(created) == 1
    assert created[0]["job"] is job
    assert data_sequence is sequence
    assert beauty_sequence is sequence


def test_three_point_lighting_scales_all_directional_intensities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_render_script(monkeypatch)
    lights: list[dict[str, object]] = []
    script._add_directional_light_actor = lambda **kwargs: lights.append(kwargs)

    script._add_three_point_lighting({"asset": {"lighting_intensity_multiplier": 4.0}})

    assert [item["intensity"] for item in lights] == [32.0, 8.0, 12.0]


def test_scene_camera_uses_catalog_target_for_first_frame_and_all_orbit_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_render_script(monkeypatch)
    target = (125.0, -75.0, 240.0)
    transform_calls: list[tuple[float, float, float, tuple[float, float, float]]] = []
    key_calls: list[dict[str, object]] = []
    camera_calls: list[dict[str, object]] = []

    def fake_transform(
        radius: float,
        azimuth: float,
        elevation: float,
        camera_target: tuple[float, float, float],
    ) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        transform_calls.append((radius, azimuth, elevation, camera_target))
        return (10.0, 20.0, 30.0), (-10.0, 15.0, 0.0)

    class FakeBinding:
        def get_id(self) -> str:
            return "camera-guid"

    class FakeBindingId:
        def __init__(self) -> None:
            self.properties: dict[str, object] = {}

        def set_editor_property(self, name: str, value: object) -> None:
            self.properties[name] = value

    class FakeSection:
        def __init__(self) -> None:
            self.start: int | None = None
            self.end: int | None = None
            self.properties: dict[str, object] = {}

        def set_start_frame(self, value: int) -> None:
            self.start = value

        def set_end_frame(self, value: int) -> None:
            self.end = value

        def set_editor_property(self, name: str, value: object) -> None:
            self.properties[name] = value

    section = FakeSection()
    track = SimpleNamespace(add_section=lambda: section)
    sequence = SimpleNamespace(add_master_track=lambda track_type: track)
    script.unreal.MovieSceneCameraCutTrack = object()
    script.unreal.MovieSceneObjectBindingID = FakeBindingId
    script._orbit_camera_transform = fake_transform

    def fake_add_camera(sequence_value: object, **kwargs: object) -> FakeBinding:
        camera_calls.append({"sequence": sequence_value, **kwargs})
        return FakeBinding()

    script._add_camera_spawnable = fake_add_camera
    script._add_orbit_transform_keys = lambda binding, **kwargs: key_calls.append(
        {"binding": binding, **kwargs}
    )
    job = {
        "camera": {
            "views": 8,
            "elevation_deg": 5.0,
            "fov": 45.0,
            "resolution": [512, 512],
        },
        "asset": {
            "kind": "scene",
            "camera_radius_cm": 650.0,
            "camera_target_cm": list(target),
            "camera_near_clip_cm": 0.1,
            "camera_azimuth_offset_deg": -35.0,
            "camera_elevation_deg": 22.5,
        },
    }

    script._add_orbit_camera(sequence, job)

    assert transform_calls == [(650.0, -35.0, 22.5, target)]
    assert camera_calls[0]["location"] == (10.0, 20.0, 30.0)
    assert camera_calls[0]["custom_near_clip_cm"] == 0.1
    assert key_calls[0]["target"] == target
    assert key_calls[0]["views"] == 8
    assert key_calls[0]["azimuth_offset"] == -35.0
    assert key_calls[0]["elevation"] == 22.5
    assert section.start == 0
    assert section.end == 8


def test_runtime_treats_scene_jobs_as_catalog_sanitized_jobs() -> None:
    tree = ast.parse(RUNTIME_SCRIPT.read_text(encoding="utf-8"))
    catalog_scene_sets = [
        {
            item.value
            for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        for node in ast.walk(tree)
        if isinstance(node, ast.Set)
    ]

    assert sum(values == {"catalog", "scene"} for values in catalog_scene_sets) >= 2
