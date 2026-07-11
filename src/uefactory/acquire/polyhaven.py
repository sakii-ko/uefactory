from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TextIO
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

import yaml

from uefactory import __version__
from uefactory.acquire.failure_journal import (
    ActiveFailure,
    CrossRunFailurePolicy,
    FailureDisposition,
    FailureJournalError,
    append_failure_event,
    append_release_event,
    append_resolution_event,
    empty_failure_journal,
    validate_failure_journal,
)
from uefactory.acquire.runtime import (
    AcquisitionFailure,
    Clock,
    DailyQuotaLimits,
    DailyQuotaRequest,
    DailyQuotaUsage,
    DiskQuotaLimits,
    DiskSnapshot,
    FailureCategory,
    FailureKind,
    MonotonicTokenBucket,
    QuotaExceeded,
    RetryPolicy,
    RuntimeValidationError,
    SystemClock,
    compute_retry_decision,
    parse_retry_after,
    reserve_daily_quota,
    reserve_disk_growth,
)
from uefactory.catalog import Catalog
from uefactory.core.asset_locking import asset_lock
from uefactory.core.config import Settings
from uefactory.core.identity import validate_asset_id
from uefactory.core.ingest_contracts import IMPORT_MANIFEST_SCHEMA_VERSION
from uefactory.core.paths import utc_timestamp
from uefactory.ingest.package_evidence import is_valid_package_bundle_evidence
from uefactory.ingest.quality import is_current_passed_quality
from uefactory.ingest.spec import load_ingest_spec
from uefactory.ingest.staging import bundle_sha256, content_sha256, gltf_dependency_paths
from uefactory.render.thumbnails import (
    is_valid_catalog_scene_sanitization,
    is_valid_thumbnail_validation,
)

POLYHAVEN_MODELS_URL = "https://api.polyhaven.com/assets?type=models"
POLYHAVEN_FILES_URL = "https://api.polyhaven.com/files/{source_id}"
POLYHAVEN_ASSET_URL = "https://polyhaven.com/a/{source_id}"
POLYHAVEN_LICENSE = "CC0-1.0"
POLYHAVEN_LICENSE_URL = "https://polyhaven.com/license"
POLYHAVEN_SOURCE = "polyhaven"
DEFAULT_RESOLUTION = "1k"
STATE_SCHEMA_VERSION = 2
LEGACY_RUN_MANIFEST_SCHEMA_VERSION = 2
RUNTIME_RUN_MANIFEST_SCHEMA_VERSION = 3
RUN_MANIFEST_SCHEMA_VERSION = 4
_SUPPORTED_RUN_MANIFEST_SCHEMA_VERSIONS = frozenset(
    {
        LEGACY_RUN_MANIFEST_SCHEMA_VERSION,
        RUNTIME_RUN_MANIFEST_SCHEMA_VERSION,
        RUN_MANIFEST_SCHEMA_VERSION,
    }
)
COMMIT_INTENT_SCHEMA_VERSION = 1
USER_AGENT = f"UEFactory/{__version__} research downloader"
DEFAULT_REQUEST_RATE_PER_SEC = 2.0

_API_HOST = "api.polyhaven.com"
_DOWNLOAD_HOST = "dl.polyhaven.org"
_SOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{0,39}\Z")
_SHA1_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_MD5_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_RESOLUTION_PATTERN = re.compile(r"[1-9][0-9]*k\Z")
_TERMINAL_STATUSES = frozenset({"imported", "render_ok", "skipped"})
_STATE_STATUSES = _TERMINAL_STATUSES | {"downloaded"}
_SELECTION_CLASSES = frozenset({"unseen", "pending"})
_RESERVED_PACKAGE_NAMES = frozenset(
    {"metadata.json", "manifest.json", "state.json", "commit_intent.json", "generated_ingest.yaml"}
)
_MAX_API_JSON_BYTES = 64 * 1024 * 1024
_MAX_FILE_BYTES = 32 * 1024 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_LISTING_DIGEST_DOMAIN = b"uefactory.polyhaven-model-listing.v1\0"
_PREPARED_MANIFEST_DIGEST_DOMAIN = b"uefactory.polyhaven-prepared-manifest.v1\0"
_STATE_TRANSITION_DIGEST_DOMAIN = b"uefactory.polyhaven-state-transition.v1\0"
_FINALIZED_MANIFEST_DIGEST_DOMAIN = b"uefactory.polyhaven-finalized-manifest.v1\0"
_QUOTA_LEDGER_SCHEMA_VERSION = 1
_QUOTA_DOWNLOAD_KEY_DOMAIN = b"uefactory.polyhaven-quota-download.v1\0"
_REDIRECT_HOOK_ATTRIBUTE = "_uefactory_before_redirect_request"
_OVERSIZE_PROBE_BYTES = 1

TerminalStatus = Literal["imported", "render_ok", "skipped"]


class PolyHavenAcquireError(RuntimeError):
    """Poly Haven metadata or downloaded bytes violated the acquisition contract."""


class PolyHavenPathSecurityError(PolyHavenAcquireError):
    """Provider-controlled path or endpoint data violated the security boundary."""


class PolyHavenIntegrityError(PolyHavenAcquireError):
    """Provider-delivered framing or bytes violated their declared closure."""


@dataclass(frozen=True, slots=True)
class PolyHavenRuntimeConfig:
    """Serializable network pacing, retry, and quota controls for one sync."""

    request_rate_per_sec: float = DEFAULT_REQUEST_RATE_PER_SEC
    request_burst: int = 1
    retry_max_attempts: int = 5
    integrity_max_attempts: int = 2
    retry_base_delay_sec: float = 5.0
    retry_max_delay_sec: float = 900.0
    max_retry_after_sec: float = 3_600.0
    max_new_items_per_day: int | None = None
    max_download_bytes_per_day: int | None = None
    max_storage_bytes: int | None = None
    min_free_bytes: int = 0
    cross_run_backoff_base_sec: float = 300.0
    cross_run_backoff_max_sec: float = 86_400.0
    integrity_quarantine_after_runs: int = 3
    retry_policy: RetryPolicy = field(init=False, repr=False)
    failure_policy: CrossRunFailurePolicy = field(init=False, repr=False)
    daily_quota_limits: DailyQuotaLimits = field(init=False, repr=False)
    disk_quota_limits: DiskQuotaLimits = field(init=False, repr=False)

    def __post_init__(self) -> None:
        rate = self.request_rate_per_sec
        if (
            isinstance(rate, bool)
            or not isinstance(rate, int | float)
            or not math.isfinite(float(rate))
            or not 1.0 / (366 * 24 * 60 * 60) <= float(rate) <= 1_000_000.0
        ):
            raise PolyHavenAcquireError(
                "request_rate_per_sec must be a finite rate between 1/31622400 and 1000000"
            )
        if (
            isinstance(self.request_burst, bool)
            or not isinstance(self.request_burst, int)
            or not 1 <= self.request_burst <= 1_000_000
        ):
            raise PolyHavenAcquireError("request_burst must be an integer between 1 and 1000000")
        try:
            retry_policy = RetryPolicy(
                max_attempts=self.retry_max_attempts,
                integrity_max_attempts=self.integrity_max_attempts,
                base_delay_sec=self.retry_base_delay_sec,
                max_delay_sec=self.retry_max_delay_sec,
                max_retry_after_sec=self.max_retry_after_sec,
            )
            daily_limits = DailyQuotaLimits(
                max_new_items=self.max_new_items_per_day,
                max_download_bytes=self.max_download_bytes_per_day,
            )
            disk_limits = DiskQuotaLimits(
                max_storage_bytes=self.max_storage_bytes,
                min_free_bytes=self.min_free_bytes,
            )
            failure_policy = CrossRunFailurePolicy(
                backoff_base_sec=self.cross_run_backoff_base_sec,
                backoff_max_sec=self.cross_run_backoff_max_sec,
                integrity_quarantine_after=self.integrity_quarantine_after_runs,
            )
        except (FailureJournalError, RuntimeValidationError) as exc:
            raise PolyHavenAcquireError(f"invalid Poly Haven runtime configuration: {exc}") from exc
        object.__setattr__(self, "request_rate_per_sec", float(rate))
        object.__setattr__(self, "retry_policy", retry_policy)
        object.__setattr__(self, "daily_quota_limits", daily_limits)
        object.__setattr__(self, "disk_quota_limits", disk_limits)
        object.__setattr__(self, "failure_policy", failure_policy)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_rate_per_sec": self.request_rate_per_sec,
            "request_burst": self.request_burst,
            "retry": {
                "max_attempts": self.retry_policy.max_attempts,
                "integrity_max_attempts": self.retry_policy.integrity_max_attempts,
                "base_delay_sec": self.retry_policy.base_delay_sec,
                "max_delay_sec": self.retry_policy.max_delay_sec,
                "max_retry_after_sec": self.retry_policy.max_retry_after_sec,
            },
            "daily_quota": {
                "max_new_items": self.daily_quota_limits.max_new_items,
                "max_download_bytes": self.daily_quota_limits.max_download_bytes,
            },
            "disk_quota": {
                "max_storage_bytes": self.disk_quota_limits.max_storage_bytes,
                "min_free_bytes": self.disk_quota_limits.min_free_bytes,
            },
            "failure_schedule": {
                "backoff_base_sec": self.failure_policy.backoff_base_sec,
                "backoff_max_sec": self.failure_policy.backoff_max_sec,
                "integrity_quarantine_after_runs": (self.failure_policy.integrity_quarantine_after),
            },
        }


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        _require_https_host(newurl, allowed_hosts=self.allowed_hosts, context="redirect target")
        hook = getattr(req, _REDIRECT_HOOK_ATTRIBUTE, None)
        if hook is not None:
            if not callable(hook):
                raise PolyHavenAcquireError("redirect request hook is invalid")
            hook()
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and hook is not None:
            setattr(redirected, _REDIRECT_HOOK_ATTRIBUTE, hook)
        return redirected


@dataclass(frozen=True, slots=True)
class PolyHavenModel:
    source_id: str
    asset_id: str
    name: str
    date_published: int
    revision: str
    authors: tuple[tuple[str, str], ...]
    categories: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolyHavenFileSpec:
    relative_path: Path
    url: str
    bytes: int
    md5: str


@dataclass(frozen=True, slots=True)
class PolyHavenModelPackage:
    source_id: str
    resolution: str
    main_file: Path
    files: tuple[PolyHavenFileSpec, ...]

    @property
    def dependencies(self) -> tuple[Path, ...]:
        return tuple(
            item.relative_path for item in self.files if item.relative_path != self.main_file
        )


@dataclass(frozen=True, slots=True)
class PolyHavenSyncItem:
    asset_id: str
    source_id: str
    revision: str
    root_dir: Path
    main_path: Path
    dependency_paths: tuple[Path, ...]
    metadata_path: Path
    downloaded_files: int
    reused_files: int
    downloaded_bytes: int
    verified_bytes: int
    source_bundle_sha256: str
    source_content_sha256: str
    acquired_at: str
    verified_at: str
    state_status: str


@dataclass(frozen=True, slots=True)
class PolyHavenSyncResult:
    run_dir: Path
    manifest_path: Path
    state_path: Path
    generated_spec_path: Path | None
    items: tuple[PolyHavenSyncItem, ...]
    discovered: int
    selected: int
    downloaded_files: int
    reused_files: int
    downloaded_bytes: int
    verified_bytes: int
    snapshot_sha256: str
    runtime_evidence: Mapping[str, Any] | None = None
    attempted: int = 0
    failed: int = 0
    deferred: int = 0
    quarantined: int = 0
    released: int = 0
    failure_journal_path: Path | None = None


@dataclass(frozen=True, slots=True)
class _DownloadedFile:
    spec: PolyHavenFileSpec
    path: Path
    sha256: str
    reused: bool
    downloaded_bytes: int


@dataclass(frozen=True, slots=True)
class _LoadedState:
    payload: dict[str, Any]
    before_exists: bool
    before_file_sha256: str | None
    before_payload_sha256: str | None
    migrated_from: int | None


@dataclass(frozen=True, slots=True)
class _LoadedFailureJournal:
    payload: dict[str, Any]
    active: Mapping[str, ActiveFailure]
    before_exists: bool
    before_file_sha256: str | None
    before_payload_sha256: str | None


@dataclass(slots=True)
class _RuntimeStats:
    request_attempts: int = 0
    retry_attempts: int = 0
    retry_after_honored: int = 0
    rate_limit_wait_sec: float = 0.0
    retry_wait_sec: float = 0.0
    disk_checks: int = 0
    max_storage_bytes_observed: int | None = None
    min_free_bytes_observed: int | None = None
    new_items_reserved: int = 0
    download_bytes_reserved: int = 0
    download_bytes_overage: int = 0
    download_probe_bytes_released: int = 0
    download_body_bytes: int = 0
    deferred_new_items: int = 0


class _AttemptFailure(Exception):
    def __init__(
        self,
        failure: AcquisitionFailure,
        *,
        retry_after_value: str | None = None,
    ) -> None:
        self.failure = failure
        self.retry_after_value = retry_after_value
        super().__init__(failure.message)


class _ClassifiedItemFailure(PolyHavenAcquireError):
    def __init__(
        self,
        failure: AcquisitionFailure,
        *,
        attempts_in_run: int,
        retry_after_deadline: datetime | None = None,
        exhausted: bool = False,
    ) -> None:
        self.failure = failure
        self.attempts_in_run = attempts_in_run
        self.retry_after_deadline = retry_after_deadline
        self.exhausted = exhausted
        suffix = "retry budget exhausted" if exhausted else "not retryable"
        super().__init__(f"{failure.message} ({suffix})")


class _DailyQuotaLedger:
    def __init__(
        self,
        *,
        path: Path,
        project_root: Path,
        clock: Clock,
        limits: DailyQuotaLimits,
        stats: _RuntimeStats,
    ) -> None:
        self.path = path
        self.project_root = project_root
        self.clock = clock
        self.limits = limits
        self.stats = stats
        self.enabled = limits.max_new_items is not None or limits.max_download_bytes is not None
        today = _clock_utc_now(clock).date()
        if self.enabled and (path.exists() or path.is_symlink()):
            _require_regular_file(path, "Poly Haven quota ledger")
            payload = _read_json_object_strict(path, "Poly Haven quota ledger")
            _validate_quota_ledger(payload)
            ledger_day = date.fromisoformat(str(payload["utc_day"]))
            _quota_usage(payload).roll_forward(today)
            if ledger_day != today:
                payload = _empty_quota_ledger(today)
                self.payload = payload
                self._persist()
            else:
                self.payload = payload
        else:
            self.payload = _empty_quota_ledger(today)
        self.before_usage = _quota_usage(self.payload)

    @property
    def item_ids(self) -> frozenset[str]:
        return frozenset(str(item) for item in self.payload["item_ids"])

    @property
    def usage(self) -> DailyQuotaUsage:
        return _quota_usage(self.payload)

    def allowed_unseen_ids(
        self,
        models: tuple[PolyHavenModel, ...],
        *,
        state: Mapping[str, Any],
        ineligible_unseen_ids: frozenset[str] = frozenset(),
    ) -> frozenset[str] | None:
        if not isinstance(ineligible_unseen_ids, frozenset):
            raise PolyHavenAcquireError("ineligible unseen ids must be a frozenset")
        maximum = self.limits.max_new_items
        if maximum is None:
            return None
        self._require_reservation_day()
        state_items = _object(state.get("items"), "Poly Haven state items")
        already_reserved = self.item_ids
        remaining = max(0, maximum - self.usage.new_items_reserved)
        allowed: set[str] = set()
        for model in models:
            if model.asset_id in state_items or model.asset_id in ineligible_unseen_ids:
                continue
            if model.asset_id in already_reserved:
                allowed.add(model.asset_id)
            elif remaining:
                allowed.add(model.asset_id)
                remaining -= 1
        return frozenset(allowed)

    def reserve_items(self, asset_ids: tuple[str, ...]) -> None:
        if not self.enabled:
            return
        existing = self.item_ids
        fresh = tuple(sorted(set(asset_ids) - existing))
        if not fresh:
            return
        self._require_reservation_day()
        previous_payload = json.loads(json.dumps(self.payload))
        try:
            reservation = reserve_daily_quota(
                limits=self.limits,
                usage=self.usage,
                request=DailyQuotaRequest(new_items=len(fresh)),
            )
        except (QuotaExceeded, RuntimeValidationError) as exc:
            raise PolyHavenAcquireError(
                f"Poly Haven daily quota rejected selected items: {exc}"
            ) from exc
        self.payload["usage"] = _quota_usage_payload(reservation.after)
        self.payload["item_ids"] = sorted(existing | set(fresh))
        try:
            self._persist()
        except BaseException:
            self.payload = previous_payload
            raise
        self.stats.new_items_reserved += len(fresh)

    def begin_download(
        self,
        *,
        asset_id: str,
        spec: PolyHavenFileSpec,
        maximum_probe_bytes: int = _OVERSIZE_PROBE_BYTES,
    ) -> str | None:
        if isinstance(maximum_probe_bytes, bool) or maximum_probe_bytes not in {
            0,
            _OVERSIZE_PROBE_BYTES,
        }:
            raise PolyHavenAcquireError("maximum oversize probe is invalid")
        if not self.enabled or self.limits.max_download_bytes is None:
            return None
        key = _quota_download_key(asset_id=asset_id, spec=spec)
        downloads = _object(self.payload["open_downloads"], "quota ledger open_downloads")
        existing = downloads.get(key)
        if existing is not None:
            checked = _object(existing, "quota ledger open download")
            probe_bytes = _nonnegative_int(
                checked.get("oversize_probe_bytes"),
                "quota ledger oversize_probe_bytes",
            )
            body_bytes_claimed = _nonnegative_int(
                checked.get("body_bytes_claimed"),
                "quota ledger body_bytes_claimed",
            )
            reserved_bytes = spec.bytes + probe_bytes
            expected = _quota_download_payload(
                asset_id=asset_id,
                spec=spec,
                reserved_bytes=reserved_bytes,
                oversize_probe_bytes=probe_bytes,
                body_bytes_claimed=body_bytes_claimed,
            )
            comparable = dict(checked)
            comparable["reserved_bytes"] = reserved_bytes
            if (
                comparable != expected
                or _nonnegative_int(checked.get("reserved_bytes"), "quota ledger reserved_bytes")
                < reserved_bytes
            ):
                raise PolyHavenAcquireError(
                    "Poly Haven quota ledger download reservation conflicts"
                )
            return key
        self._require_reservation_day()
        probe_bytes = min(
            maximum_probe_bytes,
            self.download_probe_bytes(asset_id=asset_id, spec=spec),
        )
        reserved_bytes = spec.bytes + probe_bytes
        expected = _quota_download_payload(
            asset_id=asset_id,
            spec=spec,
            reserved_bytes=reserved_bytes,
            oversize_probe_bytes=probe_bytes,
        )
        previous_payload = json.loads(json.dumps(self.payload))
        reserved_usage = self._reserve_download_bytes(reserved_bytes)
        self.payload["usage"] = _quota_usage_payload(reserved_usage)
        downloads[key] = expected
        try:
            self._persist()
        except BaseException:
            self.payload = previous_payload
            raise
        self.stats.download_bytes_reserved += reserved_bytes
        return key

    def download_probe_bytes(self, *, asset_id: str, spec: PolyHavenFileSpec) -> int:
        """Return the durable oversize probe available for this download.

        The probe is charged against the configured byte quota. At an exact
        quota boundary no probe is issued, so a valid body that exactly fills
        the user's allowance remains downloadable without receiving an
        unaccounted byte from the transport.
        """

        if not self.enabled or self.limits.max_download_bytes is None:
            return _OVERSIZE_PROBE_BYTES
        key = _quota_download_key(asset_id=asset_id, spec=spec)
        downloads = _object(self.payload["open_downloads"], "quota ledger open_downloads")
        existing = downloads.get(key)
        if existing is not None:
            checked = _object(existing, "quota ledger open download")
            probe_bytes = _nonnegative_int(
                checked.get("oversize_probe_bytes"),
                "quota ledger oversize_probe_bytes",
            )
            if probe_bytes not in {0, _OVERSIZE_PROBE_BYTES}:
                raise PolyHavenAcquireError("quota ledger oversize probe is invalid")
            return probe_bytes
        self._require_reservation_day()
        maximum = self.limits.max_download_bytes
        if maximum is None:  # Narrowed above; retained for static type checking.
            return _OVERSIZE_PROBE_BYTES
        remaining = maximum - self.usage.download_bytes_reserved
        return _OVERSIZE_PROBE_BYTES if remaining >= spec.bytes + 1 else 0

    def claim_download_body(self, key: str | None, count: int) -> None:
        """Durably reserve body bytes before asking the transport for them."""

        if key is None or count == 0:
            return
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PolyHavenAcquireError("download body claim must be a non-negative integer")
        downloads = _object(self.payload["open_downloads"], "quota ledger open_downloads")
        raw = _object(downloads.get(key), "quota ledger open download")
        claimed = _nonnegative_int(
            raw.get("body_bytes_claimed"),
            "quota ledger body_bytes_claimed",
        )
        reserved = _nonnegative_int(raw.get("reserved_bytes"), "quota ledger reserved_bytes")
        new_claimed = claimed + count
        additional = max(0, new_claimed - reserved)
        previous_payload = json.loads(json.dumps(self.payload))
        if additional:
            reserved_usage = self._reserve_download_bytes(additional)
            self.payload["usage"] = _quota_usage_payload(reserved_usage)
            raw["reserved_bytes"] = reserved + additional
        raw["body_bytes_claimed"] = new_claimed
        try:
            self._persist()
        except BaseException:
            self.payload = previous_payload
            raise
        self.stats.download_bytes_reserved += additional

    def release_download_body_claim(self, key: str | None, count: int) -> None:
        """Release bytes a completed read proved were not delivered."""

        if key is None or count == 0:
            return
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PolyHavenAcquireError("download body release must be a non-negative integer")
        downloads = _object(self.payload["open_downloads"], "quota ledger open_downloads")
        raw = _object(downloads.get(key), "quota ledger open download")
        claimed = _nonnegative_int(
            raw.get("body_bytes_claimed"),
            "quota ledger body_bytes_claimed",
        )
        if count > claimed:
            raise PolyHavenAcquireError("download body release exceeds its durable claim")
        previous_payload = json.loads(json.dumps(self.payload))
        raw["body_bytes_claimed"] = claimed - count
        try:
            self._persist()
        except BaseException:
            self.payload = previous_payload
            raise

    def claim_download_body_floor(self, key: str | None, count: int) -> None:
        """Bind a pre-existing partial to at least its persisted body bytes."""

        if key is None or count == 0:
            return
        downloads = _object(self.payload["open_downloads"], "quota ledger open_downloads")
        raw = _object(downloads.get(key), "quota ledger open download")
        claimed = _nonnegative_int(
            raw.get("body_bytes_claimed"),
            "quota ledger body_bytes_claimed",
        )
        if claimed < count:
            self.claim_download_body(key, count - claimed)

    def finish_oversized_download(self, key: str | None, extra_bytes: int) -> None:
        """Atomically record received overage and close its reservation.

        This deliberately bypasses the configured limit: once transport has
        delivered an unexpected byte, the durable ledger must reflect that debt
        and remove the reusable open reservation in the same commit.
        """

        if key is None or extra_bytes == 0:
            return
        if isinstance(extra_bytes, bool) or not isinstance(extra_bytes, int) or extra_bytes < 0:
            raise PolyHavenAcquireError("download overage must be a non-negative integer")
        downloads = _object(self.payload["open_downloads"], "quota ledger open_downloads")
        raw = _object(downloads.get(key), "quota ledger open download")
        probe_bytes = _nonnegative_int(
            raw.get("oversize_probe_bytes"),
            "quota ledger oversize_probe_bytes",
        )
        if extra_bytes > probe_bytes:
            raise PolyHavenAcquireError("download overage exceeds its durable probe reservation")
        previous_payload = json.loads(json.dumps(self.payload))
        downloads.pop(key)
        try:
            self._persist()
        except Exception as exc:
            self.payload = previous_payload
            raise PolyHavenAcquireError(
                "Poly Haven oversized download ledger commit failed"
            ) from exc
        except BaseException:
            self.payload = previous_payload
            raise
        self.stats.download_bytes_overage += extra_bytes

    def recover_oversized_download(self, key: str | None, extra_bytes: int) -> None:
        """Idempotently settle a durable oversized-body marker after restart."""

        if key is None or extra_bytes == 0:
            return
        if isinstance(extra_bytes, bool) or not isinstance(extra_bytes, int) or extra_bytes < 0:
            raise PolyHavenAcquireError("download overage must be a non-negative integer")
        downloads = _object(self.payload["open_downloads"], "quota ledger open_downloads")
        if key not in downloads:
            # The close may already have committed before a crash prevented the
            # marker unlink, or a UTC-day rollover may have retired the debt.
            return
        self.finish_oversized_download(key, extra_bytes)

    def finish_download(
        self,
        key: str | None,
        *,
        release_probe: bool = True,
    ) -> None:
        if not isinstance(release_probe, bool):
            raise PolyHavenAcquireError("download probe release flag must be boolean")
        if key is None:
            return
        downloads = _object(self.payload["open_downloads"], "quota ledger open_downloads")
        previous_payload = json.loads(json.dumps(self.payload))
        raw = downloads.get(key)
        if raw is None:
            return
        checked = _object(raw, "quota ledger open download")
        probe_bytes = _nonnegative_int(
            checked.get("oversize_probe_bytes"),
            "quota ledger oversize_probe_bytes",
        )
        reserved_bytes = _nonnegative_int(
            checked.get("reserved_bytes"),
            "quota ledger reserved_bytes",
        )
        body_bytes_claimed = _nonnegative_int(
            checked.get("body_bytes_claimed"),
            "quota ledger body_bytes_claimed",
        )
        released_probe_bytes = (
            probe_bytes
            if release_probe and reserved_bytes - probe_bytes >= body_bytes_claimed
            else 0
        )
        usage = self.usage
        try:
            updated_usage = DailyQuotaUsage(
                utc_day=usage.utc_day,
                new_items_reserved=usage.new_items_reserved,
                download_bytes_reserved=(usage.download_bytes_reserved - released_probe_bytes),
            )
        except RuntimeValidationError as exc:
            raise PolyHavenAcquireError(f"Poly Haven oversize probe release failed: {exc}") from exc
        self.payload["usage"] = _quota_usage_payload(updated_usage)
        downloads.pop(key)
        try:
            self._persist()
        except BaseException:
            self.payload = previous_payload
            raise
        self.stats.download_probe_bytes_released += released_probe_bytes

    def evidence(self) -> dict[str, Any]:
        after = self.usage
        return {
            "enabled": self.enabled,
            "ledger_path": (_portable_path(self.path, self.project_root) if self.enabled else None),
            "utc_day": after.utc_day.isoformat(),
            "usage_before": _quota_usage_payload(self.before_usage),
            "reserved_by_run": {
                "new_items": self.stats.new_items_reserved,
                "download_bytes": self.stats.download_bytes_reserved,
            },
            "accounted_overage_bytes": self.stats.download_bytes_overage,
            "released_probe_bytes": self.stats.download_probe_bytes_released,
            "deferred_new_items": self.stats.deferred_new_items,
            "usage_after": _quota_usage_payload(after),
            "item_reservations_after": len(self.payload["item_ids"]),
            "open_downloads_after": len(self.payload["open_downloads"]),
            "ledger_file_sha256": (
                _sha256_file(self.path) if self.enabled and self.path.is_file() else None
            ),
        }

    def _reserve_download_bytes(self, count: int) -> DailyQuotaUsage:
        self._require_reservation_day()
        try:
            reservation = reserve_daily_quota(
                limits=self.limits,
                usage=self.usage,
                request=DailyQuotaRequest(download_bytes=count),
            )
        except (QuotaExceeded, RuntimeValidationError) as exc:
            raise _ClassifiedItemFailure(
                AcquisitionFailure(
                    kind=FailureKind.QUOTA,
                    phase="download_quota",
                    message=f"Poly Haven daily download quota rejected bytes: {exc}",
                ),
                attempts_in_run=1,
            ) from exc
        return reservation.after

    def _require_reservation_day(self) -> None:
        current = _clock_utc_now(self.clock).date()
        ledger_day = self.usage.utc_day
        if current != ledger_day:
            raise PolyHavenAcquireError(
                "Poly Haven UTC quota day changed during this run; restart before reserving "
                "new work"
            )

    def _persist(self) -> None:
        self.payload["updated_at"] = _clock_utc_now(self.clock).strftime("%Y-%m-%dT%H:%M:%SZ")
        _validate_quota_ledger(self.payload)
        _write_json_atomic(self.path, self.payload)


class _AcquisitionRuntime:
    def __init__(
        self,
        *,
        config: PolyHavenRuntimeConfig,
        clock: Clock,
        project_root: Path,
        data_dir: Path,
        storage_root: Path | None = None,
        additional_storage_roots: tuple[Path, ...] = (),
    ) -> None:
        self.config = config
        self.clock = clock
        self.project_root = project_root
        self.data_dir = data_dir
        # Keep the model adapter's historical default while allowing the
        # provider-level facade to account for every Poly Haven asset class in
        # one storage boundary. ``models_root`` remains as a compatibility
        # alias for the existing model-adapter tests and private callers.
        self.storage_root = (
            data_dir / "acquire/polyhaven/models" if storage_root is None else storage_root
        )
        self.models_root = self.storage_root
        self.additional_storage_roots = additional_storage_roots
        self.stats = _RuntimeStats()
        try:
            self.limiter = MonotonicTokenBucket(
                clock=clock,
                rate_per_sec=config.request_rate_per_sec,
                burst=config.request_burst,
            )
        except RuntimeValidationError as exc:
            raise PolyHavenAcquireError(f"invalid Poly Haven request limiter: {exc}") from exc
        self.quota = _DailyQuotaLedger(
            path=data_dir / "acquire/polyhaven/quota_state.json",
            project_root=project_root,
            clock=clock,
            limits=config.daily_quota_limits,
            stats=self.stats,
        )

    def start_request(self) -> None:
        try:
            self.stats.rate_limit_wait_sec += self.limiter.acquire()
        except RuntimeValidationError as exc:
            raise PolyHavenAcquireError(f"Poly Haven request limiter failed: {exc}") from exc
        self.stats.request_attempts += 1

    def run_with_retries(self, *, phase: str, operation: Any) -> Any:
        failures_by_category: dict[FailureCategory, int] = {}
        attempts_in_run = 0
        retry_after_deadline: datetime | None = None
        while True:
            attempts_in_run += 1
            try:
                return operation()
            except _AttemptFailure as exc:
                category = exc.failure.category
                failure_count = failures_by_category.get(category, 0) + 1
                failures_by_category[category] = failure_count
                retry_after = None
                if exc.retry_after_value is not None:
                    try:
                        retry_after = parse_retry_after(
                            exc.retry_after_value,
                            now=_clock_utc_now(self.clock),
                            max_delay_sec=self.config.retry_policy.max_retry_after_sec,
                        )
                    except RuntimeValidationError as parse_exc:
                        raise _ClassifiedItemFailure(
                            AcquisitionFailure(
                                kind=FailureKind.SCHEMA,
                                phase=phase,
                                message=(
                                    f"Poly Haven {phase} returned an invalid Retry-After header"
                                ),
                            ),
                            attempts_in_run=attempts_in_run,
                        ) from parse_exc
                    if retry_after is not None and (
                        retry_after_deadline is None
                        or retry_after.deadline_utc > retry_after_deadline
                    ):
                        retry_after_deadline = retry_after.deadline_utc
                try:
                    decision = compute_retry_decision(
                        policy=self.config.retry_policy,
                        failure=exc.failure,
                        consecutive_failures=failure_count,
                        now=_clock_utc_now(self.clock),
                        retry_after=retry_after,
                    )
                except RuntimeValidationError as decision_exc:
                    raise PolyHavenAcquireError(
                        f"Poly Haven {phase} retry decision failed: {decision_exc}"
                    ) from decision_exc
                if not decision.will_retry or decision.delay_sec is None:
                    raise _ClassifiedItemFailure(
                        exc.failure,
                        attempts_in_run=attempts_in_run,
                        retry_after_deadline=retry_after_deadline,
                        exhausted=decision.exhausted,
                    ) from exc
                self.stats.retry_attempts += 1
                if retry_after is not None:
                    self.stats.retry_after_honored += 1
                self.stats.retry_wait_sec += decision.delay_sec
                self.clock.sleep(decision.delay_sec)

    def record_download_body_bytes(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PolyHavenAcquireError("download body byte count must be a non-negative integer")
        self.stats.download_body_bytes += count

    def check_disk_growth(
        self,
        growth_bytes: int,
        *,
        optional_probe_bytes: int = 0,
    ) -> int:
        if isinstance(optional_probe_bytes, bool) or optional_probe_bytes not in {
            0,
            _OVERSIZE_PROBE_BYTES,
        }:
            raise PolyHavenAcquireError("optional disk probe is invalid")
        snapshot = _polyhaven_disk_snapshot(
            models_root=self.models_root,
            data_dir=self.data_dir,
            additional_roots=self.additional_storage_roots,
        )
        self.stats.disk_checks += 1
        self.stats.max_storage_bytes_observed = max(
            snapshot.storage_bytes,
            self.stats.max_storage_bytes_observed or 0,
        )
        self.stats.min_free_bytes_observed = min(
            snapshot.free_bytes,
            self.stats.min_free_bytes_observed
            if self.stats.min_free_bytes_observed is not None
            else snapshot.free_bytes,
        )
        try:
            reserve_disk_growth(
                limits=self.config.disk_quota_limits,
                snapshot=snapshot,
                growth_bytes=growth_bytes + optional_probe_bytes,
            )
        except QuotaExceeded as probe_exc:
            if optional_probe_bytes == 0:
                raise _ClassifiedItemFailure(
                    AcquisitionFailure(
                        kind=FailureKind.DISK,
                        phase="disk_quota",
                        message=f"Poly Haven disk quota rejected download: {probe_exc}",
                    ),
                    attempts_in_run=1,
                ) from probe_exc
            try:
                reserve_disk_growth(
                    limits=self.config.disk_quota_limits,
                    snapshot=snapshot,
                    growth_bytes=growth_bytes,
                )
            except (QuotaExceeded, RuntimeValidationError) as exc:
                raise _ClassifiedItemFailure(
                    AcquisitionFailure(
                        kind=FailureKind.DISK,
                        phase="disk_quota",
                        message=f"Poly Haven disk quota rejected download: {exc}",
                    ),
                    attempts_in_run=1,
                ) from exc
            return 0
        except RuntimeValidationError as exc:
            raise PolyHavenAcquireError(f"Poly Haven disk quota rejected download: {exc}") from exc
        return optional_probe_bytes

    def evidence(self) -> dict[str, Any]:
        return {
            "http": {
                "request_attempts": self.stats.request_attempts,
                "retry_attempts": self.stats.retry_attempts,
                "retry_after_honored": self.stats.retry_after_honored,
                "rate_limit_wait_ms": _milliseconds(self.stats.rate_limit_wait_sec),
                "retry_wait_ms": _milliseconds(self.stats.retry_wait_sec),
                "download_body_bytes": self.stats.download_body_bytes,
            },
            "daily_quota": self.quota.evidence(),
            "disk": {
                "checks": self.stats.disk_checks,
                "max_storage_bytes_observed": self.stats.max_storage_bytes_observed,
                "min_free_bytes_observed": self.stats.min_free_bytes_observed,
            },
        }


def revisioned_asset_id(source_id: str, revision: str) -> str:
    """Return the immutable catalog identity for one provider file revision."""

    checked_source_id = _source_id(source_id, "source_id")
    if not isinstance(revision, str) or _SHA1_PATTERN.fullmatch(revision) is None:
        raise PolyHavenAcquireError("revision must be a lowercase 40-character SHA-1")
    candidate = f"polyhaven_{checked_source_id.lower()}_{revision[:12]}"
    try:
        return validate_asset_id(candidate)
    except ValueError as exc:
        raise PolyHavenAcquireError(
            f"Poly Haven source id cannot form a catalog asset id: {source_id!r}"
        ) from exc


def polyhaven_failure_report(
    *,
    settings: Settings,
    status: Literal["active", "quarantined", "all"] = "active",
) -> dict[str, Any]:
    """Return a validated, read-only view of the durable revision failure journal."""

    if status not in {"active", "quarantined", "all"}:
        raise PolyHavenAcquireError("failure report status must be active, quarantined, or all")
    project_root = settings.project_root.expanduser().resolve()
    data_dir = settings.data_dir.expanduser().resolve()
    _require_data_dir_inside_project(project_root=project_root, data_dir=data_dir)
    path = data_dir / "acquire/polyhaven/failure_journal.json"
    _reject_symlink_components(
        path,
        project_root=project_root,
        context="Poly Haven failure journal report",
    )
    loaded = _load_failure_journal(path)
    records = []
    for asset_id, active in sorted(loaded.active.items()):
        if status == "quarantined" and active.disposition is not FailureDisposition.QUARANTINED:
            continue
        records.append(
            {
                "asset_id": asset_id,
                "source_id": active.source_id,
                "revision": active.revision,
                "resolution": active.resolution,
                "event_id": active.event_id,
                "run_id": active.run_id,
                "recorded_at": active.recorded_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "failure": {
                    "category": active.failure.category.value,
                    "kind": active.failure.kind.value,
                    "phase": active.failure.phase,
                    "message": active.failure.message,
                    "http_status": active.failure.http_status,
                },
                "consecutive_failures": active.consecutive_failures,
                "integrity_streak": active.integrity_streak,
                "attempts_in_run": active.attempts_in_run,
                "disposition": active.disposition.value,
                "next_eligible_at": (
                    None
                    if active.next_eligible_at is None
                    else active.next_eligible_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                ),
            }
        )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "source": POLYHAVEN_SOURCE,
        "asset_type": "models",
        "status_filter": status,
        "journal_path": str(path),
        "event_count": len(loaded.payload["events"]),
        "head_event_sha256": loaded.payload["head_event_sha256"],
        "active_count": len(records),
        "active": records,
    }
    if status == "all":
        payload["events"] = json.loads(json.dumps(loaded.payload["events"]))
    return payload


def parse_polyhaven_model_listing(payload: Any) -> tuple[PolyHavenModel, ...]:
    """Validate the official `/assets?type=models` response."""

    root = _object(payload, "Poly Haven model listing")
    if not root:
        raise PolyHavenAcquireError("Poly Haven model listing is empty")
    models: list[PolyHavenModel] = []
    normalized_sources: dict[str, str] = {}
    for raw_source_id, raw_model in root.items():
        source_id = _source_id(raw_source_id, "model source id")
        normalized = source_id.lower()
        previous = normalized_sources.get(normalized)
        if previous is not None and previous != source_id:
            raise PolyHavenAcquireError(
                "Poly Haven source ids collide after lowercase normalization: "
                f"{previous!r}, {source_id!r}"
            )
        normalized_sources[normalized] = source_id
        model = _object(raw_model, f"model {source_id!r}")
        model_type = model.get("type")
        if isinstance(model_type, bool) or model_type != 2:
            raise PolyHavenAcquireError(f"model {source_id!r}.type must be integer 2")
        name = _string(model.get("name"), f"model {source_id!r}.name", max_length=256)
        date_published = _positive_int(
            model.get("date_published"),
            f"model {source_id!r}.date_published",
        )
        revision = _string(
            model.get("files_hash"),
            f"model {source_id!r}.files_hash",
            max_length=40,
        )
        if _SHA1_PATTERN.fullmatch(revision) is None:
            raise PolyHavenAcquireError(
                f"model {source_id!r}.files_hash must be a lowercase 40-character SHA-1"
            )
        authors_raw = _object(model.get("authors"), f"model {source_id!r}.authors")
        if not authors_raw:
            raise PolyHavenAcquireError(f"model {source_id!r}.authors is empty")
        authors = tuple(
            sorted(
                (
                    _string(author, f"model {source_id!r}.author", max_length=256),
                    _string(credit, f"model {source_id!r}.author credit", max_length=256),
                )
                for author, credit in authors_raw.items()
            )
        )
        categories = _string_sequence(
            model.get("categories"),
            f"model {source_id!r}.categories",
            max_items=256,
        )
        tags = _string_sequence(
            model.get("tags"),
            f"model {source_id!r}.tags",
            max_items=512,
        )
        models.append(
            PolyHavenModel(
                source_id=source_id,
                asset_id=revisioned_asset_id(source_id, revision),
                name=name,
                date_published=date_published,
                revision=revision,
                authors=authors,
                categories=categories,
                tags=tags,
            )
        )
    return tuple(sorted(models, key=lambda item: (item.date_published, item.source_id.casefold())))


def parse_polyhaven_model_files(
    source_id: str,
    payload: Any,
    *,
    resolution: str = DEFAULT_RESOLUTION,
) -> PolyHavenModelPackage:
    """Validate and select one glTF package and its exact include closure."""

    checked_source_id = _source_id(source_id, "source_id")
    checked_resolution = _resolution(resolution)
    root = _object(payload, f"files for {checked_source_id!r}")
    try:
        gltf_formats = _object(root["gltf"], f"files {checked_source_id!r}.gltf")
        resolution_entry = _object(
            gltf_formats[checked_resolution],
            f"files {checked_source_id!r}.gltf.{checked_resolution}",
        )
        main_entry = _object(
            resolution_entry["gltf"],
            f"files {checked_source_id!r}.gltf.{checked_resolution}.gltf",
        )
    except KeyError as exc:
        raise PolyHavenAcquireError(
            f"Poly Haven model {checked_source_id!r} has no {checked_resolution} glTF package"
        ) from exc
    main_url = _download_url(
        main_entry.get("url"),
        f"files {checked_source_id!r}.gltf.{checked_resolution}.gltf.url",
    )
    main_name = unquote(PurePosixPath(urlsplit(main_url).path).name)
    main_path = _relative_path(main_name, "glTF main file")
    if main_path.suffix.casefold() != ".gltf":
        raise PolyHavenAcquireError(f"Poly Haven model {checked_source_id!r} main file is not glTF")
    main_spec = _file_spec(
        main_path,
        main_entry,
        context=f"model {checked_source_id!r} main",
        allow_include=True,
    )
    include = _object(main_entry.get("include"), f"model {checked_source_id!r} include")
    if not include:
        raise PolyHavenAcquireError(
            f"Poly Haven model {checked_source_id!r} glTF package has no included files"
        )
    dependencies: list[PolyHavenFileSpec] = []
    seen = {main_path.as_posix()}
    for raw_path, raw_entry in sorted(include.items()):
        relative_path = _relative_path(raw_path, f"model {checked_source_id!r} include path")
        normalized = relative_path.as_posix()
        if normalized in seen:
            raise PolyHavenAcquireError(
                f"Poly Haven model {checked_source_id!r} has duplicate package path {normalized!r}"
            )
        seen.add(normalized)
        dependencies.append(
            _file_spec(
                relative_path,
                _object(raw_entry, f"model {checked_source_id!r} include {normalized!r}"),
                context=f"model {checked_source_id!r} include {normalized!r}",
            )
        )
    return PolyHavenModelPackage(
        source_id=checked_source_id,
        resolution=checked_resolution,
        main_file=main_path,
        files=(main_spec, *dependencies),
    )


def _sync_model_revision(
    *,
    model: PolyHavenModel,
    resolution: str,
    force: bool,
    data_dir: Path,
    project_root: Path,
    runtime: _AcquisitionRuntime,
    state: Mapping[str, Any],
    run_id: str,
) -> tuple[PolyHavenSyncItem, dict[str, Any]]:
    files_url = POLYHAVEN_FILES_URL.format(source_id=quote(model.source_id, safe=""))
    try:
        files_payload = _fetch_json(files_url, runtime=runtime)
    except _ClassifiedItemFailure:
        raise
    except PolyHavenAcquireError as exc:
        raise _ClassifiedItemFailure(
            AcquisitionFailure(
                kind=FailureKind.SCHEMA,
                phase="files_api",
                message=str(exc),
            ),
            attempts_in_run=1,
        ) from exc
    try:
        package = parse_polyhaven_model_files(
            model.source_id,
            files_payload,
            resolution=resolution,
        )
    except PolyHavenPathSecurityError as exc:
        raise _ClassifiedItemFailure(
            AcquisitionFailure(
                kind=FailureKind.PATH_SECURITY,
                phase="files_schema",
                message=str(exc),
            ),
            attempts_in_run=1,
        ) from exc
    except PolyHavenAcquireError as exc:
        raise _ClassifiedItemFailure(
            AcquisitionFailure(
                kind=FailureKind.SCHEMA,
                phase="files_schema",
                message=str(exc),
            ),
            attempts_in_run=1,
        ) from exc
    root_dir = _ensure_safe_directory(
        data_dir,
        Path("acquire/polyhaven/models")
        / model.source_id.lower()
        / model.revision
        / package.resolution,
    )
    metadata_path = root_dir / "metadata.json"
    acquired_at, previous_verified_at = _existing_metadata_times(
        metadata_path,
        asset_id=model.asset_id,
        revision=model.revision,
    )
    downloaded_entries: list[_DownloadedFile] = []
    for file_spec in package.files:
        destination = _safe_destination(root_dir, file_spec.relative_path)
        try:
            downloaded_entries.append(
                _acquire_file(
                    file_spec,
                    destination=destination,
                    force=force,
                    asset_id=model.asset_id,
                    runtime=runtime,
                )
            )
        except _ClassifiedItemFailure as exc:
            if exc.failure.category is FailureCategory.PERMANENT:
                runtime.quota.finish_download(
                    _quota_download_key(asset_id=model.asset_id, spec=file_spec)
                )
            raise
        except PolyHavenPathSecurityError as exc:
            runtime.quota.finish_download(
                _quota_download_key(asset_id=model.asset_id, spec=file_spec)
            )
            raise _ClassifiedItemFailure(
                AcquisitionFailure(
                    kind=FailureKind.PATH_SECURITY,
                    phase="download",
                    message=str(exc),
                ),
                attempts_in_run=1,
            ) from exc
        except PolyHavenIntegrityError as exc:
            raise _ClassifiedItemFailure(
                AcquisitionFailure(
                    kind=FailureKind.INTEGRITY,
                    phase="download",
                    message=str(exc),
                ),
                attempts_in_run=1,
            ) from exc
    downloaded = tuple(downloaded_entries)
    try:
        _require_exact_gltf_dependency_closure(root_dir=root_dir, package=package)
    except PolyHavenAcquireError as exc:
        raise _ClassifiedItemFailure(
            AcquisitionFailure(
                kind=FailureKind.SCHEMA,
                phase="gltf_closure",
                message=str(exc),
            ),
            attempts_in_run=1,
        ) from exc
    verified_at = _next_utc_timestamp(previous_verified_at)
    metadata = _metadata_payload(
        model=model,
        package=package,
        files=downloaded,
        project_root=project_root,
        acquired_at=acquired_at or verified_at,
        verified_at=verified_at,
    )
    runtime.check_disk_growth(len(_render_json(metadata)))
    _write_json_atomic(metadata_path, metadata)
    relative_files = tuple(
        sorted(
            (entry.spec.relative_path for entry in downloaded),
            key=lambda path: path.as_posix(),
        )
    )
    source_bundle_hash = bundle_sha256(root_dir, relative_files)
    source_content_hash = content_sha256(root_dir, relative_files)
    state_items = _object(state.get("items"), "Poly Haven state items")
    previous = state_items.get(model.asset_id)
    previous_status = previous.get("status") if isinstance(previous, dict) else None
    item = PolyHavenSyncItem(
        asset_id=model.asset_id,
        source_id=model.source_id,
        revision=model.revision,
        root_dir=root_dir,
        main_path=root_dir / package.main_file,
        dependency_paths=tuple(root_dir / path for path in package.dependencies),
        metadata_path=metadata_path,
        downloaded_files=sum(not entry.reused for entry in downloaded),
        reused_files=sum(entry.reused for entry in downloaded),
        downloaded_bytes=sum(entry.downloaded_bytes for entry in downloaded),
        verified_bytes=sum(entry.spec.bytes for entry in downloaded),
        source_bundle_sha256=source_bundle_hash,
        source_content_sha256=source_content_hash,
        acquired_at=acquired_at or verified_at,
        verified_at=verified_at,
        state_status="downloaded",
    )
    manifest_item = _manifest_item(
        item=item,
        model=model,
        package=package,
        files=downloaded,
        project_root=project_root,
        run_id=run_id,
        state_status_before=(str(previous_status) if previous_status is not None else None),
    )
    return item, manifest_item


def sync_polyhaven_models(
    *,
    settings: Settings,
    limit: int = 1,
    resolution: str = DEFAULT_RESOLUTION,
    force: bool = False,
    runtime_config: PolyHavenRuntimeConfig | None = None,
    retry_revisions: tuple[str, ...] = (),
) -> PolyHavenSyncResult:
    """Discover and prepare a bounded, resumable Poly Haven model ingest batch."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
        raise PolyHavenAcquireError("limit must be an integer between 1 and 10000")
    config = PolyHavenRuntimeConfig() if runtime_config is None else runtime_config
    if not isinstance(config, PolyHavenRuntimeConfig):
        raise PolyHavenAcquireError("runtime_config must be PolyHavenRuntimeConfig")
    checked_resolution = _resolution(resolution)
    if not isinstance(retry_revisions, tuple):
        raise PolyHavenAcquireError("retry_revisions must be an immutable tuple")
    checked_retry_revisions: list[str] = []
    for asset_id in retry_revisions:
        try:
            checked_retry_revisions.append(validate_asset_id(asset_id))
        except (TypeError, ValueError) as exc:
            raise PolyHavenAcquireError("retry_revisions contains an invalid asset id") from exc
    if len(checked_retry_revisions) != len(set(checked_retry_revisions)):
        raise PolyHavenAcquireError("retry_revisions contains duplicate asset ids")
    checked_retry_revisions.sort()
    project_root = settings.project_root.expanduser().resolve()
    data_dir = settings.data_dir.expanduser().resolve()
    run_id = f"{utc_timestamp()}_{uuid4().hex[:8]}"
    run_dir = project_root / "out/acquire/polyhaven" / run_id
    manifest_path = run_dir / "manifest.json"
    generated_spec_path = run_dir / "generated_ingest.yaml"
    state_path = data_dir / "acquire/polyhaven/state.json"
    intent_path = data_dir / "acquire/polyhaven/commit_intent.json"
    failure_journal_path = data_dir / "acquire/polyhaven/failure_journal.json"
    started_at = _utc_now()
    running_manifest: dict[str, Any] = {
        "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
        "source": POLYHAVEN_SOURCE,
        "asset_type": "models",
        "run_id": run_id,
        "status": "running",
        "started_at": started_at,
        "request": {
            "force": force,
            "limit": limit,
            "resolution": checked_resolution,
            "runtime": config.as_dict(),
            "retry_revisions": checked_retry_revisions,
        },
        "active_attempt": None,
        "journal_event_refs": [],
    }
    runtime: _AcquisitionRuntime | None = None
    try:
        _require_data_dir_inside_project(project_root=project_root, data_dir=data_dir)
        with _source_lock(data_dir):
            output_root = _ensure_safe_directory(
                project_root,
                Path("out/acquire/polyhaven"),
            )
            run_dir = output_root / run_id
            manifest_path = run_dir / "manifest.json"
            generated_spec_path = run_dir / "generated_ingest.yaml"
            run_dir.mkdir(exist_ok=False)
            _write_json_atomic(manifest_path, running_manifest)
            _ensure_safe_directory(data_dir, Path("acquire/polyhaven"))
            _reconcile_commit_intent(
                intent_path=intent_path,
                state_path=state_path,
                project_root=project_root,
            )
            loaded_journal = _load_failure_journal(failure_journal_path)
            if not loaded_journal.before_exists:
                _persist_failure_journal(failure_journal_path, loaded_journal.payload)
                loaded_journal = _load_failure_journal(failure_journal_path)
            runtime = _AcquisitionRuntime(
                config=config,
                clock=SystemClock(),
                project_root=project_root,
                data_dir=data_dir,
            )
            _reconcile_stale_running_manifests(
                project_root=project_root,
                exclude=manifest_path,
                journal_path=failure_journal_path,
                journal_payload=loaded_journal.payload,
                policy=config.failure_policy,
                clock=runtime.clock,
            )
            loaded_journal = _load_failure_journal(failure_journal_path)
            journal_payload = loaded_journal.payload
            active_failures = dict(loaded_journal.active)
            journal_event_refs: list[dict[str, Any]] = []
            released_events: list[dict[str, Any]] = []
            release_targets: list[ActiveFailure] = []
            for asset_id in checked_retry_revisions:
                active = active_failures.get(asset_id)
                if active is None:
                    raise PolyHavenAcquireError(
                        f"retry revision {asset_id!r} has no active failure"
                    )
                release_targets.append(active)
            for ordinal, active in enumerate(release_targets, start=1):
                try:
                    journal_payload, release_event = append_release_event(
                        journal_payload,
                        source=POLYHAVEN_SOURCE,
                        asset_type="models",
                        asset_id=active.asset_id,
                        source_id=active.source_id,
                        revision=active.revision,
                        resolution=active.resolution,
                        run_id=run_id,
                        attempt_id=f"{run_id}:release:{ordinal}",
                        recorded_at=_next_failure_journal_datetime(
                            journal_payload,
                            now=_clock_utc_now(runtime.clock),
                        ),
                        reason="operator_requested_exact_revision_retry",
                    )
                except FailureJournalError as exc:
                    raise PolyHavenAcquireError(
                        f"cannot release Poly Haven revision {active.asset_id!r}: {exc}"
                    ) from exc
                released_events.append(release_event)
                journal_event_refs.append(_failure_event_ref(release_event))
            if released_events:
                _persist_failure_journal(failure_journal_path, journal_payload)
                running_manifest["journal_event_refs"] = journal_event_refs
                _write_json_atomic(manifest_path, running_manifest)
                active_failures = dict(
                    validate_failure_journal(
                        journal_payload,
                        source=POLYHAVEN_SOURCE,
                        asset_type="models",
                    )
                )
            loaded_state = _load_state(state_path, project_root=project_root)
            state = loaded_state.payload
            listing_payload = _fetch_json(POLYHAVEN_MODELS_URL, runtime=runtime)
            models = parse_polyhaven_model_listing(listing_payload)
            snapshot_sha256 = _listing_sha256(models)
            selection_now = _clock_utc_now(runtime.clock)
            ineligible_unseen_ids = frozenset(
                asset_id
                for asset_id, failure in active_failures.items()
                if not failure.eligible(now=selection_now)
            )
            allowed_unseen_ids = runtime.quota.allowed_unseen_ids(
                models,
                state=state,
                ineligible_unseen_ids=ineligible_unseen_ids,
            )
            selected, next_selection_class = _select_models(
                models,
                state=state,
                limit=limit,
                allowed_unseen_ids=allowed_unseen_ids,
                active_failures=active_failures,
                now=selection_now,
            )
            state_items = _object(state.get("items"), "Poly Haven state items")
            unseen_selected = tuple(
                model.asset_id for model in selected if model.asset_id not in state_items
            )
            runtime.quota.reserve_items(unseen_selected)
            if allowed_unseen_ids is not None:
                runtime.stats.deferred_new_items = sum(
                    model.asset_id not in state_items
                    and model.asset_id not in allowed_unseen_ids
                    and model.asset_id not in ineligible_unseen_ids
                    for model in models
                )
            sync_items: list[PolyHavenSyncItem] = []
            manifest_items: list[dict[str, Any]] = []
            failure_events: list[dict[str, Any]] = []
            for ordinal, model in enumerate(selected, start=1):
                attempt_id = f"{run_id}:{ordinal}"
                running_manifest["active_attempt"] = {
                    "attempt_id": attempt_id,
                    "ordinal": ordinal,
                    "asset_id": model.asset_id,
                    "source_id": model.source_id,
                    "revision": model.revision,
                    "resolution": checked_resolution,
                    "started_at": _clock_utc_now(runtime.clock).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                _write_json_atomic(manifest_path, running_manifest)
                try:
                    item, manifest_item = _sync_model_revision(
                        model=model,
                        resolution=checked_resolution,
                        force=force,
                        data_dir=data_dir,
                        project_root=project_root,
                        runtime=runtime,
                        state=state,
                        run_id=run_id,
                    )
                except _ClassifiedItemFailure as exc:
                    try:
                        journal_payload, event = append_failure_event(
                            journal_payload,
                            source=POLYHAVEN_SOURCE,
                            asset_type="models",
                            asset_id=model.asset_id,
                            source_id=model.source_id,
                            revision=model.revision,
                            resolution=checked_resolution,
                            run_id=run_id,
                            attempt_id=attempt_id,
                            failure=exc.failure,
                            recorded_at=_next_failure_journal_datetime(
                                journal_payload,
                                now=_clock_utc_now(runtime.clock),
                            ),
                            policy=config.failure_policy,
                            retry_after_deadline=exc.retry_after_deadline,
                            attempts_in_run=exc.attempts_in_run,
                        )
                    except FailureJournalError as journal_exc:
                        raise PolyHavenAcquireError(
                            f"cannot journal Poly Haven revision failure: {journal_exc}"
                        ) from journal_exc
                    _persist_failure_journal(failure_journal_path, journal_payload)
                    failure_events.append(event)
                    event_ref = _failure_event_ref(event)
                    journal_event_refs.append(event_ref)
                    running_manifest["journal_event_refs"] = journal_event_refs
                    running_manifest["active_attempt"] = None
                    _write_json_atomic(manifest_path, running_manifest)
                    active_failures = dict(
                        validate_failure_journal(
                            journal_payload,
                            source=POLYHAVEN_SOURCE,
                            asset_type="models",
                        )
                    )
                    continue
                active_before_success = validate_failure_journal(
                    journal_payload,
                    source=POLYHAVEN_SOURCE,
                    asset_type="models",
                ).get(model.asset_id)
                resolution_event: dict[str, Any] | None = None
                if active_before_success is None or active_before_success.failure.kind not in {
                    FailureKind.DOWNSTREAM,
                    FailureKind.QUALITY,
                }:
                    try:
                        journal_payload, resolution_event = append_resolution_event(
                            journal_payload,
                            source=POLYHAVEN_SOURCE,
                            asset_type="models",
                            asset_id=model.asset_id,
                            source_id=model.source_id,
                            revision=model.revision,
                            resolution=checked_resolution,
                            run_id=run_id,
                            attempt_id=attempt_id,
                            recorded_at=_next_failure_journal_datetime(
                                journal_payload,
                                now=_clock_utc_now(runtime.clock),
                            ),
                        )
                    except FailureJournalError as journal_exc:
                        raise PolyHavenAcquireError(
                            f"cannot resolve Poly Haven revision failure: {journal_exc}"
                        ) from journal_exc
                if resolution_event is not None:
                    _persist_failure_journal(failure_journal_path, journal_payload)
                    journal_event_refs.append(_failure_event_ref(resolution_event))
                    running_manifest["journal_event_refs"] = journal_event_refs
                running_manifest["active_attempt"] = None
                _write_json_atomic(manifest_path, running_manifest)
                sync_items.append(item)
                manifest_items.append(manifest_item)

            resolved_spec_path: Path | None = None
            generated_spec_evidence: dict[str, Any] | None = None
            if sync_items:
                _write_ingest_spec(
                    generated_spec_path,
                    models={item.asset_id: item for item in selected},
                    sync_items=tuple(sync_items),
                )
                load_ingest_spec(generated_spec_path)
                resolved_spec_path = generated_spec_path
                generated_spec_evidence = {
                    "path": _portable_path(generated_spec_path, project_root),
                    "file_sha256": _sha256_file(generated_spec_path),
                }
            completed_at = _next_utc_timestamp(
                state.get("updated_at"),
                *(item.verified_at for item in sync_items),
            )
            new_state = _state_after_sync(
                state,
                listing_models=models,
                selected_models=selected,
                items=tuple(sync_items),
                snapshot_sha256=snapshot_sha256,
                run_id=run_id,
                completed_at=completed_at,
                project_root=project_root,
                next_selection_class=next_selection_class,
                migrated_from=loaded_state.migrated_from,
            )
            downloaded_files = sum(item.downloaded_files for item in sync_items)
            reused_files = sum(item.reused_files for item in sync_items)
            downloaded_bytes = sum(item.downloaded_bytes for item in sync_items)
            verified_bytes = sum(item.verified_bytes for item in sync_items)
            deferred_failures = sum(
                event.get("failure", {}).get("category") == FailureCategory.DEFERRED.value
                for event in failure_events
            )
            failed_revisions = len(failure_events) - deferred_failures
            quarantined_revisions = sum(
                event.get("disposition") == FailureDisposition.QUARANTINED.value
                for event in failure_events
            )
            released_revisions = len(released_events)
            run_status = (
                "prepared"
                if sync_items
                else "deferred"
                if failure_events and deferred_failures == len(failure_events)
                else "journaled"
                if failure_events
                else "released"
                if released_events
                else "noop"
            )
            failure_journal_receipt = {
                "path": _portable_path(failure_journal_path, project_root),
                "before": {
                    "exists": loaded_journal.before_exists,
                    "file_sha256": loaded_journal.before_file_sha256,
                    "payload_sha256": loaded_journal.before_payload_sha256,
                    "event_count": len(loaded_journal.payload["events"]),
                    "head_event_sha256": loaded_journal.payload["head_event_sha256"],
                },
                "after": {
                    "exists": True,
                    "file_sha256": _sha256_file(failure_journal_path),
                    "payload_sha256": _payload_sha256(journal_payload),
                    "event_count": len(journal_payload["events"]),
                    "head_event_sha256": journal_payload["head_event_sha256"],
                },
                "event_refs": journal_event_refs,
            }
            prepared_manifest: dict[str, Any] = {
                "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
                "source": POLYHAVEN_SOURCE,
                "asset_type": "models",
                "run_id": run_id,
                "status": run_status,
                "started_at": started_at,
                "completed_at": completed_at,
                "request": {
                    "force": force,
                    "limit": limit,
                    "resolution": checked_resolution,
                    "runtime": config.as_dict(),
                    "retry_revisions": checked_retry_revisions,
                },
                "listing": {
                    "url": POLYHAVEN_MODELS_URL,
                    "discovered": len(models),
                    "payload_sha256": snapshot_sha256,
                    "watermark": _listing_watermark(models),
                },
                "state": {
                    "path": _portable_path(state_path, project_root),
                    "before": {
                        "exists": loaded_state.before_exists,
                        "file_sha256": loaded_state.before_file_sha256,
                        "payload_sha256": loaded_state.before_payload_sha256,
                    },
                    "after": {
                        "transition_sha256": _state_transition_sha256(
                            new_state,
                            run_id=run_id,
                        ),
                    },
                    "migrated_from": loaded_state.migrated_from,
                },
                "generated_ingest_spec": generated_spec_evidence,
                "counts": {
                    "attempted": len(selected),
                    "selected": len(sync_items),
                    "failed": failed_revisions,
                    "deferred": deferred_failures,
                    "quarantined": quarantined_revisions,
                    "released": released_revisions,
                    "transport_body_bytes": runtime.stats.download_body_bytes,
                    "downloaded_files": downloaded_files,
                    "reused_files": reused_files,
                    "downloaded_bytes": downloaded_bytes,
                    "verified_bytes": verified_bytes,
                },
                "runtime": runtime.evidence(),
                "failure_journal": failure_journal_receipt,
                "failures": failure_events,
                "items": manifest_items,
            }
            if not sync_items:
                # The no-work receipt advances the durable state timestamp.  Do that
                # before hashing state.after; the receipt anchor itself is excluded
                # by _state_transition_sha256 to avoid a digest cycle.
                new_state["updated_at"] = completed_at
                prepared_manifest["state"]["after"] = {
                    "transition_sha256": _state_transition_sha256(
                        new_state,
                        run_id=run_id,
                    ),
                }
            prepare_receipt_sha256 = _prepared_manifest_payload_sha256(prepared_manifest)
            prepared_manifest["prepare_receipt_sha256"] = prepare_receipt_sha256
            for item in sync_items:
                new_state["items"][item.asset_id]["prepared_manifest_payload_sha256"] = (
                    prepare_receipt_sha256
                )
            if not sync_items:
                noop_receipts = _object(
                    new_state.setdefault("noop_run_receipts", {}),
                    "Poly Haven state noop_run_receipts",
                )
                noop_receipts[run_id] = prepare_receipt_sha256
            prepared_manifest["state"]["after"] = {
                "transition_sha256": _state_transition_sha256(
                    new_state,
                    run_id=run_id,
                ),
            }
            _validate_prepared_manifest_receipt(
                manifest=prepared_manifest,
                state=new_state,
                project_root=project_root,
            )
            _assert_prepared_inputs_unchanged(
                project_root=project_root,
                manifest_path=manifest_path,
                expected_running_manifest=running_manifest,
                generated_spec_path=resolved_spec_path,
                generated_spec_evidence=generated_spec_evidence,
                items=tuple(sync_items),
            )
            _commit_state_and_manifest(
                intent_path=intent_path,
                state_path=state_path,
                loaded_state=loaded_state,
                new_state=new_state,
                manifest_path=manifest_path,
                expected_manifest_file_sha256=_sha256_file(manifest_path),
                new_manifest=prepared_manifest,
                operation="sync",
                project_root=project_root,
            )
        return PolyHavenSyncResult(
            run_dir=run_dir,
            manifest_path=manifest_path,
            state_path=state_path,
            generated_spec_path=resolved_spec_path,
            items=tuple(sync_items),
            discovered=len(models),
            selected=len(sync_items),
            downloaded_files=downloaded_files,
            reused_files=reused_files,
            downloaded_bytes=downloaded_bytes,
            verified_bytes=verified_bytes,
            snapshot_sha256=snapshot_sha256,
            runtime_evidence=runtime.evidence(),
            attempted=len(selected),
            failed=failed_revisions,
            deferred=deferred_failures,
            quarantined=quarantined_revisions,
            released=released_revisions,
            failure_journal_path=failure_journal_path,
        )
    except BaseException as exc:
        _persist_run_failure(
            manifest_path=manifest_path,
            intent_path=intent_path,
            error=exc,
            runtime_evidence=runtime.evidence() if runtime is not None else None,
        )
        if isinstance(exc, PolyHavenAcquireError):
            raise
        if isinstance(exc, Exception):
            raise PolyHavenAcquireError(f"Poly Haven model sync failed: {exc}") from exc
        raise


def finalize_polyhaven_items(
    *,
    result: PolyHavenSyncResult,
    batch_manifest_path: Path,
) -> dict[str, TerminalStatus]:
    """Derive immutable terminal outcomes from strict downstream batch evidence."""

    if not result.items or result.generated_spec_path is None:
        raise PolyHavenAcquireError("a no-change Poly Haven sync result cannot be finalized")
    unresolved_run_dir = result.run_dir.expanduser().absolute()
    project_root = unresolved_run_dir.parents[3].resolve()
    _reject_symlink_components(
        unresolved_run_dir,
        project_root=project_root,
        context="Poly Haven finalize run directory",
    )
    unresolved_manifest_path = result.manifest_path.expanduser().absolute()
    expected_manifest_path = unresolved_run_dir / "manifest.json"
    if unresolved_manifest_path != expected_manifest_path:
        raise PolyHavenAcquireError("Poly Haven finalize manifest path is not the run manifest")
    _reject_symlink_components(
        unresolved_manifest_path,
        project_root=project_root,
        context="Poly Haven finalize manifest",
    )
    _require_regular_file(unresolved_manifest_path, "Poly Haven finalize manifest")
    unresolved_state_path = result.state_path.expanduser().absolute()
    _reject_symlink_components(
        unresolved_state_path,
        project_root=project_root,
        context="Poly Haven finalize state",
    )
    state_path = unresolved_state_path.resolve()
    data_dir = state_path.parents[2]
    _require_data_dir_inside_project(project_root=project_root, data_dir=data_dir)
    intent_path = data_dir / "acquire/polyhaven/commit_intent.json"
    with (
        _source_lock(data_dir),
        _asset_locks(
            data_dir=data_dir,
            asset_ids=tuple(item.asset_id for item in result.items),
        ),
    ):
        _reconcile_commit_intent(
            intent_path=intent_path,
            state_path=state_path,
            project_root=project_root,
        )
        loaded_state = _load_state(state_path, project_root=project_root)
        state = loaded_state.payload
        manifest = _read_json_object_strict(result.manifest_path, "Poly Haven run manifest")
        if (
            manifest.get("schema_version") not in _SUPPORTED_RUN_MANIFEST_SCHEMA_VERSIONS
            or manifest.get("source") != POLYHAVEN_SOURCE
            or manifest.get("asset_type") != "models"
            or manifest.get("run_id") != result.run_dir.name
        ):
            raise PolyHavenAcquireError("Poly Haven run manifest identity is invalid")
        checked_batch_path = _checked_project_file(
            batch_manifest_path,
            project_root=project_root,
            context="downstream batch manifest",
        )
        batch_file_sha256 = _sha256_file(checked_batch_path)
        if manifest.get("status") == "finalized":
            finalization, finalized_statuses, terminal_evidence, nonterminal_asset_ids = (
                _validated_finalization_payload(
                    manifest=manifest,
                    project_root=project_root,
                    expected_asset_ids={item.asset_id for item in result.items},
                )
            )
            if (
                finalization.get("batch_manifest")
                != _portable_path(checked_batch_path, project_root)
                or finalization.get("batch_manifest_file_sha256") != batch_file_sha256
            ):
                raise PolyHavenAcquireError(
                    "Poly Haven run was already finalized from different downstream evidence"
                )
            _assert_finalization_inputs_unchanged(
                result=result,
                run_manifest=manifest,
                state=state,
                project_root=project_root,
            )
            _assert_finalized_state_binding(
                manifest=manifest,
                state=state,
                finalization=finalization,
                statuses=finalized_statuses,
                terminal_evidence=terminal_evidence,
                nonterminal_asset_ids=nonterminal_asset_ids,
            )
            for asset_id in finalized_statuses:
                state_item = state["items"].get(asset_id)
                if not isinstance(state_item, dict):
                    raise PolyHavenAcquireError(
                        f"finalized Poly Haven state lost item {asset_id!r}"
                    )
                _revalidate_terminal_receipt(
                    asset_id=asset_id,
                    item=state_item,
                    project_root=project_root,
                    data_dir=data_dir,
                )
            return finalized_statuses
        if manifest.get("status") != "prepared":
            raise PolyHavenAcquireError("only a prepared Poly Haven run can be finalized")

        _assert_finalization_inputs_unchanged(
            result=result,
            run_manifest=manifest,
            state=state,
            project_root=project_root,
        )
        batch = _read_json_object_strict(checked_batch_path, "downstream batch manifest")
        terminal_evidence = _derive_terminal_statuses(
            result=result,
            run_manifest=manifest,
            batch=batch,
            batch_manifest_path=checked_batch_path,
            project_root=project_root,
        )
        downstream_journal_receipt: dict[str, Any] | None = None
        downstream_failure_events: list[dict[str, Any]] = []
        if manifest.get("schema_version") == RUN_MANIFEST_SCHEMA_VERSION:
            request = _object(manifest.get("request"), "run manifest request")
            runtime_config = _validate_runtime_config_payload(request.get("runtime"))
            raw_manifest_items = manifest.get("items")
            if not isinstance(raw_manifest_items, list):
                raise PolyHavenAcquireError("Poly Haven run manifest items must be a list")
            result_items_by_id = {item.asset_id: item for item in result.items}
            ordered_items = tuple(
                result_items_by_id[
                    _string(
                        _object(raw_item, "run manifest item").get("asset_id"),
                        "run manifest asset_id",
                        max_length=64,
                    )
                ]
                for raw_item in raw_manifest_items
            )
            downstream_journal_receipt, downstream_failure_events = _journal_downstream_outcomes(
                items=ordered_items,
                batch=batch,
                batch_file_sha256=batch_file_sha256,
                project_root=project_root,
                data_dir=data_dir,
                run_id=result.run_dir.name,
                resolution=_resolution(request.get("resolution")),
                policy=runtime_config.failure_policy,
            )
        statuses: dict[str, TerminalStatus] = {
            asset_id: evidence["status"] for asset_id, evidence in terminal_evidence.items()
        }
        finalized_at = _next_utc_timestamp(
            state.get("updated_at"),
            manifest.get("completed_at"),
        )
        new_state = json.loads(json.dumps(state))
        for asset_id, status in statuses.items():
            entry = new_state["items"][asset_id]
            current_status = entry["status"]
            if current_status in _TERMINAL_STATUSES and current_status != status:
                raise PolyHavenAcquireError(
                    f"state terminal status conflict for {asset_id!r}: "
                    f"current={current_status} derived={status}"
                )
            if current_status == status:
                continue
            if current_status != "downloaded":
                raise PolyHavenAcquireError(f"state item {asset_id!r} is not finalizable")
            entry["status"] = status
            entry["terminal"] = {
                "status": status,
                "batch_manifest": _portable_path(checked_batch_path, project_root),
                "batch_manifest_file_sha256": batch_file_sha256,
                "committed_at": finalized_at,
                "terminal_evidence_sha256": terminal_evidence[asset_id]["terminal_evidence_sha256"],
                "receipt": terminal_evidence[asset_id]["receipt"],
            }
        if statuses:
            new_state["updated_at"] = finalized_at
        new_manifest = json.loads(json.dumps(manifest))
        new_manifest["status"] = "finalized"
        new_manifest["finalized_at"] = finalized_at
        finalization_payload: dict[str, Any] = {
            "batch_manifest": _portable_path(checked_batch_path, project_root),
            "batch_manifest_file_sha256": batch_file_sha256,
            "terminal_statuses": dict(sorted(statuses.items())),
            "terminal_evidence": {
                asset_id: {
                    "terminal_evidence_sha256": evidence["terminal_evidence_sha256"],
                    "receipt": evidence["receipt"],
                }
                for asset_id, evidence in sorted(terminal_evidence.items())
            },
            "nonterminal_asset_ids": sorted(
                item.asset_id for item in result.items if item.asset_id not in statuses
            ),
        }
        if downstream_journal_receipt is not None:
            finalization_payload["failure_journal"] = downstream_journal_receipt
            finalization_payload["failures"] = downstream_failure_events
        new_manifest["finalization"] = finalization_payload
        state_payload = new_manifest.get("state")
        if not isinstance(state_payload, dict):
            raise PolyHavenAcquireError("Poly Haven run manifest state must be an object")
        state_payload["after_finalization"] = (
            {
                "transition_sha256": _state_transition_sha256(
                    new_state,
                    run_id=result.run_dir.name,
                )
            }
            if manifest.get("schema_version") == RUN_MANIFEST_SCHEMA_VERSION
            else {
                "file_sha256": _json_file_sha256(new_state),
                "payload_sha256": _payload_sha256(new_state),
            }
        )
        if manifest.get("schema_version") == RUN_MANIFEST_SCHEMA_VERSION:
            finalization_receipts = _object(
                new_state.setdefault("finalization_run_receipts", {}),
                "Poly Haven state finalization_run_receipts",
            )
            finalization_receipts[result.run_dir.name] = _finalized_manifest_payload_sha256(
                new_manifest
            )
            # The current run's finalization anchor is excluded from the state
            # transition projection, so storing it cannot create a digest cycle.
            state_payload["after_finalization"] = {
                "transition_sha256": _state_transition_sha256(
                    new_state,
                    run_id=result.run_dir.name,
                )
            }
            anchored_receipt = _finalized_manifest_payload_sha256(new_manifest)
            if finalization_receipts[result.run_dir.name] != anchored_receipt:
                raise PolyHavenAcquireError("Poly Haven finalization receipt is self-inconsistent")
        _commit_state_and_manifest(
            intent_path=intent_path,
            state_path=state_path,
            loaded_state=loaded_state,
            new_state=new_state,
            manifest_path=result.manifest_path,
            expected_manifest_file_sha256=_sha256_file(result.manifest_path),
            new_manifest=new_manifest,
            operation="finalize",
            project_root=project_root,
        )
    return statuses


def _select_models(
    models: tuple[PolyHavenModel, ...],
    *,
    state: dict[str, Any],
    limit: int,
    allowed_unseen_ids: frozenset[str] | None = None,
    active_failures: Mapping[str, ActiveFailure] | None = None,
    now: datetime | None = None,
) -> tuple[tuple[PolyHavenModel, ...], str]:
    state_items = state["items"]
    checked_failures = {} if active_failures is None else active_failures
    if checked_failures and now is None:
        raise PolyHavenAcquireError("failure-aware selection requires the current UTC time")
    pending: list[PolyHavenModel] = []
    unseen: list[PolyHavenModel] = []
    for model in models:
        entry = state_items.get(model.asset_id)
        if entry is not None and entry.get("status") in _TERMINAL_STATUSES:
            # Terminal catalog state dominates stale or independently appended
            # failure history; a completed immutable revision is never reacquired.
            continue
        failure = checked_failures.get(model.asset_id)
        if failure is not None:
            if now is None or not failure.eligible(now=now):
                continue
            if entry is None and (
                allowed_unseen_ids is not None and model.asset_id not in allowed_unseen_ids
            ):
                continue
            pending.append(model)
            continue
        if entry is None:
            if allowed_unseen_ids is None or model.asset_id in allowed_unseen_ids:
                unseen.append(model)
        elif entry["status"] == "downloaded":
            pending.append(model)

    def pending_key(model: PolyHavenModel) -> tuple[str, int, str]:
        failure = checked_failures.get(model.asset_id)
        deadline = failure.next_eligible_at if failure is not None else None
        scheduled_at = (
            deadline.isoformat()
            if deadline is not None
            else str(state_items[model.asset_id].get("last_prepared_at") or "")
        )
        return (
            scheduled_at,
            model.date_published,
            model.source_id.casefold(),
        )

    pending.sort(key=pending_key)
    queues = {"unseen": unseen, "pending": pending}
    positions = {"unseen": 0, "pending": 0}
    turn = str(state["next_selection_class"])
    selected: list[PolyHavenModel] = []
    while len(selected) < limit:
        other = "pending" if turn == "unseen" else "unseen"
        chosen = turn if positions[turn] < len(queues[turn]) else other
        if positions[chosen] >= len(queues[chosen]):
            break
        selected.append(queues[chosen][positions[chosen]])
        positions[chosen] += 1
        turn = "pending" if chosen == "unseen" else "unseen"
    return tuple(selected), turn


def _state_after_sync(
    state: dict[str, Any],
    *,
    listing_models: tuple[PolyHavenModel, ...],
    selected_models: tuple[PolyHavenModel, ...],
    items: tuple[PolyHavenSyncItem, ...],
    snapshot_sha256: str,
    run_id: str,
    completed_at: str,
    project_root: Path,
    next_selection_class: str,
    migrated_from: int | None,
) -> dict[str, Any]:
    result = json.loads(json.dumps(state))
    result.setdefault("noop_run_receipts", {})
    result.setdefault("finalization_run_receipts", {})
    watermark = _listing_watermark(listing_models)
    previous_listing = state.get("last_listing")
    if (
        not items
        and isinstance(previous_listing, dict)
        and previous_listing.get("payload_sha256") == snapshot_sha256
        and previous_listing.get("count") == len(listing_models)
        and previous_listing.get("watermark") == watermark
        and state.get("next_selection_class") == next_selection_class
    ):
        return result
    result["updated_at"] = completed_at
    result["migrated_from"] = migrated_from or result.get("migrated_from")
    result["next_selection_class"] = next_selection_class
    result["last_listing"] = {
        "payload_sha256": snapshot_sha256,
        "observed_at": completed_at,
        "count": len(listing_models),
        "watermark": watermark,
    }
    by_asset_id = {item.asset_id: item for item in selected_models}
    for item in items:
        model = by_asset_id[item.asset_id]
        metadata = _read_json_object_strict(item.metadata_path, "Poly Haven metadata")
        raw_files = metadata.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise PolyHavenAcquireError("Poly Haven metadata has no file evidence")
        metadata_file_sha256 = _sha256_file(item.metadata_path)
        prepare_token = _prepare_token(
            asset_id=item.asset_id,
            source_id=item.source_id,
            revision=item.revision,
            metadata_file_sha256=metadata_file_sha256,
            source_bundle_sha256=item.source_bundle_sha256,
            source_content_sha256=item.source_content_sha256,
            run_id=run_id,
        )
        result["items"][item.asset_id] = {
            "asset_id": item.asset_id,
            "source_id": item.source_id,
            "revision": item.revision,
            "date_published": model.date_published,
            "status": "downloaded",
            "root_dir": _portable_path(item.root_dir, project_root),
            "main_path": _portable_path(item.main_path, project_root),
            "metadata_path": _portable_path(item.metadata_path, project_root),
            "metadata_file_sha256": metadata_file_sha256,
            "source_bundle_sha256": item.source_bundle_sha256,
            "source_content_sha256": item.source_content_sha256,
            "files": [
                {
                    "relative_path": raw["relative_path"],
                    "bytes": raw["bytes"],
                    "md5": raw["md5"],
                    "sha256": raw["sha256"],
                }
                for raw in raw_files
                if isinstance(raw, dict)
            ],
            "acquired_at": item.acquired_at,
            "verified_at": item.verified_at,
            "last_prepared_at": completed_at,
            "last_run_id": run_id,
            "prepare_token": prepare_token,
            "prepared_manifest_payload_sha256": None,
            "migration_pending": False,
            "terminal": None,
        }
    return result


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "source": POLYHAVEN_SOURCE,
        "asset_type": "models",
        "migrated_from": None,
        "updated_at": None,
        "next_selection_class": "unseen",
        "last_listing": None,
        "noop_run_receipts": {},
        "finalization_run_receipts": {},
        "items": {},
    }


def _upgrade_v2_state_shape(state: dict[str, Any]) -> dict[str, Any]:
    """Add the prepared-manifest anchor to state written by early v2 clients."""

    upgraded = json.loads(json.dumps(state))
    items = upgraded.get("items")
    if isinstance(items, dict):
        for raw_item in items.values():
            if isinstance(raw_item, dict):
                raw_item.setdefault("prepared_manifest_payload_sha256", None)
    return upgraded


def _load_state(path: Path, *, project_root: Path) -> _LoadedState:
    if not path.exists() and not path.is_symlink():
        return _LoadedState(
            payload=_empty_state(),
            before_exists=False,
            before_file_sha256=None,
            before_payload_sha256=None,
            migrated_from=None,
        )
    if path.is_symlink() or not path.is_file():
        raise PolyHavenAcquireError(f"Poly Haven state is not a regular file: {path}")
    raw_state = _read_json_object_strict(path, "Poly Haven state")
    file_sha256 = _sha256_file(path)
    payload_sha256 = _payload_sha256(raw_state)
    version = raw_state.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise PolyHavenAcquireError("Poly Haven state schema version is not canonical")
    migrated_from: int | None = None
    if version == 1:
        state = _migrate_v1_state(raw_state, project_root=project_root)
        migrated_from = 1
    elif version == STATE_SCHEMA_VERSION:
        state = _upgrade_v2_state_shape(raw_state)
    else:
        raise PolyHavenAcquireError("Poly Haven state schema version is unsupported")
    _validate_v2_state(state, project_root=project_root)
    return _LoadedState(
        payload=state,
        before_exists=True,
        before_file_sha256=file_sha256,
        before_payload_sha256=payload_sha256,
        migrated_from=migrated_from,
    )


def _load_failure_journal(path: Path) -> _LoadedFailureJournal:
    if not path.exists() and not path.is_symlink():
        payload = empty_failure_journal(source=POLYHAVEN_SOURCE, asset_type="models")
        return _LoadedFailureJournal(
            payload=payload,
            active={},
            before_exists=False,
            before_file_sha256=None,
            before_payload_sha256=None,
        )
    _require_regular_file(path, "Poly Haven failure journal")
    payload = _read_json_object_strict(path, "Poly Haven failure journal")
    try:
        active = validate_failure_journal(
            payload,
            source=POLYHAVEN_SOURCE,
            asset_type="models",
        )
    except FailureJournalError as exc:
        raise PolyHavenAcquireError(f"Poly Haven failure journal is invalid: {exc}") from exc
    return _LoadedFailureJournal(
        payload=payload,
        active=active,
        before_exists=True,
        before_file_sha256=_sha256_file(path),
        before_payload_sha256=_payload_sha256(payload),
    )


def _persist_failure_journal(path: Path, payload: dict[str, Any]) -> None:
    try:
        validate_failure_journal(
            payload,
            source=POLYHAVEN_SOURCE,
            asset_type="models",
        )
    except FailureJournalError as exc:
        raise PolyHavenAcquireError(f"Poly Haven failure journal is invalid: {exc}") from exc
    _write_json_atomic(path, payload)


def _failure_journal_head(payload: Mapping[str, Any]) -> dict[str, Any]:
    events = payload.get("events")
    if not isinstance(events, list):
        raise PolyHavenAcquireError("Poly Haven failure journal events are invalid")
    return {
        "event_count": len(events),
        "head_event_sha256": payload.get("head_event_sha256"),
        "payload_sha256": _payload_sha256(payload),
    }


def _next_failure_journal_datetime(
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> datetime:
    checked_now = datetime.now(UTC) if now is None else now
    if checked_now.tzinfo is None or checked_now.utcoffset() is None:
        raise PolyHavenAcquireError("failure journal clock must be timezone-aware")
    checked_now = checked_now.astimezone(UTC)
    if checked_now.microsecond:
        checked_now = checked_now.replace(microsecond=0) + timedelta(seconds=1)
    updated_at = payload.get("updated_at")
    if updated_at is None:
        return checked_now
    checked = datetime.strptime(
        _timestamp(updated_at, "failure journal updated_at"),
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=UTC)
    return max(checked_now, checked + timedelta(seconds=1))


def _failure_event_ref(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sequence": _positive_int(event.get("sequence"), "failure event sequence"),
        "event_id": _sha256_value(event.get("event_id"), "failure event id"),
        "event_sha256": _sha256_value(
            event.get("event_sha256"),
            "failure event payload hash",
        ),
        "type": _string(event.get("type"), "failure event type", max_length=32),
    }


def _validate_v2_state(state: dict[str, Any], *, project_root: Path) -> None:
    expected_keys = {
        "schema_version",
        "source",
        "asset_type",
        "migrated_from",
        "updated_at",
        "next_selection_class",
        "last_listing",
        "items",
    }
    optional_keys = {"noop_run_receipts", "finalization_run_receipts"}
    if not expected_keys.issubset(state) or set(state) - expected_keys - optional_keys:
        raise PolyHavenAcquireError("Poly Haven state has an unsupported shape")
    schema_version = state.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != STATE_SCHEMA_VERSION
    ):
        raise PolyHavenAcquireError("Poly Haven state schema version is invalid")
    if state.get("source") != POLYHAVEN_SOURCE or state.get("asset_type") != "models":
        raise PolyHavenAcquireError("Poly Haven state source identity is invalid")
    if state.get("migrated_from") not in {None, 1}:
        raise PolyHavenAcquireError("Poly Haven state migrated_from is invalid")
    updated_at = state.get("updated_at")
    if updated_at is not None:
        _timestamp(updated_at, "state.updated_at")
    if state.get("next_selection_class") not in _SELECTION_CLASSES:
        raise PolyHavenAcquireError("Poly Haven state selection class is invalid")
    _validate_last_listing(state.get("last_listing"))
    raw_noop_receipts = state.get("noop_run_receipts", {})
    if not isinstance(raw_noop_receipts, dict):
        raise PolyHavenAcquireError("Poly Haven state noop_run_receipts must be an object")
    for run_id, receipt in raw_noop_receipts.items():
        _string(run_id, "state noop run_id", max_length=128)
        _sha256_value(receipt, f"state noop receipt {run_id}")
    raw_finalization_receipts = state.get("finalization_run_receipts", {})
    if not isinstance(raw_finalization_receipts, dict):
        raise PolyHavenAcquireError("Poly Haven state finalization_run_receipts must be an object")
    for run_id, receipt in raw_finalization_receipts.items():
        _string(run_id, "state finalization run_id", max_length=128)
        _sha256_value(receipt, f"state finalization receipt {run_id}")
    items = state.get("items")
    if not isinstance(items, dict):
        raise PolyHavenAcquireError("Poly Haven state items must be an object")
    for asset_id, raw_entry in items.items():
        try:
            validate_asset_id(asset_id)
        except ValueError as exc:
            raise PolyHavenAcquireError("Poly Haven state contains an invalid asset id") from exc
        if not isinstance(raw_entry, dict):
            raise PolyHavenAcquireError(f"Poly Haven state item {asset_id!r} must be an object")
        if raw_entry.get("asset_id") != asset_id:
            raise PolyHavenAcquireError(f"Poly Haven state item {asset_id!r} identity is invalid")
        source_id = _source_id(raw_entry.get("source_id"), f"state item {asset_id!r}.source_id")
        revision = _string(
            raw_entry.get("revision"),
            f"state item {asset_id!r}.revision",
            max_length=40,
        )
        if revisioned_asset_id(source_id, revision) != asset_id:
            raise PolyHavenAcquireError(f"Poly Haven state item {asset_id!r} revision is invalid")
        status = raw_entry.get("status")
        if status not in _STATE_STATUSES:
            raise PolyHavenAcquireError(f"Poly Haven state item {asset_id!r} status is invalid")
        _validate_state_item(
            asset_id=asset_id,
            item=raw_entry,
            status=str(status),
            project_root=project_root,
        )


def _migrate_v1_state(state: dict[str, Any], *, project_root: Path) -> dict[str, Any]:
    if state.get("source") != POLYHAVEN_SOURCE or state.get("asset_type") != "models":
        raise PolyHavenAcquireError("Poly Haven v1 state source identity is invalid")
    raw_items = state.get("items")
    if not isinstance(raw_items, dict):
        raise PolyHavenAcquireError("Poly Haven v1 state items must be an object")
    migrated = _empty_state()
    migrated["migrated_from"] = 1
    migrated["next_selection_class"] = "pending"
    raw_updated_at = state.get("updated_at")
    if raw_updated_at is not None:
        migrated["updated_at"] = _timestamp(raw_updated_at, "v1 state.updated_at")
    for asset_id, raw in raw_items.items():
        try:
            validate_asset_id(asset_id)
        except ValueError as exc:
            raise PolyHavenAcquireError("Poly Haven v1 state has an invalid asset id") from exc
        if not isinstance(raw, dict) or raw.get("asset_id") != asset_id:
            raise PolyHavenAcquireError(f"Poly Haven v1 state item {asset_id!r} is invalid")
        source_id = _source_id(raw.get("source_id"), f"v1 state item {asset_id}.source_id")
        revision = _string(raw.get("revision"), f"v1 state item {asset_id}.revision", max_length=40)
        if revisioned_asset_id(source_id, revision) != asset_id:
            raise PolyHavenAcquireError(
                f"Poly Haven v1 state item {asset_id!r} revision is invalid"
            )
        date_published = _positive_int(
            raw.get("date_published"), f"v1 state item {asset_id}.date_published"
        )
        root_dir = _portable_state_path(
            raw.get("root_dir"), project_root=project_root, context=f"v1 {asset_id}.root_dir"
        )
        main_path = _portable_state_path(
            raw.get("main_path"), project_root=project_root, context=f"v1 {asset_id}.main_path"
        )
        metadata_path = _portable_state_path(
            raw.get("metadata_path"),
            project_root=project_root,
            context=f"v1 {asset_id}.metadata_path",
        )
        last_prepared_at = raw.get("last_prepared_at")
        checked_last_prepared_at = (
            _timestamp(last_prepared_at, f"v1 {asset_id}.last_prepared_at")
            if last_prepared_at is not None
            else None
        )
        last_run_id = raw.get("last_run_id") if isinstance(raw.get("last_run_id"), str) else None
        migrated["items"][asset_id] = {
            "asset_id": asset_id,
            "source_id": source_id,
            "revision": revision,
            "date_published": date_published,
            "status": "downloaded",
            "root_dir": root_dir,
            "main_path": main_path,
            "metadata_path": metadata_path,
            "metadata_file_sha256": None,
            "source_bundle_sha256": None,
            "source_content_sha256": None,
            "files": [],
            "acquired_at": None,
            "verified_at": None,
            "last_prepared_at": checked_last_prepared_at,
            "last_run_id": last_run_id,
            "prepare_token": None,
            "prepared_manifest_payload_sha256": None,
            "migration_pending": True,
            "terminal": None,
        }
    return migrated


def _validate_last_listing(value: Any) -> None:
    if value is None:
        return
    payload = _exact_object(
        value,
        {"payload_sha256", "observed_at", "count", "watermark"},
        "state.last_listing",
    )
    _sha256_value(payload["payload_sha256"], "state.last_listing.payload_sha256")
    _timestamp(payload["observed_at"], "state.last_listing.observed_at")
    _nonnegative_int(payload["count"], "state.last_listing.count")
    watermark = _exact_object(
        payload["watermark"],
        {"date_published", "source_id", "revision"},
        "state.last_listing.watermark",
    )
    _positive_int(watermark["date_published"], "state watermark.date_published")
    _source_id(watermark["source_id"], "state watermark.source_id")
    revision = _string(watermark["revision"], "state watermark.revision", max_length=40)
    if _SHA1_PATTERN.fullmatch(revision) is None:
        raise PolyHavenAcquireError("state watermark revision is invalid")


def _validate_state_item(
    *,
    asset_id: str,
    item: dict[str, Any],
    status: str,
    project_root: Path,
) -> None:
    expected = {
        "asset_id",
        "source_id",
        "revision",
        "date_published",
        "status",
        "root_dir",
        "main_path",
        "metadata_path",
        "metadata_file_sha256",
        "source_bundle_sha256",
        "source_content_sha256",
        "files",
        "acquired_at",
        "verified_at",
        "last_prepared_at",
        "last_run_id",
        "prepare_token",
        "prepared_manifest_payload_sha256",
        "migration_pending",
        "terminal",
    }
    if set(item) != expected:
        raise PolyHavenAcquireError(f"Poly Haven state item {asset_id!r} has unsupported keys")
    _positive_int(item["date_published"], f"state item {asset_id}.date_published")
    for key in ("root_dir", "main_path", "metadata_path"):
        _portable_state_path(
            item[key], project_root=project_root, context=f"state item {asset_id}.{key}"
        )
    migration_pending = item["migration_pending"]
    if not isinstance(migration_pending, bool):
        raise PolyHavenAcquireError(f"state item {asset_id!r} migration_pending is invalid")
    if migration_pending:
        if (
            status != "downloaded"
            or any(
                item[key] is not None
                for key in (
                    "metadata_file_sha256",
                    "source_bundle_sha256",
                    "source_content_sha256",
                    "acquired_at",
                    "verified_at",
                    "prepare_token",
                    "prepared_manifest_payload_sha256",
                )
            )
            or item["files"] != []
            or item["terminal"] is not None
        ):
            raise PolyHavenAcquireError(f"migrated state item {asset_id!r} is not nonterminal")
        return
    for key in (
        "metadata_file_sha256",
        "source_bundle_sha256",
        "source_content_sha256",
        "prepare_token",
    ):
        _sha256_value(item[key], f"state item {asset_id}.{key}")
    prepared_manifest_payload_sha256 = item["prepared_manifest_payload_sha256"]
    if prepared_manifest_payload_sha256 is not None:
        _sha256_value(
            prepared_manifest_payload_sha256,
            f"state item {asset_id}.prepared_manifest_payload_sha256",
        )
    for key in ("acquired_at", "verified_at", "last_prepared_at"):
        _timestamp(item[key], f"state item {asset_id}.{key}")
    _string(item["last_run_id"], f"state item {asset_id}.last_run_id", max_length=128)
    raw_files = item["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise PolyHavenAcquireError(f"state item {asset_id!r} files are invalid")
    seen: set[str] = set()
    for index, raw_file in enumerate(raw_files):
        file_payload = _exact_object(
            raw_file,
            {"relative_path", "bytes", "md5", "sha256"},
            f"state item {asset_id}.files[{index}]",
        )
        relative = _relative_path(file_payload["relative_path"], "state file relative_path")
        _reject_reserved_package_path(relative)
        if relative.as_posix() in seen:
            raise PolyHavenAcquireError(f"state item {asset_id!r} has duplicate files")
        seen.add(relative.as_posix())
        _positive_int(file_payload["bytes"], "state file bytes")
        md5 = _string(file_payload["md5"], "state file md5", max_length=32)
        if _MD5_PATTERN.fullmatch(md5) is None:
            raise PolyHavenAcquireError("state file md5 is invalid")
        _sha256_value(file_payload["sha256"], "state file sha256")
    terminal = item["terminal"]
    if status in _TERMINAL_STATUSES:
        terminal_payload = _exact_object(
            terminal,
            {
                "status",
                "batch_manifest",
                "batch_manifest_file_sha256",
                "committed_at",
                "terminal_evidence_sha256",
                "receipt",
            },
            f"state item {asset_id}.terminal",
        )
        if terminal_payload["status"] != status:
            raise PolyHavenAcquireError(f"state item {asset_id!r} terminal status is inconsistent")
        _portable_state_path(
            terminal_payload["batch_manifest"],
            project_root=project_root,
            context=f"state item {asset_id}.terminal.batch_manifest",
        )
        _sha256_value(
            terminal_payload["batch_manifest_file_sha256"], "terminal batch manifest hash"
        )
        _sha256_value(terminal_payload["terminal_evidence_sha256"], "terminal evidence hash")
        _timestamp(terminal_payload["committed_at"], "terminal committed_at")
        if not isinstance(terminal_payload["receipt"], dict):
            raise PolyHavenAcquireError("terminal receipt must be an object")
    elif terminal is not None:
        raise PolyHavenAcquireError(f"downloaded state item {asset_id!r} may not be terminal")


def _listing_watermark(models: tuple[PolyHavenModel, ...]) -> dict[str, Any]:
    if not models:
        raise PolyHavenAcquireError("cannot create a watermark for an empty listing")
    latest = max(models, key=lambda item: (item.date_published, item.source_id, item.revision))
    return {
        "date_published": latest.date_published,
        "source_id": latest.source_id,
        "revision": latest.revision,
    }


def _prepare_token(
    *,
    asset_id: str,
    source_id: str,
    revision: str,
    metadata_file_sha256: str,
    source_bundle_sha256: str,
    source_content_sha256: str,
    run_id: str,
) -> str:
    return _domain_payload_sha256(
        b"uefactory.polyhaven-prepare.v1\0",
        {
            "asset_id": asset_id,
            "source_id": source_id,
            "revision": revision,
            "metadata_file_sha256": metadata_file_sha256,
            "source_bundle_sha256": source_bundle_sha256,
            "source_content_sha256": source_content_sha256,
            "run_id": run_id,
        },
    )


def _prepared_manifest_payload_sha256(manifest: Mapping[str, Any]) -> str:
    status = manifest.get("status")
    if status not in {
        "prepared",
        "finalized",
        "noop",
        "journaled",
        "deferred",
        "released",
    }:
        raise PolyHavenAcquireError("Poly Haven run manifest is not receipt-eligible")
    state = _object(manifest.get("state"), "run manifest state")
    prepared_status = "prepared" if status == "finalized" else status
    payload: dict[str, Any] = {
        "schema_version": manifest.get("schema_version"),
        "source": manifest.get("source"),
        "asset_type": manifest.get("asset_type"),
        "run_id": manifest.get("run_id"),
        "status": prepared_status,
        "started_at": manifest.get("started_at"),
        "completed_at": manifest.get("completed_at"),
        "request": manifest.get("request"),
        "listing": manifest.get("listing"),
        "state": {
            "path": state.get("path"),
            "before": state.get("before"),
            "migrated_from": state.get("migrated_from"),
        },
        "generated_ingest_spec": manifest.get("generated_ingest_spec"),
        "counts": manifest.get("counts"),
        "items": manifest.get("items"),
    }
    if manifest.get("schema_version") in {
        RUNTIME_RUN_MANIFEST_SCHEMA_VERSION,
        RUN_MANIFEST_SCHEMA_VERSION,
    }:
        payload["runtime"] = manifest.get("runtime")
    if manifest.get("schema_version") == RUN_MANIFEST_SCHEMA_VERSION:
        payload["state"]["after"] = state.get("after")
        payload["failure_journal"] = manifest.get("failure_journal")
        payload["failures"] = manifest.get("failures")
    return _domain_payload_sha256(_PREPARED_MANIFEST_DIGEST_DOMAIN, payload)


def _finalized_manifest_payload_sha256(manifest: Mapping[str, Any]) -> str:
    """Bind finalize-only evidence to the durable state without a hash cycle."""

    if manifest.get("status") != "finalized":
        raise PolyHavenAcquireError("Poly Haven run manifest is not finalized")
    state = _object(manifest.get("state"), "run manifest state")
    return _domain_payload_sha256(
        _FINALIZED_MANIFEST_DIGEST_DOMAIN,
        {
            "schema_version": manifest.get("schema_version"),
            "source": manifest.get("source"),
            "asset_type": manifest.get("asset_type"),
            "run_id": manifest.get("run_id"),
            "status": manifest.get("status"),
            "finalized_at": manifest.get("finalized_at"),
            "prepare_receipt_sha256": manifest.get("prepare_receipt_sha256"),
            "state_after_finalization": state.get("after_finalization"),
            "finalization": manifest.get("finalization"),
        },
    )


def _validate_runtime_config_payload(value: Any) -> PolyHavenRuntimeConfig:
    payload = _object(value, "run manifest runtime configuration")
    legacy_keys = {
        "request_rate_per_sec",
        "request_burst",
        "retry",
        "daily_quota",
        "disk_quota",
    }
    if set(payload) not in {frozenset(legacy_keys), frozenset(legacy_keys | {"failure_schedule"})}:
        raise PolyHavenAcquireError("run manifest runtime configuration has an unsupported shape")
    retry = _exact_object(
        payload["retry"],
        {
            "max_attempts",
            "integrity_max_attempts",
            "base_delay_sec",
            "max_delay_sec",
            "max_retry_after_sec",
        },
        "run manifest retry configuration",
    )
    daily = _exact_object(
        payload["daily_quota"],
        {"max_new_items", "max_download_bytes"},
        "run manifest daily quota configuration",
    )
    disk = _exact_object(
        payload["disk_quota"],
        {"max_storage_bytes", "min_free_bytes"},
        "run manifest disk quota configuration",
    )
    schedule = (
        _exact_object(
            payload["failure_schedule"],
            {
                "backoff_base_sec",
                "backoff_max_sec",
                "integrity_quarantine_after_runs",
            },
            "run manifest failure schedule configuration",
        )
        if "failure_schedule" in payload
        else {
            "backoff_base_sec": 300.0,
            "backoff_max_sec": 86_400.0,
            "integrity_quarantine_after_runs": 3,
        }
    )
    integrity_quarantine_after_runs = _positive_int(
        schedule["integrity_quarantine_after_runs"],
        "run manifest integrity quarantine threshold",
    )
    try:
        config = PolyHavenRuntimeConfig(
            request_rate_per_sec=payload["request_rate_per_sec"],
            request_burst=payload["request_burst"],
            retry_max_attempts=retry["max_attempts"],
            integrity_max_attempts=retry["integrity_max_attempts"],
            retry_base_delay_sec=retry["base_delay_sec"],
            retry_max_delay_sec=retry["max_delay_sec"],
            max_retry_after_sec=retry["max_retry_after_sec"],
            max_new_items_per_day=daily["max_new_items"],
            max_download_bytes_per_day=daily["max_download_bytes"],
            max_storage_bytes=disk["max_storage_bytes"],
            min_free_bytes=disk["min_free_bytes"],
            cross_run_backoff_base_sec=schedule["backoff_base_sec"],
            cross_run_backoff_max_sec=schedule["backoff_max_sec"],
            integrity_quarantine_after_runs=integrity_quarantine_after_runs,
        )
    except (PolyHavenAcquireError, TypeError) as exc:
        raise PolyHavenAcquireError("run manifest runtime configuration is invalid") from exc
    canonical = config.as_dict()
    if "failure_schedule" not in payload:
        canonical.pop("failure_schedule")
    if _payload_sha256(canonical) != _payload_sha256(payload):
        raise PolyHavenAcquireError("run manifest runtime configuration is not canonical")
    return config


def _runtime_usage_payload(value: Any, context: str) -> dict[str, Any]:
    payload = _exact_object(
        value,
        {"new_items_reserved", "download_bytes_reserved"},
        context,
    )
    _nonnegative_int(payload["new_items_reserved"], f"{context}.new_items_reserved")
    _nonnegative_int(payload["download_bytes_reserved"], f"{context}.download_bytes_reserved")
    return payload


def _validate_runtime_evidence(
    value: Any,
    *,
    config: PolyHavenRuntimeConfig,
    project_root: Path,
    received_body_bytes: int,
) -> None:
    payload = _exact_object(value, {"http", "daily_quota", "disk"}, "run manifest runtime")
    http = _exact_object(
        payload["http"],
        {
            "request_attempts",
            "retry_attempts",
            "retry_after_honored",
            "rate_limit_wait_ms",
            "retry_wait_ms",
            "download_body_bytes",
        },
        "run manifest runtime.http",
    )
    for key, raw in http.items():
        _nonnegative_int(raw, f"run manifest runtime.http.{key}")
    if http["request_attempts"] < 1 or http["retry_attempts"] > http["request_attempts"]:
        raise PolyHavenAcquireError("run manifest HTTP attempt accounting differs")
    if http["retry_after_honored"] > http["retry_attempts"]:
        raise PolyHavenAcquireError("run manifest Retry-After accounting differs")
    if http["download_body_bytes"] != received_body_bytes:
        raise PolyHavenAcquireError("run manifest download body accounting differs")

    daily = _exact_object(
        payload["daily_quota"],
        {
            "enabled",
            "ledger_path",
            "utc_day",
            "usage_before",
            "reserved_by_run",
            "accounted_overage_bytes",
            "released_probe_bytes",
            "deferred_new_items",
            "usage_after",
            "item_reservations_after",
            "open_downloads_after",
            "ledger_file_sha256",
        },
        "run manifest runtime.daily_quota",
    )
    enabled = daily["enabled"]
    expected_enabled = (
        config.daily_quota_limits.max_new_items is not None
        or config.daily_quota_limits.max_download_bytes is not None
    )
    if not isinstance(enabled, bool) or enabled is not expected_enabled:
        raise PolyHavenAcquireError("run manifest daily quota enablement differs")
    raw_day = daily["utc_day"]
    if not isinstance(raw_day, str):
        raise PolyHavenAcquireError("run manifest daily quota day is invalid")
    try:
        parsed_day = date.fromisoformat(raw_day)
    except ValueError as exc:
        raise PolyHavenAcquireError("run manifest daily quota day is invalid") from exc
    if parsed_day.isoformat() != raw_day:
        raise PolyHavenAcquireError("run manifest daily quota day is not canonical")
    before = _runtime_usage_payload(daily["usage_before"], "daily quota usage_before")
    after = _runtime_usage_payload(daily["usage_after"], "daily quota usage_after")
    reserved = _exact_object(
        daily["reserved_by_run"],
        {"new_items", "download_bytes"},
        "daily quota reserved_by_run",
    )
    for key, raw in reserved.items():
        _nonnegative_int(raw, f"daily quota reserved_by_run.{key}")
    accounted_overage = _nonnegative_int(
        daily["accounted_overage_bytes"], "daily quota accounted_overage_bytes"
    )
    released_probe_bytes = _nonnegative_int(
        daily["released_probe_bytes"], "daily quota released_probe_bytes"
    )
    deferred_new_items = _nonnegative_int(
        daily["deferred_new_items"], "daily quota deferred_new_items"
    )
    if (
        before["new_items_reserved"] + reserved["new_items"] != after["new_items_reserved"]
        or before["download_bytes_reserved"] + reserved["download_bytes"] - released_probe_bytes
        != after["download_bytes_reserved"]
    ):
        raise PolyHavenAcquireError("run manifest daily quota reservation accounting differs")
    item_count = _nonnegative_int(
        daily["item_reservations_after"], "daily quota item_reservations_after"
    )
    _nonnegative_int(daily["open_downloads_after"], "daily quota open_downloads_after")
    if item_count != after["new_items_reserved"]:
        raise PolyHavenAcquireError("run manifest daily item reservation accounting differs")
    ledger_path = daily["ledger_path"]
    ledger_hash = daily["ledger_file_sha256"]
    if enabled:
        _portable_state_path(
            ledger_path,
            project_root=project_root,
            context="run manifest daily quota ledger_path",
        )
        if ledger_hash is not None:
            _sha256_value(ledger_hash, "run manifest daily quota ledger hash")
    elif (
        ledger_path is not None
        or ledger_hash is not None
        or any(before.values())
        or any(after.values())
        or any(reserved.values())
        or accounted_overage
        or released_probe_bytes
        or item_count
        or daily["open_downloads_after"]
        or deferred_new_items
    ):
        raise PolyHavenAcquireError("disabled run manifest daily quota contains accounting")
    if accounted_overage:
        raise PolyHavenAcquireError("receipt-eligible run contains download overage accounting")
    if (
        config.daily_quota_limits.max_download_bytes is not None
        and received_body_bytes > after["download_bytes_reserved"]
    ):
        raise PolyHavenAcquireError("run downloaded bytes exceed durable quota reservations")

    disk = _exact_object(
        payload["disk"],
        {"checks", "max_storage_bytes_observed", "min_free_bytes_observed"},
        "run manifest runtime.disk",
    )
    checks = _nonnegative_int(disk["checks"], "run manifest runtime.disk.checks")
    for key in ("max_storage_bytes_observed", "min_free_bytes_observed"):
        raw = disk[key]
        if checks == 0:
            if raw is not None:
                raise PolyHavenAcquireError("run manifest disk observation exists without checks")
        else:
            _nonnegative_int(raw, f"run manifest runtime.disk.{key}")


def _validate_failure_journal_manifest_receipt(
    value: Any,
    *,
    failures: Any,
    counts: Mapping[str, Any],
    project_root: Path,
    expected_failure_policy: CrossRunFailurePolicy,
) -> list[dict[str, Any]]:
    receipt = _exact_object(
        value,
        {"path", "before", "after", "event_refs"},
        "run manifest failure_journal",
    )
    relative_path = _portable_state_path(
        receipt["path"],
        project_root=project_root,
        context="run manifest failure_journal.path",
    )
    path = project_root / relative_path
    loaded = _load_failure_journal(path)
    current = loaded.payload
    events = current["events"]
    snapshots: dict[str, dict[str, Any]] = {}
    for key in ("before", "after"):
        snapshot = _exact_object(
            receipt[key],
            {
                "exists",
                "file_sha256",
                "payload_sha256",
                "event_count",
                "head_event_sha256",
            },
            f"run manifest failure_journal.{key}",
        )
        exists = snapshot["exists"]
        if not isinstance(exists, bool):
            raise PolyHavenAcquireError("failure journal snapshot exists flag is invalid")
        event_count = _nonnegative_int(
            snapshot["event_count"],
            f"run manifest failure_journal.{key}.event_count",
        )
        if event_count > len(events):
            raise PolyHavenAcquireError("failure journal receipt is ahead of current history")
        if event_count:
            expected_head = events[event_count - 1]["event_sha256"]
            if snapshot["head_event_sha256"] != expected_head:
                raise PolyHavenAcquireError("failure journal receipt head differs from history")
        elif snapshot["head_event_sha256"] is not None:
            raise PolyHavenAcquireError("empty failure journal receipt has a head")
        prefix = _failure_journal_prefix(current, event_count)
        if exists:
            _sha256_value(
                snapshot["file_sha256"],
                f"run manifest failure_journal.{key}.file_sha256",
            )
            _sha256_value(
                snapshot["payload_sha256"],
                f"run manifest failure_journal.{key}.payload_sha256",
            )
            if snapshot["file_sha256"] != _json_file_sha256(prefix) or snapshot[
                "payload_sha256"
            ] != _payload_sha256(prefix):
                raise PolyHavenAcquireError(
                    "failure journal receipt snapshot differs from its history prefix"
                )
        elif (
            snapshot["file_sha256"] is not None
            or snapshot["payload_sha256"] is not None
            or event_count
        ):
            raise PolyHavenAcquireError("absent failure journal receipt contains history")
        snapshots[key] = snapshot

    if not snapshots["after"]["exists"] or not path.is_file() or path.is_symlink():
        raise PolyHavenAcquireError("failure journal receipt has no durable after file")

    refs = receipt["event_refs"]
    if not isinstance(refs, list):
        raise PolyHavenAcquireError("failure journal event_refs must be a list")
    before_count = snapshots["before"]["event_count"]
    after_count = snapshots["after"]["event_count"]
    if after_count < before_count:
        raise PolyHavenAcquireError("failure journal receipt moves its head backwards")
    referenced_events: list[dict[str, Any]] = []
    referenced_sequences: list[int] = []
    for raw_ref in refs:
        ref = _exact_object(
            raw_ref,
            {"sequence", "event_id", "event_sha256", "type"},
            "run manifest failure journal event ref",
        )
        sequence = _positive_int(ref["sequence"], "failure journal event ref sequence")
        if sequence > after_count:
            raise PolyHavenAcquireError("failure journal event ref is beyond its after head")
        if referenced_sequences and sequence <= referenced_sequences[-1]:
            raise PolyHavenAcquireError("failure journal event refs are not strictly ordered")
        referenced_sequences.append(sequence)
        event = events[sequence - 1]
        for ref_key in ("event_id", "event_sha256", "type"):
            if ref[ref_key] != event[ref_key]:
                raise PolyHavenAcquireError("failure journal event ref differs from history")
        referenced_events.append(event)
    appended_sequences = set(range(before_count + 1, after_count + 1))
    if not appended_sequences.issubset(referenced_sequences):
        raise PolyHavenAcquireError("failure journal appended event range is not fully referenced")

    if not isinstance(failures, list):
        raise PolyHavenAcquireError("run manifest failures must be a list")
    expected_failures = [event for event in referenced_events if event["type"] == "failed"]
    if failures != expected_failures:
        raise PolyHavenAcquireError("run manifest failures differ from journal events")
    expected_policy_payload = {
        "backoff_base_sec": expected_failure_policy.backoff_base_sec,
        "backoff_max_sec": expected_failure_policy.backoff_max_sec,
        "integrity_quarantine_after": expected_failure_policy.integrity_quarantine_after,
    }
    if any(event.get("policy") != expected_policy_payload for event in expected_failures):
        raise PolyHavenAcquireError("run manifest failure policy differs from its journal events")
    deferred = sum(
        event["failure"]["category"] == FailureCategory.DEFERRED.value
        for event in expected_failures
    )
    failed = len(expected_failures) - deferred
    quarantined = sum(
        event["disposition"] == FailureDisposition.QUARANTINED.value for event in expected_failures
    )
    if (
        counts["failed"] != failed
        or counts["deferred"] != deferred
        or counts["quarantined"] != quarantined
    ):
        raise PolyHavenAcquireError("run manifest failure counts differ from journal events")
    if "released" in counts:
        released = sum(event["type"] == "released" for event in referenced_events)
        if counts["released"] != released:
            raise PolyHavenAcquireError("run manifest release count differs from journal events")
    return referenced_events


def _failure_journal_prefix(payload: Mapping[str, Any], event_count: int) -> dict[str, Any]:
    result = json.loads(json.dumps(payload))
    result["events"] = result["events"][:event_count]
    result["next_sequence"] = event_count + 1
    result["head_event_sha256"] = result["events"][-1]["event_sha256"] if event_count else None
    result["updated_at"] = result["events"][-1]["recorded_at"] if event_count else None
    try:
        validate_failure_journal(
            result,
            source=POLYHAVEN_SOURCE,
            asset_type="models",
        )
    except FailureJournalError as exc:
        raise PolyHavenAcquireError(f"failure journal history prefix is invalid: {exc}") from exc
    return result


def _validate_sync_journal_event_binding(
    events: list[dict[str, Any]],
    *,
    run_id: str,
    attempted: int,
    successful_asset_ids: set[str],
    retry_revisions: tuple[str, ...],
    request_resolution: str,
) -> None:
    seen_attempt_ordinals: set[int] = set()
    seen_release_ordinals: set[int] = set()
    failed_asset_ids: set[str] = set()
    resolved_asset_ids: set[str] = set()
    for event in events:
        if event.get("run_id") != run_id:
            raise PolyHavenAcquireError("run manifest references a foreign journal event")
        asset_id = _string(
            event.get("asset_id"),
            "run journal event asset_id",
            max_length=64,
        )
        try:
            validate_asset_id(asset_id)
        except ValueError as exc:
            raise PolyHavenAcquireError("run journal event asset id is invalid") from exc
        source_id = _source_id(
            event.get("source_id"),
            "run journal event source_id",
        )
        revision = _string(
            event.get("revision"),
            "run journal event revision",
            max_length=40,
        )
        if (
            _SHA1_PATTERN.fullmatch(revision) is None
            or revisioned_asset_id(source_id, revision) != asset_id
        ):
            raise PolyHavenAcquireError("run journal event revision identity is invalid")
        event_resolution = _resolution(event.get("resolution"))
        event_type = event.get("type")
        attempt_id = _string(
            event.get("attempt_id"),
            "run journal event attempt_id",
            max_length=160,
        )
        if event_type == "released":
            ordinal = _journal_attempt_ordinal(
                attempt_id,
                prefix=f"{run_id}:release:",
                maximum=len(retry_revisions),
                context="release journal event",
            )
            if ordinal in seen_release_ordinals:
                raise PolyHavenAcquireError("run journal has a duplicate release ordinal")
            seen_release_ordinals.add(ordinal)
            if retry_revisions[ordinal - 1] != asset_id:
                raise PolyHavenAcquireError("release journal event differs from its retry target")
            continue
        if event_type not in {"failed", "resolved"}:
            raise PolyHavenAcquireError("run journal contains an unsupported event type")
        if event_resolution != request_resolution:
            raise PolyHavenAcquireError("revision journal event resolution differs from its run")
        ordinal = _journal_attempt_ordinal(
            attempt_id,
            prefix=f"{run_id}:",
            maximum=attempted,
            context="revision journal event",
        )
        if ordinal in seen_attempt_ordinals:
            raise PolyHavenAcquireError("run journal has a duplicate revision attempt ordinal")
        seen_attempt_ordinals.add(ordinal)
        if event_type == "failed":
            if asset_id in successful_asset_ids or asset_id in failed_asset_ids:
                raise PolyHavenAcquireError("failed journal event differs from the run cohort")
            failed_asset_ids.add(asset_id)
        else:
            if asset_id not in successful_asset_ids or asset_id in resolved_asset_ids:
                raise PolyHavenAcquireError("resolution journal event differs from the run cohort")
            resolved_asset_ids.add(asset_id)
    if seen_release_ordinals != set(range(1, len(retry_revisions) + 1)):
        raise PolyHavenAcquireError("run journal release events do not cover every retry target")


def _journal_attempt_ordinal(
    attempt_id: str,
    *,
    prefix: str,
    maximum: int,
    context: str,
) -> int:
    if not attempt_id.startswith(prefix):
        raise PolyHavenAcquireError(f"{context} id is not bound to its run")
    raw = attempt_id[len(prefix) :]
    if not raw.isascii() or not raw.isdigit() or raw.startswith("0"):
        raise PolyHavenAcquireError(f"{context} ordinal is not canonical")
    ordinal = int(raw)
    if ordinal < 1 or ordinal > maximum:
        raise PolyHavenAcquireError(f"{context} ordinal is outside the run cohort")
    return ordinal


def _validate_prepared_manifest_receipt(
    *,
    manifest: dict[str, Any],
    state: dict[str, Any],
    project_root: Path,
    require_current_state_receipt: bool = True,
) -> None:
    status = manifest.get("status")
    version = manifest.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise PolyHavenAcquireError("Poly Haven run manifest schema version is not canonical")
    has_runtime_receipt = version in {
        RUNTIME_RUN_MANIFEST_SCHEMA_VERSION,
        RUN_MANIFEST_SCHEMA_VERSION,
    }
    has_failure_receipt = version == RUN_MANIFEST_SCHEMA_VERSION
    if version == LEGACY_RUN_MANIFEST_SCHEMA_VERSION and status == "noop":
        raise PolyHavenAcquireError(
            "legacy schema-2 no-op receipts are unverifiable and may not be replayed"
        )
    base_keys = {
        "schema_version",
        "source",
        "asset_type",
        "run_id",
        "status",
        "started_at",
        "completed_at",
        "request",
        "listing",
        "state",
        "generated_ingest_spec",
        "counts",
        "items",
        "prepare_receipt_sha256",
    }
    if has_runtime_receipt:
        base_keys.add("runtime")
    if has_failure_receipt:
        base_keys.update({"failure_journal", "failures"})
    expected_keys = base_keys | (
        {"finalized_at", "finalization"} if status == "finalized" else set()
    )
    allowed_statuses = {"prepared", "finalized", "noop"}
    if has_failure_receipt:
        allowed_statuses.update({"journaled", "deferred", "released"})
    if status not in allowed_statuses or set(manifest) != expected_keys:
        raise PolyHavenAcquireError("Poly Haven run manifest has an unsupported receipt shape")
    if (
        version not in _SUPPORTED_RUN_MANIFEST_SCHEMA_VERSIONS
        or manifest.get("source") != POLYHAVEN_SOURCE
        or manifest.get("asset_type") != "models"
    ):
        raise PolyHavenAcquireError("Poly Haven run manifest receipt identity is invalid")
    _string(manifest.get("run_id"), "run manifest run_id", max_length=128)
    _timestamp(manifest.get("started_at"), "run manifest started_at")
    _timestamp(manifest.get("completed_at"), "run manifest completed_at")

    request_keys = {"force", "limit", "resolution"}
    if has_runtime_receipt:
        request_keys.add("runtime")
    if has_failure_receipt:
        request_keys.add("retry_revisions")
    request = _exact_object(manifest.get("request"), request_keys, "run manifest request")
    if not isinstance(request["force"], bool):
        raise PolyHavenAcquireError("run manifest request.force must be boolean")
    _positive_int(request["limit"], "run manifest request.limit")
    _resolution(request["resolution"])
    if has_failure_receipt:
        retry_revisions = request["retry_revisions"]
        if (
            not isinstance(retry_revisions, list)
            or any(not isinstance(asset_id, str) for asset_id in retry_revisions)
            or retry_revisions != sorted(set(retry_revisions))
        ):
            raise PolyHavenAcquireError("run manifest retry revisions are not canonical")
        for asset_id in retry_revisions:
            try:
                validate_asset_id(asset_id)
            except ValueError as exc:
                raise PolyHavenAcquireError(
                    "run manifest retry revisions contain an invalid asset id"
                ) from exc
    if has_failure_receipt:
        runtime_payload = _object(
            request["runtime"],
            "run manifest runtime configuration",
        )
        if "failure_schedule" not in runtime_payload:
            raise PolyHavenAcquireError(
                "schema-4 run manifest requires an explicit failure schedule"
            )
    runtime_config = (
        _validate_runtime_config_payload(request["runtime"]) if has_runtime_receipt else None
    )

    listing = _exact_object(
        manifest.get("listing"),
        {"url", "discovered", "payload_sha256", "watermark"},
        "run manifest listing",
    )
    if listing["url"] != POLYHAVEN_MODELS_URL:
        raise PolyHavenAcquireError("run manifest listing URL is invalid")
    discovered = _nonnegative_int(listing["discovered"], "run manifest listing.discovered")
    _sha256_value(listing["payload_sha256"], "run manifest listing.payload_sha256")
    watermark = listing["watermark"]
    if discovered == 0:
        if watermark is not None:
            raise PolyHavenAcquireError("empty run manifest listing has a watermark")
    else:
        checked_watermark = _exact_object(
            watermark,
            {"date_published", "source_id", "revision"},
            "run manifest listing.watermark",
        )
        _positive_int(checked_watermark["date_published"], "run manifest watermark date")
        _source_id(checked_watermark["source_id"], "run manifest watermark source_id")
        revision = _string(
            checked_watermark["revision"], "run manifest watermark revision", max_length=40
        )
        if _SHA1_PATTERN.fullmatch(revision) is None:
            raise PolyHavenAcquireError("run manifest watermark revision is invalid")

    state_payload = _object(manifest.get("state"), "run manifest state")
    state_keys = {"path", "before", "after", "migrated_from"}
    if status == "finalized":
        state_keys.add("after_finalization")
    if set(state_payload) != state_keys:
        raise PolyHavenAcquireError("Poly Haven run manifest state shape is invalid")
    _portable_state_path(
        state_payload["path"], project_root=project_root, context="run manifest state.path"
    )
    before = _exact_object(
        state_payload["before"],
        {"exists", "file_sha256", "payload_sha256"},
        "run manifest state.before",
    )
    if not isinstance(before["exists"], bool):
        raise PolyHavenAcquireError("run manifest state.before.exists must be boolean")
    for key in ("file_sha256", "payload_sha256"):
        if before["exists"]:
            _sha256_value(before[key], f"run manifest state.before.{key}")
        elif before[key] is not None:
            raise PolyHavenAcquireError("absent run manifest state has a before hash")
    state_receipt_keys = ["after"]
    if status == "finalized":
        state_receipt_keys.append("after_finalization")
    state_receipts: dict[str, dict[str, Any]] = {}
    for key in state_receipt_keys:
        if has_failure_receipt:
            state_receipts[key] = _exact_object(
                state_payload[key],
                {"transition_sha256"},
                f"run manifest state.{key}",
            )
            _sha256_value(
                state_receipts[key]["transition_sha256"],
                f"run manifest state.{key}.transition_sha256",
            )
        else:
            state_receipts[key] = _exact_object(
                state_payload[key],
                {"file_sha256", "payload_sha256"},
                f"run manifest state.{key}",
            )
            _sha256_value(
                state_receipts[key]["file_sha256"],
                f"run manifest state.{key}.file_sha256",
            )
            _sha256_value(
                state_receipts[key]["payload_sha256"],
                f"run manifest state.{key}.payload_sha256",
            )
    if require_current_state_receipt:
        current_key = "after_finalization" if status == "finalized" else "after"
        current_receipt = state_receipts[current_key]
        if has_failure_receipt:
            if current_receipt["transition_sha256"] != _state_transition_sha256(
                state,
                run_id=str(manifest["run_id"]),
            ):
                raise PolyHavenAcquireError("Poly Haven run state transition receipt is stale")
        elif current_receipt["file_sha256"] != _json_file_sha256(state) or current_receipt[
            "payload_sha256"
        ] != _payload_sha256(state):
            raise PolyHavenAcquireError("Poly Haven run manifest state receipt is stale")
    if state_payload["migrated_from"] not in {None, 1}:
        raise PolyHavenAcquireError("run manifest state migration marker is invalid")

    count_keys = {
        "selected",
        "downloaded_files",
        "reused_files",
        "downloaded_bytes",
        "verified_bytes",
    }
    if has_failure_receipt:
        count_keys.update(
            {
                "attempted",
                "failed",
                "deferred",
                "quarantined",
                "released",
                "transport_body_bytes",
            }
        )
    counts = _exact_object(manifest.get("counts"), count_keys, "run manifest counts")
    for key, value in counts.items():
        _nonnegative_int(value, f"run manifest counts.{key}")
    if runtime_config is not None:
        _validate_runtime_evidence(
            manifest.get("runtime"),
            config=runtime_config,
            project_root=project_root,
            received_body_bytes=(
                counts["transport_body_bytes"]
                if has_failure_receipt
                else counts["downloaded_bytes"]
            ),
        )
    referenced_journal_events: list[dict[str, Any]] = []
    if has_failure_receipt:
        if runtime_config is None:
            raise PolyHavenAcquireError("schema-4 run manifest has no runtime configuration")
        if counts["attempted"] != counts["selected"] + counts["failed"] + counts["deferred"]:
            raise PolyHavenAcquireError("run manifest attempt accounting differs")
        if counts["attempted"] > request["limit"]:
            raise PolyHavenAcquireError("run manifest attempted count exceeds its request limit")
        if counts["quarantined"] > counts["failed"]:
            raise PolyHavenAcquireError("run manifest quarantine accounting differs")
        referenced_journal_events = _validate_failure_journal_manifest_receipt(
            manifest.get("failure_journal"),
            failures=manifest.get("failures"),
            counts=counts,
            project_root=project_root,
            expected_failure_policy=runtime_config.failure_policy,
        )
    raw_items = manifest.get("items")
    if not isinstance(raw_items, list):
        raise PolyHavenAcquireError("Poly Haven run manifest items must be a list")
    if counts["selected"] != len(raw_items):
        raise PolyHavenAcquireError("Poly Haven run manifest selected count differs")
    if status in {"noop", "journaled", "deferred", "released"}:
        if raw_items or manifest.get("generated_ingest_spec") is not None or any(counts.values()):
            allowed_nonzero = (
                {
                    "attempted",
                    "failed",
                    "deferred",
                    "quarantined",
                    "released",
                    "transport_body_bytes",
                }
                if status in {"journaled", "deferred", "released"}
                else set()
            )
            if (
                raw_items
                or manifest.get("generated_ingest_spec") is not None
                or any(value for key, value in counts.items() if key not in allowed_nonzero)
            ):
                raise PolyHavenAcquireError(
                    "Poly Haven receipt without prepared items contains selected work"
                )
        if status == "journaled" and counts.get("failed", 0) == 0:
            raise PolyHavenAcquireError("journaled run failure counts are invalid")
        if status == "deferred" and (counts.get("deferred", 0) == 0 or counts.get("failed")):
            raise PolyHavenAcquireError("deferred run failure counts are invalid")
        if status == "released" and (
            counts.get("released", 0) == 0
            or counts.get("failed")
            or counts.get("deferred")
            or counts.get("attempted")
        ):
            raise PolyHavenAcquireError("released run counts are invalid")
    elif not raw_items:
        raise PolyHavenAcquireError("prepared Poly Haven receipt has no items")

    generated = manifest.get("generated_ingest_spec")
    if generated is not None:
        generated_payload = _exact_object(
            generated, {"path", "file_sha256"}, "run manifest generated_ingest_spec"
        )
        _portable_state_path(
            generated_payload["path"],
            project_root=project_root,
            context="run manifest generated_ingest_spec.path",
        )
        _sha256_value(
            generated_payload["file_sha256"], "run manifest generated_ingest_spec.file_sha256"
        )
    elif status in {"prepared", "finalized"}:
        raise PolyHavenAcquireError("prepared Poly Haven receipt lacks generated IngestSpec")

    item_count_keys = {
        "downloaded_files",
        "reused_files",
        "downloaded_bytes",
        "verified_bytes",
    }
    item_keys = {
        "asset_id",
        "source_id",
        "source_url",
        "revision",
        "date_published",
        "license",
        "license_tier",
        "resolution",
        "root_dir",
        "main_path",
        "dependency_paths",
        "metadata_path",
        "metadata_file_sha256",
        "source_bundle_sha256",
        "source_content_sha256",
        "acquired_at",
        "verified_at",
        "prepare_token",
        "state_status_before",
        "state_status_after",
        "counts",
        "files",
    }
    totals = {key: 0 for key in item_count_keys}
    seen_asset_ids: set[str] = set()
    prepare_receipt_sha256 = _sha256_value(
        manifest.get("prepare_receipt_sha256"), "run manifest prepare_receipt_sha256"
    )
    for index, raw_item in enumerate(raw_items):
        item = _exact_object(raw_item, item_keys, f"run manifest items[{index}]")
        asset_id = _string(item["asset_id"], "run manifest asset_id", max_length=64)
        if asset_id in seen_asset_ids:
            raise PolyHavenAcquireError("Poly Haven run manifest has duplicate asset ids")
        seen_asset_ids.add(asset_id)
        item_counts = _exact_object(
            item["counts"], item_count_keys, f"run manifest item {asset_id}.counts"
        )
        for key, value in item_counts.items():
            totals[key] += _nonnegative_int(value, f"run manifest item {asset_id}.{key}")
        files = item["files"]
        if not isinstance(files, list) or not files:
            raise PolyHavenAcquireError(f"run manifest item {asset_id!r} has no files")
        downloaded_actions = 0
        reused_actions = 0
        verified_file_bytes = 0
        for file_index, raw_file in enumerate(files):
            file_payload = _exact_object(
                raw_file,
                {"relative_path", "url", "bytes", "md5", "sha256", "action"},
                f"run manifest item {asset_id}.files[{file_index}]",
            )
            _relative_path(file_payload["relative_path"], "run manifest file relative_path")
            size = _positive_int(file_payload["bytes"], "run manifest file bytes")
            verified_file_bytes += size
            _sha256_value(file_payload["sha256"], "run manifest file sha256")
            action = file_payload["action"]
            if action == "downloaded":
                downloaded_actions += 1
            elif action == "reused":
                reused_actions += 1
            else:
                raise PolyHavenAcquireError("run manifest file action is invalid")
        if (
            item_counts["downloaded_files"] != downloaded_actions
            or item_counts["reused_files"] != reused_actions
            or item_counts["verified_bytes"] != verified_file_bytes
        ):
            raise PolyHavenAcquireError(f"run manifest item accounting differs for {asset_id!r}")
        state_item = state.get("items", {}).get(asset_id)
        if (
            not isinstance(state_item, dict)
            or state_item.get("prepared_manifest_payload_sha256") != prepare_receipt_sha256
        ):
            raise PolyHavenAcquireError(f"state prepared-manifest receipt differs for {asset_id!r}")
    if any(counts[key] != totals[key] for key in item_count_keys):
        raise PolyHavenAcquireError("Poly Haven run manifest aggregate accounting differs")
    if has_failure_receipt:
        _validate_sync_journal_event_binding(
            referenced_journal_events,
            run_id=str(manifest["run_id"]),
            attempted=counts["attempted"],
            successful_asset_ids=seen_asset_ids,
            retry_revisions=tuple(request["retry_revisions"]),
            request_resolution=str(request["resolution"]),
        )
    actual_receipt_sha256 = _prepared_manifest_payload_sha256(manifest)
    if prepare_receipt_sha256 != actual_receipt_sha256:
        raise PolyHavenAcquireError("Poly Haven prepared manifest receipt changed")
    if status in {"noop", "journaled", "deferred", "released"}:
        noop_receipts = _object(
            state.get("noop_run_receipts"),
            "Poly Haven state noop_run_receipts",
        )
        if noop_receipts.get(manifest["run_id"]) != prepare_receipt_sha256:
            raise PolyHavenAcquireError("Poly Haven no-op receipt is not anchored in state")
    if status == "finalized":
        _validated_finalization_payload(
            manifest=manifest,
            project_root=project_root,
            expected_asset_ids=seen_asset_ids,
        )
        if has_failure_receipt:
            finalization_receipts = _object(
                state.get("finalization_run_receipts"),
                "Poly Haven state finalization_run_receipts",
            )
            if finalization_receipts.get(manifest["run_id"]) != (
                _finalized_manifest_payload_sha256(manifest)
            ):
                raise PolyHavenAcquireError(
                    "Poly Haven finalized manifest receipt is not anchored in state"
                )


def _existing_metadata_times(
    path: Path,
    *,
    asset_id: str,
    revision: str,
) -> tuple[str | None, str | None]:
    if not path.exists() and not path.is_symlink():
        return None, None
    _require_regular_file(path, "Poly Haven metadata")
    payload = _read_json_object_strict(path, "Poly Haven metadata")
    if (
        payload.get("schema_version") not in {1, 2}
        or payload.get("source") != POLYHAVEN_SOURCE
        or payload.get("asset_id") != asset_id
        or payload.get("revision") != revision
    ):
        raise PolyHavenAcquireError("existing Poly Haven metadata identity is invalid")
    acquired_at = _timestamp(payload.get("acquired_at"), "metadata.acquired_at")
    raw_verified_at = payload.get("verified_at")
    verified_at = (
        _timestamp(raw_verified_at, "metadata.verified_at") if raw_verified_at is not None else None
    )
    return acquired_at, verified_at


def _require_exact_gltf_dependency_closure(
    *,
    root_dir: Path,
    package: PolyHavenModelPackage,
) -> None:
    observed = gltf_dependency_paths(root_dir / package.main_file)
    if observed != package.dependencies:
        raise PolyHavenAcquireError(
            "Poly Haven glTF URI dependency closure does not match the API include closure"
        )


def _require_data_dir_inside_project(*, project_root: Path, data_dir: Path) -> None:
    try:
        relative = data_dir.relative_to(project_root)
    except ValueError as exc:
        raise PolyHavenAcquireError("Poly Haven data_dir must be inside project_root") from exc
    if not relative.parts:
        raise PolyHavenAcquireError("Poly Haven data_dir may not equal project_root")


def _checked_project_file(path: Path, *, project_root: Path, context: str) -> Path:
    unresolved = path.expanduser()
    if not unresolved.is_absolute():
        unresolved = project_root / unresolved
    _reject_symlink_components(unresolved, project_root=project_root, context=context)
    checked = unresolved.resolve()
    try:
        checked.relative_to(project_root)
    except ValueError as exc:
        raise PolyHavenAcquireError(f"{context} must be inside project_root") from exc
    _require_regular_file(checked, context)
    return checked


def _metadata_payload(
    *,
    model: PolyHavenModel,
    package: PolyHavenModelPackage,
    files: tuple[_DownloadedFile, ...],
    project_root: Path,
    acquired_at: str,
    verified_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "source": POLYHAVEN_SOURCE,
        "source_id": model.source_id,
        "source_url": POLYHAVEN_ASSET_URL.format(source_id=quote(model.source_id, safe="")),
        "asset_id": model.asset_id,
        "name": model.name,
        "date_published": model.date_published,
        "revision": model.revision,
        "authors": [{"name": name, "credit": credit} for name, credit in model.authors],
        "categories": list(model.categories),
        "tags": list(model.tags),
        "license": POLYHAVEN_LICENSE,
        "license_tier": "open",
        "license_url": POLYHAVEN_LICENSE_URL,
        "resolution": package.resolution,
        "main_file": package.main_file.as_posix(),
        "dependencies": [path.as_posix() for path in package.dependencies],
        "acquired_at": acquired_at,
        "verified_at": verified_at,
        "files": [
            {
                "relative_path": item.spec.relative_path.as_posix(),
                "path": _portable_path(item.path, project_root),
                "url": item.spec.url,
                "bytes": item.spec.bytes,
                "md5": item.spec.md5,
                "sha256": item.sha256,
            }
            for item in files
        ],
    }


def _manifest_item(
    *,
    item: PolyHavenSyncItem,
    model: PolyHavenModel,
    package: PolyHavenModelPackage,
    files: tuple[_DownloadedFile, ...],
    project_root: Path,
    run_id: str,
    state_status_before: str | None,
) -> dict[str, Any]:
    metadata_file_sha256 = _sha256_file(item.metadata_path)
    return {
        "asset_id": item.asset_id,
        "source_id": item.source_id,
        "source_url": POLYHAVEN_ASSET_URL.format(source_id=quote(item.source_id, safe="")),
        "revision": item.revision,
        "date_published": model.date_published,
        "license": POLYHAVEN_LICENSE,
        "license_tier": "open",
        "resolution": package.resolution,
        "root_dir": _portable_path(item.root_dir, project_root),
        "main_path": _portable_path(item.main_path, project_root),
        "dependency_paths": [_portable_path(path, project_root) for path in item.dependency_paths],
        "metadata_path": _portable_path(item.metadata_path, project_root),
        "metadata_file_sha256": metadata_file_sha256,
        "source_bundle_sha256": item.source_bundle_sha256,
        "source_content_sha256": item.source_content_sha256,
        "acquired_at": item.acquired_at,
        "verified_at": item.verified_at,
        "prepare_token": _prepare_token(
            asset_id=item.asset_id,
            source_id=item.source_id,
            revision=item.revision,
            metadata_file_sha256=metadata_file_sha256,
            source_bundle_sha256=item.source_bundle_sha256,
            source_content_sha256=item.source_content_sha256,
            run_id=run_id,
        ),
        "state_status_before": state_status_before,
        "state_status_after": item.state_status,
        "counts": {
            "downloaded_files": item.downloaded_files,
            "reused_files": item.reused_files,
            "downloaded_bytes": item.downloaded_bytes,
            "verified_bytes": item.verified_bytes,
        },
        "files": [
            {
                "relative_path": entry.spec.relative_path.as_posix(),
                "url": entry.spec.url,
                "bytes": entry.spec.bytes,
                "md5": entry.spec.md5,
                "sha256": entry.sha256,
                "action": "reused" if entry.reused else "downloaded",
            }
            for entry in files
        ],
    }


def _write_ingest_spec(
    path: Path,
    *,
    models: Mapping[str, PolyHavenModel],
    sync_items: tuple[PolyHavenSyncItem, ...],
) -> None:
    assets: list[dict[str, Any]] = []
    for item in sync_items:
        model = models[item.asset_id]
        dependency_relatives = tuple(
            dependency.relative_to(item.root_dir).as_posix() for dependency in item.dependency_paths
        )
        tags = _ingest_tags(model, dependency_relatives)
        assets.append(
            {
                "asset_id": item.asset_id,
                "name": model.name,
                "normalization": {
                    "source_units": "auto",
                    "source_up_axis": "auto",
                    "source_handedness": "auto",
                    "uniform_scale": 1.0,
                    "pivot_policy": "preserve_source",
                },
                "path": os.path.relpath(item.main_path, path.parent),
                "dependencies": list(dependency_relatives),
                "source": POLYHAVEN_SOURCE,
                "source_id": model.source_id,
                "source_url": POLYHAVEN_ASSET_URL.format(source_id=quote(model.source_id, safe="")),
                "license": POLYHAVEN_LICENSE,
                "license_tier": "open",
                "license_url": POLYHAVEN_LICENSE_URL,
                "attribution": _attribution(model),
                "tags": list(tags),
            }
        )
    payload = yaml.safe_dump(
        {"assets": assets},
        allow_unicode=True,
        sort_keys=False,
        width=100,
    )
    _write_text_atomic(path, payload)


def _ingest_tags(model: PolyHavenModel, dependencies: tuple[str, ...]) -> tuple[str, ...]:
    result = ["polyhaven", "model"]
    texture_suffixes = {".jpg", ".jpeg", ".png", ".exr", ".tif", ".tiff", ".webp"}
    result.append(
        "textured"
        if any(PurePosixPath(path).suffix.casefold() in texture_suffixes for path in dependencies)
        else "untextured"
    )
    for value in (*model.categories, *model.tags):
        normalized = _tag(value)
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _attribution(model: PolyHavenModel) -> str:
    credits = [
        name if credit.casefold() == "all" else f"{name} ({credit})"
        for name, credit in model.authors
    ]
    value = "; ".join(credits) + "; distributed by Poly Haven."
    if len(value) > 1_024:
        raise PolyHavenAcquireError(
            f"model {model.source_id!r} attribution exceeds 1024 characters"
        )
    return value


def _tag(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    result = re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")[:64].rstrip("-")
    return result


def _file_spec(
    relative_path: Path,
    payload: Mapping[str, Any],
    *,
    context: str,
    allow_include: bool = False,
) -> PolyHavenFileSpec:
    _reject_reserved_package_path(relative_path)
    allowed = {"url", "md5", "size"}
    if allow_include:
        allowed.add("include")
    extra = sorted(set(payload) - allowed)
    if extra:
        raise PolyHavenAcquireError(f"{context} contains unsupported key {extra[0]!r}")
    url = _download_url(payload.get("url"), f"{context}.url")
    if unquote(PurePosixPath(urlsplit(url).path).name) != relative_path.name:
        raise PolyHavenPathSecurityError(f"{context}.url filename does not match package path")
    md5 = _string(payload.get("md5"), f"{context}.md5", max_length=32)
    if _MD5_PATTERN.fullmatch(md5) is None:
        raise PolyHavenAcquireError(f"{context}.md5 must be lowercase 32-character MD5")
    size = _positive_int(payload.get("size"), f"{context}.size")
    if size > _MAX_FILE_BYTES:
        raise PolyHavenAcquireError(f"{context}.size exceeds the 32 GiB safety limit")
    return PolyHavenFileSpec(relative_path=relative_path, url=url, bytes=size, md5=md5)


def _reject_reserved_package_path(path: Path) -> None:
    for part in path.parts:
        lowered = part.casefold()
        if part.startswith(".") or lowered in _RESERVED_PACKAGE_NAMES or lowered.endswith(".part"):
            raise PolyHavenPathSecurityError(
                f"Poly Haven package path collides with reserved metadata/temp storage: {path}"
            )


def _acquire_file(
    spec: PolyHavenFileSpec,
    *,
    destination: Path,
    force: bool,
    asset_id: str | None = None,
    runtime: _AcquisitionRuntime | None = None,
) -> _DownloadedFile:
    if runtime is not None and asset_id is None:
        raise PolyHavenAcquireError("runtime-controlled downloads require an asset_id")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.part")
    destination_exists = destination.exists() or destination.is_symlink()
    if destination_exists:
        _require_regular_file(destination, "Poly Haven destination")
    partial_size: int | None = None
    if partial.exists() or partial.is_symlink():
        _require_regular_file(partial, "Poly Haven partial download")
        partial_size = partial.stat().st_size
        if partial_size > spec.bytes:
            if partial_size != spec.bytes + _OVERSIZE_PROBE_BYTES:
                raise PolyHavenAcquireError("Poly Haven partial exceeds the bounded oversize probe")
            if runtime is not None and asset_id is not None:
                runtime.quota.recover_oversized_download(
                    _quota_download_key(asset_id=asset_id, spec=spec),
                    _OVERSIZE_PROBE_BYTES,
                )
            partial.unlink()
            _fsync_directory(partial.parent)
            raise PolyHavenIntegrityError(
                "recovered a previously received oversized Poly Haven response"
            )

    probe_release_is_safe = partial_size is None or partial_size < spec.bytes
    if destination_exists and not force:
        sha256 = _verify_file(destination, spec, context="existing Poly Haven file")
        if runtime is not None and asset_id is not None:
            runtime.quota.finish_download(
                _quota_download_key(asset_id=asset_id, spec=spec),
                release_probe=probe_release_is_safe,
            )
        if partial_size is not None:
            partial.unlink()
            _fsync_directory(partial.parent)
        return _DownloadedFile(
            spec=spec,
            path=destination,
            sha256=sha256,
            reused=True,
            downloaded_bytes=0,
        )
    if force:
        if runtime is not None and asset_id is not None:
            # A crash may have left either a replaced destination or an
            # in-progress forced partial bound to the old reservation. Settle
            # it before --force creates a distinct transfer reservation.
            runtime.quota.finish_download(
                _quota_download_key(asset_id=asset_id, spec=spec),
                release_probe=probe_release_is_safe,
            )
        if partial_size is not None:
            partial.unlink()
            _fsync_directory(partial.parent)
            partial_size = None
    retain_probe_on_close = partial_size == spec.bytes
    downloaded_bytes = 0

    def close_quota_download() -> None:
        nonlocal retain_probe_on_close
        if runtime is not None and asset_id is not None:
            runtime.quota.finish_download(
                _quota_download_key(asset_id=asset_id, spec=spec),
                release_probe=not retain_probe_on_close,
            )
        retain_probe_on_close = False

    def attempt() -> str:
        nonlocal downloaded_bytes, retain_probe_on_close
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > spec.bytes:
            raise PolyHavenAcquireError("Poly Haven partial changed beyond its expected size")
        if offset == spec.bytes:
            # Re-entering with a complete partial means the prior attempt did
            # not durably prove the EOF/sentinel read. Conservatively consume
            # its probe just like a complete partial found after restart.
            retain_probe_on_close = True
        quota_key: str | None = None
        probe_bytes = _OVERSIZE_PROBE_BYTES
        body_bytes_claimed = 0
        body_bytes_received = 0

        def release_unused_body_claim() -> None:
            nonlocal body_bytes_claimed
            unused = body_bytes_claimed - body_bytes_received
            if unused < 0:
                raise PolyHavenAcquireError("download body exceeded its durable claim")
            if runtime is not None:
                runtime.quota.release_download_body_claim(quota_key, unused)
            body_bytes_claimed = body_bytes_received

        if offset < spec.bytes:
            if runtime is not None and asset_id is not None:
                probe_bytes = runtime.quota.download_probe_bytes(
                    asset_id=asset_id,
                    spec=spec,
                )
                probe_bytes = runtime.check_disk_growth(
                    spec.bytes - offset,
                    optional_probe_bytes=probe_bytes,
                )
                quota_key = runtime.quota.begin_download(
                    asset_id=asset_id,
                    spec=spec,
                    maximum_probe_bytes=probe_bytes,
                )
                runtime.quota.claim_download_body_floor(quota_key, offset)
            request = urllib.request.Request(spec.url, headers={"User-Agent": USER_AGENT})
            if offset:
                request.add_header("Range", f"bytes={offset}-")
            body_bytes_claimed = spec.bytes - offset + probe_bytes
            if runtime is not None:
                runtime.quota.claim_download_body(quota_key, body_bytes_claimed)
                runtime.start_request()
                _set_redirect_request_hook(request, runtime.start_request)
            try:
                response = _open_url(
                    request,
                    timeout=300,
                    allowed_hosts=frozenset({_DOWNLOAD_HOST}),
                )
            except urllib.error.HTTPError as exc:
                release_unused_body_claim()
                failure = _http_attempt_failure(exc.code, phase="download", headers=exc.headers)
                exc.close()
                raise failure from exc
            except (OSError, urllib.error.URLError) as exc:
                release_unused_body_claim()
                raise _transport_attempt_failure("download") from exc
            try:
                with response:
                    _validate_response_url(response, expected_host=_DOWNLOAD_HOST)
                    status = _response_status(response)
                    mode = "ab"
                    if offset:
                        if status == 206:
                            _validate_content_range(response, offset=offset, total=spec.bytes)
                            _validate_content_length(
                                response,
                                expected_bytes=spec.bytes - offset,
                                required=probe_bytes == 0,
                            )
                        elif status == 200:
                            _validate_content_length(
                                response,
                                expected_bytes=spec.bytes,
                                required=probe_bytes == 0,
                            )
                            if runtime is not None:
                                runtime.quota.claim_download_body(quota_key, offset)
                            body_bytes_claimed += offset
                            offset = 0
                            mode = "wb"
                        elif status >= 400:
                            raise _http_attempt_failure(
                                status,
                                phase="download",
                                headers=getattr(response, "headers", None),
                            )
                        else:
                            raise PolyHavenIntegrityError(
                                f"Poly Haven resume returned HTTP {status} for {spec.url}"
                            )
                    elif status not in {200, 206}:
                        if status >= 400:
                            raise _http_attempt_failure(
                                status,
                                phase="download",
                                headers=getattr(response, "headers", None),
                            )
                        raise PolyHavenIntegrityError(
                            f"Poly Haven download returned HTTP {status} for {spec.url}"
                        )
                    else:
                        _validate_content_length(
                            response,
                            expected_bytes=spec.bytes,
                            required=probe_bytes == 0,
                        )
                    written = offset
                    with partial.open(mode) as file:
                        while True:
                            remaining = spec.bytes - written
                            # Read at most one sentinel byte beyond the declared
                            # closure when quota headroom reserved that probe.
                            # At an exact hard-quota boundary, stop after the
                            # declared closure instead of receiving an
                            # unaccounted byte from the transport.
                            readable = remaining + probe_bytes
                            if readable == 0:
                                break
                            read_size = min(_HASH_CHUNK_BYTES, readable)
                            chunk = response.read(read_size)
                            if len(chunk) > read_size:
                                excess = len(chunk) - read_size
                                if runtime is not None:
                                    runtime.quota.claim_download_body(quota_key, excess)
                                body_bytes_claimed += excess
                                body_bytes_received += len(chunk)
                                raise PolyHavenIntegrityError(
                                    "Poly Haven response returned more bytes than requested"
                                )
                            body_bytes_received += len(chunk)
                            if not chunk:
                                break
                            downloaded_bytes += len(chunk)
                            if runtime is not None:
                                runtime.record_download_body_bytes(len(chunk))
                            written += len(chunk)
                            if written > spec.bytes:
                                overage = written - spec.bytes
                                # Preserve the single probe byte in the partial
                                # until the ledger atomically records that it was
                                # consumed. If that commit fails, restart can
                                # recognize and settle the debt without network.
                                file.write(chunk)
                                file.flush()
                                os.fsync(file.fileno())
                                if runtime is not None:
                                    runtime.quota.finish_oversized_download(
                                        quota_key,
                                        overage,
                                    )
                                partial.unlink(missing_ok=True)
                                _fsync_directory(partial.parent)
                                raise PolyHavenIntegrityError(
                                    f"Poly Haven download exceeds expected size: {spec.url}"
                                )
                            file.write(chunk)
                        file.flush()
                        os.fsync(file.fileno())
            except _AttemptFailure:
                raise
            except OSError as exc:
                if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
                    raise _ClassifiedItemFailure(
                        AcquisitionFailure(
                            kind=FailureKind.DISK,
                            phase="download_write",
                            message="Poly Haven download storage is exhausted",
                        ),
                        attempts_in_run=1,
                    ) from exc
                raise _transport_attempt_failure("download") from exc
            finally:
                release_unused_body_claim()
        actual_size = partial.stat().st_size if partial.exists() else 0
        if actual_size != spec.bytes:
            raise _AttemptFailure(
                AcquisitionFailure(
                    kind=FailureKind.SHORT_READ,
                    phase="download",
                    message=(
                        f"Poly Haven download size mismatch: expected={spec.bytes} "
                        f"actual={actual_size}"
                    ),
                )
            )
        try:
            return _verify_file(partial, spec, context="Poly Haven download")
        except PolyHavenAcquireError as exc:
            close_quota_download()
            partial.unlink(missing_ok=True)
            _fsync_directory(partial.parent)
            raise _AttemptFailure(
                AcquisitionFailure(
                    kind=FailureKind.INTEGRITY,
                    phase="download",
                    message=str(exc),
                )
            ) from exc

    sha256 = _run_retryable(runtime=runtime, phase="download", operation=attempt)
    partial.replace(destination)
    _fsync_directory(destination.parent)
    close_quota_download()
    return _DownloadedFile(
        spec=spec,
        path=destination,
        sha256=sha256,
        reused=False,
        downloaded_bytes=downloaded_bytes,
    )


def _verify_file(path: Path, spec: PolyHavenFileSpec, *, context: str) -> str:
    size = path.stat().st_size
    if size != spec.bytes:
        raise PolyHavenAcquireError(
            f"{context} size mismatch: {path} expected={spec.bytes} actual={size}"
        )
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_HASH_CHUNK_BYTES):
            md5.update(chunk)
            sha256.update(chunk)
    actual_md5 = md5.hexdigest()
    if actual_md5 != spec.md5:
        raise PolyHavenAcquireError(
            f"{context} md5 mismatch: {path} expected={spec.md5} actual={actual_md5}"
        )
    return sha256.hexdigest()


def _run_retryable(
    *,
    runtime: _AcquisitionRuntime | None,
    phase: str,
    operation: Any,
) -> Any:
    if runtime is not None:
        return runtime.run_with_retries(phase=phase, operation=operation)
    try:
        return operation()
    except _AttemptFailure as exc:
        raise _ClassifiedItemFailure(
            exc.failure,
            attempts_in_run=1,
            exhausted=False,
        ) from exc


def _set_redirect_request_hook(
    request: urllib.request.Request,
    hook: Callable[[], None],
) -> None:
    setattr(request, _REDIRECT_HOOK_ATTRIBUTE, hook)


def _response_header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if isinstance(value, str):
            return value
    items = getattr(headers, "items", None)
    if callable(items):
        for key, value in items():
            if (
                isinstance(key, str)
                and key.casefold() == name.casefold()
                and isinstance(value, str)
            ):
                return value
    return None


def _validate_content_length(
    response: Any,
    *,
    expected_bytes: int,
    required: bool = False,
) -> None:
    if not isinstance(required, bool):
        raise PolyHavenAcquireError("Content-Length requirement flag must be boolean")
    headers = getattr(response, "headers", None)
    content_lengths = _response_header_values(headers, "Content-Length")
    transfer_encodings = _response_header_values(headers, "Transfer-Encoding")
    if content_lengths and transfer_encodings:
        raise PolyHavenIntegrityError(
            "Poly Haven response has ambiguous Content-Length and Transfer-Encoding framing"
        )
    if not content_lengths:
        if required:
            raise PolyHavenIntegrityError(
                "Poly Haven response requires an exact Content-Length without an oversize probe"
            )
        return
    if len(content_lengths) != 1:
        raise PolyHavenIntegrityError("Poly Haven response Content-Length is ambiguous")
    value = content_lengths[0]
    if len(value) > 20 or re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        raise PolyHavenIntegrityError("Poly Haven response Content-Length is invalid")
    if int(value) != expected_bytes:
        raise PolyHavenIntegrityError(
            "Poly Haven response Content-Length differs from its expected body"
        )


def _response_header_values(headers: Any, name: str) -> tuple[str, ...]:
    if headers is None:
        return ()
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all(name)
        if values is not None:
            if not isinstance(values, list | tuple) or any(
                not isinstance(value, str) for value in values
            ):
                raise PolyHavenIntegrityError("Poly Haven response headers are invalid")
            return tuple(values)
    items = getattr(headers, "items", None)
    if callable(items):
        matches: list[str] = []
        for key, value in items():
            if isinstance(key, str) and key.casefold() == name.casefold():
                if not isinstance(value, str):
                    raise PolyHavenIntegrityError("Poly Haven response headers are invalid")
                matches.append(value)
        return tuple(matches)
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is None:
            return ()
        if not isinstance(value, str):
            raise PolyHavenIntegrityError("Poly Haven response headers are invalid")
        return (value,)
    raise PolyHavenIntegrityError("Poly Haven response headers are invalid")


def _http_attempt_failure(status: int, *, phase: str, headers: Any) -> _AttemptFailure:
    if not 400 <= status <= 599:
        raise PolyHavenAcquireError(f"Poly Haven {phase} returned unexpected HTTP {status}")
    try:
        failure = AcquisitionFailure.from_http(
            phase=phase,
            status=status,
            message=f"Poly Haven {phase} returned HTTP {status}",
        )
    except RuntimeValidationError as exc:
        raise PolyHavenAcquireError(f"Poly Haven {phase} HTTP failure is invalid") from exc
    return _AttemptFailure(
        failure,
        retry_after_value=_response_header(headers, "Retry-After"),
    )


def _transport_attempt_failure(phase: str) -> _AttemptFailure:
    return _AttemptFailure(
        AcquisitionFailure(
            kind=FailureKind.TRANSPORT,
            phase=phase,
            message=f"cannot complete Poly Haven {phase} request",
        )
    )


def _fetch_json(url: str, *, runtime: _AcquisitionRuntime | None = None) -> dict[str, Any]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _API_HOST
        or parsed.username
        or parsed.password
    ):
        raise PolyHavenAcquireError(f"unapproved Poly Haven API URL: {url}")

    def attempt() -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        if runtime is not None:
            runtime.start_request()
            _set_redirect_request_hook(request, runtime.start_request)
        try:
            response = _open_url(request, timeout=60, allowed_hosts=frozenset({_API_HOST}))
        except urllib.error.HTTPError as exc:
            failure = _http_attempt_failure(exc.code, phase="api", headers=exc.headers)
            exc.close()
            raise failure from exc
        except (OSError, urllib.error.URLError) as exc:
            raise _transport_attempt_failure("api") from exc
        try:
            with response:
                _validate_response_url(response, expected_host=_API_HOST)
                status = _response_status(response)
                if status != 200:
                    if status >= 400:
                        raise _http_attempt_failure(
                            status,
                            phase="api",
                            headers=getattr(response, "headers", None),
                        )
                    raise PolyHavenAcquireError(
                        f"Poly Haven API returned unexpected HTTP {status}: {url}"
                    )
                return response.read(_MAX_API_JSON_BYTES + 1)
        except _AttemptFailure:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise _transport_attempt_failure("api") from exc

    payload = _run_retryable(runtime=runtime, phase="api", operation=attempt)
    if len(payload) > _MAX_API_JSON_BYTES:
        raise PolyHavenAcquireError(f"Poly Haven API response exceeds 64 MiB: {url}")
    try:
        decoded = payload.decode("utf-8")
        result = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PolyHavenAcquireError(f"Poly Haven API returned invalid JSON: {url}") from exc
    return _object(result, f"Poly Haven API response {url}")


def _download_url(value: Any, context: str) -> str:
    url = _string(value, context, max_length=4_096)
    parsed = _require_https_host(url, allowed_hosts=frozenset({_DOWNLOAD_HOST}), context=context)
    if parsed.query or parsed.fragment:
        raise PolyHavenPathSecurityError(f"{context}: unapproved Poly Haven download URL")
    return url


def _open_url(
    request: urllib.request.Request,
    *,
    timeout: int,
    allowed_hosts: frozenset[str],
) -> Any:
    _require_https_host(request.full_url, allowed_hosts=allowed_hosts, context="request URL")
    opener = urllib.request.build_opener(_AllowlistRedirectHandler(allowed_hosts))
    return opener.open(request, timeout=timeout)


def _require_https_host(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    context: str,
) -> Any:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise PolyHavenPathSecurityError(f"{context}: invalid URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username
        or parsed.password
        or port is not None
    ):
        raise PolyHavenPathSecurityError(f"{context}: unapproved HTTPS host")
    return parsed


def _validate_response_url(response: Any, *, expected_host: str) -> None:
    geturl = getattr(response, "geturl", None)
    if not callable(geturl):
        return
    final_url = geturl()
    if not isinstance(final_url, str):
        raise PolyHavenPathSecurityError("Poly Haven response final URL is invalid")
    parsed = urlsplit(final_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise PolyHavenPathSecurityError(
            f"Poly Haven response redirected to an invalid URL: {final_url}"
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.username
        or parsed.password
        or port is not None
    ):
        raise PolyHavenPathSecurityError(
            f"Poly Haven response redirected to an unapproved host: {final_url}"
        )


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else 200
    if isinstance(status, bool) or not isinstance(status, int):
        raise PolyHavenIntegrityError("Poly Haven response has an invalid HTTP status")
    return status


def _validate_content_range(response: Any, *, offset: int, total: int) -> None:
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Range") if headers is not None else None
    if not isinstance(value, str):
        raise PolyHavenIntegrityError("Poly Haven resume response has no Content-Range")
    match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", value)
    if match is None:
        raise PolyHavenIntegrityError("Poly Haven resume Content-Range is invalid")
    start, end, received_total = (int(part) for part in match.groups())
    if start != offset or received_total != total or end < start or end >= total:
        raise PolyHavenIntegrityError("Poly Haven resume Content-Range does not match request")


def _assert_prepared_inputs_unchanged(
    *,
    project_root: Path,
    manifest_path: Path,
    expected_running_manifest: dict[str, Any],
    generated_spec_path: Path | None,
    generated_spec_evidence: dict[str, Any] | None,
    items: tuple[PolyHavenSyncItem, ...],
) -> None:
    if _read_json_object_strict(manifest_path, "running Poly Haven manifest") != (
        expected_running_manifest
    ):
        raise PolyHavenAcquireError("running Poly Haven manifest changed before prepare")
    if generated_spec_path is None:
        if generated_spec_evidence is not None or items:
            raise PolyHavenAcquireError("no-change Poly Haven spec evidence is inconsistent")
        return
    if generated_spec_evidence is None:
        raise PolyHavenAcquireError("prepared Poly Haven run lacks spec evidence")
    checked_spec = _checked_project_file(
        generated_spec_path,
        project_root=project_root,
        context="generated Poly Haven IngestSpec",
    )
    if generated_spec_evidence.get("path") != _portable_path(
        checked_spec, project_root
    ) or generated_spec_evidence.get("file_sha256") != _sha256_file(checked_spec):
        raise PolyHavenAcquireError("generated Poly Haven IngestSpec changed before prepare")
    spec = load_ingest_spec(checked_spec)
    if tuple(asset.asset_id for asset in spec.assets) != tuple(item.asset_id for item in items):
        raise PolyHavenAcquireError("generated IngestSpec asset cohort changed before prepare")
    for item, asset in zip(items, spec.assets, strict=True):
        _assert_sync_item_files(item=item, project_root=project_root)
        if asset.path != item.main_path.resolve() or asset.dependencies != tuple(
            path.relative_to(item.root_dir) for path in item.dependency_paths
        ):
            raise PolyHavenAcquireError(f"generated IngestSpec paths changed for {item.asset_id!r}")


def _assert_finalization_inputs_unchanged(
    *,
    result: PolyHavenSyncResult,
    run_manifest: dict[str, Any],
    state: dict[str, Any],
    project_root: Path,
) -> None:
    _validate_prepared_manifest_receipt(
        manifest=run_manifest,
        state=state,
        project_root=project_root,
        require_current_state_receipt=False,
    )
    listing = _object(run_manifest.get("listing"), "run manifest listing")
    counts = _object(run_manifest.get("counts"), "run manifest counts")
    if (
        listing.get("discovered") != result.discovered
        or listing.get("payload_sha256") != result.snapshot_sha256
        or counts.get("selected") != result.selected
        or counts.get("downloaded_files") != result.downloaded_files
        or counts.get("reused_files") != result.reused_files
        or counts.get("downloaded_bytes") != result.downloaded_bytes
        or counts.get("verified_bytes") != result.verified_bytes
    ):
        raise PolyHavenAcquireError("Poly Haven result and prepared receipt accounting differ")
    state_receipt = _object(run_manifest.get("state"), "run manifest state")
    if state_receipt.get("path") != _portable_path(result.state_path, project_root):
        raise PolyHavenAcquireError("Poly Haven result and prepared state paths differ")
    generated = _exact_object(
        run_manifest.get("generated_ingest_spec"),
        {"path", "file_sha256"},
        "run manifest generated_ingest_spec",
    )
    if result.generated_spec_path is None:
        raise PolyHavenAcquireError("finalization result has no generated IngestSpec")
    checked_spec = _checked_project_file(
        result.generated_spec_path,
        project_root=project_root,
        context="generated Poly Haven IngestSpec",
    )
    if generated["path"] != _portable_path(checked_spec, project_root) or generated[
        "file_sha256"
    ] != _sha256_file(checked_spec):
        raise PolyHavenAcquireError("generated Poly Haven IngestSpec changed before finalization")
    raw_run_items = run_manifest.get("items")
    if not isinstance(raw_run_items, list):
        raise PolyHavenAcquireError("Poly Haven run manifest items must be a list")
    run_items: dict[str, dict[str, Any]] = {}
    for raw in raw_run_items:
        if not isinstance(raw, dict) or not isinstance(raw.get("asset_id"), str):
            raise PolyHavenAcquireError("Poly Haven run manifest item is invalid")
        if raw["asset_id"] in run_items:
            raise PolyHavenAcquireError("Poly Haven run manifest has duplicate asset ids")
        run_items[raw["asset_id"]] = raw
    result_items = {item.asset_id: item for item in result.items}
    if set(run_items) != set(result_items):
        raise PolyHavenAcquireError("Poly Haven result and run manifest cohorts differ")
    spec = load_ingest_spec(checked_spec)
    spec_items = {asset.asset_id: asset for asset in spec.assets}
    if set(spec_items) != set(result_items):
        raise PolyHavenAcquireError("Poly Haven result and generated spec cohorts differ")
    for asset_id, item in result_items.items():
        run_item = run_items[asset_id]
        state_item = state["items"].get(asset_id)
        if not isinstance(state_item, dict):
            raise PolyHavenAcquireError(f"Poly Haven state lost item {asset_id!r}")
        expected_prepare_token = _prepare_token(
            asset_id=item.asset_id,
            source_id=item.source_id,
            revision=item.revision,
            metadata_file_sha256=_sha256_file(item.metadata_path),
            source_bundle_sha256=item.source_bundle_sha256,
            source_content_sha256=item.source_content_sha256,
            run_id=result.run_dir.name,
        )
        raw_run_files = run_item.get("files")
        if not isinstance(raw_run_files, list):
            raise PolyHavenAcquireError(f"Poly Haven run files are invalid for {asset_id!r}")
        state_projection_keys = {
            "asset_id",
            "source_id",
            "revision",
            "date_published",
            "root_dir",
            "main_path",
            "metadata_path",
            "metadata_file_sha256",
            "source_bundle_sha256",
            "source_content_sha256",
            "files",
            "acquired_at",
            "verified_at",
            "last_prepared_at",
            "last_run_id",
            "prepare_token",
            "prepared_manifest_payload_sha256",
            "migration_pending",
        }
        expected_state_projection = {
            "asset_id": asset_id,
            "source_id": run_item.get("source_id"),
            "revision": run_item.get("revision"),
            "date_published": run_item.get("date_published"),
            "root_dir": run_item.get("root_dir"),
            "main_path": run_item.get("main_path"),
            "metadata_path": run_item.get("metadata_path"),
            "metadata_file_sha256": run_item.get("metadata_file_sha256"),
            "source_bundle_sha256": run_item.get("source_bundle_sha256"),
            "source_content_sha256": run_item.get("source_content_sha256"),
            "files": [
                {key: raw_file.get(key) for key in ("relative_path", "bytes", "md5", "sha256")}
                for raw_file in raw_run_files
                if isinstance(raw_file, dict)
            ],
            "acquired_at": run_item.get("acquired_at"),
            "verified_at": run_item.get("verified_at"),
            "last_prepared_at": run_manifest.get("completed_at"),
            "last_run_id": result.run_dir.name,
            "prepare_token": expected_prepare_token,
            "prepared_manifest_payload_sha256": run_manifest.get("prepare_receipt_sha256"),
            "migration_pending": False,
        }
        observed_state_projection = {key: state_item.get(key) for key in state_projection_keys}
        if (
            run_item.get("source_id") != item.source_id
            or run_item.get("revision") != item.revision
            or run_item.get("prepare_token") != expected_prepare_token
            or run_item.get("root_dir") != _portable_path(item.root_dir, project_root)
            or run_item.get("main_path") != _portable_path(item.main_path, project_root)
            or run_item.get("metadata_path") != _portable_path(item.metadata_path, project_root)
            or run_item.get("acquired_at") != item.acquired_at
            or run_item.get("verified_at") != item.verified_at
            or run_item.get("metadata_file_sha256") != _sha256_file(item.metadata_path)
            or run_item.get("source_bundle_sha256") != item.source_bundle_sha256
            or run_item.get("source_content_sha256") != item.source_content_sha256
            or _payload_sha256(observed_state_projection)
            != _payload_sha256(expected_state_projection)
        ):
            raise PolyHavenAcquireError(f"Poly Haven prepare CAS failed for {asset_id!r}")
        _assert_sync_item_files(item=item, project_root=project_root)
        asset = spec_items[asset_id]
        if (
            asset.path != item.main_path.resolve()
            or asset.dependencies
            != tuple(path.relative_to(item.root_dir) for path in item.dependency_paths)
            or asset.source != POLYHAVEN_SOURCE
            or asset.source_id != item.source_id
            or asset.source_url
            != POLYHAVEN_ASSET_URL.format(source_id=quote(item.source_id, safe=""))
            or asset.license != POLYHAVEN_LICENSE
            or asset.license_tier != "open"
            or asset.license_url != POLYHAVEN_LICENSE_URL
        ):
            raise PolyHavenAcquireError(f"generated IngestSpec provenance changed for {asset_id!r}")


def _assert_sync_item_files(*, item: PolyHavenSyncItem, project_root: Path) -> None:
    checked_root = item.root_dir.resolve()
    try:
        checked_root.relative_to(project_root)
    except ValueError as exc:
        raise PolyHavenAcquireError(f"Poly Haven item {item.asset_id!r} root escaped") from exc
    metadata = _read_json_object_strict(item.metadata_path, "Poly Haven metadata")
    if (
        metadata.get("schema_version") != 2
        or metadata.get("asset_id") != item.asset_id
        or metadata.get("source_id") != item.source_id
        or metadata.get("revision") != item.revision
        or metadata.get("acquired_at") != item.acquired_at
        or metadata.get("verified_at") != item.verified_at
    ):
        raise PolyHavenAcquireError(f"Poly Haven metadata changed for {item.asset_id!r}")
    raw_files = metadata.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise PolyHavenAcquireError(f"Poly Haven metadata files are invalid for {item.asset_id!r}")
    relative_files: list[Path] = []
    for raw in raw_files:
        file_payload = _object(raw, "Poly Haven metadata file")
        relative = _relative_path(file_payload.get("relative_path"), "metadata relative_path")
        _reject_reserved_package_path(relative)
        path = _safe_existing_file(checked_root, relative, "acquired Poly Haven file")
        spec = PolyHavenFileSpec(
            relative_path=relative,
            url=_download_url(file_payload.get("url"), "metadata file URL"),
            bytes=_positive_int(file_payload.get("bytes"), "metadata file bytes"),
            md5=_string(file_payload.get("md5"), "metadata file md5", max_length=32),
        )
        if _verify_file(path, spec, context="acquired Poly Haven file") != file_payload.get(
            "sha256"
        ):
            raise PolyHavenAcquireError(f"Poly Haven file SHA changed for {item.asset_id!r}")
        relative_files.append(relative)
    expected_paths = {item.main_path.resolve(), *(path.resolve() for path in item.dependency_paths)}
    observed_paths = {(checked_root / path).resolve() for path in relative_files}
    if expected_paths != observed_paths:
        raise PolyHavenAcquireError(f"Poly Haven file cohort changed for {item.asset_id!r}")
    sorted_files = tuple(sorted(relative_files, key=lambda path: path.as_posix()))
    if (
        bundle_sha256(checked_root, sorted_files) != item.source_bundle_sha256
        or content_sha256(checked_root, sorted_files) != item.source_content_sha256
    ):
        raise PolyHavenAcquireError(f"Poly Haven source hashes changed for {item.asset_id!r}")
    observed_dependencies = gltf_dependency_paths(item.main_path)
    expected_dependencies = tuple(path.relative_to(checked_root) for path in item.dependency_paths)
    if observed_dependencies != expected_dependencies:
        raise PolyHavenAcquireError(f"Poly Haven glTF closure changed for {item.asset_id!r}")


def _derive_terminal_statuses(
    *,
    result: PolyHavenSyncResult,
    run_manifest: dict[str, Any],
    batch: dict[str, Any],
    batch_manifest_path: Path,
    project_root: Path,
) -> dict[str, dict[str, Any]]:
    rows = _validated_batch_rows(batch)
    if result.generated_spec_path is None:
        raise PolyHavenAcquireError("downstream batch cannot bind a missing generated spec")
    source_manifest = _checked_project_file(
        Path(_string(batch.get("source_manifest"), "batch source_manifest", max_length=4_096)),
        project_root=project_root,
        context="downstream source manifest",
    )
    if source_manifest != result.generated_spec_path.resolve():
        raise PolyHavenAcquireError("downstream batch source_manifest is not the generated spec")
    generated = _object(run_manifest.get("generated_ingest_spec"), "generated spec evidence")
    if generated.get("file_sha256") != _sha256_file(source_manifest):
        raise PolyHavenAcquireError("downstream generated spec hash is stale")
    result_items = {item.asset_id: item for item in result.items}
    if set(rows) != set(result_items):
        raise PolyHavenAcquireError("downstream batch asset cohort differs from acquisition")
    catalog_path = _checked_project_file(
        Path(_string(batch.get("catalog"), "batch catalog", max_length=4_096)),
        project_root=project_root,
        context="downstream catalog",
    )
    catalog = Catalog(catalog_path, project_root=project_root)
    spec = load_ingest_spec(source_manifest)
    spec_items = {asset.asset_id: asset for asset in spec.assets}
    result_evidence: dict[str, dict[str, Any]] = {}
    batch_file_sha256 = _sha256_file(batch_manifest_path)
    data_dir = result.state_path.resolve().parents[2]
    for asset_id, item in result_items.items():
        row = rows[asset_id]
        batch_status = row["status"]
        if batch_status == "failed":
            if not isinstance(row.get("error"), dict):
                raise PolyHavenAcquireError(f"failed downstream item {asset_id!r} lacks error")
            continue
        if batch_status not in _TERMINAL_STATUSES:
            raise PolyHavenAcquireError(
                f"downstream status {batch_status!r} has no audited terminal schema"
            )
        if row.get("error") is not None:
            raise PolyHavenAcquireError(f"terminal downstream item {asset_id!r} contains error")
        if (
            row.get("bundle_sha256") != item.source_bundle_sha256
            or row.get("content_sha256") != item.source_content_sha256
        ):
            raise PolyHavenAcquireError(f"downstream source hashes differ for {asset_id!r}")
        catalog_status = row.get("catalog_status")
        if batch_status == "imported" and catalog_status != "imported":
            raise PolyHavenAcquireError(
                f"imported downstream status is inconsistent for {asset_id}"
            )
        if batch_status == "render_ok" and catalog_status != "render_ok":
            raise PolyHavenAcquireError(
                f"render_ok downstream status is inconsistent for {asset_id}"
            )
        if batch_status == "skipped" and catalog_status not in {"imported", "render_ok"}:
            raise PolyHavenAcquireError(f"skipped downstream status is inconsistent for {asset_id}")
        raw_receipt = _verify_staged_raw(
            asset_id=asset_id,
            item=item,
            raw_path_value=row.get("raw_path"),
            project_root=project_root,
            data_dir=data_dir,
        )
        catalog_receipt = _verify_catalog_terminal_evidence(
            catalog=catalog,
            catalog_path=catalog_path,
            asset_id=asset_id,
            item=item,
            asset_spec=spec_items[asset_id],
            catalog_status=str(catalog_status),
            batch_status=str(batch_status),
            batch_row=row,
            project_root=project_root,
            data_dir=data_dir,
        )
        receipt = {
            "asset_id": asset_id,
            "source_id": item.source_id,
            "revision": item.revision,
            "status": batch_status,
            "source_bundle_sha256": item.source_bundle_sha256,
            "source_content_sha256": item.source_content_sha256,
            "generated_spec_file_sha256": generated["file_sha256"],
            "batch_manifest": _portable_path(batch_manifest_path, project_root),
            "batch_manifest_file_sha256": batch_file_sha256,
            "batch_item": row,
            "raw": raw_receipt,
            "catalog": catalog_receipt,
        }
        result_evidence[asset_id] = {
            "status": batch_status,
            "receipt": receipt,
            "terminal_evidence_sha256": _domain_payload_sha256(
                b"uefactory.polyhaven-terminal-evidence.v1\0", receipt
            ),
        }
    return result_evidence


def _journal_downstream_outcomes(
    *,
    items: tuple[PolyHavenSyncItem, ...],
    batch: Mapping[str, Any],
    batch_file_sha256: str,
    project_root: Path,
    data_dir: Path,
    run_id: str,
    resolution: str,
    policy: CrossRunFailurePolicy,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    journal_path = data_dir / "acquire/polyhaven/failure_journal.json"
    _reject_symlink_components(
        journal_path,
        project_root=project_root,
        context="Poly Haven downstream failure journal",
    )
    loaded = _load_failure_journal(journal_path)
    if not loaded.before_exists:
        raise PolyHavenAcquireError("prepared Poly Haven run has no failure journal")
    journal_payload = loaded.payload
    rows = _validated_batch_rows(dict(batch))
    event_refs: list[dict[str, Any]] = []
    failure_events: list[dict[str, Any]] = []
    for ordinal, item in enumerate(items, start=1):
        row = rows[item.asset_id]
        attempt_id = f"{run_id}:finalize:{ordinal}:{batch_file_sha256}"
        event: dict[str, Any] | None = None
        if row["status"] == "failed":
            error = _object(row.get("error"), f"downstream error for {item.asset_id}")
            error_type = _string(
                error.get("type"),
                f"downstream error type for {item.asset_id}",
                max_length=128,
            )
            message = _string(
                error.get("message"),
                f"downstream error message for {item.asset_id}",
                max_length=2_048,
            )
            raw_phase = error.get("phase", "downstream")
            phase = _string(
                raw_phase,
                f"downstream error phase for {item.asset_id}",
                max_length=64,
            )
            kind = (
                FailureKind.QUALITY
                if error_type == "IngestQualityError"
                else FailureKind.DOWNSTREAM
            )
            try:
                updated, event = append_failure_event(
                    journal_payload,
                    source=POLYHAVEN_SOURCE,
                    asset_type="models",
                    asset_id=item.asset_id,
                    source_id=item.source_id,
                    revision=item.revision,
                    resolution=resolution,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    failure=AcquisitionFailure(
                        kind=kind,
                        phase=phase,
                        message=message,
                    ),
                    recorded_at=_next_failure_journal_datetime(journal_payload),
                    policy=policy,
                    attempts_in_run=1,
                )
            except FailureJournalError as exc:
                raise PolyHavenAcquireError(
                    f"cannot journal downstream outcome for {item.asset_id}: {exc}"
                ) from exc
            journal_payload = updated
            failure_events.append(event)
        else:
            active = validate_failure_journal(
                journal_payload,
                source=POLYHAVEN_SOURCE,
                asset_type="models",
            ).get(item.asset_id)
            if active is not None:
                try:
                    updated, event = append_resolution_event(
                        journal_payload,
                        source=POLYHAVEN_SOURCE,
                        asset_type="models",
                        asset_id=item.asset_id,
                        source_id=item.source_id,
                        revision=item.revision,
                        resolution=resolution,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        recorded_at=_next_failure_journal_datetime(journal_payload),
                    )
                except FailureJournalError as exc:
                    raise PolyHavenAcquireError(
                        f"cannot resolve downstream outcome for {item.asset_id}: {exc}"
                    ) from exc
                journal_payload = updated
        if event is not None:
            event_refs.append(_failure_event_ref(event))
    if journal_payload != loaded.payload:
        # Validate the entire downstream cohort before one atomic journal
        # commit; an invalid later row must not leave earlier orphan events.
        _persist_failure_journal(journal_path, journal_payload)
    return (
        {
            "path": _portable_path(journal_path, project_root),
            "before": {
                "exists": loaded.before_exists,
                "file_sha256": loaded.before_file_sha256,
                "payload_sha256": loaded.before_payload_sha256,
                "event_count": len(loaded.payload["events"]),
                "head_event_sha256": loaded.payload["head_event_sha256"],
            },
            "after": {
                "exists": True,
                "file_sha256": _sha256_file(journal_path),
                "payload_sha256": _payload_sha256(journal_payload),
                "event_count": len(journal_payload["events"]),
                "head_event_sha256": journal_payload["head_event_sha256"],
            },
            "event_refs": event_refs,
        },
        failure_events,
    )


def _validated_batch_rows(batch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    expected_batch_keys = {
        "schema_version",
        "status",
        "source_manifest",
        "catalog",
        "assets",
        "report",
        "report_error",
    }
    if set(batch) != expected_batch_keys or batch.get("schema_version") != 1:
        raise PolyHavenAcquireError("downstream batch manifest has an unsupported shape")
    if batch.get("status") not in {"ok", "failed"}:
        raise PolyHavenAcquireError("downstream batch status is invalid")
    raw_rows = batch.get("assets")
    if not isinstance(raw_rows, list):
        raise PolyHavenAcquireError("downstream batch assets must be a list")
    rows: dict[str, dict[str, Any]] = {}
    row_keys = {
        "asset_id",
        "status",
        "bundle_sha256",
        "content_sha256",
        "raw_path",
        "ingest_manifest",
        "thumbnail_manifest",
        "catalog_status",
        "error",
    }
    for raw_row in raw_rows:
        row = _exact_object(raw_row, row_keys, "downstream batch asset")
        asset_id = _string(row["asset_id"], "downstream asset_id", max_length=64)
        if asset_id in rows:
            raise PolyHavenAcquireError("downstream batch has duplicate asset ids")
        rows[asset_id] = row
    return rows


def _verify_staged_raw(
    *,
    asset_id: str,
    item: PolyHavenSyncItem,
    raw_path_value: Any,
    project_root: Path,
    data_dir: Path,
) -> dict[str, Any]:
    raw_path = _checked_project_file(
        Path(_string(raw_path_value, "batch raw_path", max_length=4_096)),
        project_root=project_root,
        context=f"staged raw file for {asset_id}",
    )
    expected_raw_path = (data_dir / "raw/local" / asset_id / item.main_path.name).resolve()
    if raw_path != expected_raw_path:
        raise PolyHavenAcquireError(f"downstream raw_path is not canonical for {asset_id!r}")
    root = raw_path.parent
    relative_files = (
        Path(item.main_path.name),
        *(path.relative_to(item.root_dir) for path in item.dependency_paths),
    )
    observed = _regular_tree_files(root)
    if observed != tuple(sorted(relative_files, key=lambda path: path.as_posix())):
        raise PolyHavenAcquireError(f"staged raw closure differs for {asset_id!r}")
    if gltf_dependency_paths(raw_path) != tuple(relative_files[1:]):
        raise PolyHavenAcquireError(f"staged glTF dependency closure differs for {asset_id!r}")
    bundle_hash = bundle_sha256(root, relative_files)
    content_hash = content_sha256(root, relative_files)
    if bundle_hash != item.source_bundle_sha256 or content_hash != item.source_content_sha256:
        raise PolyHavenAcquireError(f"staged raw hashes differ for {asset_id!r}")
    return {
        "path": _portable_path(raw_path, project_root),
        "bundle_sha256": bundle_hash,
        "content_sha256": content_hash,
        "files": [path.as_posix() for path in relative_files],
    }


def _verify_catalog_terminal_evidence(
    *,
    catalog: Catalog,
    catalog_path: Path,
    asset_id: str,
    item: PolyHavenSyncItem,
    asset_spec: Any,
    catalog_status: str,
    batch_status: str,
    batch_row: dict[str, Any],
    project_root: Path,
    data_dir: Path,
) -> dict[str, Any]:
    record = catalog.get_asset(asset_id)
    expected_raw = _portable_path(
        data_dir / "raw/local" / asset_id / item.main_path.name,
        project_root,
    )
    if (
        record is None
        or record.asset_id != asset_id
        or record.source != POLYHAVEN_SOURCE
        or record.source_id != item.source_id
        or record.source_url != POLYHAVEN_ASSET_URL.format(source_id=quote(item.source_id, safe=""))
        or record.license != POLYHAVEN_LICENSE
        or record.license_tier != "open"
        or record.license_url != POLYHAVEN_LICENSE_URL
        or record.raw_path != expected_raw
        or record.sha256 != item.source_content_sha256
        or record.status != catalog_status
        or record.error is not None
        or not isinstance(record.ue_package_path, str)
        or record.tri_count is None
        or record.tri_count <= 0
        or record.material_count is None
        or record.material_count < 0
    ):
        raise PolyHavenAcquireError(f"catalog asset evidence is invalid for {asset_id!r}")
    artifacts = catalog.list_artifacts(asset_id=asset_id)
    import_artifacts = [artifact for artifact in artifacts if artifact.kind == "import_manifest"]
    if len(import_artifacts) != 1:
        raise PolyHavenAcquireError(f"catalog import artifact is ambiguous for {asset_id!r}")
    import_artifact = import_artifacts[0]
    import_path = _checked_project_file(
        Path(import_artifact.path),
        project_root=project_root,
        context=f"import manifest for {asset_id}",
    )
    if import_artifact.sha256 is None or _sha256_file(import_path) != import_artifact.sha256:
        raise PolyHavenAcquireError(f"import artifact hash is invalid for {asset_id!r}")
    provided_import = batch_row.get("ingest_manifest")
    provided_thumbnail = batch_row.get("thumbnail_manifest")
    if batch_status == "skipped":
        if provided_import is not None or provided_thumbnail is not None:
            raise PolyHavenAcquireError(
                f"skipped downstream row contains direct artifacts for {asset_id!r}"
            )
    elif batch_status == "imported":
        if provided_import is None or provided_thumbnail is not None:
            raise PolyHavenAcquireError(
                f"imported downstream row artifact fields are invalid for {asset_id!r}"
            )
    elif batch_status == "render_ok":
        if provided_import is None or provided_thumbnail is None:
            raise PolyHavenAcquireError(
                f"render_ok downstream row lacks direct artifacts for {asset_id!r}"
            )
    else:
        raise PolyHavenAcquireError(f"unaudited downstream status for {asset_id!r}")
    if (
        provided_import is not None
        and _checked_project_file(
            Path(_string(provided_import, "batch ingest_manifest", max_length=4_096)),
            project_root=project_root,
            context="batch ingest manifest",
        )
        != import_path
    ):
        raise PolyHavenAcquireError(f"batch ingest manifest differs for {asset_id!r}")
    params = import_artifact.params
    package_evidence = params.get("ue_package_bundle")
    if (
        params.get("schema_version") != 2
        or params.get("bundle_sha256") != item.source_bundle_sha256
        or params.get("content_sha256") != item.source_content_sha256
        or params.get("source_format") != "gltf"
        or params.get("requested_normalization") != asset_spec.normalization.as_dict()
        or not isinstance(package_evidence, dict)
    ):
        raise PolyHavenAcquireError(f"import artifact provenance is invalid for {asset_id!r}")
    import_manifest = _read_json_object_strict(import_path, "catalog import manifest")
    imported_paths = import_manifest.get("imported_object_paths")
    transaction = import_manifest.get("transaction")
    finalize_validation = import_manifest.get("finalize_validation")
    require_textures = "textured" in asset_spec.tags
    if (
        import_manifest.get("schema_version") != IMPORT_MANIFEST_SCHEMA_VERSION
        or import_manifest.get("status") != "ok"
        or import_manifest.get("asset_id") != asset_id
        or import_manifest.get("bundle_sha256") != item.source_bundle_sha256
        or import_manifest.get("content_sha256") != item.source_content_sha256
        or import_manifest.get("requested_normalization") != asset_spec.normalization.as_dict()
        or not isinstance(transaction, dict)
        or transaction.get("state") != "committed"
        or not isinstance(finalize_validation, dict)
        or finalize_validation.get("status") != "ok"
        or not is_current_passed_quality(
            import_manifest.get("quality"),
            require_single_static_mesh=True,
            require_texture_references=require_textures,
        )
        or not isinstance(imported_paths, list)
        or not imported_paths
        or any(not isinstance(path, str) for path in imported_paths)
        or package_evidence != import_manifest.get("ue_package_bundle")
        or not is_valid_package_bundle_evidence(
            project_root,
            asset_id=asset_id,
            imported_object_paths=imported_paths,
            evidence=package_evidence,
        )
    ):
        raise PolyHavenAcquireError(f"import manifest evidence is invalid for {asset_id!r}")
    meshes = import_manifest.get("static_meshes")
    if not isinstance(meshes, list) or len(meshes) != 1 or not isinstance(meshes[0], dict):
        raise PolyHavenAcquireError(f"import mesh evidence is invalid for {asset_id!r}")
    mesh = meshes[0]
    if (
        mesh.get("object_path") != record.ue_package_path
        or mesh.get("triangle_count") != record.tri_count
        or mesh.get("material_count") != record.material_count
    ):
        raise PolyHavenAcquireError(f"catalog mesh evidence differs for {asset_id!r}")
    package_bundle_hash = package_evidence.get("package_bundle_sha256")
    _sha256_value(package_bundle_hash, "UE package bundle hash")
    thumbnail_receipt: dict[str, Any] | None = None
    if catalog_status == "render_ok":
        thumbnail_receipt = _verify_thumbnail_evidence(
            artifacts=artifacts,
            asset_id=asset_id,
            item=item,
            import_artifact_path=import_artifact.path,
            import_path=import_path,
            package_bundle_sha256=str(package_bundle_hash),
            batch_thumbnail=provided_thumbnail,
            project_root=project_root,
        )
    return {
        "database": _portable_path(catalog_path, project_root),
        "asset": _catalog_asset_receipt(record),
        "import": {
            "artifact": _catalog_artifact_receipt(import_artifact),
            "ue_package_bundle_sha256": package_bundle_hash,
        },
        "thumbnail": thumbnail_receipt,
    }


def _catalog_asset_receipt(record: Any) -> dict[str, Any]:
    payload = record.as_dict()
    payload.pop("created_at", None)
    payload.pop("updated_at", None)
    return payload


def _catalog_artifact_receipt(record: Any) -> dict[str, Any]:
    payload = record.as_dict()
    payload.pop("created_at", None)
    return payload


def _verify_thumbnail_evidence(
    *,
    artifacts: tuple[Any, ...],
    asset_id: str,
    item: PolyHavenSyncItem,
    import_artifact_path: str,
    import_path: Path,
    package_bundle_sha256: str,
    batch_thumbnail: Any,
    project_root: Path,
) -> dict[str, Any]:
    required = {
        "thumbnail_beauty",
        "thumbnail_mask",
        "thumbnail_mask_raw",
        "thumbnail_render_manifest",
        "thumbnail_contact_sheet",
    }
    selected = [artifact for artifact in artifacts if artifact.kind in required]
    if len(selected) != len(required) or {artifact.kind for artifact in selected} != required:
        raise PolyHavenAcquireError(f"thumbnail artifact cohort is invalid for {asset_id!r}")
    receipts: list[dict[str, Any]] = []
    render_path: Path | None = None
    render_artifact_ids = {artifact.artifact_id for artifact in selected}
    for artifact in sorted(selected, key=lambda value: value.kind):
        path = _checked_project_file(
            Path(artifact.path), project_root=project_root, context=f"{artifact.kind} artifact"
        )
        if artifact.sha256 is None or _sha256_file(path) != artifact.sha256:
            raise PolyHavenAcquireError(f"thumbnail artifact hash differs for {asset_id!r}")
        params = artifact.params
        if (
            params.get("schema_version") != 1
            or params.get("bundle_sha256") != item.source_bundle_sha256
            or params.get("ue_package_bundle_sha256") != package_bundle_sha256
            or params.get("import_manifest") != import_artifact_path
        ):
            raise PolyHavenAcquireError(f"thumbnail artifact provenance differs for {asset_id!r}")
        if artifact.kind == "thumbnail_render_manifest":
            render_path = path
        receipts.append(_catalog_artifact_receipt(artifact))
    if render_path is None:
        raise PolyHavenAcquireError(f"thumbnail render manifest is missing for {asset_id!r}")
    if (
        batch_thumbnail is not None
        and _checked_project_file(
            Path(_string(batch_thumbnail, "batch thumbnail_manifest", max_length=4_096)),
            project_root=project_root,
            context="batch thumbnail manifest",
        )
        != render_path
    ):
        raise PolyHavenAcquireError(f"batch thumbnail manifest differs for {asset_id!r}")
    render = _read_json_object_strict(render_path, "thumbnail render manifest")
    render_asset = render.get("asset")
    catalog_commit = render.get("catalog_commit")
    if (
        render.get("schema_version") != 3
        or render.get("status") != "ok"
        or not isinstance(render_asset, dict)
        or render_asset.get("kind") != "catalog"
        or render_asset.get("asset_id") != asset_id
        or render_asset.get("bundle_sha256") != item.source_bundle_sha256
        or render_asset.get("content_sha256") != item.source_content_sha256
        or render_asset.get("ue_package_bundle_sha256") != package_bundle_sha256
        or render_asset.get("import_manifest") != import_artifact_path
        or not isinstance(catalog_commit, dict)
        or catalog_commit.get("asset_id") != asset_id
        or catalog_commit.get("target_status") != "render_ok"
        or set(catalog_commit.get("artifact_ids", [])) != render_artifact_ids
        or not is_valid_thumbnail_validation(render.get("thumbnail_validation"), expected_frames=8)
        or not is_valid_catalog_scene_sanitization(
            render.get("scene_sanitization"), expected_subjobs=2
        )
    ):
        raise PolyHavenAcquireError(f"thumbnail render evidence is invalid for {asset_id!r}")
    if render_asset.get("import_manifest") != _portable_path(import_path, project_root):
        raise PolyHavenAcquireError(f"thumbnail import binding differs for {asset_id!r}")
    return {
        "render_manifest": _portable_path(render_path, project_root),
        "artifacts": receipts,
    }


def _terminal_status_mapping(value: Any) -> dict[str, TerminalStatus]:
    payload = _object(value, "terminal statuses")
    result: dict[str, TerminalStatus] = {}
    for asset_id, status in payload.items():
        try:
            validate_asset_id(asset_id)
        except ValueError as exc:
            raise PolyHavenAcquireError("terminal statuses contain an invalid asset id") from exc
        if status not in _TERMINAL_STATUSES:
            raise PolyHavenAcquireError("terminal statuses contain an unaudited status")
        result[asset_id] = status
    return result


def _validated_finalization_payload(
    *,
    manifest: dict[str, Any],
    project_root: Path,
    expected_asset_ids: set[str],
) -> tuple[dict[str, Any], dict[str, TerminalStatus], dict[str, dict[str, Any]], tuple[str, ...]]:
    _timestamp(manifest.get("finalized_at"), "run manifest finalized_at")
    finalization_keys = {
        "batch_manifest",
        "batch_manifest_file_sha256",
        "terminal_statuses",
        "terminal_evidence",
        "nonterminal_asset_ids",
    }
    has_failure_receipt = manifest.get("schema_version") == RUN_MANIFEST_SCHEMA_VERSION
    if has_failure_receipt:
        finalization_keys.update({"failure_journal", "failures"})
    finalization = _exact_object(
        manifest.get("finalization"),
        finalization_keys,
        "run manifest finalization",
    )
    _portable_state_path(
        finalization["batch_manifest"],
        project_root=project_root,
        context="run manifest finalization.batch_manifest",
    )
    _sha256_value(
        finalization["batch_manifest_file_sha256"],
        "run manifest finalization.batch_manifest_file_sha256",
    )
    statuses = _terminal_status_mapping(finalization["terminal_statuses"])
    raw_evidence = _object(
        finalization["terminal_evidence"], "run manifest finalization.terminal_evidence"
    )
    if set(raw_evidence) != set(statuses):
        raise PolyHavenAcquireError("run manifest terminal evidence cohort differs")
    terminal_evidence: dict[str, dict[str, Any]] = {}
    for asset_id, raw in raw_evidence.items():
        evidence = _exact_object(
            raw,
            {"terminal_evidence_sha256", "receipt"},
            f"run manifest terminal evidence {asset_id}",
        )
        _sha256_value(
            evidence["terminal_evidence_sha256"],
            f"run manifest terminal evidence {asset_id}.sha256",
        )
        if not isinstance(evidence["receipt"], dict):
            raise PolyHavenAcquireError("run manifest terminal evidence receipt is invalid")
        terminal_evidence[asset_id] = evidence
    raw_nonterminal = finalization["nonterminal_asset_ids"]
    if not isinstance(raw_nonterminal, list) or any(
        not isinstance(asset_id, str) for asset_id in raw_nonterminal
    ):
        raise PolyHavenAcquireError("run manifest nonterminal asset ids are invalid")
    nonterminal = tuple(raw_nonterminal)
    for asset_id in nonterminal:
        try:
            validate_asset_id(asset_id)
        except ValueError as exc:
            raise PolyHavenAcquireError(
                "run manifest nonterminal cohort contains an invalid asset id"
            ) from exc
    if nonterminal != tuple(sorted(set(nonterminal))):
        raise PolyHavenAcquireError("run manifest nonterminal asset ids are not canonical")
    if set(statuses).intersection(nonterminal) or set(statuses).union(nonterminal) != (
        expected_asset_ids
    ):
        raise PolyHavenAcquireError("run manifest finalization cohort differs")
    if has_failure_receipt:
        failures = finalization["failures"]
        if not isinstance(failures, list):
            raise PolyHavenAcquireError("run manifest finalization failures must be a list")
        request = _object(manifest.get("request"), "run manifest finalization request")
        runtime_payload = _object(
            request.get("runtime"),
            "run manifest finalization runtime configuration",
        )
        if "failure_schedule" not in runtime_payload:
            raise PolyHavenAcquireError(
                "schema-4 run manifest requires an explicit failure schedule"
            )
        runtime_config = _validate_runtime_config_payload(runtime_payload)
        quarantined = sum(
            isinstance(event, dict)
            and event.get("disposition") == FailureDisposition.QUARANTINED.value
            for event in failures
        )
        referenced_events = _validate_failure_journal_manifest_receipt(
            finalization["failure_journal"],
            failures=failures,
            counts={
                "failed": len(failures),
                "deferred": 0,
                "quarantined": quarantined,
            },
            project_root=project_root,
            expected_failure_policy=runtime_config.failure_policy,
        )
        raw_items = manifest.get("items")
        if not isinstance(raw_items, list):
            raise PolyHavenAcquireError("run manifest finalization has no item cohort")
        item_identities: dict[str, tuple[int, str, str, str]] = {}
        for ordinal, raw_item in enumerate(raw_items, start=1):
            item = _object(raw_item, "run manifest finalization item")
            asset_id = _string(
                item.get("asset_id"),
                "run manifest finalization asset_id",
                max_length=64,
            )
            item_identities[asset_id] = (
                ordinal,
                _source_id(item.get("source_id"), "run manifest finalization source_id"),
                _string(
                    item.get("revision"),
                    "run manifest finalization revision",
                    max_length=40,
                ),
                _resolution(item.get("resolution")),
            )
        run_id = _string(manifest.get("run_id"), "run manifest run_id", max_length=128)
        batch_hash = str(finalization["batch_manifest_file_sha256"])
        journal_receipt = _object(
            finalization["failure_journal"],
            "run manifest finalization failure_journal",
        )
        journal_path = project_root / _portable_state_path(
            journal_receipt.get("path"),
            project_root=project_root,
            context="run manifest finalization failure journal path",
        )
        journal = _load_failure_journal(journal_path).payload
        events_by_id = {
            str(event["event_id"]): event for event in journal["events"] if isinstance(event, dict)
        }
        failed_asset_ids: set[str] = set()
        for event in referenced_events:
            asset_id = str(event["asset_id"])
            identity = item_identities.get(asset_id)
            if identity is None:
                raise PolyHavenAcquireError(
                    "finalization journal event is outside the prepared cohort"
                )
            ordinal, source_id, revision, resolution = identity
            if (
                event.get("run_id") != run_id
                or event.get("attempt_id") != f"{run_id}:finalize:{ordinal}:{batch_hash}"
                or event.get("source_id") != source_id
                or event.get("revision") != revision
                or event.get("resolution") != resolution
            ):
                raise PolyHavenAcquireError(
                    "finalization journal event identity differs from its batch operation"
                )
            if event.get("type") == "failed":
                failure = _object(
                    event.get("failure"),
                    "run manifest finalization journal failure",
                )
                if asset_id not in nonterminal or failure.get("kind") not in {
                    FailureKind.DOWNSTREAM.value,
                    FailureKind.QUALITY.value,
                }:
                    raise PolyHavenAcquireError(
                        "finalization failure event differs from the nonterminal cohort"
                    )
                failed_asset_ids.add(asset_id)
            elif event.get("type") == "resolved":
                previous = events_by_id.get(str(event.get("failure_event_id")))
                previous_failure = previous.get("failure") if isinstance(previous, dict) else None
                if asset_id not in statuses or not isinstance(previous_failure, dict):
                    raise PolyHavenAcquireError(
                        "finalization resolution event has no prior revision failure"
                    )
            else:
                raise PolyHavenAcquireError(
                    "finalization journal contains an unsupported event type"
                )
        if failed_asset_ids != set(nonterminal):
            raise PolyHavenAcquireError(
                "finalization failure events do not match the nonterminal cohort"
            )
    return finalization, statuses, terminal_evidence, nonterminal


def _assert_finalized_state_binding(
    *,
    manifest: dict[str, Any],
    state: dict[str, Any],
    finalization: dict[str, Any],
    statuses: dict[str, TerminalStatus],
    terminal_evidence: dict[str, dict[str, Any]],
    nonterminal_asset_ids: tuple[str, ...],
) -> None:
    finalized_at = manifest["finalized_at"]
    for asset_id, status in statuses.items():
        state_item = state["items"].get(asset_id)
        if not isinstance(state_item, dict) or state_item.get("status") != status:
            raise PolyHavenAcquireError(f"finalized Poly Haven state differs for {asset_id!r}")
        terminal = _object(state_item.get("terminal"), f"state terminal {asset_id}")
        expected_evidence = {
            "terminal_evidence_sha256": terminal.get("terminal_evidence_sha256"),
            "receipt": terminal.get("receipt"),
        }
        if (
            terminal.get("status") != status
            or terminal.get("batch_manifest") != finalization["batch_manifest"]
            or terminal.get("batch_manifest_file_sha256")
            != finalization["batch_manifest_file_sha256"]
            or terminal.get("committed_at") != finalized_at
            or _payload_sha256(terminal_evidence[asset_id]) != _payload_sha256(expected_evidence)
        ):
            raise PolyHavenAcquireError(
                f"finalized Poly Haven manifest binding differs for {asset_id!r}"
            )
    for asset_id in nonterminal_asset_ids:
        state_item = state["items"].get(asset_id)
        if not isinstance(state_item, dict):
            raise PolyHavenAcquireError(
                f"finalized Poly Haven state lost nonterminal item {asset_id!r}"
            )
        raw_terminal = state_item.get("terminal")
        if raw_terminal is None:
            if state_item.get("status") != "downloaded":
                raise PolyHavenAcquireError(
                    f"finalized Poly Haven nonterminal state differs for {asset_id!r}"
                )
            continue
        terminal_payload = _object(raw_terminal, f"state terminal {asset_id}")
        if (
            terminal_payload.get("batch_manifest") == finalization["batch_manifest"]
            and terminal_payload.get("batch_manifest_file_sha256")
            == finalization["batch_manifest_file_sha256"]
        ):
            raise PolyHavenAcquireError(
                f"finalized Poly Haven terminal item was declared nonterminal: {asset_id!r}"
            )


def _validate_commit_intent_evidence(
    *,
    intent: dict[str, Any],
    new_state: dict[str, Any],
    new_manifest: dict[str, Any],
    project_root: Path,
) -> None:
    _validate_prepared_manifest_receipt(
        manifest=new_manifest,
        state=new_state,
        project_root=project_root,
    )
    evidence = _exact_object(
        intent.get("evidence"),
        {"generated_spec_file_sha256", "batch_manifest_file_sha256"},
        "commit intent evidence",
    )
    generated = new_manifest.get("generated_ingest_spec")
    if generated is None:
        if evidence["generated_spec_file_sha256"] is not None:
            raise PolyHavenAcquireError("no-op intent contains a generated spec hash")
    else:
        generated_payload = _exact_object(
            generated, {"path", "file_sha256"}, "intent generated spec"
        )
        spec_path = _checked_project_file(
            Path(generated_payload["path"]),
            project_root=project_root,
            context="intent generated IngestSpec",
        )
        if (
            generated_payload["file_sha256"] != _sha256_file(spec_path)
            or evidence["generated_spec_file_sha256"] != generated_payload["file_sha256"]
        ):
            raise PolyHavenAcquireError("intent generated IngestSpec evidence changed")
        load_ingest_spec(spec_path)
    finalization = new_manifest.get("finalization")
    if finalization is None:
        if evidence["batch_manifest_file_sha256"] is not None:
            raise PolyHavenAcquireError("sync intent contains downstream batch evidence")
    else:
        finalization_payload = _object(finalization, "intent finalization")
        batch_path = _checked_project_file(
            Path(finalization_payload["batch_manifest"]),
            project_root=project_root,
            context="intent downstream batch manifest",
        )
        if finalization_payload.get("batch_manifest_file_sha256") != _sha256_file(
            batch_path
        ) or evidence["batch_manifest_file_sha256"] != finalization_payload.get(
            "batch_manifest_file_sha256"
        ):
            raise PolyHavenAcquireError("intent downstream batch evidence changed")
    terminal_asset_ids = tuple(
        asset_id
        for asset_id, item in new_state["items"].items()
        if isinstance(item, dict) and item.get("status") in _TERMINAL_STATUSES
    )
    intent_state = _object(intent.get("state"), "commit intent state")
    intent_state_path = (project_root / str(intent_state["path"])).resolve()
    data_dir = intent_state_path.parents[2]
    with _asset_locks(data_dir=data_dir, asset_ids=terminal_asset_ids):
        for asset_id, item in new_state["items"].items():
            if not isinstance(item, dict) or item.get("migration_pending") is True:
                continue
            _assert_state_item_files(
                asset_id=asset_id,
                item=item,
                project_root=project_root,
            )
            if item.get("status") in _TERMINAL_STATUSES:
                _revalidate_terminal_receipt(
                    asset_id=asset_id,
                    item=item,
                    project_root=project_root,
                    data_dir=data_dir,
                )


def _assert_state_item_files(
    *,
    asset_id: str,
    item: dict[str, Any],
    project_root: Path,
) -> None:
    root = (project_root / str(item["root_dir"])).resolve()
    main = (project_root / str(item["main_path"])).resolve()
    metadata = (project_root / str(item["metadata_path"])).resolve()
    if (
        _sha256_file(_checked_project_file(metadata, project_root=project_root, context="metadata"))
        != item["metadata_file_sha256"]
    ):
        raise PolyHavenAcquireError(f"state metadata changed for {asset_id!r}")
    relative_files: list[Path] = []
    for raw in item["files"]:
        relative = _relative_path(raw["relative_path"], "state file relative_path")
        path = _safe_existing_file(root, relative, "state source file")
        if (
            path.stat().st_size != raw["bytes"]
            or _md5_file(path) != raw["md5"]
            or _sha256_file(path) != raw["sha256"]
        ):
            raise PolyHavenAcquireError(f"state source file changed for {asset_id!r}")
        relative_files.append(relative)
    sorted_files = tuple(sorted(relative_files, key=lambda value: value.as_posix()))
    if (
        bundle_sha256(root, sorted_files) != item["source_bundle_sha256"]
        or content_sha256(root, sorted_files) != item["source_content_sha256"]
    ):
        raise PolyHavenAcquireError(f"state source closure changed for {asset_id!r}")
    main_relative = main.relative_to(root)
    expected_dependencies = tuple(path for path in sorted_files if path != main_relative)
    if gltf_dependency_paths(main) != expected_dependencies:
        raise PolyHavenAcquireError(f"state glTF closure changed for {asset_id!r}")
    expected_tree = tuple(
        sorted((*sorted_files, metadata.relative_to(root)), key=lambda value: value.as_posix())
    )
    if _regular_tree_files(root) != expected_tree:
        raise PolyHavenAcquireError(f"state source tree has unknown files for {asset_id!r}")


def _revalidate_terminal_receipt(
    *,
    asset_id: str,
    item: dict[str, Any],
    project_root: Path,
    data_dir: Path,
) -> None:
    terminal = _object(item.get("terminal"), "terminal state")
    receipt = _exact_object(
        terminal.get("receipt"),
        {
            "asset_id",
            "source_id",
            "revision",
            "status",
            "source_bundle_sha256",
            "source_content_sha256",
            "generated_spec_file_sha256",
            "batch_manifest",
            "batch_manifest_file_sha256",
            "batch_item",
            "raw",
            "catalog",
        },
        "terminal receipt",
    )
    if terminal.get("terminal_evidence_sha256") != _domain_payload_sha256(
        b"uefactory.polyhaven-terminal-evidence.v1\0", receipt
    ):
        raise PolyHavenAcquireError(f"terminal receipt digest changed for {asset_id!r}")
    batch_path = _checked_project_file(
        Path(str(terminal["batch_manifest"])),
        project_root=project_root,
        context="terminal batch manifest",
    )
    if _sha256_file(batch_path) != terminal["batch_manifest_file_sha256"]:
        raise PolyHavenAcquireError(f"terminal batch manifest changed for {asset_id!r}")
    batch = _read_json_object_strict(batch_path, "terminal batch manifest")
    rows = _validated_batch_rows(batch)
    row = rows.get(asset_id)
    if row is None:
        raise PolyHavenAcquireError(f"terminal batch lost item {asset_id!r}")
    source_manifest = _checked_project_file(
        Path(_string(batch.get("source_manifest"), "batch source_manifest", max_length=4_096)),
        project_root=project_root,
        context="terminal generated IngestSpec",
    )
    generated_spec_file_sha256 = _sha256_file(source_manifest)
    spec = load_ingest_spec(source_manifest)
    spec_items = {asset.asset_id: asset for asset in spec.assets}
    asset_spec = spec_items.get(asset_id)
    if asset_spec is None:
        raise PolyHavenAcquireError(f"terminal IngestSpec lost item {asset_id!r}")
    catalog_path = _checked_project_file(
        Path(_string(batch.get("catalog"), "batch catalog", max_length=4_096)),
        project_root=project_root,
        context="terminal catalog",
    )
    catalog = Catalog(catalog_path, project_root=project_root)
    root_dir = (project_root / str(item["root_dir"])).resolve()
    main_path = (project_root / str(item["main_path"])).resolve()
    metadata_path = (project_root / str(item["metadata_path"])).resolve()
    dependency_paths = tuple(root_dir / path for path in gltf_dependency_paths(main_path))
    sync_item = PolyHavenSyncItem(
        asset_id=asset_id,
        source_id=str(item["source_id"]),
        revision=str(item["revision"]),
        root_dir=root_dir,
        main_path=main_path,
        dependency_paths=dependency_paths,
        metadata_path=metadata_path,
        downloaded_files=0,
        reused_files=len(item["files"]),
        downloaded_bytes=0,
        verified_bytes=sum(int(raw["bytes"]) for raw in item["files"]),
        source_bundle_sha256=str(item["source_bundle_sha256"]),
        source_content_sha256=str(item["source_content_sha256"]),
        acquired_at=str(item["acquired_at"]),
        verified_at=str(item["verified_at"]),
        state_status=str(item["status"]),
    )
    _assert_state_item_files(asset_id=asset_id, item=item, project_root=project_root)
    raw_receipt = _verify_staged_raw(
        asset_id=asset_id,
        item=sync_item,
        raw_path_value=row.get("raw_path"),
        project_root=project_root,
        data_dir=data_dir,
    )
    catalog_status = row.get("catalog_status")
    batch_status = row.get("status")
    if batch_status not in _TERMINAL_STATUSES or catalog_status not in {"imported", "render_ok"}:
        raise PolyHavenAcquireError(f"terminal batch outcome changed for {asset_id!r}")
    catalog_receipt = _verify_catalog_terminal_evidence(
        catalog=catalog,
        catalog_path=catalog_path,
        asset_id=asset_id,
        item=sync_item,
        asset_spec=asset_spec,
        catalog_status=str(catalog_status),
        batch_status=str(batch_status),
        batch_row=row,
        project_root=project_root,
        data_dir=data_dir,
    )
    expected_receipt = {
        "asset_id": asset_id,
        "source_id": item["source_id"],
        "revision": item["revision"],
        "status": batch_status,
        "source_bundle_sha256": item["source_bundle_sha256"],
        "source_content_sha256": item["source_content_sha256"],
        "generated_spec_file_sha256": generated_spec_file_sha256,
        "batch_manifest": _portable_path(batch_path, project_root),
        "batch_manifest_file_sha256": _sha256_file(batch_path),
        "batch_item": row,
        "raw": raw_receipt,
        "catalog": catalog_receipt,
    }
    if _payload_sha256(receipt) != _payload_sha256(expected_receipt):
        raise PolyHavenAcquireError(f"terminal evidence receipt changed for {asset_id!r}")
    if (
        terminal.get("status") != batch_status
        or terminal.get("batch_manifest") != expected_receipt["batch_manifest"]
        or terminal.get("batch_manifest_file_sha256")
        != expected_receipt["batch_manifest_file_sha256"]
    ):
        raise PolyHavenAcquireError(f"terminal state binding changed for {asset_id!r}")


def _commit_state_and_manifest(
    *,
    intent_path: Path,
    state_path: Path,
    loaded_state: _LoadedState,
    new_state: dict[str, Any],
    manifest_path: Path,
    expected_manifest_file_sha256: str,
    new_manifest: dict[str, Any],
    operation: str,
    project_root: Path,
) -> None:
    if operation not in {"sync", "finalize"}:
        raise PolyHavenAcquireError("Poly Haven commit operation is invalid")
    if intent_path.exists() or intent_path.is_symlink():
        raise PolyHavenAcquireError("a Poly Haven commit intent is already pending")
    _validate_v2_state(new_state, project_root=project_root)
    current_manifest_hash = _sha256_file(manifest_path)
    if current_manifest_hash != expected_manifest_file_sha256:
        raise PolyHavenAcquireError("Poly Haven run manifest changed before commit intent")
    _require_state_base(state_path, loaded_state)
    run_id = _string(new_manifest.get("run_id"), "run manifest run_id", max_length=128)
    generated = new_manifest.get("generated_ingest_spec")
    generated_hash = generated.get("file_sha256") if isinstance(generated, dict) else None
    finalization = new_manifest.get("finalization")
    batch_hash = (
        finalization.get("batch_manifest_file_sha256") if isinstance(finalization, dict) else None
    )
    intent = {
        "schema_version": COMMIT_INTENT_SCHEMA_VERSION,
        "source": POLYHAVEN_SOURCE,
        "operation": operation,
        "transaction_id": f"{run_id}_{operation}",
        "run_id": run_id,
        "created_at": _utc_now(),
        "evidence": {
            "generated_spec_file_sha256": generated_hash,
            "batch_manifest_file_sha256": batch_hash,
        },
        "state": {
            "path": _portable_path(state_path, project_root),
            "before": {
                "exists": loaded_state.before_exists,
                "file_sha256": loaded_state.before_file_sha256,
                "payload_sha256": loaded_state.before_payload_sha256,
            },
            "after": {
                "file_sha256": _json_file_sha256(new_state),
                "payload_sha256": _payload_sha256(new_state),
                "payload": new_state,
            },
        },
        "manifest": {
            "path": _portable_path(manifest_path, project_root),
            "before_file_sha256": expected_manifest_file_sha256,
            "after": {
                "file_sha256": _json_file_sha256(new_manifest),
                "payload_sha256": _payload_sha256(new_manifest),
                "payload": new_manifest,
            },
        },
    }
    _write_json_atomic(intent_path, intent)
    _reconcile_commit_intent(
        intent_path=intent_path,
        state_path=state_path,
        project_root=project_root,
    )


def _reconcile_commit_intent(
    *,
    intent_path: Path,
    state_path: Path,
    project_root: Path,
) -> None:
    if not intent_path.exists() and not intent_path.is_symlink():
        return
    _require_regular_file(intent_path, "Poly Haven commit intent")
    intent = _read_json_object_strict(intent_path, "Poly Haven commit intent")
    expected_top = {
        "schema_version",
        "source",
        "operation",
        "transaction_id",
        "run_id",
        "created_at",
        "evidence",
        "state",
        "manifest",
    }
    if set(intent) != expected_top:
        raise PolyHavenAcquireError("Poly Haven commit intent has an unsupported shape")
    if (
        intent.get("schema_version") != COMMIT_INTENT_SCHEMA_VERSION
        or intent.get("source") != POLYHAVEN_SOURCE
        or intent.get("operation") not in {"sync", "finalize"}
    ):
        raise PolyHavenAcquireError("Poly Haven commit intent identity is invalid")
    _timestamp(intent.get("created_at"), "commit intent created_at")
    run_id = _string(intent.get("run_id"), "commit intent run_id", max_length=128)
    _string(intent.get("transaction_id"), "commit intent transaction_id", max_length=256)
    state_intent = _exact_object(
        intent.get("state"), {"path", "before", "after"}, "commit intent state"
    )
    if state_intent["path"] != _portable_path(state_path, project_root):
        raise PolyHavenAcquireError("Poly Haven commit intent state path is invalid")
    before = _exact_object(
        state_intent["before"],
        {"exists", "file_sha256", "payload_sha256"},
        "commit intent state.before",
    )
    if not isinstance(before["exists"], bool):
        raise PolyHavenAcquireError("commit intent state.before.exists is invalid")
    if before["exists"]:
        _sha256_value(before["file_sha256"], "commit intent base state file hash")
        _sha256_value(before["payload_sha256"], "commit intent base state payload hash")
    elif before["file_sha256"] is not None or before["payload_sha256"] is not None:
        raise PolyHavenAcquireError("missing base state may not contain hashes")
    state_after = _exact_object(
        state_intent["after"],
        {"file_sha256", "payload_sha256", "payload"},
        "commit intent state.after",
    )
    after_state_payload = _object(state_after["payload"], "commit intent state payload")
    if state_after["file_sha256"] != _json_file_sha256(after_state_payload) or state_after[
        "payload_sha256"
    ] != _payload_sha256(after_state_payload):
        raise PolyHavenAcquireError("commit intent proposed state hashes are invalid")
    _validate_v2_state(after_state_payload, project_root=project_root)

    manifest_intent = _exact_object(
        intent.get("manifest"),
        {"path", "before_file_sha256", "after"},
        "commit intent manifest",
    )
    manifest_path = _checked_project_file_hint(
        manifest_intent["path"], project_root=project_root, context="commit manifest path"
    )
    _sha256_value(manifest_intent["before_file_sha256"], "commit intent base manifest file hash")
    manifest_after = _exact_object(
        manifest_intent["after"],
        {"file_sha256", "payload_sha256", "payload"},
        "commit intent manifest.after",
    )
    after_manifest_payload = _object(manifest_after["payload"], "commit intent manifest payload")
    if (
        manifest_after["file_sha256"] != _json_file_sha256(after_manifest_payload)
        or manifest_after["payload_sha256"] != _payload_sha256(after_manifest_payload)
        or after_manifest_payload.get("run_id") != run_id
    ):
        raise PolyHavenAcquireError("commit intent proposed manifest hashes are invalid")
    _validate_commit_intent_evidence(
        intent=intent,
        new_state=after_state_payload,
        new_manifest=after_manifest_payload,
        project_root=project_root,
    )

    state_position = _file_position(
        state_path,
        before_file_sha256=before["file_sha256"],
        after_file_sha256=state_after["file_sha256"],
        allow_missing_before=not before["exists"],
        context="Poly Haven state",
    )
    manifest_position = _file_position(
        manifest_path,
        before_file_sha256=manifest_intent["before_file_sha256"],
        after_file_sha256=manifest_after["file_sha256"],
        allow_missing_before=False,
        context="Poly Haven run manifest",
    )
    if state_position == "before":
        _write_json_atomic(state_path, after_state_payload)
    if manifest_position == "before":
        _write_json_atomic(manifest_path, after_manifest_payload)
    if (
        _sha256_file(state_path) != state_after["file_sha256"]
        or _sha256_file(manifest_path) != manifest_after["file_sha256"]
    ):
        raise PolyHavenAcquireError("Poly Haven commit reconciliation did not converge")
    intent_path.unlink()
    _fsync_directory(intent_path.parent)


def _file_position(
    path: Path,
    *,
    before_file_sha256: Any,
    after_file_sha256: Any,
    allow_missing_before: bool,
    context: str,
) -> str:
    after = _sha256_value(after_file_sha256, f"{context} after hash")
    if not path.exists() and not path.is_symlink():
        if allow_missing_before:
            return "before"
        raise PolyHavenAcquireError(f"{context} disappeared during commit")
    _require_regular_file(path, context)
    current = _sha256_file(path)
    if current == after:
        return "after"
    if before_file_sha256 is not None and current == before_file_sha256:
        return "before"
    raise PolyHavenAcquireError(f"{context} conflicts with pending commit intent")


def _require_state_base(path: Path, loaded: _LoadedState) -> None:
    if not loaded.before_exists:
        if path.exists() or path.is_symlink():
            raise PolyHavenAcquireError("Poly Haven state appeared before commit")
        return
    _require_regular_file(path, "Poly Haven state")
    if _sha256_file(path) != loaded.before_file_sha256:
        raise PolyHavenAcquireError("Poly Haven state changed before commit")


def _persist_run_failure(
    *,
    manifest_path: Path,
    intent_path: Path,
    error: BaseException,
    runtime_evidence: Mapping[str, Any] | None = None,
) -> None:
    try:
        if intent_path.exists() and _intent_targets_manifest(intent_path, manifest_path):
            _write_json_atomic(
                manifest_path.parent / "interruption.json",
                {
                    "schema_version": 1,
                    "status": "interrupted",
                    "recorded_at": _utc_now(),
                    "error": {"type": type(error).__name__, "message": str(error)},
                    "pending_commit_intent": str(intent_path),
                },
            )
            return
        if not manifest_path.is_file() or manifest_path.is_symlink():
            return
        payload = _read_json_object_strict(manifest_path, "Poly Haven run manifest")
        if payload.get("status") != "running":
            return
        payload["status"] = "failed" if isinstance(error, Exception) else "interrupted"
        if isinstance(error, Exception):
            # The exception was observed and durably classified as a run failure;
            # only process-level interruptions leave an attempt for crash recovery.
            payload["active_attempt"] = None
        payload["completed_at"] = _utc_now()
        payload["error"] = {"type": type(error).__name__, "message": str(error)}
        if runtime_evidence is not None:
            payload["runtime"] = dict(runtime_evidence)
        _write_json_atomic(manifest_path, payload)
    except BaseException:
        return


def _intent_targets_manifest(intent_path: Path, manifest_path: Path) -> bool:
    try:
        intent = _read_json_object_strict(intent_path, "Poly Haven commit intent")
        payload = intent.get("manifest")
        if not isinstance(payload, dict):
            return False
        raw_path = payload.get("path")
        if not isinstance(raw_path, str):
            return False
        project_root = manifest_path.resolve().parents[4]
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        return candidate.resolve() == manifest_path.resolve()
    except BaseException:
        return False


def _reconcile_stale_running_manifests(
    *,
    project_root: Path,
    exclude: Path,
    journal_path: Path,
    journal_payload: dict[str, Any],
    policy: CrossRunFailurePolicy,
    clock: Clock,
) -> None:
    root = project_root / "out/acquire/polyhaven"
    if root.is_symlink():
        raise PolyHavenPathSecurityError(f"Poly Haven run root is a symlink: {root}")
    if not root.is_dir():
        return
    canonical_root = root.resolve()
    for run_dir in sorted(root.iterdir()):
        if run_dir.is_symlink():
            raise PolyHavenPathSecurityError(f"Poly Haven run directory is a symlink: {run_dir}")
        if not run_dir.is_dir():
            continue
        path = run_dir / "manifest.json"
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise PolyHavenPathSecurityError(f"Poly Haven stale run manifest is unsafe: {path}")
        try:
            path.resolve().relative_to(canonical_root)
        except (OSError, ValueError) as exc:
            raise PolyHavenPathSecurityError(
                f"Poly Haven stale run manifest escapes its root: {path}"
            ) from exc
        _reject_symlink_components(
            path,
            project_root=project_root,
            context="Poly Haven stale run manifest",
        )
        if path.resolve() == exclude.resolve():
            continue
        payload = _read_json_object_strict(path, "Poly Haven run manifest")
        active_attempt = payload.get("active_attempt")
        is_transient_shape = "active_attempt" in payload or "journal_event_refs" in payload
        if payload.get("status") != "running" and active_attempt is None and not is_transient_shape:
            continue
        if active_attempt is not None:
            active = _exact_object(
                active_attempt,
                {
                    "attempt_id",
                    "ordinal",
                    "asset_id",
                    "source_id",
                    "revision",
                    "resolution",
                    "started_at",
                },
                "stale Poly Haven active_attempt",
            )
            run_id = _string(payload.get("run_id"), "stale run_id", max_length=128)
            ordinal = _positive_int(active["ordinal"], "stale active attempt ordinal")
            attempt_id = _string(
                active["attempt_id"],
                "stale active attempt id",
                max_length=160,
            )
            if attempt_id != f"{run_id}:{ordinal}":
                raise PolyHavenAcquireError("stale active attempt id is invalid")
            asset_id = _string(active["asset_id"], "stale asset_id", max_length=64)
            try:
                validate_asset_id(asset_id)
            except ValueError as exc:
                raise PolyHavenAcquireError("stale active attempt asset id is invalid") from exc
            source_id = _source_id(active["source_id"], "stale active attempt source_id")
            revision = _string(
                active["revision"],
                "stale active attempt revision",
                max_length=40,
            )
            if (
                _SHA1_PATTERN.fullmatch(revision) is None
                or revisioned_asset_id(source_id, revision) != asset_id
            ):
                raise PolyHavenAcquireError("stale active attempt revision is invalid")
            resolution = _resolution(active["resolution"])
            _timestamp(active["started_at"], "stale active attempt started_at")
            event = _journal_event_for_attempt(journal_payload, attempt_id=attempt_id)
            if event is None:
                try:
                    journal_payload, event = append_failure_event(
                        journal_payload,
                        source=POLYHAVEN_SOURCE,
                        asset_type="models",
                        asset_id=asset_id,
                        source_id=source_id,
                        revision=revision,
                        resolution=resolution,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        failure=AcquisitionFailure(
                            kind=FailureKind.INTERRUPTED,
                            phase="revision_attempt",
                            message="Poly Haven revision attempt was interrupted before receipt",
                        ),
                        recorded_at=_next_failure_journal_datetime(
                            journal_payload,
                            now=_clock_utc_now(clock),
                        ),
                        policy=policy,
                        attempts_in_run=0,
                    )
                except FailureJournalError as exc:
                    raise PolyHavenAcquireError(
                        f"cannot reconcile interrupted Poly Haven attempt: {exc}"
                    ) from exc
                _persist_failure_journal(journal_path, journal_payload)
            elif (
                event.get("asset_id") != asset_id
                or event.get("source_id") != source_id
                or event.get("revision") != revision
                or event.get("resolution") != resolution
            ):
                raise PolyHavenAcquireError("stale active attempt conflicts with its journal event")
            payload["active_attempt"] = None
        raw_refs = payload.get("journal_event_refs", [])
        if not isinstance(raw_refs, list):
            raise PolyHavenAcquireError("stale run journal_event_refs are invalid")
        run_id = _string(payload.get("run_id"), "stale run_id", max_length=128)
        events = journal_payload.get("events")
        if not isinstance(events, list):
            raise PolyHavenAcquireError("Poly Haven failure journal events are invalid")
        for event in events:
            if isinstance(event, dict) and event.get("run_id") == run_id:
                event_ref = _failure_event_ref(event)
                if event_ref not in raw_refs:
                    raw_refs.append(event_ref)
        raw_refs.sort(key=lambda item: int(item["sequence"]))
        payload["journal_event_refs"] = raw_refs
        if payload.get("status") == "running":
            payload["status"] = "interrupted"
            payload["completed_at"] = _utc_now()
            payload["error"] = {
                "type": "InterruptedRun",
                "message": "reconciled on the next source-scoped startup",
            }
        _write_json_atomic(path, payload)


def _journal_event_for_attempt(
    payload: Mapping[str, Any],
    *,
    attempt_id: str,
) -> dict[str, Any] | None:
    events = payload.get("events")
    if not isinstance(events, list):
        raise PolyHavenAcquireError("Poly Haven failure journal events are invalid")
    matches = [
        event
        for event in events
        if isinstance(event, dict) and event.get("attempt_id") == attempt_id
    ]
    if len(matches) > 1:
        raise PolyHavenAcquireError("Poly Haven attempt has multiple journal outcomes")
    return matches[0] if matches else None


@contextmanager
def _source_lock(data_dir: Path) -> Iterator[Path]:
    lock_root = _ensure_safe_directory(data_dir, Path("locks/acquire"))
    lock_path = lock_root / "polyhaven.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise PolyHavenAcquireError(f"cannot open Poly Haven source lock: {lock_path}") from exc
    handle: TextIO = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise PolyHavenAcquireError(
                f"Poly Haven source sync is busy; another process owns {lock_path}"
            ) from exc
        yield lock_path
    finally:
        with suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _asset_locks(*, data_dir: Path, asset_ids: tuple[str, ...]) -> Iterator[None]:
    with ExitStack() as stack:
        for asset_id in sorted(set(asset_ids)):
            stack.enter_context(asset_lock(data_dir=data_dir, asset_id=asset_id))
        yield


def _safe_destination(root: Path, relative_path: Path) -> Path:
    normalized = _relative_path(relative_path.as_posix(), "package path")
    canonical_root = root.resolve()
    candidate = canonical_root.joinpath(*normalized.parts)
    try:
        candidate.resolve(strict=False).relative_to(canonical_root)
    except (OSError, ValueError) as exc:
        raise PolyHavenAcquireError(
            f"Poly Haven package path escapes root: {relative_path}"
        ) from exc
    current = canonical_root
    for part in normalized.parts[:-1]:
        current /= part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise PolyHavenAcquireError(f"Poly Haven package parent is unsafe: {current}")
        else:
            current.mkdir()
    return candidate


def _safe_existing_file(root: Path, relative_path: Path, context: str) -> Path:
    normalized = _relative_path(relative_path.as_posix(), context)
    canonical_root = root.resolve()
    candidate = canonical_root.joinpath(*normalized.parts)
    current = canonical_root
    for part in normalized.parts[:-1]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise PolyHavenAcquireError(f"{context} parent is not accessible: {current}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise PolyHavenAcquireError(f"{context} parent is unsafe: {current}")
    _require_regular_file(candidate, context)
    if candidate.resolve() != candidate.absolute():
        raise PolyHavenAcquireError(f"{context} may not traverse a symlink: {candidate}")
    return candidate


def _regular_tree_files(root: Path) -> tuple[Path, ...]:
    checked_root = root.resolve()
    if root.is_symlink() or not checked_root.is_dir():
        raise PolyHavenAcquireError(f"Poly Haven evidence root is unsafe: {root}")
    files: list[Path] = []
    for directory, names, filenames in os.walk(checked_root, followlinks=False):
        base = Path(directory)
        for name in names:
            child = base / name
            if child.is_symlink():
                raise PolyHavenAcquireError(f"Poly Haven evidence tree contains symlink: {child}")
        for name in filenames:
            child = base / name
            _require_regular_file(child, "Poly Haven evidence file")
            files.append(child.relative_to(checked_root))
    return tuple(sorted(files, key=lambda path: path.as_posix()))


def _ensure_safe_directory(scope: Path, relative_path: Path) -> Path:
    canonical_scope = scope.resolve()
    canonical_scope.mkdir(parents=True, exist_ok=True)
    normalized = _relative_path(relative_path.as_posix(), "directory path")
    current = canonical_scope
    for part in normalized.parts:
        current /= part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise PolyHavenAcquireError(f"Poly Haven directory is unsafe: {current}")
        else:
            current.mkdir()
    return current


def _require_regular_file(path: Path, context: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise PolyHavenAcquireError(f"{context} is not accessible: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PolyHavenAcquireError(f"{context} is not a regular file: {path}")


def _checked_project_file_hint(value: Any, *, project_root: Path, context: str) -> Path:
    raw = _string(value, context, max_length=4_096)
    return _checked_project_file(Path(raw), project_root=project_root, context=context)


def _md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as file:
        while chunk := file.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(value: Any, context: str) -> Path:
    raw = _string(value, context, max_length=1_024)
    if "\\" in raw:
        raise PolyHavenPathSecurityError(f"{context} must use normalized POSIX separators")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise PolyHavenPathSecurityError(f"{context} must be a safe relative path")
    if pure.as_posix() != raw:
        raise PolyHavenPathSecurityError(f"{context} must be a normalized relative path")
    return Path(*pure.parts)


def _source_id(value: Any, context: str) -> str:
    result = _string(value, context, max_length=40)
    if _SOURCE_ID_PATTERN.fullmatch(result) is None:
        raise PolyHavenAcquireError(f"{context}: expected a safe Poly Haven identifier")
    return result


def _resolution(value: Any) -> str:
    result = _string(value, "resolution", max_length=16)
    if _RESOLUTION_PATTERN.fullmatch(result) is None:
        raise PolyHavenAcquireError("resolution must look like '1k' or '2k'")
    return result


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PolyHavenAcquireError(f"{context} must be a JSON object with string keys")
    return value


def _string(value: Any, context: str, *, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PolyHavenAcquireError(
            f"{context} must be non-empty trimmed text up to {max_length} characters"
        )
    return value


def _positive_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PolyHavenAcquireError(f"{context} must be a positive integer")
    return value


def _string_sequence(value: Any, context: str, *, max_items: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items:
        raise PolyHavenAcquireError(f"{context} must be a list with at most {max_items} entries")
    checked: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise PolyHavenAcquireError(f"{context}[{index}] must be text")
        checked.append(_string(item.strip(), f"{context}[{index}]", max_length=256))
    # The live API currently repeats some harmless metadata tags (for example,
    # Camera_01 contains photography/classic twice). Canonicalize these arrays
    # without weakening validation of the individual values.
    return tuple(dict.fromkeys(checked))


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolyHavenAcquireError(f"Poly Haven JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _read_json_object_strict(path: Path, context: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except PolyHavenAcquireError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PolyHavenAcquireError(f"cannot read {context}: {path}") from exc
    return _object(value, context)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    _write_text_atomic(path, _render_json(payload).decode("utf-8"))


def _write_text_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PolyHavenAcquireError(f"refusing to replace symlink: {path}")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.part")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _render_json(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _payload_sha256(payload: Any) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _state_transition_sha256(state: Mapping[str, Any], *, run_id: str) -> str:
    """Bind one run's state transition without its self-referential receipt anchor."""

    checked_run_id = _string(run_id, "state transition run_id", max_length=128)
    projection = json.loads(json.dumps(state))
    noop_receipts = projection.get("noop_run_receipts")
    if isinstance(noop_receipts, dict):
        noop_receipts.pop(checked_run_id, None)
    finalization_receipts = projection.get("finalization_run_receipts")
    if isinstance(finalization_receipts, dict):
        finalization_receipts.pop(checked_run_id, None)
    items = projection.get("items")
    if isinstance(items, dict):
        for raw_item in items.values():
            if isinstance(raw_item, dict) and raw_item.get("last_run_id") == checked_run_id:
                raw_item["prepared_manifest_payload_sha256"] = None
    return _domain_payload_sha256(_STATE_TRANSITION_DIGEST_DOMAIN, projection)


def _domain_payload_sha256(domain: bytes, payload: Any) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(domain + rendered).hexdigest()


def _json_file_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_render_json(payload)).hexdigest()


def _listing_sha256(models: tuple[PolyHavenModel, ...]) -> str:
    payload = [
        {
            "source_id": item.source_id,
            "asset_id": item.asset_id,
            "name": item.name,
            "date_published": item.date_published,
            "revision": item.revision,
            "authors": list(item.authors),
            "categories": list(item.categories),
            "tags": list(item.tags),
        }
        for item in models
    ]
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(_LISTING_DIGEST_DOMAIN + rendered).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _portable_state_path(value: Any, *, project_root: Path, context: str) -> str:
    raw = _string(value, context, max_length=4_096)
    candidate = Path(raw)
    if candidate.is_absolute() or "\\" in raw:
        raise PolyHavenAcquireError(f"{context} must be a project-relative POSIX path")
    normalized = _relative_path(raw, context)
    unresolved = project_root / normalized
    _reject_symlink_components(unresolved, project_root=project_root, context=context)
    resolved = unresolved.resolve(strict=False)
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise PolyHavenAcquireError(f"{context} escapes project_root") from exc
    return normalized.as_posix()


def _timestamp(value: Any, context: str) -> str:
    raw = _string(value, context, max_length=20)
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise PolyHavenAcquireError(f"{context} must be a UTC second timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != raw:
        raise PolyHavenAcquireError(f"{context} must be a canonical UTC timestamp")
    return raw


def _sha256_value(value: Any, context: str) -> str:
    raw = _string(value, context, max_length=64)
    if re.fullmatch(r"[0-9a-f]{64}", raw) is None:
        raise PolyHavenAcquireError(f"{context} must be lowercase SHA-256")
    return raw


def _nonnegative_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PolyHavenAcquireError(f"{context} must be a non-negative integer")
    return value


def _exact_object(value: Any, keys: set[str], context: str) -> dict[str, Any]:
    payload = _object(value, context)
    if set(payload) != keys:
        raise PolyHavenAcquireError(f"{context} has an unsupported shape")
    return payload


def _reject_symlink_components(path: Path, *, project_root: Path, context: str) -> None:
    try:
        relative = path.absolute().relative_to(project_root)
    except ValueError as exc:
        raise PolyHavenAcquireError(f"{context} must be inside project_root") from exc
    current = project_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise PolyHavenAcquireError(f"{context} may not traverse a symlink: {current}")


def _empty_quota_ledger(utc_day: date) -> dict[str, Any]:
    return {
        "schema_version": _QUOTA_LEDGER_SCHEMA_VERSION,
        "source": POLYHAVEN_SOURCE,
        "asset_type": "models",
        "utc_day": utc_day.isoformat(),
        "updated_at": None,
        "usage": {"new_items_reserved": 0, "download_bytes_reserved": 0},
        "item_ids": [],
        "open_downloads": {},
    }


def _quota_usage(payload: Mapping[str, Any]) -> DailyQuotaUsage:
    usage = _object(payload.get("usage"), "Poly Haven quota ledger usage")
    try:
        utc_day = date.fromisoformat(str(payload.get("utc_day")))
    except ValueError as exc:
        raise PolyHavenAcquireError("Poly Haven quota ledger utc_day is invalid") from exc
    try:
        return DailyQuotaUsage(
            utc_day=utc_day,
            new_items_reserved=_nonnegative_int(
                usage.get("new_items_reserved"), "quota new_items_reserved"
            ),
            download_bytes_reserved=_nonnegative_int(
                usage.get("download_bytes_reserved"), "quota download_bytes_reserved"
            ),
        )
    except RuntimeValidationError as exc:
        raise PolyHavenAcquireError(f"Poly Haven quota ledger usage is invalid: {exc}") from exc


def _quota_usage_payload(usage: DailyQuotaUsage) -> dict[str, int]:
    return {
        "new_items_reserved": usage.new_items_reserved,
        "download_bytes_reserved": usage.download_bytes_reserved,
    }


def _quota_download_payload(
    *,
    asset_id: str,
    spec: PolyHavenFileSpec,
    reserved_bytes: int,
    oversize_probe_bytes: int = _OVERSIZE_PROBE_BYTES,
    body_bytes_claimed: int = 0,
) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "relative_path": spec.relative_path.as_posix(),
        "bytes": spec.bytes,
        "md5": spec.md5,
        "reserved_bytes": reserved_bytes,
        "oversize_probe_bytes": oversize_probe_bytes,
        "body_bytes_claimed": body_bytes_claimed,
    }


def _quota_download_key(*, asset_id: str, spec: PolyHavenFileSpec) -> str:
    return _domain_payload_sha256(
        _QUOTA_DOWNLOAD_KEY_DOMAIN,
        {
            "asset_id": asset_id,
            "relative_path": spec.relative_path.as_posix(),
            "bytes": spec.bytes,
            "md5": spec.md5,
        },
    )


def _validate_quota_ledger(payload: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "source",
        "asset_type",
        "utc_day",
        "updated_at",
        "usage",
        "item_ids",
        "open_downloads",
    }
    if set(payload) != expected:
        raise PolyHavenAcquireError("Poly Haven quota ledger has an unsupported shape")
    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != _QUOTA_LEDGER_SCHEMA_VERSION
        or payload.get("source") != POLYHAVEN_SOURCE
        or payload.get("asset_type") != "models"
    ):
        raise PolyHavenAcquireError("Poly Haven quota ledger identity is invalid")
    raw_day = payload.get("utc_day")
    if not isinstance(raw_day, str):
        raise PolyHavenAcquireError("Poly Haven quota ledger utc_day is invalid")
    try:
        parsed_day = date.fromisoformat(raw_day)
    except ValueError as exc:
        raise PolyHavenAcquireError("Poly Haven quota ledger utc_day is invalid") from exc
    if parsed_day.isoformat() != raw_day:
        raise PolyHavenAcquireError("Poly Haven quota ledger utc_day is not canonical")
    updated_at = payload.get("updated_at")
    if updated_at is not None:
        _timestamp(updated_at, "quota ledger updated_at")
    usage = _quota_usage(payload)
    item_ids = payload.get("item_ids")
    if (
        not isinstance(item_ids, list)
        or any(not isinstance(item, str) for item in item_ids)
        or item_ids != sorted(set(item_ids))
    ):
        raise PolyHavenAcquireError("Poly Haven quota ledger item_ids are invalid")
    for asset_id in item_ids:
        try:
            validate_asset_id(asset_id)
        except ValueError as exc:
            raise PolyHavenAcquireError(
                "Poly Haven quota ledger contains an invalid asset id"
            ) from exc
    if usage.new_items_reserved != len(item_ids):
        raise PolyHavenAcquireError("Poly Haven quota ledger item accounting differs")
    downloads = _object(payload.get("open_downloads"), "quota ledger open_downloads")
    open_reserved_total = 0
    for key, raw_download in downloads.items():
        _sha256_value(key, "quota ledger download key")
        download = _exact_object(
            raw_download,
            {
                "asset_id",
                "relative_path",
                "bytes",
                "md5",
                "reserved_bytes",
                "oversize_probe_bytes",
                "body_bytes_claimed",
            },
            "quota ledger open download",
        )
        asset_id = _string(download["asset_id"], "quota download asset_id", max_length=64)
        try:
            validate_asset_id(asset_id)
        except ValueError as exc:
            raise PolyHavenAcquireError("quota download asset_id is invalid") from exc
        relative_path = _relative_path(download["relative_path"], "quota download relative_path")
        size = _positive_int(download["bytes"], "quota download bytes")
        md5 = _string(download["md5"], "quota download md5", max_length=32)
        if _MD5_PATTERN.fullmatch(md5) is None:
            raise PolyHavenAcquireError("quota download md5 is invalid")
        reserved = _nonnegative_int(download["reserved_bytes"], "quota reserved_bytes")
        probe_bytes = _nonnegative_int(
            download["oversize_probe_bytes"], "quota oversize_probe_bytes"
        )
        body_bytes_claimed = _nonnegative_int(
            download["body_bytes_claimed"], "quota body_bytes_claimed"
        )
        if probe_bytes not in {0, _OVERSIZE_PROBE_BYTES} or reserved < size + probe_bytes:
            raise PolyHavenAcquireError(
                "quota download reservation is smaller than its file and oversize probe"
            )
        if body_bytes_claimed > reserved:
            raise PolyHavenAcquireError(
                "quota download body claims exceed their durable reservation"
            )
        open_reserved_total += reserved
        spec = PolyHavenFileSpec(
            relative_path=relative_path,
            url="https://dl.polyhaven.org/ledger-validation",
            bytes=size,
            md5=md5,
        )
        if key != _quota_download_key(asset_id=asset_id, spec=spec):
            raise PolyHavenAcquireError("Poly Haven quota ledger download key differs")
    if open_reserved_total > usage.download_bytes_reserved:
        raise PolyHavenAcquireError("Poly Haven quota ledger open download accounting differs")


def _clock_utc_now(clock: Clock) -> datetime:
    value = clock.utc_now()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PolyHavenAcquireError("Poly Haven runtime clock must return timezone-aware UTC")
    return value.astimezone(UTC)


def _polyhaven_disk_snapshot(
    *,
    models_root: Path,
    data_dir: Path,
    additional_roots: tuple[Path, ...] = (),
) -> DiskSnapshot:
    storage_bytes = 0
    for storage_root in (models_root, *additional_roots):
        if not storage_root.exists() and not storage_root.is_symlink():
            continue
        if storage_root.is_symlink() or not storage_root.is_dir():
            raise PolyHavenAcquireError("Poly Haven model storage root is unsafe")
        for directory, names, filenames in os.walk(storage_root, followlinks=False):
            base = Path(directory)
            for name in names:
                child = base / name
                if child.is_symlink():
                    raise PolyHavenAcquireError(
                        f"Poly Haven model storage contains a symlink: {child}"
                    )
            for name in filenames:
                child = base / name
                _require_regular_file(child, "Poly Haven model storage file")
                storage_bytes += child.stat().st_size
    usage = shutil.disk_usage(data_dir)
    try:
        return DiskSnapshot(storage_bytes=storage_bytes, free_bytes=usage.free)
    except RuntimeValidationError as exc:
        raise PolyHavenAcquireError(f"Poly Haven disk snapshot is invalid: {exc}") from exc


def _milliseconds(seconds: float) -> int:
    if not math.isfinite(seconds) or seconds < 0:
        raise PolyHavenAcquireError("Poly Haven runtime wait accounting is invalid")
    return int(round(seconds * 1_000))


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_utc_timestamp(*previous_values: Any) -> str:
    previous = [
        datetime.strptime(_timestamp(value, "previous timestamp"), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        for value in previous_values
        if value is not None
    ]
    now = datetime.now(UTC).replace(microsecond=0)
    if previous and now <= max(previous):
        now = max(previous) + timedelta(seconds=1)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "DEFAULT_REQUEST_RATE_PER_SEC",
    "DEFAULT_RESOLUTION",
    "POLYHAVEN_LICENSE",
    "POLYHAVEN_LICENSE_URL",
    "PolyHavenAcquireError",
    "PolyHavenFileSpec",
    "PolyHavenModel",
    "PolyHavenModelPackage",
    "PolyHavenRuntimeConfig",
    "PolyHavenSyncItem",
    "PolyHavenSyncResult",
    "TerminalStatus",
    "finalize_polyhaven_items",
    "parse_polyhaven_model_files",
    "parse_polyhaven_model_listing",
    "revisioned_asset_id",
    "sync_polyhaven_models",
]
