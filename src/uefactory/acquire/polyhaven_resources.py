"""Strict, side-effect-free parsing for Poly Haven HDRI and PBR resources.

The provider's listing response binds an asset to ``files_hash`` while the
per-asset files response does not repeat that revision.  Parsed file packages
therefore retain provider-local filenames and require the caller to supply the
listing revision before deriving durable storage paths.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit

from uefactory.acquire.polyhaven import PolyHavenAcquireError, PolyHavenPathSecurityError
from uefactory.core.identity import validate_snake_slug

ResourceKind = Literal["hdri", "pbr_texture_set"]
ResourceProviderRole = Literal["hdri", "Diffuse", "nor_dx", "arm"]

DEFAULT_RESOURCE_RESOLUTION = "1k"
RESOURCE_PROFILE_BY_KIND: dict[ResourceKind, str] = {
    "hdri": "radiance_hdr_v1",
    "pbr_texture_set": "ue_pbr_png_v1",
}

_EXPECTED_LISTING_TYPE: dict[ResourceKind, int] = {
    "hdri": 0,
    "pbr_texture_set": 1,
}
_KIND_ID_TOKEN: dict[ResourceKind, str] = {
    "hdri": "hdri",
    "pbr_texture_set": "pbr",
}
_PBR_FILENAME_ROLE: dict[ResourceProviderRole, str] = {
    "Diffuse": "diff",
    "nor_dx": "nor_dx",
    "arm": "arm",
    "hdri": "hdri",
}
_PBR_ROLES: tuple[ResourceProviderRole, ...] = ("Diffuse", "nor_dx", "arm")
_DOWNLOAD_HOST = "dl.polyhaven.org"
_SOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?\Z")
_SHA1_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_MD5_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_RESOLUTION_PATTERN = re.compile(r"[1-9][0-9]*k\Z")
_MAX_FILE_BYTES = 32 * 1024 * 1024 * 1024
_RESOURCE_ID_DOMAIN = b"uefactory.polyhaven-resource-id.v1\0"


@dataclass(frozen=True, slots=True)
class PolyHavenResourceListing:
    """Provider metadata for one immutable HDRI or PBR source revision."""

    kind: ResourceKind
    source_id: str
    name: str
    date_published: int
    revision: str
    authors: tuple[tuple[str, str], ...]
    categories: tuple[str, ...]
    tags: tuple[str, ...]
    physical_size_mm: tuple[float, float] | None

    @property
    def profile(self) -> str:
        return RESOURCE_PROFILE_BY_KIND[self.kind]

    def resource_id(self, resolution: str = DEFAULT_RESOURCE_RESOLUTION) -> str:
        return revisioned_resource_id(self.kind, self.source_id, self.revision, resolution)


@dataclass(frozen=True, slots=True)
class PolyHavenResourceFileSpec:
    """One exact provider file selected from a resource files response."""

    provider_role: ResourceProviderRole
    relative_path: Path
    url: str
    bytes: int
    md5: str


@dataclass(frozen=True, slots=True)
class PolyHavenResourcePackage:
    """The exact file cohort selected for one kind and resolution."""

    kind: ResourceKind
    source_id: str
    profile: str
    resolution: str
    files: tuple[PolyHavenResourceFileSpec, ...]

    def storage_root(self, revision: str) -> Path:
        """Return the canonical data-dir-relative root for this revision."""

        return resource_storage_root(
            self.kind,
            self.source_id,
            revision,
            self.resolution,
        )

    def storage_files(self, revision: str) -> tuple[PolyHavenResourceFileSpec, ...]:
        """Return specs whose paths bind kind, source, revision, profile and resolution."""

        root = self.storage_root(revision)
        return tuple(
            replace(file_spec, relative_path=root / file_spec.relative_path)
            for file_spec in self.files
        )


def parse_polyhaven_resource_listing(
    payload: Any,
    kind: ResourceKind,
) -> tuple[PolyHavenResourceListing, ...]:
    """Validate an official ``/assets?type=hdris|textures`` response."""

    checked_kind = _resource_kind(kind)
    root = _object(payload, f"Poly Haven {checked_kind} listing")
    if not root:
        raise PolyHavenAcquireError(f"Poly Haven {checked_kind} listing is empty")

    listings: list[PolyHavenResourceListing] = []
    normalized_sources: dict[str, str] = {}
    for raw_source_id, raw_listing in root.items():
        source_id = _source_id(raw_source_id, "resource source id")
        normalized = source_id.casefold()
        previous = normalized_sources.get(normalized)
        if previous is not None and previous != source_id:
            raise PolyHavenAcquireError(
                "Poly Haven source ids collide after casefold normalization: "
                f"{previous!r}, {source_id!r}"
            )
        normalized_sources[normalized] = source_id

        context = f"{checked_kind} resource {source_id!r}"
        listing = _object(raw_listing, context)
        expected_type = _EXPECTED_LISTING_TYPE[checked_kind]
        listing_type = listing.get("type")
        if isinstance(listing_type, bool) or listing_type != expected_type:
            raise PolyHavenAcquireError(f"{context}.type must be integer {expected_type}")

        revision = _revision(listing.get("files_hash"), f"{context}.files_hash")
        authors_raw = _object(listing.get("authors"), f"{context}.authors")
        if not authors_raw:
            raise PolyHavenAcquireError(f"{context}.authors is empty")
        authors = tuple(
            sorted(
                (
                    _string(
                        author.strip() if isinstance(author, str) else author,
                        f"{context}.author",
                        max_length=256,
                    ),
                    _string(
                        credit.strip() if isinstance(credit, str) else credit,
                        f"{context}.author credit",
                        max_length=256,
                    ),
                )
                for author, credit in authors_raw.items()
            )
        )
        physical_size_mm = (
            _physical_size(listing.get("dimensions"), f"{context}.dimensions")
            if checked_kind == "pbr_texture_set"
            else None
        )
        listings.append(
            PolyHavenResourceListing(
                kind=checked_kind,
                source_id=source_id,
                name=_string(
                    (
                        listing["name"].strip()
                        if isinstance(listing.get("name"), str)
                        else listing.get("name")
                    ),
                    f"{context}.name",
                    max_length=256,
                ),
                date_published=_positive_int(
                    listing.get("date_published"), f"{context}.date_published"
                ),
                revision=revision,
                authors=authors,
                categories=_string_sequence(
                    listing.get("categories"), f"{context}.categories", max_items=256
                ),
                tags=_string_sequence(listing.get("tags"), f"{context}.tags", max_items=512),
                physical_size_mm=physical_size_mm,
            )
        )
    return tuple(
        sorted(listings, key=lambda item: (item.date_published, item.source_id.casefold()))
    )


def parse_polyhaven_resource_files(
    source_id: str,
    payload: Any,
    kind: ResourceKind,
    resolution: str = DEFAULT_RESOURCE_RESOLUTION,
) -> PolyHavenResourcePackage:
    """Select only the supported resource cohort; formats are never substituted."""

    checked_kind = _resource_kind(kind)
    checked_source_id = _source_id(source_id, "source_id")
    checked_resolution = _resolution(resolution)
    root = _object(payload, f"files for {checked_source_id!r}")

    roles: tuple[ResourceProviderRole, ...]
    entries: tuple[dict[str, Any], ...]
    if checked_kind == "hdri":
        entry = _nested_file_entry(
            root,
            ("hdri", checked_resolution, "hdr"),
            source_id=checked_source_id,
            kind=checked_kind,
            resolution=checked_resolution,
        )
        roles = ("hdri",)
        entries = (entry,)
    else:
        roles = _PBR_ROLES
        entries = tuple(
            _nested_file_entry(
                root,
                (role, checked_resolution, "png"),
                source_id=checked_source_id,
                kind=checked_kind,
                resolution=checked_resolution,
            )
            for role in roles
        )

    files = tuple(
        _file_spec(
            entry,
            source_id=checked_source_id,
            kind=checked_kind,
            resolution=checked_resolution,
            role=role,
        )
        for role, entry in zip(roles, entries, strict=True)
    )
    return PolyHavenResourcePackage(
        kind=checked_kind,
        source_id=checked_source_id,
        profile=RESOURCE_PROFILE_BY_KIND[checked_kind],
        resolution=checked_resolution,
        files=files,
    )


def revisioned_resource_id(
    kind: ResourceKind,
    source_id: str,
    revision: str,
    resolution: str,
) -> str:
    """Return a <=64-character identity bound to the complete resource tuple.

    The readable source component is not relied upon for uniqueness.  A
    domain-separated 128-bit suffix covers the exact (case- and punctuation-
    preserving) source id, full provider revision, profile, kind and resolution.
    """

    checked_kind = _resource_kind(kind)
    checked_source_id = _source_id(source_id, "source_id")
    checked_revision = _revision(revision, "revision")
    checked_resolution = _resolution(resolution)
    profile = RESOURCE_PROFILE_BY_KIND[checked_kind]
    identity = json.dumps(
        [checked_kind, checked_source_id, checked_revision, profile, checked_resolution],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    digest = hashlib.sha256(_RESOURCE_ID_DOMAIN + identity).hexdigest()[:32]
    prefix = f"polyhaven_{_KIND_ID_TOKEN[checked_kind]}_"
    suffix = f"_{digest}"
    source_budget = 64 - len(prefix) - len(suffix)
    readable_source = re.sub(r"[-_]+", "_", checked_source_id.casefold())
    readable_source = readable_source[:source_budget].rstrip("_")
    if not readable_source:
        raise PolyHavenAcquireError(
            f"Poly Haven source id cannot form a catalog resource id: {source_id!r}"
        )
    candidate = f"{prefix}{readable_source}{suffix}"
    try:
        return validate_snake_slug(candidate, field="resource_id", max_length=64)
    except ValueError as exc:  # pragma: no cover - guarded by the component validators
        raise PolyHavenAcquireError(
            f"Poly Haven source id cannot form a catalog resource id: {source_id!r}"
        ) from exc


def resource_storage_root(
    kind: ResourceKind,
    source_id: str,
    revision: str,
    resolution: str,
) -> Path:
    """Return a safe, injective data-dir-relative root for one resource cohort."""

    checked_kind = _resource_kind(kind)
    checked_source_id = _source_id(source_id, "source_id")
    checked_revision = _revision(revision, "revision")
    checked_resolution = _resolution(resolution)
    profile = RESOURCE_PROFILE_BY_KIND[checked_kind]
    return Path(
        "acquire",
        "polyhaven",
        "resources",
        checked_kind,
        checked_source_id,
        checked_revision,
        profile,
        checked_resolution,
    )


def _nested_file_entry(
    root: dict[str, Any],
    keys: tuple[str, str, str],
    *,
    source_id: str,
    kind: ResourceKind,
    resolution: str,
) -> dict[str, Any]:
    current: Any = root
    try:
        for key in keys:
            current = _object(current, f"files {source_id!r}.{'.'.join(keys)}")[key]
    except KeyError as exc:
        profile = RESOURCE_PROFILE_BY_KIND[kind]
        raise PolyHavenAcquireError(
            f"Poly Haven {kind} {source_id!r} has no exact {resolution} {profile} cohort"
        ) from exc
    return _object(current, f"files {source_id!r}.{'.'.join(keys)}")


def _file_spec(
    payload: dict[str, Any],
    *,
    source_id: str,
    kind: ResourceKind,
    resolution: str,
    role: ResourceProviderRole,
) -> PolyHavenResourceFileSpec:
    context = f"{kind} {source_id!r} {role} {resolution}"
    extra = sorted(set(payload) - {"url", "md5", "size"})
    if extra:
        raise PolyHavenAcquireError(f"{context} contains unsupported key {extra[0]!r}")
    expected_name = _expected_filename(source_id, kind, resolution, role)
    url = _download_url(payload.get("url"), f"{context}.url")
    actual_name = unquote(PurePosixPath(urlsplit(url).path).name)
    if actual_name != expected_name:
        raise PolyHavenPathSecurityError(
            f"{context}.url filename does not match the canonical provider filename"
        )
    md5 = _string(payload.get("md5"), f"{context}.md5", max_length=32)
    if _MD5_PATTERN.fullmatch(md5) is None:
        raise PolyHavenAcquireError(f"{context}.md5 must be lowercase 32-character MD5")
    size = _positive_int(payload.get("size"), f"{context}.size")
    if size > _MAX_FILE_BYTES:
        raise PolyHavenAcquireError(f"{context}.size exceeds the 32 GiB safety limit")
    return PolyHavenResourceFileSpec(
        provider_role=role,
        relative_path=Path(expected_name),
        url=url,
        bytes=size,
        md5=md5,
    )


def _expected_filename(
    source_id: str,
    kind: ResourceKind,
    resolution: str,
    role: ResourceProviderRole,
) -> str:
    if kind == "hdri":
        if role != "hdri":  # pragma: no cover - closed by parser construction
            raise PolyHavenAcquireError("HDRI package contains a non-HDRI provider role")
        return f"{source_id}_{resolution}.hdr"
    if role == "hdri":  # pragma: no cover - closed by parser construction
        raise PolyHavenAcquireError("PBR package contains an HDRI provider role")
    return f"{source_id}_{_PBR_FILENAME_ROLE[role]}_{resolution}.png"


def _download_url(value: Any, context: str) -> str:
    url = _string(value, context, max_length=4_096)
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise PolyHavenPathSecurityError(f"{context}: invalid URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != _DOWNLOAD_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise PolyHavenPathSecurityError(f"{context}: unapproved Poly Haven download URL")
    return url


def _resource_kind(value: Any) -> ResourceKind:
    if not isinstance(value, str) or value not in RESOURCE_PROFILE_BY_KIND:
        raise PolyHavenAcquireError("resource kind must be hdri or pbr_texture_set")
    return cast(ResourceKind, value)


def _source_id(value: Any, context: str) -> str:
    source_id = _string(value, context, max_length=64)
    if _SOURCE_ID_PATTERN.fullmatch(source_id) is None:
        raise PolyHavenAcquireError(
            f"{context}: expected 1-64 safe Poly Haven identifier characters "
            "with alphanumeric boundaries"
        )
    return source_id


def _revision(value: Any, context: str) -> str:
    revision = _string(value, context, max_length=40)
    if _SHA1_PATTERN.fullmatch(revision) is None:
        raise PolyHavenAcquireError(f"{context} must be a lowercase 40-character SHA-1")
    return revision


def _resolution(value: Any) -> str:
    resolution = _string(value, "resolution", max_length=16)
    if _RESOLUTION_PATTERN.fullmatch(resolution) is None:
        raise PolyHavenAcquireError("resolution must look like '1k' or '2k'")
    return resolution


def _physical_size(value: Any, context: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise PolyHavenAcquireError(f"{context} must contain exactly two numbers")
    result: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise PolyHavenAcquireError(f"{context}[{index}] must be a finite positive number")
        converted = float(item)
        if not math.isfinite(converted) or converted <= 0:
            raise PolyHavenAcquireError(f"{context}[{index}] must be a finite positive number")
        result.append(converted)
    return (result[0], result[1])


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
    return tuple(dict.fromkeys(checked))


__all__ = [
    "DEFAULT_RESOURCE_RESOLUTION",
    "RESOURCE_PROFILE_BY_KIND",
    "PolyHavenResourceFileSpec",
    "PolyHavenResourceListing",
    "PolyHavenResourcePackage",
    "ResourceKind",
    "parse_polyhaven_resource_files",
    "parse_polyhaven_resource_listing",
    "resource_storage_root",
    "revisioned_resource_id",
]
