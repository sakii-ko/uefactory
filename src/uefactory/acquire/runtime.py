from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

_HEADER_NAME_PATTERN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_MAX_COUNTER = (1 << 63) - 1
_MAX_DELAY_SEC = 366 * 24 * 60 * 60
_MIN_RATE_PER_SEC = 1.0 / _MAX_DELAY_SEC
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class RuntimeValidationError(ValueError):
    """A reusable acquisition runtime primitive received an invalid value."""


class Clock(Protocol):
    """Wall and monotonic time seam used by acquisition orchestration."""

    def utc_now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Production clock backed by timezone-aware UTC and monotonic time."""

    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(_finite_float(seconds, "sleep seconds", minimum=0.0))


def _normalized_host(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RuntimeValidationError(f"{field} must be a non-empty normalized host")
    normalized = value.casefold()
    if normalized != value or len(value) > 253 or ".." in value:
        raise RuntimeValidationError(f"{field} must be a lowercase normalized host")
    labels = value.split(".")
    if any(
        not label
        or len(label) > 63
        or label[0] == "-"
        or label[-1] == "-"
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in label)
        for label in labels
    ):
        raise RuntimeValidationError(f"{field} must be a lowercase normalized host")
    return value


def _validated_https_url(
    value: str,
    *,
    expected_host: str,
    allow_query: bool,
    field: str,
) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 8_192:
        raise RuntimeValidationError(f"{field} must be a normalized HTTPS URL")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise RuntimeValidationError(f"{field} contains control characters")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeValidationError(f"{field} has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.path.startswith("/")
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise RuntimeValidationError(
            f"{field} must use HTTPS on the prevalidated host {expected_host!r}"
        )
    return value


def _normalized_headers(
    value: tuple[tuple[str, str], ...],
    *,
    field: str,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, tuple):
        raise RuntimeValidationError(f"{field} must be an immutable tuple of pairs")
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, tuple) or len(item) != 2:
            raise RuntimeValidationError(f"{field}[{index}] must be a name/value pair")
        name, header_value = item
        if not isinstance(name, str) or _HEADER_NAME_PATTERN.fullmatch(name) is None:
            raise RuntimeValidationError(f"{field}[{index}] has an invalid header name")
        normalized_name = name.casefold()
        if normalized_name in seen:
            raise RuntimeValidationError(f"{field} contains duplicate header {name!r}")
        seen.add(normalized_name)
        if (
            not isinstance(header_value, str)
            or header_value != header_value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in header_value)
        ):
            raise RuntimeValidationError(f"{field}[{index}] has an invalid header value")
        result.append((name, header_value))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """A request whose initial HTTPS destination is validated before transport use."""

    url: str
    expected_host: str
    headers: tuple[tuple[str, str], ...] = ()
    timeout_sec: float = 60.0
    method: str = "GET"
    allow_query: bool = True

    def __post_init__(self) -> None:
        host = _normalized_host(self.expected_host, "expected_host")
        if type(self.allow_query) is not bool:
            raise RuntimeValidationError("allow_query must be a boolean")
        object.__setattr__(
            self,
            "url",
            _validated_https_url(
                self.url,
                expected_host=host,
                allow_query=self.allow_query,
                field="request URL",
            ),
        )
        if self.method not in {"GET", "HEAD"}:
            raise RuntimeValidationError("HTTP method must be GET or HEAD")
        object.__setattr__(self, "headers", _normalized_headers(self.headers, field="headers"))
        object.__setattr__(
            self,
            "timeout_sec",
            _finite_float(
                self.timeout_sec,
                "timeout_sec",
                minimum=0.001,
                maximum=86_400.0,
            ),
        )

    def header(self, name: str) -> str | None:
        checked = name.casefold()
        return next((value for key, value in self.headers if key.casefold() == checked), None)


@dataclass(frozen=True, slots=True)
class HttpResponseMetadata:
    """Validated response head returned only after safe redirect handling."""

    request: HttpRequest
    status: int
    final_url: str
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.request, HttpRequest):
            raise RuntimeValidationError("HTTP response request must be prevalidated")
        if (
            isinstance(self.status, bool)
            or not isinstance(self.status, int)
            or not 100 <= self.status <= 599
        ):
            raise RuntimeValidationError("HTTP response status must be between 100 and 599")
        object.__setattr__(
            self,
            "final_url",
            _validated_https_url(
                self.final_url,
                expected_host=self.request.expected_host,
                allow_query=self.request.allow_query,
                field="response final URL",
            ),
        )
        object.__setattr__(
            self,
            "headers",
            _normalized_headers(self.headers, field="response headers"),
        )

    def header(self, name: str) -> str | None:
        checked = name.casefold()
        return next((value for key, value in self.headers if key.casefold() == checked), None)


@runtime_checkable
class HttpResponse(Protocol):
    metadata: HttpResponseMetadata

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


@runtime_checkable
class HttpTransport(Protocol):
    """Transport contract; redirects must be approved before each hop is issued."""

    def open(self, request: HttpRequest) -> AbstractContextManager[HttpResponse]: ...


class MonotonicTokenBucket:
    """Sequential token bucket that fails closed if monotonic time moves backwards."""

    def __init__(self, *, clock: Clock, rate_per_sec: float, burst: int = 1) -> None:
        self.clock = clock
        self.rate_per_sec = _finite_float(
            rate_per_sec,
            "rate_per_sec",
            minimum=_MIN_RATE_PER_SEC,
            maximum=1_000_000.0,
        )
        self.burst = _bounded_int(burst, "burst", minimum=1, maximum=1_000_000)
        self._tokens = float(self.burst)
        self._last = _finite_float(clock.monotonic(), "clock.monotonic()")

    def acquire(self, cost: float = 1.0) -> float:
        checked_cost = _finite_float(
            cost,
            "token cost",
            minimum=math.nextafter(0.0, 1.0),
            maximum=float(self.burst),
        )
        waited = 0.0
        while True:
            now = _finite_float(self.clock.monotonic(), "clock.monotonic()")
            if now < self._last:
                # A broken/fork-reset clock must not create a free burst.
                self._tokens = 0.0
                self._last = now
            elapsed = now - self._last
            missing_capacity = float(self.burst) - self._tokens
            if missing_capacity > 0.0:
                time_to_full = missing_capacity / self.rate_per_sec
                self._tokens = (
                    float(self.burst)
                    if elapsed >= time_to_full
                    else self._tokens + elapsed * self.rate_per_sec
                )
            self._last = now
            if self._tokens >= checked_cost:
                self._tokens -= checked_cost
                return waited
            delay = (checked_cost - self._tokens) / self.rate_per_sec
            if not math.isfinite(delay) or delay <= 0.0:
                raise RuntimeValidationError("token bucket computed an invalid delay")
            self.clock.sleep(delay)
            after_sleep = _finite_float(self.clock.monotonic(), "clock.monotonic()")
            if after_sleep <= now:
                raise RuntimeValidationError("Clock.sleep() did not advance monotonic time")
            waited += after_sleep - now


class RetryAfterKind(StrEnum):
    DELTA_SECONDS = "delta_seconds"
    HTTP_DATE = "http_date"


@dataclass(frozen=True, slots=True)
class RetryAfter:
    kind: RetryAfterKind
    delay_sec: float
    deadline_utc: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RetryAfterKind):
            raise RuntimeValidationError("Retry-After kind is invalid")
        object.__setattr__(
            self,
            "delay_sec",
            _finite_float(
                self.delay_sec,
                "Retry-After delay",
                minimum=0.0,
                maximum=float(_MAX_DELAY_SEC),
            ),
        )
        object.__setattr__(
            self,
            "deadline_utc",
            _utc_datetime(self.deadline_utc, "Retry-After deadline"),
        )


def parse_retry_after(
    value: str | None,
    *,
    now: datetime,
    max_delay_sec: float,
) -> RetryAfter | None:
    """Parse one strict Retry-After delta or HTTP date and clamp its delay."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeValidationError("Retry-After must be text or null")
    raw = value.strip()
    if (
        not raw
        or len(raw) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise RuntimeValidationError("Retry-After is invalid")
    checked_now = _utc_datetime(now, "now")
    cap = _finite_float(
        max_delay_sec,
        "max_delay_sec",
        minimum=0.0,
        maximum=float(_MAX_DELAY_SEC),
    )
    if raw.isascii() and raw.isdigit():
        if len(raw) > 20:
            raise RuntimeValidationError("Retry-After delta is too large")
        delay = min(float(int(raw)), cap)
        return RetryAfter(
            kind=RetryAfterKind.DELTA_SECONDS,
            delay_sec=delay,
            deadline_utc=_add_seconds(checked_now, delay, "Retry-After deadline"),
        )
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeValidationError(
            "Retry-After is neither delta-seconds nor an HTTP date"
        ) from exc
    if parsed is None or parsed.tzinfo is None:
        raise RuntimeValidationError("Retry-After HTTP date must include a timezone")
    parsed_utc = parsed.astimezone(UTC)
    delay = min(max(0.0, (parsed_utc - checked_now).total_seconds()), cap)
    return RetryAfter(
        kind=RetryAfterKind.HTTP_DATE,
        delay_sec=delay,
        deadline_utc=_add_seconds(checked_now, delay, "Retry-After deadline"),
    )


class FailureKind(StrEnum):
    TRANSPORT = "transport"
    HTTP_RATE_LIMIT = "http_rate_limit"
    HTTP_TRANSIENT = "http_transient"
    HTTP_PERMANENT = "http_permanent"
    SHORT_READ = "short_read"
    INTEGRITY = "integrity"
    SCHEMA = "schema"
    PATH_SECURITY = "path_security"
    LICENSE = "license"
    QUOTA = "quota"
    DISK = "disk"
    INTERRUPTED = "interrupted"
    DOWNSTREAM = "downstream"
    QUALITY = "quality"


class FailureCategory(StrEnum):
    TRANSIENT = "transient"
    INTEGRITY = "integrity"
    PERMANENT = "permanent"
    DEFERRED = "deferred"


_FAILURE_CATEGORIES: Mapping[FailureKind, FailureCategory] = {
    FailureKind.TRANSPORT: FailureCategory.TRANSIENT,
    FailureKind.HTTP_RATE_LIMIT: FailureCategory.TRANSIENT,
    FailureKind.HTTP_TRANSIENT: FailureCategory.TRANSIENT,
    FailureKind.SHORT_READ: FailureCategory.TRANSIENT,
    FailureKind.INTEGRITY: FailureCategory.INTEGRITY,
    FailureKind.HTTP_PERMANENT: FailureCategory.PERMANENT,
    FailureKind.SCHEMA: FailureCategory.PERMANENT,
    FailureKind.PATH_SECURITY: FailureCategory.PERMANENT,
    FailureKind.LICENSE: FailureCategory.PERMANENT,
    FailureKind.QUOTA: FailureCategory.DEFERRED,
    FailureKind.DISK: FailureCategory.DEFERRED,
    FailureKind.INTERRUPTED: FailureCategory.TRANSIENT,
    FailureKind.DOWNSTREAM: FailureCategory.TRANSIENT,
    FailureKind.QUALITY: FailureCategory.PERMANENT,
}


def classify_http_status(status: int) -> FailureKind:
    checked = _bounded_int(status, "HTTP status", minimum=400, maximum=599)
    if checked == 429:
        return FailureKind.HTTP_RATE_LIMIT
    if checked in _TRANSIENT_HTTP_STATUSES:
        return FailureKind.HTTP_TRANSIENT
    return FailureKind.HTTP_PERMANENT


@dataclass(frozen=True, slots=True)
class AcquisitionFailure:
    kind: FailureKind
    phase: str
    message: str
    http_status: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FailureKind):
            raise RuntimeValidationError("failure kind is invalid")
        if not isinstance(self.phase, str) or not self.phase or self.phase != self.phase.strip():
            raise RuntimeValidationError("failure phase must be non-empty trimmed text")
        if len(self.phase) > 64 or any(ord(character) < 32 for character in self.phase):
            raise RuntimeValidationError("failure phase is invalid")
        if (
            not isinstance(self.message, str)
            or not self.message
            or self.message != self.message.strip()
            or len(self.message) > 2_048
            or any(ord(character) < 32 for character in self.message)
        ):
            raise RuntimeValidationError("failure message is invalid")
        is_http = self.kind in {
            FailureKind.HTTP_RATE_LIMIT,
            FailureKind.HTTP_TRANSIENT,
            FailureKind.HTTP_PERMANENT,
        }
        if is_http != (self.http_status is not None):
            raise RuntimeValidationError("HTTP failures require exactly one HTTP status")
        if self.http_status is not None:
            expected = classify_http_status(self.http_status)
            if expected is not self.kind:
                raise RuntimeValidationError("failure kind does not match its HTTP status")

    @property
    def category(self) -> FailureCategory:
        return _FAILURE_CATEGORIES[self.kind]

    @classmethod
    def from_http(cls, *, phase: str, status: int, message: str) -> AcquisitionFailure:
        return cls(
            kind=classify_http_status(status),
            phase=phase,
            message=message,
            http_status=status,
        )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    integrity_max_attempts: int = 2
    base_delay_sec: float = 5.0
    max_delay_sec: float = 900.0
    max_retry_after_sec: float = 3_600.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_attempts",
            _bounded_int(self.max_attempts, "max_attempts", minimum=1, maximum=1_000_000),
        )
        object.__setattr__(
            self,
            "integrity_max_attempts",
            _bounded_int(
                self.integrity_max_attempts,
                "integrity_max_attempts",
                minimum=1,
                maximum=1_000_000,
            ),
        )
        base = _finite_float(
            self.base_delay_sec,
            "base_delay_sec",
            minimum=0.001,
            maximum=float(_MAX_DELAY_SEC),
        )
        cap = _finite_float(
            self.max_delay_sec,
            "max_delay_sec",
            minimum=base,
            maximum=float(_MAX_DELAY_SEC),
        )
        retry_after_cap = _finite_float(
            self.max_retry_after_sec,
            "max_retry_after_sec",
            minimum=0.0,
            maximum=float(_MAX_DELAY_SEC),
        )
        object.__setattr__(self, "base_delay_sec", base)
        object.__setattr__(self, "max_delay_sec", cap)
        object.__setattr__(self, "max_retry_after_sec", retry_after_cap)

    def backoff_delay(self, consecutive_failures: int) -> float:
        failures = _bounded_int(
            consecutive_failures,
            "consecutive_failures",
            minimum=1,
            maximum=_MAX_COUNTER,
        )
        if self.base_delay_sec >= self.max_delay_sec:
            return self.max_delay_sec
        doublings_to_cap = math.ceil(math.log2(self.max_delay_sec / self.base_delay_sec))
        if failures - 1 >= doublings_to_cap:
            return self.max_delay_sec
        return min(self.max_delay_sec, math.ldexp(self.base_delay_sec, failures - 1))


@dataclass(frozen=True, slots=True)
class RetryDecision:
    category: FailureCategory
    will_retry: bool
    exhausted: bool
    delay_sec: float | None
    next_attempt_at: datetime | None


def compute_retry_decision(
    *,
    policy: RetryPolicy,
    failure: AcquisitionFailure,
    consecutive_failures: int,
    now: datetime,
    retry_after: RetryAfter | None = None,
) -> RetryDecision:
    checked_now = _utc_datetime(now, "now")
    failures = _bounded_int(
        consecutive_failures,
        "consecutive_failures",
        minimum=1,
        maximum=_MAX_COUNTER,
    )
    category = failure.category
    if category in {FailureCategory.PERMANENT, FailureCategory.DEFERRED}:
        return RetryDecision(
            category=category,
            will_retry=False,
            exhausted=False,
            delay_sec=None,
            next_attempt_at=None,
        )
    attempt_limit = (
        policy.integrity_max_attempts
        if category is FailureCategory.INTEGRITY
        else policy.max_attempts
    )
    if failures >= attempt_limit:
        return RetryDecision(
            category=category,
            will_retry=False,
            exhausted=True,
            delay_sec=None,
            next_attempt_at=None,
        )
    delay = policy.backoff_delay(failures)
    if retry_after is not None:
        remaining_retry_after = max(
            0.0,
            (retry_after.deadline_utc - checked_now).total_seconds(),
        )
        delay = max(delay, min(remaining_retry_after, policy.max_retry_after_sec))
    deadline = _add_seconds(checked_now, delay, "retry deadline")
    return RetryDecision(
        category=category,
        will_retry=True,
        exhausted=False,
        delay_sec=delay,
        next_attempt_at=deadline,
    )


@dataclass(frozen=True, slots=True)
class DailyQuotaLimits:
    max_new_items: int | None = None
    max_download_bytes: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_new_items",
            _optional_counter(self.max_new_items, "max_new_items"),
        )
        object.__setattr__(
            self,
            "max_download_bytes",
            _optional_counter(self.max_download_bytes, "max_download_bytes"),
        )


@dataclass(frozen=True, slots=True)
class DailyQuotaUsage:
    utc_day: date
    new_items_reserved: int = 0
    download_bytes_reserved: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.utc_day, date) or isinstance(self.utc_day, datetime):
            raise RuntimeValidationError("utc_day must be a date")
        object.__setattr__(
            self,
            "new_items_reserved",
            _counter(self.new_items_reserved, "new_items_reserved"),
        )
        object.__setattr__(
            self,
            "download_bytes_reserved",
            _counter(self.download_bytes_reserved, "download_bytes_reserved"),
        )

    def roll_forward(self, utc_day: date) -> DailyQuotaUsage:
        if not isinstance(utc_day, date) or isinstance(utc_day, datetime):
            raise RuntimeValidationError("utc_day must be a date")
        if utc_day < self.utc_day:
            raise RuntimeValidationError("daily quota window may not move backwards")
        return self if utc_day == self.utc_day else DailyQuotaUsage(utc_day=utc_day)


@dataclass(frozen=True, slots=True)
class DailyQuotaRequest:
    new_items: int = 0
    download_bytes: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "new_items", _counter(self.new_items, "new_items"))
        object.__setattr__(
            self,
            "download_bytes",
            _counter(self.download_bytes, "download_bytes"),
        )


class QuotaExceeded(RuntimeError):
    def __init__(
        self,
        *,
        resource: str,
        limit: int,
        used: int,
        requested: int,
    ) -> None:
        self.resource = resource
        self.limit = limit
        self.used = used
        self.requested = requested
        super().__init__(
            f"{resource} quota exceeded: limit={limit} used={used} requested={requested}"
        )


@dataclass(frozen=True, slots=True)
class DailyQuotaReservation:
    before: DailyQuotaUsage
    request: DailyQuotaRequest
    after: DailyQuotaUsage


def reserve_daily_quota(
    *,
    limits: DailyQuotaLimits,
    usage: DailyQuotaUsage,
    request: DailyQuotaRequest,
) -> DailyQuotaReservation:
    new_items = _checked_add(usage.new_items_reserved, request.new_items, "new items")
    download_bytes = _checked_add(
        usage.download_bytes_reserved,
        request.download_bytes,
        "download bytes",
    )
    if limits.max_new_items is not None and new_items > limits.max_new_items:
        raise QuotaExceeded(
            resource="new_items",
            limit=limits.max_new_items,
            used=usage.new_items_reserved,
            requested=request.new_items,
        )
    if limits.max_download_bytes is not None and download_bytes > limits.max_download_bytes:
        raise QuotaExceeded(
            resource="download_bytes",
            limit=limits.max_download_bytes,
            used=usage.download_bytes_reserved,
            requested=request.download_bytes,
        )
    after = DailyQuotaUsage(
        utc_day=usage.utc_day,
        new_items_reserved=new_items,
        download_bytes_reserved=download_bytes,
    )
    return DailyQuotaReservation(before=usage, request=request, after=after)


@dataclass(frozen=True, slots=True)
class DiskQuotaLimits:
    max_storage_bytes: int | None = None
    min_free_bytes: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_storage_bytes",
            _optional_counter(self.max_storage_bytes, "max_storage_bytes"),
        )
        object.__setattr__(
            self,
            "min_free_bytes",
            _counter(self.min_free_bytes, "min_free_bytes"),
        )


@dataclass(frozen=True, slots=True)
class DiskSnapshot:
    storage_bytes: int
    free_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "storage_bytes", _counter(self.storage_bytes, "storage_bytes"))
        object.__setattr__(self, "free_bytes", _counter(self.free_bytes, "free_bytes"))


@dataclass(frozen=True, slots=True)
class DiskReservation:
    before: DiskSnapshot
    growth_bytes: int
    after: DiskSnapshot


def reserve_disk_growth(
    *,
    limits: DiskQuotaLimits,
    snapshot: DiskSnapshot,
    growth_bytes: int,
) -> DiskReservation:
    growth = _counter(growth_bytes, "growth_bytes")
    storage_after = _checked_add(snapshot.storage_bytes, growth, "storage bytes")
    if limits.max_storage_bytes is not None and storage_after > limits.max_storage_bytes:
        raise QuotaExceeded(
            resource="storage_bytes",
            limit=limits.max_storage_bytes,
            used=snapshot.storage_bytes,
            requested=growth,
        )
    available_growth = max(0, snapshot.free_bytes - limits.min_free_bytes)
    if growth > available_growth:
        raise QuotaExceeded(
            resource="free_bytes",
            limit=available_growth,
            used=0,
            requested=growth,
        )
    after = DiskSnapshot(
        storage_bytes=storage_after,
        free_bytes=snapshot.free_bytes - growth,
    )
    return DiskReservation(before=snapshot, growth_bytes=growth, after=after)


def _finite_float(
    value: float,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuntimeValidationError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise RuntimeValidationError(f"{field} must be a finite number")
    if minimum is not None and result < minimum:
        raise RuntimeValidationError(f"{field} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise RuntimeValidationError(f"{field} must be at most {maximum}")
    return result


def _bounded_int(value: int, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RuntimeValidationError(f"{field} must be an integer in [{minimum}, {maximum}]")
    return value


def _counter(value: int, field: str) -> int:
    return _bounded_int(value, field, minimum=0, maximum=_MAX_COUNTER)


def _optional_counter(value: int | None, field: str) -> int | None:
    return None if value is None else _counter(value, field)


def _checked_add(left: int, right: int, field: str) -> int:
    result = left + right
    if result > _MAX_COUNTER:
        raise RuntimeValidationError(f"{field} exceeds the signed 64-bit counter limit")
    return result


def _utc_datetime(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeValidationError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _add_seconds(value: datetime, seconds: float, field: str) -> datetime:
    try:
        return value + timedelta(seconds=seconds)
    except (OverflowError, ValueError) as exc:
        raise RuntimeValidationError(f"{field} overflows datetime") from exc


__all__ = [
    "AcquisitionFailure",
    "Clock",
    "DailyQuotaLimits",
    "DailyQuotaRequest",
    "DailyQuotaReservation",
    "DailyQuotaUsage",
    "DiskQuotaLimits",
    "DiskReservation",
    "DiskSnapshot",
    "FailureCategory",
    "FailureKind",
    "HttpRequest",
    "HttpResponse",
    "HttpResponseMetadata",
    "HttpTransport",
    "MonotonicTokenBucket",
    "QuotaExceeded",
    "RetryAfter",
    "RetryAfterKind",
    "RetryDecision",
    "RetryPolicy",
    "RuntimeValidationError",
    "SystemClock",
    "classify_http_status",
    "compute_retry_decision",
    "parse_retry_after",
    "reserve_daily_quota",
    "reserve_disk_growth",
]
