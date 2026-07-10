from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from uefactory.acquire import models
from uefactory.acquire.models import ModelAcquireError, ModelFileSpec, ModelSpec
from uefactory.core.config import Settings


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _spec(*, main_payload: bytes, dependency_payload: bytes | None = None) -> ModelSpec:
    base_url = "https://raw.githubusercontent.com/example/project/commit"
    files = [
        ModelFileSpec(
            relative_path=Path("fixture.glb"),
            url=f"{base_url}/fixture.glb",
            bytes=len(main_payload),
            sha256=_sha256(main_payload),
        )
    ]
    if dependency_payload is not None:
        files.append(
            ModelFileSpec(
                relative_path=Path("textures/fixture.png"),
                url=f"{base_url}/textures/fixture.png",
                bytes=len(dependency_payload),
                sha256=_sha256(dependency_payload),
            )
        )
    return ModelSpec(
        asset_id="fixture_model",
        name="Fixture Model",
        source="khronos",
        source_id="FixtureModel",
        source_url="https://github.com/example/project/tree/commit/FixtureModel",
        license="CC0-1.0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        attribution="Fixture Author",
        tags=("fixture",),
        main_file=Path("fixture.glb"),
        files=tuple(files),
    )


def _responses(monkeypatch: pytest.MonkeyPatch, payloads: dict[str, bytes]) -> list[str]:
    requested: list[str] = []

    def fake_urlopen(request: Any, timeout: int) -> _Response:
        assert timeout == 300
        requested.append(request.full_url)
        return _Response(payloads[request.full_url])

    monkeypatch.setattr(models.urllib.request, "urlopen", fake_urlopen)
    return requested


def test_pinned_inventory_has_eleven_open_models_and_required_format_mix() -> None:
    inventory = models.M2_MODEL_SPECS

    models._validate_inventory(inventory)

    assert len(inventory) == 11
    assert len({item.asset_id for item in inventory}) == 11
    assert {item.source for item in inventory} == {"khronos", "polyhaven"}
    assert sum(item.main_file.suffix == ".glb" for item in inventory) == 6
    assert sum(item.main_file.suffix == ".fbx" for item in inventory) == 5
    assert {item.license for item in inventory} == {"CC0-1.0", "CC-BY-4.0"}
    assert all(item.attribution for item in inventory)
    assert all(item.source_url.startswith("https://") for item in inventory)
    assert sum(file.bytes for item in inventory for file in item.files) < 70_000_000

    box = next(item for item in inventory if item.asset_id == "khronos_box")
    assert box.source_id == "Box"
    assert box.main_file == Path("Box.glb")
    assert box.dependencies == ()
    assert box.license == "CC-BY-4.0"
    assert box.license_url == "https://creativecommons.org/licenses/by/4.0/legalcode"
    assert box.attribution == "Cesium; distributed by Khronos Group glTF Sample Assets."
    assert "untextured" in box.tags
    assert "textured" not in box.tags
    assert box.files == (
        ModelFileSpec(
            relative_path=Path("Box.glb"),
            url=(
                "https://raw.githubusercontent.com/KhronosGroup/glTF-Sample-Assets/"
                "2bac6f8c57bf471df0d2a1e8a8ec023c7801dddf/Models/Box/glTF-Binary/Box.glb"
            ),
            bytes=1_664,
            sha256="ed52f7192b8311d700ac0ce80644e3852cd01537e4d62241b9acba023da3d54e",
        ),
    )


def test_example_manifest_exactly_matches_pinned_inventory() -> None:
    manifest_path = Path(__file__).parents[1] / "examples/m2_assets.yaml"
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest_assets = {item["asset_id"]: item for item in raw["assets"]}

    assert tuple(manifest_assets) == tuple(item.asset_id for item in models.M2_MODEL_SPECS)
    for spec in models.M2_MODEL_SPECS:
        item = manifest_assets[spec.asset_id]
        expected_prefix = f"../data/m2_samples/{spec.asset_id}/"
        assert item["path"] == expected_prefix + spec.main_file.as_posix()
        assert item["dependencies"] == [path.as_posix() for path in spec.dependencies]
        assert item["source"] == spec.source
        assert item["source_id"] == spec.source_id
        assert item["source_url"] == spec.source_url
        assert item["license"] == spec.license
        assert item["license_tier"] == "open"
        assert item["license_url"] == spec.license_url
        assert item["attribution"] == spec.attribution
        assert item["tags"] == list(spec.tags)


def test_inventory_license_allowlist_accepts_box_and_rejects_unapproved_license() -> None:
    box = next(item for item in models.M2_MODEL_SPECS if item.asset_id == "khronos_box")

    models._validate_inventory((box,))

    with pytest.raises(ModelAcquireError, match="does not use an approved open license"):
        models._validate_inventory((replace(box, license="LicenseRef-Proprietary"),))


def test_acquire_models_downloads_checks_and_writes_metadata_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main_payload = b"fixture-glb"
    dependency_payload = b"fixture-texture"
    spec = _spec(main_payload=main_payload, dependency_payload=dependency_payload)
    monkeypatch.setattr(models, "M2_MODEL_SPECS", (spec,))
    requested = _responses(
        monkeypatch,
        {
            spec.files[0].url: main_payload,
            spec.files[1].url: dependency_payload,
        },
    )
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data")

    result = models.acquire_m2_models(settings=settings)

    assert set(requested) == {item.url for item in spec.files}
    assert result.downloaded_files == 2
    assert result.reused_files == 0
    assert result.bytes == len(main_payload) + len(dependency_payload)
    acquired = result.models[0]
    assert acquired.main_path.read_bytes() == main_payload
    assert acquired.dependency_paths[0].read_bytes() == dependency_payload
    metadata = json.loads(acquired.metadata_path.read_text(encoding="utf-8"))
    assert metadata["license"] == "CC0-1.0"
    assert metadata["source"] == "khronos"
    assert metadata["attribution"] == "Fixture Author"
    assert metadata["files"][0]["sha256"] == _sha256(main_payload)
    inventory = json.loads(result.inventory_path.read_text(encoding="utf-8"))
    assert inventory["license_policy"] == models.M2_LICENSE_POLICY
    assert inventory["totals"] == {
        "models": 1,
        "files": 2,
        "bytes": len(main_payload) + len(dependency_payload),
    }
    assert not list(result.root_dir.rglob("*.part"))


def test_acquire_models_reuses_only_exact_existing_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"fixture-glb"
    spec = _spec(main_payload=payload)
    monkeypatch.setattr(models, "M2_MODEL_SPECS", (spec,))
    _responses(monkeypatch, {spec.files[0].url: payload})
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data")
    first = models.acquire_m2_models(settings=settings)

    def unexpected_urlopen(request: Any, timeout: int) -> None:
        raise AssertionError(f"unexpected network call: {request.full_url}")

    monkeypatch.setattr(models.urllib.request, "urlopen", unexpected_urlopen)
    second = models.acquire_m2_models(settings=settings)

    assert first.downloaded_files == 1
    assert second.downloaded_files == 0
    assert second.reused_files == 1


def test_acquire_models_rejects_corrupt_existing_file_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"expected"
    corrupt = b"corrupt!"
    spec = _spec(main_payload=expected)
    monkeypatch.setattr(models, "M2_MODEL_SPECS", (spec,))
    destination = tmp_path / "data/m2_samples/fixture_model/fixture.glb"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(corrupt)

    def unexpected_urlopen(request: Any, timeout: int) -> None:
        raise AssertionError(f"unexpected network call: {request.full_url}")

    monkeypatch.setattr(models.urllib.request, "urlopen", unexpected_urlopen)

    with pytest.raises(ModelAcquireError, match="existing model file sha256 mismatch"):
        models.acquire_m2_models(
            settings=Settings(project_root=tmp_path, data_dir=tmp_path / "data")
        )

    assert destination.read_bytes() == corrupt


def test_acquire_models_removes_bad_download_and_preserves_existing_on_force(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"expected"
    existing = b"existing"
    spec = _spec(main_payload=expected)
    monkeypatch.setattr(models, "M2_MODEL_SPECS", (spec,))
    _responses(monkeypatch, {spec.files[0].url: b"bad-data"})
    destination = tmp_path / "data/m2_samples/fixture_model/fixture.glb"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(existing)

    with pytest.raises(ModelAcquireError, match="model download sha256 mismatch"):
        models.acquire_m2_models(
            settings=Settings(project_root=tmp_path, data_dir=tmp_path / "data"),
            force=True,
        )

    assert destination.read_bytes() == existing
    assert not list(destination.parent.glob("*.part"))


def test_acquire_models_rejects_wrong_download_size_and_removes_partial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"expected"
    spec = _spec(main_payload=expected)
    monkeypatch.setattr(models, "M2_MODEL_SPECS", (spec,))
    _responses(monkeypatch, {spec.files[0].url: b"too-short"})
    destination = tmp_path / "data/m2_samples/fixture_model/fixture.glb"

    with pytest.raises(ModelAcquireError, match="model download size mismatch"):
        models.acquire_m2_models(
            settings=Settings(project_root=tmp_path, data_dir=tmp_path / "data")
        )

    assert not destination.exists()
    assert not list(destination.parent.glob("*.part"))


def test_acquire_models_force_replaces_only_after_verified_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = b"expected"
    spec = _spec(main_payload=expected)
    monkeypatch.setattr(models, "M2_MODEL_SPECS", (spec,))
    _responses(monkeypatch, {spec.files[0].url: expected})
    destination = tmp_path / "data/m2_samples/fixture_model/fixture.glb"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old-data")

    result = models.acquire_m2_models(
        settings=Settings(project_root=tmp_path, data_dir=tmp_path / "data"),
        force=True,
    )

    assert result.downloaded_files == 1
    assert destination.read_bytes() == expected


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
