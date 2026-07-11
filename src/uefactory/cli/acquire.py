from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

import typer

from uefactory.acquire.blackmyth import BlackMythLibraryError, scan_blackmyth_scene_library
from uefactory.acquire.hdri import (
    DEFAULT_HDRI_ASSET,
    DEFAULT_HDRI_RESOLUTION,
    acquire_polyhaven_hdri,
)
from uefactory.acquire.models import ModelAcquireError, acquire_m2_models
from uefactory.acquire.polyhaven import (
    DEFAULT_REQUEST_RATE_PER_SEC,
    PolyHavenAcquireError,
    PolyHavenRuntimeConfig,
    PolyHavenSyncResult,
    TerminalStatus,
    finalize_polyhaven_items,
    polyhaven_failure_report,
    sync_polyhaven_models,
)
from uefactory.acquire.polyhaven import (
    DEFAULT_RESOLUTION as DEFAULT_POLYHAVEN_RESOLUTION,
)
from uefactory.acquire.polyhaven_resource_sync import (
    PolyHavenResourceSyncResult,
    sync_polyhaven_resources,
)
from uefactory.acquire.polyhaven_resources import (
    DEFAULT_RESOURCE_RESOLUTION,
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
    request_rate: Annotated[
        float,
        typer.Option(
            "--request-rate",
            min=0.000001,
            max=1_000_000,
            help="Maximum Poly Haven HTTP request rate per second (monotonic).",
        ),
    ] = DEFAULT_REQUEST_RATE_PER_SEC,
    request_burst: Annotated[
        int,
        typer.Option("--request-burst", min=1, max=1_000_000, help="HTTP token bucket burst."),
    ] = 1,
    retry_max_attempts: Annotated[
        int,
        typer.Option("--retry-max-attempts", min=1, help="Transient HTTP/transport attempts."),
    ] = 5,
    integrity_max_attempts: Annotated[
        int,
        typer.Option("--integrity-max-attempts", min=1, help="Checksum attempt budget."),
    ] = 2,
    retry_base_delay_sec: Annotated[
        float,
        typer.Option("--retry-base-sec", min=0.001, help="Initial exponential backoff."),
    ] = 5.0,
    retry_max_delay_sec: Annotated[
        float,
        typer.Option("--retry-max-sec", min=0.001, help="Maximum exponential backoff."),
    ] = 900.0,
    max_retry_after_sec: Annotated[
        float,
        typer.Option("--max-retry-after-sec", min=0.0, help="Maximum honored Retry-After."),
    ] = 3_600.0,
    max_new_items_per_day: Annotated[
        int | None,
        typer.Option("--max-new-items-per-day", min=0, help="Durable UTC-day item quota."),
    ] = None,
    max_download_bytes_per_day: Annotated[
        int | None,
        typer.Option(
            "--max-download-bytes-per-day",
            min=0,
            help="Durable UTC-day worst-case transfer reservation quota.",
        ),
    ] = None,
    max_storage_bytes: Annotated[
        int | None,
        typer.Option(
            "--max-storage-bytes",
            min=0,
            help="Maximum Poly Haven model-tree bytes, including download growth.",
        ),
    ] = None,
    min_free_bytes: Annotated[
        int,
        typer.Option("--min-free-bytes", min=0, help="Free-space floor before downloads."),
    ] = 0,
    cross_run_backoff_base_sec: Annotated[
        float,
        typer.Option(
            "--cross-run-backoff-base-sec",
            min=1.0,
            help="Initial delay before retrying a failed revision in a later run.",
        ),
    ] = 300.0,
    cross_run_backoff_max_sec: Annotated[
        float,
        typer.Option(
            "--cross-run-backoff-max-sec",
            min=1.0,
            help="Maximum cross-run revision retry delay.",
        ),
    ] = 86_400.0,
    integrity_quarantine_after_runs: Annotated[
        int,
        typer.Option(
            "--integrity-quarantine-after-runs",
            min=1,
            help="Run-level integrity failures before exact-revision quarantine.",
        ),
    ] = 3,
    retry_revision: Annotated[
        list[str] | None,
        typer.Option(
            "--retry-revision",
            help="Release one exact failed asset revision for an audited retry; repeatable.",
        ),
    ] = None,
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
        runtime_config = PolyHavenRuntimeConfig(
            request_rate_per_sec=request_rate,
            request_burst=request_burst,
            retry_max_attempts=retry_max_attempts,
            integrity_max_attempts=integrity_max_attempts,
            retry_base_delay_sec=retry_base_delay_sec,
            retry_max_delay_sec=retry_max_delay_sec,
            max_retry_after_sec=max_retry_after_sec,
            max_new_items_per_day=max_new_items_per_day,
            max_download_bytes_per_day=max_download_bytes_per_day,
            max_storage_bytes=max_storage_bytes,
            min_free_bytes=min_free_bytes,
            cross_run_backoff_base_sec=cross_run_backoff_base_sec,
            cross_run_backoff_max_sec=cross_run_backoff_max_sec,
            integrity_quarantine_after_runs=integrity_quarantine_after_runs,
        )
        result = sync_polyhaven_models(
            settings=settings,
            limit=limit,
            resolution=resolution,
            force=force,
            runtime_config=runtime_config,
            retry_revisions=tuple(retry_revision or ()),
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
        elif not (
            _polyhaven_deferred_items(result) or result.deferred or result.failed or result.released
        ):
            typer.echo("No eligible Poly Haven model revisions; source is unchanged")
        if _polyhaven_deferred_items(result):
            typer.echo(
                f"Poly Haven unseen revisions deferred by daily item quota: "
                f"{_polyhaven_deferred_items(result)}"
            )
        if result.deferred:
            typer.echo(
                f"Poly Haven revisions deferred by durable failure policy: {result.deferred}"
            )
        if result.failed:
            typer.echo(
                f"Poly Haven revision failures journaled: {result.failed}; "
                f"quarantined: {result.quarantined}"
            )
        if result.released:
            typer.echo(f"Poly Haven revision failure releases recorded: {result.released}")
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
            "journaled"
            if batch is None and result.generated_spec_path is None and result.failed
            else "deferred"
            if batch is None
            and result.generated_spec_path is None
            and (_polyhaven_deferred_items(result) or result.deferred)
            else "released"
            if batch is None and result.generated_spec_path is None and result.released
            else "noop"
            if batch is None and result.generated_spec_path is None
            else ("prepared" if batch is None else batch.status)
        ),
        "source": "polyhaven",
        "asset_type": "models",
        "discovered": result.discovered,
        "selected": result.selected,
        "attempted": result.attempted,
        "failed": result.failed,
        "deferred": result.deferred,
        "quarantined": result.quarantined,
        "released": result.released,
        "downloaded_files": result.downloaded_files,
        "reused_files": result.reused_files,
        "downloaded_bytes": result.downloaded_bytes,
        "verified_bytes": result.verified_bytes,
        "snapshot_sha256": result.snapshot_sha256,
        "runtime": result.runtime_evidence,
        "run_dir": str(result.run_dir),
        "manifest": str(result.manifest_path),
        "state": str(result.state_path),
        "failure_journal": (
            None if result.failure_journal_path is None else str(result.failure_journal_path)
        ),
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


def _polyhaven_deferred_items(result: PolyHavenSyncResult) -> int:
    runtime = result.runtime_evidence
    if not isinstance(runtime, Mapping):
        return 0
    daily = runtime.get("daily_quota")
    if not isinstance(daily, Mapping):
        return 0
    value = daily.get("deferred_new_items")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


@acquire_app.command("polyhaven-resources")
def acquire_polyhaven_resources(
    ctx: typer.Context,
    kind: Annotated[
        Literal["hdri", "pbr_texture_set"],
        typer.Option("--kind", help="Resource cohort to acquire and publish."),
    ] = "hdri",
    source_id: Annotated[
        list[str] | None,
        typer.Option(
            "--source-id",
            help="Acquire an exact listing-bound source id; repeatable.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=10_000, help="Maximum resource revisions."),
    ] = 1,
    resolution: Annotated[
        str,
        typer.Option("--resolution", help="Exact Poly Haven resolution, e.g. 1k or 2k."),
    ] = DEFAULT_RESOURCE_RESOLUTION,
    force: Annotated[
        bool,
        typer.Option("--force", help="Redownload provider files before exact verification."),
    ] = False,
    database: Annotated[
        Path | None,
        typer.Option("--database", "--db", help="Catalog database to publish into."),
    ] = None,
    request_rate: Annotated[
        float,
        typer.Option(
            "--request-rate",
            min=0.000001,
            max=1_000_000,
            help="Maximum Poly Haven HTTP request rate per second (monotonic).",
        ),
    ] = DEFAULT_REQUEST_RATE_PER_SEC,
    request_burst: Annotated[
        int,
        typer.Option("--request-burst", min=1, max=1_000_000, help="HTTP token bucket burst."),
    ] = 1,
    retry_max_attempts: Annotated[
        int,
        typer.Option("--retry-max-attempts", min=1, help="Transient HTTP/transport attempts."),
    ] = 5,
    integrity_max_attempts: Annotated[
        int,
        typer.Option("--integrity-max-attempts", min=1, help="Checksum attempt budget."),
    ] = 2,
    retry_base_delay_sec: Annotated[
        float,
        typer.Option("--retry-base-sec", min=0.001, help="Initial exponential backoff."),
    ] = 5.0,
    retry_max_delay_sec: Annotated[
        float,
        typer.Option("--retry-max-sec", min=0.001, help="Maximum exponential backoff."),
    ] = 900.0,
    max_retry_after_sec: Annotated[
        float,
        typer.Option("--max-retry-after-sec", min=0.0, help="Maximum honored Retry-After."),
    ] = 3_600.0,
    max_new_items_per_day: Annotated[
        int | None,
        typer.Option("--max-new-items-per-day", min=0, help="Durable UTC-day item quota."),
    ] = None,
    max_download_bytes_per_day: Annotated[
        int | None,
        typer.Option(
            "--max-download-bytes-per-day",
            min=0,
            help="Durable UTC-day worst-case transfer reservation quota.",
        ),
    ] = None,
    max_storage_bytes: Annotated[
        int | None,
        typer.Option(
            "--max-storage-bytes",
            min=0,
            help="Maximum shared Poly Haven acquisition-tree bytes.",
        ),
    ] = None,
    min_free_bytes: Annotated[
        int,
        typer.Option("--min-free-bytes", min=0, help="Free-space floor before downloads."),
    ] = 0,
    cross_run_backoff_base_sec: Annotated[
        float,
        typer.Option(
            "--cross-run-backoff-base-sec",
            min=1.0,
            help="Initial delay before retrying a failed resource revision.",
        ),
    ] = 300.0,
    cross_run_backoff_max_sec: Annotated[
        float,
        typer.Option(
            "--cross-run-backoff-max-sec",
            min=1.0,
            help="Maximum cross-run resource retry delay.",
        ),
    ] = 86_400.0,
    integrity_quarantine_after_runs: Annotated[
        int,
        typer.Option(
            "--integrity-quarantine-after-runs",
            min=1,
            help="Run-level integrity failures before exact-revision quarantine.",
        ),
    ] = 3,
    retry_revision: Annotated[
        list[str] | None,
        typer.Option(
            "--retry-revision",
            help="Release one exact failed resource id for an audited retry; repeatable.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a machine-readable resource run receipt."),
    ] = False,
) -> None:
    """Incrementally acquire, validate, and atomically catalog Poly Haven resources."""

    settings = settings_from_context(ctx)
    try:
        runtime_config = PolyHavenRuntimeConfig(
            request_rate_per_sec=request_rate,
            request_burst=request_burst,
            retry_max_attempts=retry_max_attempts,
            integrity_max_attempts=integrity_max_attempts,
            retry_base_delay_sec=retry_base_delay_sec,
            retry_max_delay_sec=retry_max_delay_sec,
            max_retry_after_sec=max_retry_after_sec,
            max_new_items_per_day=max_new_items_per_day,
            max_download_bytes_per_day=max_download_bytes_per_day,
            max_storage_bytes=max_storage_bytes,
            min_free_bytes=min_free_bytes,
            cross_run_backoff_base_sec=cross_run_backoff_base_sec,
            cross_run_backoff_max_sec=cross_run_backoff_max_sec,
            integrity_quarantine_after_runs=integrity_quarantine_after_runs,
        )
        result = sync_polyhaven_resources(
            settings=settings,
            kind=kind,
            limit=limit,
            resolution=resolution,
            source_ids=tuple(source_id or ()),
            force=force,
            runtime_config=runtime_config,
            database_path=database,
            retry_revisions=tuple(retry_revision or ()),
        )
    except Exception as exc:
        typer.echo(f"Poly Haven resource acquisition failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    payload = _polyhaven_resource_payload(result, settings.project_root)
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        counts = payload["counts"]
        assert isinstance(counts, Mapping)
        typer.echo(
            f"Poly Haven resources {result.status}: {result.kind}; "
            f"{counts['ready']} ready, {counts['skipped']} skipped, "
            f"{counts['failed']} failed, {counts['deferred']} deferred"
        )
        if result.status == "partial":
            typer.echo("Partial run: at least one resource completed and at least one did not")
        typer.echo(f"Run: {result.run_id}")
        typer.echo(f"Acquisition manifest: {result.manifest_path}")
        typer.echo(f"Catalog: {result.catalog_path}")
    if result.status == "failed":
        raise typer.Exit(1)


def _polyhaven_resource_payload(
    result: PolyHavenResourceSyncResult,
    project_root: Path,
) -> dict[str, object]:
    receipt = result.as_dict(project_root=project_root.resolve())
    return {
        "source": "polyhaven",
        "asset_type": result.kind,
        "resolution": result.resolution,
        "run_id": result.run_id,
        "status": result.status,
        "counts": receipt["counts"],
        "listing_sha256": result.listing_sha256,
        "manifest": receipt["manifest_path"],
        "state": receipt["state_path"],
        "failure_journal": receipt["failure_journal_path"],
        "catalog": receipt["catalog_path"],
        "items": receipt["items"],
    }


@acquire_app.command("polyhaven-failures")
def acquire_polyhaven_failures(
    ctx: typer.Context,
    status: Annotated[
        Literal["active", "quarantined", "all"],
        typer.Option("--status", help="Failure records to display."),
    ] = "active",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the validated journal view as JSON."),
    ] = False,
) -> None:
    """Inspect durable Poly Haven revision failures without mutating the journal."""

    try:
        payload = polyhaven_failure_report(
            settings=settings_from_context(ctx),
            status=status,
        )
    except PolyHavenAcquireError as exc:
        typer.echo(f"Poly Haven failure journal is unavailable: {exc}", err=True)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(
        f"Poly Haven failures ({status}): {payload['active_count']} active; "
        f"{payload['event_count']} journal events"
    )
    for record in payload["active"]:
        typer.echo(
            f"{record['asset_id']}: {record['disposition']} "
            f"{record['failure']['kind']} ({record['failure']['phase']})"
        )
    if status == "all":
        for event in payload["events"]:
            details = ""
            if event["type"] == "failed":
                details = (
                    f" {event['disposition']} {event['failure']['kind']} "
                    f"({event['failure']['phase']})"
                )
            elif event["type"] == "released":
                details = f" ({event['reason']})"
            typer.echo(f"event {event['sequence']}: {event['type']} {event['asset_id']}{details}")


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
