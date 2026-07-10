from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from typer.testing import CliRunner

from uefactory.acquire.models import (
    AcquiredModel,
    ModelAcquireError,
    ModelAcquireResult,
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


def test_acquire_models_cli_emits_json(monkeypatch: Any, tmp_path: Path) -> None:
    root_dir = tmp_path / "data/m2_samples"
    model_dir = root_dir / "fixture_model"
    result = ModelAcquireResult(
        root_dir=root_dir,
        inventory_path=root_dir / "inventory.json",
        models=(
            AcquiredModel(
                asset_id="fixture_model",
                main_path=model_dir / "fixture.glb",
                dependency_paths=(),
                metadata_path=model_dir / "metadata.json",
                downloaded_files=1,
                reused_files=0,
                bytes=42,
            ),
        ),
        downloaded_files=1,
        reused_files=0,
        bytes=42,
    )
    calls: list[dict[str, Any]] = []

    def fake_acquire(**kwargs: Any) -> ModelAcquireResult:
        calls.append(kwargs)
        return result

    monkeypatch.setattr("uefactory.cli.acquire.acquire_m2_models", fake_acquire)

    invocation = CliRunner().invoke(_app(tmp_path), ["acquire", "models", "--json"])

    assert invocation.exit_code == 0, invocation.output
    assert json.loads(invocation.stdout) == {
        "root_dir": str(root_dir),
        "inventory_path": str(root_dir / "inventory.json"),
        "models": 1,
        "downloaded_files": 1,
        "reused_files": 0,
        "bytes": 42,
    }
    assert calls[0]["force"] is False


def test_acquire_models_cli_reports_failure(monkeypatch: Any, tmp_path: Path) -> None:
    def fail(**kwargs: Any) -> None:
        raise ModelAcquireError("fixture mismatch")

    monkeypatch.setattr("uefactory.cli.acquire.acquire_m2_models", fail)

    invocation = CliRunner().invoke(_app(tmp_path), ["acquire", "models"])

    assert invocation.exit_code == 1
    assert "Model acquisition failed: fixture mismatch" in invocation.output
