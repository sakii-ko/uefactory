from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from PIL import Image

from uefactory.catalog import AssetUpsert, Catalog
from uefactory.core.config import Settings
from uefactory.render.jobspec import load_jobspec
from uefactory.render.thumbnails import (
    _create_subject_mask_png,
    _validate_black_background_consistency,
    is_valid_thumbnail_validation,
    thumbnail_catalog_asset,
)


def _settings(tmp_path: Path) -> Settings:
    project_root = tmp_path / "project"
    project_root.mkdir()
    return Settings(
        project_root=project_root,
        data_dir=project_root / "data",
        log_dir=project_root / "logs",
    )


def _imported_asset() -> AssetUpsert:
    return AssetUpsert(
        asset_id="test_asset",
        name="Test Asset",
        source="local",
        source_id="test-asset",
        source_url="https://example.test/test-asset",
        license="CC0-1.0",
        license_tier="open",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        raw_path="data/raw/test_asset/model.glb",
        sha256="a" * 64,
        status="imported",
        ue_package_path="/Game/UEF/Ingested/test_asset/SM_Test.SM_Test",
        tri_count=12,
        material_count=1,
    )


def test_create_subject_mask_png_extracts_only_stencil_one(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    pixels = np.zeros((4, 5, 4), dtype=np.float16)
    pixels[1:3, 2:4, :3] = np.float16(1.0 / 255.0)
    pixels[:, :, 3] = np.float16(1.0)
    monkeypatch.setattr(
        "uefactory.render.thumbnails._read_half_rgba_exr",
        lambda pass_name, path: (pixels, (5, 4)),
    )
    output = tmp_path / "mask.png"

    _create_subject_mask_png(tmp_path / "mask.exr", output)

    with Image.open(output) as image:
        assert image.mode == "L"
        assert image.size == (5, 4)
        assert image.getextrema() == (0, 255)


def test_create_subject_mask_png_extracts_scene_stencil_union(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    pixels = np.zeros((8, 8, 4), dtype=np.float16)
    pixels[1:4, 1:4, :3] = np.float16(1.0 / 255.0)
    pixels[4:7, 4:7, :3] = np.float16(0.07056)
    pixels[:, :, 3] = np.float16(1.0)
    monkeypatch.setattr(
        "uefactory.render.thumbnails._read_half_rgba_exr",
        lambda pass_name, path: (pixels, (8, 8)),
    )
    output = tmp_path / "mask.png"

    _create_subject_mask_png(
        tmp_path / "mask.exr",
        output,
        subject_stencil_ids=(1, 18),
    )

    with Image.open(output) as image:
        assert np.count_nonzero(np.asarray(image)) == 18


def test_thumbnail_catalog_asset_commits_render_and_artifacts_atomically(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    catalog = Catalog(settings.data_dir / "catalog.db", project_root=settings.project_root)
    catalog.upsert_asset(_imported_asset())
    render_dir = settings.project_root / "out/thumbnails/run/test_asset"
    beauty = render_dir / "beauty_lit/frame_0000.png"
    mask = render_dir / "object_mask/frame_0000.exr"
    beauty.parent.mkdir(parents=True)
    mask.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8), (20, 160, 60)).save(beauty)
    mask.write_bytes(b"raw mask fixture")
    render_manifest = render_dir / "manifest.json"
    requested_normalization = {
        "source_units": "auto",
        "source_up_axis": "auto",
        "source_handedness": "auto",
        "uniform_scale": 1.0,
        "pivot_policy": "preserve_source",
    }
    render_manifest.write_text(
        json.dumps(
            {
                "status": "ok",
                "asset": {
                    "kind": "catalog",
                    "asset_id": "test_asset",
                    "mesh_path": "/Game/UEF/Ingested/test_asset/SM_Test.SM_Test",
                    "import_manifest": "out/ingest/test_asset/manifest.json",
                    "bundle_sha256": "b" * 64,
                    "content_sha256": "a" * 64,
                    "ue_package_bundle_sha256": "c" * 64,
                    "normalization": {"request": requested_normalization},
                },
            }
        ),
        encoding="utf-8",
    )
    contact_sheet = render_dir / "contact_sheet.png"
    Image.new("RGB", (8, 8), (10, 10, 10)).save(contact_sheet)
    fake_result = SimpleNamespace(
        run_dir=render_dir,
        manifest_path=render_manifest,
        frame_paths={"beauty_lit": [beauty], "object_mask": [mask]},
        artifacts=SimpleNamespace(contact_sheet=contact_sheet),
    )
    calls: list[dict[str, Any]] = []

    def fake_render_job(**kwargs: Any) -> Any:
        calls.append(kwargs)
        fake_result.spec = load_jobspec(kwargs["job_path"])
        return fake_result

    def fake_mask(mask_path: Path, output_path: Path) -> None:
        assert mask_path == mask
        Image.new("L", (8, 8), 255).save(output_path)

    monkeypatch.setattr("uefactory.render.thumbnails.render_job", fake_render_job)
    monkeypatch.setattr(
        "uefactory.render.thumbnails.resolve_render_asset",
        lambda settings, spec, database_path: json.loads(
            render_manifest.read_text(encoding="utf-8")
        )["asset"],
    )
    monkeypatch.setattr("uefactory.render.thumbnails._create_subject_mask_png", fake_mask)
    monkeypatch.setattr(
        "uefactory.render.thumbnails._validate_black_background_consistency",
        lambda beauty_frames, mask_frames: [
            {
                "frame": beauty_frames[0].name,
                "safe_background_pixels": 1,
                "contaminated_pixels": 0,
                "contamination_ratio": 0.0,
                "total_pixels": 64,
                "subject_pixels": 64,
                "subject_area_ratio": 1.0,
            }
        ],
    )

    result = thumbnail_catalog_asset(settings=settings, asset_id="test_asset")

    assert result.thumbnail_path.is_file()
    assert result.subject_mask_path.is_file()
    assert calls[0]["database_path"] == settings.data_dir / "catalog.db"
    record = catalog.get_asset("test_asset")
    assert record is not None and record.status == "render_ok"
    artifacts = catalog.list_artifacts(asset_id="test_asset")
    assert {item.kind for item in artifacts} == {
        "thumbnail_beauty",
        "thumbnail_mask",
        "thumbnail_mask_raw",
        "thumbnail_render_manifest",
        "thumbnail_contact_sheet",
    }
    manifest = json.loads(render_manifest.read_text(encoding="utf-8"))
    assert manifest["catalog_commit"]["target_status"] == "render_ok"
    assert manifest["catalog_commit"]["ue_package_bundle_sha256"] == "c" * 64
    assert set(manifest["catalog_commit"]["artifact_ids"]) == {
        item.artifact_id for item in artifacts
    }
    assert manifest["thumbnail_validation"]["status"] == "passed"
    assert is_valid_thumbnail_validation(manifest["thumbnail_validation"], expected_frames=1)
    assert all(
        item.params["requested_normalization"] == requested_normalization for item in artifacts
    )
    assert all(item.params["bundle_sha256"] == "b" * 64 for item in artifacts)
    assert all(item.params["ue_package_bundle_sha256"] == "c" * 64 for item in artifacts)


def test_thumbnail_rejects_generation_change_without_catalog_commit(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    catalog = Catalog(settings.data_dir / "catalog.db", project_root=settings.project_root)
    catalog.upsert_asset(_imported_asset())
    render_dir = settings.project_root / "out/thumbnails/run/test_asset"
    beauty = render_dir / "beauty_lit/frame_0000.png"
    mask = render_dir / "object_mask/frame_0000.exr"
    beauty.parent.mkdir(parents=True)
    mask.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8), (20, 160, 60)).save(beauty)
    mask.write_bytes(b"raw mask fixture")
    requested_normalization = {
        "source_units": "auto",
        "source_up_axis": "auto",
        "source_handedness": "auto",
        "uniform_scale": 1.0,
        "pivot_policy": "preserve_source",
    }
    rendered_asset = {
        "kind": "catalog",
        "asset_id": "test_asset",
        "mesh_path": "/Game/UEF/Ingested/test_asset/SM_Test.SM_Test",
        "import_manifest": "out/ingest/old/manifest.json",
        "bundle_sha256": "b" * 64,
        "content_sha256": "a" * 64,
        "ue_package_bundle_sha256": "c" * 64,
        "normalization": {"request": requested_normalization},
    }
    render_manifest = render_dir / "manifest.json"
    render_manifest.write_text(
        json.dumps({"status": "ok", "asset": rendered_asset}),
        encoding="utf-8",
    )
    contact_sheet = render_dir / "contact_sheet.png"
    Image.new("RGB", (8, 8), (10, 10, 10)).save(contact_sheet)
    fake_result = SimpleNamespace(
        run_dir=render_dir,
        manifest_path=render_manifest,
        frame_paths={"beauty_lit": [beauty], "object_mask": [mask]},
        artifacts=SimpleNamespace(contact_sheet=contact_sheet),
    )

    def fake_render_job(**kwargs: Any) -> Any:
        fake_result.spec = load_jobspec(kwargs["job_path"])
        return fake_result

    monkeypatch.setattr("uefactory.render.thumbnails.render_job", fake_render_job)
    monkeypatch.setattr(
        "uefactory.render.thumbnails.resolve_render_asset",
        lambda settings, spec, database_path: {
            **rendered_asset,
            "import_manifest": "out/ingest/new/manifest.json",
            "ue_package_bundle_sha256": "d" * 64,
        },
    )
    monkeypatch.setattr(
        "uefactory.render.thumbnails._create_subject_mask_png",
        lambda mask_path, output_path: Image.new("L", (8, 8), 255).save(output_path),
    )
    monkeypatch.setattr(
        "uefactory.render.thumbnails._validate_black_background_consistency",
        lambda beauty_frames, mask_frames: [
            {
                "frame": beauty_frames[0].name,
                "safe_background_pixels": 1,
                "contaminated_pixels": 0,
                "contamination_ratio": 0.0,
                "total_pixels": 64,
                "subject_pixels": 64,
                "subject_area_ratio": 1.0,
            }
        ],
    )

    with pytest.raises(RuntimeError, match="generation changed before catalog commit"):
        thumbnail_catalog_asset(settings=settings, asset_id="test_asset")

    record = catalog.get_asset("test_asset")
    assert record is not None and record.status == "imported"
    assert catalog.list_artifacts(asset_id="test_asset") == ()
    failed_manifest = json.loads(render_manifest.read_text(encoding="utf-8"))
    assert failed_manifest["catalog_commit"]["status"] == "failed"
    assert "import_manifest" in failed_manifest["catalog_commit"]["error"]
    assert "ue_package_bundle_sha256" in failed_manifest["catalog_commit"]["error"]


def test_black_background_consistency_rejects_non_stenciled_foreground(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    beauty = tmp_path / "beauty.png"
    mask = tmp_path / "mask.exr"
    mask.write_bytes(b"mask fixture")
    stencil = np.zeros((16, 16, 4), dtype=np.float16)
    stencil[6:10, 6:10, 0] = np.float16(1.0 / 255.0)
    stencil[12:, :, 0] = np.float16(2.0 / 255.0)
    monkeypatch.setattr(
        "uefactory.render.thumbnails._read_half_rgba_exr",
        lambda pass_name, path: (stencil, (16, 16)),
    )

    clean = np.zeros((16, 16, 3), dtype=np.uint8)
    clean[6:10, 6:10] = (120, 80, 40)
    clean[12:] = (100, 100, 100)
    Image.fromarray(clean, mode="RGB").save(beauty)
    metrics = _validate_black_background_consistency([beauty], [mask])
    assert metrics[0]["contaminated_pixels"] == 0

    contaminated = clean.copy()
    contaminated[1, 1] = (255, 255, 255)
    Image.fromarray(contaminated, mode="RGB").save(beauty)
    with pytest.raises(RuntimeError, match="non-stenciled foreground contamination"):
        _validate_black_background_consistency([beauty], [mask])

    metrics = _validate_black_background_consistency(
        [beauty],
        [mask],
        maximum_contamination_ratio=0.008,
    )
    assert metrics[0]["contaminated_pixels"] == 1


def test_thumbnail_consistency_rejects_tiny_subject(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    beauty = tmp_path / "beauty.png"
    mask = tmp_path / "mask.exr"
    mask.write_bytes(b"mask fixture")
    stencil = np.zeros((64, 64, 4), dtype=np.float16)
    stencil[31:33, 31:33, 0] = np.float16(1.0 / 255.0)
    monkeypatch.setattr(
        "uefactory.render.thumbnails._read_half_rgba_exr",
        lambda pass_name, path: (stencil, (64, 64)),
    )
    pixels = np.zeros((64, 64, 3), dtype=np.uint8)
    pixels[31:33, 31:33] = (180, 120, 60)
    Image.fromarray(pixels, mode="RGB").save(beauty)

    with pytest.raises(RuntimeError, match="occupies too little"):
        _validate_black_background_consistency([beauty], [mask])


def test_thumbnail_consistency_counts_scene_stencil_union_as_subject(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    beauty = tmp_path / "beauty.png"
    mask = tmp_path / "mask.exr"
    mask.write_bytes(b"mask fixture")
    stencil = np.zeros((16, 16, 4), dtype=np.float16)
    stencil[4:8, 4:8, 0] = np.float16(1.0 / 255.0)
    stencil[8:12, 8:12, 0] = np.float16(2.0 / 255.0)
    monkeypatch.setattr(
        "uefactory.render.thumbnails._read_half_rgba_exr",
        lambda pass_name, path: (stencil, (16, 16)),
    )
    pixels = np.zeros((16, 16, 3), dtype=np.uint8)
    pixels[4:8, 4:8] = (180, 120, 60)
    pixels[8:12, 8:12] = (60, 160, 120)
    Image.fromarray(pixels, mode="RGB").save(beauty)

    metrics = _validate_black_background_consistency(
        [beauty],
        [mask],
        subject_stencil_ids=(1, 2),
    )

    assert metrics[0]["subject_pixels"] == 32
    assert metrics[0]["subject_area_ratio"] == 0.125
