from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from uefactory.acquire import failure_journal as journal_module
from uefactory.acquire.failure_journal import (
    CrossRunFailurePolicy,
    FailureDisposition,
    FailureJournalError,
    append_failure_event,
    append_release_event,
    append_resolution_event,
    empty_failure_journal,
    validate_failure_journal,
)
from uefactory.acquire.runtime import AcquisitionFailure, FailureKind

NOW = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
SOURCE = "polyhaven"
ASSET_TYPE = "models"
ASSET_ID = "polyhaven_bad_asset_0123456789ab"
SOURCE_ID = "bad_asset"
REVISION = "0" * 40
RESOLUTION = "1k"
RUN_ID = "20260711T080000Z_deadbeef"


def _empty() -> dict[str, object]:
    return empty_failure_journal(source=SOURCE, asset_type=ASSET_TYPE)


def _failure(kind: FailureKind = FailureKind.TRANSPORT) -> AcquisitionFailure:
    if kind is FailureKind.HTTP_PERMANENT:
        return AcquisitionFailure.from_http(
            phase="files_api",
            status=404,
            message="Poly Haven files API returned HTTP 404",
        )
    return AcquisitionFailure(
        kind=kind,
        phase="download",
        message=f"controlled {kind.value} failure",
    )


def _append(
    payload: dict[str, object],
    *,
    attempt: int,
    failure: AcquisitionFailure | None = None,
    recorded_at: datetime | None = None,
    policy: CrossRunFailurePolicy | None = None,
    retry_after_deadline: datetime | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    result, event = append_failure_event(
        payload,  # type: ignore[arg-type]
        source=SOURCE,
        asset_type=ASSET_TYPE,
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        revision=REVISION,
        resolution=RESOLUTION,
        run_id=f"{RUN_ID}_{attempt}",
        attempt_id=f"{RUN_ID}_{attempt}:1",
        failure=failure or _failure(),
        recorded_at=recorded_at or NOW + timedelta(minutes=attempt),
        policy=policy or CrossRunFailurePolicy(),
        retry_after_deadline=retry_after_deadline,
    )
    return result, event


def test_empty_failure_journal_is_canonical_and_has_no_active_records() -> None:
    payload = _empty()

    assert (
        validate_failure_journal(
            payload,
            source=SOURCE,
            asset_type=ASSET_TYPE,
        )
        == {}
    )
    assert payload == {
        "schema_version": 1,
        "source": SOURCE,
        "asset_type": ASSET_TYPE,
        "updated_at": None,
        "next_sequence": 1,
        "head_event_sha256": None,
        "events": [],
    }


def test_permanent_failure_is_immediately_quarantined_and_hash_chained() -> None:
    payload, event = _append(
        _empty(),
        attempt=1,
        failure=_failure(FailureKind.HTTP_PERMANENT),
    )
    active = validate_failure_journal(payload, source=SOURCE, asset_type=ASSET_TYPE)
    record = active[ASSET_ID]

    assert event["sequence"] == 1
    assert event["previous_event_sha256"] is None
    assert event["event_sha256"] == payload["head_event_sha256"]
    assert record.disposition is FailureDisposition.QUARANTINED
    assert record.next_eligible_at is None
    assert record.failure.http_status == 404
    assert record.eligible(now=NOW + timedelta(days=30)) is False
    assert record.eligible(now=NOW, retry_quarantined=True) is True


def test_transient_failures_back_off_across_runs_and_append_is_idempotent() -> None:
    policy = CrossRunFailurePolicy(backoff_base_sec=300, backoff_max_sec=600)
    first, first_event = _append(_empty(), attempt=1, policy=policy)
    second, second_event = _append(first, attempt=2, policy=policy)
    third, third_event = _append(second, attempt=3, policy=policy)
    replayed, replayed_event = _append(third, attempt=3, policy=policy)
    active = validate_failure_journal(third, source=SOURCE, asset_type=ASSET_TYPE)

    assert first_event["next_eligible_at"] == "2026-07-11T08:06:00Z"
    assert second_event["next_eligible_at"] == "2026-07-11T08:12:00Z"
    assert third_event["next_eligible_at"] == "2026-07-11T08:13:00Z"
    assert second_event["previous_event_sha256"] == first_event["event_sha256"]
    assert third_event["previous_event_sha256"] == second_event["event_sha256"]
    assert replayed == third
    assert replayed_event == third_event
    assert active[ASSET_ID].consecutive_failures == 3


def test_retry_after_extends_cross_run_deadline_without_changing_policy() -> None:
    deadline = NOW + timedelta(hours=2)
    payload, event = _append(
        _empty(),
        attempt=0,
        recorded_at=NOW,
        retry_after_deadline=deadline,
    )
    record = validate_failure_journal(
        payload,
        source=SOURCE,
        asset_type=ASSET_TYPE,
    )[ASSET_ID]

    assert event["retry_after_deadline"] == "2026-07-11T10:00:00Z"
    assert event["next_eligible_at"] == "2026-07-11T10:00:00Z"
    assert record.eligible(now=deadline - timedelta(seconds=1)) is False
    assert record.eligible(now=deadline) is True


def test_subsecond_query_and_deadlines_are_canonicalized_without_shortening_wait() -> None:
    retry_after = NOW + timedelta(seconds=10, microseconds=1)
    payload, event = _append(
        _empty(),
        attempt=0,
        recorded_at=NOW,
        policy=CrossRunFailurePolicy(backoff_base_sec=1.5),
        retry_after_deadline=retry_after,
    )
    record = validate_failure_journal(
        payload,
        source=SOURCE,
        asset_type=ASSET_TYPE,
    )[ASSET_ID]

    assert event["retry_after_deadline"] == "2026-07-11T08:00:11Z"
    assert event["next_eligible_at"] == "2026-07-11T08:00:11Z"
    assert record.eligible(now=NOW + timedelta(seconds=10, microseconds=999_999)) is False
    assert record.eligible(now=NOW + timedelta(seconds=11, microseconds=1)) is True


def test_integrity_failure_quarantines_only_at_the_configured_run_threshold() -> None:
    policy = CrossRunFailurePolicy(integrity_quarantine_after=3)
    payload = _empty()
    dispositions: list[str] = []
    for attempt in range(1, 4):
        payload, event = _append(
            payload,
            attempt=attempt,
            failure=_failure(FailureKind.INTEGRITY),
            policy=policy,
        )
        dispositions.append(str(event["disposition"]))

    assert dispositions == ["backoff", "backoff", "quarantined"]
    active = validate_failure_journal(payload, source=SOURCE, asset_type=ASSET_TYPE)
    assert active[ASSET_ID].consecutive_failures == 3
    assert active[ASSET_ID].next_eligible_at is None


def test_non_integrity_failures_do_not_advance_integrity_quarantine_streak() -> None:
    policy = CrossRunFailurePolicy(integrity_quarantine_after=2)
    payload, deferred = _append(
        _empty(),
        attempt=1,
        failure=_failure(FailureKind.DISK),
        policy=policy,
    )
    payload, first_integrity = _append(
        payload,
        attempt=2,
        failure=_failure(FailureKind.INTEGRITY),
        policy=policy,
    )
    payload, transient = _append(
        payload,
        attempt=3,
        failure=_failure(FailureKind.TRANSPORT),
        policy=policy,
    )
    payload, reset_integrity = _append(
        payload,
        attempt=4,
        failure=_failure(FailureKind.INTEGRITY),
        policy=policy,
    )
    payload, second_integrity = _append(
        payload,
        attempt=5,
        failure=_failure(FailureKind.INTEGRITY),
        policy=policy,
    )

    assert deferred["integrity_streak"] == 0
    assert first_integrity["integrity_streak"] == 1
    assert first_integrity["disposition"] == "backoff"
    assert transient["integrity_streak"] == 0
    assert reset_integrity["integrity_streak"] == 1
    assert reset_integrity["disposition"] == "backoff"
    assert second_integrity["integrity_streak"] == 2
    assert second_integrity["disposition"] == "quarantined"


def test_same_provider_revision_can_retry_at_a_different_resolution() -> None:
    failed, _ = _append(_empty(), attempt=1)
    retried, event = append_failure_event(
        failed,  # type: ignore[arg-type]
        source=SOURCE,
        asset_type=ASSET_TYPE,
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        revision=REVISION,
        resolution="2k",
        run_id=f"{RUN_ID}_2",
        attempt_id=f"{RUN_ID}_2:1",
        failure=_failure(),
        recorded_at=NOW + timedelta(hours=1),
        policy=CrossRunFailurePolicy(),
    )

    active = validate_failure_journal(
        retried,
        source=SOURCE,
        asset_type=ASSET_TYPE,
    )[ASSET_ID]
    assert event["resolution"] == "2k"
    assert active.resolution == "2k"
    assert active.consecutive_failures == 2


def test_successful_retry_appends_resolution_and_clears_active_failure() -> None:
    failed, failure_event = _append(_empty(), attempt=1)
    resolved, resolution_event = append_resolution_event(
        failed,  # type: ignore[arg-type]
        source=SOURCE,
        asset_type=ASSET_TYPE,
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        revision=REVISION,
        resolution=RESOLUTION,
        run_id=f"{RUN_ID}_2",
        attempt_id=f"{RUN_ID}_2:1",
        recorded_at=NOW + timedelta(hours=1),
    )

    assert resolution_event is not None
    assert resolution_event["failure_event_id"] == failure_event["event_id"]
    assert resolution_event["previous_event_sha256"] == failure_event["event_sha256"]
    assert (
        validate_failure_journal(
            resolved,
            source=SOURCE,
            asset_type=ASSET_TYPE,
        )
        == {}
    )

    unchanged, duplicate = append_resolution_event(
        resolved,  # type: ignore[arg-type]
        source=SOURCE,
        asset_type=ASSET_TYPE,
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        revision=REVISION,
        resolution=RESOLUTION,
        run_id=f"{RUN_ID}_2",
        attempt_id=f"{RUN_ID}_2:1",
        recorded_at=NOW + timedelta(hours=1),
    )
    assert unchanged == resolved
    assert duplicate == resolution_event


def test_old_resolution_cannot_clear_a_newer_active_failure() -> None:
    failed, _ = _append(_empty(), attempt=1)
    resolved, _ = append_resolution_event(
        failed,  # type: ignore[arg-type]
        source=SOURCE,
        asset_type=ASSET_TYPE,
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        revision=REVISION,
        resolution=RESOLUTION,
        run_id=f"{RUN_ID}_resolve",
        attempt_id=f"{RUN_ID}_resolve:1",
        recorded_at=NOW + timedelta(hours=1),
    )
    newer, newer_event = _append(
        resolved,
        attempt=2,
        recorded_at=NOW + timedelta(hours=2),
    )

    with pytest.raises(FailureJournalError, match="stale relative to a newer failure"):
        append_resolution_event(
            newer,  # type: ignore[arg-type]
            source=SOURCE,
            asset_type=ASSET_TYPE,
            asset_id=ASSET_ID,
            source_id=SOURCE_ID,
            revision=REVISION,
            resolution=RESOLUTION,
            run_id=f"{RUN_ID}_resolve",
            attempt_id=f"{RUN_ID}_resolve:1",
            recorded_at=NOW + timedelta(hours=1),
        )
    active = validate_failure_journal(newer, source=SOURCE, asset_type=ASSET_TYPE)
    assert active[ASSET_ID].event_id == newer_event["event_id"]


def test_old_failure_event_cannot_replay_over_a_newer_active_failure() -> None:
    first, _ = _append(_empty(), attempt=1)
    resolved, _ = append_resolution_event(
        first,  # type: ignore[arg-type]
        source=SOURCE,
        asset_type=ASSET_TYPE,
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        revision=REVISION,
        resolution=RESOLUTION,
        run_id=f"{RUN_ID}_resolve",
        attempt_id=f"{RUN_ID}_resolve:1",
        recorded_at=NOW + timedelta(hours=1),
    )
    newer, _ = _append(
        resolved,
        attempt=2,
        recorded_at=NOW + timedelta(hours=2),
    )

    with pytest.raises(FailureJournalError, match="no longer the active outcome"):
        _append(
            newer,
            attempt=1,
            recorded_at=NOW + timedelta(minutes=1),
        )


def test_targeted_release_is_audited_and_idempotently_clears_quarantine() -> None:
    failed, failure_event = _append(
        _empty(),
        attempt=1,
        failure=_failure(FailureKind.HTTP_PERMANENT),
    )
    released, release_event = append_release_event(
        failed,  # type: ignore[arg-type]
        source=SOURCE,
        asset_type=ASSET_TYPE,
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        revision=REVISION,
        resolution=RESOLUTION,
        run_id=f"{RUN_ID}_release",
        attempt_id=f"{RUN_ID}_release:1",
        recorded_at=NOW + timedelta(hours=1),
        reason="operator_requested_exact_revision_retry",
    )

    assert release_event["type"] == "released"
    assert release_event["failure_event_id"] == failure_event["event_id"]
    assert release_event["reason"] == "operator_requested_exact_revision_retry"
    assert (
        validate_failure_journal(
            released,
            source=SOURCE,
            asset_type=ASSET_TYPE,
        )
        == {}
    )

    replayed, replayed_event = append_release_event(
        released,
        source=SOURCE,
        asset_type=ASSET_TYPE,
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        revision=REVISION,
        resolution=RESOLUTION,
        run_id=f"{RUN_ID}_release",
        attempt_id=f"{RUN_ID}_release:1",
        recorded_at=NOW + timedelta(hours=1),
        reason="operator_requested_exact_revision_retry",
    )
    assert replayed == released
    assert replayed_event == release_event


def test_old_release_cannot_clear_a_newer_active_failure() -> None:
    failed, _ = _append(
        _empty(),
        attempt=1,
        failure=_failure(FailureKind.HTTP_PERMANENT),
    )
    released, _ = append_release_event(
        failed,  # type: ignore[arg-type]
        source=SOURCE,
        asset_type=ASSET_TYPE,
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        revision=REVISION,
        resolution=RESOLUTION,
        run_id=f"{RUN_ID}_release",
        attempt_id=f"{RUN_ID}_release:1",
        recorded_at=NOW + timedelta(hours=1),
        reason="operator_requested_exact_revision_retry",
    )
    newer, newer_event = _append(
        released,
        attempt=2,
        recorded_at=NOW + timedelta(hours=2),
    )

    with pytest.raises(FailureJournalError, match="stale relative to a newer failure"):
        append_release_event(
            newer,  # type: ignore[arg-type]
            source=SOURCE,
            asset_type=ASSET_TYPE,
            asset_id=ASSET_ID,
            source_id=SOURCE_ID,
            revision=REVISION,
            resolution=RESOLUTION,
            run_id=f"{RUN_ID}_release",
            attempt_id=f"{RUN_ID}_release:1",
            recorded_at=NOW + timedelta(hours=1),
            reason="operator_requested_exact_revision_retry",
        )
    active = validate_failure_journal(newer, source=SOURCE, asset_type=ASSET_TYPE)
    assert active[ASSET_ID].event_id == newer_event["event_id"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(head_event_sha256="f" * 64),
        lambda value: value.update(next_sequence=True),
        lambda value: value["events"][0].update(sequence=2),
        lambda value: value["events"][0].update(message="extra"),
        lambda value: value["events"][0]["failure"].update(message="tampered"),
        lambda value: value["events"][0].update(previous_event_sha256="0" * 64),
        lambda value: value["events"][0].update(event_sha256="0" * 64),
        lambda value: value["events"].append(json.loads(json.dumps(value["events"][0]))),
    ],
)
def test_failure_journal_rejects_schema_sequence_and_hash_tampering(mutate: object) -> None:
    payload, _ = _append(_empty(), attempt=1)
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(FailureJournalError):
        validate_failure_journal(payload, source=SOURCE, asset_type=ASSET_TYPE)


def test_failure_journal_rejects_identity_and_policy_drift() -> None:
    payload, _ = _append(_empty(), attempt=1)

    with pytest.raises(FailureJournalError, match="source identity"):
        validate_failure_journal(payload, source="other", asset_type=ASSET_TYPE)
    with pytest.raises(FailureJournalError):
        CrossRunFailurePolicy(backoff_base_sec=True)  # type: ignore[arg-type]
    with pytest.raises(FailureJournalError):
        CrossRunFailurePolicy(backoff_base_sec=0.5)
    with pytest.raises(FailureJournalError):
        CrossRunFailurePolicy(backoff_base_sec=10, backoff_max_sec=1)
    with pytest.raises(FailureJournalError):
        CrossRunFailurePolicy(integrity_quarantine_after=0)


@pytest.mark.parametrize(
    ("failure", "disposition", "next_eligible_at", "message"),
    [
        (
            _failure(FailureKind.HTTP_PERMANENT),
            "backoff",
            "2026-07-11T09:00:00Z",
            "permanent failure must be quarantined",
        ),
        (
            _failure(FailureKind.TRANSPORT),
            "quarantined",
            None,
            "retryable failure must use backoff",
        ),
    ],
)
def test_failure_journal_rejects_rehashed_impossible_dispositions(
    failure: AcquisitionFailure,
    disposition: str,
    next_eligible_at: str | None,
    message: str,
) -> None:
    payload, _ = _append(_empty(), attempt=1, failure=failure)
    event = payload["events"][0]
    event["disposition"] = disposition
    event["next_eligible_at"] = next_eligible_at
    event["event_sha256"] = journal_module._event_payload_sha256(
        {key: value for key, value in event.items() if key != "event_sha256"}
    )
    payload["head_event_sha256"] = event["event_sha256"]

    with pytest.raises(FailureJournalError, match=message):
        validate_failure_journal(payload, source=SOURCE, asset_type=ASSET_TYPE)


def test_failure_journal_policy_fully_binds_integrity_disposition_and_deadline() -> None:
    payload, _ = _append(
        _empty(),
        attempt=1,
        failure=_failure(FailureKind.INTEGRITY),
        policy=CrossRunFailurePolicy(
            backoff_base_sec=10,
            backoff_max_sec=20,
            integrity_quarantine_after=3,
        ),
    )
    event = payload["events"][0]
    event["policy"]["integrity_quarantine_after"] = 1
    event["event_sha256"] = journal_module._event_payload_sha256(
        {key: value for key, value in event.items() if key != "event_sha256"}
    )
    payload["head_event_sha256"] = event["event_sha256"]

    with pytest.raises(FailureJournalError, match="disposition differs from its recorded policy"):
        validate_failure_journal(payload, source=SOURCE, asset_type=ASSET_TYPE)
