from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from uefactory.catalog import ArtifactRecord, ArtifactUpsert, AssetUpsert, Catalog
from uefactory.core.config import Settings
from uefactory.core.ingest_contracts import (
    FBX_MATERIAL_POSTPROCESS_POLICY,
    IMPORT_ARTIFACT_SCHEMA_VERSION,
    IMPORT_MANIFEST_SCHEMA_VERSION,
    static_mesh_quality_policy,
)
from uefactory.core.paths import utc_timestamp
from uefactory.ingest.batch_report import (
    BatchReportArtifacts,
    BatchReportAsset,
    create_batch_report,
)
from uefactory.ingest.executor import IngestExecutionError, IngestResult, ingest_asset
from uefactory.ingest.package_evidence import is_valid_package_bundle_evidence
from uefactory.ingest.quality import (
    QUALITY_RULESET_VERSION,
    IngestQualityError,
    is_current_passed_quality,
)
from uefactory.ingest.source_structure import is_valid_source_structure_evidence
from uefactory.ingest.spec import IngestAssetSpec, load_ingest_spec
from uefactory.ingest.staging import StagedAsset, stage_asset
from uefactory.render.thumbnails import (
    THUMBNAIL_PRESET,
    is_valid_catalog_scene_sanitization,
    is_valid_thumbnail_validation,
    thumbnail_catalog_asset,
)

_ENGINE_NORMALIZATION = {
    "target_units": "centimeters",
    "target_up_axis": "Z",
    "target_handedness": "left_handed",
    "source_conversion": "delegated_to_engine_importer",
    "package_pivot_policy": "preserve",
    "uniform_scale": 1.0,
}


@dataclass(frozen=True)
class BatchAssetResult:
    asset_id: str
    status: str
    bundle_sha256: str | None
    content_sha256: str | None
    raw_path: Path | None
    ingest_manifest: Path | None
    thumbnail_manifest: Path | None = None
    catalog_status: str | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class BatchIngestResult:
    status: str
    run_dir: Path
    manifest_path: Path
    catalog_path: Path
    assets: tuple[BatchAssetResult, ...]
    report: BatchReportArtifacts | None = None
    report_error: dict[str, str] | None = None


def ingest_batch(
    *,
    settings: Settings,
    manifest_path: Path,
    database_path: Path | None = None,
    timeout_sec: int = 1800,
    render_thumbnails: bool = False,
) -> BatchIngestResult:
    try:
        settings.data_dir.resolve().relative_to(settings.project_root.resolve())
    except ValueError as exc:
        raise ValueError(
            "ingest requires data_dir to be inside project_root so catalog paths stay portable: "
            f"data_dir={settings.data_dir.resolve()}, "
            f"project_root={settings.project_root.resolve()}"
        ) from exc
    spec = load_ingest_spec(manifest_path)
    resolved_database = database_path or settings.data_dir / "catalog.db"
    if not resolved_database.is_absolute():
        resolved_database = settings.project_root / resolved_database
    catalog = Catalog(
        resolved_database,
        project_root=settings.project_root,
    )
    catalog.initialize()
    run_id = f"{utc_timestamp()}_{uuid4().hex[:8]}"
    run_dir = settings.project_root / "out/ingest_batches" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    result_manifest = run_dir / "manifest.json"
    results: list[BatchAssetResult] = []

    for asset in spec.assets:
        staged: StagedAsset | None = None
        ingest_result: IngestResult | None = None
        try:
            staged = stage_asset(asset, raw_root=settings.data_dir / "raw/local")
            require_texture_references = "textured" in asset.tags
            quality_policy = static_mesh_quality_policy(
                require_single_static_mesh=True,
                require_texture_references=require_texture_references,
            )
            existing = catalog.get_asset(asset.asset_id)
            active_ue_package_bundle_sha256 = (
                _valid_import_package_bundle_sha256(
                    catalog=catalog,
                    asset_id=asset.asset_id,
                    expected_bundle_sha256=staged.bundle_sha256,
                    expected_content_sha256=staged.content_sha256,
                    expected_normalization=asset.normalization.as_dict(),
                    expected_source_format=asset.format,
                    expected_source_structure=staged.source_structure,
                    expected_source_structure_sha256=staged.source_structure_sha256,
                    expected_quality_policy=quality_policy,
                    project_root=settings.project_root,
                )
                if existing is not None
                and existing.sha256 == staged.content_sha256
                and existing.status in {"imported", "render_ok"}
                else None
            )
            valid_existing_import = (
                existing is not None and active_ue_package_bundle_sha256 is not None
            )
            if valid_existing_import:
                assert existing is not None
                assert active_ue_package_bundle_sha256 is not None
                thumbnail_complete = (
                    existing.status == "render_ok"
                    and _has_valid_thumbnail_completion(
                        catalog=catalog,
                        asset_id=asset.asset_id,
                        expected_bundle_sha256=staged.bundle_sha256,
                        expected_content_sha256=staged.content_sha256,
                        expected_normalization=asset.normalization.as_dict(),
                        expected_ue_package_bundle_sha256=(active_ue_package_bundle_sha256),
                        project_root=settings.project_root,
                    )
                )
                preserved_status = existing.status
                if existing.status == "render_ok" and not thumbnail_complete:
                    # A render_ok row is only truthful while one complete, hash-valid
                    # thumbnail artifact group exists. Downgrade even for an
                    # import-only run; a failed or deferred repair must not leave a
                    # false success state in the catalog.
                    preserved_status = "imported"
                catalog.upsert_asset(
                    _asset_upsert(
                        asset,
                        staged,
                        status=preserved_status,
                        ue_package_path=existing.ue_package_path,
                        tri_count=existing.tri_count,
                        material_count=existing.material_count,
                    )
                )
                if render_thumbnails and not thumbnail_complete:
                    try:
                        thumbnail = thumbnail_catalog_asset(
                            settings=settings,
                            asset_id=asset.asset_id,
                            database_path=catalog.database_path,
                            timeout_sec=timeout_sec,
                        )
                    except Exception as exc:
                        results.append(
                            _thumbnail_failure_result(
                                asset_id=asset.asset_id,
                                staged=staged,
                                ingest_manifest=None,
                                error=exc,
                            )
                        )
                        continue
                    results.append(
                        BatchAssetResult(
                            asset_id=asset.asset_id,
                            status="render_ok",
                            bundle_sha256=staged.bundle_sha256,
                            content_sha256=staged.content_sha256,
                            raw_path=staged.raw_path,
                            ingest_manifest=None,
                            thumbnail_manifest=thumbnail.render.manifest_path,
                            catalog_status="render_ok",
                        )
                    )
                else:
                    results.append(
                        BatchAssetResult(
                            asset_id=asset.asset_id,
                            status="skipped",
                            bundle_sha256=staged.bundle_sha256,
                            content_sha256=staged.content_sha256,
                            raw_path=staged.raw_path,
                            ingest_manifest=None,
                            catalog_status=preserved_status,
                        )
                    )
                continue

            catalog.upsert_asset(_asset_upsert(asset, staged, status="raw"))
            ingest_result = ingest_asset(
                settings=settings,
                asset_id=asset.asset_id,
                source_file=staged.raw_path,
                timeout_sec=timeout_sec,
                bundle_root=staged.raw_dir,
                bundle_files=tuple(path.relative_to(staged.raw_dir) for path in staged.files),
                expected_bundle_sha256=staged.bundle_sha256,
                expected_content_sha256=staged.content_sha256,
                require_single_static_mesh=True,
                require_texture_references=require_texture_references,
                requested_normalization=asset.normalization.as_dict(),
                expected_source_structure=staged.source_structure,
                expected_source_structure_sha256=staged.source_structure_sha256,
            )
            ue_manifest = json.loads(ingest_result.manifest_path.read_text(encoding="utf-8"))
            if (
                ue_manifest.get("schema_version") != IMPORT_MANIFEST_SCHEMA_VERSION
                or ue_manifest.get("source_structure") != staged.source_structure
                or ue_manifest.get("source_structure_sha256") != staged.source_structure_sha256
                or not is_valid_source_structure_evidence(
                    ue_manifest.get("source_structure"),
                    ue_manifest.get("source_structure_sha256"),
                    expected_source_format=asset.format,
                )
            ):
                raise RuntimeError("UE ingest manifest source_structure provenance is invalid")
            if ue_manifest.get("requested_normalization") != asset.normalization.as_dict():
                raise RuntimeError(
                    "UE ingest manifest requested_normalization does not match the IngestSpec"
                )
            if (
                ue_manifest.get("import_backend") != "asset_tools_auto"
                or ue_manifest.get("normalization") != _ENGINE_NORMALIZATION
            ):
                raise RuntimeError("UE ingest manifest engine normalization is invalid")
            material_postprocess = ue_manifest.get("material_postprocess")
            expected_postprocess_policy = (
                FBX_MATERIAL_POSTPROCESS_POLICY if asset.format == "fbx" else "not_applicable"
            )
            if (
                not isinstance(material_postprocess, dict)
                or material_postprocess.get("policy") != expected_postprocess_policy
            ):
                raise RuntimeError("UE ingest manifest material postprocess is invalid")
            if not is_current_passed_quality(
                ue_manifest.get("quality"),
                require_single_static_mesh=True,
                require_texture_references=require_texture_references,
            ):
                raise RuntimeError(
                    f"UE ingest manifest does not contain passed {QUALITY_RULESET_VERSION} quality"
                )
            imported_paths = ue_manifest.get("imported_object_paths")
            package_evidence = ue_manifest.get("ue_package_bundle")
            if (
                not isinstance(imported_paths, list)
                or not imported_paths
                or any(not isinstance(path, str) for path in imported_paths)
                or not is_valid_package_bundle_evidence(
                    settings.project_root,
                    asset_id=asset.asset_id,
                    imported_object_paths=imported_paths,
                    evidence=package_evidence,
                )
            ):
                raise RuntimeError("UE ingest manifest package byte evidence is invalid")
            meshes = ue_manifest.get("static_meshes")
            if not isinstance(meshes, list) or len(meshes) != 1:
                raise RuntimeError(
                    "M2 v1 requires exactly one StaticMesh per logical asset; "
                    f"got {0 if not isinstance(meshes, list) else len(meshes)}"
                )
            mesh = meshes[0]
            if not isinstance(mesh, dict):
                raise RuntimeError("UE ingest StaticMesh payload must be an object")
            object_path = mesh.get("object_path")
            tri_count = mesh.get("triangle_count")
            material_count = mesh.get("material_count")
            if not isinstance(object_path, str):
                raise RuntimeError("UE ingest manifest is missing StaticMesh object_path")
            if not isinstance(tri_count, int) or tri_count <= 0:
                raise RuntimeError("UE ingest manifest requires triangle_count > 0")
            if not isinstance(material_count, int) or material_count < 0:
                raise RuntimeError("UE ingest manifest requires material_count >= 0")

            imported_asset = _asset_upsert(
                asset,
                staged,
                status="imported",
                ue_package_path=object_path,
                tri_count=tri_count,
                material_count=material_count,
            )
            import_artifact = ArtifactUpsert(
                artifact_id=_artifact_id(
                    asset.asset_id,
                    "import_manifest",
                    ingest_result.manifest_path,
                ),
                asset_id=asset.asset_id,
                kind="import_manifest",
                path=ingest_result.manifest_path,
                params={
                    "schema_version": IMPORT_ARTIFACT_SCHEMA_VERSION,
                    "source_format": asset.format,
                    "bundle_sha256": staged.bundle_sha256,
                    "content_sha256": staged.content_sha256,
                    "quality_ruleset_version": QUALITY_RULESET_VERSION,
                    "quality_policy": quality_policy,
                    "requested_normalization": asset.normalization.as_dict(),
                    "import_backend": "asset_tools_auto",
                    "engine_normalization": _ENGINE_NORMALIZATION,
                    "material_postprocess_policy": expected_postprocess_policy,
                    "source_structure": staged.source_structure,
                    "source_structure_sha256": staged.source_structure_sha256,
                    "ue_package_bundle": package_evidence,
                },
                sha256=_sha256(ingest_result.manifest_path),
            )
            catalog.finalize_import(imported_asset, import_artifact)
            thumbnail_manifest: Path | None = None
            final_status = "imported"
            if render_thumbnails:
                try:
                    thumbnail = thumbnail_catalog_asset(
                        settings=settings,
                        asset_id=asset.asset_id,
                        database_path=catalog.database_path,
                        timeout_sec=timeout_sec,
                    )
                except Exception as exc:
                    results.append(
                        _thumbnail_failure_result(
                            asset_id=asset.asset_id,
                            staged=staged,
                            ingest_manifest=ingest_result.manifest_path,
                            error=exc,
                        )
                    )
                    continue
                thumbnail_manifest = thumbnail.render.manifest_path
                final_status = "render_ok"
            results.append(
                BatchAssetResult(
                    asset_id=asset.asset_id,
                    status=final_status,
                    bundle_sha256=staged.bundle_sha256,
                    content_sha256=staged.content_sha256,
                    raw_path=staged.raw_path,
                    ingest_manifest=ingest_result.manifest_path,
                    thumbnail_manifest=thumbnail_manifest,
                    catalog_status=final_status,
                )
            )
        except Exception as exc:
            failed_manifest = (
                ingest_result.manifest_path
                if ingest_result is not None
                else _exception_manifest_path(exc)
            )
            error = _ingest_failure_payload(
                exc,
                project_root=settings.project_root,
                manifest_path=failed_manifest,
            )
            catalog_error: Exception | None = None
            if staged is not None:
                try:
                    failed_package, failed_triangles, failed_materials = (
                        _recover_failed_import_metadata(failed_manifest)
                    )
                    catalog.upsert_asset(
                        _asset_upsert(
                            asset,
                            staged,
                            status="failed",
                            ue_package_path=failed_package,
                            tri_count=failed_triangles,
                            material_count=failed_materials,
                            error=error,
                        )
                    )
                except Exception as record_exc:
                    catalog_error = record_exc
            if catalog_error is not None:
                error["catalog_error"] = f"{type(catalog_error).__name__}: {catalog_error}"
            results.append(
                BatchAssetResult(
                    asset_id=asset.asset_id,
                    status="failed",
                    bundle_sha256=None if staged is None else staged.bundle_sha256,
                    content_sha256=None if staged is None else staged.content_sha256,
                    raw_path=None if staged is None else staged.raw_path,
                    ingest_manifest=failed_manifest,
                    catalog_status=(
                        "failed" if staged is not None and catalog_error is None else None
                    ),
                    error=error,
                )
            )

    status = "ok" if all(result.status != "failed" for result in results) else "failed"
    report = None
    report_error: dict[str, str] | None = None
    if status == "ok" and render_thumbnails:
        try:
            report_assets: list[BatchReportAsset] = []
            for result in results:
                if (
                    result.bundle_sha256 is None
                    or result.content_sha256 is None
                    or result.catalog_status is None
                ):
                    raise RuntimeError(
                        f"thumbnail-complete result has incomplete report metadata: "
                        f"{result.asset_id}"
                    )
                report_assets.append(
                    BatchReportAsset(
                        asset_id=result.asset_id,
                        batch_status=result.status,
                        catalog_status=result.catalog_status,
                        bundle_sha256=result.bundle_sha256,
                        content_sha256=result.content_sha256,
                        requested_normalization=next(
                            item.normalization.as_dict()
                            for item in spec.assets
                            if item.asset_id == result.asset_id
                        ),
                    )
                )
            report = create_batch_report(
                project_root=settings.project_root,
                run_dir=run_dir,
                manifest_path=result_manifest,
                catalog=catalog,
                assets=tuple(report_assets),
            )
        except Exception as exc:
            status = "failed"
            report_error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "phase": "batch_report",
            }
    payload = {
        "schema_version": 1,
        "status": status,
        "source_manifest": _relative_or_absolute(settings.project_root, spec.source_path),
        "catalog": _relative_or_absolute(settings.project_root, catalog.database_path),
        "assets": [_batch_result_payload(settings.project_root, result) for result in results],
        "report": None
        if report is None
        else report.manifest_payload(project_root=settings.project_root),
        "report_error": report_error,
    }
    _write_json(result_manifest, payload)
    return BatchIngestResult(
        status=status,
        run_dir=run_dir,
        manifest_path=result_manifest,
        catalog_path=catalog.database_path,
        assets=tuple(results),
        report=report,
        report_error=report_error,
    )


def _asset_upsert(
    asset: IngestAssetSpec,
    staged: StagedAsset,
    *,
    status: str,
    ue_package_path: str | None = None,
    tri_count: int | None = None,
    material_count: int | None = None,
    error: dict[str, Any] | None = None,
) -> AssetUpsert:
    return AssetUpsert(
        asset_id=asset.asset_id,
        name=asset.name,
        source=asset.source,
        source_id=asset.source_id,
        source_url=asset.source_url,
        license=asset.license,
        license_tier=asset.license_tier,
        license_url=asset.license_url,
        attribution=asset.attribution,
        status=status,
        tags=asset.tags,
        raw_path=staged.raw_path,
        ue_package_path=ue_package_path,
        tri_count=tri_count,
        material_count=material_count,
        sha256=staged.content_sha256,
        error=error,
    )


def _batch_result_payload(project_root: Path, result: BatchAssetResult) -> dict[str, Any]:
    return {
        "asset_id": result.asset_id,
        "status": result.status,
        "bundle_sha256": result.bundle_sha256,
        "content_sha256": result.content_sha256,
        "raw_path": None
        if result.raw_path is None
        else _relative_or_absolute(project_root, result.raw_path),
        "ingest_manifest": None
        if result.ingest_manifest is None
        else _relative_or_absolute(project_root, result.ingest_manifest),
        "thumbnail_manifest": None
        if result.thumbnail_manifest is None
        else _relative_or_absolute(project_root, result.thumbnail_manifest),
        "catalog_status": result.catalog_status,
        "error": result.error,
    }


def _relative_or_absolute(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_id(asset_id: str, kind: str, path: Path) -> str:
    path_key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"{asset_id}_{kind}_{path_key}"


def _recover_failed_import_metadata(
    manifest_path: Path | None,
) -> tuple[str | None, int | None, int | None]:
    if manifest_path is None or not manifest_path.is_file():
        return None, None, None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, None, None
    meshes = payload.get("static_meshes") if isinstance(payload, dict) else None
    if not isinstance(meshes, list) or len(meshes) != 1 or not isinstance(meshes[0], dict):
        return None, None, None
    mesh = meshes[0]
    object_path = mesh.get("object_path")
    triangle_count = mesh.get("triangle_count")
    material_count = mesh.get("material_count")
    if not isinstance(object_path, str):
        return None, None, None
    if not isinstance(triangle_count, int) or isinstance(triangle_count, bool):
        return None, None, None
    if not isinstance(material_count, int) or isinstance(material_count, bool):
        return None, None, None
    return object_path, triangle_count, material_count


def _valid_import_package_bundle_sha256(
    *,
    catalog: Catalog,
    asset_id: str,
    expected_bundle_sha256: str,
    expected_content_sha256: str,
    expected_normalization: dict[str, str | float],
    expected_source_format: str,
    expected_source_structure: dict[str, object],
    expected_source_structure_sha256: str,
    expected_quality_policy: dict[str, bool],
    project_root: Path,
) -> str | None:
    for artifact in catalog.list_artifacts(asset_id=asset_id):
        if artifact.kind != "import_manifest":
            continue
        if (
            artifact.params.get("schema_version") != IMPORT_ARTIFACT_SCHEMA_VERSION
            or artifact.params.get("bundle_sha256") != expected_bundle_sha256
            or artifact.params.get("content_sha256") != expected_content_sha256
            or artifact.params.get("source_format") != expected_source_format
            or artifact.params.get("quality_ruleset_version") != QUALITY_RULESET_VERSION
            or artifact.params.get("quality_policy") != expected_quality_policy
            or artifact.params.get("requested_normalization") != expected_normalization
            or artifact.params.get("import_backend") != "asset_tools_auto"
            or artifact.params.get("engine_normalization") != _ENGINE_NORMALIZATION
            or artifact.params.get("material_postprocess_policy")
            != (
                FBX_MATERIAL_POSTPROCESS_POLICY
                if expected_source_format == "fbx"
                else "not_applicable"
            )
            or artifact.params.get("source_structure") != expected_source_structure
            or artifact.params.get("source_structure_sha256") != expected_source_structure_sha256
        ):
            continue
        artifact_path = project_root / artifact.path
        if not _is_regular_project_file(project_root, artifact_path) or artifact.sha256 is None:
            continue
        if _sha256(artifact_path) != artifact.sha256:
            continue
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != IMPORT_MANIFEST_SCHEMA_VERSION
            or payload.get("status") != "ok"
        ):
            continue
        if not is_current_passed_quality(
            payload.get("quality"),
            require_single_static_mesh=expected_quality_policy["require_single_static_mesh"],
            require_texture_references=expected_quality_policy["require_texture_references"],
        ):
            continue
        if (
            payload.get("bundle_sha256") != expected_bundle_sha256
            or payload.get("content_sha256") != expected_content_sha256
        ):
            continue
        if payload.get("requested_normalization") != expected_normalization:
            continue
        if (
            payload.get("source_structure") != expected_source_structure
            or payload.get("source_structure_sha256") != expected_source_structure_sha256
            or not is_valid_source_structure_evidence(
                payload.get("source_structure"),
                payload.get("source_structure_sha256"),
                expected_source_format=expected_source_format,
            )
        ):
            continue
        if (
            payload.get("import_backend") != "asset_tools_auto"
            or payload.get("normalization") != _ENGINE_NORMALIZATION
        ):
            continue
        material_postprocess = payload.get("material_postprocess")
        if not isinstance(material_postprocess, dict) or material_postprocess.get("policy") != (
            FBX_MATERIAL_POSTPROCESS_POLICY if expected_source_format == "fbx" else "not_applicable"
        ):
            continue
        imported_paths = payload.get("imported_object_paths")
        package_evidence = payload.get("ue_package_bundle")
        if (
            not isinstance(imported_paths, list)
            or not imported_paths
            or any(not isinstance(path, str) for path in imported_paths)
            or artifact.params.get("ue_package_bundle") != package_evidence
        ):
            continue
        if is_valid_package_bundle_evidence(
            project_root,
            asset_id=asset_id,
            imported_object_paths=imported_paths,
            evidence=package_evidence,
        ):
            assert isinstance(package_evidence, dict)
            package_bundle_sha256 = package_evidence.get("package_bundle_sha256")
            if isinstance(package_bundle_sha256, str):
                return package_bundle_sha256
    return None


def _exception_manifest_path(error: Exception) -> Path | None:
    if isinstance(error, IngestQualityError | IngestExecutionError):
        return error.manifest_path
    return None


def _ingest_failure_payload(
    error: Exception,
    *,
    project_root: Path,
    manifest_path: Path | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": type(error).__name__,
        "message": str(error),
    }
    if isinstance(error, IngestExecutionError):
        payload["cause_type"] = error.cause_type
    if isinstance(error, IngestQualityError):
        payload["quality"] = error.report
    if manifest_path is not None:
        payload["ingest_manifest"] = _relative_or_absolute(project_root, manifest_path)
    return payload


def _has_valid_thumbnail_completion(
    *,
    catalog: Catalog,
    asset_id: str,
    expected_bundle_sha256: str,
    expected_content_sha256: str,
    expected_normalization: dict[str, str | float],
    expected_ue_package_bundle_sha256: str,
    project_root: Path,
) -> bool:
    required = {
        "thumbnail_beauty",
        "thumbnail_mask",
        "thumbnail_mask_raw",
        "thumbnail_render_manifest",
        "thumbnail_contact_sheet",
    }
    valid_groups: dict[str, dict[str, ArtifactRecord]] = {}
    for artifact in catalog.list_artifacts(asset_id=asset_id):
        if artifact.kind not in required or artifact.sha256 is None:
            continue
        render_manifest = artifact.params.get("render_manifest")
        if not isinstance(render_manifest, str) or not render_manifest:
            continue
        if not _valid_thumbnail_artifact_params(
            artifact.params,
            expected_bundle_sha256=expected_bundle_sha256,
            expected_normalization=expected_normalization,
            expected_ue_package_bundle_sha256=expected_ue_package_bundle_sha256,
        ):
            continue
        path = project_root / artifact.path
        if _is_regular_project_file(project_root, path) and _sha256(path) == artifact.sha256:
            valid_groups.setdefault(render_manifest, {})[artifact.kind] = artifact
    return any(
        set(artifacts) == required
        and artifacts["thumbnail_render_manifest"].path == render_manifest
        and _valid_thumbnail_render_manifest(
            project_root=project_root,
            manifest_path=project_root / render_manifest,
            asset_id=asset_id,
            expected_bundle_sha256=expected_bundle_sha256,
            expected_content_sha256=expected_content_sha256,
            expected_normalization=expected_normalization,
            expected_ue_package_bundle_sha256=expected_ue_package_bundle_sha256,
            expected_import_manifest=str(
                artifacts["thumbnail_render_manifest"].params["import_manifest"]
            ),
            expected_selected_view_index=int(
                artifacts["thumbnail_render_manifest"].params["selected_view_index"]
            ),
            artifact_ids={artifact.artifact_id for artifact in artifacts.values()},
        )
        for render_manifest, artifacts in valid_groups.items()
    )


def _valid_thumbnail_artifact_params(
    params: dict[str, Any],
    *,
    expected_bundle_sha256: str,
    expected_normalization: dict[str, str | float],
    expected_ue_package_bundle_sha256: str,
) -> bool:
    return (
        params.get("schema_version") == 1
        and params.get("thumbnail_preset") == THUMBNAIL_PRESET
        and params.get("views") == 8
        and params.get("resolution") == [512, 512]
        and params.get("lighting") == "three_point"
        and params.get("subject_stencil_id") == 1
        and params.get("bundle_sha256") == expected_bundle_sha256
        and params.get("ue_package_bundle_sha256") == expected_ue_package_bundle_sha256
        and isinstance(params.get("selected_view_index"), int)
        and not isinstance(params.get("selected_view_index"), bool)
        and 0 <= params["selected_view_index"] < 8
        and params.get("requested_normalization") == expected_normalization
        and isinstance(params.get("import_manifest"), str)
        and bool(params.get("import_manifest"))
    )


def _valid_thumbnail_render_manifest(
    *,
    project_root: Path,
    manifest_path: Path,
    asset_id: str,
    expected_bundle_sha256: str,
    expected_content_sha256: str,
    expected_normalization: dict[str, str | float],
    expected_ue_package_bundle_sha256: str,
    expected_import_manifest: str,
    expected_selected_view_index: int,
    artifact_ids: set[str],
) -> bool:
    if not _is_regular_project_file(project_root, manifest_path):
        return False
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    asset = payload.get("asset")
    job = payload.get("job")
    camera = job.get("camera") if isinstance(job, dict) else None
    lighting = job.get("lighting") if isinstance(job, dict) else None
    commit = payload.get("catalog_commit")
    normalization = asset.get("normalization") if isinstance(asset, dict) else None
    return (
        payload.get("schema_version") == 3
        and payload.get("status") == "ok"
        and payload.get("asset_id") == asset_id
        and isinstance(asset, dict)
        and asset.get("kind") == "catalog"
        and asset.get("asset_id") == asset_id
        and asset.get("bundle_sha256") == expected_bundle_sha256
        and asset.get("content_sha256") == expected_content_sha256
        and asset.get("ue_package_bundle_sha256") == expected_ue_package_bundle_sha256
        and asset.get("import_manifest") == expected_import_manifest
        and isinstance(normalization, dict)
        and normalization.get("request") == expected_normalization
        and isinstance(job, dict)
        and job.get("assets") == [asset_id]
        and job.get("passes") == ["beauty_lit", "object_mask"]
        and isinstance(camera, dict)
        and camera.get("rig") == "orbit"
        and camera.get("views") == 8
        and camera.get("elevation_deg") == 20
        and camera.get("fov") == 45
        and camera.get("resolution") == [512, 512]
        and isinstance(lighting, dict)
        and lighting.get("preset") == "three_point"
        and isinstance(commit, dict)
        and commit.get("asset_id") == asset_id
        and commit.get("target_status") == "render_ok"
        and commit.get("bundle_sha256") == expected_bundle_sha256
        and commit.get("ue_package_bundle_sha256") == expected_ue_package_bundle_sha256
        and commit.get("thumbnail_preset") == THUMBNAIL_PRESET
        and commit.get("selected_view_index") == expected_selected_view_index
        and commit.get("requested_normalization") == expected_normalization
        and commit.get("import_manifest") == expected_import_manifest
        and is_valid_thumbnail_validation(
            payload.get("thumbnail_validation"),
            expected_frames=8,
        )
        and payload["thumbnail_validation"].get("selected_view_index")
        == expected_selected_view_index
        and is_valid_catalog_scene_sanitization(
            payload.get("scene_sanitization"),
            expected_subjobs=2,
        )
        and _same_artifact_ids(commit.get("artifact_ids"), artifact_ids)
    )


def _same_artifact_ids(value: Any, expected: set[str]) -> bool:
    return (
        isinstance(value, list)
        and len(value) == len(expected)
        and all(isinstance(item, str) for item in value)
        and set(value) == expected
    )


def _thumbnail_failure_result(
    *,
    asset_id: str,
    staged: StagedAsset,
    ingest_manifest: Path | None,
    error: Exception,
) -> BatchAssetResult:
    return BatchAssetResult(
        asset_id=asset_id,
        status="failed",
        bundle_sha256=staged.bundle_sha256,
        content_sha256=staged.content_sha256,
        raw_path=staged.raw_path,
        ingest_manifest=ingest_manifest,
        thumbnail_manifest=None,
        catalog_status="imported",
        error={
            "type": type(error).__name__,
            "message": str(error),
            "phase": "thumbnail",
        },
    )


def _is_regular_project_file(project_root: Path, path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return False
    return True


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
