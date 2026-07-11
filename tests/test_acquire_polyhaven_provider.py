from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from uefactory.acquire import polyhaven
from uefactory.acquire.polyhaven_provider import (
    PolyHavenAcquireError,
    PolyHavenFileSpec,
    PolyHavenProviderSession,
    PolyHavenRuntimeConfig,
    ProviderOperationError,
    polyhaven_source_lock,
)
from uefactory.acquire.runtime import FailureKind


@dataclass
class _FakeClock:
    wall: datetime = datetime(2026, 7, 11, tzinfo=UTC)
    monotonic_value: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def utc_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.monotonic_value += seconds
        self.wall += timedelta(seconds=seconds)


class _Response:
    def __init__(
        self,
        payload: bytes,
        *,
        url: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = io.BytesIO(payload)
        self._url = url
        self.status = status
        self.headers = {} if headers is None else headers

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        self._body.close()


def _session(
    tmp_path: Path,
    *,
    config: PolyHavenRuntimeConfig | None = None,
    clock: _FakeClock | None = None,
    provider_wide: bool = False,
) -> PolyHavenProviderSession:
    data_dir = tmp_path / "data"
    return PolyHavenProviderSession(
        project_root=tmp_path,
        data_dir=data_dir,
        config=config,
        clock=clock,
        storage_root=data_dir / "acquire/polyhaven" if provider_wide else None,
    )


def _spec(payload: bytes, *, name: str = "fixture.bin") -> PolyHavenFileSpec:
    return PolyHavenFileSpec(
        relative_path=Path(name),
        url=f"https://dl.polyhaven.org/{name}",
        bytes=len(payload),
        md5=hashlib.md5(payload, usedforsecurity=False).hexdigest(),
    )


def test_provider_session_fetch_and_download_expose_detached_runtime_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _FakeClock()
    api_url = "https://api.polyhaven.com/files/fixture"
    download = b"provider bytes"
    file_url = "https://dl.polyhaven.org/fixture.bin"

    def fake_open(request: Any, **_kwargs: Any) -> _Response:
        if request.full_url == api_url:
            return _Response(b'{"ok":true}', url=api_url)
        assert request.full_url == file_url
        return _Response(
            download,
            url=file_url,
            headers={"Content-Length": str(len(download))},
        )

    monkeypatch.setattr(polyhaven, "_open_url", fake_open)
    with polyhaven_source_lock(tmp_path / "data") as lock_path:
        assert lock_path == tmp_path / "data/locks/acquire/polyhaven.lock"
        session = _session(
            tmp_path,
            config=PolyHavenRuntimeConfig(request_rate_per_sec=1_000_000),
            clock=clock,
            provider_wide=True,
        )
        assert session.fetch_json(api_url) == {"ok": True}
        destination = session.storage_root / "resources/fixture/fixture.bin"
        first = session.acquire_file(
            _spec(download),
            destination=destination,
            force=False,
            item_id="polyhaven_fixture_aaaaaaaaaaaa",
        )
        reused = session.acquire_file(
            _spec(download),
            destination=destination,
            force=False,
            item_id="polyhaven_fixture_aaaaaaaaaaaa",
        )

    assert first.path == destination
    assert first.sha256 == hashlib.sha256(download).hexdigest()
    assert first.reused is False
    assert first.downloaded_bytes == len(download)
    assert reused.reused is True
    assert reused.downloaded_bytes == 0
    assert destination.read_bytes() == download

    evidence = session.runtime_evidence()
    assert evidence["http"] == {
        "request_attempts": 2,
        "retry_attempts": 0,
        "retry_after_honored": 0,
        "rate_limit_wait_ms": 0,
        "retry_wait_ms": 0,
        "download_body_bytes": len(download),
    }
    evidence["http"]["request_attempts"] = 999  # type: ignore[index]
    assert session.runtime_evidence()["http"]["request_attempts"] == 2


def test_provider_operation_error_preserves_retry_classification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _FakeClock()
    session = _session(
        tmp_path,
        config=PolyHavenRuntimeConfig(
            request_rate_per_sec=1_000_000,
            retry_max_attempts=2,
            retry_base_delay_sec=0.001,
            retry_max_delay_sec=0.001,
        ),
        clock=clock,
    )
    url = "https://api.polyhaven.com/assets?type=hdris"
    monkeypatch.setattr(
        polyhaven,
        "_open_url",
        lambda *_args, **_kwargs: _Response(b"", url=url, status=503),
    )

    with pytest.raises(ProviderOperationError) as raised:
        session.fetch_json(url)

    error = raised.value
    assert error.failure.kind is FailureKind.HTTP_TRANSIENT
    assert error.failure.phase == "api"
    assert error.failure.http_status == 503
    assert error.attempts_in_run == 2
    assert error.retry_after_deadline is None
    assert error.exhausted is True
    assert clock.sleeps == [0.001]
    assert isinstance(error.__cause__, Exception)


def test_provider_wide_storage_root_is_used_for_disk_quota(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "data/acquire/polyhaven/resources/existing.bin"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"1234")
    session = _session(
        tmp_path,
        config=PolyHavenRuntimeConfig(
            request_rate_per_sec=1_000_000,
            max_storage_bytes=5,
        ),
        clock=_FakeClock(),
        provider_wide=True,
    )

    with pytest.raises(ProviderOperationError) as raised:
        session.acquire_file(
            _spec(b"xx"),
            destination=session.storage_root / "resources/new/fixture.bin",
            force=False,
            item_id="polyhaven_fixture_bbbbbbbbbbbb",
        )

    assert raised.value.failure.kind is FailureKind.DISK
    assert raised.value.attempts_in_run == 1
    assert raised.value.exhausted is False
    evidence = session.runtime_evidence()
    assert evidence["disk"]["max_storage_bytes_observed"] == 4
    assert evidence["http"]["request_attempts"] == 0


def test_additional_storage_roots_are_included_in_shared_disk_quota(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    storage_root = data_dir / "acquire/polyhaven"
    additional_root = data_dir / "hdri"
    primary_file = storage_root / "resources/existing.bin"
    additional_file = additional_root / "studio.hdr"
    primary_file.parent.mkdir(parents=True)
    additional_file.parent.mkdir(parents=True)
    primary_file.write_bytes(b"12")
    additional_file.write_bytes(b"345")
    session = PolyHavenProviderSession(
        project_root=tmp_path,
        data_dir=data_dir,
        config=PolyHavenRuntimeConfig(max_storage_bytes=6),
        clock=_FakeClock(),
        storage_root=storage_root,
        additional_storage_roots=(additional_root,),
    )

    session.check_disk_growth(1)
    with pytest.raises(ProviderOperationError) as raised:
        session.check_disk_growth(2)

    assert raised.value.failure.kind is FailureKind.DISK
    assert raised.value.failure.phase == "disk_quota"
    evidence = session.runtime_evidence()
    assert evidence["disk"]["checks"] == 2
    assert evidence["disk"]["max_storage_bytes_observed"] == 5
    assert evidence["http"]["request_attempts"] == 0


def test_provider_item_reservations_share_the_durable_quota_ledger(tmp_path: Path) -> None:
    item_id = "polyhaven_fixture_cccccccccccc"

    with polyhaven_source_lock(tmp_path / "data"):
        session = _session(
            tmp_path,
            config=PolyHavenRuntimeConfig(max_new_items_per_day=1),
            clock=_FakeClock(),
            provider_wide=True,
        )
        session.reserve_items((item_id,))

    evidence = session.runtime_evidence()["daily_quota"]
    assert evidence["reserved_by_run"]["new_items"] == 1
    assert evidence["usage_after"]["new_items_reserved"] == 1
    assert evidence["item_reservations_after"] == 1
    assert (tmp_path / "data/acquire/polyhaven/quota_state.json").is_file()


def test_provider_bounded_item_reservations_allow_prefix_and_defer_remainder(
    tmp_path: Path,
) -> None:
    first = "polyhaven_fixture_111111111111"
    second = "polyhaven_fixture_222222222222"
    third = "polyhaven_fixture_333333333333"
    fourth = "polyhaven_fixture_444444444444"

    with polyhaven_source_lock(tmp_path / "data"):
        session = _session(
            tmp_path,
            config=PolyHavenRuntimeConfig(max_new_items_per_day=2),
            clock=_FakeClock(),
            provider_wide=True,
        )
        allowed, deferred = session.reserve_items_bounded((first, second, third))
        already_reserved, newly_deferred = session.reserve_items_bounded((first, fourth))

    assert allowed == (first, second)
    assert deferred == (third,)
    assert already_reserved == (first,)
    assert newly_deferred == (fourth,)
    evidence = session.runtime_evidence()["daily_quota"]
    assert evidence["reserved_by_run"]["new_items"] == 2
    assert evidence["usage_after"]["new_items_reserved"] == 2
    assert evidence["item_reservations_after"] == 2
    assert evidence["deferred_new_items"] == 2


def test_provider_bounded_item_reservations_allow_all_when_unlimited(
    tmp_path: Path,
) -> None:
    item_ids = (
        "polyhaven_fixture_555555555555",
        "polyhaven_fixture_666666666666",
    )
    session = _session(tmp_path, clock=_FakeClock(), provider_wide=True)

    assert session.reserve_items_bounded(item_ids) == (item_ids, ())
    evidence = session.runtime_evidence()["daily_quota"]
    assert evidence["enabled"] is False
    assert evidence["reserved_by_run"]["new_items"] == 0
    assert evidence["deferred_new_items"] == 0
    assert not (tmp_path / "data/acquire/polyhaven/quota_state.json").exists()


def test_provider_rejects_overlapping_accounted_storage_roots(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    storage_root = data_dir / "acquire/polyhaven"

    with pytest.raises(PolyHavenAcquireError, match="may not overlap"):
        PolyHavenProviderSession(
            project_root=tmp_path,
            data_dir=data_dir,
            storage_root=storage_root,
            additional_storage_roots=(storage_root / "resources",),
        )


def test_provider_rejects_destinations_outside_storage_root(tmp_path: Path) -> None:
    session = _session(tmp_path, provider_wide=True)

    with pytest.raises(PolyHavenAcquireError, match="inside storage_root"):
        session.acquire_file(
            _spec(b"x"),
            destination=tmp_path / "outside/fixture.bin",
            force=False,
            item_id="polyhaven_fixture_dddddddddddd",
        )


def test_provider_revalidates_directly_constructed_file_specs(tmp_path: Path) -> None:
    session = _session(tmp_path, provider_wide=True)
    unsafe = _spec(b"x")
    unsafe = PolyHavenFileSpec(
        relative_path=Path("/fixture.bin"),
        url=unsafe.url,
        bytes=unsafe.bytes,
        md5=unsafe.md5,
    )

    with pytest.raises(PolyHavenAcquireError, match="relative path"):
        session.acquire_file(
            unsafe,
            destination=session.storage_root / "fixture.bin",
            force=False,
            item_id="polyhaven_fixture_eeeeeeeeeeee",
        )
