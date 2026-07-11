from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import typer
from typer.testing import CliRunner

from uefactory.acquire.polyhaven import (
    PolyHavenAcquireError,
    PolyHavenRuntimeConfig,
    PolyHavenSyncItem,
    PolyHavenSyncResult,
)
from uefactory.cli.acquire import acquire_app
from uefactory.core.config import Settings
from uefactory.ingest.pipeline import BatchAssetResult, BatchIngestResult


def _app(tmp_path: Path) -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def root(ctx: typer.Context) -> None:
        ctx.obj = {"settings": Settings(project_root=tmp_path, data_dir=tmp_path / "data")}

    app.add_typer(acquire_app, name="acquire")
    return app


def _sync_result(tmp_path: Path, *, count: int = 1) -> PolyHavenSyncResult:
    run_dir = tmp_path / "out/acquire/polyhaven/run"
    items = tuple(
        PolyHavenSyncItem(
            asset_id=f"polyhaven_fixture_{index}_{'a' * 12}",
            source_id=f"fixture_{index}",
            revision="a" * 40,
            root_dir=tmp_path / f"data/acquire/polyhaven/models/fixture_{index}",
            main_path=tmp_path / f"data/acquire/polyhaven/models/fixture_{index}/model.gltf",
            dependency_paths=(
                tmp_path / f"data/acquire/polyhaven/models/fixture_{index}/model.bin",
            ),
            metadata_path=(
                tmp_path / f"data/acquire/polyhaven/models/fixture_{index}/metadata.json"
            ),
            downloaded_files=2,
            reused_files=0,
            downloaded_bytes=42,
            verified_bytes=42,
            source_bundle_sha256="c" * 64,
            source_content_sha256="d" * 64,
            acquired_at="2026-07-10T12:00:00Z",
            verified_at="2026-07-10T12:00:00Z",
            state_status="downloaded",
        )
        for index in range(count)
    )
    return PolyHavenSyncResult(
        run_dir=run_dir,
        manifest_path=run_dir / "manifest.json",
        state_path=tmp_path / "data/acquire/polyhaven/state.json",
        generated_spec_path=(run_dir / "generated_ingest.yaml" if count else None),
        items=items,
        discovered=521,
        selected=count,
        downloaded_files=2 * count,
        reused_files=0,
        downloaded_bytes=42 * count,
        verified_bytes=42 * count,
        snapshot_sha256="b" * 64,
        attempted=count,
        failure_journal_path=tmp_path / "data/acquire/polyhaven/failure_journal.json",
    )


def test_polyhaven_cli_download_only_emits_json_and_forwards_options(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    result = _sync_result(tmp_path, count=2)
    calls: list[dict[str, Any]] = []

    def fake_sync(**kwargs: Any) -> PolyHavenSyncResult:
        calls.append(kwargs)
        return result

    monkeypatch.setattr("uefactory.cli.acquire.sync_polyhaven_models", fake_sync)

    invocation = CliRunner().invoke(
        _app(tmp_path),
        [
            "acquire",
            "polyhaven",
            "--limit",
            "2",
            "--resolution",
            "2k",
            "--force",
            "--request-rate",
            "4",
            "--request-burst",
            "2",
            "--retry-max-attempts",
            "3",
            "--integrity-max-attempts",
            "1",
            "--retry-base-sec",
            "0.5",
            "--retry-max-sec",
            "4",
            "--max-retry-after-sec",
            "10",
            "--max-new-items-per-day",
            "5",
            "--max-download-bytes-per-day",
            "1000",
            "--max-storage-bytes",
            "2000",
            "--min-free-bytes",
            "100",
            "--cross-run-backoff-base-sec",
            "12",
            "--cross-run-backoff-max-sec",
            "34",
            "--integrity-quarantine-after-runs",
            "4",
            "--retry-revision",
            "polyhaven_bad_aaaaaaaaaaaa",
            "--download-only",
            "--json",
        ],
    )

    assert invocation.exit_code == 0, invocation.output
    payload = json.loads(invocation.stdout)
    assert payload["status"] == "prepared"
    assert payload["selected"] == 2
    assert payload["ingest"] is None
    assert [item["state_status"] for item in payload["items"]] == [
        "downloaded",
        "downloaded",
    ]
    assert calls == [
        {
            "settings": calls[0]["settings"],
            "limit": 2,
            "resolution": "2k",
            "force": True,
            "runtime_config": PolyHavenRuntimeConfig(
                request_rate_per_sec=4,
                request_burst=2,
                retry_max_attempts=3,
                integrity_max_attempts=1,
                retry_base_delay_sec=0.5,
                retry_max_delay_sec=4,
                max_retry_after_sec=10,
                max_new_items_per_day=5,
                max_download_bytes_per_day=1_000,
                max_storage_bytes=2_000,
                min_free_bytes=100,
                cross_run_backoff_base_sec=12,
                cross_run_backoff_max_sec=34,
                integrity_quarantine_after_runs=4,
            ),
            "retry_revisions": ("polyhaven_bad_aaaaaaaaaaaa",),
        }
    ]


def test_polyhaven_cli_ingests_and_finalizes_only_terminal_assets(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    result = _sync_result(tmp_path, count=2)
    first, second = result.items
    batch = BatchIngestResult(
        status="ok",
        run_dir=tmp_path / "out/ingest_batches/run",
        manifest_path=tmp_path / "out/ingest_batches/run/manifest.json",
        catalog_path=tmp_path / "data/catalog_m3.db",
        assets=(
            BatchAssetResult(
                asset_id=first.asset_id,
                status="render_ok",
                bundle_sha256="c" * 64,
                content_sha256="d" * 64,
                raw_path=tmp_path / "data/raw/first/model.gltf",
                ingest_manifest=tmp_path / "out/ingest/first/manifest.json",
                catalog_status="render_ok",
            ),
            BatchAssetResult(
                asset_id=second.asset_id,
                status="skipped",
                bundle_sha256="e" * 64,
                content_sha256="f" * 64,
                raw_path=tmp_path / "data/raw/second/model.gltf",
                ingest_manifest=None,
                catalog_status="render_ok",
            ),
        ),
    )
    ingest_calls: list[dict[str, Any]] = []
    finalize_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "uefactory.cli.acquire.sync_polyhaven_models",
        lambda **_kwargs: result,
    )

    def fake_ingest(**kwargs: Any) -> BatchIngestResult:
        ingest_calls.append(kwargs)
        return batch

    def fake_finalize(**kwargs: Any) -> dict[str, str]:
        finalize_calls.append(kwargs)
        return {first.asset_id: "render_ok", second.asset_id: "skipped"}

    monkeypatch.setattr("uefactory.cli.acquire.ingest_batch", fake_ingest)
    monkeypatch.setattr("uefactory.cli.acquire.finalize_polyhaven_items", fake_finalize)

    invocation = CliRunner().invoke(
        _app(tmp_path),
        [
            "acquire",
            "polyhaven",
            "--limit",
            "2",
            "--ingest",
            "--database",
            "data/catalog_m3.db",
            "--timeout-sec",
            "90",
            "--json",
        ],
    )

    assert invocation.exit_code == 0, invocation.output
    payload = json.loads(invocation.stdout)
    assert payload["status"] == "ok"
    assert [item["terminal_status"] for item in payload["ingest"]["assets"]] == [
        "render_ok",
        "skipped",
    ]
    assert ingest_calls[0]["manifest_path"] == result.generated_spec_path
    assert ingest_calls[0]["database_path"] == Path("data/catalog_m3.db")
    assert ingest_calls[0]["timeout_sec"] == 90
    assert ingest_calls[0]["render_thumbnails"] is True
    assert finalize_calls == [{"result": result, "batch_manifest_path": batch.manifest_path}]


def test_polyhaven_cli_records_failed_batch_without_finalizing_failed_item(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    result = _sync_result(tmp_path)
    item = result.items[0]
    batch = BatchIngestResult(
        status="failed",
        run_dir=tmp_path / "out/ingest_batches/failed",
        manifest_path=tmp_path / "out/ingest_batches/failed/manifest.json",
        catalog_path=tmp_path / "data/catalog_m3.db",
        assets=(
            BatchAssetResult(
                asset_id=item.asset_id,
                status="failed",
                bundle_sha256=None,
                content_sha256=None,
                raw_path=None,
                ingest_manifest=None,
                error={"type": "RuntimeError", "message": "fixture failure"},
            ),
        ),
    )
    finalize_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "uefactory.cli.acquire.sync_polyhaven_models",
        lambda **_kwargs: result,
    )
    monkeypatch.setattr("uefactory.cli.acquire.ingest_batch", lambda **_kwargs: batch)

    def fake_finalize(**kwargs: Any) -> dict[str, str]:
        finalize_calls.append(kwargs)
        return {}

    monkeypatch.setattr("uefactory.cli.acquire.finalize_polyhaven_items", fake_finalize)

    invocation = CliRunner().invoke(
        _app(tmp_path),
        ["acquire", "polyhaven", "--ingest", "--json"],
    )

    assert invocation.exit_code == 1
    assert json.loads(invocation.stdout)["status"] == "failed"
    assert finalize_calls == [{"result": result, "batch_manifest_path": batch.manifest_path}]


def test_polyhaven_cli_noop_does_not_invoke_ingest(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    result = _sync_result(tmp_path, count=0)
    monkeypatch.setattr(
        "uefactory.cli.acquire.sync_polyhaven_models",
        lambda **_kwargs: result,
    )

    def unexpected_ingest(**_kwargs: Any) -> None:
        raise AssertionError("no-change sync must not invoke ingest")

    monkeypatch.setattr("uefactory.cli.acquire.ingest_batch", unexpected_ingest)

    invocation = CliRunner().invoke(
        _app(tmp_path),
        ["acquire", "polyhaven", "--ingest", "--json"],
    )

    assert invocation.exit_code == 0, invocation.output
    payload = json.loads(invocation.stdout)
    assert payload["status"] == "noop"
    assert payload["selected"] == 0
    assert payload["generated_ingest_spec"] is None
    assert payload["ingest"] is None


def test_polyhaven_cli_reports_journaled_revision_failures(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    result = replace(
        _sync_result(tmp_path, count=0),
        attempted=1,
        failed=1,
        quarantined=1,
    )
    monkeypatch.setattr(
        "uefactory.cli.acquire.sync_polyhaven_models",
        lambda **_kwargs: result,
    )

    invocation = CliRunner().invoke(
        _app(tmp_path),
        ["acquire", "polyhaven", "--download-only", "--json"],
    )

    assert invocation.exit_code == 0, invocation.output
    payload = json.loads(invocation.stdout)
    assert payload["status"] == "journaled"
    assert payload["attempted"] == 1
    assert payload["failed"] == 1
    assert payload["deferred"] == 0
    assert payload["quarantined"] == 1
    assert payload["failure_journal"].endswith("failure_journal.json")


def test_polyhaven_failure_report_cli_is_read_only_and_json_serializable(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_report(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "schema_version": 1,
            "source": "polyhaven",
            "asset_type": "models",
            "status_filter": "all",
            "journal_path": str(tmp_path / "data/acquire/polyhaven/failure_journal.json"),
            "event_count": 1,
            "head_event_sha256": "a" * 64,
            "active_count": 1,
            "active": [
                {
                    "asset_id": "polyhaven_bad_aaaaaaaaaaaa",
                    "disposition": "quarantined",
                    "failure": {"kind": "http_permanent", "phase": "api"},
                }
            ],
            "events": [{"type": "failed"}],
        }

    monkeypatch.setattr("uefactory.cli.acquire.polyhaven_failure_report", fake_report)
    invocation = CliRunner().invoke(
        _app(tmp_path),
        ["acquire", "polyhaven-failures", "--status", "all", "--json"],
    )

    assert invocation.exit_code == 0, invocation.output
    payload = json.loads(invocation.stdout)
    assert payload["event_count"] == 1
    assert payload["active"][0]["disposition"] == "quarantined"
    assert calls == [{"settings": calls[0]["settings"], "status": "all"}]


def test_polyhaven_failure_report_human_all_lists_complete_event_history(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    asset_id = "polyhaven_bad_aaaaaaaaaaaa"
    monkeypatch.setattr(
        "uefactory.cli.acquire.polyhaven_failure_report",
        lambda **_kwargs: {
            "schema_version": 1,
            "source": "polyhaven",
            "asset_type": "models",
            "status_filter": "all",
            "journal_path": str(tmp_path / "data/acquire/polyhaven/failure_journal.json"),
            "event_count": 3,
            "head_event_sha256": "a" * 64,
            "active_count": 0,
            "active": [],
            "events": [
                {
                    "sequence": 1,
                    "type": "failed",
                    "asset_id": asset_id,
                    "disposition": "backoff",
                    "failure": {"kind": "transport", "phase": "download"},
                },
                {"sequence": 2, "type": "resolved", "asset_id": asset_id},
                {
                    "sequence": 3,
                    "type": "released",
                    "asset_id": asset_id,
                    "reason": "operator_requested_exact_revision_retry",
                },
            ],
        },
    )

    invocation = CliRunner().invoke(
        _app(tmp_path),
        ["acquire", "polyhaven-failures", "--status", "all"],
    )

    assert invocation.exit_code == 0, invocation.output
    assert f"event 1: failed {asset_id} backoff transport (download)" in invocation.output
    assert f"event 2: resolved {asset_id}" in invocation.output
    assert (
        f"event 3: released {asset_id} (operator_requested_exact_revision_retry)"
        in invocation.output
    )


def test_polyhaven_cli_reports_acquisition_failure(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    def fail(**_kwargs: Any) -> None:
        raise PolyHavenAcquireError("fixture listing drift")

    monkeypatch.setattr("uefactory.cli.acquire.sync_polyhaven_models", fail)

    invocation = CliRunner().invoke(_app(tmp_path), ["acquire", "polyhaven"])

    assert invocation.exit_code == 1
    assert "Poly Haven acquisition failed: fixture listing drift" in invocation.output
