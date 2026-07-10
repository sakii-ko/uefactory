from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime

import pytest

from uefactory.acquire.runtime import (
    AcquisitionFailure,
    DailyQuotaLimits,
    DailyQuotaRequest,
    DailyQuotaUsage,
    DiskQuotaLimits,
    DiskSnapshot,
    FailureCategory,
    FailureKind,
    HttpRequest,
    HttpResponse,
    HttpResponseMetadata,
    HttpTransport,
    MonotonicTokenBucket,
    QuotaExceeded,
    RetryAfterKind,
    RetryPolicy,
    RuntimeValidationError,
    SystemClock,
    classify_http_status,
    compute_retry_decision,
    parse_retry_after,
    reserve_daily_quota,
    reserve_disk_growth,
)

NOW = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
MAX_COUNTER = (1 << 63) - 1


@dataclass
class _FakeClock:
    wall: datetime = NOW
    monotonic_value: float = 0.0
    sleeps: list[float] = field(default_factory=list)
    advance_on_sleep: bool = True
    sleep_scale: float = 1.0

    def utc_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.advance_on_sleep:
            elapsed = seconds * self.sleep_scale
            self.monotonic_value += elapsed
            self.wall += timedelta(seconds=elapsed)


class _FakeResponse:
    def __init__(self, metadata: HttpResponseMetadata, payload: bytes = b"") -> None:
        self.metadata = metadata
        self._payload = payload
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            result, self._payload = self._payload, b""
            return result
        result, self._payload = self._payload[:size], self._payload[size:]
        return result

    def close(self) -> None:
        self.closed = True


class _FakeTransport:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.requests: list[HttpRequest] = []

    @contextmanager
    def open(self, request: HttpRequest) -> Iterator[HttpResponse]:
        self.requests.append(request)
        try:
            yield self.response
        finally:
            self.response.close()


def test_system_clock_exposes_aware_utc_and_monotonic_time() -> None:
    clock = SystemClock()

    assert clock.utc_now().tzinfo is UTC
    assert clock.monotonic() >= 0.0


def test_http_contract_validates_endpoints_headers_and_structural_transport() -> None:
    request = HttpRequest(
        url="https://api.polyhaven.com/assets?type=models",
        expected_host="api.polyhaven.com",
        headers=(("User-Agent", "UEFactory/test"),),
        timeout_sec=30,
    )
    metadata = HttpResponseMetadata(
        request=request,
        status=200,
        final_url=request.url,
        headers=(("Content-Type", "application/json"),),
    )
    response = _FakeResponse(metadata, b"{}")
    transport = _FakeTransport(response)

    assert request.header("user-agent") == "UEFactory/test"
    assert metadata.header("content-type") == "application/json"
    assert isinstance(response, HttpResponse)
    assert isinstance(transport, HttpTransport)
    with transport.open(request) as opened:
        assert opened.read() == b"{}"
    assert response.closed is True
    assert transport.requests == [request]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"url": "http://api.polyhaven.com/assets"}, "must use HTTPS"),
        ({"url": "https://example.test/assets"}, "prevalidated host"),
        ({"url": "https://user@api.polyhaven.com/assets"}, "prevalidated host"),
        ({"url": "https://api.polyhaven.com:443/assets"}, "prevalidated host"),
        ({"url": "https://api.polyhaven.com/assets#fragment"}, "prevalidated host"),
        ({"expected_host": "API.POLYHAVEN.COM"}, "lowercase normalized host"),
        ({"headers": (("Bad Header", "value"),)}, "invalid header name"),
        ({"headers": (("X-Test", "one"), ("x-test", "two"))}, "duplicate header"),
        ({"headers": (("X-Test", "line\nbreak"),)}, "invalid header value"),
        ({"timeout_sec": float("nan")}, "finite number"),
        ({"allow_query": 1}, "must be a boolean"),
    ],
)
def test_http_request_rejects_unapproved_or_ambiguous_inputs(
    changes: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "url": "https://api.polyhaven.com/assets?type=models",
        "expected_host": "api.polyhaven.com",
    }
    values.update(changes)

    with pytest.raises(RuntimeValidationError, match=message):
        HttpRequest(**values)  # type: ignore[arg-type]


def test_http_response_rejects_a_cross_host_final_url() -> None:
    request = HttpRequest(
        url="https://dl.polyhaven.org/model.gltf",
        expected_host="dl.polyhaven.org",
        allow_query=False,
    )

    with pytest.raises(RuntimeValidationError, match="prevalidated host"):
        HttpResponseMetadata(
            request=request,
            status=200,
            final_url="https://internal.example/model.gltf",
        )


def test_http_response_requires_a_prevalidated_request() -> None:
    with pytest.raises(RuntimeValidationError, match="must be prevalidated"):
        HttpResponseMetadata(
            request=object(),  # type: ignore[arg-type]
            status=200,
            final_url="https://api.polyhaven.com/assets",
        )


def test_token_bucket_paces_requests_with_monotonic_time() -> None:
    clock = _FakeClock()
    limiter = MonotonicTokenBucket(clock=clock, rate_per_sec=2.0, burst=2)

    assert limiter.acquire() == 0.0
    assert limiter.acquire() == 0.0
    assert limiter.acquire() == pytest.approx(0.5)
    assert clock.sleeps == [pytest.approx(0.5)]


def test_token_bucket_fails_closed_when_monotonic_time_moves_backwards() -> None:
    clock = _FakeClock(monotonic_value=100.0)
    limiter = MonotonicTokenBucket(clock=clock, rate_per_sec=1.0)
    assert limiter.acquire() == 0.0
    clock.monotonic_value = 90.0

    assert limiter.acquire() == pytest.approx(1.0)
    assert clock.sleeps == [pytest.approx(1.0)]


def test_token_bucket_rejects_a_clock_that_does_not_advance_during_sleep() -> None:
    clock = _FakeClock(advance_on_sleep=False)
    limiter = MonotonicTokenBucket(clock=clock, rate_per_sec=1.0)
    limiter.acquire()

    with pytest.raises(RuntimeValidationError, match="did not advance"):
        limiter.acquire()


def test_token_bucket_reports_actual_monotonic_wait_when_sleep_overshoots() -> None:
    clock = _FakeClock(sleep_scale=2.0)
    limiter = MonotonicTokenBucket(clock=clock, rate_per_sec=1.0)
    limiter.acquire()

    assert limiter.acquire() == pytest.approx(2.0)


@pytest.mark.parametrize(
    "value",
    [0.0, -1.0, 1e-50, float("inf"), float("nan"), True],
)
def test_token_bucket_rejects_invalid_rates(value: object) -> None:
    with pytest.raises(RuntimeValidationError):
        MonotonicTokenBucket(clock=_FakeClock(), rate_per_sec=value)  # type: ignore[arg-type]


def test_retry_after_parses_delta_and_http_date_with_a_strict_cap() -> None:
    delta = parse_retry_after(" 120 ", now=NOW, max_delay_sec=60)
    target = NOW + timedelta(seconds=45)
    http_date = parse_retry_after(
        format_datetime(target, usegmt=True),
        now=NOW,
        max_delay_sec=60,
    )

    assert delta is not None
    assert delta.kind is RetryAfterKind.DELTA_SECONDS
    assert delta.delay_sec == 60
    assert delta.deadline_utc == NOW + timedelta(seconds=60)
    assert http_date is not None
    assert http_date.kind is RetryAfterKind.HTTP_DATE
    assert http_date.delay_sec == 45
    assert http_date.deadline_utc == target
    assert parse_retry_after(None, now=NOW, max_delay_sec=60) is None


def test_retry_after_past_date_is_immediate_and_invalid_values_fail() -> None:
    past = parse_retry_after(
        format_datetime(NOW - timedelta(days=1), usegmt=True),
        now=NOW,
        max_delay_sec=60,
    )
    assert past is not None and past.delay_sec == 0 and past.deadline_utc == NOW

    for value in ("", "1.5", "+12", "not-a-date", "9" * 21):
        with pytest.raises(RuntimeValidationError):
            parse_retry_after(value, now=NOW, max_delay_sec=60)
    with pytest.raises(RuntimeValidationError, match="timezone-aware"):
        parse_retry_after("12", now=datetime(2026, 7, 11), max_delay_sec=60)


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (408, FailureKind.HTTP_TRANSIENT),
        (429, FailureKind.HTTP_RATE_LIMIT),
        (500, FailureKind.HTTP_TRANSIENT),
        (503, FailureKind.HTTP_TRANSIENT),
        (400, FailureKind.HTTP_PERMANENT),
        (404, FailureKind.HTTP_PERMANENT),
        (501, FailureKind.HTTP_PERMANENT),
    ],
)
def test_http_status_taxonomy(status: int, kind: FailureKind) -> None:
    failure = AcquisitionFailure.from_http(
        phase="listing",
        status=status,
        message=f"HTTP {status}",
    )

    assert classify_http_status(status) is kind
    assert failure.kind is kind


def test_failure_taxonomy_distinguishes_retry_permanent_and_deferred() -> None:
    assert (
        AcquisitionFailure(
            kind=FailureKind.TRANSPORT,
            phase="download",
            message="connection reset",
        ).category
        is FailureCategory.TRANSIENT
    )
    assert (
        AcquisitionFailure(
            kind=FailureKind.INTEGRITY,
            phase="verify",
            message="checksum mismatch",
        ).category
        is FailureCategory.INTEGRITY
    )
    assert (
        AcquisitionFailure(
            kind=FailureKind.SCHEMA,
            phase="listing",
            message="unsupported payload",
        ).category
        is FailureCategory.PERMANENT
    )
    assert (
        AcquisitionFailure(
            kind=FailureKind.DISK,
            phase="download",
            message="free space guard",
        ).category
        is FailureCategory.DEFERRED
    )
    with pytest.raises(RuntimeValidationError, match="failure kind"):
        AcquisitionFailure(
            kind="transport",  # type: ignore[arg-type]
            phase="download",
            message="connection reset",
        )


def test_retry_policy_caps_backoff_honors_retry_after_and_exhausts() -> None:
    policy = RetryPolicy(
        max_attempts=4,
        integrity_max_attempts=2,
        base_delay_sec=5,
        max_delay_sec=12,
        max_retry_after_sec=30,
    )
    failure = AcquisitionFailure(
        kind=FailureKind.TRANSPORT,
        phase="download",
        message="timeout",
    )
    retry_after = parse_retry_after("20", now=NOW, max_delay_sec=30)
    assert retry_after is not None

    first = compute_retry_decision(
        policy=policy,
        failure=failure,
        consecutive_failures=1,
        now=NOW,
    )
    third = compute_retry_decision(
        policy=policy,
        failure=failure,
        consecutive_failures=3,
        now=NOW,
    )
    server_delayed = compute_retry_decision(
        policy=policy,
        failure=failure,
        consecutive_failures=1,
        now=NOW,
        retry_after=retry_after,
    )
    exhausted = compute_retry_decision(
        policy=policy,
        failure=failure,
        consecutive_failures=4,
        now=NOW,
    )

    assert first.delay_sec == 5 and first.next_attempt_at == NOW + timedelta(seconds=5)
    assert third.delay_sec == 12
    assert server_delayed.delay_sec == 20
    assert exhausted.will_retry is False and exhausted.exhausted is True


def test_retry_after_deadline_remains_absolute_if_decision_is_delayed() -> None:
    failure = AcquisitionFailure(
        kind=FailureKind.TRANSPORT,
        phase="download",
        message="timeout",
    )
    retry_after = parse_retry_after("20", now=NOW, max_delay_sec=30)
    assert retry_after is not None

    decision = compute_retry_decision(
        policy=RetryPolicy(base_delay_sec=1, max_delay_sec=10, max_retry_after_sec=30),
        failure=failure,
        consecutive_failures=1,
        now=NOW + timedelta(seconds=12),
        retry_after=retry_after,
    )

    assert decision.delay_sec == 8
    assert decision.next_attempt_at == NOW + timedelta(seconds=20)


def test_retry_policy_uses_smaller_integrity_budget_and_never_retries_permanent() -> None:
    policy = RetryPolicy(max_attempts=5, integrity_max_attempts=2)
    integrity = AcquisitionFailure(
        kind=FailureKind.INTEGRITY,
        phase="verify",
        message="md5 mismatch",
    )
    permanent = AcquisitionFailure(
        kind=FailureKind.PATH_SECURITY,
        phase="metadata",
        message="unsafe path",
    )

    assert compute_retry_decision(
        policy=policy,
        failure=integrity,
        consecutive_failures=2,
        now=NOW,
    ).exhausted
    decision = compute_retry_decision(
        policy=policy,
        failure=permanent,
        consecutive_failures=1,
        now=NOW,
    )
    assert decision.category is FailureCategory.PERMANENT
    assert decision.will_retry is False and decision.exhausted is False


@pytest.mark.parametrize(
    "changes",
    [
        {"max_attempts": 0},
        {"base_delay_sec": 0},
        {"base_delay_sec": 1e-50},
        {"base_delay_sec": float("nan")},
        {"base_delay_sec": 10, "max_delay_sec": 5},
        {"max_retry_after_sec": float("inf")},
    ],
)
def test_retry_policy_rejects_invalid_or_nonfinite_values(changes: dict[str, object]) -> None:
    with pytest.raises(RuntimeValidationError):
        RetryPolicy(**changes)  # type: ignore[arg-type]


def test_retry_decision_validates_failure_count_even_for_permanent_errors() -> None:
    failure = AcquisitionFailure(
        kind=FailureKind.SCHEMA,
        phase="listing",
        message="invalid payload",
    )

    with pytest.raises(RuntimeValidationError, match="consecutive_failures"):
        compute_retry_decision(
            policy=RetryPolicy(),
            failure=failure,
            consecutive_failures=0,
            now=NOW,
        )


def test_daily_quota_reserves_exact_boundaries_and_reports_rejections() -> None:
    limits = DailyQuotaLimits(max_new_items=3, max_download_bytes=1_000)
    usage = DailyQuotaUsage(
        utc_day=date(2026, 7, 11),
        new_items_reserved=1,
        download_bytes_reserved=400,
    )
    request = DailyQuotaRequest(new_items=2, download_bytes=600)

    reservation = reserve_daily_quota(limits=limits, usage=usage, request=request)

    assert reservation.before == usage
    assert reservation.after.new_items_reserved == 3
    assert reservation.after.download_bytes_reserved == 1_000
    with pytest.raises(QuotaExceeded) as item_error:
        reserve_daily_quota(
            limits=limits,
            usage=reservation.after,
            request=DailyQuotaRequest(new_items=1),
        )
    assert item_error.value.resource == "new_items"
    with pytest.raises(QuotaExceeded) as byte_error:
        reserve_daily_quota(
            limits=limits,
            usage=reservation.after,
            request=DailyQuotaRequest(download_bytes=1),
        )
    assert byte_error.value.resource == "download_bytes"


def test_daily_quota_rolls_only_forward_and_retries_can_reserve_zero_items() -> None:
    usage = DailyQuotaUsage(
        utc_day=date(2026, 7, 11),
        new_items_reserved=5,
        download_bytes_reserved=100,
    )

    same = usage.roll_forward(date(2026, 7, 11))
    next_day = usage.roll_forward(date(2026, 7, 12))
    retry = reserve_daily_quota(
        limits=DailyQuotaLimits(max_new_items=0, max_download_bytes=100),
        usage=next_day,
        request=DailyQuotaRequest(new_items=0, download_bytes=100),
    )

    assert same is usage
    assert next_day == DailyQuotaUsage(utc_day=date(2026, 7, 12))
    assert retry.after.new_items_reserved == 0
    with pytest.raises(RuntimeValidationError, match="may not move backwards"):
        usage.roll_forward(date(2026, 7, 10))


def test_daily_quota_guards_signed_counter_overflow() -> None:
    usage = DailyQuotaUsage(
        utc_day=date(2026, 7, 11),
        download_bytes_reserved=MAX_COUNTER,
    )

    with pytest.raises(RuntimeValidationError, match="signed 64-bit"):
        reserve_daily_quota(
            limits=DailyQuotaLimits(),
            usage=usage,
            request=DailyQuotaRequest(download_bytes=1),
        )
    with pytest.raises(RuntimeValidationError):
        DailyQuotaRequest(download_bytes=True)  # type: ignore[arg-type]
    with pytest.raises(RuntimeValidationError):
        DailyQuotaLimits(max_new_items=MAX_COUNTER + 1)


def test_disk_reservation_enforces_storage_and_minimum_free_guards() -> None:
    snapshot = DiskSnapshot(storage_bytes=700, free_bytes=500)
    reservation = reserve_disk_growth(
        limits=DiskQuotaLimits(max_storage_bytes=1_000, min_free_bytes=200),
        snapshot=snapshot,
        growth_bytes=300,
    )

    assert reservation.after == DiskSnapshot(storage_bytes=1_000, free_bytes=200)
    with pytest.raises(QuotaExceeded) as storage_error:
        reserve_disk_growth(
            limits=DiskQuotaLimits(max_storage_bytes=999),
            snapshot=snapshot,
            growth_bytes=300,
        )
    assert storage_error.value.resource == "storage_bytes"
    with pytest.raises(QuotaExceeded) as free_error:
        reserve_disk_growth(
            limits=DiskQuotaLimits(min_free_bytes=201),
            snapshot=snapshot,
            growth_bytes=300,
        )
    assert free_error.value.resource == "free_bytes"


def test_disk_reservation_rejects_counter_overflow_and_invalid_values() -> None:
    with pytest.raises(RuntimeValidationError, match="signed 64-bit"):
        reserve_disk_growth(
            limits=DiskQuotaLimits(),
            snapshot=DiskSnapshot(storage_bytes=MAX_COUNTER, free_bytes=1),
            growth_bytes=1,
        )
    with pytest.raises(RuntimeValidationError):
        DiskQuotaLimits(min_free_bytes=-1)
    with pytest.raises(RuntimeValidationError):
        DiskSnapshot(storage_bytes=0, free_bytes=float("inf"))  # type: ignore[arg-type]
    with pytest.raises(RuntimeValidationError):
        DiskQuotaLimits(max_storage_bytes=MAX_COUNTER + 1)
