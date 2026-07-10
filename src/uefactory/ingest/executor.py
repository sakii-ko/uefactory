from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from uefactory.core.config import Settings
from uefactory.core.identity import validate_asset_id
from uefactory.core.ingest_contracts import (
    FBX_GLASS_OPACITY,
    FBX_GLASS_OVERRIDE_POLICY,
    FBX_MATERIAL_POSTPROCESS_POLICY,
    IMPORT_MANIFEST_SCHEMA_VERSION,
)
from uefactory.core.paths import utc_timestamp
from uefactory.ingest.quality import IngestQualityError, require_static_mesh_quality
from uefactory.ingest.source_structure import (
    SourceStructureEvidence,
    inspect_source_structure,
)
from uefactory.ingest.staging import bundle_sha256, content_sha256
from uefactory.render.smoke import _prepend_env_path, _runtime_settings
from uefactory.render.ue_runner import UERunnerError, run_ue


@dataclass(frozen=True)
class IngestResult:
    run_dir: Path
    manifest_path: Path
    ue_log_path: Path
    reload_log_path: Path
    finalize_log_path: Path
    asset_id: str
    imported_object_paths: tuple[str, ...]
    static_mesh_paths: tuple[str, ...]


class IngestExecutionError(RuntimeError):
    """Carries the durable failure manifest created by an ingest attempt."""

    def __init__(self, *, manifest_path: Path, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.manifest_path = manifest_path
        self.cause_type = type(cause).__name__


@dataclass(frozen=True)
class _HashSnapshot:
    source_sha256: str
    bundle_sha256: str
    content_sha256: str


_NORMALIZATION_KEYS = {
    "source_units",
    "source_up_axis",
    "source_handedness",
    "uniform_scale",
    "pivot_policy",
}
_DEFAULT_REQUESTED_NORMALIZATION: dict[str, str | float] = {
    "source_units": "auto",
    "source_up_axis": "auto",
    "source_handedness": "auto",
    "uniform_scale": 1.0,
    "pivot_policy": "preserve_source",
}


def _requested_normalization_payload(
    value: Mapping[str, Any] | None,
) -> dict[str, str | float]:
    if value is None:
        return dict(_DEFAULT_REQUESTED_NORMALIZATION)
    if set(value) != _NORMALIZATION_KEYS:
        raise ValueError(
            "requested_normalization requires exactly: " + ", ".join(sorted(_NORMALIZATION_KEYS))
        )
    expected_strings = {
        "source_units": "auto",
        "source_up_axis": "auto",
        "source_handedness": "auto",
        "pivot_policy": "preserve_source",
    }
    for field, expected in expected_strings.items():
        if value.get(field) != expected:
            raise ValueError(f"requested_normalization.{field} must be {expected!r}")
    raw_scale = value.get("uniform_scale")
    if (
        isinstance(raw_scale, bool)
        or not isinstance(raw_scale, int | float)
        or not math.isfinite(float(raw_scale))
        or not 0.0001 <= float(raw_scale) <= 10_000.0
    ):
        raise ValueError(
            "requested_normalization.uniform_scale must be finite and in [0.0001, 10000.0]"
        )
    return {
        **expected_strings,
        "uniform_scale": float(raw_scale),
    }


def ingest_asset(
    *,
    settings: Settings,
    asset_id: str,
    source_file: Path,
    out_root: Path | None = None,
    timeout_sec: int = 1800,
    bundle_root: Path | None = None,
    bundle_files: Sequence[Path] | None = None,
    expected_bundle_sha256: str | None = None,
    expected_content_sha256: str | None = None,
    require_single_static_mesh: bool = True,
    require_texture_references: bool = False,
    requested_normalization: Mapping[str, Any] | None = None,
    expected_source_structure: Mapping[str, Any] | None = None,
    expected_source_structure_sha256: str | None = None,
) -> IngestResult:
    unresolved_source = source_file.expanduser()
    if unresolved_source.is_symlink():
        raise ValueError(f"Ingest source file may not be a symbolic link: {unresolved_source}")
    source_file = unresolved_source.resolve()
    if not source_file.is_file():
        raise FileNotFoundError(f"Ingest source file not found: {source_file}")
    if source_file.suffix.lower() not in {".fbx", ".gltf", ".glb"}:
        raise ValueError(f"Unsupported ingest source format: {source_file.suffix}")
    validate_asset_id(asset_id)
    if timeout_sec <= 0:
        raise ValueError("timeout_sec must be positive")
    normalization = _requested_normalization_payload(requested_normalization)
    source_structure = _resolve_source_structure(
        source_file,
        expected_payload=expected_source_structure,
        expected_sha256=expected_source_structure_sha256,
    )

    hash_root, hash_files = _normalize_bundle_inputs(
        source_file=source_file,
        bundle_root=bundle_root,
        bundle_files=bundle_files,
    )
    initial_hashes = _hash_snapshot(source_file, hash_root, hash_files)
    _assert_source_structure_unchanged(source_structure, source_file)
    _assert_expected_hashes(
        initial_hashes,
        expected_bundle_sha256=expected_bundle_sha256,
        expected_content_sha256=expected_content_sha256,
    )

    project_path = settings.project_root / "ue/UEFBase/UEFBase.uproject"
    script_path = settings.project_root / "ue/UEFBase/Content/Python/uef_ingest_asset.py"
    for label, path in {
        "UE project": project_path,
        "UE ingest script": script_path,
    }.items():
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")

    run_id = f"{utc_timestamp()}_{uuid4().hex[:8]}"
    root = out_root or settings.project_root / "out/ingest"
    run_dir = root / run_id / asset_id
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = run_dir / "manifest.json"
    ue_log_path = run_dir / "ue.log"
    job_path = run_dir / "job.json"
    reload_manifest_path = run_dir / "reload_manifest.json"
    reload_log_path = run_dir / "ue_reload.log"
    reload_job_path = run_dir / "reload_job.json"
    finalize_manifest_path = run_dir / "finalize_manifest.json"
    finalize_log_path = run_dir / "ue_finalize.log"
    finalize_job_path = run_dir / "finalize_job.json"
    finalize_retry_manifest_path = run_dir / "finalize_retry_manifest.json"
    finalize_retry_log_path = run_dir / "ue_finalize_retry.log"
    finalize_retry_job_path = run_dir / "finalize_retry_job.json"
    inspect_manifest_path = run_dir / "inspect_manifest.json"
    inspect_log_path = run_dir / "ue_inspect.log"
    inspect_job_path = run_dir / "inspect_job.json"

    ddc_dir = settings.ddc_dir or settings.data_dir / "ddc"
    ddc_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = settings.data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    settings.ue_home.mkdir(parents=True, exist_ok=True)
    runtime = _runtime_settings(settings.runtime_lib_dir)
    env = {
        "HOME": str(settings.ue_home),
        "TMPDIR": str(tmp_dir),
        "UEF_JOB_FILE": str(job_path),
        "UE-LocalDataCachePath": str(ddc_dir),
    }
    if runtime["enabled"]:
        env["LD_LIBRARY_PATH"] = _prepend_env_path(
            Path(str(runtime["lib_dir"])),
            "LD_LIBRARY_PATH",
        )
    command: list[str | Path] = [
        settings.ue_root / "Engine/Binaries/Linux/UnrealEditor-Cmd",
        project_path,
        f"-ExecutePythonScript={script_path}",
        "-unattended",
        "-nopause",
        "-nosplash",
        "-NullRHI",
        "-stdout",
        "-FullStdOutLogOutput",
        "-NoSound",
        "-ddc=InstalledNoZenLocalFallback",
        f"-LocalDataCachePath={ddc_dir}",
    ]

    job = {
        "schema_version": 1,
        "job": "ingest_asset",
        "asset_id": asset_id,
        "source_file": str(source_file),
        "manifest_path": str(manifest_path),
        "require_single_static_mesh": require_single_static_mesh,
        "require_texture_references": require_texture_references,
        "requested_normalization": normalization,
        "expected_bundle_sha256": initial_hashes.bundle_sha256,
        "expected_content_sha256": initial_hashes.content_sha256,
        "source_structure": source_structure.payload,
        "source_structure_sha256": source_structure.sha256,
    }
    _write_json(job_path, job)
    phase = "primary_ue_process"
    try:
        ue_result = run_ue(
            command,
            cwd=settings.project_root,
            log_path=ue_log_path,
            timeout_sec=timeout_sec,
            env=env,
        )
        phase = "primary_manifest"
        manifest = _read_json_object(
            manifest_path,
            missing_message=f"UE ingest did not create manifest; log: {ue_log_path}",
        )
        manifest.update(
            {
                "schema_version": IMPORT_MANIFEST_SCHEMA_VERSION,
                "command": ue_result.command,
                "duration_sec": ue_result.duration_sec,
                "engine": _engine_version(settings),
                "runtime": runtime,
                "source_sha256": initial_hashes.source_sha256,
                "bundle_sha256": initial_hashes.bundle_sha256,
                "content_sha256": initial_hashes.content_sha256,
                "source_structure": source_structure.payload,
                "source_structure_sha256": source_structure.sha256,
                "requested_normalization": normalization,
                "ue_log": ue_log_path.name,
                "ue_summary": _summary_payload(ue_result),
            }
        )
        _write_json(manifest_path, manifest)
        _require_clean_ue_result(
            manifest=manifest,
            result=ue_result,
            label="UE ingest",
            log_path=ue_log_path,
        )
        phase = "primary_contract_validation"
        imported_object_paths, imported_objects, static_meshes = _validate_asset_manifest(
            manifest,
            asset_id=asset_id,
            require_single_static_mesh=require_single_static_mesh,
        )
        phase = "primary_quality_gate"
        try:
            manifest["quality"] = require_static_mesh_quality(
                manifest,
                require_single_static_mesh=require_single_static_mesh,
                require_texture_references=require_texture_references,
            )
        except IngestQualityError as quality_error:
            manifest["quality"] = quality_error.report
            _write_json(manifest_path, manifest)
            raise
        _write_json(manifest_path, manifest)
        _assert_hashes_unchanged(initial_hashes, source_file, hash_root, hash_files)
    except BaseException as exc:
        cleanup = _rollback_transaction(
            settings=settings,
            asset_id=asset_id,
            command=command,
            env=env,
            run_dir=run_dir,
            runtime=runtime,
            timeout_sec=timeout_sec,
            primary_manifest_path=manifest_path,
        )
        _record_failure_without_masking(
            manifest_path=manifest_path,
            job=job,
            command=_exception_command(exc, command),
            runtime=runtime,
            error=exc,
            phase=phase,
            cleanup=cleanup,
        )
        _raise_typed_failure(exc, manifest_path)

    reload_job = {
        "schema_version": 1,
        "job": "validate_ingested_asset",
        "asset_id": asset_id,
        "imported_objects": list(imported_objects),
        "manifest_path": str(reload_manifest_path),
        "require_single_static_mesh": require_single_static_mesh,
        "requested_normalization": normalization,
        "source_format": source_file.suffix.lower().lstrip("."),
    }
    _write_json(reload_job_path, reload_job)
    reload_env = dict(env)
    reload_env["UEF_JOB_FILE"] = str(reload_job_path)
    phase = "reload_ue_process"
    try:
        reload_result = run_ue(
            command,
            cwd=settings.project_root,
            log_path=reload_log_path,
            timeout_sec=timeout_sec,
            env=reload_env,
        )
        phase = "reload_manifest"
        reload_manifest = _read_json_object(
            reload_manifest_path,
            missing_message=(
                f"UE reload validation did not create manifest; log: {reload_log_path}"
            ),
        )
        _require_clean_ue_result(
            manifest=reload_manifest,
            result=reload_result,
            label="UE reload validation",
            log_path=reload_log_path,
        )
        _validate_asset_manifest(
            reload_manifest,
            asset_id=asset_id,
            require_single_static_mesh=require_single_static_mesh,
        )
        phase = "reload_contract_validation"
        manifest["reload_validation"] = {
            "status": "running",
            "manifest": reload_manifest_path.name,
            "ue_log": reload_log_path.name,
            "duration_sec": reload_result.duration_sec,
            "ue_summary": _summary_payload(reload_result),
            "imported_objects": reload_manifest["imported_objects"],
            "static_meshes": reload_manifest["static_meshes"],
        }
        _write_json(manifest_path, manifest)
        try:
            _require_same_asset_payload(manifest, reload_manifest)
            _assert_hashes_unchanged(initial_hashes, source_file, hash_root, hash_files)
        except BaseException:
            manifest["reload_validation"]["status"] = "failed"
            _write_json(manifest_path, manifest)
            raise
        manifest["reload_validation"]["status"] = "ok"
        _write_json(manifest_path, manifest)
    except BaseException as exc:
        cleanup = _rollback_transaction(
            settings=settings,
            asset_id=asset_id,
            command=command,
            env=env,
            run_dir=run_dir,
            runtime=runtime,
            timeout_sec=timeout_sec,
            primary_manifest_path=manifest_path,
        )
        _record_failure_without_masking(
            manifest_path=manifest_path,
            job=job,
            command=_exception_command(exc, command),
            runtime=runtime,
            error=exc,
            phase=phase,
            cleanup=cleanup,
        )
        _raise_typed_failure(exc, manifest_path)

    transaction = manifest.get("transaction")
    had_existing = bool(isinstance(transaction, dict) and transaction.get("had_existing") is True)
    remove_new_destination = bool(
        isinstance(transaction, dict) and transaction.get("had_existing") is False
    )
    expected_asset_payload = {key: manifest[key] for key in _ASSET_PAYLOAD_KEYS}
    finalize_job_base = {
        "schema_version": 1,
        "job": "finalize_ingested_asset",
        "asset_id": asset_id,
        "imported_objects": list(imported_objects),
        "require_single_static_mesh": require_single_static_mesh,
        "requested_normalization": normalization,
        "source_format": source_file.suffix.lower().lstrip("."),
        "expected_asset_payload": expected_asset_payload,
        "had_existing": had_existing,
        "remove_new_destination": remove_new_destination,
    }
    finalize_attempts: list[dict[str, Any]] = []
    finalize_errors: list[BaseException] = []
    finalize_success: tuple[dict[str, Any], Any, Path, Path, str] | None = None
    for attempt_index, (attempt_job_path, attempt_manifest_path, attempt_log_path) in enumerate(
        (
            (finalize_job_path, finalize_manifest_path, finalize_log_path),
            (
                finalize_retry_job_path,
                finalize_retry_manifest_path,
                finalize_retry_log_path,
            ),
        ),
        start=1,
    ):
        finalize_job = {
            **finalize_job_base,
            "manifest_path": str(attempt_manifest_path),
            "attempt": attempt_index,
        }
        _write_json(attempt_job_path, finalize_job)
        finalize_env = dict(env)
        finalize_env["UEF_JOB_FILE"] = str(attempt_job_path)
        phase = f"finalize_attempt_{attempt_index}_ue_process"
        try:
            finalize_result = run_ue(
                command,
                cwd=settings.project_root,
                log_path=attempt_log_path,
                timeout_sec=timeout_sec,
                env=finalize_env,
            )
            phase = f"finalize_attempt_{attempt_index}_manifest"
            finalize_manifest = _read_json_object(
                attempt_manifest_path,
                missing_message=(
                    f"UE transaction finalize did not create manifest; log: {attempt_log_path}"
                ),
            )
            _require_clean_ue_result(
                manifest=finalize_manifest,
                result=finalize_result,
                label=f"UE transaction finalize attempt {attempt_index}",
                log_path=attempt_log_path,
            )
            _validate_asset_manifest(
                finalize_manifest,
                asset_id=asset_id,
                require_single_static_mesh=require_single_static_mesh,
            )
            phase = f"finalize_attempt_{attempt_index}_contract_validation"
            _require_same_asset_payload(manifest, finalize_manifest)
            _assert_hashes_unchanged(initial_hashes, source_file, hash_root, hash_files)
            if finalize_manifest.get("transaction_state") != "committed":
                raise RuntimeError("UE transaction finalize did not report committed state")
            confirmation = "direct" if attempt_index == 1 else "retry"
            finalize_attempts.append(
                {
                    "attempt": attempt_index,
                    "status": "ok",
                    "manifest": attempt_manifest_path.name,
                    "ue_log": attempt_log_path.name,
                    "duration_sec": finalize_result.duration_sec,
                    "ue_summary": _summary_payload(finalize_result),
                }
            )
            finalize_success = (
                finalize_manifest,
                finalize_result,
                attempt_manifest_path,
                attempt_log_path,
                confirmation,
            )
            break
        except BaseException as exc:
            finalize_errors.append(exc)
            finalize_attempts.append(
                {
                    "attempt": attempt_index,
                    "status": "failed",
                    "manifest": attempt_manifest_path.name,
                    "ue_log": attempt_log_path.name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    if finalize_success is None:
        inspect_job = {
            **finalize_job_base,
            "job": "inspect_ingest_transaction",
            "manifest_path": str(inspect_manifest_path),
        }
        _write_json(inspect_job_path, inspect_job)
        inspect_env = dict(env)
        inspect_env["UEF_JOB_FILE"] = str(inspect_job_path)
        inspect_state = "in_doubt"
        inspect_evidence: dict[str, Any] = {
            "status": "failed",
            "manifest": inspect_manifest_path.name,
            "ue_log": inspect_log_path.name,
        }
        phase = "finalize_inspect_ue_process"
        try:
            inspect_result = run_ue(
                command,
                cwd=settings.project_root,
                log_path=inspect_log_path,
                timeout_sec=timeout_sec,
                env=inspect_env,
            )
            phase = "finalize_inspect_manifest"
            inspect_manifest = _read_json_object(
                inspect_manifest_path,
                missing_message=(
                    f"UE transaction inspection did not create manifest; log: {inspect_log_path}"
                ),
            )
            _require_clean_ue_result(
                manifest=inspect_manifest,
                result=inspect_result,
                label="UE transaction inspection",
                log_path=inspect_log_path,
            )
            raw_inspect_state = inspect_manifest.get("transaction_state")
            if raw_inspect_state not in {"pre_commit", "committed", "in_doubt"}:
                raise RuntimeError("UE transaction inspection returned an invalid state")
            inspect_state = str(raw_inspect_state)
            if inspect_state in {"pre_commit", "committed"} and (
                inspect_manifest.get("payload_matches") is not True
            ):
                raise RuntimeError("UE transaction inspection state lacks exact payload proof")
            inspect_evidence = {
                "status": "ok",
                "manifest": inspect_manifest_path.name,
                "ue_log": inspect_log_path.name,
                "duration_sec": inspect_result.duration_sec,
                "ue_summary": _summary_payload(inspect_result),
                "transaction_state": inspect_state,
                "destination_exists": inspect_manifest.get("destination_exists"),
                "transaction_exists": inspect_manifest.get("transaction_exists"),
                "candidate_exists": inspect_manifest.get("candidate_exists"),
                "backup_exists": inspect_manifest.get("backup_exists"),
                "payload_matches": inspect_manifest.get("payload_matches"),
            }
            if inspect_state == "committed":
                _require_same_asset_payload(manifest, inspect_manifest)
                _assert_hashes_unchanged(
                    initial_hashes,
                    source_file,
                    hash_root,
                    hash_files,
                )
                finalize_success = (
                    inspect_manifest,
                    inspect_result,
                    inspect_manifest_path,
                    inspect_log_path,
                    "inspect",
                )
        except BaseException as inspect_error:
            inspect_evidence.update(
                {
                    "error_type": type(inspect_error).__name__,
                    "error": str(inspect_error),
                }
            )
            inspect_state = "in_doubt"

        if finalize_success is None:
            failure = finalize_errors[-1]
            if inspect_state == "pre_commit":
                cleanup = _rollback_transaction(
                    settings=settings,
                    asset_id=asset_id,
                    command=command,
                    env=env,
                    run_dir=run_dir,
                    runtime=runtime,
                    timeout_sec=timeout_sec,
                    primary_manifest_path=manifest_path,
                )
                phase = "finalize_pre_commit_failure"
            else:
                cleanup = {
                    "status": "in_doubt",
                    "reason": ("commit point could not be proven; transaction state was preserved"),
                    "inspection": inspect_evidence,
                }
                phase = "finalize_in_doubt"
            manifest["finalize_validation"] = {
                "status": "failed",
                "attempts": finalize_attempts,
                "inspection": inspect_evidence,
            }
            _write_json(manifest_path, manifest)
            _record_failure_without_masking(
                manifest_path=manifest_path,
                job=job,
                command=_exception_command(failure, command),
                runtime=runtime,
                error=failure,
                phase=phase,
                cleanup=cleanup,
            )
            _raise_typed_failure(failure, manifest_path)

    assert finalize_success is not None
    (
        committed_manifest,
        committed_result,
        committed_manifest_path,
        committed_log_path,
        commit_confirmation,
    ) = finalize_success
    manifest["transaction"]["state"] = "committed"
    manifest["finalize_validation"] = {
        "status": "ok",
        "commit_confirmation": commit_confirmation,
        "attempts": finalize_attempts,
        "manifest": committed_manifest_path.name,
        "ue_log": committed_log_path.name,
        "duration_sec": committed_result.duration_sec,
        "ue_summary": _summary_payload(committed_result),
        "removed_backup": committed_manifest.get("removed_backup", False),
    }

    _write_json(manifest_path, manifest)
    return IngestResult(
        run_dir=run_dir,
        manifest_path=manifest_path,
        ue_log_path=ue_log_path,
        reload_log_path=reload_log_path,
        finalize_log_path=finalize_log_path,
        asset_id=asset_id,
        imported_object_paths=imported_object_paths,
        static_mesh_paths=static_meshes,
    )


def _normalize_bundle_inputs(
    *,
    source_file: Path,
    bundle_root: Path | None,
    bundle_files: Sequence[Path] | None,
) -> tuple[Path, tuple[Path, ...]]:
    if (bundle_root is None) != (bundle_files is None):
        raise ValueError("bundle_root and bundle_files must be provided together")
    if bundle_root is None:
        return source_file.parent, (Path(source_file.name),)
    root = bundle_root.expanduser().resolve()
    files = tuple(bundle_files or ())
    if not files:
        raise ValueError("bundle_files must contain at least one relative path")
    resolved_source = source_file.resolve()
    resolved_files = {(root / relative).resolve() for relative in files}
    if resolved_source not in resolved_files:
        raise ValueError("source_file must be a member of the declared bundle")
    return root, files


def _resolve_source_structure(
    source_file: Path,
    *,
    expected_payload: Mapping[str, Any] | None,
    expected_sha256: str | None,
) -> SourceStructureEvidence:
    if (expected_payload is None) != (expected_sha256 is None):
        raise ValueError(
            "expected_source_structure and expected_source_structure_sha256 must be provided "
            "together"
        )
    actual = inspect_source_structure(source_file)
    if expected_payload is None:
        return actual
    if actual.payload != dict(expected_payload) or actual.sha256 != expected_sha256:
        raise RuntimeError(
            "staged asset source_structure changed before UE import: "
            f"expected_sha256={expected_sha256}, actual_sha256={actual.sha256}"
        )
    return actual


def _hash_snapshot(source_file: Path, root: Path, files: tuple[Path, ...]) -> _HashSnapshot:
    return _HashSnapshot(
        source_sha256=_sha256(source_file),
        bundle_sha256=bundle_sha256(root, files),
        content_sha256=content_sha256(root, files),
    )


def _assert_source_structure_unchanged(
    expected: SourceStructureEvidence,
    source_file: Path,
) -> None:
    current = inspect_source_structure(source_file)
    if current != expected:
        raise RuntimeError(
            "asset source_structure changed while initial hashes were captured: "
            f"before={expected.sha256}, after={current.sha256}"
        )


def _assert_expected_hashes(
    snapshot: _HashSnapshot,
    *,
    expected_bundle_sha256: str | None,
    expected_content_sha256: str | None,
) -> None:
    expected = {
        "bundle_sha256": expected_bundle_sha256,
        "content_sha256": expected_content_sha256,
    }
    actual = {
        "bundle_sha256": snapshot.bundle_sha256,
        "content_sha256": snapshot.content_sha256,
    }
    for field, expected_value in expected.items():
        if expected_value is not None and actual[field] != expected_value:
            raise RuntimeError(
                f"staged asset {field} changed before UE import: "
                f"expected {expected_value}, got {actual[field]}"
            )


def _assert_hashes_unchanged(
    initial: _HashSnapshot,
    source_file: Path,
    root: Path,
    files: tuple[Path, ...],
) -> None:
    current = _hash_snapshot(source_file, root, files)
    if current != initial:
        raise RuntimeError(
            "staged asset bytes changed during UE import/validation: "
            f"before={initial}, after={current}"
        )


def _require_clean_ue_result(
    *,
    manifest: dict[str, Any],
    result: Any,
    label: str,
    log_path: Path,
) -> None:
    if manifest.get("status") != "ok":
        raise RuntimeError(f"{label} runtime failed; manifest: {manifest}")
    if result.summary.error_count or result.summary.warning_count:
        raise RuntimeError(
            f"{label} log contains {result.summary.error_count} error(s) and "
            f"{result.summary.warning_count} warning(s); log: {log_path}"
        )


def _validate_asset_manifest(
    manifest: dict[str, Any],
    *,
    asset_id: str,
    require_single_static_mesh: bool,
) -> tuple[tuple[str, ...], tuple[dict[str, str], ...], tuple[str, ...]]:
    # Semantic mesh acceptance belongs to the versioned host quality ruleset.
    # This validator intentionally limits itself to the transport/inventory shape.
    del require_single_static_mesh
    if manifest.get("import_backend") != "asset_tools_auto":
        raise RuntimeError("UE ingest manifest has an unsupported import_backend")
    if manifest.get("normalization") != {
        "target_units": "centimeters",
        "target_up_axis": "Z",
        "target_handedness": "left_handed",
        "source_conversion": "delegated_to_engine_importer",
        "package_pivot_policy": "preserve",
        "uniform_scale": 1.0,
    }:
        raise RuntimeError("UE ingest manifest has an invalid engine normalization contract")
    destination_prefix = f"/Game/UEF/Ingested/{asset_id}/"
    imported = manifest.get("imported_object_paths")
    if not isinstance(imported, list) or not imported:
        raise RuntimeError("UE ingest manifest requires non-empty imported_object_paths")
    if any(
        not isinstance(path, str) or not path.startswith(destination_prefix) for path in imported
    ):
        raise RuntimeError(f"UE ingest manifest contains an object outside {destination_prefix}")
    imported_paths = tuple(imported)
    if len(imported_paths) != len(set(imported_paths)):
        raise RuntimeError("UE ingest manifest contains duplicate imported object paths")
    if manifest.get("object_count") != len(imported_paths):
        raise RuntimeError("UE ingest manifest object_count does not match imported paths")

    objects = manifest.get("imported_objects")
    if not isinstance(objects, list) or len(objects) != len(imported_paths):
        raise RuntimeError("UE ingest manifest imported_objects does not match object inventory")
    object_payloads: list[dict[str, str]] = []
    for index, item in enumerate(objects):
        if not isinstance(item, dict) or set(item) != {"object_path", "class"}:
            raise RuntimeError(f"UE ingest imported object {index} has an invalid payload")
        object_path = item.get("object_path")
        class_name = item.get("class")
        if (
            object_path != imported_paths[index]
            or not isinstance(class_name, str)
            or not class_name
        ):
            raise RuntimeError(f"UE ingest imported object {index} does not match its path/class")
        object_payloads.append({"object_path": object_path, "class": class_name})

    meshes = manifest.get("static_meshes")
    if not isinstance(meshes, list):
        raise RuntimeError("UE ingest manifest static_meshes must be a list")
    if manifest.get("static_mesh_count") != len(meshes):
        raise RuntimeError("UE ingest manifest static_mesh_count does not match payload")
    for field in ("material_count", "texture_count"):
        if not _is_nonnegative_int(manifest.get(field)):
            raise RuntimeError(f"UE ingest manifest requires non-negative {field}")

    static_mesh_paths: list[str] = []
    for index, mesh in enumerate(meshes):
        if not isinstance(mesh, dict):
            raise RuntimeError(f"UE ingest StaticMesh payload {index} must be an object")
        object_path = mesh.get("object_path")
        if not isinstance(object_path, str) or object_path not in imported_paths:
            raise RuntimeError(f"UE ingest StaticMesh payload {index} has an unknown object_path")
        for field in (
            "lod_count",
            "triangle_count",
            "render_fallback_triangle_count",
            "vertex_count",
        ):
            if not _is_int(mesh.get(field)):
                raise RuntimeError(f"UE ingest StaticMesh payload {index} requires integer {field}")
        if not _is_nonnegative_int(mesh.get("material_count")):
            raise RuntimeError(f"UE ingest StaticMesh payload {index} requires material_count >= 0")
        if not _valid_bounds_shape(mesh.get("bounds_cm")):
            raise RuntimeError(f"UE ingest StaticMesh payload {index} has invalid bounds shape")
        static_mesh_paths.append(object_path)
    if len(static_mesh_paths) != len(set(static_mesh_paths)):
        raise RuntimeError("UE ingest manifest contains duplicate StaticMesh paths")
    _validate_material_postprocess(manifest, imported_paths=imported_paths)
    return imported_paths, tuple(object_payloads), tuple(static_mesh_paths)


def _validate_material_postprocess(
    manifest: dict[str, Any],
    *,
    imported_paths: tuple[str, ...],
) -> None:
    value = manifest.get("material_postprocess")
    if not isinstance(value, dict) or set(value) != {"policy", "materials"}:
        raise RuntimeError("UE ingest manifest has an invalid material_postprocess payload")
    policy = value.get("policy")
    materials = value.get("materials")
    if not isinstance(materials, list):
        raise RuntimeError("UE ingest material_postprocess materials must be a list")
    if policy == "not_applicable":
        if materials:
            raise RuntimeError("not_applicable material_postprocess must have no materials")
        return
    if policy != FBX_MATERIAL_POSTPROCESS_POLICY or not materials:
        raise RuntimeError("UE ingest used an unsupported material_postprocess policy")
    connected_count = 0
    for index, material in enumerate(materials):
        if not isinstance(material, dict) or set(material) != {
            "material_path",
            "bindings",
            "shading_override",
        }:
            raise RuntimeError(f"UE ingest material_postprocess material {index} is invalid")
        material_path = material.get("material_path")
        bindings = material.get("bindings")
        shading_override = material.get("shading_override")
        if material_path not in imported_paths or not isinstance(bindings, list):
            raise RuntimeError(f"UE ingest material_postprocess material {index} is unresolved")
        if shading_override is not None and (
            not isinstance(shading_override, dict)
            or set(shading_override) != {"policy", "blend_mode", "opacity"}
            or shading_override.get("policy") != FBX_GLASS_OVERRIDE_POLICY
            or shading_override.get("blend_mode") != "translucent"
            or not isinstance(shading_override.get("opacity"), float)
            or shading_override.get("opacity") != FBX_GLASS_OPACITY
        ):
            raise RuntimeError(
                f"UE ingest material_postprocess material {index} has an invalid shading override"
            )
        seen_roles: set[str] = set()
        for binding in bindings:
            if not isinstance(binding, dict):
                raise RuntimeError("UE ingest material_postprocess binding must be an object")
            role = binding.get("role")
            texture_path = binding.get("texture_path")
            expected_keys = {"role", "texture_path"}
            if role == "normal":
                expected_keys.update({"source_convention", "green_channel_flipped"})
            if (
                set(binding) != expected_keys
                or role not in {"base_color", "metallic", "roughness", "normal"}
                or role in seen_roles
                or texture_path not in imported_paths
            ):
                raise RuntimeError("UE ingest material_postprocess binding is invalid")
            if role == "normal":
                source_convention = binding.get("source_convention")
                flipped = binding.get("green_channel_flipped")
                if (
                    source_convention not in {"opengl", "directx"}
                    or not isinstance(flipped, bool)
                    or flipped != (source_convention == "opengl")
                ):
                    raise RuntimeError("UE ingest normal-map convention evidence is invalid")
            seen_roles.add(str(role))
            connected_count += 1
    if connected_count == 0:
        raise RuntimeError("UE ingest material_postprocess contains no effective bindings")


_ASSET_PAYLOAD_KEYS = (
    "import_backend",
    "normalization",
    "material_postprocess",
    "imported_object_paths",
    "imported_objects",
    "object_count",
    "static_mesh_count",
    "material_count",
    "texture_count",
    "static_meshes",
)


def _require_same_asset_payload(first: dict[str, Any], second: dict[str, Any]) -> None:
    differing = [key for key in _ASSET_PAYLOAD_KEYS if first.get(key) != second.get(key)]
    if differing:
        raise RuntimeError(
            "Independent UE process asset payload differs from import for fields: "
            + ", ".join(differing)
        )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_bounds_shape(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"min", "max", "size"}:
        return False
    for key in ("min", "max", "size"):
        vector = value[key]
        if not isinstance(vector, list) or len(vector) != 3:
            return False
        if any(isinstance(item, bool) or not isinstance(item, int | float) for item in vector):
            return False
    return True


def _read_json_object(path: Path, *, missing_message: str) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(missing_message)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read JSON manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON manifest must contain an object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _exception_command(error: BaseException, command: Sequence[str | Path]) -> list[str]:
    if isinstance(error, UERunnerError):
        return error.result.command
    return [str(part) for part in command]


def _raise_typed_failure(error: BaseException, manifest_path: Path) -> NoReturn:
    if isinstance(error, IngestQualityError):
        error.manifest_path = manifest_path
        raise error
    if isinstance(error, IngestExecutionError):
        raise error
    raise IngestExecutionError(manifest_path=manifest_path, cause=error) from error


def _record_failure_without_masking(
    *,
    manifest_path: Path,
    job: dict[str, Any],
    command: list[str],
    runtime: dict[str, object],
    error: BaseException,
    phase: str,
    cleanup: dict[str, Any],
) -> None:
    try:
        _merge_host_failure(
            manifest_path=manifest_path,
            job=job,
            command=command,
            runtime=runtime,
            error=error,
            phase=phase,
            cleanup=cleanup,
        )
    except Exception as record_error:  # pragma: no cover - secondary filesystem failure
        error.add_note(f"Could not record ingest failure: {record_error}")


def _rollback_transaction(
    *,
    settings: Settings,
    asset_id: str,
    command: Sequence[str | Path],
    env: dict[str, str],
    run_dir: Path,
    runtime: dict[str, object],
    timeout_sec: int,
    primary_manifest_path: Path,
) -> dict[str, Any]:
    remove_new_destination = False
    if primary_manifest_path.is_file():
        try:
            primary = _read_json_object(
                primary_manifest_path,
                missing_message="primary manifest disappeared before rollback",
            )
            transaction = primary.get("transaction")
            remove_new_destination = bool(
                isinstance(transaction, dict) and transaction.get("had_existing") is False
            )
        except RuntimeError:
            pass
    rollback_job_path = run_dir / "rollback_job.json"
    rollback_manifest_path = run_dir / "rollback_manifest.json"
    rollback_log_path = run_dir / "ue_rollback.log"
    rollback_job = {
        "schema_version": 1,
        "job": "rollback_ingested_asset",
        "asset_id": asset_id,
        "manifest_path": str(rollback_manifest_path),
        "remove_new_destination": remove_new_destination,
    }
    try:
        _write_json(rollback_job_path, rollback_job)
        rollback_env = dict(env)
        rollback_env["UEF_JOB_FILE"] = str(rollback_job_path)
        result = run_ue(
            command,
            cwd=settings.project_root,
            log_path=rollback_log_path,
            timeout_sec=timeout_sec,
            env=rollback_env,
        )
        payload = _read_json_object(
            rollback_manifest_path,
            missing_message=f"UE rollback did not create manifest; log: {rollback_log_path}",
        )
        payload.update(
            {
                "manifest": rollback_manifest_path.name,
                "ue_log": rollback_log_path.name,
                "duration_sec": result.duration_sec,
                "ue_summary": _summary_payload(result),
            }
        )
        if (
            payload.get("status") != "ok"
            or result.summary.error_count
            or result.summary.warning_count
        ):
            payload["status"] = "failed"
            payload["host_error"] = "UE transaction rollback failed its manifest or log contract"
        _write_json(rollback_manifest_path, payload)
        return payload
    except BaseException as rollback_error:
        command_payload = _exception_command(rollback_error, command)
        try:
            _merge_host_failure(
                manifest_path=rollback_manifest_path,
                job=rollback_job,
                command=command_payload,
                runtime=runtime,
                error=rollback_error,
                phase="rollback_ue_process",
                cleanup=None,
            )
        except Exception as record_error:  # pragma: no cover - secondary filesystem failure
            rollback_error.add_note(f"Could not record UE rollback failure: {record_error}")
        return {
            "status": "failed",
            "manifest": rollback_manifest_path.name,
            "ue_log": rollback_log_path.name,
            "error": {
                "type": type(rollback_error).__name__,
                "message": str(rollback_error),
            },
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _engine_version(settings: Settings) -> dict[str, Any]:
    path = settings.ue_root / "Engine/Build/Build.version"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"UE Build.version must be an object: {path}")
    return payload


def _summary_payload(result: Any) -> dict[str, Any]:
    return {
        "warning_count": result.summary.warning_count,
        "warning_noise_count": result.summary.warning_noise_count,
        "warning_noise": result.summary.warning_noise or {},
        "error_count": result.summary.error_count,
        "error_noise_count": result.summary.error_noise_count,
        "error_noise": result.summary.error_noise or {},
        "warnings": result.summary.warnings,
        "errors": result.summary.errors,
    }


def _merge_host_failure(
    *,
    manifest_path: Path,
    job: dict[str, Any],
    command: list[str],
    runtime: dict[str, object],
    error: BaseException,
    phase: str,
    cleanup: dict[str, Any] | None,
) -> None:
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                manifest.update(payload)
        except (OSError, json.JSONDecodeError) as exc:
            manifest["manifest_read_error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
    manifest.update(
        {
            "schema_version": (
                IMPORT_MANIFEST_SCHEMA_VERSION if job["job"] == "ingest_asset" else 1
            ),
            "status": "failed",
            "asset_id": job["asset_id"],
            "job": job["job"],
            "command": command,
            "runtime": runtime,
            "failure_phase": phase,
            "host_error": {
                "type": type(error).__name__,
                "message": str(error),
            },
        }
    )
    if cleanup is not None:
        manifest["asset_cleanup"] = cleanup
    if "source_file" in job:
        manifest["source_file"] = job["source_file"]
    if "requested_normalization" in job:
        manifest["requested_normalization"] = job["requested_normalization"]
    if "source_structure" in job:
        manifest["source_structure"] = job["source_structure"]
    if "source_structure_sha256" in job:
        manifest["source_structure_sha256"] = job["source_structure_sha256"]
    manifest.setdefault("error", str(error))
    _write_json(manifest_path, manifest)
