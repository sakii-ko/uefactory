"""Public hardened transport/runtime boundary for Poly Haven adapters.

The model adapter predates the provider-wide HDRI and texture adapters.  Its
transport and quota implementation is intentionally kept in one place, while
this facade prevents new adapters from depending on model-private symbols.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from uefactory.acquire import polyhaven as _model_adapter
from uefactory.acquire.polyhaven import (
    PolyHavenAcquireError,
    PolyHavenFileSpec,
    PolyHavenRuntimeConfig,
)
from uefactory.acquire.runtime import AcquisitionFailure, Clock, SystemClock
from uefactory.core.identity import validate_asset_id


class ProviderOperationError(PolyHavenAcquireError):
    """A classified provider operation failure suitable for durable journals."""

    def __init__(
        self,
        *,
        failure: AcquisitionFailure,
        attempts_in_run: int,
        retry_after_deadline: datetime | None,
        exhausted: bool,
    ) -> None:
        self.failure = failure
        self.attempts_in_run = attempts_in_run
        self.retry_after_deadline = retry_after_deadline
        self.exhausted = exhausted
        suffix = "retry budget exhausted" if exhausted else "not retryable"
        super().__init__(f"{failure.message} ({suffix})")

    @classmethod
    def _from_internal(
        cls,
        exc: _model_adapter._ClassifiedItemFailure,
    ) -> ProviderOperationError:
        # This is the sole compatibility seam with the original model
        # adapter.  Provider consumers only see the stable public exception.
        classified = exc
        return cls(
            failure=classified.failure,
            attempts_in_run=classified.attempts_in_run,
            retry_after_deadline=classified.retry_after_deadline,
            exhausted=classified.exhausted,
        )


@dataclass(frozen=True, slots=True)
class ProviderFileResult:
    """Verified local result of one provider file acquisition."""

    path: Path
    sha256: str
    reused: bool
    downloaded_bytes: int


@contextmanager
def polyhaven_source_lock(data_dir: Path) -> Iterator[Path]:
    """Acquire the provider mutation lock before constructing quota runtime state."""

    if not isinstance(data_dir, Path):
        raise PolyHavenAcquireError("data_dir must be a pathlib.Path")
    with _model_adapter._source_lock(data_dir.expanduser().resolve()) as lock_path:
        yield lock_path


class PolyHavenProviderSession:
    """Share Poly Haven pacing, retries, quotas, disk accounting, and locking."""

    def __init__(
        self,
        *,
        project_root: Path,
        data_dir: Path,
        config: PolyHavenRuntimeConfig | None = None,
        clock: Clock | None = None,
        storage_root: Path | None = None,
        additional_storage_roots: tuple[Path, ...] = (),
    ) -> None:
        checked_project_root = project_root.expanduser().resolve()
        unresolved_data_dir = data_dir.expanduser()
        if not unresolved_data_dir.is_absolute():
            unresolved_data_dir = checked_project_root / unresolved_data_dir
        _model_adapter._reject_symlink_components(
            unresolved_data_dir,
            project_root=checked_project_root,
            context="Poly Haven provider data_dir",
        )
        checked_data_dir = unresolved_data_dir.resolve()
        _model_adapter._require_data_dir_inside_project(
            project_root=checked_project_root,
            data_dir=checked_data_dir,
        )

        unresolved_storage_root = (
            checked_data_dir / "acquire/polyhaven/models"
            if storage_root is None
            else storage_root.expanduser()
        )
        if not unresolved_storage_root.is_absolute():
            unresolved_storage_root = checked_data_dir / unresolved_storage_root
        _model_adapter._reject_symlink_components(
            unresolved_storage_root,
            project_root=checked_project_root,
            context="Poly Haven provider storage root",
        )
        checked_storage_root = unresolved_storage_root.resolve()
        try:
            storage_relative = checked_storage_root.relative_to(checked_data_dir)
        except ValueError as exc:
            raise PolyHavenAcquireError(
                "Poly Haven provider storage root must be inside data_dir"
            ) from exc
        if not storage_relative.parts:
            raise PolyHavenAcquireError("Poly Haven provider storage root may not equal data_dir")
        if not isinstance(additional_storage_roots, tuple):
            raise PolyHavenAcquireError("additional_storage_roots must be a tuple")
        checked_additional_roots: list[Path] = []
        for extra_root in additional_storage_roots:
            if not isinstance(extra_root, Path):
                raise PolyHavenAcquireError(
                    "additional_storage_roots must contain pathlib.Path values"
                )
            unresolved_extra = extra_root.expanduser()
            if not unresolved_extra.is_absolute():
                unresolved_extra = checked_data_dir / unresolved_extra
            _model_adapter._reject_symlink_components(
                unresolved_extra,
                project_root=checked_project_root,
                context="Poly Haven additional storage root",
            )
            checked_extra = unresolved_extra.resolve()
            try:
                extra_relative = checked_extra.relative_to(checked_data_dir)
            except ValueError as exc:
                raise PolyHavenAcquireError(
                    "Poly Haven additional storage roots must be inside data_dir"
                ) from exc
            if not extra_relative.parts:
                raise PolyHavenAcquireError(
                    "Poly Haven additional storage root may not equal data_dir"
                )
            checked_additional_roots.append(checked_extra)
        all_roots = (checked_storage_root, *checked_additional_roots)
        if any(
            left == right or _is_relative_to(left, right) or _is_relative_to(right, left)
            for index, left in enumerate(all_roots)
            for right in all_roots[index + 1 :]
        ):
            raise PolyHavenAcquireError("Poly Haven accounted storage roots may not overlap")

        checked_config = PolyHavenRuntimeConfig() if config is None else config
        if not isinstance(checked_config, PolyHavenRuntimeConfig):
            raise PolyHavenAcquireError("config must be PolyHavenRuntimeConfig")
        checked_clock = SystemClock() if clock is None else clock

        self.project_root = checked_project_root
        self.data_dir = checked_data_dir
        self.storage_root = checked_storage_root
        self.additional_storage_roots = tuple(checked_additional_roots)
        self.config = checked_config
        self.clock = checked_clock
        self._runtime = _model_adapter._AcquisitionRuntime(
            config=checked_config,
            clock=checked_clock,
            project_root=checked_project_root,
            data_dir=checked_data_dir,
            storage_root=checked_storage_root,
            additional_storage_roots=self.additional_storage_roots,
        )

    def fetch_json(self, url: str) -> dict[str, Any]:
        """Fetch and strictly decode an allowlisted Poly Haven API object."""

        try:
            return _model_adapter._fetch_json(url, runtime=self._runtime)
        except _model_adapter._ClassifiedItemFailure as exc:
            raise ProviderOperationError._from_internal(exc) from exc

    def acquire_file(
        self,
        spec: PolyHavenFileSpec,
        *,
        destination: Path,
        force: bool,
        item_id: str,
    ) -> ProviderFileResult:
        """Acquire and verify one file within the configured storage boundary."""

        if not isinstance(spec, PolyHavenFileSpec):
            raise PolyHavenAcquireError("spec must be PolyHavenFileSpec")
        try:
            checked_item_id = validate_asset_id(item_id)
        except (TypeError, ValueError) as exc:
            raise PolyHavenAcquireError("item_id must be a valid asset id") from exc
        if not isinstance(force, bool):
            raise PolyHavenAcquireError("force must be boolean")
        if not isinstance(spec.relative_path, Path):
            raise PolyHavenAcquireError("spec.relative_path must be a Path")
        checked_relative_path = _model_adapter._relative_path(
            spec.relative_path.as_posix(),
            "Poly Haven provider file path",
        )

        checked_spec = _model_adapter._file_spec(
            checked_relative_path,
            {"url": spec.url, "md5": spec.md5, "size": spec.bytes},
            context="Poly Haven provider file",
        )
        if not isinstance(destination, Path):
            raise PolyHavenAcquireError("destination must be a Path")
        unresolved_destination = destination.expanduser()
        if not unresolved_destination.is_absolute():
            unresolved_destination = self.storage_root / unresolved_destination
        _model_adapter._reject_symlink_components(
            unresolved_destination,
            project_root=self.project_root,
            context="Poly Haven provider destination",
        )
        checked_destination = unresolved_destination.resolve()
        try:
            destination_relative = checked_destination.relative_to(self.storage_root)
        except ValueError as exc:
            raise PolyHavenAcquireError(
                "Poly Haven provider destination must be inside storage_root"
            ) from exc
        if not destination_relative.parts:
            raise PolyHavenAcquireError(
                "Poly Haven provider destination may not equal storage_root"
            )
        if checked_destination.name != checked_spec.relative_path.name:
            raise PolyHavenAcquireError(
                "Poly Haven provider destination filename differs from the declared file"
            )

        try:
            result = _model_adapter._acquire_file(
                checked_spec,
                destination=checked_destination,
                force=force,
                asset_id=checked_item_id,
                runtime=self._runtime,
            )
        except _model_adapter._ClassifiedItemFailure as exc:
            raise ProviderOperationError._from_internal(exc) from exc
        return ProviderFileResult(
            path=result.path,
            sha256=result.sha256,
            reused=result.reused,
            downloaded_bytes=result.downloaded_bytes,
        )

    def reserve_items(self, item_ids: tuple[str, ...]) -> None:
        """Durably reserve newly selected provider item identities."""

        if not isinstance(item_ids, tuple):
            raise PolyHavenAcquireError("item_ids must be an immutable tuple")
        checked: list[str] = []
        for item_id in item_ids:
            try:
                checked.append(validate_asset_id(item_id))
            except (TypeError, ValueError) as exc:
                raise PolyHavenAcquireError("item_ids contains an invalid asset id") from exc
        if len(checked) != len(set(checked)):
            raise PolyHavenAcquireError("item_ids contains duplicate asset ids")
        self._runtime.quota.reserve_items(tuple(checked))

    def reserve_items_bounded(
        self, item_ids: tuple[str, ...]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Reserve the quota-eligible prefix and return ``(allowed, deferred)``."""

        if not isinstance(item_ids, tuple):
            raise PolyHavenAcquireError("item_ids must be an immutable tuple")
        checked: list[str] = []
        for item_id in item_ids:
            try:
                checked.append(validate_asset_id(item_id))
            except (TypeError, ValueError) as exc:
                raise PolyHavenAcquireError("item_ids contains an invalid asset id") from exc
        if len(checked) != len(set(checked)):
            raise PolyHavenAcquireError("item_ids contains duplicate asset ids")
        maximum = self.config.daily_quota_limits.max_new_items
        if maximum is None:
            self._runtime.quota.reserve_items(tuple(checked))
            return tuple(checked), ()
        existing = self._runtime.quota.item_ids
        remaining = max(0, maximum - self._runtime.quota.usage.new_items_reserved)
        allowed: list[str] = []
        deferred: list[str] = []
        for item_id in checked:
            if item_id in existing:
                allowed.append(item_id)
            elif remaining:
                allowed.append(item_id)
                remaining -= 1
            else:
                deferred.append(item_id)
        self._runtime.quota.reserve_items(tuple(allowed))
        self._runtime.stats.deferred_new_items += len(deferred)
        return tuple(allowed), tuple(deferred)

    def check_disk_growth(self, growth_bytes: int) -> None:
        """Apply the shared disk/free-space policy before a non-HTTP copy."""

        try:
            self._runtime.check_disk_growth(growth_bytes)
        except _model_adapter._ClassifiedItemFailure as exc:
            raise ProviderOperationError._from_internal(exc) from exc

    def runtime_evidence(self) -> Mapping[str, Any]:
        """Return a detached, JSON-serializable snapshot of runtime accounting."""

        return copy.deepcopy(self._runtime.evidence())


__all__ = [
    "PolyHavenAcquireError",
    "PolyHavenFileSpec",
    "PolyHavenProviderSession",
    "PolyHavenRuntimeConfig",
    "ProviderFileResult",
    "ProviderOperationError",
    "polyhaven_source_lock",
]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
