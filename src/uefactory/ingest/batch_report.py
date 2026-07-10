from __future__ import annotations

import hashlib
import html
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from uefactory.catalog import ArtifactRecord, AssetRecord, Catalog
from uefactory.render.thumbnails import (
    THUMBNAIL_PRESET,
    is_valid_catalog_scene_sanitization,
    is_valid_thumbnail_validation,
)

_SHA256_LENGTH = 64
_REPORT_DIRECTORY = "report"
_THUMBNAIL_ARTIFACT_KINDS = frozenset(
    {
        "thumbnail_beauty",
        "thumbnail_mask",
        "thumbnail_mask_raw",
        "thumbnail_render_manifest",
        "thumbnail_contact_sheet",
    }
)


class BatchReportError(RuntimeError):
    """Raised when a successful thumbnail batch cannot be summarized safely."""


@dataclass(frozen=True)
class BatchReportAsset:
    asset_id: str
    batch_status: str
    catalog_status: str
    bundle_sha256: str
    content_sha256: str
    requested_normalization: dict[str, str | float]


@dataclass(frozen=True)
class BatchReportThumbnail:
    asset_id: str
    path: Path
    sha256: str
    asset_sheet_path: Path
    asset_sheet_sha256: str


@dataclass(frozen=True)
class BatchReportArtifacts:
    contact_sheet: Path
    index_html: Path
    thumbnails: tuple[BatchReportThumbnail, ...]

    def manifest_payload(self, *, project_root: Path) -> dict[str, object]:
        return {
            "contact_sheet": _relative_project_path(project_root, self.contact_sheet),
            "index_html": _relative_project_path(project_root, self.index_html),
            "thumbnails": [
                {
                    "asset_id": item.asset_id,
                    "path": _relative_project_path(project_root, item.path),
                    "sha256": item.sha256,
                    "asset_sheet_path": _relative_project_path(
                        project_root,
                        item.asset_sheet_path,
                    ),
                    "asset_sheet_sha256": item.asset_sheet_sha256,
                }
                for item in self.thumbnails
            ],
        }


@dataclass(frozen=True)
class _ReportEntry:
    request: BatchReportAsset
    record: AssetRecord
    source_thumbnail: Path
    source_asset_sheet: Path


@dataclass(frozen=True)
class _ThumbnailEvidence:
    beauty: ArtifactRecord
    asset_sheet: ArtifactRecord


def create_batch_report(
    *,
    project_root: Path,
    run_dir: Path,
    manifest_path: Path,
    catalog: Catalog,
    assets: tuple[BatchReportAsset, ...],
) -> BatchReportArtifacts:
    """Create one self-contained offline report for a successful thumbnail batch."""

    root = project_root.expanduser().resolve()
    resolved_run_dir = run_dir.expanduser().resolve()
    _require_project_directory(root, resolved_run_dir, field="run_dir")
    if manifest_path.expanduser().resolve().parent != resolved_run_dir:
        raise BatchReportError("batch manifest must be a direct child of run_dir")
    if catalog.project_root != root:
        raise BatchReportError(
            f"catalog project_root does not match report project_root: {catalog.project_root}"
        )
    entries = _resolve_entries(project_root=root, catalog=catalog, assets=assets)

    report_dir = resolved_run_dir / _REPORT_DIRECTORY
    if report_dir.exists() or report_dir.is_symlink():
        raise BatchReportError(f"batch report destination already exists: {report_dir}")
    temporary = Path(tempfile.mkdtemp(prefix=".batch-report-", dir=resolved_run_dir))
    try:
        temporary_thumbnails = temporary / "thumbnails"
        temporary_thumbnails.mkdir()
        temporary_asset_sheets = temporary / "asset_sheets"
        temporary_asset_sheets.mkdir()
        for entry in entries:
            _write_normalized_thumbnail(
                entry.source_thumbnail,
                temporary_thumbnails / f"{entry.request.asset_id}.png",
            )
            _write_normalized_thumbnail(
                entry.source_asset_sheet,
                temporary_asset_sheets / f"{entry.request.asset_id}.png",
            )
        _create_contact_sheet(
            entries=entries,
            thumbnails_dir=temporary_thumbnails,
            output_path=temporary / "contact_sheet.png",
        )
        _create_index_html(
            entries=entries,
            report_dir=temporary,
            manifest_path=manifest_path,
            output_path=temporary / "index.html",
        )
        temporary.replace(report_dir)
    except BatchReportError:
        raise
    except OSError as exc:
        raise BatchReportError(f"cannot create batch report in {report_dir}: {exc}") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    thumbnails = tuple(
        BatchReportThumbnail(
            asset_id=entry.request.asset_id,
            path=report_dir / "thumbnails" / f"{entry.request.asset_id}.png",
            sha256=_sha256(report_dir / "thumbnails" / f"{entry.request.asset_id}.png"),
            asset_sheet_path=report_dir / "asset_sheets" / f"{entry.request.asset_id}.png",
            asset_sheet_sha256=_sha256(
                report_dir / "asset_sheets" / f"{entry.request.asset_id}.png"
            ),
        )
        for entry in entries
    )
    return BatchReportArtifacts(
        contact_sheet=report_dir / "contact_sheet.png",
        index_html=report_dir / "index.html",
        thumbnails=thumbnails,
    )


def _resolve_entries(
    *,
    project_root: Path,
    catalog: Catalog,
    assets: tuple[BatchReportAsset, ...],
) -> tuple[_ReportEntry, ...]:
    if not assets:
        raise BatchReportError("cannot create a batch report without assets")
    seen: set[str] = set()
    entries: list[_ReportEntry] = []
    for request in assets:
        if request.asset_id in seen:
            raise BatchReportError(f"duplicate report asset_id: {request.asset_id!r}")
        seen.add(request.asset_id)
        if request.batch_status not in {"render_ok", "skipped"}:
            raise BatchReportError(
                f"asset {request.asset_id!r} is not thumbnail-complete: "
                f"batch_status={request.batch_status!r}"
            )
        if request.catalog_status != "render_ok":
            raise BatchReportError(
                f"asset {request.asset_id!r} is not thumbnail-complete: "
                f"catalog_status={request.catalog_status!r}"
            )
        _require_sha256(request.bundle_sha256, f"{request.asset_id}.bundle_sha256")
        _require_sha256(request.content_sha256, f"{request.asset_id}.content_sha256")
        record = catalog.get_asset(request.asset_id)
        if record is None:
            raise BatchReportError(f"catalog asset is missing: {request.asset_id!r}")
        if record.status != request.catalog_status:
            raise BatchReportError(
                f"catalog status changed for {request.asset_id!r}: "
                f"expected={request.catalog_status!r} actual={record.status!r}"
            )
        if record.sha256 != request.content_sha256:
            raise BatchReportError(
                f"catalog content hash mismatch for {request.asset_id!r}: "
                f"expected={request.content_sha256} actual={record.sha256}"
            )
        if record.tri_count is None or record.material_count is None:
            raise BatchReportError(f"catalog mesh stats are incomplete for {request.asset_id!r}")
        evidence = _latest_valid_thumbnail(
            project_root=project_root,
            catalog=catalog,
            asset_id=request.asset_id,
            expected_bundle_sha256=request.bundle_sha256,
            expected_content_sha256=request.content_sha256,
            expected_normalization=request.requested_normalization,
        )
        entries.append(
            _ReportEntry(
                request=request,
                record=record,
                source_thumbnail=project_root / evidence.beauty.path,
                source_asset_sheet=project_root / evidence.asset_sheet.path,
            )
        )
    return tuple(entries)


def _latest_valid_thumbnail(
    *,
    project_root: Path,
    catalog: Catalog,
    asset_id: str,
    expected_bundle_sha256: str,
    expected_content_sha256: str,
    expected_normalization: dict[str, str | float],
) -> _ThumbnailEvidence:
    valid_groups: dict[str, dict[str, ArtifactRecord]] = {}
    for artifact in catalog.list_artifacts(asset_id=asset_id):
        if artifact.kind not in _THUMBNAIL_ARTIFACT_KINDS or artifact.sha256 is None:
            continue
        render_manifest = artifact.params.get("render_manifest")
        if not isinstance(render_manifest, str) or not render_manifest:
            continue
        if not _valid_thumbnail_artifact_params(
            artifact.params,
            expected_bundle_sha256=expected_bundle_sha256,
            expected_normalization=expected_normalization,
        ):
            continue
        source = project_root / artifact.path
        if not _is_regular_project_file(project_root, source):
            continue
        if _sha256(source) != artifact.sha256:
            continue
        if artifact.kind in {"thumbnail_beauty", "thumbnail_contact_sheet"} and not (
            _is_valid_image(source)
        ):
            continue
        group = valid_groups.setdefault(render_manifest, {})
        previous = group.get(artifact.kind)
        if previous is None or (artifact.created_at, artifact.artifact_id) > (
            previous.created_at,
            previous.artifact_id,
        ):
            group[artifact.kind] = artifact
    candidates: list[_ThumbnailEvidence] = []
    for render_manifest, group in valid_groups.items():
        if set(group) != _THUMBNAIL_ARTIFACT_KINDS:
            continue
        if group["thumbnail_render_manifest"].path != render_manifest:
            continue
        if not _valid_thumbnail_render_manifest(
            project_root=project_root,
            manifest_path=project_root / render_manifest,
            asset_id=asset_id,
            expected_bundle_sha256=expected_bundle_sha256,
            expected_content_sha256=expected_content_sha256,
            expected_normalization=expected_normalization,
            expected_import_manifest=str(
                group["thumbnail_render_manifest"].params["import_manifest"]
            ),
            expected_selected_view_index=int(
                group["thumbnail_render_manifest"].params["selected_view_index"]
            ),
            artifact_ids={artifact.artifact_id for artifact in group.values()},
        ):
            continue
        candidates.append(
            _ThumbnailEvidence(
                beauty=group["thumbnail_beauty"],
                asset_sheet=group["thumbnail_contact_sheet"],
            )
        )
    if not candidates:
        raise BatchReportError(
            f"asset {asset_id!r} has no complete hash-valid thumbnail artifact group"
        )
    return max(
        candidates,
        key=lambda item: (item.beauty.created_at, item.beauty.artifact_id),
    )


def _valid_thumbnail_artifact_params(
    params: dict[str, object],
    *,
    expected_bundle_sha256: str,
    expected_normalization: dict[str, str | float],
) -> bool:
    selected_view_index = params.get("selected_view_index")
    return (
        params.get("schema_version") == 1
        and params.get("thumbnail_preset") == THUMBNAIL_PRESET
        and params.get("views") == 8
        and params.get("resolution") == [512, 512]
        and params.get("lighting") == "three_point"
        and params.get("subject_stencil_id") == 1
        and params.get("bundle_sha256") == expected_bundle_sha256
        and isinstance(selected_view_index, int)
        and not isinstance(selected_view_index, bool)
        and 0 <= selected_view_index < 8
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


def _same_artifact_ids(value: object, expected: set[str]) -> bool:
    return (
        isinstance(value, list)
        and len(value) == len(expected)
        and all(isinstance(item, str) for item in value)
        and set(value) == expected
    )


def _write_normalized_thumbnail(source: Path, output_path: Path) -> None:
    try:
        with Image.open(source) as image:
            image.load()
            normalized = image.convert("RGB")
    except (OSError, UnidentifiedImageError) as exc:
        raise BatchReportError(f"cannot decode thumbnail {source}: {exc}") from exc
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        normalized.save(
            temporary,
            format="PNG",
            optimize=False,
            compress_level=6,
        )
        temporary.replace(output_path)
    finally:
        normalized.close()
        temporary.unlink(missing_ok=True)


def _create_contact_sheet(
    *,
    entries: tuple[_ReportEntry, ...],
    thumbnails_dir: Path,
    output_path: Path,
) -> None:
    columns = min(5, len(entries))
    rows = math.ceil(len(entries) / columns)
    thumb_width = 220
    thumb_height = 220
    padding = 10
    title_height = 38
    label_height = 42
    cell_width = thumb_width + padding * 2
    cell_height = thumb_height + label_height + padding * 2
    sheet = Image.new(
        "RGB",
        (columns * cell_width, title_height + rows * cell_height),
        (15, 18, 23),
    )
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    draw.text(
        (padding, 12),
        f"UEFactory batch contact sheet - {len(entries)} assets",
        fill=(232, 236, 242),
        font=font,
    )
    for index, entry in enumerate(entries):
        column = index % columns
        row = index // columns
        left = column * cell_width
        top = title_height + row * cell_height
        draw.rectangle(
            (left + 4, top + 4, left + cell_width - 4, top + cell_height - 4),
            fill=(28, 32, 40),
            outline=(62, 70, 84),
        )
        path = thumbnails_dir / f"{entry.request.asset_id}.png"
        with Image.open(path) as source:
            source.load()
            preview = ImageOps.contain(
                source.convert("RGB"),
                (thumb_width, thumb_height),
                Image.Resampling.LANCZOS,
            )
        image_x = left + padding + (thumb_width - preview.width) // 2
        image_y = top + padding + (thumb_height - preview.height) // 2
        sheet.paste(preview, (image_x, image_y))
        preview.close()
        draw.text(
            (left + padding, top + padding + thumb_height + 4),
            entry.request.asset_id,
            fill=(240, 242, 246),
            font=font,
        )
        draw.text(
            (left + padding, top + padding + thumb_height + 20),
            f"{entry.record.tri_count} tris / {entry.record.material_count} materials",
            fill=(168, 178, 194),
            font=font,
        )
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        sheet.save(temporary, format="PNG", optimize=False, compress_level=6)
        temporary.replace(output_path)
    finally:
        sheet.close()
        temporary.unlink(missing_ok=True)


def _create_index_html(
    *,
    entries: tuple[_ReportEntry, ...],
    report_dir: Path,
    manifest_path: Path,
    output_path: Path,
) -> None:
    rows: list[str] = []
    for entry in entries:
        asset_id = html.escape(entry.request.asset_id)
        thumbnail = f"thumbnails/{entry.request.asset_id}.png"
        asset_sheet = f"asset_sheets/{entry.request.asset_id}.png"
        rows.append(
            "".join(
                [
                    f'<tr id="{asset_id}">',
                    '<td class="preview">',
                    f'<a href="{asset_sheet}"><img src="{thumbnail}" alt="{asset_id}"></a>',
                    f'<br><a href="{asset_sheet}">8-view beauty + mask sheet</a>',
                    "</td>",
                    "<td>",
                    f"<strong>{html.escape(entry.record.name)}</strong><br>",
                    f"<code>{asset_id}</code><br>",
                    f"{html.escape(entry.record.source)} · {html.escape(entry.record.license)}",
                    "</td>",
                    "<td>",
                    f"catalog: <strong>{html.escape(entry.request.catalog_status)}</strong><br>",
                    f"batch: {html.escape(entry.request.batch_status)}",
                    "</td>",
                    '<td class="hash">',
                    f"content <code>{html.escape(entry.request.content_sha256)}</code><br>",
                    f"bundle <code>{html.escape(entry.request.bundle_sha256)}</code>",
                    "</td>",
                    "<td>",
                    f"{entry.record.tri_count:,} triangles<br>",
                    f"{entry.record.material_count:,} materials",
                    "</td>",
                    "</tr>",
                ]
            )
        )
    manifest_link = html.escape(_relative_link(report_dir, manifest_path))
    output_path.write_text(
        "\n".join(
            [
                "<!doctype html>",
                '<html lang="en">',
                "<head>",
                '<meta charset="utf-8">',
                '<meta name="viewport" content="width=device-width,initial-scale=1">',
                "<title>UEFactory Batch Report</title>",
                "<style>",
                "body{font-family:system-ui,sans-serif;margin:24px;background:#0f1217;color:#edf0f5}",
                "a{color:#8cc8ff}code{font-family:ui-monospace,monospace}",
                "img.sheet{max-width:100%;height:auto;border:1px solid #3e4654}",
                "table{width:100%;border-collapse:collapse;margin-top:18px}",
                "th,td{padding:10px;border-bottom:1px solid #343b47;text-align:left;"
                "vertical-align:top}",
                "td.preview{width:144px}td.preview img{width:128px;height:128px;object-fit:contain;"
                "background:#1c2028;border:1px solid #3e4654}",
                "td.hash{max-width:38rem;overflow-wrap:anywhere;font-size:.78rem}",
                "</style>",
                "</head>",
                "<body>",
                "<h1>UEFactory Batch Report</h1>",
                f"<p>{len(entries)} thumbnail-complete assets · "
                f'<a href="{manifest_link}">manifest.json</a></p>',
                '<p><a href="contact_sheet.png"><img class="sheet" src="contact_sheet.png" '
                'alt="batch contact sheet"></a></p>',
                "<table>",
                "<thead><tr><th>Thumbnail</th><th>Asset</th><th>Status</th>"
                "<th>Hashes</th><th>Mesh stats</th></tr></thead>",
                "<tbody>",
                *rows,
                "</tbody>",
                "</table>",
                "</body>",
                "</html>",
            ]
        ),
        encoding="utf-8",
    )


def _require_sha256(value: str, field: str) -> None:
    if len(value) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise BatchReportError(f"{field} must be a lowercase SHA-256")


def _require_project_directory(project_root: Path, path: Path, *, field: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise BatchReportError(f"{field} is not a regular directory: {path}")
    try:
        path.relative_to(project_root)
    except ValueError as exc:
        raise BatchReportError(f"{field} is outside project_root: {path}") from exc


def _is_regular_project_file(project_root: Path, path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        path.resolve().relative_to(project_root)
    except ValueError:
        return False
    return True


def _is_valid_image(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, UnidentifiedImageError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_project_path(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError as exc:  # pragma: no cover - guarded before report publication
        raise BatchReportError(f"report artifact is outside project_root: {path}") from exc


def _relative_link(base_dir: Path, target: Path) -> str:
    return Path(os.path.relpath(target.resolve(), start=base_dir.resolve())).as_posix()
