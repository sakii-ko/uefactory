from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest


class _FakeClass:
    def __init__(self, name: str) -> None:
        self._name = name

    def get_name(self) -> str:
        return self._name


class _FakeAsset:
    def __init__(self, path: str, class_name: str) -> None:
        self._path = path
        self._class_name = class_name

    def get_path_name(self) -> str:
        return self._path

    def get_name(self) -> str:
        return self._path.rsplit(".", 1)[-1]

    def get_class(self) -> _FakeClass:
        return _FakeClass(self._class_name)


class _FakeMaterialInterface(_FakeAsset):
    pass


class _FakeExpression(_FakeAsset):
    _ordinal = 0

    def __init__(self, texture: _FakeTexture | None = None) -> None:
        type(self)._ordinal += 1
        super().__init__(
            f"/Transient/Expression_{self._ordinal}", "MaterialExpressionTextureSample"
        )
        self.texture = texture
        self.material: _FakeMaterial | None = None
        self.properties: dict[str, object] = {}

    def set_editor_property(self, name: str, value: object) -> None:
        self.properties[name] = value
        if name == "texture":
            self.texture = cast(_FakeTexture, value)

    def get_editor_property(self, name: str) -> object:
        return self.properties[name]


class _FakeMaterial(_FakeMaterialInterface):
    def __init__(
        self,
        path: str,
        *,
        used_textures: list[_FakeTexture] | None = None,
        defaults: dict[str, _FakeTexture] | None = None,
    ) -> None:
        super().__init__(path, "Material")
        self.used_textures = used_textures or []
        self.defaults = defaults or {}
        self.properties: dict[str, object] = {"blend_mode": "opaque"}
        self.graph = {
            role: _FakeExpression(texture)
            for role, texture in zip(
                ("base_color", "normal", "roughness", "metallic"),
                self.used_textures,
                strict=False,
            )
        }

    def set_editor_property(self, name: str, value: object) -> None:
        self.properties[name] = value

    def get_editor_property(self, name: str) -> object:
        return self.properties[name]


class _FakeMaterialInstanceConstant(_FakeMaterialInterface):
    def __init__(
        self,
        path: str,
        *,
        base_material: _FakeMaterial,
        current_textures: dict[str, _FakeTexture],
    ) -> None:
        super().__init__(path, "MaterialInstanceConstant")
        self.base_material = base_material
        self.current_textures = current_textures

    def get_base_material(self) -> _FakeMaterial:
        return self.base_material


class _FakeTexture(_FakeAsset):
    def __init__(self, path: str) -> None:
        super().__init__(path, "Texture2D")
        self.properties: dict[str, object] = {
            "srgb": True,
            "compression_settings": "default",
            "flip_green_channel": False,
        }

    def set_editor_property(self, name: str, value: object) -> None:
        self.properties[name] = value

    def get_editor_property(self, name: str) -> object:
        return self.properties[name]


class _FakeSlot:
    def __init__(self, name: str, material: _FakeMaterialInterface | None) -> None:
        self.name = name
        self.material = material

    def get_editor_property(self, name: str) -> object:
        if name == "material_interface":
            return self.material
        if name == "material_slot_name":
            return self.name
        raise AssertionError(name)


class _Vector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z

    def __sub__(self, other: _Vector) -> _Vector:
        return _Vector(self.x - other.x, self.y - other.y, self.z - other.z)


class _Box:
    def __init__(self) -> None:
        self.min = _Vector(-1.0, -2.0, 0.0)
        self.max = _Vector(1.0, 2.0, 3.0)


class _MeshDescription:
    def get_triangle_count(self) -> int:
        return 6

    def get_vertex_count(self) -> int:
        return 12


class _FakeStaticMesh(_FakeAsset):
    def __init__(self, path: str, slots: list[_FakeSlot]) -> None:
        super().__init__(path, "StaticMesh")
        self.slots = slots

    def get_editor_property(self, name: str) -> list[_FakeSlot]:
        if name != "static_materials":
            raise AssertionError(name)
        return self.slots

    def get_bounding_box(self) -> _Box:
        return _Box()

    def get_static_mesh_description(self, lod_index: int) -> _MeshDescription:
        assert lod_index == 0
        return _MeshDescription()

    def get_num_lods(self) -> int:
        return 1

    def get_num_triangles(self, lod_index: int) -> int:
        assert lod_index == 0
        return 4


class _EditorAssetLibrary:
    registry: dict[str, _FakeAsset] = {}

    @classmethod
    def load_asset(cls, path: str) -> _FakeAsset | None:
        return cls.registry.get(path)


class _MaterialEditingLibrary:
    @classmethod
    def get_used_textures(cls, material: _FakeMaterial) -> list[_FakeTexture]:
        return material.used_textures

    @classmethod
    def get_material_property_input_node(
        cls,
        material: _FakeMaterial,
        material_property: str,
    ) -> _FakeExpression | None:
        return material.graph.get(material_property)

    @classmethod
    def get_inputs_for_material_expression(
        cls,
        material: _FakeMaterial,
        expression: _FakeExpression,
    ) -> list[_FakeExpression | None]:
        del material, expression
        return [None]

    @classmethod
    def create_material_expression(
        cls,
        material: _FakeMaterial,
        expression_class: type[_FakeExpression],
        x: int,
        y: int,
    ) -> _FakeExpression:
        del x, y
        expression = expression_class()
        expression.material = material
        return expression

    @classmethod
    def connect_material_property(
        cls,
        expression: _FakeExpression,
        output_name: str,
        material_property: str,
    ) -> bool:
        del output_name
        assert expression.material is not None
        expression.material.graph[material_property] = expression
        return True

    @classmethod
    def recompile_material(cls, material: _FakeMaterial) -> None:
        del material

    @classmethod
    def get_texture_parameter_names(
        cls,
        material: _FakeMaterialInstanceConstant,
    ) -> list[str]:
        return list(material.current_textures)

    @classmethod
    def get_material_default_texture_parameter_value(
        cls,
        material: _FakeMaterial,
        parameter_name: str,
    ) -> _FakeTexture | None:
        return material.defaults.get(parameter_name)

    @classmethod
    def get_material_instance_texture_parameter_value(
        cls,
        material: _FakeMaterialInstanceConstant,
        parameter_name: str,
    ) -> _FakeTexture | None:
        return material.current_textures.get(parameter_name)


def _load_ingest_script(monkeypatch: pytest.MonkeyPatch) -> Any:
    unreal = ModuleType("unreal")
    unreal_api = cast(Any, unreal)
    unreal_api.StaticMesh = _FakeStaticMesh
    unreal_api.MaterialInterface = _FakeMaterialInterface
    unreal_api.Material = _FakeMaterial
    unreal_api.MaterialInstanceConstant = _FakeMaterialInstanceConstant
    unreal_api.Texture = _FakeTexture
    unreal_api.Texture2D = _FakeTexture
    unreal_api.EditorAssetLibrary = _EditorAssetLibrary
    unreal_api.MaterialEditingLibrary = _MaterialEditingLibrary
    unreal_api.MaterialProperty = SimpleNamespace(
        MP_BASE_COLOR="base_color",
        MP_METALLIC="metallic",
        MP_ROUGHNESS="roughness",
        MP_NORMAL="normal",
        MP_OPACITY="opacity",
    )
    unreal_api.MaterialExpressionTextureSampleParameter2D = _FakeExpression
    unreal_api.MaterialExpressionConstant = _FakeExpression
    unreal_api.MaterialSamplerType = SimpleNamespace(
        SAMPLERTYPE_COLOR="color",
        SAMPLERTYPE_NORMAL="normal",
        SAMPLERTYPE_MASKS="masks",
    )
    unreal_api.BlendMode = SimpleNamespace(BLEND_TRANSLUCENT="translucent")
    unreal_api.TextureCompressionSettings = SimpleNamespace(
        TC_DEFAULT="default",
        TC_NORMALMAP="normalmap",
        TC_MASKS="masks",
    )
    monkeypatch.setitem(sys.modules, "unreal", unreal)

    script_path = Path(__file__).parents[1] / "ue/UEFBase/Content/Python/uef_ingest_asset.py"
    spec = importlib.util.spec_from_file_location("test_uef_ingest_asset", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_asset_payload_records_effective_material_texture_dependencies_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_ingest_script(monkeypatch)
    default_texture = _FakeTexture("/Engine/Defaults/T_Default.T_Default")
    direct_texture = _FakeTexture("/Game/Asset/T_Direct.T_Direct")
    override_texture = _FakeTexture("/Game/Asset/T_Override.T_Override")
    base_material = _FakeMaterial(
        "/Engine/Materials/M_Base.M_Base",
        used_textures=[default_texture, direct_texture],
        defaults={"BaseColor": default_texture},
    )
    material = _FakeMaterialInstanceConstant(
        "/Game/Asset/MI_Asset.MI_Asset",
        base_material=base_material,
        current_textures={"BaseColor": override_texture},
    )
    mesh = _FakeStaticMesh(
        "/Game/Asset/SM_Asset.SM_Asset",
        [_FakeSlot("Body", material), _FakeSlot("Unused", None)],
    )
    _EditorAssetLibrary.registry = {
        asset.get_path_name(): asset
        for asset in [base_material, material, mesh, direct_texture, override_texture]
    }

    primary = script._asset_payload(
        [mesh, material, direct_texture, override_texture],
        True,
    )
    reload = script._asset_payload(
        [mesh, material, direct_texture, override_texture],
        True,
    )
    finalize = script._asset_payload(
        [mesh, material, direct_texture, override_texture],
        True,
    )

    assert primary == reload == finalize
    assert primary["import_backend"] == "asset_tools_auto"
    assert primary["normalization"] == {
        "target_units": "centimeters",
        "target_up_axis": "Z",
        "target_handedness": "left_handed",
        "source_conversion": "delegated_to_engine_importer",
        "package_pivot_policy": "preserve",
        "uniform_scale": 1.0,
    }
    assert primary["static_meshes"][0]["triangle_count"] == 6
    assert primary["static_meshes"][0]["render_fallback_triangle_count"] == 4
    assert primary["static_meshes"][0]["material_slots"] == [
        {
            "index": 0,
            "slot_name": "Body",
            "material_path": "/Game/Asset/MI_Asset.MI_Asset",
            "texture_paths": [
                "/Game/Asset/T_Direct.T_Direct",
                "/Game/Asset/T_Override.T_Override",
            ],
        },
        {
            "index": 1,
            "slot_name": "Unused",
            "material_path": None,
            "texture_paths": [],
        },
    ]


def test_material_slot_rejects_non_loadable_material_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_ingest_script(monkeypatch)
    material = _FakeMaterial("/Game/Asset/M_Missing.M_Missing")
    mesh = _FakeStaticMesh(
        "/Game/Asset/SM_Asset.SM_Asset",
        [_FakeSlot("Body", material)],
    )
    _EditorAssetLibrary.registry = {}

    with pytest.raises(RuntimeError, match="could not reload slot material"):
        script._material_slots_payload(mesh)


def test_fbx_pbr_postprocess_connects_all_roles_and_records_normal_convention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_ingest_script(monkeypatch)
    material = _FakeMaterial("/Game/Asset/Shelf_01.Shelf_01")
    textures = [
        _FakeTexture("/Game/Asset/Shelf_01_diff_1k.Shelf_01_diff_1k"),
        _FakeTexture("/Game/Asset/Shelf_01_nor_gl_1k.Shelf_01_nor_gl_1k"),
        _FakeTexture("/Game/Asset/Shelf_01_roughness_1k.Shelf_01_roughness_1k"),
        _FakeTexture("/Game/Asset/Shelf_01_metallic_1k.Shelf_01_metallic_1k"),
    ]
    mesh = _FakeStaticMesh(
        "/Game/Asset/SM_Shelf.SM_Shelf",
        [_FakeSlot("Shelf_01", material)],
    )
    loaded = [mesh, material, *textures]
    _EditorAssetLibrary.registry = {asset.get_path_name(): asset for asset in loaded}

    script._apply_fbx_pbr_postprocess(loaded)
    payload = script._asset_payload(loaded, True, "fbx")

    bindings = payload["material_postprocess"]["materials"][0]["bindings"]
    assert {item["role"] for item in bindings} == {
        "base_color",
        "metallic",
        "normal",
        "roughness",
    }
    normal = next(item for item in bindings if item["role"] == "normal")
    assert normal["source_convention"] == "opengl"
    assert normal["green_channel_flipped"] is True
    assert textures[1].get_editor_property("compression_settings") == "normalmap"
    assert textures[2].get_editor_property("srgb") is False
    assert textures[3].get_editor_property("compression_settings") == "masks"
    assert len(payload["static_meshes"][0]["material_slots"][0]["texture_paths"]) == 4


def test_fbx_texture_mapping_uses_longest_material_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_ingest_script(monkeypatch)
    main = _FakeMaterial("/Game/Asset/standing_picture_frame_01.standing_picture_frame_01")
    artwork = _FakeMaterial(
        "/Game/Asset/standing_picture_frame_01_artwork.standing_picture_frame_01_artwork"
    )
    glass = _FakeMaterial(
        "/Game/Asset/standing_picture_frame_01_glass.standing_picture_frame_01_glass"
    )
    main_diff = _FakeTexture(
        "/Game/Asset/standing_picture_frame_01_diff_1k.standing_picture_frame_01_diff_1k"
    )
    artwork_diff = _FakeTexture(
        "/Game/Asset/standing_picture_frame_01_artwork_diff_1k."
        "standing_picture_frame_01_artwork_diff_1k"
    )
    mesh = _FakeStaticMesh(
        "/Game/Asset/SM_Frame.SM_Frame",
        [
            _FakeSlot("glass", glass),
            _FakeSlot("artwork", artwork),
            _FakeSlot("frame", main),
        ],
    )

    mapping = script._fbx_material_texture_map(
        [mesh, main, artwork, glass, main_diff, artwork_diff]
    )

    assert mapping[main]["base_color"][0] is main_diff
    assert mapping[artwork]["base_color"][0] is artwork_diff
    assert mapping[glass] == {}


def test_fbx_pbr_postprocess_clears_importer_metallic_when_texture_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_ingest_script(monkeypatch)
    material = _FakeMaterial("/Game/Asset/Artwork.Artwork")
    stale_metallic = _FakeTexture("/Game/Asset/LegacyColor.LegacyColor")
    material.graph["metallic"] = _FakeExpression(stale_metallic)
    textures = [
        _FakeTexture("/Game/Asset/Artwork_diff_1k.Artwork_diff_1k"),
        _FakeTexture("/Game/Asset/Artwork_nor_gl_1k.Artwork_nor_gl_1k"),
        _FakeTexture("/Game/Asset/Artwork_rough_1k.Artwork_rough_1k"),
    ]
    mesh = _FakeStaticMesh(
        "/Game/Asset/SM_Artwork.SM_Artwork",
        [_FakeSlot("Artwork", material)],
    )
    loaded = [mesh, material, stale_metallic, *textures]
    _EditorAssetLibrary.registry = {asset.get_path_name(): asset for asset in loaded}

    script._apply_fbx_pbr_postprocess(loaded)
    payload = script._asset_payload(loaded, True, "fbx")

    metallic_root = material.graph["metallic"]
    assert metallic_root.texture is None
    assert metallic_root.properties["r"] == 0.0
    bindings = payload["material_postprocess"]["materials"][0]["bindings"]
    assert {item["role"] for item in bindings} == {
        "base_color",
        "normal",
        "roughness",
    }


def test_fbx_pbr_postprocess_makes_untextured_named_glass_translucent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_ingest_script(monkeypatch)
    material = _FakeMaterial("/Game/Asset/Frame.Frame")
    glass = _FakeMaterial("/Game/Asset/Frame_glass.Frame_glass")
    textures = [
        _FakeTexture("/Game/Asset/Frame_diff_1k.Frame_diff_1k"),
        _FakeTexture("/Game/Asset/Frame_nor_gl_1k.Frame_nor_gl_1k"),
        _FakeTexture("/Game/Asset/Frame_rough_1k.Frame_rough_1k"),
    ]
    mesh = _FakeStaticMesh(
        "/Game/Asset/SM_Frame.SM_Frame",
        [_FakeSlot("Frame", material), _FakeSlot("Glass", glass)],
    )
    loaded = [mesh, material, glass, *textures]
    _EditorAssetLibrary.registry = {asset.get_path_name(): asset for asset in loaded}

    script._apply_fbx_pbr_postprocess(loaded)
    payload = script._asset_payload(loaded, True, "fbx")

    glass_payload = next(
        item
        for item in payload["material_postprocess"]["materials"]
        if item["material_path"] == glass.get_path_name()
    )
    assert glass_payload["bindings"] == []
    assert glass_payload["shading_override"] == {
        "policy": "glass_translucent_v1",
        "blend_mode": "translucent",
        "opacity": 0.12,
    }
    assert glass.graph["opacity"].properties["r"] == 0.12


def test_finalize_rejects_payload_mismatch_before_irreversible_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _load_ingest_script(monkeypatch)
    texture = _FakeTexture("/Game/Asset/Asset_diff_1k.Asset_diff_1k")
    material = _FakeMaterial("/Game/Asset/Asset.Asset", used_textures=[texture])
    mesh = _FakeStaticMesh(
        "/Game/Asset/SM_Asset.SM_Asset",
        [_FakeSlot("Asset", material)],
    )
    loaded = [mesh, material, texture]
    _EditorAssetLibrary.registry = {asset.get_path_name(): asset for asset in loaded}
    expected = script._asset_payload(loaded, True, "glb")
    expected["static_mesh_count"] = 2
    deleted: list[str] = []
    monkeypatch.setattr(script, "_save_directory", lambda path: None)
    monkeypatch.setattr(script, "_delete_directory", lambda path: deleted.append(path))

    with pytest.raises(RuntimeError, match="differs from host-approved"):
        script._run_finalize(
            {
                "imported_objects": [script._object_payload(asset) for asset in loaded],
                "require_single_static_mesh": True,
                "source_format": "glb",
                "expected_asset_payload": expected,
                "had_existing": True,
            },
            {},
            tmp_path / "finalize_manifest.json",
            "test_asset",
        )

    assert deleted == []
