from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from uefactory import __version__
from uefactory.core.config import Settings

USER_AGENT = f"UEFactory/{__version__} research downloader"
M2_SAMPLE_SUBDIR = "m2_samples"
M2_INVENTORY_VERSION = 1
MAX_DOWNLOAD_WORKERS = 6
KHRONOS_COMMIT = "2bac6f8c57bf471df0d2a1e8a8ec023c7801dddf"
CC0_LICENSE = "CC0-1.0"
CC0_LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
CC_BY_4_LICENSE = "CC-BY-4.0"
CC_BY_4_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/legalcode"
M2_OPEN_LICENSES = frozenset({CC0_LICENSE, CC_BY_4_LICENSE})
M2_LICENSE_POLICY = "All assets use approved open licenses: CC0-1.0 or CC-BY-4.0."

_ASSET_ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED_DOWNLOAD_HOSTS = frozenset({"dl.polyhaven.org", "raw.githubusercontent.com"})


class ModelAcquireError(RuntimeError):
    """Raised when the pinned M2 sample set cannot be acquired safely."""


@dataclass(frozen=True)
class ModelFileSpec:
    relative_path: Path
    url: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class ModelSpec:
    asset_id: str
    name: str
    source: str
    source_id: str
    source_url: str
    license: str
    license_url: str
    attribution: str
    tags: tuple[str, ...]
    main_file: Path
    files: tuple[ModelFileSpec, ...]

    @property
    def dependencies(self) -> tuple[Path, ...]:
        return tuple(
            item.relative_path for item in self.files if item.relative_path != self.main_file
        )


@dataclass(frozen=True)
class AcquiredModel:
    asset_id: str
    main_path: Path
    dependency_paths: tuple[Path, ...]
    metadata_path: Path
    downloaded_files: int
    reused_files: int
    bytes: int


@dataclass(frozen=True)
class ModelAcquireResult:
    root_dir: Path
    inventory_path: Path
    models: tuple[AcquiredModel, ...]
    downloaded_files: int
    reused_files: int
    bytes: int


def _file(relative_path: str, size: int, sha256: str, url: str) -> ModelFileSpec:
    return ModelFileSpec(Path(relative_path), url, size, sha256)


def _khronos_file(model: str, size: int, sha256: str) -> ModelFileSpec:
    return _file(
        f"{model}.glb",
        size,
        sha256,
        "https://raw.githubusercontent.com/KhronosGroup/glTF-Sample-Assets/"
        f"{KHRONOS_COMMIT}/Models/{model}/glTF-Binary/{model}.glb",
    )


def _khronos_model(
    *,
    asset_id: str,
    model: str,
    name: str,
    size: int,
    sha256: str,
    attribution: str,
    tags: tuple[str, ...],
    license: str = CC0_LICENSE,
    license_url: str = CC0_LICENSE_URL,
) -> ModelSpec:
    return ModelSpec(
        asset_id=asset_id,
        name=name,
        source="khronos",
        source_id=model,
        source_url=(
            "https://github.com/KhronosGroup/glTF-Sample-Assets/tree/"
            f"{KHRONOS_COMMIT}/Models/{model}"
        ),
        license=license,
        license_url=license_url,
        attribution=attribution,
        tags=tags,
        main_file=Path(f"{model}.glb"),
        files=(_khronos_file(model, size, sha256),),
    )


M2_MODEL_SPECS: tuple[ModelSpec, ...] = (
    _khronos_model(
        asset_id="khronos_avocado",
        model="Avocado",
        name="Avocado",
        size=8_110_040,
        sha256="ccc9c3ce56423720b09399c2351537207cd5a65f859f9e6e2f30922762f3abd4",
        attribution="Microsoft; distributed by Khronos Group glTF Sample Assets.",
        tags=("food", "fruit", "textured"),
    ),
    _khronos_model(
        asset_id="khronos_barramundi_fish",
        model="BarramundiFish",
        name="Barramundi Fish",
        size=12_488_144,
        sha256="ecc3bafb6b00f2c8b810863c388e3768a7b7ea0d0335e8cb8c574c266e571f4a",
        attribution="Microsoft; distributed by Khronos Group glTF Sample Assets.",
        tags=("animal", "fish", "textured"),
    ),
    _khronos_model(
        asset_id="khronos_boom_box",
        model="BoomBox",
        name="Boom Box",
        size=10_614_184,
        sha256="f8b918445ebdd006768232205a62f5182d2208ca57f84c6ccc084943c0bc8f15",
        attribution="Microsoft; distributed by Khronos Group glTF Sample Assets.",
        tags=("electronics", "radio", "textured"),
    ),
    _khronos_model(
        asset_id="khronos_corset",
        model="Corset",
        name="Corset",
        size=13_491_364,
        sha256="9582c0dc0dee813be77f60e6ddf7213987c7e11497bf3cc66fd7b18957ae0d26",
        attribution="UX3D and Microsoft; distributed by Khronos Group glTF Sample Assets.",
        tags=("clothing", "fabric", "textured"),
    ),
    _khronos_model(
        asset_id="khronos_water_bottle",
        model="WaterBottle",
        name="Water Bottle",
        size=8_966_700,
        sha256="b337e526fd6a162013c2984aeec163f5fbb4f717252724dfc3f3458bd51df94b",
        attribution="Microsoft; distributed by Khronos Group glTF Sample Assets.",
        tags=("bottle", "container", "textured"),
    ),
    ModelSpec(
        asset_id="polyhaven_shelf_01",
        name="Shelf 01",
        source="polyhaven",
        source_id="Shelf_01",
        source_url="https://polyhaven.com/a/Shelf_01",
        license=CC0_LICENSE,
        license_url="https://polyhaven.com/license",
        attribution="Gabriel Radić; distributed by Poly Haven.",
        tags=("furniture", "shelf", "textured"),
        main_file=Path("Shelf_01_1k.fbx"),
        files=(
            _file(
                "Shelf_01_1k.fbx",
                26_588,
                "3c1987d8913f6cdc762e705a3347da77c241b635ce551448ff2b89e1d9166f69",
                "https://dl.polyhaven.org/file/ph-assets/Models/fbx/1k/Shelf_01/Shelf_01_1k.fbx",
            ),
            _file(
                "textures/Shelf_01_diff_1k.jpg",
                153_220,
                "875bb8110dec4f409d9127cba95f7c0ead02bdfeef6dd47232df939e4de35a5c",
                "https://dl.polyhaven.org/file/ph-assets/Models/jpg/1k/Shelf_01/"
                "Shelf_01_diff_1k.jpg",
            ),
            _file(
                "textures/Shelf_01_metallic_1k.exr",
                4_784,
                "3cd6acee16794ccbac8a3c81e1152511c76d018f108ff4687bae9dbf5112f300",
                "https://dl.polyhaven.org/file/ph-assets/Models/exr/1k/Shelf_01/"
                "Shelf_01_metallic_1k.exr",
            ),
            _file(
                "textures/Shelf_01_nor_gl_1k.exr",
                2_027_513,
                "c331fe8ec54e9da701a0293a8f1ee8ea3102b1ed7335fdd46f1cfe53ab08f1ea",
                "https://dl.polyhaven.org/file/ph-assets/Models/exr/1k/Shelf_01/"
                "Shelf_01_nor_gl_1k.exr",
            ),
            _file(
                "textures/Shelf_01_roughness_1k.jpg",
                235_823,
                "cdcbf1cbad05ac35ab4ed0bc636d5d486413e22dad5cbe9fc4314386c972388f",
                "https://dl.polyhaven.org/file/ph-assets/Models/jpg/1k/Shelf_01/"
                "Shelf_01_roughness_1k.jpg",
            ),
        ),
    ),
    ModelSpec(
        asset_id="polyhaven_side_table_01",
        name="Side Table 01",
        source="polyhaven",
        source_id="side_table_01",
        source_url="https://polyhaven.com/a/side_table_01",
        license=CC0_LICENSE,
        license_url="https://polyhaven.com/license",
        attribution="James Ray Cock; distributed by Poly Haven.",
        tags=("furniture", "table", "textured"),
        main_file=Path("side_table_01_1k.fbx"),
        files=(
            _file(
                "side_table_01_1k.fbx",
                98_268,
                "be9b3812818e8f4d536679c549b4d048447d61856c081688d69d5f40f9f79df7",
                "https://dl.polyhaven.org/file/ph-assets/Models/fbx/1k/side_table_01/"
                "side_table_01_1k.fbx",
            ),
            _file(
                "textures/side_table_01_diff_1k.jpg",
                183_315,
                "e0d9f2f91bede592868f9b89d141f5e8542524b8356f1b68f0f5c6bb6447731d",
                "https://dl.polyhaven.org/file/ph-assets/Models/jpg/1k/side_table_01/"
                "side_table_01_diff_1k.jpg",
            ),
            _file(
                "textures/side_table_01_metal_1k.exr",
                21_982,
                "eb5342142a2dc5bfe9594c9906363e7e7a0f3260e2b9456eed809f08c90621bc",
                "https://dl.polyhaven.org/file/ph-assets/Models/exr/1k/side_table_01/"
                "side_table_01_metal_1k.exr",
            ),
            _file(
                "textures/side_table_01_nor_gl_1k.exr",
                534_449,
                "221f2545544b417481a5f36971e654db7ab8daef317dda589a94a78c4d8fdeaf",
                "https://dl.polyhaven.org/file/ph-assets/Models/exr/1k/side_table_01/"
                "side_table_01_nor_gl_1k.exr",
            ),
            _file(
                "textures/side_table_01_rough_1k.jpg",
                148_894,
                "b8825f54e30a63d7506c8c3c96a31c6467992edd75f22350506e055727d01e96",
                "https://dl.polyhaven.org/file/ph-assets/Models/jpg/1k/side_table_01/"
                "side_table_01_rough_1k.jpg",
            ),
        ),
    ),
    ModelSpec(
        asset_id="polyhaven_standing_picture_frame_01",
        name="Standing Picture Frame 01",
        source="polyhaven",
        source_id="standing_picture_frame_01",
        source_url="https://polyhaven.com/a/standing_picture_frame_01",
        license=CC0_LICENSE,
        license_url="https://polyhaven.com/license",
        attribution="James Ray Cock; distributed by Poly Haven.",
        tags=("decorative", "frame", "textured"),
        main_file=Path("standing_picture_frame_01_1k.fbx"),
        files=(
            _file(
                "standing_picture_frame_01_1k.fbx",
                84_572,
                "125d3316638c81eb8dff2fdf548d60b783f1c26cca5fbdd6f2e114021b4172c1",
                "https://dl.polyhaven.org/file/ph-assets/Models/fbx/1k/standing_picture_frame_01/"
                "standing_picture_frame_01_1k.fbx",
            ),
            _file(
                "textures/standing_picture_frame_01_artwork_diff_1k.png",
                466_004,
                "a362c83c936cb01ea5c50f8e5660bea03ca0b7da8ab63ee75300520743a40c18",
                "https://dl.polyhaven.org/file/ph-assets/Models/png/1k/"
                "standing_picture_frame_01/standing_picture_frame_01_artwork_diff_1k.png",
            ),
            _file(
                "textures/standing_picture_frame_01_artwork_nor_gl_1k.png",
                5_376,
                "574f4935a0d6805b9ae0e10a2d262629bd1022956a3254bb1eae1ebcb171e2a3",
                "https://dl.polyhaven.org/file/ph-assets/Models/png/1k/"
                "standing_picture_frame_01/standing_picture_frame_01_artwork_nor_gl_1k.png",
            ),
            _file(
                "textures/standing_picture_frame_01_artwork_rough_1k.png",
                3_200,
                "b0f5654c6cff2ea188d712219d7b839f971c59e99a3b4d9a446ad83ee95e833d",
                "https://dl.polyhaven.org/file/ph-assets/Models/png/1k/"
                "standing_picture_frame_01/standing_picture_frame_01_artwork_rough_1k.png",
            ),
            _file(
                "textures/standing_picture_frame_01_diff_1k.jpg",
                94_436,
                "e9d1030940b00ed576cafd704f7fc44226a255ed8d2db5ef3b8fd02d5521a763",
                "https://dl.polyhaven.org/file/ph-assets/Models/jpg/1k/"
                "standing_picture_frame_01/standing_picture_frame_01_diff_1k.jpg",
            ),
            _file(
                "textures/standing_picture_frame_01_metal_1k.exr",
                23_119,
                "f58d49aadabfa335da3466b07f1e5e7864fe6ed4f5b0d3186035f63a916db06f",
                "https://dl.polyhaven.org/file/ph-assets/Models/exr/1k/"
                "standing_picture_frame_01/standing_picture_frame_01_metal_1k.exr",
            ),
            _file(
                "textures/standing_picture_frame_01_nor_gl_1k.exr",
                370_286,
                "a7a3909164cf14b8aca3fed22d66d7a4d63bf14d1a3abe61b16936c77919adcf",
                "https://dl.polyhaven.org/file/ph-assets/Models/exr/1k/"
                "standing_picture_frame_01/standing_picture_frame_01_nor_gl_1k.exr",
            ),
            _file(
                "textures/standing_picture_frame_01_rough_1k.jpg",
                93_384,
                "9e3da05a5e3dd684f808fe23945505f0623fed9983f986d7517784fc220bd001",
                "https://dl.polyhaven.org/file/ph-assets/Models/jpg/1k/"
                "standing_picture_frame_01/standing_picture_frame_01_rough_1k.jpg",
            ),
        ),
    ),
    ModelSpec(
        asset_id="polyhaven_school_desk_01",
        name="School Desk 01",
        source="polyhaven",
        source_id="SchoolDesk_01",
        source_url="https://polyhaven.com/a/SchoolDesk_01",
        license=CC0_LICENSE,
        license_url="https://polyhaven.com/license",
        attribution="Ethan Place; distributed by Poly Haven.",
        tags=("desk", "furniture", "textured"),
        main_file=Path("SchoolDesk_01_1k.fbx"),
        files=(
            _file(
                "SchoolDesk_01_1k.fbx",
                135_916,
                "2837c146dab5d8ca2a174b85e4948985319875a5fc1d7991cef99923c6182221",
                "https://dl.polyhaven.org/file/ph-assets/Models/fbx/1k/SchoolDesk_01/"
                "SchoolDesk_01_1k.fbx",
            ),
            _file(
                "textures/SchoolDesk_01_diff_1k.jpg",
                71_566,
                "4fed6d98265c6be27b9e6c9b010a5bba55a9aca4edfcc3fae9a12309ca6a5b94",
                "https://dl.polyhaven.org/file/ph-assets/Models/jpg/1k/SchoolDesk_01/"
                "SchoolDesk_01_diff_1k.jpg",
            ),
            _file(
                "textures/SchoolDesk_01_metallic_1k.exr",
                71_520,
                "0a2a17187c8f9e8d64a99c03ae86c7cefd3f4cb5a7f7fe9db228440f61e1b921",
                "https://dl.polyhaven.org/file/ph-assets/Models/exr/1k/SchoolDesk_01/"
                "SchoolDesk_01_metallic_1k.exr",
            ),
            _file(
                "textures/SchoolDesk_01_nor_gl_1k.exr",
                701_695,
                "0abfcceb28239b001e2d13bb5759188b2032cfb5b203fb0c71ee92da6749fe6c",
                "https://dl.polyhaven.org/file/ph-assets/Models/exr/1k/SchoolDesk_01/"
                "SchoolDesk_01_nor_gl_1k.exr",
            ),
            _file(
                "textures/SchoolDesk_01_roughness_1k.jpg",
                103_462,
                "4216dc514a6f3819a03b634272e6b2cd5858d4a8af7b8e9fc2993f40e9db4c05",
                "https://dl.polyhaven.org/file/ph-assets/Models/jpg/1k/SchoolDesk_01/"
                "SchoolDesk_01_roughness_1k.jpg",
            ),
        ),
    ),
    ModelSpec(
        asset_id="polyhaven_ceramic_vase_01",
        name="Ceramic Vase 01",
        source="polyhaven",
        source_id="ceramic_vase_01",
        source_url="https://polyhaven.com/a/ceramic_vase_01",
        license=CC0_LICENSE,
        license_url="https://polyhaven.com/license",
        attribution="James Ray Cock; distributed by Poly Haven.",
        tags=("decorative", "vase", "textured"),
        main_file=Path("ceramic_vase_01_1k.fbx"),
        files=(
            _file(
                "ceramic_vase_01_1k.fbx",
                284_076,
                "e55748bc144a1a1412d146ea3727a0afe3a9df513477fc07f56d662e08146d1d",
                "https://dl.polyhaven.org/file/ph-assets/Models/fbx/1k/ceramic_vase_01/"
                "ceramic_vase_01_1k.fbx",
            ),
            _file(
                "textures/ceramic_vase_01_diff_1k.jpg",
                32_351,
                "5f7d22178b74a59fe92aa0ccc958b897e2fb80df514eb28b97000097c0e71beb",
                "https://dl.polyhaven.org/file/ph-assets/Models/jpg/1k/ceramic_vase_01/"
                "ceramic_vase_01_diff_1k.jpg",
            ),
            _file(
                "textures/ceramic_vase_01_metal_1k.exr",
                4_784,
                "3dcb9dfd1c17d187b7b73382df3d1dfa7910aae8857a6c2f8869d301bb7f5a66",
                "https://dl.polyhaven.org/file/ph-assets/Models/exr/1k/ceramic_vase_01/"
                "ceramic_vase_01_metal_1k.exr",
            ),
            _file(
                "textures/ceramic_vase_01_nor_gl_1k.exr",
                296_872,
                "a249b2f7e0e4655ef957fec320db25c1755d3c29f9cc1b8237f4c599c16ab688",
                "https://dl.polyhaven.org/file/ph-assets/Models/exr/1k/ceramic_vase_01/"
                "ceramic_vase_01_nor_gl_1k.exr",
            ),
            _file(
                "textures/ceramic_vase_01_rough_1k.jpg",
                54_396,
                "2f6e631a389ef2c751e089ae033a5b8dc4d4c370afee3f0b4868cc3b8da49f6b",
                "https://dl.polyhaven.org/file/ph-assets/Models/jpg/1k/ceramic_vase_01/"
                "ceramic_vase_01_rough_1k.jpg",
            ),
        ),
    ),
    _khronos_model(
        asset_id="khronos_box",
        model="Box",
        name="Box",
        size=1_664,
        sha256="ed52f7192b8311d700ac0ce80644e3852cd01537e4d62241b9acba023da3d54e",
        attribution="Cesium; distributed by Khronos Group glTF Sample Assets.",
        tags=("geometry", "hierarchy", "primitive", "untextured"),
        license=CC_BY_4_LICENSE,
        license_url=CC_BY_4_LICENSE_URL,
    ),
)


def acquire_m2_models(
    *,
    settings: Settings,
    force: bool = False,
) -> ModelAcquireResult:
    """Acquire the fixed, open-license M2 model sample set with exact verification."""

    _validate_inventory(M2_MODEL_SPECS)
    root_dir = (settings.data_dir / M2_SAMPLE_SUBDIR).resolve()
    root_dir.mkdir(parents=True, exist_ok=True)
    acquired: list[AcquiredModel] = []
    file_results: dict[tuple[str, Path], bool] = {}
    futures: dict[Future[bool], tuple[str, Path]] = {}

    total_files = sum(len(model.files) for model in M2_MODEL_SPECS)
    executor = ThreadPoolExecutor(max_workers=min(MAX_DOWNLOAD_WORKERS, total_files))
    try:
        for model in M2_MODEL_SPECS:
            model_dir = root_dir / model.asset_id
            model_dir.mkdir(parents=True, exist_ok=True)
            for file_spec in model.files:
                key = (model.asset_id, file_spec.relative_path)
                future = executor.submit(
                    _acquire_file,
                    file_spec,
                    destination=model_dir / file_spec.relative_path,
                    force=force,
                )
                futures[future] = key
        for future in as_completed(futures):
            file_results[futures[future]] = future.result()
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)

    for model in M2_MODEL_SPECS:
        model_dir = root_dir / model.asset_id
        model_reused = sum(
            file_results[(model.asset_id, file_spec.relative_path)] for file_spec in model.files
        )
        model_downloaded = len(model.files) - model_reused

        metadata_path = model_dir / "metadata.json"
        _write_json_atomic(metadata_path, _model_metadata(model, model_dir=model_dir))
        acquired.append(
            AcquiredModel(
                asset_id=model.asset_id,
                main_path=model_dir / model.main_file,
                dependency_paths=tuple(model_dir / path for path in model.dependencies),
                metadata_path=metadata_path,
                downloaded_files=model_downloaded,
                reused_files=model_reused,
                bytes=sum(item.bytes for item in model.files),
            )
        )

    inventory_path = root_dir / "inventory.json"
    reused_files = sum(file_results.values())
    downloaded_files = total_files - reused_files
    total_bytes = sum(item.bytes for model in M2_MODEL_SPECS for item in model.files)
    _write_json_atomic(
        inventory_path,
        {
            "inventory_version": M2_INVENTORY_VERSION,
            "khronos_commit": KHRONOS_COMMIT,
            "license_policy": M2_LICENSE_POLICY,
            "models": [
                _model_metadata(model, model_dir=root_dir / model.asset_id)
                for model in M2_MODEL_SPECS
            ],
            "totals": {
                "models": len(M2_MODEL_SPECS),
                "files": sum(len(model.files) for model in M2_MODEL_SPECS),
                "bytes": total_bytes,
            },
        },
    )
    return ModelAcquireResult(
        root_dir=root_dir,
        inventory_path=inventory_path,
        models=tuple(acquired),
        downloaded_files=downloaded_files,
        reused_files=reused_files,
        bytes=total_bytes,
    )


def _acquire_file(spec: ModelFileSpec, *, destination: Path, force: bool) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise ModelAcquireError(f"model destination is not a regular file: {destination}")
        if not force:
            _verify_file(destination, spec, context="existing model file")
            return True

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".part",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    actual_size = 0
    try:
        request = urllib.request.Request(spec.url, headers={"User-Agent": USER_AGENT})
        try:
            with (
                urllib.request.urlopen(request, timeout=300) as response,
                temporary.open("wb") as file,
            ):
                while chunk := response.read(1024 * 1024):
                    file.write(chunk)
                    digest.update(chunk)
                    actual_size += len(chunk)
                    if actual_size > spec.bytes:
                        raise ModelAcquireError(
                            f"model download size mismatch: {spec.url} "
                            f"expected={spec.bytes} actual>{spec.bytes}"
                        )
        except (OSError, urllib.error.URLError) as exc:
            raise ModelAcquireError(f"cannot download model file {spec.url}: {exc}") from exc

        actual_sha256 = digest.hexdigest()
        if actual_size != spec.bytes:
            raise ModelAcquireError(
                f"model download size mismatch: {spec.url} "
                f"expected={spec.bytes} actual={actual_size}"
            )
        if actual_sha256 != spec.sha256:
            raise ModelAcquireError(
                f"model download sha256 mismatch: {spec.url} "
                f"expected={spec.sha256} actual={actual_sha256}"
            )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return False


def _verify_file(path: Path, spec: ModelFileSpec, *, context: str) -> None:
    actual_size = path.stat().st_size
    if actual_size != spec.bytes:
        raise ModelAcquireError(
            f"{context} size mismatch: {path} expected={spec.bytes} actual={actual_size}"
        )
    actual_sha256 = _sha256_file(path)
    if actual_sha256 != spec.sha256:
        raise ModelAcquireError(
            f"{context} sha256 mismatch: {path} expected={spec.sha256} actual={actual_sha256}"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _model_metadata(model: ModelSpec, *, model_dir: Path) -> dict[str, Any]:
    return {
        "asset_id": model.asset_id,
        "name": model.name,
        "source": model.source,
        "source_id": model.source_id,
        "source_url": model.source_url,
        "license": model.license,
        "license_url": model.license_url,
        "attribution": model.attribution,
        "tags": list(model.tags),
        "main_file": str(model_dir / model.main_file),
        "dependencies": [str(model_dir / path) for path in model.dependencies],
        "files": [
            {
                "path": str(model_dir / item.relative_path),
                "relative_path": item.relative_path.as_posix(),
                "url": item.url,
                "bytes": item.bytes,
                "sha256": item.sha256,
            }
            for item in model.files
        ],
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".part",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_inventory(models: tuple[ModelSpec, ...]) -> None:
    if not models:
        raise ModelAcquireError("model inventory is empty")
    seen_asset_ids: set[str] = set()
    for model in models:
        if _ASSET_ID_PATTERN.fullmatch(model.asset_id) is None:
            raise ModelAcquireError(f"invalid inventory asset_id: {model.asset_id!r}")
        if model.asset_id in seen_asset_ids:
            raise ModelAcquireError(f"duplicate inventory asset_id: {model.asset_id!r}")
        seen_asset_ids.add(model.asset_id)
        if model.license not in M2_OPEN_LICENSES:
            raise ModelAcquireError(
                f"M2 inventory asset {model.asset_id!r} does not use an approved open license: "
                f"{model.license!r}"
            )
        if not model.attribution:
            raise ModelAcquireError(f"M2 inventory asset {model.asset_id!r} has no attribution")
        file_paths: set[str] = set()
        for item in model.files:
            normalized = _normalized_relative_path(item.relative_path)
            if normalized in file_paths:
                raise ModelAcquireError(
                    f"duplicate file path in model {model.asset_id!r}: {normalized!r}"
                )
            file_paths.add(normalized)
            if item.bytes <= 0:
                raise ModelAcquireError(f"invalid expected size for {model.asset_id}/{normalized}")
            if _SHA256_PATTERN.fullmatch(item.sha256) is None:
                raise ModelAcquireError(f"invalid sha256 for {model.asset_id}/{normalized}")
            parsed = urlsplit(item.url)
            if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
                raise ModelAcquireError(f"unapproved download URL for {model.asset_id}: {item.url}")
        main_file = _normalized_relative_path(model.main_file)
        if main_file not in file_paths:
            raise ModelAcquireError(f"main file missing from model {model.asset_id!r}: {main_file}")
        if model.main_file.suffix.lower() not in {".fbx", ".glb", ".gltf"}:
            raise ModelAcquireError(
                f"unsupported main file format for model {model.asset_id!r}: {model.main_file}"
            )


def _normalized_relative_path(path: Path) -> str:
    raw = path.as_posix()
    pure = PurePosixPath(raw)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise ModelAcquireError(f"unsafe inventory relative path: {raw!r}")
    normalized = pure.as_posix()
    if normalized != raw:
        raise ModelAcquireError(f"non-normalized inventory relative path: {raw!r}")
    return normalized
