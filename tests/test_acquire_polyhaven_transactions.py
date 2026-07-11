from __future__ import annotations

import copy
from pathlib import Path

import pytest

from uefactory.acquire import polyhaven

_TIMESTAMP = "2026-07-11T00:00:00Z"
_RUNTIME_CONFIG = polyhaven.PolyHavenRuntimeConfig().as_dict()


def _runtime_evidence() -> dict[str, object]:
    return {
        "http": {
            "request_attempts": 1,
            "retry_attempts": 0,
            "retry_after_honored": 0,
            "rate_limit_wait_ms": 0,
            "retry_wait_ms": 0,
            "download_body_bytes": 0,
        },
        "daily_quota": {
            "enabled": False,
            "ledger_path": None,
            "utc_day": "2026-07-11",
            "usage_before": {"new_items_reserved": 0, "download_bytes_reserved": 0},
            "reserved_by_run": {"new_items": 0, "download_bytes": 0},
            "accounted_overage_bytes": 0,
            "released_probe_bytes": 0,
            "deferred_new_items": 0,
            "usage_after": {"new_items_reserved": 0, "download_bytes_reserved": 0},
            "item_reservations_after": 0,
            "open_downloads_after": 0,
            "ledger_file_sha256": None,
        },
        "disk": {
            "checks": 0,
            "max_storage_bytes_observed": None,
            "min_free_bytes_observed": None,
        },
    }


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
        "request": {
            "force": False,
            "limit": 1,
            "resolution": "1k",
            "runtime": _RUNTIME_CONFIG,
        },
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
        "request": {
            "force": False,
            "limit": 1,
            "resolution": "1k",
            "runtime": _RUNTIME_CONFIG,
        },
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
        "runtime": _runtime_evidence(),
        "items": [],
    }
    new_manifest["prepare_receipt_sha256"] = polyhaven._prepared_manifest_payload_sha256(
        new_manifest
    )
    receipt = new_manifest["prepare_receipt_sha256"]
    assert isinstance(receipt, str)
    noop_receipts = new_state["noop_run_receipts"]
    assert isinstance(noop_receipts, dict)
    noop_receipts["run"] = receipt
    state_receipt = new_manifest["state"]
    assert isinstance(state_receipt, dict)
    state_receipt["after"] = {
        "file_sha256": polyhaven._json_file_sha256(new_state),
        "payload_sha256": polyhaven._payload_sha256(new_state),
    }
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


def test_legacy_schema_2_noop_receipt_is_rejected_as_unverifiable(tmp_path: Path) -> None:
    state_path, _, _, _ = _write_base_files(tmp_path)
    loaded_state = polyhaven._load_state(state_path, project_root=tmp_path)
    new_state, manifest = _noop_after_payloads(
        tmp_path=tmp_path,
        state_path=state_path,
        loaded_state=loaded_state,
    )
    manifest["schema_version"] = polyhaven.LEGACY_RUN_MANIFEST_SCHEMA_VERSION
    request = manifest["request"]
    assert isinstance(request, dict)
    request.pop("runtime")
    manifest.pop("runtime")
    manifest["prepare_receipt_sha256"] = polyhaven._prepared_manifest_payload_sha256(manifest)

    with pytest.raises(polyhaven.PolyHavenAcquireError, match="schema-2 no-op.*unverifiable"):
        polyhaven._validate_prepared_manifest_receipt(
            manifest=manifest,
            state=new_state,
            project_root=tmp_path,
        )


def test_schema_3_noop_runtime_receipt_cannot_be_recomputed_without_state_anchor(
    tmp_path: Path,
) -> None:
    state_path, _, _, _ = _write_base_files(tmp_path)
    loaded_state = polyhaven._load_state(state_path, project_root=tmp_path)
    new_state, manifest = _noop_after_payloads(
        tmp_path=tmp_path,
        state_path=state_path,
        loaded_state=loaded_state,
    )
    runtime = manifest["runtime"]
    assert isinstance(runtime, dict)
    http = runtime["http"]
    assert isinstance(http, dict)
    http["request_attempts"] = 2
    manifest["prepare_receipt_sha256"] = polyhaven._prepared_manifest_payload_sha256(manifest)

    with pytest.raises(polyhaven.PolyHavenAcquireError, match="not anchored in state"):
        polyhaven._validate_prepared_manifest_receipt(
            manifest=manifest,
            state=new_state,
            project_root=tmp_path,
        )


@pytest.mark.parametrize("mutation", ["integer_float", "float_schema"])
def test_schema_3_receipt_rejects_noncanonical_numeric_representations(
    tmp_path: Path,
    mutation: str,
) -> None:
    state_path, _, _, _ = _write_base_files(tmp_path)
    loaded_state = polyhaven._load_state(state_path, project_root=tmp_path)
    new_state, manifest = _noop_after_payloads(
        tmp_path=tmp_path,
        state_path=state_path,
        loaded_state=loaded_state,
    )
    if mutation == "integer_float":
        request = manifest["request"]
        assert isinstance(request, dict)
        runtime = request["runtime"]
        assert isinstance(runtime, dict)
        runtime["request_rate_per_sec"] = 2
        expected = "configuration is not canonical"
    else:
        manifest["schema_version"] = 3.0
        expected = "schema version is not canonical"

    with pytest.raises(polyhaven.PolyHavenAcquireError, match=expected):
        polyhaven._validate_prepared_manifest_receipt(
            manifest=manifest,
            state=new_state,
            project_root=tmp_path,
        )
