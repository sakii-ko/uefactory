from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from uefactory.acquire.runtime import (
    AcquisitionFailure,
    FailureCategory,
    FailureKind,
    RuntimeValidationError,
)

FAILURE_JOURNAL_SCHEMA_VERSION = 1
_EVENT_ID_DOMAIN = b"uefactory.acquire-failure-event.v1\0"
_EVENT_SHA256_DOMAIN = b"uefactory.acquire-failure-event-payload.v1\0"
_MAX_COUNTER = (1 << 63) - 1
_MAX_DELAY_SEC = 366 * 24 * 60 * 60


class FailureJournalError(ValueError):
    """A durable acquisition failure journal violated its canonical schema."""


class FailureDisposition(StrEnum):
    BACKOFF = "backoff"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class CrossRunFailurePolicy:
    """Scheduling policy applied after one run exhausts its local retry budget."""

    backoff_base_sec: float = 300.0
    backoff_max_sec: float = 86_400.0
    integrity_quarantine_after: int = 3

    def __post_init__(self) -> None:
        base = _finite_float(
            self.backoff_base_sec,
            "backoff_base_sec",
            minimum=1.0,
            maximum=float(_MAX_DELAY_SEC),
        )
        maximum = _finite_float(
            self.backoff_max_sec,
            "backoff_max_sec",
            minimum=base,
            maximum=float(_MAX_DELAY_SEC),
        )
        threshold = _bounded_int(
            self.integrity_quarantine_after,
            "integrity_quarantine_after",
            minimum=1,
            maximum=_MAX_COUNTER,
        )
        object.__setattr__(self, "backoff_base_sec", base)
        object.__setattr__(self, "backoff_max_sec", maximum)
        object.__setattr__(self, "integrity_quarantine_after", threshold)

    def backoff_delay(self, consecutive_failures: int) -> float:
        failures = _bounded_int(
            consecutive_failures,
            "consecutive_failures",
            minimum=1,
            maximum=_MAX_COUNTER,
        )
        if self.backoff_base_sec >= self.backoff_max_sec:
            return self.backoff_max_sec
        doublings_to_cap = math.ceil(math.log2(self.backoff_max_sec / self.backoff_base_sec))
        if failures - 1 >= doublings_to_cap:
            return self.backoff_max_sec
        return min(
            self.backoff_max_sec,
            math.ldexp(self.backoff_base_sec, failures - 1),
        )


@dataclass(frozen=True, slots=True)
class ActiveFailure:
    asset_id: str
    source_id: str
    revision: str
    resolution: str
    event_id: str
    run_id: str
    recorded_at: datetime
    failure: AcquisitionFailure
    consecutive_failures: int
    integrity_streak: int
    attempts_in_run: int
    disposition: FailureDisposition
    next_eligible_at: datetime | None
    retry_after_deadline: datetime | None

    def eligible(self, *, now: datetime, retry_quarantined: bool = False) -> bool:
        checked_now = _aware_utc_datetime(now, "now")
        if not isinstance(retry_quarantined, bool):
            raise FailureJournalError("retry_quarantined must be a boolean")
        if self.disposition is FailureDisposition.QUARANTINED:
            return retry_quarantined
        if self.next_eligible_at is None:
            raise FailureJournalError("backoff failure has no eligibility deadline")
        return checked_now >= self.next_eligible_at


def empty_failure_journal(*, source: str, asset_type: str) -> dict[str, Any]:
    return {
        "schema_version": FAILURE_JOURNAL_SCHEMA_VERSION,
        "source": _identifier(source, "source"),
        "asset_type": _identifier(asset_type, "asset_type"),
        "updated_at": None,
        "next_sequence": 1,
        "head_event_sha256": None,
        "events": [],
    }


def validate_failure_journal(
    payload: Any,
    *,
    source: str,
    asset_type: str,
) -> dict[str, ActiveFailure]:
    """Validate the complete event stream and replay its active records."""

    expected_source = _identifier(source, "source")
    expected_asset_type = _identifier(asset_type, "asset_type")
    root = _exact_object(
        payload,
        {
            "schema_version",
            "source",
            "asset_type",
            "updated_at",
            "next_sequence",
            "head_event_sha256",
            "events",
        },
        "failure journal",
    )
    if root["schema_version"] != FAILURE_JOURNAL_SCHEMA_VERSION or isinstance(
        root["schema_version"], bool
    ):
        raise FailureJournalError("failure journal schema version is invalid")
    if root["source"] != expected_source or root["asset_type"] != expected_asset_type:
        raise FailureJournalError("failure journal source identity is invalid")
    events = root["events"]
    if not isinstance(events, list):
        raise FailureJournalError("failure journal events must be an array")
    next_sequence = _bounded_int(
        root["next_sequence"],
        "failure journal next_sequence",
        minimum=1,
        maximum=_MAX_COUNTER,
    )
    if next_sequence != len(events) + 1:
        raise FailureJournalError("failure journal sequence does not match its event count")
    updated_at = root["updated_at"]
    if not events:
        if updated_at is not None or root["head_event_sha256"] is not None:
            raise FailureJournalError("empty failure journal must not have a head")
        return {}
    checked_updated_at = _timestamp(updated_at, "failure journal updated_at")
    checked_head = _sha256(root["head_event_sha256"], "failure journal head_event_sha256")

    active: dict[str, ActiveFailure] = {}
    seen_event_ids: set[str] = set()
    previous_recorded_at: datetime | None = None
    previous_event_sha256: str | None = None
    for index, raw_event in enumerate(events, start=1):
        event = _exact_object(
            raw_event,
            _event_keys(raw_event),
            f"failure journal event {index}",
        )
        sequence = _bounded_int(
            event["sequence"],
            f"failure journal event {index} sequence",
            minimum=1,
            maximum=_MAX_COUNTER,
        )
        if sequence != index:
            raise FailureJournalError("failure journal event sequence is not contiguous")
        event_id = _sha256(event["event_id"], f"failure journal event {index} id")
        if event_id in seen_event_ids:
            raise FailureJournalError("failure journal event id is duplicated")
        seen_event_ids.add(event_id)
        expected_event_id = _event_id(
            run_id=event["run_id"],
            attempt_id=event["attempt_id"],
            event_type=event["type"],
        )
        if event_id != expected_event_id:
            raise FailureJournalError("failure journal event id does not bind its identity")
        raw_previous = event["previous_event_sha256"]
        if raw_previous is not None:
            raw_previous = _sha256(
                raw_previous,
                f"failure journal event {index} previous_event_sha256",
            )
        if raw_previous != previous_event_sha256:
            raise FailureJournalError("failure journal hash chain is broken")
        event_sha256 = _sha256(
            event["event_sha256"],
            f"failure journal event {index} event_sha256",
        )
        expected_event_sha256 = _event_payload_sha256(
            {key: value for key, value in event.items() if key != "event_sha256"}
        )
        if event_sha256 != expected_event_sha256:
            raise FailureJournalError("failure journal event hash does not bind its payload")
        previous_event_sha256 = event_sha256
        recorded_at = _timestamp(event["recorded_at"], f"failure journal event {index} recorded_at")
        if previous_recorded_at is not None and recorded_at < previous_recorded_at:
            raise FailureJournalError("failure journal event timestamps move backwards")
        previous_recorded_at = recorded_at
        run_id = _text(event["run_id"], f"failure journal event {index} run_id", 128)
        _text(event["attempt_id"], f"failure journal event {index} attempt_id", 160)
        asset_id = _text(event["asset_id"], f"failure journal event {index} asset_id", 128)
        source_id = _text(event["source_id"], f"failure journal event {index} source_id", 128)
        revision = _text(event["revision"], f"failure journal event {index} revision", 128)
        resolution = _text(
            event["resolution"],
            f"failure journal event {index} resolution",
            32,
        )
        event_type = event["type"]
        previous = active.get(asset_id)
        if event_type == "failed":
            failure = _failure_from_payload(
                event["failure"],
                context=f"failure journal event {index} failure",
            )
            consecutive = _bounded_int(
                event["consecutive_failures"],
                f"failure journal event {index} consecutive_failures",
                minimum=1,
                maximum=_MAX_COUNTER,
            )
            attempts_in_run = _bounded_int(
                event["attempts_in_run"],
                f"failure journal event {index} attempts_in_run",
                minimum=0,
                maximum=_MAX_COUNTER,
            )
            integrity_streak = _bounded_int(
                event["integrity_streak"],
                f"failure journal event {index} integrity_streak",
                minimum=0,
                maximum=_MAX_COUNTER,
            )
            expected_integrity_streak = (
                (previous.integrity_streak + 1)
                if failure.category is FailureCategory.INTEGRITY
                and previous is not None
                and previous.failure.category is FailureCategory.INTEGRITY
                else 1
                if failure.category is FailureCategory.INTEGRITY
                else 0
            )
            if integrity_streak != expected_integrity_streak:
                raise FailureJournalError("failure journal integrity streak is not replayable")
            expected_consecutive = 1 if previous is None else previous.consecutive_failures + 1
            if consecutive != expected_consecutive:
                raise FailureJournalError("failure journal consecutive count is not replayable")
            recorded_policy = _policy_from_payload(
                event["policy"],
                context=f"failure journal event {index} policy",
            )
            disposition = _disposition(event["disposition"])
            next_eligible_at = (
                None
                if event["next_eligible_at"] is None
                else _timestamp(
                    event["next_eligible_at"],
                    f"failure journal event {index} next_eligible_at",
                )
            )
            retry_after_deadline = (
                None
                if event["retry_after_deadline"] is None
                else _timestamp(
                    event["retry_after_deadline"],
                    f"failure journal event {index} retry_after_deadline",
                )
            )
            if disposition is FailureDisposition.BACKOFF:
                if next_eligible_at is None or next_eligible_at < recorded_at:
                    raise FailureJournalError("backoff event has an invalid eligibility deadline")
                if next_eligible_at > recorded_at + timedelta(seconds=_MAX_DELAY_SEC):
                    raise FailureJournalError("backoff eligibility deadline exceeds the safety cap")
                if retry_after_deadline is not None and next_eligible_at < retry_after_deadline:
                    raise FailureJournalError("backoff ignores its Retry-After deadline")
            elif next_eligible_at is not None:
                raise FailureJournalError("quarantine event must not have an eligibility deadline")
            if retry_after_deadline is not None and retry_after_deadline < recorded_at:
                raise FailureJournalError("Retry-After deadline predates its event")
            if retry_after_deadline is not None and retry_after_deadline > recorded_at + timedelta(
                seconds=_MAX_DELAY_SEC
            ):
                raise FailureJournalError("Retry-After deadline exceeds the safety cap")
            if (
                failure.category is FailureCategory.PERMANENT
                and disposition is not FailureDisposition.QUARANTINED
            ):
                raise FailureJournalError("permanent failure must be quarantined")
            if (
                failure.category not in {FailureCategory.PERMANENT, FailureCategory.INTEGRITY}
                and disposition is not FailureDisposition.BACKOFF
            ):
                raise FailureJournalError("retryable failure must use backoff")
            expected_disposition = _failure_disposition(
                failure=failure,
                integrity_streak=integrity_streak,
                policy=recorded_policy,
            )
            if disposition is not expected_disposition:
                raise FailureJournalError("failure disposition differs from its recorded policy")
            expected_next_eligible_at: datetime | None = None
            if expected_disposition is FailureDisposition.BACKOFF:
                expected_next_eligible_at = _ceil_utc_datetime(
                    recorded_at + timedelta(seconds=recorded_policy.backoff_delay(consecutive)),
                    "expected next_eligible_at",
                )
                if (
                    retry_after_deadline is not None
                    and retry_after_deadline > expected_next_eligible_at
                ):
                    expected_next_eligible_at = retry_after_deadline
            if next_eligible_at != expected_next_eligible_at:
                raise FailureJournalError(
                    "failure eligibility deadline differs from its recorded policy"
                )
            if previous is not None and (
                previous.source_id != source_id or previous.revision != revision
            ):
                raise FailureJournalError("failure journal asset identity changed between events")
            active[asset_id] = ActiveFailure(
                asset_id=asset_id,
                source_id=source_id,
                revision=revision,
                resolution=resolution,
                event_id=event_id,
                run_id=run_id,
                recorded_at=recorded_at,
                failure=failure,
                consecutive_failures=consecutive,
                integrity_streak=integrity_streak,
                attempts_in_run=attempts_in_run,
                disposition=disposition,
                next_eligible_at=next_eligible_at,
                retry_after_deadline=retry_after_deadline,
            )
        elif event_type in {"resolved", "released"}:
            if previous is None:
                raise FailureJournalError("failure-clearing event has no active failure")
            if previous.source_id != source_id or previous.revision != revision:
                raise FailureJournalError("resolution event identity differs from its failure")
            if event["failure_event_id"] != previous.event_id:
                raise FailureJournalError(
                    "failure-clearing event does not reference the active failure"
                )
            if event_type == "released":
                _text(event["reason"], f"failure journal event {index} reason", 256)
            active.pop(asset_id)
        else:
            raise FailureJournalError("failure journal event type is invalid")
    if checked_updated_at != previous_recorded_at:
        raise FailureJournalError("failure journal updated_at differs from its final event")
    if checked_head != previous_event_sha256:
        raise FailureJournalError("failure journal head differs from its final event")
    return active


def append_failure_event(
    payload: dict[str, Any],
    *,
    source: str,
    asset_type: str,
    asset_id: str,
    source_id: str,
    revision: str,
    resolution: str,
    run_id: str,
    attempt_id: str,
    failure: AcquisitionFailure,
    recorded_at: datetime,
    policy: CrossRunFailurePolicy,
    retry_after_deadline: datetime | None = None,
    attempts_in_run: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    active = validate_failure_journal(payload, source=source, asset_type=asset_type)
    if not isinstance(failure, AcquisitionFailure):
        raise FailureJournalError("failure must be AcquisitionFailure")
    if not isinstance(policy, CrossRunFailurePolicy):
        raise FailureJournalError("policy must be CrossRunFailurePolicy")
    checked_recorded_at = _utc_datetime(recorded_at, "recorded_at")
    checked_asset_id = _text(asset_id, "asset_id", 128)
    checked_source_id = _text(source_id, "source_id", 128)
    checked_revision = _text(revision, "revision", 128)
    checked_resolution = _text(resolution, "resolution", 32)
    checked_run_id = _text(run_id, "run_id", 128)
    checked_attempt_id = _text(attempt_id, "attempt_id", 160)
    checked_attempts_in_run = _bounded_int(
        attempts_in_run,
        "attempts_in_run",
        minimum=0,
        maximum=_MAX_COUNTER,
    )
    checked_retry_after_deadline = (
        None
        if retry_after_deadline is None
        else _ceil_utc_datetime(retry_after_deadline, "retry_after_deadline")
    )
    if (
        checked_retry_after_deadline is not None
        and checked_retry_after_deadline < checked_recorded_at
    ):
        # The journal clock is deliberately monotonic across wall-clock
        # rollback.  A provider deadline already behind that logical clock no
        # longer adds a constraint; policy backoff still begins at recorded_at.
        checked_retry_after_deadline = None
    expected_event_id = _event_id(
        run_id=checked_run_id,
        attempt_id=checked_attempt_id,
        event_type="failed",
    )
    existing = _event_by_id(payload, expected_event_id)
    if existing is not None:
        if (
            existing.get("asset_id") != checked_asset_id
            or existing.get("source_id") != checked_source_id
            or existing.get("revision") != checked_revision
            or existing.get("resolution") != checked_resolution
            or existing.get("failure") != _failure_payload(failure)
            or existing.get("attempts_in_run") != checked_attempts_in_run
            or existing.get("policy") != _policy_payload(policy)
        ):
            raise FailureJournalError("failure event id conflicts with its existing payload")
        current = active.get(checked_asset_id)
        if current is None or current.event_id != expected_event_id:
            raise FailureJournalError("failure event is no longer the active outcome")
        return json.loads(json.dumps(payload)), json.loads(json.dumps(existing))
    previous = active.get(checked_asset_id)
    if previous is not None and (
        previous.source_id != checked_source_id or previous.revision != checked_revision
    ):
        raise FailureJournalError("failure asset identity conflicts with its active record")
    consecutive = 1 if previous is None else previous.consecutive_failures + 1
    integrity_streak = (
        (previous.integrity_streak + 1)
        if failure.category is FailureCategory.INTEGRITY
        and previous is not None
        and previous.failure.category is FailureCategory.INTEGRITY
        else 1
        if failure.category is FailureCategory.INTEGRITY
        else 0
    )
    disposition = _failure_disposition(
        failure=failure,
        integrity_streak=integrity_streak,
        policy=policy,
    )
    if disposition is FailureDisposition.BACKOFF:
        next_eligible_at = _ceil_utc_datetime(
            checked_recorded_at + timedelta(seconds=policy.backoff_delay(consecutive)),
            "next_eligible_at",
        )
        if (
            checked_retry_after_deadline is not None
            and checked_retry_after_deadline > next_eligible_at
        ):
            next_eligible_at = checked_retry_after_deadline
    else:
        next_eligible_at = None
    event_without_hash: dict[str, Any] = {
        "sequence": payload["next_sequence"],
        "event_id": expected_event_id,
        "previous_event_sha256": payload["head_event_sha256"],
        "type": "failed",
        "recorded_at": _timestamp_text(checked_recorded_at),
        "run_id": checked_run_id,
        "attempt_id": checked_attempt_id,
        "asset_id": checked_asset_id,
        "source_id": checked_source_id,
        "revision": checked_revision,
        "resolution": checked_resolution,
        "failure": _failure_payload(failure),
        "policy": _policy_payload(policy),
        "consecutive_failures": consecutive,
        "integrity_streak": integrity_streak,
        "attempts_in_run": checked_attempts_in_run,
        "disposition": disposition.value,
        "next_eligible_at": (
            None if next_eligible_at is None else _timestamp_text(next_eligible_at)
        ),
        "retry_after_deadline": (
            None
            if checked_retry_after_deadline is None
            else _timestamp_text(checked_retry_after_deadline)
        ),
    }
    event = dict(event_without_hash)
    event["event_sha256"] = _event_payload_sha256(event_without_hash)
    result = json.loads(json.dumps(payload))
    result["events"].append(event)
    result["next_sequence"] = event["sequence"] + 1
    result["updated_at"] = event["recorded_at"]
    result["head_event_sha256"] = event["event_sha256"]
    validate_failure_journal(result, source=source, asset_type=asset_type)
    return result, event


def append_resolution_event(
    payload: dict[str, Any],
    *,
    source: str,
    asset_type: str,
    asset_id: str,
    source_id: str,
    revision: str,
    resolution: str,
    run_id: str,
    attempt_id: str,
    recorded_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    active = validate_failure_journal(payload, source=source, asset_type=asset_type)
    checked_asset_id = _text(asset_id, "asset_id", 128)
    checked_source_id = _text(source_id, "source_id", 128)
    checked_revision = _text(revision, "revision", 128)
    checked_resolution = _text(resolution, "resolution", 32)
    checked_recorded_at = _utc_datetime(recorded_at, "recorded_at")
    checked_run_id = _text(run_id, "run_id", 128)
    checked_attempt_id = _text(attempt_id, "attempt_id", 160)
    expected_event_id = _event_id(
        run_id=checked_run_id,
        attempt_id=checked_attempt_id,
        event_type="resolved",
    )
    existing = _event_by_id(payload, expected_event_id)
    if existing is not None:
        if (
            existing.get("asset_id") != checked_asset_id
            or existing.get("source_id") != checked_source_id
            or existing.get("revision") != checked_revision
            or existing.get("resolution") != checked_resolution
        ):
            raise FailureJournalError("resolution event id conflicts with its existing payload")
        if active.get(checked_asset_id) is not None:
            raise FailureJournalError("resolution event is stale relative to a newer failure")
        return json.loads(json.dumps(payload)), json.loads(json.dumps(existing))
    previous = active.get(checked_asset_id)
    if previous is None:
        return json.loads(json.dumps(payload)), None
    if previous.source_id != checked_source_id or previous.revision != checked_revision:
        raise FailureJournalError("resolution asset identity conflicts with its active record")
    event_without_hash: dict[str, Any] = {
        "sequence": payload["next_sequence"],
        "event_id": expected_event_id,
        "previous_event_sha256": payload["head_event_sha256"],
        "type": "resolved",
        "recorded_at": _timestamp_text(checked_recorded_at),
        "run_id": checked_run_id,
        "attempt_id": checked_attempt_id,
        "asset_id": checked_asset_id,
        "source_id": checked_source_id,
        "revision": checked_revision,
        "resolution": checked_resolution,
        "failure_event_id": previous.event_id,
    }
    event = dict(event_without_hash)
    event["event_sha256"] = _event_payload_sha256(event_without_hash)
    result = json.loads(json.dumps(payload))
    result["events"].append(event)
    result["next_sequence"] = event["sequence"] + 1
    result["updated_at"] = event["recorded_at"]
    result["head_event_sha256"] = event["event_sha256"]
    validate_failure_journal(result, source=source, asset_type=asset_type)
    return result, event


def append_release_event(
    payload: dict[str, Any],
    *,
    source: str,
    asset_type: str,
    asset_id: str,
    source_id: str,
    revision: str,
    resolution: str,
    run_id: str,
    attempt_id: str,
    recorded_at: datetime,
    reason: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    active = validate_failure_journal(payload, source=source, asset_type=asset_type)
    checked_asset_id = _text(asset_id, "asset_id", 128)
    checked_source_id = _text(source_id, "source_id", 128)
    checked_revision = _text(revision, "revision", 128)
    checked_resolution = _text(resolution, "resolution", 32)
    checked_recorded_at = _utc_datetime(recorded_at, "recorded_at")
    checked_run_id = _text(run_id, "run_id", 128)
    checked_attempt_id = _text(attempt_id, "attempt_id", 160)
    checked_reason = _text(reason, "reason", 256)
    expected_event_id = _event_id(
        run_id=checked_run_id,
        attempt_id=checked_attempt_id,
        event_type="released",
    )
    existing = _event_by_id(payload, expected_event_id)
    if existing is not None:
        if (
            existing.get("asset_id") != checked_asset_id
            or existing.get("source_id") != checked_source_id
            or existing.get("revision") != checked_revision
            or existing.get("resolution") != checked_resolution
            or existing.get("reason") != checked_reason
        ):
            raise FailureJournalError("release event id conflicts with its existing payload")
        if active.get(checked_asset_id) is not None:
            raise FailureJournalError("release event is stale relative to a newer failure")
        return json.loads(json.dumps(payload)), json.loads(json.dumps(existing))
    previous = active.get(checked_asset_id)
    if previous is None:
        raise FailureJournalError("release target has no active failure")
    if previous.source_id != checked_source_id or previous.revision != checked_revision:
        raise FailureJournalError("release asset identity conflicts with its active record")
    event_without_hash: dict[str, Any] = {
        "sequence": payload["next_sequence"],
        "event_id": expected_event_id,
        "previous_event_sha256": payload["head_event_sha256"],
        "type": "released",
        "recorded_at": _timestamp_text(checked_recorded_at),
        "run_id": checked_run_id,
        "attempt_id": checked_attempt_id,
        "asset_id": checked_asset_id,
        "source_id": checked_source_id,
        "revision": checked_revision,
        "resolution": checked_resolution,
        "failure_event_id": previous.event_id,
        "reason": checked_reason,
    }
    event = dict(event_without_hash)
    event["event_sha256"] = _event_payload_sha256(event_without_hash)
    result = json.loads(json.dumps(payload))
    result["events"].append(event)
    result["next_sequence"] = event["sequence"] + 1
    result["updated_at"] = event["recorded_at"]
    result["head_event_sha256"] = event["event_sha256"]
    validate_failure_journal(result, source=source, asset_type=asset_type)
    return result, event


def _event_keys(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    event_type = value.get("type")
    shared = {
        "sequence",
        "event_id",
        "previous_event_sha256",
        "event_sha256",
        "type",
        "recorded_at",
        "run_id",
        "attempt_id",
        "asset_id",
        "source_id",
        "revision",
        "resolution",
    }
    if event_type == "failed":
        return shared | {
            "failure",
            "policy",
            "consecutive_failures",
            "integrity_streak",
            "attempts_in_run",
            "disposition",
            "next_eligible_at",
            "retry_after_deadline",
        }
    if event_type == "resolved":
        return shared | {"failure_event_id"}
    if event_type == "released":
        return shared | {"failure_event_id", "reason"}
    return shared


def _failure_disposition(
    *,
    failure: AcquisitionFailure,
    integrity_streak: int,
    policy: CrossRunFailurePolicy,
) -> FailureDisposition:
    if failure.category is FailureCategory.PERMANENT:
        return FailureDisposition.QUARANTINED
    if (
        failure.category is FailureCategory.INTEGRITY
        and integrity_streak >= policy.integrity_quarantine_after
    ):
        return FailureDisposition.QUARANTINED
    return FailureDisposition.BACKOFF


def _disposition(value: Any) -> FailureDisposition:
    try:
        return FailureDisposition(value)
    except (TypeError, ValueError) as exc:
        raise FailureJournalError("failure journal disposition is invalid") from exc


def _failure_payload(failure: AcquisitionFailure) -> dict[str, Any]:
    return {
        "category": failure.category.value,
        "kind": failure.kind.value,
        "phase": failure.phase,
        "message": failure.message,
        "http_status": failure.http_status,
    }


def _policy_payload(policy: CrossRunFailurePolicy) -> dict[str, Any]:
    return {
        "backoff_base_sec": policy.backoff_base_sec,
        "backoff_max_sec": policy.backoff_max_sec,
        "integrity_quarantine_after": policy.integrity_quarantine_after,
    }


def _policy_from_payload(value: Any, *, context: str) -> CrossRunFailurePolicy:
    payload = _exact_object(
        value,
        {"backoff_base_sec", "backoff_max_sec", "integrity_quarantine_after"},
        context,
    )
    if not isinstance(payload["backoff_base_sec"], float) or not isinstance(
        payload["backoff_max_sec"], float
    ):
        raise FailureJournalError(f"{context} delays must use canonical JSON floats")
    try:
        return CrossRunFailurePolicy(
            backoff_base_sec=payload["backoff_base_sec"],
            backoff_max_sec=payload["backoff_max_sec"],
            integrity_quarantine_after=payload["integrity_quarantine_after"],
        )
    except FailureJournalError as exc:
        raise FailureJournalError(f"{context} is invalid") from exc


def _failure_from_payload(value: Any, *, context: str) -> AcquisitionFailure:
    payload = _exact_object(
        value,
        {"category", "kind", "phase", "message", "http_status"},
        context,
    )
    try:
        kind = FailureKind(payload["kind"])
        category = FailureCategory(payload["category"])
        failure = AcquisitionFailure(
            kind=kind,
            phase=payload["phase"],
            message=payload["message"],
            http_status=payload["http_status"],
        )
    except (RuntimeValidationError, TypeError, ValueError) as exc:
        raise FailureJournalError(f"{context} is invalid") from exc
    if failure.category is not category:
        raise FailureJournalError(f"{context} category differs from its kind")
    return failure


def _event_id(*, run_id: Any, attempt_id: Any, event_type: Any) -> str:
    identity = {
        "run_id": _text(run_id, "event run_id", 128),
        "attempt_id": _text(attempt_id, "event attempt_id", 160),
        "type": _text(event_type, "event type", 32),
    }
    rendered = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(_EVENT_ID_DOMAIN + rendered).hexdigest()


def _event_payload_sha256(event_without_hash: dict[str, Any]) -> str:
    rendered = json.dumps(
        event_without_hash,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(_EVENT_SHA256_DOMAIN + rendered).hexdigest()


def _event_by_id(payload: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    for event in payload["events"]:
        if isinstance(event, dict) and event.get("event_id") == event_id:
            return event
    return None


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise FailureJournalError(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise FailureJournalError(f"{field} must be a canonical UTC timestamp") from exc
    return parsed


def _timestamp_text(value: datetime) -> str:
    return _utc_datetime(value, "timestamp").strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_datetime(value: Any, field: str) -> datetime:
    checked = _aware_utc_datetime(value, field)
    if checked.microsecond:
        raise FailureJournalError(f"{field} must have whole-second precision")
    return checked


def _aware_utc_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FailureJournalError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _ceil_utc_datetime(value: Any, field: str) -> datetime:
    checked = _aware_utc_datetime(value, field)
    if checked.microsecond:
        checked = checked.replace(microsecond=0) + timedelta(seconds=1)
    return checked


def _exact_object(value: Any, keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise FailureJournalError(f"{field} has an unsupported shape")
    if any(not isinstance(key, str) for key in value):
        raise FailureJournalError(f"{field} keys must be strings")
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise FailureJournalError(f"{field} must be non-empty normalized text")
    return value


def _identifier(value: Any, field: str) -> str:
    checked = _text(value, field, 64)
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in checked):
        raise FailureJournalError(f"{field} is not a canonical identifier")
    return checked


def _sha256(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FailureJournalError(f"{field} must be lowercase SHA-256")
    return value


def _bounded_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise FailureJournalError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _finite_float(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise FailureJournalError(
            f"{field} must be a finite number between {minimum} and {maximum}"
        )
    return float(value)
