from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from typer.testing import CliRunner

from uefactory.cli.ingest import ingest_app
from uefactory.core.config import Settings
from uefactory.ingest.batch_report import BatchReportArtifacts, BatchReportThumbnail
from uefactory.ingest.executor import IngestResult
from uefactory.ingest.pipeline import BatchAssetResult, BatchIngestResult


def _app(tmp_path: Path) -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def root(ctx: typer.Context) -> None:
        ctx.obj = {"settings": Settings(project_root=tmp_path, data_dir=tmp_path / "data")}

    app.add_typer(ingest_app, name="ingest")
    return app


def test_ingest_asset_cli_emits_json(monkeypatch: Any, tmp_path: Path) -> None:
    source = tmp_path / "source.glb"
    source.write_bytes(b"glb")
    run_dir = tmp_path / "out/ingest/run/test_asset"
    expected = IngestResult(
        run_dir=run_dir,
        manifest_path=run_dir / "manifest.json",
        ue_log_path=run_dir / "ue.log",
        reload_log_path=run_dir / "ue_reload.log",
        finalize_log_path=run_dir / "ue_finalize.log",
        asset_id="test_asset",
        imported_object_paths=("/Game/UEF/Ingested/test_asset/Test.Test",),
        static_mesh_paths=("/Game/UEF/Ingested/test_asset/Test.Test",),
    )
    calls: list[dict[str, Any]] = []

    def fake_ingest_asset(**kwargs: Any) -> IngestResult:
        calls.append(kwargs)
        return expected

    monkeypatch.setattr("uefactory.cli.ingest.ingest_asset", fake_ingest_asset)

    result = CliRunner().invoke(
        _app(tmp_path),
        ["ingest", "asset", str(source), "--id", "test_asset", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["static_mesh_paths"] == list(expected.static_mesh_paths)
    assert calls[0]["asset_id"] == "test_asset"
    assert calls[0]["source_file"] == source


def test_ingest_batch_cli_reports_failed_asset_and_exits_one(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source_manifest = tmp_path / "batch.yaml"
    source_manifest.write_text("assets: []\n", encoding="utf-8")
    run_dir = tmp_path / "out/ingest_batches/run"
    batch = BatchIngestResult(
        status="failed",
        run_dir=run_dir,
        manifest_path=run_dir / "manifest.json",
        catalog_path=tmp_path / "data/catalog.db",
        assets=(
            BatchAssetResult(
                asset_id="bad_asset",
                status="failed",
                bundle_sha256=None,
                content_sha256=None,
                raw_path=None,
                ingest_manifest=None,
                error={"type": "RuntimeError", "message": "fixture failure"},
            ),
        ),
        report_error={
            "type": "RuntimeError",
            "message": "fixture report failure",
            "phase": "batch_report",
        },
    )
    monkeypatch.setattr("uefactory.cli.ingest.ingest_batch", lambda **kwargs: batch)

    result = CliRunner().invoke(
        _app(tmp_path),
        ["ingest", "batch", str(source_manifest), "--json"],
    )
    human = CliRunner().invoke(
        _app(tmp_path),
        ["ingest", "batch", str(source_manifest)],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["assets"][0]["error"]["message"] == "fixture failure"
    assert payload["report"] is None
    assert payload["report_error"]["message"] == "fixture report failure"
    assert human.exit_code == 1
    assert "Report failed: RuntimeError: fixture report failure" in human.stdout


def test_ingest_batch_cli_exposes_report_in_json_and_human_output(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source_manifest = tmp_path / "batch.yaml"
    source_manifest.write_text("assets: []\n", encoding="utf-8")
    run_dir = tmp_path / "out/ingest_batches/run"
    report = BatchReportArtifacts(
        contact_sheet=run_dir / "report/contact_sheet.png",
        index_html=run_dir / "report/index.html",
        thumbnails=(
            BatchReportThumbnail(
                asset_id="test_asset",
                path=run_dir / "report/thumbnails/test_asset.png",
                sha256="1" * 64,
                asset_sheet_path=run_dir / "report/asset_sheets/test_asset.png",
                asset_sheet_sha256="2" * 64,
            ),
        ),
    )
    batch = BatchIngestResult(
        status="ok",
        run_dir=run_dir,
        manifest_path=run_dir / "manifest.json",
        catalog_path=tmp_path / "data/catalog.db",
        assets=(),
        report=report,
    )
    monkeypatch.setattr("uefactory.cli.ingest.ingest_batch", lambda **kwargs: batch)
    runner = CliRunner()

    json_result = runner.invoke(
        _app(tmp_path),
        ["ingest", "batch", str(source_manifest), "--json"],
    )
    human_result = runner.invoke(
        _app(tmp_path),
        ["ingest", "batch", str(source_manifest)],
    )

    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.stdout)
    assert payload["report"] == {
        "contact_sheet": str(report.contact_sheet),
        "index_html": str(report.index_html),
        "thumbnails": [
            {
                "asset_id": "test_asset",
                "path": str(report.thumbnails[0].path),
                "sha256": "1" * 64,
            }
        ],
    }
    assert payload["report_error"] is None
    assert human_result.exit_code == 0, human_result.output
    assert f"Contact sheet: {report.contact_sheet}" in human_result.stdout
    assert f"Index: {report.index_html}" in human_result.stdout


def test_ingest_batch_cli_enables_thumbnails_by_default_and_allows_opt_out(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source_manifest = tmp_path / "batch.yaml"
    source_manifest.write_text("assets: []\n", encoding="utf-8")
    run_dir = tmp_path / "out/ingest_batches/run"
    calls: list[bool] = []

    def fake_batch(**kwargs: Any) -> BatchIngestResult:
        calls.append(bool(kwargs["render_thumbnails"]))
        return BatchIngestResult(
            status="ok",
            run_dir=run_dir,
            manifest_path=run_dir / "manifest.json",
            catalog_path=tmp_path / "data/catalog.db",
            assets=(),
        )

    monkeypatch.setattr("uefactory.cli.ingest.ingest_batch", fake_batch)

    default = CliRunner().invoke(
        _app(tmp_path),
        ["ingest", "batch", str(source_manifest), "--json"],
    )
    opted_out = CliRunner().invoke(
        _app(tmp_path),
        ["ingest", "batch", str(source_manifest), "--no-thumbnails", "--json"],
    )

    assert default.exit_code == 0, default.output
    assert opted_out.exit_code == 0, opted_out.output
    assert calls == [True, False]


def test_ingest_batch_cli_rejects_invalid_manifest(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    from uefactory.ingest.spec import IngestSpecError

    source_manifest = tmp_path / "batch.yaml"
    source_manifest.write_text("assets: []\n", encoding="utf-8")

    def fail(**kwargs: Any) -> None:
        raise IngestSpecError("$.assets: expected at least 1 item(s)")

    monkeypatch.setattr("uefactory.cli.ingest.ingest_batch", fail)

    result = CliRunner().invoke(
        _app(tmp_path),
        ["ingest", "batch", str(source_manifest)],
    )

    assert result.exit_code == 2
    assert "Invalid ingest manifest" in result.output
