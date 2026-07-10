from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from uefactory.catalog import ArtifactUpsert, AssetUpsert, Catalog
from uefactory.ingest.batch_report import (
    BatchReportAsset,
    BatchReportError,
    create_batch_report,
)

_KINDS = (
    "thumbnail_beauty",
    "thumbnail_mask",
    "thumbnail_mask_raw",
    "thumbnail_render_manifest",
    "thumbnail_contact_sheet",
)


def _thumbnail_validation_fixture() -> dict[str, object]:
    return {
        "rule_version": "catalog_thumbnail_visual_v1",
        "max_background_contamination_ratio": 0.001,
        "min_subject_max_area_ratio": 0.02,
        "min_subject_median_area_ratio": 0.01,
        "selected_view_index": 0,
        "subject_area": {"minimum": 0.25, "median": 0.25, "maximum": 0.25},
        "frames": [
            {
                "frame": f"frame_{index:04d}.png",
                "safe_background_pixels": 48,
                "contaminated_pixels": 0,
                "contamination_ratio": 0.0,
                "total_pixels": 64,
                "subject_pixels": 16,
                "subject_area_ratio": 0.25,
            }
            for index in range(8)
        ],
        "status": "passed",
    }


def _scene_sanitization_fixture() -> dict[str, object]:
    return {
        "policy": "catalog_hide_all_pawns_v2",
        "subjobs": [
            {
                "subjob_index": index,
                "hidden_pawn_count": 1,
                "editor_hidden_pawn_count": 1,
                "hidden_static_meshes": ["/Engine/EngineMeshes/Sphere.Sphere"],
            }
            for index in range(2)
        ],
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _catalog_fixture(
    project_root: Path,
    *,
    count: int,
) -> tuple[Catalog, tuple[BatchReportAsset, ...], dict[str, Path]]:
    catalog = Catalog(project_root / "data/catalog.db", project_root=project_root)
    requests: list[BatchReportAsset] = []
    beauty_paths: dict[str, Path] = {}
    for index in range(count):
        asset_id = f"asset_{index:02d}"
        content_sha256 = f"{index + 1:064x}"
        bundle_sha256 = f"{index + 101:064x}"
        requested_normalization: dict[str, str | float] = {
            "source_units": "auto",
            "source_up_axis": "auto",
            "source_handedness": "auto",
            "uniform_scale": 1.0,
            "pivot_policy": "preserve_source",
        }
        import_manifest = f"out/ingest/{asset_id}/manifest.json"
        run_dir = project_root / "out/thumbnails" / asset_id
        run_dir.mkdir(parents=True)
        paths = {
            "thumbnail_beauty": run_dir / "thumbnail.png",
            "thumbnail_mask": run_dir / "subject_mask.png",
            "thumbnail_mask_raw": run_dir / "object_mask.exr",
            "thumbnail_render_manifest": run_dir / "manifest.json",
            "thumbnail_contact_sheet": run_dir / "contact_sheet.png",
        }
        artifact_ids = {kind: f"{asset_id}_{kind}_{index}" for kind in paths}
        color = (30 + index * 10, 70 + index * 5, 140 + index * 3)
        Image.new("RGB", (48 + index, 32 + index), color).save(paths["thumbnail_beauty"])
        Image.new("L", (16, 16), 255).save(paths["thumbnail_mask"])
        paths["thumbnail_mask_raw"].write_bytes(f"exr fixture {index}".encode())
        paths["thumbnail_render_manifest"].write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "status": "ok",
                    "asset_id": asset_id,
                    "asset": {
                        "kind": "catalog",
                        "asset_id": asset_id,
                        "bundle_sha256": bundle_sha256,
                        "content_sha256": content_sha256,
                        "import_manifest": import_manifest,
                        "normalization": {"request": requested_normalization},
                    },
                    "job": {
                        "assets": [asset_id],
                        "camera": {
                            "rig": "orbit",
                            "views": 8,
                            "elevation_deg": 20,
                            "fov": 45,
                            "resolution": [512, 512],
                        },
                        "lighting": {"preset": "three_point"},
                        "passes": ["beauty_lit", "object_mask"],
                    },
                    "thumbnail_validation": _thumbnail_validation_fixture(),
                    "scene_sanitization": _scene_sanitization_fixture(),
                    "catalog_commit": {
                        "asset_id": asset_id,
                        "target_status": "render_ok",
                        "bundle_sha256": bundle_sha256,
                        "thumbnail_preset": "catalog_thumbnail_v1",
                        "selected_view_index": 0,
                        "requested_normalization": requested_normalization,
                        "import_manifest": import_manifest,
                        "artifact_ids": list(artifact_ids.values()),
                    },
                }
            ),
            encoding="utf-8",
        )
        Image.new("RGB", (16, 16), color).save(paths["thumbnail_contact_sheet"])
        beauty_paths[asset_id] = paths["thumbnail_beauty"]

        asset = AssetUpsert(
            asset_id=asset_id,
            name="Chair <Prototype>" if index == 0 else f"Fixture Asset {index:02d}",
            source="fixture",
            source_id=f"fixture-{index}",
            source_url=f"https://example.test/assets/{asset_id}",
            license="CC0-1.0",
            license_tier="open",
            license_url="https://creativecommons.org/publicdomain/zero/1.0/",
            attribution="Fixture author",
            status="render_ok",
            tags=("fixture",),
            raw_path=f"data/raw/{asset_id}.glb",
            ue_package_path=f"/Game/UEF/Ingested/{asset_id}/SM_{asset_id}",
            tri_count=100 + index,
            material_count=1 + index % 3,
            sha256=content_sha256,
        )
        relative_manifest = paths["thumbnail_render_manifest"].relative_to(project_root).as_posix()
        artifacts = tuple(
            ArtifactUpsert(
                artifact_id=artifact_ids[kind],
                asset_id=asset_id,
                kind=kind,
                path=paths[kind],
                params={
                    "schema_version": 1,
                    "thumbnail_preset": "catalog_thumbnail_v1",
                    "render_manifest": relative_manifest,
                    "views": 8,
                    "resolution": [512, 512],
                    "lighting": "three_point",
                    "subject_stencil_id": 1,
                    "selected_view_index": 0,
                    "bundle_sha256": bundle_sha256,
                    "requested_normalization": requested_normalization,
                    "import_manifest": import_manifest,
                },
                sha256=_sha256(paths[kind]),
            )
            for kind in _KINDS
        )
        catalog.finalize_render(asset, artifacts)
        requests.append(
            BatchReportAsset(
                asset_id=asset_id,
                batch_status="render_ok" if index % 2 == 0 else "skipped",
                catalog_status="render_ok",
                bundle_sha256=bundle_sha256,
                content_sha256=content_sha256,
                requested_normalization=requested_normalization,
            )
        )
    return catalog, tuple(requests), beauty_paths


def test_create_batch_report_writes_ten_asset_sheet_and_offline_index(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    run_dir = project_root / "out/ingest_batches/test_run"
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    catalog, assets, _ = _catalog_fixture(project_root, count=10)

    report = create_batch_report(
        project_root=project_root,
        run_dir=run_dir,
        manifest_path=manifest_path,
        catalog=catalog,
        assets=assets,
    )

    assert report.contact_sheet == run_dir / "report/contact_sheet.png"
    assert report.index_html == run_dir / "report/index.html"
    assert len(report.thumbnails) == 10
    assert [item.asset_id for item in report.thumbnails] == [
        f"asset_{index:02d}" for index in range(10)
    ]
    with Image.open(report.contact_sheet) as contact_sheet:
        contact_sheet.load()
        assert contact_sheet.mode == "RGB"
        assert contact_sheet.size == (1200, 602)
    assert all(item.path.is_file() for item in report.thumbnails)
    assert all(_sha256(item.path) == item.sha256 for item in report.thumbnails)
    assert all(item.asset_sheet_path.is_file() for item in report.thumbnails)
    assert all(
        _sha256(item.asset_sheet_path) == item.asset_sheet_sha256 for item in report.thumbnails
    )

    html = report.index_html.read_text(encoding="utf-8")
    assert "10 thumbnail-complete assets" in html
    assert "Chair &lt;Prototype&gt;" in html
    assert "Chair <Prototype>" not in html
    assert "render_ok" in html
    assert "skipped" in html
    assert assets[0].content_sha256 in html
    assert "100 triangles" in html
    assert 'src="thumbnails/asset_00.png"' in html
    assert 'href="asset_sheets/asset_00.png"' in html
    assert "8-view beauty + mask sheet" in html
    assert 'href="../manifest.json"' in html
    assert "http://" not in html
    assert "https://" not in html
    assert str(project_root) not in html

    payload = report.manifest_payload(project_root=project_root)
    assert payload["contact_sheet"] == ("out/ingest_batches/test_run/report/contact_sheet.png")
    assert payload["index_html"] == "out/ingest_batches/test_run/report/index.html"
    thumbnails = payload["thumbnails"]
    assert isinstance(thumbnails, list)
    assert len(thumbnails) == 10
    assert thumbnails[0]["path"] == ("out/ingest_batches/test_run/report/thumbnails/asset_00.png")
    assert thumbnails[0]["asset_sheet_path"] == (
        "out/ingest_batches/test_run/report/asset_sheets/asset_00.png"
    )
    assert not list(run_dir.glob(".batch-report-*"))


def test_create_batch_report_rejects_thumbnail_from_different_bundle(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    run_dir = project_root / "out/ingest_batches/bundle_mismatch"
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    catalog, assets, _ = _catalog_fixture(project_root, count=1)
    mismatched = (replace(assets[0], bundle_sha256="f" * 64),)

    with pytest.raises(BatchReportError, match="no complete hash-valid thumbnail"):
        create_batch_report(
            project_root=project_root,
            run_dir=run_dir,
            manifest_path=manifest_path,
            catalog=catalog,
            assets=mismatched,
        )


def test_create_batch_report_rejects_incomplete_or_corrupt_thumbnail_group(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    run_dir = project_root / "out/ingest_batches/test_run"
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    catalog, assets, beauty_paths = _catalog_fixture(project_root, count=1)
    beauty_paths["asset_00"].write_bytes(b"tampered")

    with pytest.raises(BatchReportError, match="no complete hash-valid thumbnail artifact group"):
        create_batch_report(
            project_root=project_root,
            run_dir=run_dir,
            manifest_path=manifest_path,
            catalog=catalog,
            assets=assets,
        )

    assert not (run_dir / "report").exists()
    assert not list(run_dir.glob(".batch-report-*"))
