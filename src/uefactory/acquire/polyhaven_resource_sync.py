"""Durable Poly Haven HDRI and PBR acquisition-to-catalog pipeline.

This module deliberately keeps resource publication separate from the model
adapter: HDRIs and texture cohorts are catalog resources, not importable mesh
assets.  Provider bytes live in immutable revision/resolution roots, pass the
strict CPU validators, and are then published with one atomic catalog cohort.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import quote
from uuid import uuid4

from uefactory.acquire.failure_journal import (
    ActiveFailure,
    FailureJournalError,
    append_failure_event,
    append_release_event,
    append_resolution_event,
    empty_failure_journal,
    validate_failure_journal,
)
from uefactory.acquire.polyhaven import (
    POLYHAVEN_ASSET_URL,
    POLYHAVEN_FILES_URL,
    POLYHAVEN_LICENSE,
    POLYHAVEN_LICENSE_URL,
    POLYHAVEN_SOURCE,
    PolyHavenAcquireError,
    PolyHavenFileSpec,
    PolyHavenIntegrityError,
    PolyHavenPathSecurityError,
    PolyHavenRuntimeConfig,
)
from uefactory.acquire.polyhaven_provider import (
    PolyHavenProviderSession,
    ProviderOperationError,
    polyhaven_source_lock,
)
from uefactory.acquire.polyhaven_resources import (
    DEFAULT_RESOURCE_RESOLUTION,
    PolyHavenResourceFileSpec,
    PolyHavenResourceListing,
    PolyHavenResourcePackage,
    ResourceKind,
    parse_polyhaven_resource_files,
    parse_polyhaven_resource_listing,
    resource_storage_root,
)
from uefactory.acquire.resource_validation import (
    PbrCohortValidationEvidence,
    PbrMapInput,
    RadianceHdrValidationEvidence,
    ResourceValidationError,
    validate_pbr_cohort,
    validate_radiance_hdr,
)
from uefactory.acquire.runtime import (
    AcquisitionFailure,
    Clock,
    FailureKind,
    SystemClock,
)
from uefactory.catalog import (
    Catalog,
    ResourceArtifactUpsert,
    ResourceFileUpsert,
    ResourceUpsert,
)
from uefactory.core.config import Settings
from uefactory.ingest.staging import bundle_sha256, content_sha256

RESOURCE_STATE_SCHEMA_VERSION = 1
RESOURCE_RUN_SCHEMA_VERSION = 1
RESOURCE_EVIDENCE_SCHEMA_VERSION = 1

POLYHAVEN_RESOURCE_LISTING_URLS: dict[ResourceKind, str] = {
    "hdri": "https://api.polyhaven.com/assets?type=hdris",
    "pbr_texture_set": "https://api.polyhaven.com/assets?type=textures",
}

ResourceItemStatus = Literal["ready", "skipped", "failed", "deferred"]
ResourceRunStatus = Literal["ready", "partial", "failed", "deferred", "noop"]

_RESOURCE_ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_RESOLUTION_PATTERN = re.compile(r"[1-9][0-9]*k\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_STATE_DIGEST_DOMAIN = b"uefactory.polyhaven-resource-state.v1\0"
_LISTING_DIGEST_DOMAIN = b"uefactory.polyhaven-resource-listing.v1\0"
_CATALOG_DIGEST_DOMAIN = b"uefactory.polyhaven-resource-catalog.v1\0"
_LISTING_EVIDENCE_DOMAIN = b"uefactory.polyhaven-resource-listing-evidence.v1\0"
_ID_DIGEST_DOMAIN = b"uefactory.polyhaven-resource-child-id.v1\0"
_HASH_CHUNK_BYTES = 1024 * 1024


class PolyHavenResourceSyncError(PolyHavenAcquireError):
    """A resource run could not preserve its fail-closed contract."""


class PolyHavenResourceCommitPendingError(RuntimeError):
    """Catalog commit succeeded; a durable compatibility intent still needs replay."""


@dataclass(frozen=True, slots=True)
class PolyHavenResourceSyncItem:
    resource_id: str
    kind: ResourceKind
    source_id: str
    revision: str
    resolution: str
    status: ResourceItemStatus
    root_dir: Path | None = None
    file_paths: tuple[Path, ...] = ()
    artifact_paths: tuple[Path, ...] = ()
    compatibility_path: Path | None = None
    compatibility_metadata_path: Path | None = None
    downloaded_files: int = 0
    reused_files: int = 0
    downloaded_bytes: int = 0
    verified_bytes: int = 0
    bundle_sha256: str | None = None
    content_sha256: str | None = None
    error: Mapping[str, Any] | None = None
    failure_event_id: str | None = None

    def as_dict(self, *, project_root: Path | None = None) -> dict[str, Any]:
        def path(value: Path | None) -> str | None:
            if value is None:
                return None
            return _portable_path(value, project_root) if project_root is not None else str(value)

        return {
            "resource_id": self.resource_id,
            "kind": self.kind,
            "source_id": self.source_id,
            "revision": self.revision,
            "resolution": self.resolution,
            "status": self.status,
            "root_dir": path(self.root_dir),
            "file_paths": [path(item) for item in self.file_paths],
            "artifact_paths": [path(item) for item in self.artifact_paths],
            "compatibility_path": path(self.compatibility_path),
            "compatibility_metadata_path": path(self.compatibility_metadata_path),
            "downloaded_files": self.downloaded_files,
            "reused_files": self.reused_files,
            "downloaded_bytes": self.downloaded_bytes,
            "verified_bytes": self.verified_bytes,
            "bundle_sha256": self.bundle_sha256,
            "content_sha256": self.content_sha256,
            "error": None if self.error is None else dict(self.error),
            "failure_event_id": self.failure_event_id,
        }


@dataclass(frozen=True, slots=True)
class PolyHavenResourceSyncResult:
    kind: ResourceKind
    resolution: str
    run_id: str
    status: ResourceRunStatus
    manifest_path: Path
    state_path: Path
    failure_journal_path: Path
    catalog_path: Path
    listing_sha256: str
    items: tuple[PolyHavenResourceSyncItem, ...]

    @property
    def ready(self) -> int:
        return sum(item.status == "ready" for item in self.items)

    @property
    def skipped(self) -> int:
        return sum(item.status == "skipped" for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.items)

    @property
    def deferred(self) -> int:
        return sum(item.status == "deferred" for item in self.items)

    def as_dict(self, *, project_root: Path | None = None) -> dict[str, Any]:
        return {
            "schema_version": RESOURCE_RUN_SCHEMA_VERSION,
            "kind": self.kind,
            "resolution": self.resolution,
            "run_id": self.run_id,
            "status": self.status,
            "manifest_path": _portable_path(self.manifest_path, project_root),
            "state_path": _portable_path(self.state_path, project_root),
            "failure_journal_path": _portable_path(self.failure_journal_path, project_root),
            "catalog_path": _portable_path(self.catalog_path, project_root),
            "listing_sha256": self.listing_sha256,
            "counts": {
                "ready": self.ready,
                "skipped": self.skipped,
                "failed": self.failed,
                "deferred": self.deferred,
            },
            "items": [item.as_dict(project_root=project_root) for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class _PreparedHdriCompatibility:
    alias_path: Path
    metadata_path: Path
    alias_temporary: Path | None
    metadata_temporary: Path | None


def sync_polyhaven_resources(
    *,
    settings: Settings,
    kind: ResourceKind,
    limit: int = 1,
    resolution: str = DEFAULT_RESOURCE_RESOLUTION,
    source_ids: tuple[str, ...] = (),
    force: bool = False,
    runtime_config: PolyHavenRuntimeConfig | None = None,
    database_path: Path | None = None,
    retry_revisions: tuple[str, ...] = (),
    clock: Clock | None = None,
) -> PolyHavenResourceSyncResult:
    """Acquire, validate, and atomically publish one resource-kind cycle.

    Runs of different resource kinds intentionally share the provider lock and
    quota ledger but keep distinct state and failure journals.  The optional
    ``source_ids`` filter remains listing-bound: a caller cannot bypass current
    provider revision and license discovery with a direct files URL.
    """

    checked_kind = _checked_kind(kind)
    checked_resolution = _checked_resolution(resolution)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
        raise PolyHavenResourceSyncError("limit must be an integer between 1 and 10000")
    if not isinstance(source_ids, tuple) or any(not isinstance(item, str) for item in source_ids):
        raise PolyHavenResourceSyncError("source_ids must be an immutable tuple of strings")
    if len(source_ids) != len(set(source_ids)):
        raise PolyHavenResourceSyncError("source_ids contains duplicates")
    if source_ids and len(source_ids) > limit:
        raise PolyHavenResourceSyncError("source_ids count exceeds limit")
    if not isinstance(force, bool):
        raise PolyHavenResourceSyncError("force must be boolean")
    if not isinstance(retry_revisions, tuple) or any(
        not isinstance(item, str) or _RESOURCE_ID_PATTERN.fullmatch(item) is None
        for item in retry_revisions
    ):
        raise PolyHavenResourceSyncError(
            "retry_revisions must contain exact lower_snake_case resource ids"
        )
    if len(retry_revisions) != len(set(retry_revisions)):
        raise PolyHavenResourceSyncError("retry_revisions contains duplicates")

    project_root = settings.project_root.resolve()
    data_dir = settings.data_dir.resolve()
    _require_inside(data_dir, project_root, "data_dir")
    catalog_path = (
        data_dir / "catalog.db"
        if database_path is None
        else _resolve_project_path(database_path, project_root)
    )
    _require_inside(catalog_path, project_root, "catalog database")
    selected_clock = SystemClock() if clock is None else clock
    config = PolyHavenRuntimeConfig() if runtime_config is None else runtime_config
    if not isinstance(config, PolyHavenRuntimeConfig):
        raise PolyHavenResourceSyncError("runtime_config must be PolyHavenRuntimeConfig")

    control_root = data_dir / "acquire/polyhaven/resources" / checked_kind
    state_path = control_root / "state.json"
    journal_path = control_root / "failure_journal.json"
    runs_root = project_root / "out/acquire/polyhaven-resources" / checked_kind
    evidence_root = project_root / "out/resources/polyhaven"
    with polyhaven_source_lock(data_dir):
        _reject_symlink_ancestors(catalog_path, project_root=project_root)
        if catalog_path.is_symlink():
            raise PolyHavenPathSecurityError(
                f"catalog database must not be a symlink: {catalog_path}"
            )
        session = PolyHavenProviderSession(
            project_root=project_root,
            data_dir=data_dir,
            config=config,
            clock=selected_clock,
            storage_root=data_dir / "acquire/polyhaven",
            additional_storage_roots=(data_dir / "hdri",),
        )
        _reject_symlink_ancestors(control_root, project_root=project_root)
        _reject_symlink_ancestors(runs_root, project_root=project_root)
        _reject_symlink_ancestors(evidence_root, project_root=project_root)
        control_root.mkdir(parents=True, exist_ok=True)
        runs_root.mkdir(parents=True, exist_ok=True)
        evidence_root.mkdir(parents=True, exist_ok=True)
        _reconcile_force_candidates(
            data_dir / "acquire/polyhaven/resources/.force_verify",
            project_root=project_root,
        )
        state = _load_or_create_state(state_path, checked_kind)
        state_changed = _reconcile_compatibility_intents(
            control_root / "compatibility_intents",
            kind=checked_kind,
            state=state,
            project_root=project_root,
            data_dir=data_dir,
            catalog_path=catalog_path,
            session=session,
        )
        state_changed = (
            _reconcile_catalog_ready_resources(
                kind=checked_kind,
                state=state,
                project_root=project_root,
                data_dir=data_dir,
                catalog_path=catalog_path,
                session=session,
            )
            or state_changed
        )
        journal = _load_or_create_journal(journal_path, checked_kind)
        state_changed = (
            _reconcile_run_manifests(
                runs_root,
                selected_clock,
                project_root=project_root,
                kind=checked_kind,
                state=state,
                journal=journal,
            )
            or state_changed
        )
        if state_changed:
            state["updated_at"] = _timestamp(selected_clock)
            _write_json_atomic(state_path, state)
        run_id = _run_id(selected_clock)
        manifest_path = runs_root / run_id / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=False)
        started_at = _timestamp(selected_clock)
        running: dict[str, Any] = {
            "schema_version": RESOURCE_RUN_SCHEMA_VERSION,
            "source": POLYHAVEN_SOURCE,
            "asset_type": checked_kind,
            "run_id": run_id,
            "status": "running",
            "started_at": started_at,
            "completed_at": None,
            "request": {
                "kind": checked_kind,
                "resolution": checked_resolution,
                "limit": limit,
                "source_ids": list(source_ids),
                "force": force,
                "retry_revisions": list(retry_revisions),
                "runtime": config.as_dict(),
            },
            "listing": None,
            "active_attempt": None,
            "journal_event_refs": [],
            "items": [],
            "runtime": None,
            "error": None,
        }
        _write_json_atomic(manifest_path, running)
        journal, recovery_event_refs = _resolve_recovered_ready_failures(
            journal,
            recovered_resource_ids=set(state["items"]),
            state=state,
            kind=checked_kind,
            run_id=run_id,
            clock=selected_clock,
            journal_path=journal_path,
        )
        if recovery_event_refs:
            running["journal_event_refs"] = recovery_event_refs
            _write_json_atomic(manifest_path, running)

        try:
            listing_url = POLYHAVEN_RESOURCE_LISTING_URLS[checked_kind]
            listing_payload = session.fetch_json(listing_url)
            listings = parse_polyhaven_resource_listing(listing_payload, checked_kind)
            listing_sha = _listing_sha256(listings)
            running["listing"] = {
                "url": listing_url,
                "sha256": listing_sha,
                "discovered": len(listings),
            }
            _write_json_atomic(manifest_path, running)

            _validate_retry_selection(
                listings,
                retry_revisions=retry_revisions,
                source_ids=source_ids,
                resolution=checked_resolution,
                limit=limit,
            )
            journal, release_refs = _release_failures(
                journal,
                retry_revisions=retry_revisions,
                kind=checked_kind,
                run_id=run_id,
                clock=selected_clock,
                journal_path=journal_path,
            )
            running["journal_event_refs"].extend(release_refs)
            if release_refs:
                _write_json_atomic(manifest_path, running)

            active = validate_failure_journal(
                journal, source=POLYHAVEN_SOURCE, asset_type=checked_kind
            )
            alias_repair_ids = _hdri_alias_repair_resource_ids(
                listings,
                resolution=checked_resolution,
                source_ids=source_ids,
                state=state,
                project_root=project_root,
                data_dir=data_dir,
                catalog_path=catalog_path,
            )
            candidates, predeferred = _select_listings(
                listings,
                source_ids=source_ids,
                limit=limit,
                resolution=checked_resolution,
                state=state,
                active_failures=active,
                now=_whole_second(selected_clock.utc_now()),
                priority_resource_ids=tuple(dict.fromkeys((*retry_revisions, *alias_repair_ids))),
            )
            items: list[PolyHavenResourceSyncItem] = list(predeferred)
            if catalog_path.is_symlink():
                raise PolyHavenPathSecurityError(
                    f"catalog database must not be a symlink: {catalog_path}"
                )
            selection_catalog = (
                None
                if not catalog_path.exists()
                else Catalog(catalog_path, project_root=project_root)
            )
            catalog_existing_ids = (
                set()
                if selection_catalog is None
                else {
                    item.resource_id(checked_resolution)
                    for item in candidates
                    if selection_catalog.get_resource(item.resource_id(checked_resolution))
                    is not None
                }
            )
            fresh_quota_ids = tuple(
                _quota_item_id(item.resource_id(checked_resolution))
                for item in candidates
                if item.resource_id(checked_resolution) not in state["items"]
                and item.resource_id(checked_resolution) not in catalog_existing_ids
            )
            deferred_quota_ids: set[str] = set()
            if fresh_quota_ids:
                _allowed_quota_ids, deferred_ids = session.reserve_items_bounded(fresh_quota_ids)
                deferred_quota_ids = set(deferred_ids)
            if deferred_quota_ids:
                eligible_candidates: list[PolyHavenResourceListing] = []
                for listing in candidates:
                    quota_id = _quota_item_id(listing.resource_id(checked_resolution))
                    if quota_id in deferred_quota_ids:
                        items.append(_quota_deferred_item(listing, checked_resolution))
                    else:
                        eligible_candidates.append(listing)
                candidates = tuple(eligible_candidates)

            for ordinal, listing in enumerate(candidates, start=1):
                resource_id = listing.resource_id(checked_resolution)
                attempt_id = f"{run_id}:{ordinal}"
                running["active_attempt"] = {
                    "attempt_id": attempt_id,
                    "ordinal": ordinal,
                    "resource_id": resource_id,
                    "source_id": listing.source_id,
                    "revision": listing.revision,
                    "resolution": checked_resolution,
                    "started_at": _timestamp(selected_clock),
                }
                _write_json_atomic(manifest_path, running)
                terminal_receipt: dict[str, Any] | None = None
                try:
                    existing = state["items"].get(resource_id)
                    if existing is not None:
                        item = _replay_terminal_item(
                            existing,
                            project_root=project_root,
                            data_dir=data_dir,
                            catalog_path=catalog_path,
                            listing=listing,
                            resolution=checked_resolution,
                            session=session,
                        )
                        if existing.get("listing_evidence", {}).get("mode") != "live":
                            existing["listing_evidence"] = _listing_evidence(listing, mode="live")
                            terminal_receipt = existing
                        if force:
                            item = _force_verify_terminal_item(
                                existing,
                                replayed=item,
                                listing=listing,
                                resolution=checked_resolution,
                                data_dir=data_dir,
                                session=session,
                            )
                        journal, resolution_ref = _resolve_success_failure(
                            journal,
                            kind=checked_kind,
                            resource_id=resource_id,
                            source_id=listing.source_id,
                            revision=listing.revision,
                            resolution=checked_resolution,
                            run_id=run_id,
                            attempt_id=attempt_id,
                            clock=selected_clock,
                            journal_path=journal_path,
                        )
                        if resolution_ref is not None:
                            running["journal_event_refs"].append(resolution_ref)
                    else:
                        published = (
                            None
                            if not catalog_path.exists()
                            else Catalog(catalog_path, project_root=project_root).get_resource(
                                resource_id
                            )
                        )
                        if published is not None and published.status != "ready":
                            raise PolyHavenResourceSyncError(
                                "catalog already contains a non-ready resource with this "
                                "immutable revision identity"
                            )
                        if published is not None:
                            item, receipt = _recover_catalog_terminal_item(
                                listing=listing,
                                resolution=checked_resolution,
                                project_root=project_root,
                                data_dir=data_dir,
                                catalog_path=catalog_path,
                                session=session,
                            )
                            if force:
                                item = _force_verify_terminal_item(
                                    receipt,
                                    replayed=item,
                                    listing=listing,
                                    resolution=checked_resolution,
                                    data_dir=data_dir,
                                    session=session,
                                )
                        else:
                            item, receipt = _sync_resource_revision(
                                listing=listing,
                                resolution=checked_resolution,
                                force=force,
                                project_root=project_root,
                                data_dir=data_dir,
                                evidence_root=evidence_root,
                                catalog_path=catalog_path,
                                session=session,
                            )
                        journal, resolution_ref = _resolve_success_failure(
                            journal,
                            kind=checked_kind,
                            resource_id=resource_id,
                            source_id=listing.source_id,
                            revision=listing.revision,
                            resolution=checked_resolution,
                            run_id=run_id,
                            attempt_id=attempt_id,
                            clock=selected_clock,
                            journal_path=journal_path,
                        )
                        if resolution_ref is not None:
                            running["journal_event_refs"].append(resolution_ref)
                        terminal_receipt = receipt
                except (ProviderOperationError, ResourceValidationError) as exc:
                    failure, attempts, deadline = _classified_failure(exc)
                    journal, event = append_failure_event(
                        journal,
                        source=POLYHAVEN_SOURCE,
                        asset_type=checked_kind,
                        asset_id=resource_id,
                        source_id=listing.source_id,
                        revision=listing.revision,
                        resolution=checked_resolution,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        failure=failure,
                        recorded_at=_next_journal_datetime(
                            journal, _whole_second(selected_clock.utc_now())
                        ),
                        policy=config.failure_policy,
                        retry_after_deadline=deadline,
                        attempts_in_run=attempts,
                    )
                    _write_json_atomic(journal_path, journal)
                    ref = _event_ref(event)
                    running["journal_event_refs"].append(ref)
                    item = _failed_item(listing, checked_resolution, event)
                except (PolyHavenPathSecurityError, PolyHavenIntegrityError) as exc:
                    failure_kind = (
                        FailureKind.PATH_SECURITY
                        if isinstance(exc, PolyHavenPathSecurityError)
                        else FailureKind.INTEGRITY
                    )
                    journal, event = _journal_adapter_failure(
                        journal,
                        journal_path=journal_path,
                        kind=checked_kind,
                        listing=listing,
                        resolution=checked_resolution,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        clock=selected_clock,
                        policy=config.failure_policy,
                        failure=AcquisitionFailure(
                            kind=failure_kind,
                            phase="resource_files",
                            message=str(exc),
                        ),
                    )
                    ref = _event_ref(event)
                    running["journal_event_refs"].append(ref)
                    item = _failed_item(listing, checked_resolution, event)
                except PolyHavenAcquireError as exc:
                    journal, event = _journal_adapter_failure(
                        journal,
                        journal_path=journal_path,
                        kind=checked_kind,
                        listing=listing,
                        resolution=checked_resolution,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        clock=selected_clock,
                        policy=config.failure_policy,
                        failure=AcquisitionFailure(
                            kind=FailureKind.SCHEMA,
                            phase="resource_schema",
                            message=str(exc),
                        ),
                    )
                    ref = _event_ref(event)
                    running["journal_event_refs"].append(ref)
                    item = _failed_item(listing, checked_resolution, event)
                items.append(item)
                running["items"] = [child.as_dict(project_root=project_root) for child in items]
                running["active_attempt"] = None
                _write_json_atomic(manifest_path, running)
                if terminal_receipt is not None:
                    state["items"][resource_id] = terminal_receipt
                    state["updated_at"] = _timestamp(selected_clock)
                    _write_json_atomic(state_path, state)

            running["items"] = [child.as_dict(project_root=project_root) for child in items]
            run_status = _run_status(tuple(items))
            completed_at = _timestamp(selected_clock)
            final_manifest = dict(running)
            final_manifest["status"] = run_status
            final_manifest["completed_at"] = completed_at
            final_manifest["active_attempt"] = None
            final_manifest["runtime"] = dict(session.runtime_evidence())
            final_manifest["failure_journal"] = _journal_receipt(
                journal, journal_path, project_root
            )
            _write_json_atomic(manifest_path, final_manifest)
            manifest_sha = _sha256_file(manifest_path)
            state["last_listing"] = {
                "url": listing_url,
                "sha256": listing_sha,
                "discovered": len(listings),
                "recorded_at": completed_at,
            }
            state["run_receipts"][run_id] = {
                "status": run_status,
                "manifest_path": _portable_path(manifest_path, project_root),
                "manifest_sha256": manifest_sha,
                "completed_at": completed_at,
            }
            state["updated_at"] = completed_at
            _write_json_atomic(state_path, state)
            return PolyHavenResourceSyncResult(
                kind=checked_kind,
                resolution=checked_resolution,
                run_id=run_id,
                status=run_status,
                manifest_path=manifest_path,
                state_path=state_path,
                failure_journal_path=journal_path,
                catalog_path=catalog_path,
                listing_sha256=listing_sha,
                items=tuple(items),
            )
        except BaseException as exc:
            _persist_run_error(
                manifest_path,
                exc,
                clock=selected_clock,
                runtime=session.runtime_evidence(),
            )
            raise


def _sync_resource_revision(
    *,
    listing: PolyHavenResourceListing,
    resolution: str,
    force: bool,
    project_root: Path,
    data_dir: Path,
    evidence_root: Path,
    catalog_path: Path,
    session: PolyHavenProviderSession,
) -> tuple[PolyHavenResourceSyncItem, dict[str, Any]]:
    resource_id = listing.resource_id(resolution)
    files_url = POLYHAVEN_FILES_URL.format(source_id=quote(listing.source_id, safe=""))
    files_payload = session.fetch_json(files_url)
    package = parse_polyhaven_resource_files(
        listing.source_id, files_payload, listing.kind, resolution
    )
    storage_specs = package.storage_files(listing.revision)
    root_dir = data_dir / package.storage_root(listing.revision)
    _require_inside(root_dir, data_dir, "resource storage root")
    _reject_symlink_ancestors(root_dir, project_root=project_root)
    root_dir.mkdir(parents=True, exist_ok=True)

    downloaded_files = 0
    reused_files = 0
    downloaded_bytes = 0
    file_paths: list[Path] = []
    for provider_spec, storage_spec in zip(package.files, storage_specs, strict=True):
        destination = data_dir / storage_spec.relative_path
        if listing.kind == "hdri" and not force and not destination.exists():
            _adopt_legacy_hdri(
                listing=listing,
                resolution=resolution,
                spec=provider_spec,
                destination=destination,
                data_dir=data_dir,
                project_root=project_root,
                session=session,
            )
        result = session.acquire_file(
            PolyHavenFileSpec(
                relative_path=provider_spec.relative_path,
                url=provider_spec.url,
                bytes=provider_spec.bytes,
                md5=provider_spec.md5,
            ),
            destination=destination,
            force=force,
            item_id=_quota_item_id(resource_id),
        )
        file_paths.append(result.path)
        downloaded_files += int(not result.reused)
        reused_files += int(result.reused)
        downloaded_bytes += result.downloaded_bytes

    validation = _validate_resource(package, listing, tuple(file_paths))
    relative_files = tuple(path.relative_to(root_dir) for path in file_paths)
    source_bundle = bundle_sha256(root_dir, relative_files)
    source_content = content_sha256(root_dir, relative_files)
    resource, catalog_files = _catalog_resource_cohort(
        listing=listing,
        package=package,
        paths=tuple(file_paths),
        validation=validation,
        bundle_hash=source_bundle,
        content_hash=source_content,
        project_root=project_root,
    )
    artifact_dir = evidence_root / resource_id
    _require_inside(artifact_dir, evidence_root, "resource artifact directory")
    _reject_symlink_ancestors(artifact_dir, project_root=project_root)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifacts = _catalog_artifacts(
        resource=resource,
        files=catalog_files,
        validation=validation,
        listing=listing,
        package=package,
        artifact_dir=artifact_dir,
        project_root=project_root,
    )
    prepared_compatibility: _PreparedHdriCompatibility | None = None
    compatibility_intent_path: Path | None = None
    if listing.kind == "hdri":
        prepared_compatibility = _prepare_hdri_compatibility(
            listing=listing,
            resolution=resolution,
            source_path=file_paths[0],
            file_spec=package.files[0],
            resource_id=resource_id,
            catalog_path=catalog_path,
            data_dir=data_dir,
            project_root=project_root,
            session=session,
        )
        compatibility_intent_path = _compatibility_intent_path(data_dir, listing.kind, resource_id)
        _persist_compatibility_intent(
            compatibility_intent_path,
            prepared_compatibility,
            resource_id=resource_id,
            kind=listing.kind,
            catalog_path=catalog_path,
            project_root=project_root,
            status="prepared",
        )
    catalog = Catalog(catalog_path, project_root=project_root)
    try:
        record, file_records, artifact_records = catalog.finalize_resource(
            resource, catalog_files, artifacts
        )
    except BaseException:
        if prepared_compatibility is not None:
            _discard_prepared_hdri_compatibility(prepared_compatibility)
        if compatibility_intent_path is not None:
            _clear_compatibility_intent(compatibility_intent_path)
        raise
    catalog_projection = _catalog_projection(record, file_records, artifact_records)
    catalog_digest = _domain_sha256(_CATALOG_DIGEST_DOMAIN, catalog_projection)

    compatibility_path: Path | None = None
    compatibility_metadata_path: Path | None = None
    if prepared_compatibility is not None:
        if compatibility_intent_path is None:  # pragma: no cover - constructed together
            raise PolyHavenResourceSyncError("HDRI compatibility intent is missing")
        _persist_compatibility_intent(
            compatibility_intent_path,
            prepared_compatibility,
            resource_id=resource_id,
            kind=listing.kind,
            catalog_path=catalog_path,
            project_root=project_root,
            status="catalog_committed",
        )
        try:
            compatibility_path, compatibility_metadata_path = _commit_prepared_hdri_compatibility(
                prepared_compatibility,
                project_root=project_root,
                allowed_root=data_dir,
            )
        except BaseException as exc:
            raise PolyHavenResourceCommitPendingError(
                f"ready catalog resource has a pending HDRI compatibility commit: {resource_id}"
            ) from exc
        _clear_compatibility_intent(compatibility_intent_path)

    receipt = {
        "schema_version": RESOURCE_STATE_SCHEMA_VERSION,
        "resource_id": resource_id,
        "kind": listing.kind,
        "profile": listing.profile,
        "source_id": listing.source_id,
        "revision": listing.revision,
        "resolution": resolution,
        "root_dir": _portable_path(root_dir, project_root),
        "bundle_sha256": source_bundle,
        "content_sha256": source_content,
        "provider_files": [
            {
                "provider_role": spec.provider_role,
                "path": _portable_path(path, project_root),
                "url": spec.url,
                "bytes": spec.bytes,
                "md5": spec.md5,
                "sha256": catalog_file.sha256,
            }
            for spec, path, catalog_file in zip(
                package.files, file_paths, catalog_files, strict=True
            )
        ],
        "artifacts": [
            {
                "artifact_id": artifact.artifact_id,
                "kind": artifact.kind,
                "path": _portable_path(project_root / Path(artifact.path), project_root),
                "sha256": artifact.sha256,
            }
            for artifact in artifacts
        ],
        "catalog": {
            "database": _portable_path(catalog_path, project_root),
            "projection_sha256": catalog_digest,
            "projection": catalog_projection,
        },
        "compatibility": {
            "path": (
                None
                if compatibility_path is None
                else _portable_path(compatibility_path, project_root)
            ),
            "metadata_path": (
                None
                if compatibility_metadata_path is None
                else _portable_path(compatibility_metadata_path, project_root)
            ),
        },
        "listing_evidence": _listing_evidence(listing, mode="live"),
        "ready_at": _utc_now(),
    }
    _validate_state_item(receipt, resource_id=resource_id, kind=listing.kind)
    item = PolyHavenResourceSyncItem(
        resource_id=resource_id,
        kind=listing.kind,
        source_id=listing.source_id,
        revision=listing.revision,
        resolution=resolution,
        status="ready",
        root_dir=root_dir,
        file_paths=tuple(file_paths),
        artifact_paths=tuple(project_root / Path(item.path) for item in artifacts),
        compatibility_path=compatibility_path,
        compatibility_metadata_path=compatibility_metadata_path,
        downloaded_files=downloaded_files,
        reused_files=reused_files,
        downloaded_bytes=downloaded_bytes,
        verified_bytes=sum(spec.bytes for spec in package.files),
        bundle_sha256=source_bundle,
        content_sha256=source_content,
    )
    return item, receipt


def _validate_resource(
    package: PolyHavenResourcePackage,
    listing: PolyHavenResourceListing,
    paths: tuple[Path, ...],
) -> RadianceHdrValidationEvidence | PbrCohortValidationEvidence:
    if package.kind == "hdri":
        spec = package.files[0]
        return validate_radiance_hdr(paths[0], expected_size=spec.bytes, provider_md5=spec.md5)
    if listing.physical_size_mm is None:  # pragma: no cover - parser enforces this
        raise PolyHavenResourceSyncError("PBR listing has no physical dimensions")
    return validate_pbr_cohort(
        maps=tuple(
            PbrMapInput(
                role=spec.provider_role,
                path=path,
                expected_size=spec.bytes,
                provider_md5=spec.md5,
            )
            for spec, path in zip(package.files, paths, strict=True)
        ),
        physical_size_mm=listing.physical_size_mm,
    )


def _resource_tags(listing: PolyHavenResourceListing) -> tuple[str, ...]:
    return tuple(
        tag
        for tag in dict.fromkeys(
            (
                "polyhaven",
                "hdri" if listing.kind == "hdri" else "pbr",
                *listing.categories,
                *listing.tags,
            )
        )
        if len(tag) <= 64
    )


def _resource_attribution(listing: PolyHavenResourceListing) -> str:
    return ", ".join(name for name, _credit in listing.authors)


def _catalog_resource_cohort(
    *,
    listing: PolyHavenResourceListing,
    package: PolyHavenResourcePackage,
    paths: tuple[Path, ...],
    validation: RadianceHdrValidationEvidence | PbrCohortValidationEvidence,
    bundle_hash: str,
    content_hash: str,
    project_root: Path,
) -> tuple[ResourceUpsert, tuple[ResourceFileUpsert, ...]]:
    resource_id = listing.resource_id(package.resolution)
    tags = _resource_tags(listing)
    resource = ResourceUpsert(
        resource_id=resource_id,
        resource_kind=listing.kind,
        profile=listing.profile,
        resolution=package.resolution,
        name=listing.name,
        source=POLYHAVEN_SOURCE,
        source_id=listing.source_id,
        source_url=POLYHAVEN_ASSET_URL.format(source_id=quote(listing.source_id, safe="")),
        source_revision=listing.revision,
        source_revision_scheme="sha1_files_hash",
        license=POLYHAVEN_LICENSE,
        license_tier="open",
        license_url=POLYHAVEN_LICENSE_URL,
        attribution=_resource_attribution(listing),
        status="ready",
        tags=tags,
        bundle_sha256=bundle_hash,
        content_sha256=content_hash,
        physical_size_mm=listing.physical_size_mm,
    )
    if isinstance(validation, RadianceHdrValidationEvidence):
        spec = package.files[0]
        return resource, (
            ResourceFileUpsert(
                file_id=_child_id(resource_id, "file", "environment_radiance"),
                resource_id=resource_id,
                semantic_role="environment_radiance",
                provider_role="hdri",
                resolution=package.resolution,
                format="hdr",
                path=_portable_path(paths[0], project_root),
                source_url=spec.url,
                byte_size=spec.bytes,
                provider_md5=spec.md5,
                sha256=validation.file.sha256,
                color_space="linear",
                width=validation.width,
                height=validation.height,
                is_primary=True,
                params={
                    "encoding": validation.encoding,
                    "format": validation.format,
                    "orientation": validation.orientation,
                    "scanlines": validation.scanlines,
                },
            ),
        )

    by_role = {item.descriptor.role: item for item in validation.maps}
    files: list[ResourceFileUpsert] = []
    role_mapping = {
        "Diffuse": ("base_color", "srgb", None, None),
        "nor_dx": ("normal", "data", "directx", None),
        "arm": (
            "packed_material",
            "data",
            None,
            {
                "r": "ambient_occlusion",
                "g": "roughness",
                "b": "metallic",
            },
        ),
    }
    for spec, path in zip(package.files, paths, strict=True):
        evidence = by_role[cast(Any, spec.provider_role)]
        semantic, color_space, normal, channels = role_mapping[spec.provider_role]
        files.append(
            ResourceFileUpsert(
                file_id=_child_id(resource_id, "file", semantic),
                resource_id=resource_id,
                semantic_role=semantic,
                provider_role=spec.provider_role,
                resolution=package.resolution,
                format="png",
                path=_portable_path(path, project_root),
                source_url=spec.url,
                byte_size=spec.bytes,
                provider_md5=spec.md5,
                sha256=evidence.image.file.sha256,
                color_space=color_space,
                normal_convention=normal,
                channels=channels,
                width=evidence.image.width,
                height=evidence.image.height,
                is_primary=semantic == "base_color",
                params={
                    "bit_depth": evidence.image.bit_depth,
                    "mode": evidence.image.mode,
                },
            )
        )
    return resource, tuple(files)


def _catalog_artifacts(
    *,
    resource: ResourceUpsert,
    files: tuple[ResourceFileUpsert, ...],
    validation: RadianceHdrValidationEvidence | PbrCohortValidationEvidence,
    listing: PolyHavenResourceListing,
    package: PolyHavenResourcePackage,
    artifact_dir: Path,
    project_root: Path,
) -> tuple[ResourceArtifactUpsert, ...]:
    if resource.bundle_sha256 is None or resource.content_sha256 is None:  # pragma: no cover
        raise PolyHavenResourceSyncError("ready resource has no source hashes")
    common: dict[str, Any] = {
        "schema_version": RESOURCE_EVIDENCE_SCHEMA_VERSION,
        "resource_id": resource.resource_id,
        "resource_kind": resource.resource_kind,
        "profile": resource.profile,
        "resolution": resource.resolution,
        "bundle_sha256": resource.bundle_sha256,
        "content_sha256": resource.content_sha256,
    }
    file_ids = sorted(item.file_id for item in files)
    source_payload = {
        **common,
        "source": {
            "provider": POLYHAVEN_SOURCE,
            "source_id": listing.source_id,
            "source_url": resource.source_url,
            "revision": listing.revision,
            "revision_scheme": resource.source_revision_scheme,
            "files_url": POLYHAVEN_FILES_URL.format(source_id=quote(listing.source_id, safe="")),
            "license": POLYHAVEN_LICENSE,
            "license_url": POLYHAVEN_LICENSE_URL,
            "authors": [list(item) for item in listing.authors],
            "date_published": listing.date_published,
            "categories": list(listing.categories),
            "tags": list(listing.tags),
        },
        "files": [
            {
                "file_id": item.file_id,
                "semantic_role": item.semantic_role,
                "provider_role": item.provider_role,
                "path": _portable_path(project_root / Path(item.path), project_root),
                "source_url": item.source_url,
                "bytes": item.byte_size,
                "provider_md5": item.provider_md5,
                "sha256": item.sha256,
            }
            for item in files
        ],
    }
    payloads: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("resource_source_manifest", source_payload, dict(common))
    ]
    if isinstance(validation, RadianceHdrValidationEvidence):
        params = {
            **common,
            "validation_status": "passed",
            "width": validation.width,
            "height": validation.height,
            "file_id": files[0].file_id,
        }
        payloads.append(
            (
                "hdri_validation_manifest",
                {
                    **params,
                    "encoding": validation.encoding,
                    "format": validation.format,
                    "orientation": validation.orientation,
                    "scanlines": validation.scanlines,
                    "file": _file_evidence_payload(validation.file, project_root),
                },
                params,
            )
        )
    else:
        descriptor_params = {
            **common,
            "file_ids": file_ids,
            "physical_size_mm": list(validation.descriptor.physical_size_mm),
        }
        payloads.append(
            (
                "pbr_material_descriptor",
                {
                    **descriptor_params,
                    "pixel_dimensions": list(validation.descriptor.pixel_dimensions),
                    "maps": [asdict(item) for item in validation.descriptor.maps],
                },
                descriptor_params,
            )
        )
        validation_params = {
            **common,
            "validation_status": "passed",
            "file_ids": file_ids,
        }
        payloads.append(
            (
                "pbr_validation_manifest",
                {
                    **validation_params,
                    "maps": [
                        {
                            "descriptor": asdict(item.descriptor),
                            "image": {
                                "file": _file_evidence_payload(item.image.file, project_root),
                                "width": item.image.width,
                                "height": item.image.height,
                                "bit_depth": item.image.bit_depth,
                                "channels": item.image.channels,
                                "mode": item.image.mode,
                            },
                        }
                        for item in validation.maps
                    ],
                },
                validation_params,
            )
        )

    artifacts: list[ResourceArtifactUpsert] = []
    for kind, payload, params in payloads:
        path = artifact_dir / f"{kind}.json"
        sha256 = _write_immutable_json(path, payload)
        artifacts.append(
            ResourceArtifactUpsert(
                artifact_id=_child_id(resource.resource_id, "artifact", kind),
                resource_id=resource.resource_id,
                kind=kind,
                path=_portable_path(path, project_root),
                params=params,
                sha256=sha256,
            )
        )
    return tuple(artifacts)


def _replay_terminal_item(
    receipt: Any,
    *,
    project_root: Path,
    data_dir: Path,
    catalog_path: Path,
    listing: PolyHavenResourceListing,
    resolution: str,
    session: PolyHavenProviderSession,
) -> PolyHavenResourceSyncItem:
    resource_id = listing.resource_id(resolution)
    checked = _validate_state_item(receipt, resource_id=resource_id, kind=listing.kind)
    if (
        checked["revision"] != listing.revision
        or checked["source_id"] != listing.source_id
        or checked["profile"] != listing.profile
        or checked["resolution"] != resolution
    ):
        raise PolyHavenResourceSyncError("terminal resource state differs from live listing")
    listing_evidence = checked.get("listing_evidence")
    if (
        isinstance(listing_evidence, dict)
        and listing_evidence.get("mode") == "live"
        and listing_evidence != _listing_evidence(listing, mode="live")
    ):
        raise PolyHavenResourceSyncError("live listing changed within one files_hash revision")
    if isinstance(listing_evidence, dict) and listing_evidence.get("mode") == "catalog_recovery":
        recovered_projection = cast(dict[str, Any], listing_evidence["projection"])
        live_projection = _listing_projection(listing)
        changed = [
            key for key, value in recovered_projection.items() if live_projection.get(key) != value
        ]
        if changed:
            raise PolyHavenResourceSyncError(
                "live listing differs from immutable source manifest evidence: "
                + ", ".join(sorted(changed))
            )
    root_dir = _path_from_portable(checked["root_dir"], project_root)
    _require_inside(root_dir, data_dir, "terminal resource root")
    expected_root = data_dir / resource_storage_root(
        listing.kind,
        listing.source_id,
        listing.revision,
        resolution,
    )
    if root_dir != expected_root:
        raise PolyHavenResourceSyncError(
            "terminal resource root differs from its canonical revision path"
        )
    provider_files = checked["provider_files"]
    if any(not isinstance(item, dict) for item in provider_files):
        raise PolyHavenResourceSyncError("terminal provider file receipt is invalid")
    paths = tuple(_path_from_portable(item["path"], project_root) for item in provider_files)
    recorded_projection = checked["catalog"]["projection"]
    if not isinstance(recorded_projection, dict):
        raise PolyHavenResourceSyncError("terminal catalog projection is invalid")
    _validate_listing_projection(recorded_projection.get("resource"), listing, resolution)
    projected_files = recorded_projection.get("files")
    projected_artifacts = recorded_projection.get("artifacts")
    if not isinstance(projected_files, list) or not isinstance(projected_artifacts, list):
        raise PolyHavenResourceSyncError("terminal catalog projection cohorts are invalid")
    state_artifact_ids = [
        item.get("artifact_id") if isinstance(item, dict) else None for item in checked["artifacts"]
    ]
    projected_artifact_ids = [
        item.get("artifact_id") if isinstance(item, dict) else None for item in projected_artifacts
    ]
    if len(state_artifact_ids) != len(set(state_artifact_ids)) or sorted(
        state_artifact_ids, key=str
    ) != sorted(projected_artifact_ids, key=str):
        raise PolyHavenResourceSyncError("terminal resource artifact cohort is incomplete")
    catalog_files_by_role = {
        item.get("provider_role"): item for item in projected_files if isinstance(item, dict)
    }
    if len(catalog_files_by_role) != len(provider_files):
        raise PolyHavenResourceSyncError("terminal provider files differ from the catalog cohort")
    for provider_file, path in zip(provider_files, paths, strict=True):
        catalog_file = catalog_files_by_role.get(provider_file.get("provider_role"))
        if not isinstance(catalog_file, dict) or any(
            catalog_file.get(catalog_key) != provider_file.get(state_key)
            for catalog_key, state_key in (
                ("path", "path"),
                ("source_url", "url"),
                ("byte_size", "bytes"),
                ("provider_md5", "md5"),
                ("sha256", "sha256"),
            )
        ):
            raise PolyHavenResourceSyncError("terminal provider file does not bind its catalog row")
        if path.parent != root_dir:
            raise PolyHavenResourceSyncError(
                "terminal provider file is outside its canonical resource root"
            )
    if listing.kind == "hdri":
        if len(paths) != 1:
            raise PolyHavenResourceSyncError("terminal HDRI state has wrong file count")
        validate_radiance_hdr(
            paths[0],
            expected_size=provider_files[0]["bytes"],
            provider_md5=provider_files[0]["md5"],
        )
    else:
        if listing.physical_size_mm is None:
            raise PolyHavenResourceSyncError("terminal PBR listing has no physical size")
        validate_pbr_cohort(
            maps=tuple(
                PbrMapInput(
                    role=item["provider_role"],
                    path=path,
                    expected_size=item["bytes"],
                    provider_md5=item["md5"],
                )
                for item, path in zip(provider_files, paths, strict=True)
            ),
            physical_size_mm=listing.physical_size_mm,
        )
    relative_files = tuple(path.relative_to(root_dir) for path in paths)
    if bundle_sha256(root_dir, relative_files) != checked["bundle_sha256"]:
        raise PolyHavenResourceSyncError("terminal resource bundle hash changed")
    if content_sha256(root_dir, relative_files) != checked["content_sha256"]:
        raise PolyHavenResourceSyncError("terminal resource content hash changed")
    for artifact in checked["artifacts"]:
        artifact_path = _path_from_portable(artifact["path"], project_root)
        matching_artifacts = [
            row
            for row in projected_artifacts
            if isinstance(row, dict) and row.get("artifact_id") == artifact["artifact_id"]
        ]
        if len(matching_artifacts) != 1 or any(
            matching_artifacts[0].get(key) != artifact.get(key)
            for key in ("kind", "path", "sha256")
        ):
            raise PolyHavenResourceSyncError(
                "terminal resource artifact does not bind its catalog row"
            )
        _require_regular_file(artifact_path, "terminal resource artifact")
        if _sha256_file(artifact_path) != artifact["sha256"]:
            raise PolyHavenResourceSyncError("terminal resource artifact hash changed")
    catalog_receipt = checked["catalog"]
    if _path_from_portable(catalog_receipt["database"], project_root) != catalog_path:
        raise PolyHavenResourceSyncError("terminal resource catalog path changed")
    catalog = Catalog(catalog_path, project_root=project_root)
    record = catalog.get_resource(resource_id)
    if record is None:
        raise PolyHavenResourceSyncError("terminal resource disappeared from catalog")
    projection = _catalog_projection(
        record,
        catalog.list_resource_files(resource_id=resource_id),
        catalog.list_resource_artifacts(resource_id=resource_id),
    )
    digest = _domain_sha256(_CATALOG_DIGEST_DOMAIN, projection)
    if (
        digest != catalog_receipt["projection_sha256"]
        or projection != catalog_receipt["projection"]
    ):
        raise PolyHavenResourceSyncError("terminal resource catalog evidence changed")

    compatibility = checked["compatibility"]
    compatibility_path = (
        None
        if compatibility["path"] is None
        else _path_from_portable(compatibility["path"], project_root)
    )
    compatibility_metadata_path = (
        None
        if compatibility["metadata_path"] is None
        else _path_from_portable(compatibility["metadata_path"], project_root)
    )
    if listing.kind == "hdri":
        expected_compatibility_path = data_dir / "hdri" / f"{listing.source_id}_{resolution}.hdr"
        if (
            compatibility_path != expected_compatibility_path
            or compatibility_metadata_path != expected_compatibility_path.with_suffix(".json")
        ):
            raise PolyHavenResourceSyncError("terminal HDRI compatibility receipt is not canonical")
        spec = provider_files[0]
        compatibility_path, compatibility_metadata_path = _publish_hdri_compatibility(
            listing=listing,
            resolution=resolution,
            source_path=paths[0],
            file_spec=PolyHavenResourceFileSpec(
                provider_role="hdri",
                relative_path=Path(paths[0].name),
                url=spec["url"],
                bytes=spec["bytes"],
                md5=spec["md5"],
            ),
            resource_id=resource_id,
            catalog_path=catalog_path,
            data_dir=data_dir,
            project_root=project_root,
            session=session,
        )
    elif compatibility_path is not None or compatibility_metadata_path is not None:
        raise PolyHavenResourceSyncError("PBR resources may not define HDRI compatibility files")
    return PolyHavenResourceSyncItem(
        resource_id=resource_id,
        kind=listing.kind,
        source_id=listing.source_id,
        revision=listing.revision,
        resolution=resolution,
        status="skipped",
        root_dir=root_dir,
        file_paths=paths,
        artifact_paths=tuple(
            _path_from_portable(item["path"], project_root) for item in checked["artifacts"]
        ),
        compatibility_path=compatibility_path,
        compatibility_metadata_path=compatibility_metadata_path,
        reused_files=len(paths),
        verified_bytes=sum(item["bytes"] for item in provider_files),
        bundle_sha256=checked["bundle_sha256"],
        content_sha256=checked["content_sha256"],
    )


def _force_verify_terminal_item(
    receipt: Mapping[str, Any],
    *,
    replayed: PolyHavenResourceSyncItem,
    listing: PolyHavenResourceListing,
    resolution: str,
    data_dir: Path,
    session: PolyHavenProviderSession,
) -> PolyHavenResourceSyncItem:
    """Redownload a published revision without exposing it to in-place mutation.

    The provider body is written to an isolated candidate root.  Only an exact
    match to every already-published SHA-256 and bundle digest is accepted; the
    canonical cohort is left untouched even on a malicious same-revision MD5
    collision or a process crash.
    """

    resource_id = listing.resource_id(resolution)
    files_url = POLYHAVEN_FILES_URL.format(source_id=quote(listing.source_id, safe=""))
    package = parse_polyhaven_resource_files(
        listing.source_id,
        session.fetch_json(files_url),
        listing.kind,
        resolution,
    )
    recorded_files = receipt["provider_files"]
    expected_provider = [
        {
            "provider_role": item["provider_role"],
            "url": item["url"],
            "bytes": item["bytes"],
            "md5": item["md5"],
        }
        for item in recorded_files
    ]
    live_provider = [
        {
            "provider_role": item.provider_role,
            "url": item.url,
            "bytes": item.bytes,
            "md5": item.md5,
        }
        for item in package.files
    ]
    if live_provider != expected_provider:
        raise PolyHavenIntegrityError(
            "Poly Haven changed a published file cohort without changing files_hash"
        )

    candidate_root = data_dir / "acquire/polyhaven/resources/.force_verify" / resource_id
    _require_inside(candidate_root, session.storage_root, "force verification root")
    _reject_symlink_ancestors(candidate_root, project_root=session.project_root)
    candidate_root.mkdir(parents=True, exist_ok=True)
    try:
        results = []
        paths: list[Path] = []
        for spec in package.files:
            result = session.acquire_file(
                PolyHavenFileSpec(
                    relative_path=spec.relative_path,
                    url=spec.url,
                    bytes=spec.bytes,
                    md5=spec.md5,
                ),
                destination=candidate_root / spec.relative_path,
                force=True,
                item_id=_quota_item_id(resource_id),
            )
            results.append(result)
            paths.append(result.path)
        _validate_resource(package, listing, tuple(paths))
        for recorded, result in zip(recorded_files, results, strict=True):
            if recorded["sha256"] != result.sha256:
                raise PolyHavenIntegrityError(
                    "Poly Haven force verification changed SHA-256 within one revision"
                )
        relative_files = tuple(path.relative_to(candidate_root) for path in paths)
        if bundle_sha256(candidate_root, relative_files) != receipt["bundle_sha256"]:
            raise PolyHavenIntegrityError(
                "Poly Haven force verification changed the published bundle"
            )
        if content_sha256(candidate_root, relative_files) != receipt["content_sha256"]:
            raise PolyHavenIntegrityError(
                "Poly Haven force verification changed the published content cohort"
            )
        return replace(
            replayed,
            status="ready",
            downloaded_files=sum(not item.reused for item in results),
            reused_files=sum(item.reused for item in results),
            downloaded_bytes=sum(item.downloaded_bytes for item in results),
        )
    finally:
        if candidate_root.exists() and not candidate_root.is_symlink():
            shutil.rmtree(candidate_root)
            _fsync_directory(candidate_root.parent)


def _recover_catalog_terminal_item(
    *,
    listing: PolyHavenResourceListing,
    resolution: str,
    project_root: Path,
    data_dir: Path,
    catalog_path: Path,
    session: PolyHavenProviderSession,
    listing_evidence_mode: Literal["live", "catalog_recovery"] = "live",
    publish_compatibility: bool = True,
) -> tuple[PolyHavenResourceSyncItem, dict[str, Any]]:
    """Rebuild missing state after a catalog-ready/state-write crash window."""

    resource_id = listing.resource_id(resolution)
    catalog = Catalog(catalog_path, project_root=project_root)
    record = catalog.get_resource(resource_id)
    if record is None or record.status != "ready":
        raise PolyHavenResourceSyncError("catalog recovery requires an existing ready resource")
    expected_source_url = POLYHAVEN_ASSET_URL.format(source_id=quote(listing.source_id, safe=""))
    expected_metadata = {
        "resource_kind": listing.kind,
        "profile": listing.profile,
        "resolution": resolution,
        "name": listing.name,
        "source": POLYHAVEN_SOURCE,
        "source_id": listing.source_id,
        "source_url": expected_source_url,
        "source_revision": listing.revision,
        "source_revision_scheme": "sha1_files_hash",
        "license": POLYHAVEN_LICENSE,
        "license_tier": "open",
        "license_url": POLYHAVEN_LICENSE_URL,
        "attribution": _resource_attribution(listing),
        "tags": tuple(sorted(_resource_tags(listing))),
        "physical_size_mm": listing.physical_size_mm,
    }
    actual_metadata = {
        "resource_kind": record.resource_kind,
        "profile": record.profile,
        "resolution": record.resolution,
        "name": record.name,
        "source": record.source,
        "source_id": record.source_id,
        "source_url": record.source_url,
        "source_revision": record.source_revision,
        "source_revision_scheme": record.source_revision_scheme,
        "license": record.license,
        "license_tier": record.license_tier,
        "license_url": record.license_url,
        "attribution": record.attribution,
        "tags": record.tags,
        "physical_size_mm": record.physical_size_mm,
    }
    if actual_metadata != expected_metadata:
        raise PolyHavenResourceSyncError(
            "ready catalog metadata differs from the current resource listing"
        )
    if record.bundle_sha256 is None or record.content_sha256 is None:
        raise PolyHavenResourceSyncError("ready catalog resource has no source hashes")

    file_records = catalog.list_resource_files(resource_id=resource_id)
    role_order = ("hdri",) if listing.kind == "hdri" else ("Diffuse", "nor_dx", "arm")
    by_role = {item.provider_role: item for item in file_records}
    if set(by_role) != set(role_order) or len(file_records) != len(role_order):
        raise PolyHavenResourceSyncError(
            "ready catalog resource has the wrong provider file cohort"
        )
    root_dir = data_dir / resource_storage_root(
        listing.kind, listing.source_id, listing.revision, resolution
    )
    package_files: list[PolyHavenResourceFileSpec] = []
    paths: list[Path] = []
    for role in role_order:
        file_record = by_role[role]
        path = _path_from_portable(file_record.path, project_root)
        if path.parent != root_dir:
            raise PolyHavenResourceSyncError(
                "ready catalog file is outside its canonical revision root"
            )
        if not isinstance(file_record.provider_md5, str):
            raise PolyHavenResourceSyncError("ready catalog file is missing provider MD5 evidence")
        spec = PolyHavenResourceFileSpec(
            provider_role=cast(Any, role),
            relative_path=Path(path.name),
            url=file_record.source_url,
            bytes=file_record.byte_size,
            md5=file_record.provider_md5,
        )
        package_files.append(spec)
        paths.append(path)
    package = PolyHavenResourcePackage(
        kind=listing.kind,
        source_id=listing.source_id,
        profile=listing.profile,
        resolution=resolution,
        files=tuple(package_files),
    )
    validation = _validate_resource(package, listing, tuple(paths))
    for path, file_record in zip(paths, (by_role[role] for role in role_order), strict=True):
        if _sha256_file(path) != file_record.sha256:
            raise PolyHavenResourceSyncError("ready catalog source file hash changed")
    relative_files = tuple(path.relative_to(root_dir) for path in paths)
    if bundle_sha256(root_dir, relative_files) != record.bundle_sha256:
        raise PolyHavenResourceSyncError("ready catalog bundle hash changed")
    if content_sha256(root_dir, relative_files) != record.content_sha256:
        raise PolyHavenResourceSyncError("ready catalog content hash changed")

    _expected_resource, expected_files = _catalog_resource_cohort(
        listing=listing,
        package=package,
        paths=tuple(paths),
        validation=validation,
        bundle_hash=record.bundle_sha256,
        content_hash=record.content_sha256,
        project_root=project_root,
    )
    expected_files_by_role = {
        item.provider_role: _resource_file_upsert_projection(item) for item in expected_files
    }
    for role in role_order:
        actual_file = by_role[role].as_dict()
        actual_file.pop("created_at", None)
        if actual_file != expected_files_by_role[role]:
            raise PolyHavenResourceSyncError(
                f"ready catalog file semantics changed for provider role {role!r}"
            )

    artifact_records = catalog.list_resource_artifacts(resource_id=resource_id)
    required_artifact_kinds = (
        {"resource_source_manifest", "hdri_validation_manifest"}
        if listing.kind == "hdri"
        else {
            "resource_source_manifest",
            "pbr_material_descriptor",
            "pbr_validation_manifest",
        }
    )
    if {item.kind for item in artifact_records} != required_artifact_kinds or len(
        artifact_records
    ) != len(required_artifact_kinds):
        raise PolyHavenResourceSyncError("ready catalog resource has an incomplete artifact cohort")
    expected_artifact_root = project_root / "out/resources/polyhaven" / resource_id
    for artifact in artifact_records:
        path = _path_from_portable(artifact.path, project_root)
        if path != expected_artifact_root / f"{artifact.kind}.json":
            raise PolyHavenResourceSyncError(
                "ready catalog artifact is outside its canonical evidence root"
            )
        if artifact.sha256 is None or _sha256_file(path) != artifact.sha256:
            raise PolyHavenResourceSyncError("ready catalog artifact hash changed")
    recovered_listing_projection = _validate_recovered_artifact_semantics(
        resource=record,
        files=expected_files,
        artifacts=artifact_records,
        validation=validation,
        listing=listing,
        project_root=project_root,
        require_live_source=listing_evidence_mode == "live",
    )

    projection = _catalog_projection(record, file_records, artifact_records)
    projection_sha = _domain_sha256(_CATALOG_DIGEST_DOMAIN, projection)
    compatibility_path: Path | None = None
    compatibility_metadata_path: Path | None = None
    if listing.kind == "hdri":
        compatibility_path = data_dir / "hdri" / f"{listing.source_id}_{resolution}.hdr"
        compatibility_metadata_path = compatibility_path.with_suffix(".json")
        if publish_compatibility:
            compatibility_path, compatibility_metadata_path = _publish_hdri_compatibility(
                listing=listing,
                resolution=resolution,
                source_path=paths[0],
                file_spec=package.files[0],
                resource_id=resource_id,
                catalog_path=catalog_path,
                data_dir=data_dir,
                project_root=project_root,
                session=session,
            )
    receipt = {
        "schema_version": RESOURCE_STATE_SCHEMA_VERSION,
        "resource_id": resource_id,
        "kind": listing.kind,
        "profile": listing.profile,
        "source_id": listing.source_id,
        "revision": listing.revision,
        "resolution": resolution,
        "root_dir": _portable_path(root_dir, project_root),
        "bundle_sha256": record.bundle_sha256,
        "content_sha256": record.content_sha256,
        "provider_files": [
            {
                "provider_role": spec.provider_role,
                "path": file_record.path,
                "url": spec.url,
                "bytes": spec.bytes,
                "md5": spec.md5,
                "sha256": file_record.sha256,
            }
            for spec, file_record in zip(
                package.files, (by_role[role] for role in role_order), strict=True
            )
        ],
        "artifacts": [
            {
                "artifact_id": artifact.artifact_id,
                "kind": artifact.kind,
                "path": artifact.path,
                "sha256": artifact.sha256,
            }
            for artifact in artifact_records
        ],
        "catalog": {
            "database": _portable_path(catalog_path, project_root),
            "projection_sha256": projection_sha,
            "projection": projection,
        },
        "compatibility": {
            "path": (
                None
                if compatibility_path is None
                else _portable_path(compatibility_path, project_root)
            ),
            "metadata_path": (
                None
                if compatibility_metadata_path is None
                else _portable_path(compatibility_metadata_path, project_root)
            ),
        },
        "listing_evidence": (
            _listing_evidence(listing, mode="live")
            if listing_evidence_mode == "live"
            else _listing_evidence_from_projection(
                recovered_listing_projection,
                mode="catalog_recovery",
            )
        ),
        "ready_at": record.updated_at,
    }
    _validate_state_item(receipt, resource_id=resource_id, kind=listing.kind)
    item = PolyHavenResourceSyncItem(
        resource_id=resource_id,
        kind=listing.kind,
        source_id=listing.source_id,
        revision=listing.revision,
        resolution=resolution,
        status="skipped",
        root_dir=root_dir,
        file_paths=tuple(paths),
        artifact_paths=tuple(
            _path_from_portable(artifact.path, project_root) for artifact in artifact_records
        ),
        compatibility_path=compatibility_path,
        compatibility_metadata_path=compatibility_metadata_path,
        reused_files=len(paths),
        verified_bytes=sum(spec.bytes for spec in package.files),
        bundle_sha256=record.bundle_sha256,
        content_sha256=record.content_sha256,
    )
    return item, receipt


def _catalog_projection(record: Any, files: Any, artifacts: Any) -> dict[str, Any]:
    resource = record.as_dict()
    resource.pop("created_at", None)
    resource.pop("updated_at", None)
    file_rows = []
    for item in files:
        payload = item.as_dict()
        payload.pop("created_at", None)
        file_rows.append(payload)
    artifact_rows = []
    for item in artifacts:
        payload = item.as_dict()
        payload.pop("created_at", None)
        artifact_rows.append(payload)
    return {
        "resource": resource,
        "files": sorted(file_rows, key=lambda item: item["file_id"]),
        "artifacts": sorted(artifact_rows, key=lambda item: item["artifact_id"]),
    }


def _resource_file_upsert_projection(value: ResourceFileUpsert) -> dict[str, Any]:
    return {
        "file_id": value.file_id,
        "resource_id": value.resource_id,
        "semantic_role": value.semantic_role,
        "provider_role": value.provider_role,
        "resolution": value.resolution,
        "format": value.format,
        "path": str(value.path),
        "source_url": value.source_url,
        "byte_size": value.byte_size,
        "provider_md5": value.provider_md5,
        "sha256": value.sha256,
        "color_space": value.color_space,
        "normal_convention": value.normal_convention,
        "channels": {} if value.channels is None else dict(value.channels),
        "width": value.width,
        "height": value.height,
        "is_primary": value.is_primary,
        "params": {} if value.params is None else dict(value.params),
    }


def _expected_recovered_artifacts(
    *,
    resource: Any,
    files: tuple[ResourceFileUpsert, ...],
    validation: RadianceHdrValidationEvidence | PbrCohortValidationEvidence,
    listing: PolyHavenResourceListing,
    project_root: Path,
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    common: dict[str, Any] = {
        "schema_version": RESOURCE_EVIDENCE_SCHEMA_VERSION,
        "resource_id": resource.resource_id,
        "resource_kind": resource.resource_kind,
        "profile": resource.profile,
        "resolution": resource.resolution,
        "bundle_sha256": resource.bundle_sha256,
        "content_sha256": resource.content_sha256,
    }
    source_payload = {
        **common,
        "source": {
            "provider": POLYHAVEN_SOURCE,
            "source_id": listing.source_id,
            "source_url": resource.source_url,
            "revision": listing.revision,
            "revision_scheme": resource.source_revision_scheme,
            "files_url": POLYHAVEN_FILES_URL.format(source_id=quote(listing.source_id, safe="")),
            "license": POLYHAVEN_LICENSE,
            "license_url": POLYHAVEN_LICENSE_URL,
            "authors": [list(item) for item in listing.authors],
            "date_published": listing.date_published,
            "categories": list(listing.categories),
            "tags": list(listing.tags),
        },
        "files": [
            {
                "file_id": item.file_id,
                "semantic_role": item.semantic_role,
                "provider_role": item.provider_role,
                "path": str(item.path),
                "source_url": item.source_url,
                "bytes": item.byte_size,
                "provider_md5": item.provider_md5,
                "sha256": item.sha256,
            }
            for item in files
        ],
    }
    result = {"resource_source_manifest": (dict(common), source_payload)}
    if isinstance(validation, RadianceHdrValidationEvidence):
        params = {
            **common,
            "validation_status": "passed",
            "width": validation.width,
            "height": validation.height,
            "file_id": files[0].file_id,
        }
        result["hdri_validation_manifest"] = (
            params,
            {
                **params,
                "encoding": validation.encoding,
                "format": validation.format,
                "orientation": validation.orientation,
                "scanlines": validation.scanlines,
                "file": _file_evidence_payload(validation.file, project_root),
            },
        )
        return result

    file_ids = sorted(item.file_id for item in files)
    descriptor_params = {
        **common,
        "file_ids": file_ids,
        "physical_size_mm": list(validation.descriptor.physical_size_mm),
    }
    result["pbr_material_descriptor"] = (
        descriptor_params,
        {
            **descriptor_params,
            "pixel_dimensions": list(validation.descriptor.pixel_dimensions),
            "maps": [asdict(item) for item in validation.descriptor.maps],
        },
    )
    validation_params = {
        **common,
        "validation_status": "passed",
        "file_ids": file_ids,
    }
    result["pbr_validation_manifest"] = (
        validation_params,
        {
            **validation_params,
            "maps": [
                {
                    "descriptor": asdict(item.descriptor),
                    "image": {
                        "file": _file_evidence_payload(item.image.file, project_root),
                        "width": item.image.width,
                        "height": item.image.height,
                        "bit_depth": item.image.bit_depth,
                        "channels": item.image.channels,
                        "mode": item.image.mode,
                    },
                }
                for item in validation.maps
            ],
        },
    )
    return result


def _validate_recovered_source_manifest(
    actual: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    resource: Any,
    listing: PolyHavenResourceListing,
    require_live_source: bool,
) -> dict[str, Any]:
    legacy_expected = json.loads(json.dumps(expected))
    legacy_source = cast(dict[str, Any], legacy_expected["source"])
    for key in ("date_published", "categories", "tags"):
        legacy_source.pop(key)
    if require_live_source and actual not in (expected, legacy_expected):
        raise PolyHavenResourceSyncError(
            "ready catalog source manifest differs from the live listing"
        )

    expected_common = {
        key: value for key, value in expected.items() if key not in {"source", "files"}
    }
    actual_common = {key: value for key, value in actual.items() if key not in {"source", "files"}}
    if actual_common != expected_common or actual.get("files") != expected.get("files"):
        raise PolyHavenResourceSyncError("ready catalog source manifest cohort changed")
    source = actual.get("source")
    expected_source = expected.get("source")
    if not isinstance(source, dict) or not isinstance(expected_source, dict):
        raise PolyHavenResourceSyncError("ready catalog source manifest is invalid")
    optional = {"date_published", "categories", "tags"}
    required = set(expected_source) - optional
    if frozenset(source) not in {frozenset(required), frozenset(required | optional)}:
        raise PolyHavenResourceSyncError("ready catalog source manifest has an unsupported shape")
    if not require_live_source:
        for key in required - {"authors"}:
            if source.get(key) != expected_source.get(key):
                raise PolyHavenResourceSyncError("ready catalog source provenance changed")
    authors = source.get("authors")
    if (
        not isinstance(authors, list)
        or any(
            not isinstance(item, list)
            or len(item) != 2
            or any(not isinstance(part, str) for part in item)
            for item in authors
        )
        or ", ".join(item[0] for item in authors) != resource.attribution
    ):
        raise PolyHavenResourceSyncError("ready catalog source authors differ from attribution")
    projection = _listing_projection(listing)
    projection["authors"] = [list(item) for item in authors]
    if optional <= set(source):
        date_published = source["date_published"]
        categories = source["categories"]
        tags = source["tags"]
        if (
            isinstance(date_published, bool)
            or not isinstance(date_published, int)
            or date_published <= 0
            or not isinstance(categories, list)
            or not isinstance(tags, list)
            or any(not isinstance(item, str) for item in (*categories, *tags))
        ):
            raise PolyHavenResourceSyncError("ready catalog source listing evidence is invalid")
        recovered_listing = replace(
            listing,
            date_published=date_published,
            authors=tuple((item[0], item[1]) for item in authors),
            categories=tuple(categories),
            tags=tuple(tags),
        )
        if tuple(sorted(_resource_tags(recovered_listing))) != resource.tags:
            raise PolyHavenResourceSyncError(
                "ready catalog tags differ from source listing evidence"
            )
        projection["date_published"] = date_published
        projection["categories"] = list(categories)
        projection["tags"] = list(tags)
    else:
        for key in optional:
            projection.pop(key)
    return projection


def _validate_recovered_artifact_semantics(
    *,
    resource: Any,
    files: tuple[ResourceFileUpsert, ...],
    artifacts: Any,
    validation: RadianceHdrValidationEvidence | PbrCohortValidationEvidence,
    listing: PolyHavenResourceListing,
    project_root: Path,
    require_live_source: bool,
) -> dict[str, Any]:
    expected = _expected_recovered_artifacts(
        resource=resource,
        files=files,
        validation=validation,
        listing=listing,
        project_root=project_root,
    )
    recovered_listing_projection: dict[str, Any] | None = None
    for artifact in artifacts:
        pair = expected.get(artifact.kind)
        if (
            pair is None
            or artifact.artifact_id != _child_id(resource.resource_id, "artifact", artifact.kind)
            or artifact.params != pair[0]
        ):
            raise PolyHavenResourceSyncError(
                f"ready catalog artifact semantics changed for {artifact.kind!r}"
            )
        path = _path_from_portable(artifact.path, project_root)
        actual_payload = _read_json(path, f"ready {artifact.kind} artifact")
        if artifact.kind == "resource_source_manifest":
            recovered_listing_projection = _validate_recovered_source_manifest(
                actual_payload,
                pair[1],
                resource=resource,
                listing=listing,
                require_live_source=require_live_source,
            )
        elif actual_payload != pair[1]:
            raise PolyHavenResourceSyncError(
                f"ready catalog validation evidence changed for {artifact.kind!r}"
            )
    if recovered_listing_projection is None:  # pragma: no cover - cohort checked above
        raise PolyHavenResourceSyncError("ready catalog source listing evidence is missing")
    return recovered_listing_projection


def _validate_listing_projection(
    value: Any,
    listing: PolyHavenResourceListing,
    resolution: str,
) -> None:
    if not isinstance(value, dict):
        raise PolyHavenResourceSyncError("terminal resource projection is invalid")
    expected = {
        "resource_kind": listing.kind,
        "profile": listing.profile,
        "resolution": resolution,
        "name": listing.name,
        "source": POLYHAVEN_SOURCE,
        "source_id": listing.source_id,
        "source_url": POLYHAVEN_ASSET_URL.format(source_id=quote(listing.source_id, safe="")),
        "source_revision": listing.revision,
        "source_revision_scheme": "sha1_files_hash",
        "license": POLYHAVEN_LICENSE,
        "license_tier": "open",
        "license_url": POLYHAVEN_LICENSE_URL,
        "attribution": _resource_attribution(listing),
        "tags": list(sorted(_resource_tags(listing))),
        "physical_size_mm": (
            None if listing.physical_size_mm is None else list(listing.physical_size_mm)
        ),
        "status": "ready",
    }
    changed = [key for key, expected_value in expected.items() if value.get(key) != expected_value]
    if changed:
        raise PolyHavenResourceSyncError(
            "live listing metadata differs within one files_hash revision: " + ", ".join(changed)
        )


def _publish_hdri_compatibility(
    *,
    listing: PolyHavenResourceListing,
    resolution: str,
    source_path: Path,
    file_spec: PolyHavenResourceFileSpec,
    resource_id: str,
    catalog_path: Path,
    data_dir: Path,
    project_root: Path,
    session: PolyHavenProviderSession,
) -> tuple[Path, Path]:
    intent_path = _compatibility_intent_path(data_dir, listing.kind, resource_id)
    if intent_path.exists() or intent_path.is_symlink():
        _validate_compatibility_intent(
            intent_path,
            resource_id=resource_id,
            kind=listing.kind,
            catalog_path=catalog_path,
            project_root=project_root,
            data_dir=data_dir,
            expected_alias=(data_dir / "hdri" / f"{listing.source_id}_{resolution}.hdr"),
        )
    prepared = _prepare_hdri_compatibility(
        listing=listing,
        resolution=resolution,
        source_path=source_path,
        file_spec=file_spec,
        resource_id=resource_id,
        catalog_path=catalog_path,
        data_dir=data_dir,
        project_root=project_root,
        session=session,
    )
    result = _commit_prepared_hdri_compatibility(
        prepared,
        project_root=project_root,
        allowed_root=data_dir,
    )
    _clear_compatibility_intent(intent_path)
    return result


def _prepare_hdri_compatibility(
    *,
    listing: PolyHavenResourceListing,
    resolution: str,
    source_path: Path,
    file_spec: PolyHavenResourceFileSpec,
    resource_id: str,
    catalog_path: Path,
    data_dir: Path,
    project_root: Path,
    session: PolyHavenProviderSession,
) -> _PreparedHdriCompatibility:
    alias, metadata, payload = _hdri_compatibility_payload(
        listing=listing,
        resolution=resolution,
        source_path=source_path,
        file_spec=file_spec,
        resource_id=resource_id,
        catalog_path=catalog_path,
        data_dir=data_dir,
        project_root=project_root,
    )
    alias_growth = _regular_copy_growth(
        source_path,
        alias,
        project_root=project_root,
        allowed_root=data_dir,
    )
    rendered_metadata = _render_json(payload)
    metadata_growth = len(rendered_metadata)
    if metadata.exists() or metadata.is_symlink():
        _reject_symlink_ancestors(metadata, project_root=project_root)
        _require_regular_file(metadata, "HDRI compatibility metadata")
        if metadata.read_bytes() == rendered_metadata:
            metadata_growth = 0
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias_temporary = (
        None if alias_growth == 0 else alias.with_name(f".{alias.name}.{resource_id}.prepared")
    )
    metadata_temporary = (
        None
        if metadata_growth == 0
        else metadata.with_name(f".{metadata.name}.{resource_id}.prepared")
    )
    for temporary in (alias_temporary, metadata_temporary):
        if temporary is not None:
            _reset_prepared_path(temporary, project_root=project_root)
    growth = alias_growth + metadata_growth
    if growth:
        session.check_disk_growth(growth)
    if alias_temporary is not None:
        _copy_regular_file(source_path, alias_temporary)
        if _sha256_file(alias_temporary) != _sha256_file(source_path):
            raise PolyHavenResourceSyncError("prepared HDRI compatibility copy hash mismatch")
    if metadata_temporary is not None:
        _write_new_file(metadata_temporary, rendered_metadata)
    _fsync_directory(alias.parent)
    return _PreparedHdriCompatibility(
        alias_path=alias,
        metadata_path=metadata,
        alias_temporary=alias_temporary,
        metadata_temporary=metadata_temporary,
    )


def _commit_prepared_hdri_compatibility(
    prepared: _PreparedHdriCompatibility,
    *,
    project_root: Path,
    allowed_root: Path,
) -> tuple[Path, Path]:
    for temporary, destination in (
        (prepared.alias_temporary, prepared.alias_path),
        (prepared.metadata_temporary, prepared.metadata_path),
    ):
        _require_inside(destination, allowed_root, "HDRI compatibility destination")
        _reject_symlink_ancestors(destination, project_root=project_root)
        if temporary is None:
            continue
        _require_regular_file(temporary, "prepared HDRI compatibility file")
        temporary.replace(destination)
        _fsync_directory(destination.parent)
    return prepared.alias_path, prepared.metadata_path


def _discard_prepared_hdri_compatibility(
    prepared: _PreparedHdriCompatibility,
) -> None:
    for temporary in (prepared.alias_temporary, prepared.metadata_temporary):
        if temporary is not None and not temporary.is_symlink():
            temporary.unlink(missing_ok=True)


def _compatibility_intent_path(
    data_dir: Path,
    kind: ResourceKind,
    resource_id: str,
) -> Path:
    return (
        data_dir
        / "acquire/polyhaven/resources"
        / kind
        / "compatibility_intents"
        / f"{resource_id}.json"
    )


def _persist_compatibility_intent(
    path: Path,
    prepared: _PreparedHdriCompatibility,
    *,
    resource_id: str,
    kind: ResourceKind,
    catalog_path: Path,
    project_root: Path,
    status: Literal["prepared", "catalog_committed"],
) -> None:
    _reject_symlink_ancestors(path, project_root=project_root)
    payload = {
        "schema_version": 1,
        "source": POLYHAVEN_SOURCE,
        "asset_type": kind,
        "resource_id": resource_id,
        "status": status,
        "catalog_path": _portable_path(catalog_path, project_root),
        "alias_path": _portable_path(prepared.alias_path, project_root),
        "metadata_path": _portable_path(prepared.metadata_path, project_root),
        "alias_temporary": (
            None
            if prepared.alias_temporary is None
            else _portable_path(prepared.alias_temporary, project_root)
        ),
        "metadata_temporary": (
            None
            if prepared.metadata_temporary is None
            else _portable_path(prepared.metadata_temporary, project_root)
        ),
        "alias_temporary_sha256": (
            None if prepared.alias_temporary is None else _sha256_file(prepared.alias_temporary)
        ),
        "metadata_temporary_sha256": (
            None
            if prepared.metadata_temporary is None
            else _sha256_file(prepared.metadata_temporary)
        ),
        "updated_at": _utc_now(),
    }
    _write_json_atomic(path, payload)


def _validate_compatibility_intent(
    path: Path,
    *,
    resource_id: str,
    kind: ResourceKind,
    catalog_path: Path,
    project_root: Path,
    data_dir: Path,
    expected_alias: Path | None = None,
) -> None:
    _reject_symlink_ancestors(path, project_root=project_root)
    _require_regular_file(path, "compatibility commit intent")
    payload = _read_json(path, "compatibility commit intent")
    expected_keys = {
        "schema_version",
        "source",
        "asset_type",
        "resource_id",
        "status",
        "catalog_path",
        "alias_path",
        "metadata_path",
        "alias_temporary",
        "metadata_temporary",
        "alias_temporary_sha256",
        "metadata_temporary_sha256",
        "updated_at",
    }
    if set(payload) != expected_keys or any(
        (
            payload["schema_version"] != 1,
            payload["source"] != POLYHAVEN_SOURCE,
            payload["asset_type"] != kind,
            payload["resource_id"] != resource_id,
            payload["status"] not in {"prepared", "catalog_committed"},
            payload["catalog_path"] != _portable_path(catalog_path, project_root),
        )
    ):
        raise PolyHavenResourceSyncError("compatibility commit intent identity is invalid")
    alias = _path_from_portable(payload["alias_path"], project_root)
    metadata = _path_from_portable(payload["metadata_path"], project_root)
    _require_inside(alias, data_dir / "hdri", "compatibility intent alias")
    if metadata != alias.with_suffix(".json"):
        raise PolyHavenResourceSyncError("compatibility intent metadata path is not canonical")
    if expected_alias is not None and alias != expected_alias:
        raise PolyHavenResourceSyncError(
            "compatibility intent alias differs from catalog provenance"
        )
    for path_key, hash_key, destination in (
        ("alias_temporary", "alias_temporary_sha256", alias),
        ("metadata_temporary", "metadata_temporary_sha256", metadata),
    ):
        raw_path = payload[path_key]
        raw_hash = payload[hash_key]
        if raw_path is None:
            if raw_hash is not None:
                raise PolyHavenResourceSyncError(
                    "compatibility intent has a hash without a prepared file"
                )
            continue
        temporary = _path_from_portable(raw_path, project_root)
        expected_temporary = destination.with_name(f".{destination.name}.{resource_id}.prepared")
        if temporary != expected_temporary or not isinstance(raw_hash, str):
            raise PolyHavenResourceSyncError(
                "compatibility intent prepared path is not deterministic"
            )
        if temporary.exists() or temporary.is_symlink():
            _require_regular_file(temporary, "compatibility intent prepared file")
            if _sha256_file(temporary) != raw_hash:
                raise PolyHavenResourceSyncError("compatibility intent prepared file hash changed")


def _clear_compatibility_intent(path: Path) -> None:
    if path.is_symlink():
        raise PolyHavenPathSecurityError(f"compatibility intent must not be a symlink: {path}")
    if path.exists():
        _require_regular_file(path, "compatibility commit intent")
        path.unlink()
        _fsync_directory(path.parent)


def _reset_prepared_path(path: Path, *, project_root: Path) -> None:
    _reject_symlink_ancestors(path, project_root=project_root)
    if path.is_symlink():
        raise PolyHavenPathSecurityError(f"prepared path must not be a symlink: {path}")
    if path.exists():
        _require_regular_file(path, "stale prepared compatibility file")
        path.unlink()


def _copy_regular_file(source: Path, destination: Path) -> None:
    _require_regular_file(source, "compatibility copy source")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            descriptor = -1
            shutil.copyfileobj(reader, writer, length=_HASH_CHUNK_BYTES)
            writer.flush()
            os.fsync(writer.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_new_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())


def _hdri_compatibility_payload(
    *,
    listing: PolyHavenResourceListing,
    resolution: str,
    source_path: Path,
    file_spec: PolyHavenResourceFileSpec,
    resource_id: str,
    catalog_path: Path,
    data_dir: Path,
    project_root: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    alias = data_dir / "hdri" / f"{listing.source_id}_{resolution}.hdr"
    metadata = alias.with_suffix(".json")
    payload = {
        "asset_id": listing.source_id,
        "resolution": resolution,
        "file": str(alias),
        "source_url": file_spec.url,
        "asset_url": POLYHAVEN_ASSET_URL.format(source_id=quote(listing.source_id, safe="")),
        "license": "CC0",
        "bytes": file_spec.bytes,
        "md5": file_spec.md5,
        "schema_version": 2,
        "resource_id": resource_id,
        "source_revision": listing.revision,
        "source_revision_scheme": "sha1_files_hash",
        "sha256": _sha256_file(source_path),
        "canonical_file": _portable_path(source_path, project_root),
        "catalog": _portable_path(catalog_path, project_root),
    }
    return alias, metadata, payload


def _adopt_legacy_hdri(
    *,
    listing: PolyHavenResourceListing,
    resolution: str,
    spec: PolyHavenResourceFileSpec,
    destination: Path,
    data_dir: Path,
    project_root: Path,
    session: PolyHavenProviderSession,
) -> bool:
    legacy = data_dir / "hdri" / f"{listing.source_id}_{resolution}.hdr"
    _require_inside(legacy, data_dir, "legacy HDRI compatibility file")
    _reject_symlink_ancestors(legacy, project_root=project_root)
    if not legacy.exists() and not legacy.is_symlink():
        return False
    _require_regular_file(legacy, "legacy HDRI compatibility file")
    try:
        validate_radiance_hdr(legacy, expected_size=spec.bytes, provider_md5=spec.md5)
    except ResourceValidationError:
        return False
    _install_regular_alias(
        legacy,
        destination,
        project_root=project_root,
        allowed_root=data_dir,
        session=session,
    )
    return True


def _install_regular_alias(
    source: Path,
    destination: Path,
    *,
    project_root: Path,
    allowed_root: Path,
    session: PolyHavenProviderSession,
    disk_preflighted: bool = False,
) -> None:
    _require_regular_file(source, "resource alias source")
    _require_inside(source, allowed_root, "resource alias source")
    _require_inside(destination, allowed_root, "resource alias destination")
    _reject_symlink_ancestors(source, project_root=project_root)
    _reject_symlink_ancestors(destination, project_root=project_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise PolyHavenPathSecurityError(f"refusing to replace symlink alias: {destination}")
    if destination.exists() and not destination.is_file():
        raise PolyHavenPathSecurityError(
            f"resource alias destination is not a regular file: {destination}"
        )
    if destination.is_file() and _sha256_file(destination) == _sha256_file(source):
        source_stat = source.stat()
        destination_stat = destination.stat()
        if (source_stat.st_dev, source_stat.st_ino) != (
            destination_stat.st_dev,
            destination_stat.st_ino,
        ):
            return
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    if not disk_preflighted:
        session.check_disk_growth(source.stat().st_size)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            shutil.copyfileobj(reader, writer, length=_HASH_CHUNK_BYTES)
            writer.flush()
            os.fsync(writer.fileno())
        _require_regular_file(temporary, "resource alias temporary")
        if _sha256_file(temporary) != _sha256_file(source):
            raise PolyHavenResourceSyncError("resource alias copy hash mismatch")
        temporary.replace(destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _regular_copy_growth(
    source: Path,
    destination: Path,
    *,
    project_root: Path,
    allowed_root: Path,
) -> int:
    _require_regular_file(source, "resource alias source")
    _require_inside(source, allowed_root, "resource alias source")
    _require_inside(destination, allowed_root, "resource alias destination")
    _reject_symlink_ancestors(source, project_root=project_root)
    _reject_symlink_ancestors(destination, project_root=project_root)
    if destination.is_symlink():
        raise PolyHavenPathSecurityError(f"refusing to replace symlink alias: {destination}")
    if destination.exists() and not destination.is_file():
        raise PolyHavenPathSecurityError(
            f"resource alias destination is not a regular file: {destination}"
        )
    if destination.is_file() and _sha256_file(destination) == _sha256_file(source):
        source_stat = source.stat()
        destination_stat = destination.stat()
        if (source_stat.st_dev, source_stat.st_ino) != (
            destination_stat.st_dev,
            destination_stat.st_ino,
        ):
            return 0
    return source.stat().st_size


def _hdri_alias_repair_resource_ids(
    listings: tuple[PolyHavenResourceListing, ...],
    *,
    resolution: str,
    source_ids: tuple[str, ...],
    state: Mapping[str, Any],
    project_root: Path,
    data_dir: Path,
    catalog_path: Path,
) -> tuple[str, ...]:
    """Return current terminal HDRIs whose mutable compatibility view drifted."""

    requested = set(source_ids)
    repair: list[str] = []
    state_items = cast(Mapping[str, Any], state["items"])
    for listing in listings:
        if listing.kind != "hdri" or (requested and listing.source_id not in requested):
            continue
        resource_id = listing.resource_id(resolution)
        receipt = state_items.get(resource_id)
        if not isinstance(receipt, dict):
            continue
        provider_files = receipt.get("provider_files")
        compatibility = receipt.get("compatibility")
        if (
            not isinstance(provider_files, list)
            or len(provider_files) != 1
            or not isinstance(provider_files[0], dict)
            or not isinstance(compatibility, dict)
        ):
            repair.append(resource_id)
            continue
        provider_file = provider_files[0]
        source_path = _path_from_portable(provider_file.get("path"), project_root)
        alias, metadata, payload = _hdri_compatibility_payload(
            listing=listing,
            resolution=resolution,
            source_path=source_path,
            file_spec=PolyHavenResourceFileSpec(
                provider_role="hdri",
                relative_path=Path(source_path.name),
                url=cast(str, provider_file.get("url")),
                bytes=cast(int, provider_file.get("bytes")),
                md5=cast(str, provider_file.get("md5")),
            ),
            resource_id=resource_id,
            catalog_path=catalog_path,
            data_dir=data_dir,
            project_root=project_root,
        )
        recorded_alias = compatibility.get("path")
        recorded_metadata = compatibility.get("metadata_path")
        if (
            recorded_alias != _portable_path(alias, project_root)
            or recorded_metadata != _portable_path(metadata, project_root)
            or alias.is_symlink()
            or metadata.is_symlink()
            or not alias.is_file()
            or not metadata.is_file()
            or not source_path.is_file()
            or _sha256_file(alias) != _sha256_file(source_path)
            or alias.stat().st_ino == source_path.stat().st_ino
            and alias.stat().st_dev == source_path.stat().st_dev
            or metadata.read_bytes() != _render_json(payload)
        ):
            repair.append(resource_id)
    return tuple(repair)


def _select_listings(
    listings: tuple[PolyHavenResourceListing, ...],
    *,
    source_ids: tuple[str, ...],
    limit: int,
    resolution: str,
    state: dict[str, Any],
    active_failures: Mapping[str, ActiveFailure],
    now: datetime,
    priority_resource_ids: tuple[str, ...] = (),
) -> tuple[tuple[PolyHavenResourceListing, ...], tuple[PolyHavenResourceSyncItem, ...]]:
    by_source = {item.source_id: item for item in listings}
    priority = set(priority_resource_ids)
    priority_listings = tuple(item for item in listings if item.resource_id(resolution) in priority)
    if source_ids:
        missing = [source_id for source_id in source_ids if source_id not in by_source]
        if missing:
            raise PolyHavenResourceSyncError(
                "requested source ids are absent from the live listing: " + ", ".join(missing)
            )
        requested = tuple(by_source[source_id] for source_id in source_ids)
        ordered = priority_listings + tuple(
            item for item in requested if item.resource_id(resolution) not in priority
        )
    else:
        ordered = priority_listings + tuple(
            item for item in listings if item.resource_id(resolution) not in priority
        )
    selected: list[PolyHavenResourceListing] = []
    deferred: list[PolyHavenResourceSyncItem] = []
    for listing in ordered:
        resource_id = listing.resource_id(resolution)
        if not source_ids and resource_id not in priority and resource_id in state["items"]:
            continue
        failure = active_failures.get(resource_id)
        if failure is not None and not failure.eligible(now=now):
            if source_ids:
                deferred.append(
                    PolyHavenResourceSyncItem(
                        resource_id=resource_id,
                        kind=listing.kind,
                        source_id=listing.source_id,
                        revision=listing.revision,
                        resolution=resolution,
                        status="deferred",
                        error={
                            "failure_event_id": failure.event_id,
                            "disposition": failure.disposition.value,
                            "next_eligible_at": (
                                None
                                if failure.next_eligible_at is None
                                else failure.next_eligible_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                            ),
                        },
                        failure_event_id=failure.event_id,
                    )
                )
            continue
        selected.append(listing)
        if len(selected) >= limit:
            break
    return tuple(selected), tuple(deferred)


def _validate_retry_selection(
    listings: tuple[PolyHavenResourceListing, ...],
    *,
    retry_revisions: tuple[str, ...],
    source_ids: tuple[str, ...],
    resolution: str,
    limit: int,
) -> None:
    if not retry_revisions:
        return
    if len(retry_revisions) > limit:
        raise PolyHavenResourceSyncError("limit is smaller than the exact retry revision cohort")
    by_resource = {item.resource_id(resolution): item for item in listings}
    missing = [resource_id for resource_id in retry_revisions if resource_id not in by_resource]
    if missing:
        raise PolyHavenResourceSyncError(
            "retry revisions are absent from the current listing: " + ", ".join(missing)
        )
    if source_ids:
        requested = set(source_ids)
        omitted = [
            resource_id
            for resource_id in retry_revisions
            if by_resource[resource_id].source_id not in requested
        ]
        if omitted:
            raise PolyHavenResourceSyncError(
                "retry revisions must be included by --source-id: " + ", ".join(omitted)
            )


def _release_failures(
    journal: dict[str, Any],
    *,
    retry_revisions: tuple[str, ...],
    kind: ResourceKind,
    run_id: str,
    clock: Clock,
    journal_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    active = validate_failure_journal(journal, source=POLYHAVEN_SOURCE, asset_type=kind)
    refs: list[dict[str, Any]] = []
    for ordinal, resource_id in enumerate(retry_revisions, start=1):
        failure = active.get(resource_id)
        if failure is None:
            raise PolyHavenResourceSyncError(
                f"retry revision {resource_id!r} has no active failure"
            )
        journal, event = append_release_event(
            journal,
            source=POLYHAVEN_SOURCE,
            asset_type=kind,
            asset_id=failure.asset_id,
            source_id=failure.source_id,
            revision=failure.revision,
            resolution=failure.resolution,
            run_id=run_id,
            attempt_id=f"{run_id}:release:{ordinal}",
            recorded_at=_next_journal_datetime(journal, _whole_second(clock.utc_now())),
            reason="operator_requested_exact_resource_revision_retry",
        )
        refs.append(_event_ref(event))
        active = validate_failure_journal(journal, source=POLYHAVEN_SOURCE, asset_type=kind)
    if refs:
        _write_json_atomic(journal_path, journal)
    return journal, refs


def _resolve_recovered_ready_failures(
    journal: dict[str, Any],
    *,
    recovered_resource_ids: set[str],
    state: Mapping[str, Any],
    kind: ResourceKind,
    run_id: str,
    clock: Clock,
    journal_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not recovered_resource_ids:
        return journal, []
    active = validate_failure_journal(journal, source=POLYHAVEN_SOURCE, asset_type=kind)
    refs: list[dict[str, Any]] = []
    for ordinal, resource_id in enumerate(sorted(recovered_resource_ids), start=1):
        failure = active.get(resource_id)
        if failure is None:
            continue
        receipt = state["items"][resource_id]
        try:
            ready_at = datetime.strptime(receipt["ready_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PolyHavenResourceSyncError(
                "recovered resource ready timestamp is invalid"
            ) from exc
        if ready_at <= failure.recorded_at:
            continue
        journal, event = append_resolution_event(
            journal,
            source=POLYHAVEN_SOURCE,
            asset_type=kind,
            asset_id=failure.asset_id,
            source_id=failure.source_id,
            revision=failure.revision,
            resolution=failure.resolution,
            run_id=run_id,
            attempt_id=f"{run_id}:catalog-recovery:{ordinal}",
            recorded_at=_next_journal_datetime(journal, _whole_second(clock.utc_now())),
        )
        if event is not None:
            refs.append(_event_ref(event))
        active = validate_failure_journal(journal, source=POLYHAVEN_SOURCE, asset_type=kind)
    if refs:
        _write_json_atomic(journal_path, journal)
    return journal, refs


def _resolve_success_failure(
    journal: dict[str, Any],
    *,
    kind: ResourceKind,
    resource_id: str,
    source_id: str,
    revision: str,
    resolution: str,
    run_id: str,
    attempt_id: str,
    clock: Clock,
    journal_path: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    active = validate_failure_journal(journal, source=POLYHAVEN_SOURCE, asset_type=kind).get(
        resource_id
    )
    if active is None:
        return journal, None
    updated, event = append_resolution_event(
        journal,
        source=POLYHAVEN_SOURCE,
        asset_type=kind,
        asset_id=resource_id,
        source_id=source_id,
        revision=revision,
        resolution=resolution,
        run_id=run_id,
        attempt_id=attempt_id,
        recorded_at=_next_journal_datetime(journal, _whole_second(clock.utc_now())),
    )
    if event is not None:
        _write_json_atomic(journal_path, updated)
        return updated, _event_ref(event)
    return updated, None


def _journal_adapter_failure(
    journal: dict[str, Any],
    *,
    journal_path: Path,
    kind: ResourceKind,
    listing: PolyHavenResourceListing,
    resolution: str,
    run_id: str,
    attempt_id: str,
    clock: Clock,
    policy: Any,
    failure: AcquisitionFailure,
) -> tuple[dict[str, Any], dict[str, Any]]:
    updated, event = append_failure_event(
        journal,
        source=POLYHAVEN_SOURCE,
        asset_type=kind,
        asset_id=listing.resource_id(resolution),
        source_id=listing.source_id,
        revision=listing.revision,
        resolution=resolution,
        run_id=run_id,
        attempt_id=attempt_id,
        failure=failure,
        recorded_at=_next_journal_datetime(journal, _whole_second(clock.utc_now())),
        policy=policy,
        attempts_in_run=1,
    )
    _write_json_atomic(journal_path, updated)
    return updated, event


def _failed_item(
    listing: PolyHavenResourceListing,
    resolution: str,
    event: Mapping[str, Any],
) -> PolyHavenResourceSyncItem:
    return PolyHavenResourceSyncItem(
        resource_id=listing.resource_id(resolution),
        kind=listing.kind,
        source_id=listing.source_id,
        revision=listing.revision,
        resolution=resolution,
        status="failed",
        error={
            "failure": event["failure"],
            "disposition": event["disposition"],
            "next_eligible_at": event["next_eligible_at"],
        },
        failure_event_id=cast(str, event["event_id"]),
    )


def _quota_deferred_item(
    listing: PolyHavenResourceListing,
    resolution: str,
) -> PolyHavenResourceSyncItem:
    return PolyHavenResourceSyncItem(
        resource_id=listing.resource_id(resolution),
        kind=listing.kind,
        source_id=listing.source_id,
        revision=listing.revision,
        resolution=resolution,
        status="deferred",
        error={
            "failure": {
                "kind": FailureKind.QUOTA.value,
                "category": "deferred",
                "phase": "item_quota",
                "message": "daily new-item quota deferred this resource revision",
            }
        },
    )


def _classified_failure(
    exc: ProviderOperationError | ResourceValidationError,
) -> tuple[AcquisitionFailure, int, datetime | None]:
    if isinstance(exc, ProviderOperationError):
        return exc.failure, exc.attempts_in_run, exc.retry_after_deadline
    return (
        AcquisitionFailure(
            kind=FailureKind.QUALITY,
            phase="resource_validation",
            message=str(exc),
        ),
        1,
        None,
    )


def _load_or_create_state(path: Path, kind: ResourceKind) -> dict[str, Any]:
    if path.exists() or path.is_symlink():
        _require_regular_file(path, "resource state")
        state = _read_json(path, "resource state")
        return _validate_state(state, kind)
    state = {
        "schema_version": RESOURCE_STATE_SCHEMA_VERSION,
        "source": POLYHAVEN_SOURCE,
        "asset_type": kind,
        "updated_at": None,
        "last_listing": None,
        "items": {},
        "run_receipts": {},
    }
    _write_json_atomic(path, state)
    return state


def _validate_state(value: Any, kind: ResourceKind) -> dict[str, Any]:
    expected = {
        "schema_version",
        "source",
        "asset_type",
        "updated_at",
        "last_listing",
        "items",
        "run_receipts",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise PolyHavenResourceSyncError("resource state has an unsupported shape")
    if (
        value["schema_version"] != RESOURCE_STATE_SCHEMA_VERSION
        or value["source"] != POLYHAVEN_SOURCE
        or value["asset_type"] != kind
    ):
        raise PolyHavenResourceSyncError("resource state identity is invalid")
    if not isinstance(value["items"], dict) or not isinstance(value["run_receipts"], dict):
        raise PolyHavenResourceSyncError("resource state indexes must be objects")
    for resource_id, item in value["items"].items():
        if not isinstance(resource_id, str):
            raise PolyHavenResourceSyncError("resource state item key is invalid")
        _validate_state_item(item, resource_id=resource_id, kind=kind)
    return value


def _validate_state_item(value: Any, *, resource_id: str, kind: ResourceKind) -> dict[str, Any]:
    required = {
        "schema_version",
        "resource_id",
        "kind",
        "profile",
        "source_id",
        "revision",
        "resolution",
        "root_dir",
        "bundle_sha256",
        "content_sha256",
        "provider_files",
        "artifacts",
        "catalog",
        "compatibility",
        "ready_at",
    }
    allowed_shapes = (required, required | {"listing_evidence"})
    if not isinstance(value, dict) or set(value) not in allowed_shapes:
        raise PolyHavenResourceSyncError("resource state item has an unsupported shape")
    if (
        value["schema_version"] != RESOURCE_STATE_SCHEMA_VERSION
        or value["resource_id"] != resource_id
        or value["kind"] != kind
        or _RESOURCE_ID_PATTERN.fullmatch(resource_id) is None
    ):
        raise PolyHavenResourceSyncError("resource state item identity is invalid")
    if not isinstance(value["provider_files"], list) or not value["provider_files"]:
        raise PolyHavenResourceSyncError("resource state item has no provider files")
    if not isinstance(value["artifacts"], list) or not value["artifacts"]:
        raise PolyHavenResourceSyncError("resource state item has no artifacts")
    if any(
        not isinstance(value[field], str)
        for field in (
            "source_id",
            "revision",
            "resolution",
            "root_dir",
            "bundle_sha256",
            "content_sha256",
            "ready_at",
        )
    ):
        raise PolyHavenResourceSyncError("resource state item fields are invalid")
    if (
        _SHA256_PATTERN.fullmatch(value["bundle_sha256"]) is None
        or _SHA256_PATTERN.fullmatch(value["content_sha256"]) is None
    ):
        raise PolyHavenResourceSyncError("resource state item hashes are invalid")
    catalog = value["catalog"]
    compatibility = value["compatibility"]
    if not isinstance(catalog, dict) or set(catalog) != {
        "database",
        "projection_sha256",
        "projection",
    }:
        raise PolyHavenResourceSyncError("resource state catalog receipt is invalid")
    if (
        not isinstance(catalog["projection_sha256"], str)
        or _SHA256_PATTERN.fullmatch(catalog["projection_sha256"]) is None
    ):
        raise PolyHavenResourceSyncError("resource state catalog hash is invalid")
    if not isinstance(compatibility, dict) or set(compatibility) != {
        "path",
        "metadata_path",
    }:
        raise PolyHavenResourceSyncError("resource state compatibility receipt is invalid")
    if "listing_evidence" in value:
        evidence = value["listing_evidence"]
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"mode", "projection", "sha256"}
            or evidence["mode"] not in {"live", "catalog_recovery"}
            or not isinstance(evidence["projection"], dict)
            or not isinstance(evidence["sha256"], str)
            or evidence["sha256"]
            != _domain_sha256(
                _LISTING_EVIDENCE_DOMAIN,
                {
                    "mode": evidence["mode"],
                    "projection": evidence["projection"],
                },
            )
        ):
            raise PolyHavenResourceSyncError("resource state listing evidence is invalid")
    return value


def _load_or_create_journal(path: Path, kind: ResourceKind) -> dict[str, Any]:
    if path.exists() or path.is_symlink():
        _require_regular_file(path, "resource failure journal")
        journal = _read_json(path, "resource failure journal")
    else:
        journal = empty_failure_journal(source=POLYHAVEN_SOURCE, asset_type=kind)
        _write_json_atomic(path, journal)
    validate_failure_journal(journal, source=POLYHAVEN_SOURCE, asset_type=kind)
    return journal


def _reconcile_compatibility_intents(
    root: Path,
    *,
    kind: ResourceKind,
    state: dict[str, Any],
    project_root: Path,
    data_dir: Path,
    catalog_path: Path,
    session: PolyHavenProviderSession,
) -> bool:
    if not root.exists() and not root.is_symlink():
        return False
    _reject_symlink_ancestors(root, project_root=project_root)
    if root.is_symlink() or not root.is_dir():
        raise PolyHavenPathSecurityError(
            f"compatibility intent root must be a regular directory: {root}"
        )
    changed = False
    for intent_path in sorted(root.iterdir()):
        if intent_path.is_symlink() or not intent_path.is_file():
            raise PolyHavenPathSecurityError(
                f"compatibility intent must be a regular file: {intent_path}"
            )
        payload = _read_json(intent_path, "compatibility commit intent")
        resource_id = payload.get("resource_id")
        if not isinstance(resource_id, str) or intent_path.name != f"{resource_id}.json":
            raise PolyHavenResourceSyncError(
                "compatibility intent filename differs from its resource id"
            )
        _validate_compatibility_intent(
            intent_path,
            resource_id=resource_id,
            kind=kind,
            catalog_path=catalog_path,
            project_root=project_root,
            data_dir=data_dir,
        )
        if not catalog_path.exists():
            _discard_intent_temporaries(payload, project_root=project_root, data_dir=data_dir)
            _clear_compatibility_intent(intent_path)
            continue
        catalog = Catalog(catalog_path, project_root=project_root)
        record = catalog.get_resource(resource_id)
        if record is None:
            _discard_intent_temporaries(payload, project_root=project_root, data_dir=data_dir)
            _clear_compatibility_intent(intent_path)
            continue
        if record.status != "ready" or record.resource_kind != "hdri":
            raise PolyHavenResourceSyncError(
                "compatibility intent catalog resource is not a ready HDRI"
            )
        _validate_compatibility_intent(
            intent_path,
            resource_id=resource_id,
            kind=kind,
            catalog_path=catalog_path,
            project_root=project_root,
            data_dir=data_dir,
            expected_alias=(data_dir / "hdri" / f"{record.source_id}_{record.resolution}.hdr"),
        )
        listing = _catalog_recovery_listing(record)
        if listing.resource_id(record.resolution) != resource_id:
            raise PolyHavenResourceSyncError(
                "compatibility intent resource id differs from catalog provenance"
            )
        newer_state_exists = any(
            isinstance(item, dict)
            and candidate_id != resource_id
            and item.get("source_id") == record.source_id
            and item.get("resolution") == record.resolution
            for candidate_id, item in state["items"].items()
        )
        if newer_state_exists:
            _discard_intent_temporaries(payload, project_root=project_root, data_dir=data_dir)
            _clear_compatibility_intent(intent_path)
        _item, receipt = _recover_catalog_terminal_item(
            listing=listing,
            resolution=record.resolution,
            project_root=project_root,
            data_dir=data_dir,
            catalog_path=catalog_path,
            session=session,
            listing_evidence_mode="catalog_recovery",
            publish_compatibility=not newer_state_exists,
        )
        existing = state["items"].get(resource_id)
        if existing is not None:
            comparable_existing = dict(cast(dict[str, Any], existing))
            comparable_receipt = dict(receipt)
            comparable_existing.pop("listing_evidence", None)
            comparable_receipt.pop("listing_evidence", None)
            if comparable_existing != comparable_receipt:
                raise PolyHavenResourceSyncError(
                    "compatibility intent recovery conflicts with resource state"
                )
        if existing is None:
            state["items"][resource_id] = receipt
            changed = True
    return changed


def _reconcile_force_candidates(root: Path, *, project_root: Path) -> None:
    if not root.exists() and not root.is_symlink():
        return
    _reject_symlink_ancestors(root, project_root=project_root)
    if root.is_symlink() or not root.is_dir():
        raise PolyHavenPathSecurityError(
            f"force verification root must be a regular directory: {root}"
        )
    for child in sorted(root.iterdir()):
        if (
            child.is_symlink()
            or not child.is_dir()
            or _RESOURCE_ID_PATTERN.fullmatch(child.name) is None
        ):
            raise PolyHavenPathSecurityError(f"unsafe force verification candidate: {child}")
        for directory, names, filenames in os.walk(child, followlinks=False):
            base = Path(directory)
            if any((base / name).is_symlink() for name in names):
                raise PolyHavenPathSecurityError(
                    f"force verification candidate contains a symlink: {child}"
                )
            for filename in filenames:
                _require_regular_file(base / filename, "force verification candidate file")
        shutil.rmtree(child)
    _fsync_directory(root)


def _catalog_recovery_listing(record: Any) -> PolyHavenResourceListing:
    base_tags = {"polyhaven", "hdri" if record.resource_kind == "hdri" else "pbr"}
    return PolyHavenResourceListing(
        kind=cast(ResourceKind, record.resource_kind),
        source_id=record.source_id,
        name=record.name,
        date_published=1,
        revision=record.source_revision,
        authors=(((record.attribution, "catalog_recovery"),) if record.attribution else ()),
        categories=(),
        tags=tuple(tag for tag in record.tags if tag not in base_tags),
        physical_size_mm=record.physical_size_mm,
    )


def _reconcile_catalog_ready_resources(
    *,
    kind: ResourceKind,
    state: dict[str, Any],
    project_root: Path,
    data_dir: Path,
    catalog_path: Path,
    session: PolyHavenProviderSession,
) -> bool:
    if not catalog_path.exists():
        return False
    catalog = Catalog(catalog_path, project_root=project_root)
    changed = False
    for record in catalog.list_resources(
        resource_kind=kind,
        status="ready",
        source=POLYHAVEN_SOURCE,
        limit=10_000,
    ):
        if record.resource_id in state["items"]:
            continue
        listing = _catalog_recovery_listing(record)
        if listing.resource_id(record.resolution) != record.resource_id:
            raise PolyHavenResourceSyncError(
                "ready catalog resource id differs from its provider provenance"
            )
        _item, receipt = _recover_catalog_terminal_item(
            listing=listing,
            resolution=record.resolution,
            project_root=project_root,
            data_dir=data_dir,
            catalog_path=catalog_path,
            session=session,
            listing_evidence_mode="catalog_recovery",
            publish_compatibility=False,
        )
        state["items"][record.resource_id] = receipt
        changed = True
    return changed


def _discard_intent_temporaries(
    payload: Mapping[str, Any],
    *,
    project_root: Path,
    data_dir: Path,
) -> None:
    for key in ("alias_temporary", "metadata_temporary"):
        value = payload.get(key)
        if value is None:
            continue
        path = _path_from_portable(value, project_root)
        _require_inside(path, data_dir / "hdri", "compatibility prepared file")
        if path.is_symlink():
            raise PolyHavenPathSecurityError(
                f"compatibility prepared file must not be a symlink: {path}"
            )
        if path.exists():
            _require_regular_file(path, "compatibility prepared file")
            path.unlink()
            _fsync_directory(path.parent)


def _reconcile_run_manifests(
    root: Path,
    clock: Clock,
    *,
    project_root: Path,
    kind: ResourceKind,
    state: dict[str, Any],
    journal: Mapping[str, Any],
) -> bool:
    state_changed = False
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            raise PolyHavenPathSecurityError(
                f"resource run directory must not be a symlink: {child}"
            )
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.exists() and not manifest_path.is_symlink():
            continue
        _reject_symlink_ancestors(manifest_path, project_root=project_root)
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise PolyHavenPathSecurityError(f"unsafe resource run manifest: {manifest_path}")
        payload = _read_json(manifest_path, "resource run manifest")
        if (
            payload.get("schema_version") != RESOURCE_RUN_SCHEMA_VERSION
            or payload.get("source") != POLYHAVEN_SOURCE
            or payload.get("asset_type") != kind
            or payload.get("run_id") != child.name
        ):
            raise PolyHavenResourceSyncError(
                f"resource run manifest identity is invalid: {manifest_path}"
            )
        raw_refs = payload.get("journal_event_refs")
        if not isinstance(raw_refs, list) or any(not isinstance(item, dict) for item in raw_refs):
            raise PolyHavenResourceSyncError(
                f"resource run journal refs are invalid: {manifest_path}"
            )
        expected_refs = [
            _event_ref(event)
            for event in cast(list[dict[str, Any]], journal["events"])
            if event.get("run_id") == child.name
        ]
        if any(item not in expected_refs for item in raw_refs):
            raise PolyHavenResourceSyncError(
                f"resource run references an absent journal event: {manifest_path}"
            )
        merged_refs = sorted(expected_refs, key=lambda item: cast(int, item["sequence"]))
        if raw_refs != merged_refs:
            payload["journal_event_refs"] = merged_refs
            _write_json_atomic(manifest_path, payload)
        if payload.get("status") == "running":
            payload["completed_at"] = _timestamp(clock)
            payload["status"] = "interrupted"
            payload["error"] = {
                "type": "InterruptedRunRecovered",
                "message": "stale running manifest recovered under the source lock",
            }
            if not isinstance(payload.get("runtime"), dict):
                payload["runtime"] = {
                    "recovery": {"status": "evidence_unavailable_after_process_crash"}
                }
            _write_json_atomic(manifest_path, payload)
        _validate_reconciled_manifest(payload, kind=kind, state=state)
        status = payload.get("status")
        completed_at = payload.get("completed_at")
        if status != "running" and isinstance(completed_at, str):
            receipt = {
                "status": status,
                "manifest_path": _portable_path(manifest_path, project_root),
                "manifest_sha256": _sha256_file(manifest_path),
                "completed_at": completed_at,
            }
            if state["run_receipts"].get(child.name) != receipt:
                if child.name in state["run_receipts"]:
                    raise PolyHavenResourceSyncError(
                        f"resource run receipt conflicts with manifest: {child.name}"
                    )
                state["run_receipts"][child.name] = receipt
                state_changed = True
    return state_changed


def _validate_reconciled_manifest(
    payload: Mapping[str, Any],
    *,
    kind: ResourceKind,
    state: Mapping[str, Any],
) -> None:
    status = payload.get("status")
    if status not in {"ready", "partial", "failed", "deferred", "noop", "interrupted"}:
        raise PolyHavenResourceSyncError("resource run manifest status is invalid")
    if not isinstance(payload.get("completed_at"), str):
        raise PolyHavenResourceSyncError("resource run manifest has no completion timestamp")
    request = payload.get("request")
    if not isinstance(request, dict) or request.get("kind") != kind:
        raise PolyHavenResourceSyncError("resource run request identity is invalid")
    resolution = request.get("resolution")
    if not isinstance(resolution, str) or _RESOLUTION_PATTERN.fullmatch(resolution) is None:
        raise PolyHavenResourceSyncError("resource run resolution is invalid")
    items = payload.get("items")
    if not isinstance(items, list):
        raise PolyHavenResourceSyncError("resource run items must be an array")
    seen: set[str] = set()
    require_terminal_state = status in {"ready", "partial", "deferred", "noop"} or (
        status == "failed" and payload.get("error") is None
    )
    for item in items:
        if not isinstance(item, dict):
            raise PolyHavenResourceSyncError("resource run item is invalid")
        resource_id = item.get("resource_id")
        item_status = item.get("status")
        if (
            not isinstance(resource_id, str)
            or _RESOURCE_ID_PATTERN.fullmatch(resource_id) is None
            or resource_id in seen
            or item.get("kind") != kind
            or item.get("resolution") != resolution
            or item_status not in {"ready", "skipped", "failed", "deferred"}
        ):
            raise PolyHavenResourceSyncError("resource run item identity is invalid")
        seen.add(resource_id)
        if (
            require_terminal_state
            and item_status in {"ready", "skipped"}
            and resource_id not in state["items"]
        ):
            raise PolyHavenResourceSyncError(
                "terminal run item has no matching resource state receipt"
            )
    if status in {"ready", "partial", "deferred", "noop"}:
        expected = _manifest_item_status_from_values(items)
        if status != expected or payload.get("active_attempt") is not None:
            raise PolyHavenResourceSyncError("resource run status does not match its item cohort")
        if not isinstance(payload.get("failure_journal"), dict):
            raise PolyHavenResourceSyncError(
                "finalized resource run has no failure journal receipt"
            )
    if not isinstance(payload.get("runtime"), dict):
        raise PolyHavenResourceSyncError("resource run has no runtime receipt")


def _manifest_item_status_from_values(items: list[Any]) -> ResourceRunStatus:
    statuses = [item["status"] for item in items]
    successes = sum(status in {"ready", "skipped"} for status in statuses)
    failures = statuses.count("failed")
    deferred = statuses.count("deferred")
    if successes and not failures and not deferred:
        return "ready"
    if successes:
        return "partial"
    if failures:
        return "failed"
    if deferred:
        return "deferred"
    return "noop"


def _persist_run_error(
    path: Path,
    error: BaseException,
    *,
    clock: Clock,
    runtime: Mapping[str, Any],
) -> None:
    try:
        payload = _read_json(path, "resource run manifest")
        if payload.get("status") != "running":
            return
        commit_pending = isinstance(error, PolyHavenResourceCommitPendingError)
        payload["status"] = (
            "interrupted" if commit_pending or not isinstance(error, Exception) else "failed"
        )
        payload["completed_at"] = _timestamp(clock)
        payload["active_attempt"] = (
            payload.get("active_attempt")
            if commit_pending or not isinstance(error, Exception)
            else None
        )
        payload["runtime"] = dict(runtime)
        payload["error"] = {"type": type(error).__name__, "message": str(error)}
        _write_json_atomic(path, payload)
    except BaseException:
        return


def _run_status(items: tuple[PolyHavenResourceSyncItem, ...]) -> ResourceRunStatus:
    if not items:
        return "noop"
    successes = sum(item.status in {"ready", "skipped"} for item in items)
    failures = sum(item.status == "failed" for item in items)
    deferred = sum(item.status == "deferred" for item in items)
    if successes and not failures and not deferred:
        return "ready"
    if successes:
        return "partial"
    if failures:
        return "failed"
    return "deferred"


def _listing_sha256(listings: tuple[PolyHavenResourceListing, ...]) -> str:
    payload = [
        {
            "kind": item.kind,
            "source_id": item.source_id,
            "name": item.name,
            "date_published": item.date_published,
            "revision": item.revision,
            "authors": [list(author) for author in item.authors],
            "categories": list(item.categories),
            "tags": list(item.tags),
            "physical_size_mm": (
                None if item.physical_size_mm is None else list(item.physical_size_mm)
            ),
        }
        for item in listings
    ]
    return _domain_sha256(_LISTING_DIGEST_DOMAIN, payload)


def _listing_evidence(
    listing: PolyHavenResourceListing,
    *,
    mode: Literal["live", "catalog_recovery"],
) -> dict[str, Any]:
    return _listing_evidence_from_projection(
        _listing_projection(listing),
        mode=mode,
    )


def _listing_projection(listing: PolyHavenResourceListing) -> dict[str, Any]:
    return {
        "kind": listing.kind,
        "profile": listing.profile,
        "source_id": listing.source_id,
        "name": listing.name,
        "date_published": listing.date_published,
        "revision": listing.revision,
        "authors": [list(item) for item in listing.authors],
        "categories": list(listing.categories),
        "tags": list(listing.tags),
        "physical_size_mm": (
            None if listing.physical_size_mm is None else list(listing.physical_size_mm)
        ),
    }


def _listing_evidence_from_projection(
    projection: Mapping[str, Any],
    *,
    mode: Literal["live", "catalog_recovery"],
) -> dict[str, Any]:
    normalized = dict(projection)
    return {
        "mode": mode,
        "projection": normalized,
        "sha256": _domain_sha256(
            _LISTING_EVIDENCE_DOMAIN,
            {"mode": mode, "projection": normalized},
        ),
    }


def _catalog_artifact_paths(value: Mapping[str, Any], project_root: Path) -> tuple[Path, ...]:
    return tuple(_path_from_portable(item["path"], project_root) for item in value["artifacts"])


def _journal_receipt(journal: Mapping[str, Any], path: Path, project_root: Path) -> dict[str, Any]:
    return {
        "path": _portable_path(path, project_root),
        "events": len(cast(list[Any], journal["events"])),
        "head_event_sha256": journal["head_event_sha256"],
        "file_sha256": _sha256_file(path),
    }


def _event_ref(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "event_sha256": event["event_sha256"],
        "sequence": event["sequence"],
        "type": event["type"],
        "asset_id": event["asset_id"],
    }


def _next_journal_datetime(payload: Mapping[str, Any], now: datetime) -> datetime:
    updated_at = payload.get("updated_at")
    if updated_at is None:
        return now
    try:
        previous = datetime.strptime(cast(str, updated_at), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except (TypeError, ValueError) as exc:
        raise FailureJournalError("failure journal updated_at is invalid") from exc
    return max(previous, now)


def _child_id(resource_id: str, category: str, role: str) -> str:
    identity = json.dumps(
        [resource_id, category, role], separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    digest = hashlib.sha256(_ID_DIGEST_DOMAIN + identity).hexdigest()[:24]
    prefix = "phrf" if category == "file" else "phra"
    readable = re.sub(r"[^a-z0-9]+", "_", role.casefold()).strip("_")[:24]
    return f"{prefix}_{readable}_{digest}"


def _quota_item_id(resource_id: str) -> str:
    digest = hashlib.sha256(resource_id.encode("ascii")).hexdigest()[:32]
    return f"resource_{digest}"


def _file_evidence_payload(value: Any, project_root: Path) -> dict[str, Any]:
    return {
        "path": _portable_path(value.path, project_root),
        "bytes": value.bytes,
        "provider_md5": value.provider_md5,
        "md5": value.md5,
        "sha256": value.sha256,
    }


def _domain_sha256(domain: bytes, payload: Any) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain + rendered).hexdigest()


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> str:
    rendered = _render_json(payload)
    expected = hashlib.sha256(rendered).hexdigest()
    if path.exists() or path.is_symlink():
        _require_regular_file(path, "immutable resource evidence")
        if _sha256_file(path) != expected or path.read_bytes() != rendered:
            raise PolyHavenResourceSyncError(
                f"immutable resource evidence conflicts with existing file: {path}"
            )
        return expected
    _write_bytes_atomic(path, rendered)
    return expected


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    _write_bytes_atomic(path, _render_json(payload))


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PolyHavenPathSecurityError(f"refusing to replace symlink: {path}")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.part")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _render_json(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _read_json(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PolyHavenResourceSyncError(f"cannot read {context}: {path}") from exc
    if not isinstance(value, dict):
        raise PolyHavenResourceSyncError(f"{context} must be a JSON object")
    return value


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolyHavenResourceSyncError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _sha256_file(path: Path) -> str:
    _require_regular_file(path, "hash input")
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(path: Path, context: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise PolyHavenPathSecurityError(f"{context} must be a regular file: {path}")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _portable_path(path: Path, project_root: Path | None) -> str:
    resolved = path.resolve()
    if project_root is None:
        return str(resolved)
    try:
        return resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError as exc:
        raise PolyHavenResourceSyncError(
            f"resource evidence path escapes project root: {path}"
        ) from exc


def _path_from_portable(value: Any, project_root: Path) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PolyHavenResourceSyncError("portable resource path is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PolyHavenResourceSyncError("portable resource path is not normalized")
    candidate = project_root.joinpath(*pure.parts)
    _reject_symlink_ancestors(candidate, project_root=project_root)
    resolved = candidate.resolve(strict=False)
    _require_inside(resolved, project_root, "portable resource path")
    return resolved


def _reject_symlink_ancestors(path: Path, *, project_root: Path) -> None:
    unresolved = path if path.is_absolute() else project_root / path
    try:
        relative = unresolved.relative_to(project_root)
    except ValueError as exc:
        raise PolyHavenPathSecurityError(f"path escapes project root: {path}") from exc
    current = project_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PolyHavenPathSecurityError(f"path traverses symlink: {current}")


def _resolve_project_path(path: Path, project_root: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    _reject_symlink_ancestors(candidate, project_root=project_root)
    return candidate.resolve(strict=False)


def _require_inside(path: Path, root: Path, context: str) -> None:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise PolyHavenResourceSyncError(f"{context} must be inside {root}") from exc


def _checked_kind(value: Any) -> ResourceKind:
    if value not in POLYHAVEN_RESOURCE_LISTING_URLS:
        raise PolyHavenResourceSyncError("kind must be hdri or pbr_texture_set")
    return cast(ResourceKind, value)


def _checked_resolution(value: Any) -> str:
    if not isinstance(value, str) or _RESOLUTION_PATTERN.fullmatch(value) is None:
        raise PolyHavenResourceSyncError("resolution must look like '1k' or '2k'")
    return value


def _whole_second(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PolyHavenResourceSyncError("runtime clock must return timezone-aware UTC")
    return value.astimezone(UTC).replace(microsecond=0)


def _timestamp(clock: Clock) -> str:
    return _whole_second(clock.utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_id(clock: Clock) -> str:
    return f"{_whole_second(clock.utc_now()).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"


__all__ = [
    "POLYHAVEN_RESOURCE_LISTING_URLS",
    "PolyHavenResourceSyncError",
    "PolyHavenResourceSyncItem",
    "PolyHavenResourceSyncResult",
    "sync_polyhaven_resources",
]
