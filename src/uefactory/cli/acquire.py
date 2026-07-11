from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import typer

from uefactory.acquire.blackmyth import BlackMythLibraryError, scan_blackmyth_scene_library
from uefactory.acquire.hdri import (
    DEFAULT_HDRI_ASSET,
    DEFAULT_HDRI_RESOLUTION,
    acquire_polyhaven_hdri,
)
from uefactory.acquire.models import ModelAcquireError, acquire_m2_models
from uefactory.acquire.polyhaven import (
    DEFAULT_RESOLUTION as DEFAULT_POLYHAVEN_RESOLUTION,
)
from uefactory.acquire.polyhaven import (
    PolyHavenAcquireError,
    PolyHavenSyncResult,
    TerminalStatus,
    finalize_polyhaven_items,
    sync_polyhaven_models,
)
from uefactory.cli._common import settings_from_context
from uefactory.ingest.pipeline import BatchIngestResult, ingest_batch

acquire_app = typer.Typer(help="Acquire runtime assets.")


@acquire_app.command("polyhaven")
def acquire_polyhaven(
    ctx: typer.Context,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=10_000, help="Maximum model revisions per cycle."),
    ] = 1,
    resolution: Annotated[
        str,
        typer.Option("--resolution", help="Poly Haven glTF resolution, e.g. 1k or 2k."),
    ] = DEFAULT_POLYHAVEN_RESOLUTION,
    force: Annotated[
        bool,
        typer.Option("--force", help="Redownload files before exact verification."),
    ] = False,
    ingest: Annotated[
        bool,
        typer.Option(
            "--ingest/--download-only",
            help="Run the generated strict IngestSpec through UE after acquisition.",
        ),
    ] = False,
    database: Annotated[
        Path | None,
        typer.Option("--database", "--db", help="Catalog database for --ingest."),
    ] = None,
    timeout_sec: Annotated[
        int,
        typer.Option("--timeout-sec", min=1, help="Timeout for each UE process."),
    ] = 1800,
    thumbnails: Annotated[
        bool,
        typer.Option(
            "--thumbnails/--no-thumbnails",
            help="Render standard beauty/mask thumbnails when --ingest is enabled.",
        ),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a machine-readable acquisition summary."),
    ] = False,
) -> None:
    """Incrementally prepare verified Poly Haven models and optionally ingest them."""

    settings = settings_from_context(ctx)
    try:
        result = sync_polyhaven_models(
            settings=settings,
            limit=limit,
            resolution=resolution,
            force=force,
        )
    except PolyHavenAcquireError as exc:
        typer.echo(f"Poly Haven acquisition failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    batch: BatchIngestResult | None = None
    terminal_statuses: dict[str, TerminalStatus] = {}
    if ingest and result.generated_spec_path is not None:
        try:
            batch = ingest_batch(
                settings=settings,
                manifest_path=result.generated_spec_path,
                database_path=database,
                timeout_sec=timeout_sec,
                render_thumbnails=thumbnails,
            )
            terminal_statuses = finalize_polyhaven_items(
                result=result,
                batch_manifest_path=batch.manifest_path,
            )
        except Exception as exc:
            typer.echo(f"Poly Haven downstream ingest failed: {exc}", err=True)
            raise typer.Exit(1) from exc

    payload = _polyhaven_payload(
        result=result,
        batch=batch,
        terminal_statuses=terminal_statuses,
    )
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(
            f"Poly Haven models prepared: {result.selected}/{result.discovered}; "
            f"{result.downloaded_files} files downloaded, {result.reused_files} reused; "
            f"{result.downloaded_bytes} bytes transferred, "
            f"{result.verified_bytes} verified"
        )
        if result.generated_spec_path is not None:
            typer.echo(f"Generated IngestSpec: {result.generated_spec_path}")
        else:
            typer.echo("No eligible Poly Haven model revisions; source is unchanged")
        typer.echo(f"Acquisition manifest: {result.manifest_path}")
        if batch is not None:
            typer.echo(f"Ingest status: {batch.status}")
            typer.echo(f"Ingest manifest: {batch.manifest_path}")
            if batch.report is not None:
                typer.echo(f"Contact sheet: {batch.report.contact_sheet}")
    if batch is not None and batch.status != "ok":
        raise typer.Exit(1)


def _polyhaven_payload(
    *,
    result: PolyHavenSyncResult,
    batch: BatchIngestResult | None,
    terminal_statuses: Mapping[str, TerminalStatus],
) -> dict[str, object]:
    ingest_payload: dict[str, object] | None = None
    if batch is not None:
        ingest_payload = {
            "status": batch.status,
            "manifest": str(batch.manifest_path),
            "catalog": str(batch.catalog_path),
            "assets": [
                {
                    "asset_id": item.asset_id,
                    "status": item.status,
                    "catalog_status": item.catalog_status,
                    "terminal_status": terminal_statuses.get(item.asset_id),
                    "error": item.error,
                }
                for item in batch.assets
            ],
            "report": (
                None
                if batch.report is None
                else {
                    "contact_sheet": str(batch.report.contact_sheet),
                    "index_html": str(batch.report.index_html),
                }
            ),
            "report_error": batch.report_error,
        }
    return {
        "status": (
            "noop"
            if batch is None and result.generated_spec_path is None
            else ("prepared" if batch is None else batch.status)
        ),
        "source": "polyhaven",
        "asset_type": "models",
        "discovered": result.discovered,
        "selected": result.selected,
        "downloaded_files": result.downloaded_files,
        "reused_files": result.reused_files,
        "downloaded_bytes": result.downloaded_bytes,
        "verified_bytes": result.verified_bytes,
        "snapshot_sha256": result.snapshot_sha256,
        "run_dir": str(result.run_dir),
        "manifest": str(result.manifest_path),
        "state": str(result.state_path),
        "generated_ingest_spec": (
            None if result.generated_spec_path is None else str(result.generated_spec_path)
        ),
        "items": [
            {
                "asset_id": item.asset_id,
                "source_id": item.source_id,
                "revision": item.revision,
                "main_path": str(item.main_path),
                "dependencies": [str(path) for path in item.dependency_paths],
                "downloaded_files": item.downloaded_files,
                "reused_files": item.reused_files,
                "downloaded_bytes": item.downloaded_bytes,
                "verified_bytes": item.verified_bytes,
                "source_bundle_sha256": item.source_bundle_sha256,
                "source_content_sha256": item.source_content_sha256,
                "acquired_at": item.acquired_at,
                "verified_at": item.verified_at,
                "state_status": terminal_statuses.get(item.asset_id, item.state_status),
            }
            for item in result.items
        ],
        "ingest": ingest_payload,
    }


@acquire_app.command("hdri")
def acquire_hdri(
    ctx: typer.Context,
    asset_id: Annotated[
        str,
        typer.Option("--asset-id", help="PolyHaven HDRI asset id."),
    ] = DEFAULT_HDRI_ASSET,
    resolution: Annotated[
        str,
        typer.Option("--resolution", help="PolyHaven HDRI resolution, e.g. 1k or 2k."),
    ] = DEFAULT_HDRI_RESOLUTION,
    force: Annotated[
        bool,
        typer.Option("--force", help="Redownload even when the checked file already exists."),
    ] = False,
) -> None:
    settings = settings_from_context(ctx)
    result = acquire_polyhaven_hdri(
        settings=settings,
        asset_id=asset_id,
        resolution=resolution,
        force=force,
    )
    action = "reused" if result.skipped else "downloaded"
    typer.echo(f"HDRI {action}: {result.file_path}")
    typer.echo(f"Metadata: {result.metadata_path}")
    typer.echo(f"License: {result.license}")


@acquire_app.command("models")
def acquire_models(
    ctx: typer.Context,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Redownload all model files and replace them only after exact verification.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a machine-readable acquisition summary."),
    ] = False,
) -> None:
    """Acquire the pinned M2 open-license sample set."""

    settings = settings_from_context(ctx)
    try:
        result = acquire_m2_models(settings=settings, force=force)
    except ModelAcquireError as exc:
        typer.echo(f"Model acquisition failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    payload = {
        "root_dir": str(result.root_dir),
        "inventory_path": str(result.inventory_path),
        "models": len(result.models),
        "downloaded_files": result.downloaded_files,
        "reused_files": result.reused_files,
        "bytes": result.bytes,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"Models ready: {len(result.models)} in {result.root_dir}")
    typer.echo(
        f"Files: {result.downloaded_files} downloaded, {result.reused_files} reused; "
        f"{result.bytes} bytes verified"
    )
    typer.echo(f"Inventory: {result.inventory_path}")


@acquire_app.command("blackmyth")
def scan_blackmyth(
    root: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            help="Approved BlackMyth/external library root to scan read-only.",
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit checked records and quarantined entries."),
    ] = False,
) -> None:
    """Safely discover scene GLBs without copying or modifying the source library."""

    try:
        result = scan_blackmyth_scene_library(root)
    except BlackMythLibraryError as exc:
        typer.echo(f"BlackMyth scene scan failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    open_count = sum(item.license_tier == "open" for item in result.records)
    nc_count = sum(item.license_tier == "nc" for item in result.records)
    payload = {
        "root": str(result.root),
        "records": [item.as_dict() for item in result.records],
        "quarantined": [item.as_dict() for item in result.quarantined],
        "counts": {
            "records": len(result.records),
            "open": open_count,
            "nc": nc_count,
            "quarantined": len(result.quarantined),
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"Scene GLBs: {len(result.records)} checked")
    typer.echo(
        f"License tiers: {open_count} open, {nc_count} nc; {len(result.quarantined)} quarantined"
    )
    for record in result.records:
        typer.echo(
            f"{record.library_uid}: {record.title} [{record.license}] "
            f"{record.bytes} bytes {record.sha256[:12]}"
        )
