from __future__ import annotations

import copy
from pathlib import Path

import pytest

from uefactory.acquire import polyhaven

_TIMESTAMP = "2026-07-11T00:00:00Z"


def _write_base_files(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    state_path = tmp_path / "data/acquire/polyhaven/state.json"
    intent_path = tmp_path / "data/acquire/polyhaven/commit_intent.json"
    manifest_path = tmp_path / "out/acquire/polyhaven/run/manifest.json"

    base_state = polyhaven._empty_state()
    base_manifest: dict[str, object] = {
        "schema_version": polyhaven.RUN_MANIFEST_SCHEMA_VERSION,
        "source": polyhaven.POLYHAVEN_SOURCE,
        "asset_type": "models",
        "run_id": "run",
        "status": "running",
        "started_at": _TIMESTAMP,
        "request": {"force": False, "limit": 1, "resolution": "1k"},
    }
    polyhaven._write_json_atomic(state_path, base_state)
    polyhaven._write_json_atomic(manifest_path, base_manifest)
    return state_path, intent_path, manifest_path, base_manifest


def _noop_after_payloads(
    *,
    tmp_path: Path,
    state_path: Path,
    loaded_state: polyhaven._LoadedState,
) -> tuple[dict[str, object], dict[str, object]]:
    new_state = copy.deepcopy(loaded_state.payload)
    new_state["updated_at"] = _TIMESTAMP
    new_manifest: dict[str, object] = {
        "schema_version": polyhaven.RUN_MANIFEST_SCHEMA_VERSION,
        "source": polyhaven.POLYHAVEN_SOURCE,
        "asset_type": "models",
        "run_id": "run",
        "status": "noop",
        "started_at": _TIMESTAMP,
        "completed_at": _TIMESTAMP,
        "request": {"force": False, "limit": 1, "resolution": "1k"},
        "listing": {
            "url": polyhaven.POLYHAVEN_MODELS_URL,
            "discovered": 0,
            "payload_sha256": "0" * 64,
            "watermark": None,
        },
        "state": {
            "path": polyhaven._portable_path(state_path, tmp_path),
            "before": {
                "exists": loaded_state.before_exists,
                "file_sha256": loaded_state.before_file_sha256,
                "payload_sha256": loaded_state.before_payload_sha256,
            },
            "after": {
                "file_sha256": polyhaven._json_file_sha256(new_state),
                "payload_sha256": polyhaven._payload_sha256(new_state),
            },
            "migrated_from": loaded_state.migrated_from,
        },
        "generated_ingest_spec": None,
        "counts": {
            "selected": 0,
            "downloaded_files": 0,
            "reused_files": 0,
            "downloaded_bytes": 0,
            "verified_bytes": 0,
        },
        "items": [],
    }
    new_manifest["prepare_receipt_sha256"] = polyhaven._prepared_manifest_payload_sha256(
        new_manifest
    )
    return new_state, new_manifest


@pytest.mark.parametrize(
    ("state_is_after", "manifest_is_after"),
    [(False, False), (True, False), (False, True), (True, True)],
    ids=["intent-only", "state-only", "manifest-only", "both"],
)
def test_commit_intent_reconciliation_converges_from_every_disk_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    state_is_after: bool,
    manifest_is_after: bool,
) -> None:
    state_path, intent_path, manifest_path, _ = _write_base_files(tmp_path)
    loaded_state = polyhaven._load_state(state_path, project_root=tmp_path)
    new_state, new_manifest = _noop_after_payloads(
        tmp_path=tmp_path,
        state_path=state_path,
        loaded_state=loaded_state,
    )

    # Stop immediately after the durable intent write, simulating a process crash.
    with monkeypatch.context() as context:
        context.setattr(polyhaven, "_reconcile_commit_intent", lambda **_: None)
        polyhaven._commit_state_and_manifest(
            intent_path=intent_path,
            state_path=state_path,
            loaded_state=loaded_state,
            new_state=new_state,
            manifest_path=manifest_path,
            expected_manifest_file_sha256=polyhaven._sha256_file(manifest_path),
            new_manifest=new_manifest,
            operation="sync",
            project_root=tmp_path,
        )

    assert intent_path.is_file()
    if state_is_after:
        polyhaven._write_json_atomic(state_path, new_state)
    if manifest_is_after:
        polyhaven._write_json_atomic(manifest_path, new_manifest)

    polyhaven._reconcile_commit_intent(
        intent_path=intent_path,
        state_path=state_path,
        project_root=tmp_path,
    )

    assert not intent_path.exists()
    assert polyhaven._read_json_object_strict(state_path, "test state") == new_state
    assert polyhaven._read_json_object_strict(manifest_path, "test manifest") == new_manifest
