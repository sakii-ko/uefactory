from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_RUNTIME_SCRIPT = Path(__file__).parents[1] / "ue/UEFBase/Content/Python/uef_render_job_runtime.py"


class _Component:
    def __init__(self) -> None:
        self.properties: dict[str, object] = {}
        self.static_mesh = _Mesh("/Engine/EngineMeshes/Sphere.Sphere")

    def set_editor_property(self, name: str, value: object) -> None:
        self.properties[name] = value

    def get_editor_property(self, name: str) -> object:
        assert name == "static_mesh"
        return self.static_mesh


class _Mesh:
    def __init__(self, path: str) -> None:
        self.path = path

    def get_path_name(self) -> str:
        return self.path


class _Pawn:
    def __init__(self, component_count: int = 1) -> None:
        self.hidden_in_game = False
        self.hidden_in_editor = False
        self.components = [_Component() for _ in range(component_count)]

    def set_actor_hidden_in_game(self, hidden: bool) -> None:
        self.hidden_in_game = hidden

    def set_is_temporarily_hidden_in_editor(self, hidden: bool) -> None:
        self.hidden_in_editor = hidden

    def get_components_by_class(self, component_class: object) -> list[_Component]:
        assert component_class is _PrimitiveComponent
        return self.components


class _PrimitiveComponent:
    pass


class _World:
    def __init__(self, *pawns: _Pawn) -> None:
        self.pawns = list(pawns)


class _Delegate:
    def __init__(self) -> None:
        self.bindings: list[tuple[object, str]] = []

    def add_function_unique(self, target: object, function_name: str) -> None:
        self.bindings.append((target, function_name))


class _Pipeline:
    def __init__(self, *, outer: _World) -> None:
        self.outer = outer
        self.on_movie_pipeline_work_finished_delegate = _Delegate()
        self.initialized_job: object | None = None

    def initialize(self, job: object) -> None:
        self.initialized_job = job


class _Queue:
    def __init__(self, *jobs: object) -> None:
        self.jobs = list(jobs)

    def get_jobs(self) -> list[object]:
        return self.jobs


class _Executor:
    target_pipeline_class = _Pipeline

    def __init__(self, *worlds: _World) -> None:
        self.worlds = list(worlds)
        self.world_index = 0
        self.active_movie_pipeline: _Pipeline | None = None

    def get_last_loaded_world(self) -> _World:
        world = self.worlds[self.world_index]
        self.world_index += 1
        return world


def _load_pipeline_functions(
    *,
    asset_kind: str,
    worlds_queried: list[_World],
    pipelines: list[_Pipeline],
    queue: _Queue,
) -> dict[str, Any]:
    tree = ast.parse(_RUNTIME_SCRIPT.read_text(encoding="utf-8"))
    selected: list[ast.stmt] = []
    selected_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
            "_hide_catalog_pawns",
            "_start_next_pipeline",
        }:
            selected.append(node)
            selected_names.add(node.name)
    assert selected_names == {
        "_hide_catalog_pawns",
        "_start_next_pipeline",
    }

    class GameplayStatics:
        @staticmethod
        def get_all_actors_of_class(world: _World, actor_class: object) -> list[_Pawn]:
            assert actor_class is Pawn
            worlds_queried.append(world)
            return world.pawns

    class Pawn:
        pass

    def new_object(
        pipeline_class: type[_Pipeline],
        *,
        outer: _World,
        base_type: object,
    ) -> _Pipeline:
        assert pipeline_class is _Pipeline
        assert base_type is MoviePipeline
        pipeline = pipeline_class(outer=outer)
        pipelines.append(pipeline)
        return pipeline

    class MoviePipeline:
        pass

    unreal = SimpleNamespace(
        Pawn=Pawn,
        GameplayStatics=GameplayStatics,
        MoviePipeline=MoviePipeline,
        PrimitiveComponent=_PrimitiveComponent,
        StaticMeshComponent=_Component,
        log=lambda _message: None,
        new_object=new_object,
    )
    namespace: dict[str, Any] = {
        "_PIPELINE_QUEUE": queue,
        "_STATE": {
            "job": {"asset": {"kind": asset_kind}},
            "job_index": 0,
            "scene_sanitization": (
                {
                    "policy": "catalog_hide_all_pawns_v2",
                    "subjobs": [],
                }
                if asset_kind == "catalog"
                else {"policy": "not_applicable"}
            ),
        },
        "json": json,
        "unreal": unreal,
    }
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, _RUNTIME_SCRIPT, "exec"), namespace)
    return namespace


def test_catalog_all_pawns_are_hidden_again_for_every_subjob() -> None:
    jobs = (object(), object())
    queue = _Queue(*jobs)
    worlds = (_World(_Pawn(2)), _World(_Pawn(1)))
    worlds_queried: list[_World] = []
    pipelines: list[_Pipeline] = []
    runtime = _load_pipeline_functions(
        asset_kind="catalog",
        worlds_queried=worlds_queried,
        pipelines=pipelines,
        queue=queue,
    )
    executor = _Executor(*worlds)

    assert runtime["_start_next_pipeline"](executor) is True
    runtime["_STATE"]["job_index"] = 1
    assert runtime["_start_next_pipeline"](executor) is True

    assert worlds_queried == list(worlds)
    assert [pipeline.outer for pipeline in pipelines] == list(worlds)
    assert [pipeline.initialized_job for pipeline in pipelines] == list(jobs)
    assert runtime["_STATE"]["scene_sanitization"] == {
        "policy": "catalog_hide_all_pawns_v2",
        "subjobs": [
            {
                "hidden_pawn_count": 1,
                "editor_hidden_pawn_count": 1,
                "hidden_static_meshes": ["/Engine/EngineMeshes/Sphere.Sphere"],
                "subjob_index": 0,
            },
            {
                "hidden_pawn_count": 1,
                "editor_hidden_pawn_count": 1,
                "hidden_static_meshes": ["/Engine/EngineMeshes/Sphere.Sphere"],
                "subjob_index": 1,
            },
        ],
    }
    for world in worlds:
        for pawn in world.pawns:
            assert pawn.hidden_in_game is True
            assert pawn.hidden_in_editor is True
            assert all(
                component.properties == {"visible": False, "hidden_in_game": True}
                for component in pawn.components
            )


def test_builtin_subjobs_do_not_query_or_mutate_pawns() -> None:
    jobs = (object(), object())
    queue = _Queue(*jobs)
    pawns = (_Pawn(2), _Pawn(1))
    worlds = (_World(pawns[0]), _World(pawns[1]))
    worlds_queried: list[_World] = []
    pipelines: list[_Pipeline] = []
    runtime = _load_pipeline_functions(
        asset_kind="builtin",
        worlds_queried=worlds_queried,
        pipelines=pipelines,
        queue=queue,
    )
    executor = _Executor(*worlds)

    assert runtime["_start_next_pipeline"](executor) is True
    runtime["_STATE"]["job_index"] = 1
    assert runtime["_start_next_pipeline"](executor) is True

    assert worlds_queried == []
    assert [pipeline.initialized_job for pipeline in pipelines] == list(jobs)
    assert runtime["_STATE"]["scene_sanitization"] == {"policy": "not_applicable"}
    for pawn in pawns:
        assert pawn.hidden_in_game is False
        assert pawn.hidden_in_editor is False
        assert all(component.properties == {} for component in pawn.components)
