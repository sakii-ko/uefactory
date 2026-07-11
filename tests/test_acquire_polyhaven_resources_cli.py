from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from typer.testing import CliRunner

from uefactory.acquire.polyhaven import PolyHavenRuntimeConfig
from uefactory.acquire.polyhaven_resource_sync import (
    PolyHavenResourceSyncItem,
    PolyHavenResourceSyncResult,
)
from uefactory.cli.acquire import acquire_app
from uefactory.core.config import Settings


def _app(tmp_path: Path) -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def root(ctx: typer.Context) -> None:
        ctx.obj = {"settings": Settings(project_root=tmp_path, data_dir=tmp_path / "data")}

    app.add_typer(acquire_app, name="acquire")
    return app


def _sync_result(
    tmp_path: Path,
    *,
    status: str = "ready",
    item_statuses: tuple[str, ...] = ("ready",),
) -> PolyHavenResourceSyncResult:
    items = tuple(
        PolyHavenResourceSyncItem(
            resource_id=f"polyhaven_pbr_fixture_{index}_" + chr(97 + index) * 32,
            kind="pbr_texture_set",
            source_id=f"fixture_{index}",
            revision=chr(98 + index) * 40,
            resolution="2k",
            status=item_status,  # type: ignore[arg-type]
            root_dir=tmp_path / f"data/acquire/polyhaven/resources/pbr/{index}",
            downloaded_files=3 if item_status == "ready" else 0,
            reused_files=0,
            downloaded_bytes=300 if item_status == "ready" else 0,
            verified_bytes=300 if item_status == "ready" else 0,
            bundle_sha256="d" * 64 if item_status == "ready" else None,
            content_sha256="e" * 64 if item_status == "ready" else None,
            error=(
                {"type": "FixtureError", "message": "fixture failure"}
                if item_status == "failed"
                else None
            ),
        )
        for index, item_status in enumerate(item_statuses)
    )
    return PolyHavenResourceSyncResult(
        kind="pbr_texture_set",
        resolution="2k",
        run_id="20260711T120000Z_fixture",
        status=status,  # type: ignore[arg-type]
        manifest_path=(
            tmp_path / "out/acquire/polyhaven-resources/pbr_texture_set/run/manifest.json"
        ),
        state_path=(tmp_path / "data/acquire/polyhaven/resources/pbr_texture_set/state.json"),
        failure_journal_path=(
            tmp_path / "data/acquire/polyhaven/resources/pbr_texture_set/failure_journal.json"
        ),
        catalog_path=tmp_path / "data/resources.db",
        listing_sha256="f" * 64,
        items=items,
    )


def test_polyhaven_resources_cli_json_forwards_runtime_and_selection_options(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    result = _sync_result(tmp_path, item_statuses=("ready", "skipped"))
    calls: list[dict[str, Any]] = []

    def fake_sync(**kwargs: Any) -> PolyHavenResourceSyncResult:
        calls.append(kwargs)
        return result

    monkeypatch.setattr("uefactory.cli.acquire.sync_polyhaven_resources", fake_sync)
    invocation = CliRunner().invoke(
        _app(tmp_path),
        [
            "acquire",
            "polyhaven-resources",
            "--kind",
            "pbr_texture_set",
            "--source-id",
            "fixture_0",
            "--source-id",
            "fixture_1",
            "--limit",
            "2",
            "--resolution",
            "2k",
            "--force",
            "--database",
            "data/resources.db",
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
            "polyhaven_pbr_bad_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "--json",
        ],
    )

    assert invocation.exit_code == 0, invocation.output
    payload = json.loads(invocation.stdout)
    assert payload["run_id"] == result.run_id
    assert payload["status"] == "ready"
    assert payload["counts"] == {"ready": 1, "skipped": 1, "failed": 0, "deferred": 0}
    assert payload["manifest"].endswith("manifest.json")
    assert payload["catalog"] == "data/resources.db"
    assert calls == [
        {
            "settings": calls[0]["settings"],
            "kind": "pbr_texture_set",
            "limit": 2,
            "resolution": "2k",
            "source_ids": ("fixture_0", "fixture_1"),
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
            "database_path": Path("data/resources.db"),
            "retry_revisions": ("polyhaven_pbr_bad_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
        }
    ]


def test_polyhaven_resources_cli_failed_run_emits_receipt_and_is_nonzero(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    result = _sync_result(tmp_path, status="failed", item_statuses=("failed",))
    monkeypatch.setattr(
        "uefactory.cli.acquire.sync_polyhaven_resources",
        lambda **_kwargs: result,
    )

    invocation = CliRunner().invoke(
        _app(tmp_path),
        ["acquire", "polyhaven-resources", "--kind", "pbr_texture_set", "--json"],
    )

    assert invocation.exit_code == 1
    payload = json.loads(invocation.stdout)
    assert payload["status"] == "failed"
    assert payload["counts"]["failed"] == 1
    assert payload["items"][0]["error"]["message"] == "fixture failure"


def test_polyhaven_resources_cli_partial_run_is_explicit_in_human_output(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    result = _sync_result(
        tmp_path,
        status="partial",
        item_statuses=("ready", "failed"),
    )
    monkeypatch.setattr(
        "uefactory.cli.acquire.sync_polyhaven_resources",
        lambda **_kwargs: result,
    )

    invocation = CliRunner().invoke(
        _app(tmp_path),
        ["acquire", "polyhaven-resources", "--kind", "pbr_texture_set", "--limit", "2"],
    )

    assert invocation.exit_code == 0, invocation.output
    assert "Poly Haven resources partial: pbr_texture_set" in invocation.output
    assert "1 ready, 0 skipped, 1 failed, 0 deferred" in invocation.output
    assert "Partial run:" in invocation.output
    assert f"Run: {result.run_id}" in invocation.output
    assert f"Acquisition manifest: {result.manifest_path}" in invocation.output
    assert f"Catalog: {result.catalog_path}" in invocation.output


def test_polyhaven_resources_cli_reports_sync_exception_without_traceback(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    def fail(**_kwargs: Any) -> None:
        raise RuntimeError("fixture catalog conflict")

    monkeypatch.setattr("uefactory.cli.acquire.sync_polyhaven_resources", fail)

    invocation = CliRunner().invoke(
        _app(tmp_path),
        ["acquire", "polyhaven-resources"],
    )

    assert invocation.exit_code == 1
    assert "Poly Haven resource acquisition failed: fixture catalog conflict" in invocation.output


def test_hdri_cli_keeps_legacy_three_line_human_output(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "data/hdri/studio_small_03_1k.hdr"
    metadata_path = file_path.with_suffix(".json")
    monkeypatch.setattr(
        "uefactory.cli.acquire.acquire_polyhaven_hdri",
        lambda **_kwargs: type(
            "FixtureHdriResult",
            (),
            {
                "file_path": file_path,
                "metadata_path": metadata_path,
                "license": "CC0",
                "skipped": True,
            },
        )(),
    )

    invocation = CliRunner().invoke(_app(tmp_path), ["acquire", "hdri"])

    assert invocation.exit_code == 0, invocation.output
    assert invocation.output.splitlines() == [
        f"HDRI reused: {file_path}",
        f"Metadata: {metadata_path}",
        "License: CC0",
    ]
