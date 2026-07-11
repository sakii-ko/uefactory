from __future__ import annotations

import json
import math
import sqlite3
import time
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

SCHEMA_VERSION = 5
DEFAULT_BUSY_TIMEOUT_MS = 5_000

ASSET_STATUSES = frozenset({"raw", "imported", "render_ok", "failed"})
SCENE_STATUSES = frozenset({"raw", "built", "render_ok", "failed", "quarantined"})
RESOURCE_KINDS = frozenset({"hdri", "pbr_texture_set"})
RESOURCE_STATUSES = frozenset({"verified", "ready", "failed", "quarantined"})
RESOURCE_COLOR_SPACES = frozenset({"srgb", "linear", "data"})
RESOURCE_NORMAL_CONVENTIONS = frozenset({"opengl", "directx"})
RESOURCE_CHANNELS = frozenset({"r", "g", "b", "a"})
RESOURCE_PROFILES = {
    "hdri": frozenset({"radiance_hdr_v1", "radiance_exr_v1"}),
    "pbr_texture_set": frozenset({"ue_pbr_png_v1"}),
}
RESOURCE_REQUIRED_ARTIFACT_KINDS = {
    "verified": frozenset({"resource_source_manifest"}),
    "hdri": frozenset({"resource_source_manifest", "hdri_validation_manifest"}),
    "pbr_texture_set": frozenset(
        {
            "resource_source_manifest",
            "pbr_material_descriptor",
            "pbr_validation_manifest",
        }
    ),
}
LICENSE_TIERS = frozenset({"open", "nc", "ue-only"})
SCENE_BUILD_ARTIFACT_KINDS = frozenset(
    {
        "scene_build_manifest",
        "scene_primary_manifest",
        "scene_reload_manifest",
        "scene_finalize_manifest",
    }
)
SCENE_RENDER_ARTIFACT_KINDS = frozenset(
    {
        "scene_thumbnail_beauty",
        "scene_thumbnail_mask",
        "scene_thumbnail_mask_raw",
        "scene_thumbnail_render_manifest",
        "scene_thumbnail_contact_sheet",
    }
)

OPEN_LICENSES = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC0-1.0",
        "CC-BY-3.0",
        "CC-BY-4.0",
        "CC-BY-SA-3.0",
        "CC-BY-SA-4.0",
        "GPL-2.0-only",
        "GPL-3.0-only",
        "MIT",
        "Unlicense",
    }
)
NC_LICENSES = frozenset(
    {
        "CC-BY-NC-3.0",
        "CC-BY-NC-4.0",
        "CC-BY-NC-ND-3.0",
        "CC-BY-NC-ND-4.0",
        "CC-BY-NC-SA-3.0",
        "CC-BY-NC-SA-4.0",
        "LicenseRef-Research-Only",
    }
)
UE_ONLY_LICENSES = frozenset({"LicenseRef-UE-Only"})
SUPPORTED_LICENSES = OPEN_LICENSES | NC_LICENSES | UE_ONLY_LICENSES


class CatalogError(RuntimeError):
    """Base error for catalog operations."""


class CatalogValidationError(CatalogError, ValueError):
    """A catalog record or query violates the public contract."""


class CatalogConflictError(CatalogError):
    """A stable catalog identity conflicts with an existing record."""


class CatalogSchemaError(CatalogError):
    """The database schema cannot be read or migrated by this client."""


@dataclass(frozen=True)
class AssetUpsert:
    asset_id: str
    name: str
    source: str
    source_id: str
    source_url: str
    license: str
    license_tier: str
    license_url: str
    raw_path: str | Path
    sha256: str
    attribution: str = ""
    status: str = "raw"
    tags: tuple[str, ...] = ()
    ue_package_path: str | None = None
    tri_count: int | None = None
    material_count: int | None = None
    error: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    name: str
    source: str
    source_id: str
    source_url: str
    license: str
    license_tier: str
    license_url: str
    attribution: str
    status: str
    tags: tuple[str, ...]
    raw_path: str
    ue_package_path: str | None
    tri_count: int | None
    material_count: int | None
    sha256: str
    error: dict[str, Any] | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "name": self.name,
            "source": self.source,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "license": self.license,
            "license_tier": self.license_tier,
            "license_url": self.license_url,
            "attribution": self.attribution,
            "status": self.status,
            "tags": list(self.tags),
            "raw_path": self.raw_path,
            "ue_package_path": self.ue_package_path,
            "tri_count": self.tri_count,
            "material_count": self.material_count,
            "sha256": self.sha256,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ArtifactUpsert:
    artifact_id: str
    asset_id: str
    kind: str
    path: str | Path
    params: Mapping[str, Any] | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    asset_id: str
    kind: str
    path: str
    params: dict[str, Any]
    sha256: str | None
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "asset_id": self.asset_id,
            "kind": self.kind,
            "path": self.path,
            "params": self.params,
            "sha256": self.sha256,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ResourceUpsert:
    resource_id: str
    resource_kind: str
    profile: str
    resolution: str
    name: str
    source: str
    source_id: str
    source_url: str
    source_revision: str
    source_revision_scheme: str
    license: str
    license_tier: str
    license_url: str
    attribution: str = ""
    status: str = "verified"
    tags: tuple[str, ...] = ()
    bundle_sha256: str | None = None
    content_sha256: str | None = None
    physical_size_mm: tuple[float, float] | None = None
    error: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ResourceRecord:
    resource_id: str
    resource_kind: str
    profile: str
    resolution: str
    name: str
    source: str
    source_id: str
    source_url: str
    source_revision: str
    source_revision_scheme: str
    license: str
    license_tier: str
    license_url: str
    attribution: str
    status: str
    tags: tuple[str, ...]
    bundle_sha256: str | None
    content_sha256: str | None
    physical_size_mm: tuple[float, float] | None
    error: dict[str, Any] | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "resource_kind": self.resource_kind,
            "profile": self.profile,
            "resolution": self.resolution,
            "name": self.name,
            "source": self.source,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "source_revision": self.source_revision,
            "source_revision_scheme": self.source_revision_scheme,
            "license": self.license,
            "license_tier": self.license_tier,
            "license_url": self.license_url,
            "attribution": self.attribution,
            "status": self.status,
            "tags": list(self.tags),
            "bundle_sha256": self.bundle_sha256,
            "content_sha256": self.content_sha256,
            "physical_size_mm": (
                None if self.physical_size_mm is None else list(self.physical_size_mm)
            ),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ResourceFileUpsert:
    file_id: str
    resource_id: str
    semantic_role: str
    provider_role: str
    resolution: str
    format: str
    path: str | Path
    source_url: str
    byte_size: int
    sha256: str
    color_space: str
    provider_md5: str | None = None
    normal_convention: str | None = None
    channels: Mapping[str, str] | None = None
    width: int | None = None
    height: int | None = None
    is_primary: bool = False
    params: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ResourceFileRecord:
    file_id: str
    resource_id: str
    semantic_role: str
    provider_role: str
    resolution: str
    format: str
    path: str
    source_url: str
    byte_size: int
    provider_md5: str | None
    sha256: str
    color_space: str
    normal_convention: str | None
    channels: dict[str, str]
    width: int | None
    height: int | None
    is_primary: bool
    params: dict[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "resource_id": self.resource_id,
            "semantic_role": self.semantic_role,
            "provider_role": self.provider_role,
            "resolution": self.resolution,
            "format": self.format,
            "path": self.path,
            "source_url": self.source_url,
            "byte_size": self.byte_size,
            "provider_md5": self.provider_md5,
            "sha256": self.sha256,
            "color_space": self.color_space,
            "normal_convention": self.normal_convention,
            "channels": self.channels,
            "width": self.width,
            "height": self.height,
            "is_primary": self.is_primary,
            "params": self.params,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ResourceArtifactUpsert:
    artifact_id: str
    resource_id: str
    kind: str
    path: str | Path
    params: Mapping[str, Any] | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class ResourceArtifactRecord:
    artifact_id: str
    resource_id: str
    kind: str
    path: str
    params: dict[str, Any]
    sha256: str | None
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "resource_id": self.resource_id,
            "kind": self.kind,
            "path": self.path,
            "params": self.params,
            "sha256": self.sha256,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ResourceBindingUpsert:
    binding_id: str
    resource_id: str
    role: str
    asset_id: str | None = None
    scene_id: str | None = None
    consumer_resource_id: str | None = None
    params: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ResourceBindingRecord:
    binding_id: str
    resource_id: str
    role: str
    asset_id: str | None
    scene_id: str | None
    consumer_resource_id: str | None
    params: dict[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "resource_id": self.resource_id,
            "role": self.role,
            "asset_id": self.asset_id,
            "scene_id": self.scene_id,
            "consumer_resource_id": self.consumer_resource_id,
            "params": self.params,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ResourceCohort:
    resource: ResourceRecord
    files: tuple[ResourceFileRecord, ...]
    artifacts: tuple[ResourceArtifactRecord, ...]
    bindings: tuple[ResourceBindingRecord, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = self.resource.as_dict()
        payload["files"] = [item.as_dict() for item in self.files]
        payload["artifacts"] = [item.as_dict() for item in self.artifacts]
        payload["bindings"] = [item.as_dict() for item in self.bindings]
        return payload


@dataclass(frozen=True)
class SceneUpsert:
    scene_id: str
    name: str
    source: str
    source_id: str
    source_url: str
    license: str
    license_tier: str
    license_url: str
    source_path: str | Path
    source_sha256: str
    spec_sha256: str
    source_file: str | Path | None = None
    build_sha256: str | None = None
    attribution: str = ""
    status: str = "raw"
    map_path: str | None = None
    actor_count: int | None = None
    static_mesh_count: int | None = None
    triangle_count: int | None = None
    material_count: int | None = None
    texture_count: int | None = None
    bounds: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class SceneRecord:
    scene_id: str
    name: str
    source: str
    source_id: str
    source_url: str
    license: str
    license_tier: str
    license_url: str
    attribution: str
    source_path: str
    source_file: str | None
    source_sha256: str
    spec_sha256: str
    build_sha256: str | None
    status: str
    map_path: str | None
    actor_count: int | None
    static_mesh_count: int | None
    triangle_count: int | None
    material_count: int | None
    texture_count: int | None
    bounds: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "name": self.name,
            "source": self.source,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "license": self.license,
            "license_tier": self.license_tier,
            "license_url": self.license_url,
            "attribution": self.attribution,
            "source_path": self.source_path,
            "source_file": self.source_file,
            "source_sha256": self.source_sha256,
            "spec_sha256": self.spec_sha256,
            "build_sha256": self.build_sha256,
            "status": self.status,
            "map_path": self.map_path,
            "actor_count": self.actor_count,
            "static_mesh_count": self.static_mesh_count,
            "triangle_count": self.triangle_count,
            "material_count": self.material_count,
            "texture_count": self.texture_count,
            "bounds": self.bounds,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class SceneObjectUpsert:
    object_id: str
    scene_id: str
    actor_name: str
    actor_class: str
    transform: Mapping[str, Any]
    mesh_path: str | None = None
    bounds: Mapping[str, Any] | None = None
    triangle_count: int | None = None
    material_count: int | None = None


@dataclass(frozen=True)
class SceneObjectRecord:
    object_id: str
    scene_id: str
    actor_name: str
    actor_class: str
    mesh_path: str | None
    transform: dict[str, Any]
    bounds: dict[str, Any] | None
    triangle_count: int | None
    material_count: int | None
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "scene_id": self.scene_id,
            "actor_name": self.actor_name,
            "actor_class": self.actor_class,
            "mesh_path": self.mesh_path,
            "transform": self.transform,
            "bounds": self.bounds,
            "triangle_count": self.triangle_count,
            "material_count": self.material_count,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SceneArtifactUpsert:
    artifact_id: str
    scene_id: str
    kind: str
    path: str | Path
    params: Mapping[str, Any] | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class SceneArtifactRecord:
    artifact_id: str
    scene_id: str
    kind: str
    path: str
    params: dict[str, Any]
    sha256: str | None
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "scene_id": self.scene_id,
            "kind": self.kind,
            "path": self.path,
            "params": self.params,
            "sha256": self.sha256,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CatalogStats:
    total_assets: int
    total_artifacts: int
    by_status: dict[str, int]
    by_source: dict[str, int]
    by_license: dict[str, int]
    by_license_tier: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_assets": self.total_assets,
            "total_artifacts": self.total_artifacts,
            "by_status": self.by_status,
            "by_source": self.by_source,
            "by_license": self.by_license,
            "by_license_tier": self.by_license_tier,
        }


@dataclass(frozen=True)
class ResourceStats:
    total_resources: int
    total_files: int
    total_artifacts: int
    total_bindings: int
    by_kind: dict[str, int]
    by_status: dict[str, int]
    by_source: dict[str, int]
    by_license: dict[str, int]
    by_license_tier: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_resources": self.total_resources,
            "total_files": self.total_files,
            "total_artifacts": self.total_artifacts,
            "total_bindings": self.total_bindings,
            "by_kind": self.by_kind,
            "by_status": self.by_status,
            "by_source": self.by_source,
            "by_license": self.by_license,
            "by_license_tier": self.by_license_tier,
        }


class Catalog:
    """Versioned SQLite catalog with one connection per operation."""

    def __init__(
        self,
        database_path: Path,
        *,
        project_root: Path | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if busy_timeout_ms < 0:
            raise CatalogValidationError("busy_timeout_ms must be non-negative")
        self.database_path = database_path.expanduser().resolve()
        self.project_root = (project_root or self.database_path.parent).expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms

    def initialize(self) -> int:
        """Create or migrate the database and return its schema version."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise CatalogSchemaError(
                    f"catalog schema {version} is newer than supported version {SCHEMA_VERSION}"
                )
            while version < SCHEMA_VERSION:
                next_version = version + 1
                migration = _MIGRATIONS.get(next_version)
                if migration is None:
                    raise CatalogSchemaError(f"missing catalog migration {next_version}")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    locked_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if locked_version != version:
                        connection.rollback()
                        if locked_version > SCHEMA_VERSION:
                            raise CatalogSchemaError(
                                f"catalog schema {locked_version} is newer than supported version "
                                f"{SCHEMA_VERSION}"
                            )
                        version = locked_version
                        continue
                    for statement in migration:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {next_version}")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                version = next_version
            return version
        except sqlite3.DatabaseError as exc:
            raise CatalogSchemaError(f"could not initialize catalog: {exc}") from exc
        finally:
            connection.close()

    def schema_version(self) -> int:
        if not self.database_path.exists():
            return 0
        connection = self._connect()
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()

    def connection_settings(self) -> dict[str, int | str]:
        """Return effective SQLite safety settings for diagnostics and tests."""
        self.initialize()
        connection = self._connect()
        try:
            return {
                "foreign_keys": int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
                "busy_timeout_ms": int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
                "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
            }
        finally:
            connection.close()

    def preflight_write(self) -> int:
        """Migrate and prove that an immediate write transaction can be acquired."""

        version = self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("SELECT 1").fetchone()
            connection.rollback()
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise CatalogConflictError(f"catalog is not writable for finalization: {exc}") from exc
        finally:
            connection.close()
        return version

    def upsert_asset(self, asset: AssetUpsert) -> AssetRecord:
        return self.upsert_assets((asset,))[0]

    def upsert_assets(self, assets: Iterable[AssetUpsert]) -> tuple[AssetRecord, ...]:
        prepared = tuple(self._prepare_asset(asset) for asset in assets)
        if not prepared:
            return ()
        _reject_batch_duplicates(prepared, "asset_id")
        _reject_batch_duplicates(prepared, "sha256")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for values in prepared:
                _upsert_prepared_asset(connection, values)
            connection.commit()
        except CatalogConflictError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(f"asset upsert conflicts with catalog: {exc}") from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        records: list[AssetRecord] = []
        for values in prepared:
            record = self.get_asset(str(values["asset_id"]))
            if record is None:  # pragma: no cover - a committed insert must be visible
                raise CatalogError(f"asset disappeared after commit: {values['asset_id']}")
            records.append(record)
        return tuple(records)

    def upsert_artifact(self, artifact: ArtifactUpsert) -> ArtifactRecord:
        return self.upsert_artifacts((artifact,))[0]

    def upsert_artifacts(self, artifacts: Iterable[ArtifactUpsert]) -> tuple[ArtifactRecord, ...]:
        prepared = tuple(self._prepare_artifact(artifact) for artifact in artifacts)
        if not prepared:
            return ()
        _reject_batch_duplicates(prepared, "artifact_id")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for values in prepared:
                _upsert_prepared_artifact(connection, values)
            connection.commit()
        except CatalogConflictError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(f"artifact upsert conflicts with catalog: {exc}") from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        records: list[ArtifactRecord] = []
        for values in prepared:
            record = self.get_artifact(str(values["artifact_id"]))
            if record is None:  # pragma: no cover - a committed insert must be visible
                raise CatalogError(f"artifact disappeared after commit: {values['artifact_id']}")
            records.append(record)
        return tuple(records)

    def finalize_import(
        self,
        asset: AssetUpsert,
        artifact: ArtifactUpsert,
    ) -> tuple[AssetRecord, ArtifactRecord]:
        """Atomically commit an imported asset and its provenance artifact."""
        if asset.status not in {"imported", "render_ok"}:
            raise CatalogValidationError(
                "finalize_import requires asset status 'imported' or 'render_ok'"
            )
        if artifact.asset_id != asset.asset_id:
            raise CatalogValidationError(
                "finalize_import requires artifact.asset_id to match asset.asset_id"
            )
        prepared_asset = self._prepare_asset(asset)
        prepared_artifact = self._prepare_artifact(artifact)
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _upsert_prepared_asset(connection, prepared_asset)
            # A newly committed import supersedes every prior import/render
            # artifact for this logical asset. Keeping historical rows here
            # makes the active package ambiguous and can attach thumbnails to
            # a stale path-sensitive bundle identity.
            connection.execute(
                "DELETE FROM artifacts WHERE asset_id = ?",
                (asset.asset_id,),
            )
            _upsert_prepared_artifact(connection, prepared_artifact)
            connection.commit()
        except CatalogConflictError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(
                f"import finalization conflicts with catalog: {exc}"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        asset_record = self.get_asset(asset.asset_id)
        artifact_record = self.get_artifact(artifact.artifact_id)
        if asset_record is None or artifact_record is None:  # pragma: no cover
            raise CatalogError("finalized import disappeared after commit")
        return asset_record, artifact_record

    def finalize_render(
        self,
        asset: AssetUpsert,
        artifacts: Iterable[ArtifactUpsert],
    ) -> tuple[AssetRecord, tuple[ArtifactRecord, ...]]:
        """Atomically mark an asset render_ok and attach all render artifacts."""

        if asset.status != "render_ok":
            raise CatalogValidationError("finalize_render requires asset status 'render_ok'")
        artifact_items = tuple(artifacts)
        if not artifact_items:
            raise CatalogValidationError("finalize_render requires at least one artifact")
        if any(item.asset_id != asset.asset_id for item in artifact_items):
            raise CatalogValidationError(
                "finalize_render requires every artifact.asset_id to match asset.asset_id"
            )
        prepared_asset = self._prepare_asset(asset)
        prepared_artifacts = tuple(self._prepare_artifact(item) for item in artifact_items)
        _reject_batch_duplicates(prepared_artifacts, "artifact_id")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _upsert_prepared_asset(connection, prepared_asset)
            for values in prepared_artifacts:
                _upsert_prepared_artifact(connection, values)
            connection.commit()
        except CatalogConflictError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(
                f"render finalization conflicts with catalog: {exc}"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        asset_record = self.get_asset(asset.asset_id)
        artifact_records = tuple(
            record
            for item in artifact_items
            if (record := self.get_artifact(item.artifact_id)) is not None
        )
        if asset_record is None or len(artifact_records) != len(artifact_items):  # pragma: no cover
            raise CatalogError("finalized render disappeared after commit")
        return asset_record, artifact_records

    def get_asset(self, asset_id: str) -> AssetRecord | None:
        _validate_slug(asset_id, "asset_id")
        self.initialize()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            return None if row is None else _asset_from_row(row)
        finally:
            connection.close()

    def show_asset(self, asset_id: str) -> AssetRecord | None:
        """Return one asset by id; named for parity with the CLI command."""
        return self.get_asset(asset_id)

    def get_asset_by_sha256(self, sha256: str) -> AssetRecord | None:
        """Return the existing owner of a content hash, if any."""
        _validate_sha256(sha256, "sha256")
        self.initialize()
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM assets WHERE sha256 = ?", (sha256,)).fetchone()
            return None if row is None else _asset_from_row(row)
        finally:
            connection.close()

    def list_assets(
        self,
        *,
        asset_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        license: str | None = None,
        tag: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[AssetRecord, ...]:
        clauses: list[str] = []
        parameters: list[str | int] = []
        if asset_id is not None:
            _validate_slug(asset_id, "asset_id")
            clauses.append("assets.asset_id = ?")
            parameters.append(asset_id)
        if status is not None:
            _validate_status(status)
            clauses.append("assets.status = ?")
            parameters.append(status)
        if source is not None:
            _validate_slug(source, "source")
            clauses.append("assets.source = ?")
            parameters.append(source)
        if license is not None:
            _validate_license_name(license)
            clauses.append("assets.license = ?")
            parameters.append(license)
        if tag is not None:
            _validate_tag(tag)
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(assets.tags_json) WHERE json_each.value = ?)"
            )
            parameters.append(tag)
        if limit < 1 or limit > 10_000:
            raise CatalogValidationError("limit must be between 1 and 10000")
        if offset < 0:
            raise CatalogValidationError("offset must be non-negative")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))
        self.initialize()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT assets.* FROM assets{where} ORDER BY asset_id LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
            return tuple(_asset_from_row(row) for row in rows)
        finally:
            connection.close()

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        _validate_slug(artifact_id, "artifact_id")
        self.initialize()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return None if row is None else _artifact_from_row(row)
        finally:
            connection.close()

    def list_artifacts(self, *, asset_id: str | None = None) -> tuple[ArtifactRecord, ...]:
        parameters: tuple[str, ...] = ()
        where = ""
        if asset_id is not None:
            _validate_slug(asset_id, "asset_id")
            where = " WHERE asset_id = ?"
            parameters = (asset_id,)
        self.initialize()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM artifacts{where} ORDER BY artifact_id", parameters
            ).fetchall()
            return tuple(_artifact_from_row(row) for row in rows)
        finally:
            connection.close()

    def upsert_resource(self, resource: ResourceUpsert) -> ResourceRecord:
        return self.upsert_resources((resource,))[0]

    def upsert_resources(self, resources: Iterable[ResourceUpsert]) -> tuple[ResourceRecord, ...]:
        items = tuple(resources)
        if any(item.status in {"verified", "ready"} for item in items):
            raise CatalogValidationError(
                "verified and ready resources require finalize_resource with complete evidence"
            )
        prepared = tuple(self._prepare_resource(item) for item in items)
        if not prepared:
            return ()
        _reject_batch_duplicates(prepared, "resource_id")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for values in prepared:
                _upsert_prepared_resource(connection, values)
            connection.commit()
        except (CatalogConflictError, CatalogValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(f"resource upsert conflicts with catalog: {exc}") from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        records = tuple(self.get_resource(str(values["resource_id"])) for values in prepared)
        if any(record is None for record in records):  # pragma: no cover
            raise CatalogError("resource disappeared after commit")
        return tuple(record for record in records if record is not None)

    def finalize_resource(
        self,
        resource: ResourceUpsert,
        files: Iterable[ResourceFileUpsert],
        artifacts: Iterable[ResourceArtifactUpsert],
    ) -> tuple[
        ResourceRecord,
        tuple[ResourceFileRecord, ...],
        tuple[ResourceArtifactRecord, ...],
    ]:
        """Atomically publish a complete verified or consumer-ready resource package."""

        if resource.status not in {"verified", "ready"}:
            raise CatalogValidationError(
                "finalize_resource requires resource status 'verified' or 'ready'"
            )
        file_items = tuple(files)
        artifact_items = tuple(artifacts)
        if any(item.resource_id != resource.resource_id for item in file_items):
            raise CatalogValidationError(
                "finalize_resource requires every file.resource_id to match resource.resource_id"
            )
        if any(item.resource_id != resource.resource_id for item in artifact_items):
            raise CatalogValidationError(
                "finalize_resource requires every artifact.resource_id to match "
                "resource.resource_id"
            )
        prepared_resource = self._prepare_resource(resource)
        prepared_resource["published_once"] = 1
        prepared_files = tuple(self._prepare_resource_file(item) for item in file_items)
        prepared_artifacts = tuple(self._prepare_resource_artifact(item) for item in artifact_items)
        _reject_batch_duplicates(prepared_files, "file_id")
        _reject_batch_duplicate_fields(prepared_files, ("resource_id", "path"))
        _reject_batch_duplicates(prepared_artifacts, "artifact_id")
        _validate_resource_evidence(
            prepared_resource,
            prepared_files,
            prepared_artifacts,
        )
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM resources WHERE resource_id = ?", (resource.resource_id,)
            ).fetchone()
            if existing is not None and _resource_has_published_lineage(connection, existing):
                if existing["status"] == "verified" and resource.status == "ready":
                    _assert_verified_resource_upgrade(
                        connection,
                        existing=existing,
                        resource=prepared_resource,
                        files=prepared_files,
                        artifacts=prepared_artifacts,
                    )
                    _upsert_prepared_resource(connection, prepared_resource)
                    existing_artifact_ids = {
                        str(row["artifact_id"])
                        for row in connection.execute(
                            "SELECT artifact_id FROM resource_artifacts WHERE resource_id = ?",
                            (resource.resource_id,),
                        )
                    }
                    for values in prepared_artifacts:
                        if str(values["artifact_id"]) not in existing_artifact_ids:
                            _upsert_prepared_resource_artifact(
                                connection,
                                values,
                                allow_published_mutation=True,
                            )
                    connection.commit()
                elif existing["status"] in {"verified", "ready"}:
                    _assert_published_resource_cohort_unchanged(
                        connection,
                        existing=existing,
                        resource=prepared_resource,
                        files=prepared_files,
                        artifacts=prepared_artifacts,
                    )
                    connection.rollback()
                else:
                    raise CatalogConflictError(
                        "legacy published resource lineage cannot be republished"
                    )
            else:
                _upsert_prepared_resource(connection, prepared_resource)
                connection.execute(
                    "DELETE FROM resource_files WHERE resource_id = ?", (resource.resource_id,)
                )
                connection.execute(
                    "DELETE FROM resource_artifacts WHERE resource_id = ?",
                    (resource.resource_id,),
                )
                for values in prepared_files:
                    _upsert_prepared_resource_file(
                        connection, values, allow_published_mutation=True
                    )
                for values in prepared_artifacts:
                    _upsert_prepared_resource_artifact(
                        connection, values, allow_published_mutation=True
                    )
                connection.commit()
        except (CatalogConflictError, CatalogValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(
                f"resource finalization conflicts with catalog: {exc}"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        resource_record = self.get_resource(resource.resource_id)
        file_records = self.list_resource_files(resource_id=resource.resource_id)
        artifact_records = self.list_resource_artifacts(resource_id=resource.resource_id)
        if resource_record is None:  # pragma: no cover
            raise CatalogError("finalized resource disappeared after commit")
        return resource_record, file_records, artifact_records

    def upsert_resource_files(
        self, files: Iterable[ResourceFileUpsert]
    ) -> tuple[ResourceFileRecord, ...]:
        prepared = tuple(self._prepare_resource_file(item) for item in files)
        if not prepared:
            return ()
        _reject_batch_duplicates(prepared, "file_id")
        _reject_batch_duplicate_fields(prepared, ("resource_id", "path"))
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for values in prepared:
                _upsert_prepared_resource_file(connection, values)
            connection.commit()
        except CatalogConflictError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(
                f"resource file upsert conflicts with catalog: {exc}"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        records = tuple(self.get_resource_file(str(values["file_id"])) for values in prepared)
        if any(record is None for record in records):  # pragma: no cover
            raise CatalogError("resource file disappeared after commit")
        return tuple(record for record in records if record is not None)

    def upsert_resource_file(self, item: ResourceFileUpsert) -> ResourceFileRecord:
        return self.upsert_resource_files((item,))[0]

    def upsert_resource_artifacts(
        self, artifacts: Iterable[ResourceArtifactUpsert]
    ) -> tuple[ResourceArtifactRecord, ...]:
        prepared = tuple(self._prepare_resource_artifact(item) for item in artifacts)
        if not prepared:
            return ()
        _reject_batch_duplicates(prepared, "artifact_id")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for values in prepared:
                _upsert_prepared_resource_artifact(connection, values)
            connection.commit()
        except CatalogConflictError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(
                f"resource artifact upsert conflicts with catalog: {exc}"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        records = tuple(
            self.get_resource_artifact(str(values["artifact_id"])) for values in prepared
        )
        if any(record is None for record in records):  # pragma: no cover
            raise CatalogError("resource artifact disappeared after commit")
        return tuple(record for record in records if record is not None)

    def upsert_resource_artifact(self, item: ResourceArtifactUpsert) -> ResourceArtifactRecord:
        return self.upsert_resource_artifacts((item,))[0]

    def upsert_resource_bindings(
        self, bindings: Iterable[ResourceBindingUpsert]
    ) -> tuple[ResourceBindingRecord, ...]:
        prepared = tuple(self._prepare_resource_binding(item) for item in bindings)
        if not prepared:
            return ()
        _reject_batch_duplicates(prepared, "binding_id")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for values in prepared:
                _upsert_prepared_resource_binding(connection, values)
            connection.commit()
        except CatalogConflictError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(
                f"resource binding upsert conflicts with catalog: {exc}"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        records = tuple(self.get_resource_binding(str(values["binding_id"])) for values in prepared)
        if any(record is None for record in records):  # pragma: no cover
            raise CatalogError("resource binding disappeared after commit")
        return tuple(record for record in records if record is not None)

    def upsert_resource_binding(self, item: ResourceBindingUpsert) -> ResourceBindingRecord:
        return self.upsert_resource_bindings((item,))[0]

    def get_resource(self, resource_id: str) -> ResourceRecord | None:
        _validate_slug(resource_id, "resource_id")
        self.initialize()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM resources WHERE resource_id = ?", (resource_id,)
            ).fetchone()
            return None if row is None else _resource_from_row(row)
        finally:
            connection.close()

    def show_resource(self, resource_id: str) -> ResourceRecord | None:
        return self.get_resource(resource_id)

    def get_resource_cohort(self, resource_id: str) -> ResourceCohort | None:
        """Read one resource and every child row from one SQLite snapshot."""

        _validate_slug(resource_id, "resource_id")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            resource_row = connection.execute(
                "SELECT * FROM resources WHERE resource_id = ?", (resource_id,)
            ).fetchone()
            if resource_row is None:
                connection.commit()
                return None
            file_rows = connection.execute(
                "SELECT * FROM resource_files WHERE resource_id = ? ORDER BY file_id",
                (resource_id,),
            ).fetchall()
            artifact_rows = connection.execute(
                "SELECT * FROM resource_artifacts WHERE resource_id = ? ORDER BY artifact_id",
                (resource_id,),
            ).fetchall()
            binding_rows = connection.execute(
                "SELECT * FROM resource_bindings WHERE resource_id = ? ORDER BY binding_id",
                (resource_id,),
            ).fetchall()
            cohort = ResourceCohort(
                resource=_resource_from_row(resource_row),
                files=tuple(_resource_file_from_row(row) for row in file_rows),
                artifacts=tuple(_resource_artifact_from_row(row) for row in artifact_rows),
                bindings=tuple(_resource_binding_from_row(row) for row in binding_rows),
            )
            connection.commit()
            return cohort
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_resources(
        self,
        *,
        resource_id: str | None = None,
        resource_kind: str | None = None,
        profile: str | None = None,
        resolution: str | None = None,
        status: str | None = None,
        source: str | None = None,
        license: str | None = None,
        license_tier: str | None = None,
        tag: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[ResourceRecord, ...]:
        clauses: list[str] = []
        parameters: list[str | int] = []
        if resource_id is not None:
            _validate_slug(resource_id, "resource_id")
            clauses.append("resources.resource_id = ?")
            parameters.append(resource_id)
        if resource_kind is not None:
            _validate_resource_kind(resource_kind)
            clauses.append("resources.resource_kind = ?")
            parameters.append(resource_kind)
        if profile is not None:
            _validate_slug(profile, "profile")
            clauses.append("resources.profile = ?")
            parameters.append(profile)
        if resolution is not None:
            _validate_resource_token(resolution, "resolution")
            clauses.append("resources.resolution = ?")
            parameters.append(resolution)
        if status is not None:
            _validate_resource_status(status)
            clauses.append("resources.status = ?")
            parameters.append(status)
        if source is not None:
            _validate_slug(source, "source")
            clauses.append("resources.source = ?")
            parameters.append(source)
        if license is not None:
            _validate_license_name(license)
            clauses.append("resources.license = ?")
            parameters.append(license)
        if license_tier is not None:
            if license_tier not in LICENSE_TIERS:
                allowed = ", ".join(sorted(LICENSE_TIERS))
                raise CatalogValidationError(f"license_tier must be one of: {allowed}")
            clauses.append("resources.license_tier = ?")
            parameters.append(license_tier)
        if tag is not None:
            _validate_tag(tag)
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(resources.tags_json) WHERE json_each.value = ?)"
            )
            parameters.append(tag)
        _validate_page(limit=limit, offset=offset)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))
        self.initialize()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT resources.* FROM resources{where} ORDER BY resource_id LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
            return tuple(_resource_from_row(row) for row in rows)
        finally:
            connection.close()

    def get_resource_file(self, file_id: str) -> ResourceFileRecord | None:
        _validate_slug(file_id, "file_id")
        self.initialize()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM resource_files WHERE file_id = ?", (file_id,)
            ).fetchone()
            return None if row is None else _resource_file_from_row(row)
        finally:
            connection.close()

    def list_resource_files(
        self,
        *,
        resource_id: str | None = None,
        semantic_role: str | None = None,
    ) -> tuple[ResourceFileRecord, ...]:
        clauses: list[str] = []
        parameters: list[str] = []
        if resource_id is not None:
            _validate_slug(resource_id, "resource_id")
            clauses.append("resource_id = ?")
            parameters.append(resource_id)
        if semantic_role is not None:
            _validate_slug(semantic_role, "semantic_role")
            clauses.append("semantic_role = ?")
            parameters.append(semantic_role)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        self.initialize()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM resource_files{where} ORDER BY file_id", parameters
            ).fetchall()
            return tuple(_resource_file_from_row(row) for row in rows)
        finally:
            connection.close()

    def get_resource_artifact(self, artifact_id: str) -> ResourceArtifactRecord | None:
        _validate_slug(artifact_id, "artifact_id")
        self.initialize()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM resource_artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return None if row is None else _resource_artifact_from_row(row)
        finally:
            connection.close()

    def list_resource_artifacts(
        self, *, resource_id: str | None = None, kind: str | None = None
    ) -> tuple[ResourceArtifactRecord, ...]:
        clauses: list[str] = []
        parameters: list[str] = []
        if resource_id is not None:
            _validate_slug(resource_id, "resource_id")
            clauses.append("resource_id = ?")
            parameters.append(resource_id)
        if kind is not None:
            _validate_slug(kind, "kind")
            clauses.append("kind = ?")
            parameters.append(kind)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        self.initialize()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM resource_artifacts{where} ORDER BY artifact_id", parameters
            ).fetchall()
            return tuple(_resource_artifact_from_row(row) for row in rows)
        finally:
            connection.close()

    def get_resource_binding(self, binding_id: str) -> ResourceBindingRecord | None:
        _validate_slug(binding_id, "binding_id")
        self.initialize()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM resource_bindings WHERE binding_id = ?", (binding_id,)
            ).fetchone()
            return None if row is None else _resource_binding_from_row(row)
        finally:
            connection.close()

    def list_resource_bindings(
        self,
        *,
        resource_id: str | None = None,
        asset_id: str | None = None,
        scene_id: str | None = None,
        consumer_resource_id: str | None = None,
    ) -> tuple[ResourceBindingRecord, ...]:
        clauses: list[str] = []
        parameters: list[str] = []
        for field, value in (
            ("resource_id", resource_id),
            ("asset_id", asset_id),
            ("scene_id", scene_id),
            ("consumer_resource_id", consumer_resource_id),
        ):
            if value is not None:
                _validate_slug(value, field)
                clauses.append(f"{field} = ?")
                parameters.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        self.initialize()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM resource_bindings{where} ORDER BY binding_id", parameters
            ).fetchall()
            return tuple(_resource_binding_from_row(row) for row in rows)
        finally:
            connection.close()

    def upsert_scene(self, scene: SceneUpsert) -> SceneRecord:
        return self.upsert_scenes((scene,))[0]

    def upsert_scenes(self, scenes: Iterable[SceneUpsert]) -> tuple[SceneRecord, ...]:
        prepared = tuple(self._prepare_scene(scene) for scene in scenes)
        if not prepared:
            return ()
        _reject_batch_duplicates(prepared, "scene_id")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for values in prepared:
                _upsert_prepared_scene(connection, values)
            connection.commit()
        except CatalogConflictError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(f"scene upsert conflicts with catalog: {exc}") from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        records = tuple(self.get_scene(str(values["scene_id"])) for values in prepared)
        if any(record is None for record in records):  # pragma: no cover
            raise CatalogError("scene disappeared after commit")
        return tuple(record for record in records if record is not None)

    def upsert_scene_objects(
        self, objects: Iterable[SceneObjectUpsert]
    ) -> tuple[SceneObjectRecord, ...]:
        prepared = tuple(self._prepare_scene_object(item) for item in objects)
        if not prepared:
            return ()
        _reject_batch_duplicates(prepared, "object_id")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for values in prepared:
                _upsert_prepared_scene_object(connection, values)
            connection.commit()
        except CatalogConflictError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(
                f"scene object upsert conflicts with catalog: {exc}"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        records = tuple(self.get_scene_object(str(values["object_id"])) for values in prepared)
        if any(record is None for record in records):  # pragma: no cover
            raise CatalogError("scene object disappeared after commit")
        return tuple(record for record in records if record is not None)

    def upsert_scene_artifacts(
        self, artifacts: Iterable[SceneArtifactUpsert]
    ) -> tuple[SceneArtifactRecord, ...]:
        prepared = tuple(self._prepare_scene_artifact(item) for item in artifacts)
        if not prepared:
            return ()
        _reject_batch_duplicates(prepared, "artifact_id")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for values in prepared:
                _upsert_prepared_scene_artifact(connection, values)
            connection.commit()
        except CatalogConflictError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(
                f"scene artifact upsert conflicts with catalog: {exc}"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        records = tuple(self.get_scene_artifact(str(values["artifact_id"])) for values in prepared)
        if any(record is None for record in records):  # pragma: no cover
            raise CatalogError("scene artifact disappeared after commit")
        return tuple(record for record in records if record is not None)

    def upsert_scene_object(self, item: SceneObjectUpsert) -> SceneObjectRecord:
        return self.upsert_scene_objects((item,))[0]

    def upsert_scene_artifact(self, item: SceneArtifactUpsert) -> SceneArtifactRecord:
        return self.upsert_scene_artifacts((item,))[0]

    def finalize_scene_build(
        self,
        scene: SceneUpsert,
        objects: Iterable[SceneObjectUpsert],
        artifacts: Iterable[SceneArtifactUpsert],
    ) -> tuple[SceneRecord, tuple[SceneObjectRecord, ...], tuple[SceneArtifactRecord, ...]]:
        """Atomically publish a built map and replace its complete build inventory."""
        prepared_scene, prepared_objects, prepared_artifacts = self._prepare_scene_build(
            scene, objects, artifacts
        )
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _write_prepared_scene_build(
                connection,
                scene_id=scene.scene_id,
                scene=prepared_scene,
                objects=prepared_objects,
                artifacts=prepared_artifacts,
            )
            connection.commit()
        except (CatalogConflictError, CatalogValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(
                f"scene build finalization conflicts with catalog: {exc}"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        scene_record = self.get_scene(scene.scene_id)
        object_records = self.list_scene_objects(scene_id=scene.scene_id)
        artifact_records = self.list_scene_artifacts(scene_id=scene.scene_id)
        if scene_record is None:  # pragma: no cover
            raise CatalogError("finalized scene build disappeared after commit")
        return scene_record, object_records, artifact_records

    def validate_scene_build(
        self,
        scene: SceneUpsert,
        objects: Iterable[SceneObjectUpsert],
        artifacts: Iterable[SceneArtifactUpsert],
    ) -> None:
        """Prove that a complete scene build can be committed, then roll it back.

        This performs the same SQL writes as :meth:`finalize_scene_build` inside an
        immediate transaction.  Scene executors use it while the UE backup still
        exists, so catalog/schema/provenance failures remain safely reversible.
        """

        prepared_scene, prepared_objects, prepared_artifacts = self._prepare_scene_build(
            scene, objects, artifacts
        )
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _write_prepared_scene_build(
                connection,
                scene_id=scene.scene_id,
                scene=prepared_scene,
                objects=prepared_objects,
                artifacts=prepared_artifacts,
            )
            connection.rollback()
        except (CatalogConflictError, CatalogValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(
                f"scene build preflight conflicts with catalog: {exc}"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _prepare_scene_build(
        self,
        scene: SceneUpsert,
        objects: Iterable[SceneObjectUpsert],
        artifacts: Iterable[SceneArtifactUpsert],
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
        if scene.status != "built":
            raise CatalogValidationError("finalize_scene_build requires scene status 'built'")
        object_items = tuple(objects)
        artifact_items = tuple(artifacts)
        if not object_items:
            raise CatalogValidationError("finalize_scene_build requires at least one object")
        if not artifact_items:
            raise CatalogValidationError("finalize_scene_build requires at least one artifact")
        if any(item.scene_id != scene.scene_id for item in object_items):
            raise CatalogValidationError(
                "finalize_scene_build requires every object.scene_id to match scene.scene_id"
            )
        if any(item.scene_id != scene.scene_id for item in artifact_items):
            raise CatalogValidationError(
                "finalize_scene_build requires every artifact.scene_id to match scene.scene_id"
            )
        artifact_kinds = {item.kind for item in artifact_items}
        if artifact_kinds != SCENE_BUILD_ARTIFACT_KINDS or len(artifact_items) != len(
            SCENE_BUILD_ARTIFACT_KINDS
        ):
            raise CatalogValidationError(
                "finalize_scene_build requires exactly one complete scene build artifact cohort"
            )
        if scene.build_sha256 is None:
            raise CatalogValidationError("finalize_scene_build requires build_sha256")
        for item in artifact_items:
            if item.sha256 is None:
                raise CatalogValidationError("scene build artifacts require sha256")
            params = item.params
            if not isinstance(params, Mapping) or params.get("build_sha256") != (
                scene.build_sha256
            ):
                raise CatalogValidationError(
                    "scene build artifact params must match the active build_sha256"
                )
        build_manifest = next(
            item for item in artifact_items if item.kind == "scene_build_manifest"
        )
        if build_manifest.sha256 != scene.build_sha256:
            raise CatalogValidationError(
                "scene build manifest sha256 must equal scene build_sha256"
            )
        prepared_scene = self._prepare_scene(scene)
        prepared_objects = tuple(self._prepare_scene_object(item) for item in object_items)
        prepared_artifacts = tuple(self._prepare_scene_artifact(item) for item in artifact_items)
        _reject_batch_duplicates(prepared_objects, "object_id")
        _reject_batch_duplicates(prepared_artifacts, "artifact_id")
        if scene.actor_count != len(prepared_objects):
            raise CatalogValidationError(
                "scene actor_count must equal the complete scene object inventory"
            )
        mesh_count = len(
            {
                str(values["mesh_path"])
                for values in prepared_objects
                if values["mesh_path"] is not None
            }
        )
        if scene.static_mesh_count != mesh_count:
            raise CatalogValidationError(
                "scene static_mesh_count must equal distinct object mesh paths"
            )
        return prepared_scene, prepared_objects, prepared_artifacts

    def finalize_scene_render(
        self,
        scene: SceneUpsert,
        artifacts: Iterable[SceneArtifactUpsert],
    ) -> tuple[SceneRecord, tuple[SceneArtifactRecord, ...]]:
        """Atomically mark a built scene rendered and replace incoming artifact kinds."""
        if scene.status != "render_ok":
            raise CatalogValidationError("finalize_scene_render requires scene status 'render_ok'")
        artifact_items = tuple(artifacts)
        if not artifact_items:
            raise CatalogValidationError("finalize_scene_render requires at least one artifact")
        if any(item.scene_id != scene.scene_id for item in artifact_items):
            raise CatalogValidationError(
                "finalize_scene_render requires every artifact.scene_id to match scene.scene_id"
            )
        artifact_kinds = {item.kind for item in artifact_items}
        if artifact_kinds != SCENE_RENDER_ARTIFACT_KINDS:
            raise CatalogValidationError(
                "finalize_scene_render requires the complete scene thumbnail artifact cohort"
            )
        if scene.build_sha256 is None:
            raise CatalogValidationError("finalize_scene_render requires build_sha256")
        for item in artifact_items:
            if item.sha256 is None:
                raise CatalogValidationError("scene render artifacts require sha256")
            params = item.params
            if not isinstance(params, Mapping) or params.get("build_sha256") != (
                scene.build_sha256
            ):
                raise CatalogValidationError(
                    "scene render artifact params must match the active build_sha256"
                )
        prepared_scene = self._prepare_scene(scene)
        prepared_artifacts = tuple(self._prepare_scene_artifact(item) for item in artifact_items)
        _reject_batch_duplicates(prepared_artifacts, "artifact_id")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM scenes WHERE scene_id = ?", (scene.scene_id,)
            ).fetchone()
            if existing is None or existing["status"] not in {"built", "render_ok"}:
                raise CatalogValidationError(
                    "finalize_scene_render requires an existing built scene"
                )
            _validate_render_scene_unchanged(existing, prepared_scene)
            _upsert_prepared_scene(connection, prepared_scene)
            kinds = sorted({str(values["kind"]) for values in prepared_artifacts})
            placeholders = ", ".join("?" for _ in kinds)
            connection.execute(
                f"DELETE FROM scene_artifacts WHERE scene_id = ? AND kind IN ({placeholders})",
                (scene.scene_id, *kinds),
            )
            for values in prepared_artifacts:
                _upsert_prepared_scene_artifact(connection, values)
            connection.commit()
        except (CatalogConflictError, CatalogValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise CatalogConflictError(
                f"scene render finalization conflicts with catalog: {exc}"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        scene_record = self.get_scene(scene.scene_id)
        artifact_records = tuple(
            record
            for item in artifact_items
            if (record := self.get_scene_artifact(item.artifact_id)) is not None
        )
        if scene_record is None or len(artifact_records) != len(artifact_items):  # pragma: no cover
            raise CatalogError("finalized scene render disappeared after commit")
        return scene_record, artifact_records

    def get_scene(self, scene_id: str) -> SceneRecord | None:
        _validate_slug(scene_id, "scene_id")
        self.initialize()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM scenes WHERE scene_id = ?", (scene_id,)
            ).fetchone()
            return None if row is None else _scene_from_row(row)
        finally:
            connection.close()

    def show_scene(self, scene_id: str) -> SceneRecord | None:
        """Return one scene by id; named for parity with the asset API."""
        return self.get_scene(scene_id)

    def list_scenes(
        self,
        *,
        scene_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        license: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[SceneRecord, ...]:
        clauses: list[str] = []
        parameters: list[str | int] = []
        if scene_id is not None:
            _validate_slug(scene_id, "scene_id")
            clauses.append("scene_id = ?")
            parameters.append(scene_id)
        if status is not None:
            _validate_scene_status(status)
            clauses.append("status = ?")
            parameters.append(status)
        if source is not None:
            _validate_slug(source, "source")
            clauses.append("source = ?")
            parameters.append(source)
        if license is not None:
            _validate_license_name(license)
            clauses.append("license = ?")
            parameters.append(license)
        if limit < 1 or limit > 10_000:
            raise CatalogValidationError("limit must be between 1 and 10000")
        if offset < 0:
            raise CatalogValidationError("offset must be non-negative")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))
        self.initialize()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM scenes{where} ORDER BY scene_id LIMIT ? OFFSET ?", parameters
            ).fetchall()
            return tuple(_scene_from_row(row) for row in rows)
        finally:
            connection.close()

    def get_scene_object(self, object_id: str) -> SceneObjectRecord | None:
        _validate_slug(object_id, "object_id")
        self.initialize()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM scene_objects WHERE object_id = ?", (object_id,)
            ).fetchone()
            return None if row is None else _scene_object_from_row(row)
        finally:
            connection.close()

    def list_scene_objects(self, *, scene_id: str | None = None) -> tuple[SceneObjectRecord, ...]:
        where = ""
        parameters: tuple[str, ...] = ()
        if scene_id is not None:
            _validate_slug(scene_id, "scene_id")
            where = " WHERE scene_id = ?"
            parameters = (scene_id,)
        self.initialize()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM scene_objects{where} ORDER BY object_id", parameters
            ).fetchall()
            return tuple(_scene_object_from_row(row) for row in rows)
        finally:
            connection.close()

    def get_scene_artifact(self, artifact_id: str) -> SceneArtifactRecord | None:
        _validate_slug(artifact_id, "artifact_id")
        self.initialize()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM scene_artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return None if row is None else _scene_artifact_from_row(row)
        finally:
            connection.close()

    def list_scene_artifacts(
        self, *, scene_id: str | None = None, kind: str | None = None
    ) -> tuple[SceneArtifactRecord, ...]:
        clauses: list[str] = []
        parameters: list[str] = []
        if scene_id is not None:
            _validate_slug(scene_id, "scene_id")
            clauses.append("scene_id = ?")
            parameters.append(scene_id)
        if kind is not None:
            _validate_slug(kind, "kind")
            clauses.append("kind = ?")
            parameters.append(kind)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        self.initialize()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM scene_artifacts{where} ORDER BY artifact_id", parameters
            ).fetchall()
            return tuple(_scene_artifact_from_row(row) for row in rows)
        finally:
            connection.close()

    def stats(self) -> CatalogStats:
        self.initialize()
        connection = self._connect()
        try:
            return CatalogStats(
                total_assets=int(connection.execute("SELECT count(*) FROM assets").fetchone()[0]),
                total_artifacts=int(
                    connection.execute("SELECT count(*) FROM artifacts").fetchone()[0]
                ),
                by_status=_group_counts(connection, "status"),
                by_source=_group_counts(connection, "source"),
                by_license=_group_counts(connection, "license"),
                by_license_tier=_group_counts(connection, "license_tier"),
            )
        finally:
            connection.close()

    def resource_stats(self) -> ResourceStats:
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            result = ResourceStats(
                total_resources=int(
                    connection.execute("SELECT count(*) FROM resources").fetchone()[0]
                ),
                total_files=int(
                    connection.execute("SELECT count(*) FROM resource_files").fetchone()[0]
                ),
                total_artifacts=int(
                    connection.execute("SELECT count(*) FROM resource_artifacts").fetchone()[0]
                ),
                total_bindings=int(
                    connection.execute("SELECT count(*) FROM resource_bindings").fetchone()[0]
                ),
                by_kind=_resource_group_counts(connection, "resource_kind"),
                by_status=_resource_group_counts(connection, "status"),
                by_source=_resource_group_counts(connection, "source"),
                by_license=_resource_group_counts(connection, "license"),
                by_license_tier=_resource_group_counts(connection, "license_tier"),
            )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        deadline = time.monotonic() + self.busy_timeout_ms / 1_000
        while True:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    connection.close()
                    raise
                time.sleep(0.01)
        return connection

    def _prepare_asset(self, asset: AssetUpsert) -> dict[str, Any]:
        _validate_slug(asset.asset_id, "asset_id")
        _validate_text(asset.name, "name")
        _validate_slug(asset.source, "source")
        _validate_text(asset.source_id, "source_id")
        _validate_uri(asset.source_url, "source_url")
        _validate_license(asset.license, asset.license_tier)
        _validate_uri(asset.license_url, "license_url")
        if not isinstance(asset.attribution, str) or asset.attribution != asset.attribution.strip():
            raise CatalogValidationError("attribution must be text without surrounding whitespace")
        _reject_control_characters(asset.attribution, "attribution")
        _validate_status(asset.status)
        _validate_sha256(asset.sha256, "sha256")
        raw_path = _relative_path(asset.raw_path, self.project_root, "raw_path")
        tags = _normalize_tags(asset.tags)
        ue_package_path = _validate_ue_package_path(asset.ue_package_path)
        _validate_count(asset.tri_count, "tri_count")
        _validate_count(asset.material_count, "material_count")
        error_json = _json_object(asset.error, "error") if asset.error is not None else None
        _validate_status_payload(
            status=asset.status,
            ue_package_path=ue_package_path,
            tri_count=asset.tri_count,
            material_count=asset.material_count,
            error=asset.error,
        )
        now = _utc_now()
        return {
            "asset_id": asset.asset_id,
            "name": asset.name.strip(),
            "source": asset.source,
            "source_id": asset.source_id.strip(),
            "source_url": asset.source_url.strip(),
            "license": asset.license,
            "license_tier": asset.license_tier,
            "license_url": asset.license_url.strip(),
            "attribution": asset.attribution.strip(),
            "status": asset.status,
            "tags_json": json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
            "raw_path": raw_path,
            "ue_package_path": ue_package_path,
            "tri_count": asset.tri_count,
            "material_count": asset.material_count,
            "sha256": asset.sha256,
            "error_json": error_json,
            "created_at": now,
            "updated_at": now,
        }

    def _prepare_artifact(self, artifact: ArtifactUpsert) -> dict[str, Any]:
        _validate_slug(artifact.artifact_id, "artifact_id")
        _validate_slug(artifact.asset_id, "asset_id")
        _validate_slug(artifact.kind, "kind")
        path = _relative_path(artifact.path, self.project_root, "path")
        params_json = _json_object(artifact.params or {}, "params")
        if artifact.sha256 is not None:
            _validate_sha256(artifact.sha256, "sha256")
        return {
            "artifact_id": artifact.artifact_id,
            "asset_id": artifact.asset_id,
            "kind": artifact.kind,
            "path": path,
            "params_json": params_json,
            "sha256": artifact.sha256,
            "created_at": _utc_now(),
        }

    def _prepare_resource(self, resource: ResourceUpsert) -> dict[str, Any]:
        _validate_slug(resource.resource_id, "resource_id")
        _validate_resource_kind(resource.resource_kind)
        _validate_slug(resource.profile, "profile")
        if resource.profile not in RESOURCE_PROFILES[resource.resource_kind]:
            allowed = ", ".join(sorted(RESOURCE_PROFILES[resource.resource_kind]))
            raise CatalogValidationError(
                f"profile for {resource.resource_kind!r} must be one of: {allowed}"
            )
        _validate_resource_token(resource.resolution, "resolution")
        _validate_text(resource.name, "name")
        _validate_slug(resource.source, "source")
        _validate_text(resource.source_id, "source_id")
        _validate_uri(resource.source_url, "source_url")
        _validate_text(resource.source_revision, "source_revision")
        if len(resource.source_revision) > 128:
            raise CatalogValidationError("source_revision must contain at most 128 characters")
        _validate_slug(resource.source_revision_scheme, "source_revision_scheme")
        _validate_license(resource.license, resource.license_tier)
        _validate_uri(resource.license_url, "license_url")
        if (
            not isinstance(resource.attribution, str)
            or resource.attribution != resource.attribution.strip()
        ):
            raise CatalogValidationError("attribution must be text without surrounding whitespace")
        _reject_control_characters(resource.attribution, "attribution")
        _validate_resource_status(resource.status)
        tags = _normalize_tags(resource.tags)
        if resource.bundle_sha256 is not None:
            _validate_sha256(resource.bundle_sha256, "bundle_sha256")
        if resource.content_sha256 is not None:
            _validate_sha256(resource.content_sha256, "content_sha256")
        physical_width_mm: float | None = None
        physical_height_mm: float | None = None
        if resource.physical_size_mm is not None:
            if (
                not isinstance(resource.physical_size_mm, tuple | list)
                or len(resource.physical_size_mm) != 2
            ):
                raise CatalogValidationError("physical_size_mm must contain exactly two numbers")
            width, height = resource.physical_size_mm
            if any(
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or float(value) <= 0.0
                for value in (width, height)
            ):
                raise CatalogValidationError("physical_size_mm values must be finite and positive")
            physical_width_mm = float(width)
            physical_height_mm = float(height)
        error_json = (
            _json_object(resource.error, "error", require_nonempty=True)
            if resource.error is not None
            else None
        )
        _validate_resource_status_payload(
            resource_kind=resource.resource_kind,
            status=resource.status,
            bundle_sha256=resource.bundle_sha256,
            content_sha256=resource.content_sha256,
            physical_size_mm=resource.physical_size_mm,
            error=resource.error,
        )
        now = _utc_now()
        return {
            "resource_id": resource.resource_id,
            "resource_kind": resource.resource_kind,
            "profile": resource.profile,
            "resolution": resource.resolution,
            "name": resource.name.strip(),
            "source": resource.source,
            "source_id": resource.source_id.strip(),
            "source_url": resource.source_url.strip(),
            "source_revision": resource.source_revision,
            "source_revision_scheme": resource.source_revision_scheme,
            "license": resource.license,
            "license_tier": resource.license_tier,
            "license_url": resource.license_url.strip(),
            "attribution": resource.attribution,
            "status": resource.status,
            "tags_json": json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
            "bundle_sha256": resource.bundle_sha256,
            "content_sha256": resource.content_sha256,
            "physical_width_mm": physical_width_mm,
            "physical_height_mm": physical_height_mm,
            "error_json": error_json,
            "created_at": now,
            "updated_at": now,
            "published_once": 0,
        }

    def _prepare_resource_file(self, item: ResourceFileUpsert) -> dict[str, Any]:
        _validate_slug(item.file_id, "file_id")
        _validate_slug(item.resource_id, "resource_id")
        _validate_slug(item.semantic_role, "semantic_role")
        _validate_text(item.provider_role, "provider_role")
        if len(item.provider_role) > 128:
            raise CatalogValidationError("provider_role must contain at most 128 characters")
        _validate_resource_token(item.resolution, "resolution")
        _validate_slug(item.format, "format")
        path = _relative_path(item.path, self.project_root, "path")
        _validate_uri(item.source_url, "source_url")
        if (
            isinstance(item.byte_size, bool)
            or not isinstance(item.byte_size, int)
            or item.byte_size <= 0
        ):
            raise CatalogValidationError("byte_size must be a positive integer")
        if item.provider_md5 is not None and (
            not isinstance(item.provider_md5, str)
            or len(item.provider_md5) != 32
            or any(character not in "0123456789abcdef" for character in item.provider_md5)
        ):
            raise CatalogValidationError(
                "provider_md5 must be a lowercase 32-character MD5 or null"
            )
        _validate_sha256(item.sha256, "sha256")
        if item.color_space not in RESOURCE_COLOR_SPACES:
            allowed = ", ".join(sorted(RESOURCE_COLOR_SPACES))
            raise CatalogValidationError(f"color_space must be one of: {allowed}")
        if (
            item.normal_convention is not None
            and item.normal_convention not in RESOURCE_NORMAL_CONVENTIONS
        ):
            allowed = ", ".join(sorted(RESOURCE_NORMAL_CONVENTIONS))
            raise CatalogValidationError(f"normal_convention must be null or one of: {allowed}")
        if (item.width is None) != (item.height is None):
            raise CatalogValidationError("width and height must be provided together")
        for value, field in ((item.width, "width"), (item.height, "height")):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise CatalogValidationError(f"{field} must be a positive integer or null")
        if not isinstance(item.is_primary, bool):
            raise CatalogValidationError("is_primary must be a boolean")
        channels = {} if item.channels is None else item.channels
        if not isinstance(channels, Mapping):
            raise CatalogValidationError("channels must be a JSON object")
        for channel, semantic in channels.items():
            if channel not in RESOURCE_CHANNELS:
                allowed = ", ".join(sorted(RESOURCE_CHANNELS))
                raise CatalogValidationError(f"channels keys must be one of: {allowed}")
            _validate_slug(semantic, f"channels[{channel!r}]")
        channels_json = _json_object(channels, "channels")
        params_json = _json_object({} if item.params is None else item.params, "params")
        return {
            "file_id": item.file_id,
            "resource_id": item.resource_id,
            "semantic_role": item.semantic_role,
            "provider_role": item.provider_role,
            "resolution": item.resolution,
            "format": item.format,
            "path": path,
            "source_url": item.source_url.strip(),
            "byte_size": item.byte_size,
            "provider_md5": item.provider_md5,
            "sha256": item.sha256,
            "color_space": item.color_space,
            "normal_convention": item.normal_convention,
            "channels_json": channels_json,
            "width": item.width,
            "height": item.height,
            "is_primary": int(item.is_primary),
            "params_json": params_json,
            "created_at": _utc_now(),
        }

    def _prepare_resource_artifact(self, item: ResourceArtifactUpsert) -> dict[str, Any]:
        _validate_slug(item.artifact_id, "artifact_id")
        _validate_slug(item.resource_id, "resource_id")
        _validate_slug(item.kind, "kind")
        path = _relative_path(item.path, self.project_root, "path")
        params_json = _json_object({} if item.params is None else item.params, "params")
        if item.sha256 is not None:
            _validate_sha256(item.sha256, "sha256")
        return {
            "artifact_id": item.artifact_id,
            "resource_id": item.resource_id,
            "kind": item.kind,
            "path": path,
            "params_json": params_json,
            "sha256": item.sha256,
            "created_at": _utc_now(),
        }

    def _prepare_resource_binding(self, item: ResourceBindingUpsert) -> dict[str, Any]:
        _validate_slug(item.binding_id, "binding_id")
        _validate_slug(item.resource_id, "resource_id")
        _validate_slug(item.role, "role")
        consumers = (item.asset_id, item.scene_id, item.consumer_resource_id)
        if sum(value is not None for value in consumers) != 1:
            raise CatalogValidationError(
                "resource binding requires exactly one asset, scene, or resource consumer"
            )
        for value, field in (
            (item.asset_id, "asset_id"),
            (item.scene_id, "scene_id"),
            (item.consumer_resource_id, "consumer_resource_id"),
        ):
            if value is not None:
                _validate_slug(value, field)
        if item.consumer_resource_id == item.resource_id:
            raise CatalogValidationError("a resource may not bind to itself")
        return {
            "binding_id": item.binding_id,
            "resource_id": item.resource_id,
            "role": item.role,
            "asset_id": item.asset_id,
            "scene_id": item.scene_id,
            "consumer_resource_id": item.consumer_resource_id,
            "params_json": _json_object({} if item.params is None else item.params, "params"),
            "created_at": _utc_now(),
        }

    def _prepare_scene(self, scene: SceneUpsert) -> dict[str, Any]:
        _validate_slug(scene.scene_id, "scene_id")
        _validate_text(scene.name, "name")
        _validate_slug(scene.source, "source")
        _validate_text(scene.source_id, "source_id")
        _validate_uri(scene.source_url, "source_url")
        _validate_license(scene.license, scene.license_tier)
        _validate_uri(scene.license_url, "license_url")
        if not isinstance(scene.attribution, str) or scene.attribution != scene.attribution.strip():
            raise CatalogValidationError("attribution must be text without surrounding whitespace")
        _reject_control_characters(scene.attribution, "attribution")
        _validate_scene_status(scene.status)
        source_path = _relative_path(scene.source_path, self.project_root, "source_path")
        source_file = (
            _source_file_path(scene.source_file, self.project_root)
            if scene.source_file is not None
            else None
        )
        _validate_sha256(scene.source_sha256, "source_sha256")
        _validate_sha256(scene.spec_sha256, "spec_sha256")
        if scene.build_sha256 is not None:
            _validate_sha256(scene.build_sha256, "build_sha256")
        map_path = _validate_scene_map_path(scene.map_path, scene.scene_id)
        _validate_count(scene.actor_count, "actor_count")
        _validate_count(scene.static_mesh_count, "static_mesh_count")
        _validate_count(scene.triangle_count, "triangle_count")
        _validate_count(scene.material_count, "material_count")
        _validate_count(scene.texture_count, "texture_count")
        bounds_json = (
            _json_object(scene.bounds, "bounds", require_nonempty=True)
            if scene.bounds is not None
            else None
        )
        error_json = (
            _json_object(scene.error, "error", require_nonempty=True)
            if scene.error is not None
            else None
        )
        _validate_scene_status_payload(
            status=scene.status,
            map_path=map_path,
            actor_count=scene.actor_count,
            static_mesh_count=scene.static_mesh_count,
            triangle_count=scene.triangle_count,
            material_count=scene.material_count,
            texture_count=scene.texture_count,
            bounds=scene.bounds,
            error=scene.error,
            source_file=source_file,
            build_sha256=scene.build_sha256,
        )
        now = _utc_now()
        return {
            "scene_id": scene.scene_id,
            "name": scene.name.strip(),
            "source": scene.source,
            "source_id": scene.source_id.strip(),
            "source_url": scene.source_url.strip(),
            "license": scene.license,
            "license_tier": scene.license_tier,
            "license_url": scene.license_url.strip(),
            "attribution": scene.attribution.strip(),
            "source_path": source_path,
            "source_file": source_file,
            "source_sha256": scene.source_sha256,
            "spec_sha256": scene.spec_sha256,
            "build_sha256": scene.build_sha256,
            "status": scene.status,
            "map_path": map_path,
            "actor_count": scene.actor_count,
            "static_mesh_count": scene.static_mesh_count,
            "triangle_count": scene.triangle_count,
            "material_count": scene.material_count,
            "texture_count": scene.texture_count,
            "bounds_json": bounds_json,
            "error_json": error_json,
            "created_at": now,
            "updated_at": now,
        }

    def _prepare_scene_object(self, item: SceneObjectUpsert) -> dict[str, Any]:
        _validate_slug(item.object_id, "object_id")
        _validate_slug(item.scene_id, "scene_id")
        _validate_text(item.actor_name, "actor_name")
        _validate_text(item.actor_class, "actor_class")
        mesh_path = _validate_ue_package_path(item.mesh_path, field="mesh_path")
        transform_json = _json_object(item.transform, "transform", require_nonempty=True)
        bounds_json = (
            _json_object(item.bounds, "bounds", require_nonempty=True)
            if item.bounds is not None
            else None
        )
        _validate_count(item.triangle_count, "triangle_count")
        _validate_count(item.material_count, "material_count")
        if mesh_path is None and any(
            count is not None for count in (item.triangle_count, item.material_count)
        ):
            raise CatalogValidationError(
                "scene objects without mesh_path may not contain mesh statistics"
            )
        if mesh_path is not None and (item.triangle_count is None or item.triangle_count <= 0):
            raise CatalogValidationError("scene objects with mesh_path require triangle_count > 0")
        if mesh_path is not None and item.material_count is None:
            raise CatalogValidationError("scene objects with mesh_path require material_count")
        return {
            "object_id": item.object_id,
            "scene_id": item.scene_id,
            "actor_name": item.actor_name.strip(),
            "actor_class": item.actor_class.strip(),
            "mesh_path": mesh_path,
            "transform_json": transform_json,
            "bounds_json": bounds_json,
            "triangle_count": item.triangle_count,
            "material_count": item.material_count,
            "created_at": _utc_now(),
        }

    def _prepare_scene_artifact(self, item: SceneArtifactUpsert) -> dict[str, Any]:
        _validate_slug(item.artifact_id, "artifact_id")
        _validate_slug(item.scene_id, "scene_id")
        _validate_slug(item.kind, "kind")
        path = _relative_path(item.path, self.project_root, "path")
        params_json = _json_object(item.params or {}, "params")
        if item.sha256 is not None:
            _validate_sha256(item.sha256, "sha256")
        return {
            "artifact_id": item.artifact_id,
            "scene_id": item.scene_id,
            "kind": item.kind,
            "path": path,
            "params_json": params_json,
            "sha256": item.sha256,
            "created_at": _utc_now(),
        }


def _validate_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CatalogValidationError(f"{field} must be non-empty without surrounding whitespace")
    _reject_control_characters(value, field)


def _reject_control_characters(value: str, field: str) -> None:
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise CatalogValidationError(f"{field} must not contain control characters")


def _validate_slug(value: str, field: str) -> None:
    max_length = 96 if field in {"artifact_id", "kind", "object_id"} else 64
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise CatalogValidationError(f"{field} must contain 1 to {max_length} characters")
    if value[0] not in "abcdefghijklmnopqrstuvwxyz":
        raise CatalogValidationError(f"{field} must start with a lowercase ASCII letter")
    if (
        any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in value)
        or "__" in value
        or value.endswith("_")
    ):
        raise CatalogValidationError(
            f"{field} must be lower_snake_case without consecutive or trailing underscores"
        )


def _validate_uri(value: str, field: str) -> None:
    _validate_text(value, field)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https", "file", "urn"}:
        raise CatalogValidationError(f"{field} must use http, https, file, or urn")
    if parsed.scheme in {"http", "https"} and not parsed.netloc:
        raise CatalogValidationError(f"{field} must include a host")
    if parsed.scheme == "file" and not (parsed.netloc or parsed.path.startswith("/")):
        raise CatalogValidationError(f"{field} must be an absolute file URI")
    if parsed.scheme == "urn" and not parsed.path:
        raise CatalogValidationError(f"{field} must include a URN namespace")


def _validate_status(status: str) -> None:
    if status not in ASSET_STATUSES:
        allowed = ", ".join(sorted(ASSET_STATUSES))
        raise CatalogValidationError(f"status must be one of: {allowed}")


def _validate_scene_status(status: str) -> None:
    if status not in SCENE_STATUSES:
        allowed = ", ".join(sorted(SCENE_STATUSES))
        raise CatalogValidationError(f"scene status must be one of: {allowed}")


def _validate_resource_kind(resource_kind: str) -> None:
    if resource_kind not in RESOURCE_KINDS:
        allowed = ", ".join(sorted(RESOURCE_KINDS))
        raise CatalogValidationError(f"resource_kind must be one of: {allowed}")


def _validate_resource_status(status: str) -> None:
    if status not in RESOURCE_STATUSES:
        allowed = ", ".join(sorted(RESOURCE_STATUSES))
        raise CatalogValidationError(f"resource status must be one of: {allowed}")


def _validate_resource_token(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 32
        or value != value.strip()
        or value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value)
        or value.endswith(("_", "-"))
    ):
        raise CatalogValidationError(
            f"{field} must be a 1 to 32 character lowercase resource token"
        )


def _validate_page(*, limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
        raise CatalogValidationError("limit must be between 1 and 10000")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise CatalogValidationError("offset must be non-negative")


def _validate_license_name(license_name: str) -> None:
    if license_name not in SUPPORTED_LICENSES:
        allowed = ", ".join(sorted(SUPPORTED_LICENSES))
        raise CatalogValidationError(f"license must be a supported identifier: {allowed}")


def _validate_license(license_name: str, tier: str) -> None:
    _validate_license_name(license_name)
    if tier not in LICENSE_TIERS:
        allowed = ", ".join(sorted(LICENSE_TIERS))
        raise CatalogValidationError(f"license_tier must be one of: {allowed}")
    expected = "open"
    if license_name in NC_LICENSES:
        expected = "nc"
    elif license_name in UE_ONLY_LICENSES:
        expected = "ue-only"
    if tier != expected:
        raise CatalogValidationError(
            f"license {license_name!r} requires license_tier {expected!r}, got {tier!r}"
        )


def _validate_sha256(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CatalogValidationError(f"{field} must be a lowercase 64-character SHA-256")


def _validate_count(value: int | None, field: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise CatalogValidationError(f"{field} must be a non-negative integer or null")


def _validate_tag(tag: str) -> None:
    if not isinstance(tag, str) or not tag or tag != tag.strip() or len(tag) > 64:
        raise CatalogValidationError(
            "tag must contain 1 to 64 characters without surrounding whitespace"
        )
    _reject_control_characters(tag, "tag")


def _normalize_tags(tags: Iterable[str]) -> tuple[str, ...]:
    if isinstance(tags, str):
        raise CatalogValidationError("tags must be a sequence of strings, not one string")
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        _validate_tag(tag)
        if tag in seen:
            raise CatalogValidationError(f"duplicate tag: {tag!r}")
        seen.add(tag)
        normalized.append(tag)
    return tuple(sorted(normalized))


def _validate_ue_package_path(value: str | None, *, field: str = "ue_package_path") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogValidationError(f"{field} must be text or null")
    _reject_control_characters(value, field)
    if value != value.strip() or not value.startswith("/Game/"):
        raise CatalogValidationError(f"{field} must start with /Game/")
    invalid_part = any(part in {"", ".", ".."} for part in value[1:].split("/"))
    if "\\" in value or "//" in value or invalid_part:
        raise CatalogValidationError(f"{field} must be a normalized Unreal /Game/ path")
    return value


def _validate_scene_map_path(value: str | None, scene_id: str) -> str | None:
    if value is None:
        return None
    package_path = _validate_ue_package_path(value, field="map_path")
    expected = f"/Game/UEF/Scenes/{scene_id}/L_{scene_id}"
    if package_path != expected:
        raise CatalogValidationError(f"map_path must be exactly {expected}")
    return package_path


def _validate_status_payload(
    *,
    status: str,
    ue_package_path: str | None,
    tri_count: int | None,
    material_count: int | None,
    error: Mapping[str, Any] | None,
) -> None:
    if status == "failed":
        if not error:
            raise CatalogValidationError("failed assets require a non-empty structured error")
    elif error is not None:
        raise CatalogValidationError("only failed assets may contain error details")
    if status == "raw" and any(
        value is not None for value in (ue_package_path, tri_count, material_count)
    ):
        raise CatalogValidationError(
            "raw assets may not contain imported package or mesh statistics"
        )
    if status in {"imported", "render_ok"}:
        if ue_package_path is None:
            raise CatalogValidationError(f"{status} assets require ue_package_path")
        if tri_count is None or tri_count <= 0:
            raise CatalogValidationError(f"{status} assets require tri_count > 0")


def _validate_resource_status_payload(
    *,
    resource_kind: str,
    status: str,
    bundle_sha256: str | None,
    content_sha256: str | None,
    physical_size_mm: tuple[float, float] | None,
    error: Mapping[str, Any] | None,
) -> None:
    if status in {"failed", "quarantined"}:
        if not error:
            raise CatalogValidationError(f"{status} resources require a non-empty structured error")
    elif error is not None:
        raise CatalogValidationError(
            "only failed or quarantined resources may contain error details"
        )
    if status in {"verified", "ready"} and (bundle_sha256 is None or content_sha256 is None):
        raise CatalogValidationError(f"{status} resources require bundle_sha256 and content_sha256")
    if resource_kind == "hdri" and physical_size_mm is not None:
        raise CatalogValidationError("HDRI resources may not define physical_size_mm")
    if resource_kind == "pbr_texture_set" and status == "ready" and physical_size_mm is None:
        raise CatalogValidationError("ready PBR texture sets require physical_size_mm")


def _validate_scene_status_payload(
    *,
    status: str,
    map_path: str | None,
    actor_count: int | None,
    static_mesh_count: int | None,
    triangle_count: int | None,
    material_count: int | None,
    texture_count: int | None,
    bounds: Mapping[str, Any] | None,
    error: Mapping[str, Any] | None,
    source_file: str | None,
    build_sha256: str | None,
) -> None:
    build_values = (
        map_path,
        actor_count,
        static_mesh_count,
        triangle_count,
        material_count,
        texture_count,
        bounds,
    )
    if status in {"failed", "quarantined"}:
        if not error:
            raise CatalogValidationError(f"{status} scenes require a non-empty structured error")
    elif error is not None:
        raise CatalogValidationError("only failed or quarantined scenes may contain error details")
    if status in {"raw", "quarantined"} and any(value is not None for value in build_values):
        raise CatalogValidationError(f"{status} scenes may not contain built map statistics")
    if status in {"built", "render_ok"}:
        _require_complete_scene_build_payload(
            status=status,
            map_path=map_path,
            actor_count=actor_count,
            static_mesh_count=static_mesh_count,
            triangle_count=triangle_count,
            material_count=material_count,
            texture_count=texture_count,
            bounds=bounds,
            source_file=source_file,
            build_sha256=build_sha256,
        )
    if status == "failed" and any(value is not None for value in build_values):
        _require_complete_scene_build_payload(
            status=status,
            map_path=map_path,
            actor_count=actor_count,
            static_mesh_count=static_mesh_count,
            triangle_count=triangle_count,
            material_count=material_count,
            texture_count=texture_count,
            bounds=bounds,
            source_file=source_file,
            build_sha256=build_sha256,
        )


def _require_complete_scene_build_payload(
    *,
    status: str,
    map_path: str | None,
    actor_count: int | None,
    static_mesh_count: int | None,
    triangle_count: int | None,
    material_count: int | None,
    texture_count: int | None,
    bounds: Mapping[str, Any] | None,
    source_file: str | None,
    build_sha256: str | None,
) -> None:
    if source_file is None:
        raise CatalogValidationError(f"{status} scenes require source_file")
    if build_sha256 is None:
        raise CatalogValidationError(f"{status} scenes require build_sha256")
    if map_path is None:
        raise CatalogValidationError(f"{status} scenes require map_path")
    if actor_count is None or actor_count <= 0:
        raise CatalogValidationError(f"{status} scenes require actor_count > 0")
    if static_mesh_count is None or static_mesh_count <= 0:
        raise CatalogValidationError(f"{status} scenes require static_mesh_count > 0")
    if static_mesh_count > actor_count:
        raise CatalogValidationError("static_mesh_count may not exceed actor_count")
    if triangle_count is None or triangle_count <= 0:
        raise CatalogValidationError(f"{status} scenes require triangle_count > 0")
    if material_count is None:
        raise CatalogValidationError(f"{status} scenes require material_count")
    if texture_count is None:
        raise CatalogValidationError(f"{status} scenes require texture_count")
    if not bounds:
        raise CatalogValidationError(f"{status} scenes require non-empty bounds")


def _relative_path(value: str | Path, project_root: Path, field: str) -> str:
    if not isinstance(value, str | Path):
        raise CatalogValidationError(f"{field} must be a string or Path")
    raw = str(value)
    _reject_control_characters(raw, field)
    if not raw or raw != raw.strip() or "\\" in raw or "//" in raw:
        raise CatalogValidationError(
            f"{field} must be a non-empty normalized path without backslashes"
        )
    path = Path(raw)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(project_root)
        except ValueError as exc:
            raise CatalogValidationError(
                f"{field} absolute path must be inside project_root {project_root}"
            ) from exc
    pure = PurePosixPath(path.as_posix())
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or (pure.parts and pure.parts[0].endswith(":"))
    ):
        raise CatalogValidationError(f"{field} must be a normalized relative path")
    normalized = pure.as_posix()
    if normalized in {"", "."} or "//" in normalized:
        raise CatalogValidationError(f"{field} must be a normalized relative path")
    candidate = project_root / Path(*pure.parts)
    current = project_root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise CatalogValidationError(f"{field} may not traverse a symbolic link")
    try:
        candidate.resolve().relative_to(project_root)
    except ValueError as exc:
        raise CatalogValidationError(f"{field} must resolve inside project_root") from exc
    return normalized


def _source_file_path(value: str | Path, project_root: Path) -> str:
    if not isinstance(value, str | Path):
        raise CatalogValidationError("source_file must be a string or Path")
    raw = str(value)
    _reject_control_characters(raw, "source_file")
    if not raw or raw != raw.strip() or "\\" in raw or "\x00" in raw:
        raise CatalogValidationError("source_file must be a non-empty normalized path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return _relative_path(path, project_root, "source_file")
    pure = PurePosixPath(path.as_posix())
    if any(part in {"", ".", ".."} for part in pure.parts[1:]) or "//" in raw:
        raise CatalogValidationError("source_file must be a normalized absolute path")
    return pure.as_posix()


def _json_object(value: Mapping[str, Any], field: str, *, require_nonempty: bool = False) -> str:
    if not isinstance(value, Mapping):
        raise CatalogValidationError(f"{field} must be a JSON object")
    if require_nonempty and not value:
        raise CatalogValidationError(f"{field} must be a non-empty JSON object")
    try:
        normalized = _normalize_json_value(value, field)
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CatalogValidationError(f"{field} must be JSON serializable: {exc}") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise CatalogValidationError(f"{field} must be a JSON object")
    return encoded


def _normalize_json_value(value: Any, field: str) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CatalogValidationError(f"{field} must contain only finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise CatalogValidationError(f"{field} JSON object keys must be strings")
            normalized[key] = _normalize_json_value(child, field)
        return normalized
    if isinstance(value, list | tuple):
        return [_normalize_json_value(child, field) for child in value]
    raise CatalogValidationError(
        f"{field} must contain only JSON-compatible values, got {type(value).__name__}"
    )


def _validate_resource_evidence(
    resource: Mapping[str, Any],
    files: tuple[dict[str, Any], ...],
    artifacts: tuple[dict[str, Any], ...],
) -> None:
    if not files:
        raise CatalogValidationError("finalize_resource requires at least one source file")
    if not artifacts:
        raise CatalogValidationError("finalize_resource requires evidence artifacts")
    if any(item["resolution"] != resource["resolution"] for item in files):
        raise CatalogValidationError(
            "every resource file must use the finalized resource resolution"
        )
    primary_files = [item for item in files if item["is_primary"] == 1]
    if len(primary_files) != 1:
        raise CatalogValidationError("finalize_resource requires exactly one primary file")
    artifact_counts = Counter(str(item["kind"]) for item in artifacts)
    required_kinds = RESOURCE_REQUIRED_ARTIFACT_KINDS[
        "verified" if resource["status"] == "verified" else str(resource["resource_kind"])
    ]
    missing_or_ambiguous = sorted(kind for kind in required_kinds if artifact_counts.get(kind) != 1)
    if missing_or_ambiguous:
        raise CatalogValidationError(
            "resource evidence requires exactly one of each artifact kind: "
            + ", ".join(missing_or_ambiguous)
        )
    for artifact in artifacts:
        if artifact["sha256"] is None:
            raise CatalogValidationError("resource evidence artifacts require sha256")
        if artifact["kind"] in required_kinds:
            _validate_resource_artifact_binding(resource, artifact)

    if resource["status"] != "ready":
        return
    if resource["resource_kind"] == "hdri":
        _validate_ready_hdri(resource, files, artifacts)
    elif resource["resource_kind"] == "pbr_texture_set":
        _validate_ready_pbr(resource, files, artifacts)
    else:  # pragma: no cover - resource kind validation is closed above this layer
        raise CatalogValidationError("unsupported ready resource kind")


def _validate_resource_artifact_binding(
    resource: Mapping[str, Any], artifact: Mapping[str, Any]
) -> None:
    params = json.loads(str(artifact["params_json"]))
    expected = {
        "schema_version": 1,
        "resource_id": resource["resource_id"],
        "resource_kind": resource["resource_kind"],
        "profile": resource["profile"],
        "resolution": resource["resolution"],
        "bundle_sha256": resource["bundle_sha256"],
        "content_sha256": resource["content_sha256"],
    }
    changed = [key for key, value in expected.items() if params.get(key) != value]
    if changed:
        raise CatalogValidationError(
            f"resource artifact {artifact['kind']!r} does not bind active evidence: "
            + ", ".join(changed)
        )


def _artifact_params(artifacts: tuple[dict[str, Any], ...], kind: str) -> dict[str, Any]:
    artifact = next(item for item in artifacts if item["kind"] == kind)
    return json.loads(str(artifact["params_json"]))


def _validate_ready_hdri(
    resource: Mapping[str, Any],
    files: tuple[dict[str, Any], ...],
    artifacts: tuple[dict[str, Any], ...],
) -> None:
    if len(files) != 1:
        raise CatalogValidationError("ready HDRI profiles require exactly one radiance file")
    item = files[0]
    expected_format = "hdr" if resource["profile"] == "radiance_hdr_v1" else "exr"
    if item["semantic_role"] != "environment_radiance":
        raise CatalogValidationError("ready HDRI primary file must be environment_radiance")
    if item["format"] != expected_format:
        raise CatalogValidationError(
            f"ready HDRI profile {resource['profile']!r} requires {expected_format!r} format"
        )
    if item["color_space"] != "linear" or item["normal_convention"] is not None:
        raise CatalogValidationError("ready HDRI radiance must be linear and not a normal map")
    width = item["width"]
    height = item["height"]
    if width is None or height is None or width != height * 2:
        raise CatalogValidationError("ready HDRI radiance must decode to a positive 2:1 image")
    validation = _artifact_params(artifacts, "hdri_validation_manifest")
    if (
        validation.get("validation_status") != "passed"
        or validation.get("width") != width
        or validation.get("height") != height
        or validation.get("file_id") != item["file_id"]
    ):
        raise CatalogValidationError("HDRI validation manifest does not prove active radiance")


def _validate_ready_pbr(
    resource: Mapping[str, Any],
    files: tuple[dict[str, Any], ...],
    artifacts: tuple[dict[str, Any], ...],
) -> None:
    if resource["profile"] != "ue_pbr_png_v1":  # pragma: no cover - closed profile enum
        raise CatalogValidationError("unsupported ready PBR profile")
    if any(item["format"] != "png" for item in files):
        raise CatalogValidationError("ue_pbr_png_v1 requires only PNG source maps")
    if any(item["width"] is None or item["height"] is None for item in files):
        raise CatalogValidationError("ready PBR maps require decoded width and height")
    dimensions = {(item["width"], item["height"]) for item in files}
    if len(dimensions) != 1:
        raise CatalogValidationError("ready PBR maps must share one decoded resolution")
    by_role: dict[str, list[dict[str, Any]]] = {}
    for item in files:
        by_role.setdefault(str(item["semantic_role"]), []).append(item)
    for role in ("base_color", "normal"):
        if len(by_role.get(role, ())) != 1:
            raise CatalogValidationError(f"ready PBR profile requires exactly one {role} map")
    roughness = by_role.get("roughness", ())
    packed = by_role.get("packed_material", ())
    if (len(roughness) == 1) == (len(packed) == 1):
        raise CatalogValidationError(
            "ready PBR profile requires exactly one roughness or packed_material source"
        )
    if len(roughness) > 1 or len(packed) > 1:
        raise CatalogValidationError("ready PBR material roles must be unambiguous")
    base_color = by_role["base_color"][0]
    normal = by_role["normal"][0]
    if base_color["color_space"] != "srgb" or base_color["is_primary"] != 1:
        raise CatalogValidationError("ready PBR base_color must be the primary sRGB map")
    if normal["color_space"] not in {"linear", "data"}:
        raise CatalogValidationError("ready PBR normal map must use a non-color space")
    if normal["normal_convention"] != "directx":
        raise CatalogValidationError("ue_pbr_png_v1 requires a DirectX normal map")
    for role, items in by_role.items():
        if role != "base_color" and any(item["color_space"] == "srgb" for item in items):
            raise CatalogValidationError(f"ready PBR {role} maps may not use sRGB")
    if packed:
        channel_values = tuple(json.loads(str(packed[0]["channels_json"])).values())
        required_channels = {"ambient_occlusion", "roughness", "metallic"}
        if not required_channels <= set(channel_values) or len(channel_values) != len(
            set(channel_values)
        ):
            raise CatalogValidationError(
                "packed_material channels must map distinct ambient_occlusion, roughness, "
                "and metallic semantics"
            )
    descriptor = _artifact_params(artifacts, "pbr_material_descriptor")
    validation = _artifact_params(artifacts, "pbr_validation_manifest")
    expected_file_ids = sorted(str(item["file_id"]) for item in files)
    expected_physical_size = [
        resource["physical_width_mm"],
        resource["physical_height_mm"],
    ]
    if (
        descriptor.get("file_ids") != expected_file_ids
        or descriptor.get("physical_size_mm") != expected_physical_size
    ):
        raise CatalogValidationError("PBR material descriptor does not bind the file cohort")
    if (
        validation.get("validation_status") != "passed"
        or validation.get("file_ids") != expected_file_ids
    ):
        raise CatalogValidationError("PBR validation manifest does not prove the file cohort")


def _assert_published_resource_cohort_unchanged(
    connection: sqlite3.Connection,
    *,
    existing: sqlite3.Row,
    resource: Mapping[str, Any],
    files: tuple[dict[str, Any], ...],
    artifacts: tuple[dict[str, Any], ...],
) -> None:
    immutable_resource_fields = (
        "name",
        "resource_kind",
        "profile",
        "resolution",
        "source",
        "source_id",
        "source_url",
        "source_revision",
        "source_revision_scheme",
        "license",
        "license_tier",
        "license_url",
        "attribution",
        "tags_json",
        "bundle_sha256",
        "content_sha256",
        "physical_width_mm",
        "physical_height_mm",
    )
    changed = [field for field in immutable_resource_fields if existing[field] != resource[field]]
    if resource["status"] != existing["status"]:
        changed.append("status")
    if changed:
        raise CatalogConflictError(
            "published resource evidence is immutable: " + ", ".join(changed)
        )

    def comparable(values: Mapping[str, Any]) -> dict[str, Any]:
        return {key: values[key] for key in values if key != "created_at"}

    existing_files = tuple(
        comparable(dict(row))
        for row in connection.execute(
            "SELECT * FROM resource_files WHERE resource_id = ? ORDER BY file_id",
            (resource["resource_id"],),
        )
    )
    proposed_files = tuple(
        comparable(item) for item in sorted(files, key=lambda row: row["file_id"])
    )
    existing_artifacts = tuple(
        comparable(dict(row))
        for row in connection.execute(
            "SELECT * FROM resource_artifacts WHERE resource_id = ? ORDER BY artifact_id",
            (resource["resource_id"],),
        )
    )
    proposed_artifacts = tuple(
        comparable(item) for item in sorted(artifacts, key=lambda row: row["artifact_id"])
    )
    if existing_files != proposed_files or existing_artifacts != proposed_artifacts:
        raise CatalogConflictError("published resource file and artifact evidence is immutable")


def _assert_verified_resource_upgrade(
    connection: sqlite3.Connection,
    *,
    existing: sqlite3.Row,
    resource: Mapping[str, Any],
    files: tuple[dict[str, Any], ...],
    artifacts: tuple[dict[str, Any], ...],
) -> None:
    """Allow verified→ready only by appending the required validation proofs."""

    immutable_resource_fields = (
        "name",
        "resource_kind",
        "profile",
        "resolution",
        "source",
        "source_id",
        "source_url",
        "source_revision",
        "source_revision_scheme",
        "license",
        "license_tier",
        "license_url",
        "attribution",
        "tags_json",
        "bundle_sha256",
        "content_sha256",
        "physical_width_mm",
        "physical_height_mm",
    )
    changed = [field for field in immutable_resource_fields if existing[field] != resource[field]]
    if changed:
        raise CatalogConflictError(
            "verified resource upgrade changes immutable evidence: " + ", ".join(changed)
        )

    def comparable(values: Mapping[str, Any]) -> dict[str, Any]:
        return {key: values[key] for key in values if key != "created_at"}

    existing_files = tuple(
        comparable(dict(row))
        for row in connection.execute(
            "SELECT * FROM resource_files WHERE resource_id = ? ORDER BY file_id",
            (resource["resource_id"],),
        )
    )
    proposed_files = tuple(
        comparable(item) for item in sorted(files, key=lambda row: row["file_id"])
    )
    if existing_files != proposed_files:
        raise CatalogConflictError(
            "verified resource file evidence is immutable during ready upgrade"
        )

    existing_artifacts = {
        str(row["artifact_id"]): comparable(dict(row))
        for row in connection.execute(
            "SELECT * FROM resource_artifacts WHERE resource_id = ? ORDER BY artifact_id",
            (resource["resource_id"],),
        )
    }
    proposed_artifacts = {str(item["artifact_id"]): comparable(item) for item in artifacts}
    if any(
        artifact_id not in proposed_artifacts
        or proposed_artifacts[artifact_id] != existing_artifact
        for artifact_id, existing_artifact in existing_artifacts.items()
    ):
        raise CatalogConflictError(
            "verified resource artifact evidence is immutable during ready upgrade"
        )
    required_ready = RESOURCE_REQUIRED_ARTIFACT_KINDS[str(resource["resource_kind"])]
    existing_kinds = {str(item["kind"]) for item in existing_artifacts.values()}
    allowed_new_kinds = required_ready - existing_kinds
    actual_new_kinds = {
        str(item["kind"])
        for artifact_id, item in proposed_artifacts.items()
        if artifact_id not in existing_artifacts
    }
    if actual_new_kinds != allowed_new_kinds:
        raise CatalogConflictError(
            "verified resource upgrade may append only its required ready proofs"
        )


def _reject_batch_duplicates(rows: tuple[dict[str, Any], ...], key: str) -> None:
    counts = Counter(str(row[key]) for row in rows)
    duplicates = sorted(value for value, count in counts.items() if count > 1)
    if duplicates:
        raise CatalogConflictError(f"duplicate {key} in transaction: {', '.join(duplicates)}")


def _reject_batch_duplicate_fields(
    rows: tuple[dict[str, Any], ...], fields: tuple[str, ...]
) -> None:
    counts = Counter(tuple(str(row[field]) for field in fields) for row in rows)
    duplicates = sorted(values for values, count in counts.items() if count > 1)
    if duplicates:
        rendered = ", ".join("/".join(values) for values in duplicates)
        raise CatalogConflictError(f"duplicate {'/'.join(fields)} in transaction: {rendered}")


def _upsert_prepared_asset(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> None:
    provenance_fields = (
        "sha256",
        "source",
        "source_id",
        "source_url",
        "license",
        "license_tier",
        "license_url",
        "attribution",
    )
    existing = connection.execute(
        f"SELECT {', '.join(provenance_fields)} FROM assets WHERE asset_id = ?",
        (values["asset_id"],),
    ).fetchone()
    if existing is not None:
        changed = [field for field in provenance_fields if existing[field] != values[field]]
        if changed:
            raise CatalogConflictError(
                f"asset_id {values['asset_id']!r} has immutable provenance conflicts: "
                + ", ".join(changed)
            )
    connection.execute(_UPSERT_ASSET_SQL, values)


def _upsert_prepared_artifact(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> None:
    existing = connection.execute(
        "SELECT asset_id, kind, path FROM artifacts WHERE artifact_id = ?",
        (values["artifact_id"],),
    ).fetchone()
    identity = (values["asset_id"], values["kind"], values["path"])
    if existing is not None and tuple(existing) != identity:
        raise CatalogConflictError(
            f"artifact_id {values['artifact_id']!r} is already bound to another artifact"
        )
    connection.execute(_UPSERT_ARTIFACT_SQL, values)


def _upsert_prepared_resource(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> None:
    provenance_fields = (
        "resource_kind",
        "profile",
        "resolution",
        "source",
        "source_id",
        "source_url",
        "source_revision",
        "source_revision_scheme",
        "license",
        "license_tier",
        "license_url",
        "attribution",
    )
    existing = connection.execute(
        "SELECT * FROM resources WHERE resource_id = ?", (values["resource_id"],)
    ).fetchone()
    if existing is not None:
        changed = [field for field in provenance_fields if existing[field] != values[field]]
        for field in (
            "bundle_sha256",
            "content_sha256",
            "physical_width_mm",
            "physical_height_mm",
        ):
            if existing[field] is not None and existing[field] != values[field]:
                changed.append(field)
        if changed:
            raise CatalogConflictError(
                f"resource_id {values['resource_id']!r} has immutable conflicts: "
                + ", ".join(changed)
            )
        if _resource_has_published_lineage(connection, existing) and existing["status"] not in {
            "verified",
            "ready",
        }:
            lineage_fields = (
                "name",
                "tags_json",
                "status",
                "error_json",
                "bundle_sha256",
                "content_sha256",
                "physical_width_mm",
                "physical_height_mm",
            )
            lineage_changed = [
                field for field in lineage_fields if existing[field] != values[field]
            ]
            if lineage_changed:
                raise CatalogConflictError(
                    "legacy published resource lineage is immutable: " + ", ".join(lineage_changed)
                )
        transitions = {
            "verified": {"verified", "ready"},
            "ready": {"ready"},
            "failed": {"failed", "verified", "ready", "quarantined"},
            "quarantined": {"quarantined"},
        }
        if values["status"] not in transitions[str(existing["status"])]:
            raise CatalogValidationError(
                f"illegal resource status transition: {existing['status']} -> {values['status']}"
            )
    connection.execute(_UPSERT_RESOURCE_SQL, values)


def _resource_has_published_lineage(
    connection: sqlite3.Connection,
    resource: Mapping[str, Any],
) -> bool:
    del connection
    return bool(resource["published_once"])


def _upsert_prepared_resource_file(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
    *,
    allow_published_mutation: bool = False,
) -> None:
    parent = connection.execute(
        "SELECT * FROM resources WHERE resource_id = ?", (values["resource_id"],)
    ).fetchone()
    if parent is None:
        raise CatalogValidationError("resource file requires an existing resource")
    existing = connection.execute(
        "SELECT * FROM resource_files WHERE file_id = ?", (values["file_id"],)
    ).fetchone()
    published = _resource_has_published_lineage(connection, parent)
    if published and existing is None and not allow_published_mutation:
        raise CatalogConflictError("published resource file evidence is immutable")
    if existing is not None:
        immutable_fields = tuple(key for key in values if key not in {"params_json", "created_at"})
        changed = [field for field in immutable_fields if existing[field] != values[field]]
        if (
            published
            and not allow_published_mutation
            and existing["params_json"] != values["params_json"]
        ):
            changed.append("params_json")
        if changed:
            raise CatalogConflictError(
                f"resource file {values['file_id']!r} has immutable conflicts: "
                + ", ".join(changed)
            )
    connection.execute(_UPSERT_RESOURCE_FILE_SQL, values)


def _upsert_prepared_resource_artifact(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
    *,
    allow_published_mutation: bool = False,
) -> None:
    parent = connection.execute(
        "SELECT * FROM resources WHERE resource_id = ?",
        (values["resource_id"],),
    ).fetchone()
    if parent is None:
        raise CatalogValidationError("resource artifact requires an existing resource")
    existing = connection.execute(
        "SELECT * FROM resource_artifacts WHERE artifact_id = ?",
        (values["artifact_id"],),
    ).fetchone()
    identity = (values["resource_id"], values["kind"], values["path"])
    if (
        existing is not None
        and tuple(existing[field] for field in ("resource_id", "kind", "path")) != identity
    ):
        raise CatalogConflictError(
            f"resource artifact {values['artifact_id']!r} is bound to another identity"
        )
    if (
        _resource_has_published_lineage(connection, parent)
        and not allow_published_mutation
        and (
            existing is None
            or any(existing[field] != values[field] for field in ("params_json", "sha256"))
        )
    ):
        raise CatalogConflictError("published resource artifact evidence is immutable")
    connection.execute(_UPSERT_RESOURCE_ARTIFACT_SQL, values)


def _upsert_prepared_resource_binding(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> None:
    referenced = connection.execute(
        "SELECT status FROM resources WHERE resource_id = ?", (values["resource_id"],)
    ).fetchone()
    if referenced is None or referenced["status"] != "ready":
        raise CatalogValidationError("resource bindings require a ready referenced resource")
    identity_fields = (
        "resource_id",
        "role",
        "asset_id",
        "scene_id",
        "consumer_resource_id",
    )
    existing = connection.execute(
        "SELECT * FROM resource_bindings WHERE binding_id = ?", (values["binding_id"],)
    ).fetchone()
    if existing is not None:
        changed = [field for field in identity_fields if existing[field] != values[field]]
        if changed:
            raise CatalogConflictError(
                f"resource binding {values['binding_id']!r} has immutable conflicts: "
                + ", ".join(changed)
            )
    connection.execute(_UPSERT_RESOURCE_BINDING_SQL, values)


def _upsert_prepared_scene(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> None:
    provenance_fields = (
        "source",
        "source_id",
        "source_url",
        "license",
        "license_tier",
        "license_url",
        "attribution",
    )
    existing = connection.execute(
        f"SELECT {', '.join(provenance_fields)} FROM scenes WHERE scene_id = ?",
        (values["scene_id"],),
    ).fetchone()
    if existing is not None:
        changed = [field for field in provenance_fields if existing[field] != values[field]]
        if changed:
            raise CatalogConflictError(
                f"scene_id {values['scene_id']!r} has immutable provenance conflicts: "
                + ", ".join(changed)
            )
    connection.execute(_UPSERT_SCENE_SQL, values)


def _upsert_prepared_scene_object(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> None:
    existing = connection.execute(
        "SELECT scene_id FROM scene_objects WHERE object_id = ?", (values["object_id"],)
    ).fetchone()
    if existing is not None and existing["scene_id"] != values["scene_id"]:
        raise CatalogConflictError(
            f"object_id {values['object_id']!r} is already bound to another scene"
        )
    connection.execute(_UPSERT_SCENE_OBJECT_SQL, values)


def _upsert_prepared_scene_artifact(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> None:
    existing = connection.execute(
        "SELECT scene_id, kind, path FROM scene_artifacts WHERE artifact_id = ?",
        (values["artifact_id"],),
    ).fetchone()
    identity = (values["scene_id"], values["kind"], values["path"])
    if existing is not None and tuple(existing) != identity:
        raise CatalogConflictError(
            f"artifact_id {values['artifact_id']!r} is already bound to another scene artifact"
        )
    connection.execute(_UPSERT_SCENE_ARTIFACT_SQL, values)


def _write_prepared_scene_build(
    connection: sqlite3.Connection,
    *,
    scene_id: str,
    scene: Mapping[str, Any],
    objects: tuple[Mapping[str, Any], ...],
    artifacts: tuple[Mapping[str, Any], ...],
) -> None:
    """Apply the complete scene-build replacement to an open transaction."""

    _upsert_prepared_scene(connection, scene)
    connection.execute("DELETE FROM scene_objects WHERE scene_id = ?", (scene_id,))
    connection.execute("DELETE FROM scene_artifacts WHERE scene_id = ?", (scene_id,))
    for values in objects:
        _upsert_prepared_scene_object(connection, values)
    for values in artifacts:
        _upsert_prepared_scene_artifact(connection, values)


def _validate_render_scene_unchanged(
    existing: sqlite3.Row,
    prepared: Mapping[str, Any],
) -> None:
    build_fields = (
        "name",
        "source",
        "source_id",
        "source_url",
        "license",
        "license_tier",
        "license_url",
        "attribution",
        "source_path",
        "source_file",
        "source_sha256",
        "spec_sha256",
        "build_sha256",
        "map_path",
        "actor_count",
        "static_mesh_count",
        "triangle_count",
        "material_count",
        "texture_count",
        "bounds_json",
    )
    changed = [field for field in build_fields if existing[field] != prepared[field]]
    if changed:
        raise CatalogValidationError(
            "finalize_scene_render may not change built scene fields: " + ", ".join(changed)
        )


def _asset_from_row(row: sqlite3.Row) -> AssetRecord:
    tags = json.loads(str(row["tags_json"]))
    error = None if row["error_json"] is None else json.loads(str(row["error_json"]))
    return AssetRecord(
        asset_id=str(row["asset_id"]),
        name=str(row["name"]),
        source=str(row["source"]),
        source_id=str(row["source_id"]),
        source_url=str(row["source_url"]),
        license=str(row["license"]),
        license_tier=str(row["license_tier"]),
        license_url=str(row["license_url"]),
        attribution=str(row["attribution"]),
        status=str(row["status"]),
        tags=tuple(str(tag) for tag in tags),
        raw_path=str(row["raw_path"]),
        ue_package_path=None if row["ue_package_path"] is None else str(row["ue_package_path"]),
        tri_count=None if row["tri_count"] is None else int(row["tri_count"]),
        material_count=None if row["material_count"] is None else int(row["material_count"]),
        sha256=str(row["sha256"]),
        error=error,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=str(row["artifact_id"]),
        asset_id=str(row["asset_id"]),
        kind=str(row["kind"]),
        path=str(row["path"]),
        params=json.loads(str(row["params_json"])),
        sha256=None if row["sha256"] is None else str(row["sha256"]),
        created_at=str(row["created_at"]),
    )


def _resource_from_row(row: sqlite3.Row) -> ResourceRecord:
    physical_size_mm = (
        None
        if row["physical_width_mm"] is None
        else (float(row["physical_width_mm"]), float(row["physical_height_mm"]))
    )
    error = None if row["error_json"] is None else json.loads(str(row["error_json"]))
    return ResourceRecord(
        resource_id=str(row["resource_id"]),
        resource_kind=str(row["resource_kind"]),
        profile=str(row["profile"]),
        resolution=str(row["resolution"]),
        name=str(row["name"]),
        source=str(row["source"]),
        source_id=str(row["source_id"]),
        source_url=str(row["source_url"]),
        source_revision=str(row["source_revision"]),
        source_revision_scheme=str(row["source_revision_scheme"]),
        license=str(row["license"]),
        license_tier=str(row["license_tier"]),
        license_url=str(row["license_url"]),
        attribution=str(row["attribution"]),
        status=str(row["status"]),
        tags=tuple(str(tag) for tag in json.loads(str(row["tags_json"]))),
        bundle_sha256=(None if row["bundle_sha256"] is None else str(row["bundle_sha256"])),
        content_sha256=(None if row["content_sha256"] is None else str(row["content_sha256"])),
        physical_size_mm=physical_size_mm,
        error=error,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _resource_file_from_row(row: sqlite3.Row) -> ResourceFileRecord:
    return ResourceFileRecord(
        file_id=str(row["file_id"]),
        resource_id=str(row["resource_id"]),
        semantic_role=str(row["semantic_role"]),
        provider_role=str(row["provider_role"]),
        resolution=str(row["resolution"]),
        format=str(row["format"]),
        path=str(row["path"]),
        source_url=str(row["source_url"]),
        byte_size=int(row["byte_size"]),
        provider_md5=None if row["provider_md5"] is None else str(row["provider_md5"]),
        sha256=str(row["sha256"]),
        color_space=str(row["color_space"]),
        normal_convention=(
            None if row["normal_convention"] is None else str(row["normal_convention"])
        ),
        channels=json.loads(str(row["channels_json"])),
        width=None if row["width"] is None else int(row["width"]),
        height=None if row["height"] is None else int(row["height"]),
        is_primary=bool(row["is_primary"]),
        params=json.loads(str(row["params_json"])),
        created_at=str(row["created_at"]),
    )


def _resource_artifact_from_row(row: sqlite3.Row) -> ResourceArtifactRecord:
    return ResourceArtifactRecord(
        artifact_id=str(row["artifact_id"]),
        resource_id=str(row["resource_id"]),
        kind=str(row["kind"]),
        path=str(row["path"]),
        params=json.loads(str(row["params_json"])),
        sha256=None if row["sha256"] is None else str(row["sha256"]),
        created_at=str(row["created_at"]),
    )


def _resource_binding_from_row(row: sqlite3.Row) -> ResourceBindingRecord:
    return ResourceBindingRecord(
        binding_id=str(row["binding_id"]),
        resource_id=str(row["resource_id"]),
        role=str(row["role"]),
        asset_id=None if row["asset_id"] is None else str(row["asset_id"]),
        scene_id=None if row["scene_id"] is None else str(row["scene_id"]),
        consumer_resource_id=(
            None if row["consumer_resource_id"] is None else str(row["consumer_resource_id"])
        ),
        params=json.loads(str(row["params_json"])),
        created_at=str(row["created_at"]),
    )


def _scene_from_row(row: sqlite3.Row) -> SceneRecord:
    bounds = None if row["bounds_json"] is None else json.loads(str(row["bounds_json"]))
    error = None if row["error_json"] is None else json.loads(str(row["error_json"]))
    return SceneRecord(
        scene_id=str(row["scene_id"]),
        name=str(row["name"]),
        source=str(row["source"]),
        source_id=str(row["source_id"]),
        source_url=str(row["source_url"]),
        license=str(row["license"]),
        license_tier=str(row["license_tier"]),
        license_url=str(row["license_url"]),
        attribution=str(row["attribution"]),
        source_path=str(row["source_path"]),
        source_file=None if row["source_file"] is None else str(row["source_file"]),
        source_sha256=str(row["source_sha256"]),
        spec_sha256=str(row["spec_sha256"]),
        build_sha256=None if row["build_sha256"] is None else str(row["build_sha256"]),
        status=str(row["status"]),
        map_path=None if row["map_path"] is None else str(row["map_path"]),
        actor_count=None if row["actor_count"] is None else int(row["actor_count"]),
        static_mesh_count=(
            None if row["static_mesh_count"] is None else int(row["static_mesh_count"])
        ),
        triangle_count=(None if row["triangle_count"] is None else int(row["triangle_count"])),
        material_count=(None if row["material_count"] is None else int(row["material_count"])),
        texture_count=(None if row["texture_count"] is None else int(row["texture_count"])),
        bounds=bounds,
        error=error,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _scene_object_from_row(row: sqlite3.Row) -> SceneObjectRecord:
    bounds = None if row["bounds_json"] is None else json.loads(str(row["bounds_json"]))
    return SceneObjectRecord(
        object_id=str(row["object_id"]),
        scene_id=str(row["scene_id"]),
        actor_name=str(row["actor_name"]),
        actor_class=str(row["actor_class"]),
        mesh_path=None if row["mesh_path"] is None else str(row["mesh_path"]),
        transform=json.loads(str(row["transform_json"])),
        bounds=bounds,
        triangle_count=(None if row["triangle_count"] is None else int(row["triangle_count"])),
        material_count=(None if row["material_count"] is None else int(row["material_count"])),
        created_at=str(row["created_at"]),
    )


def _scene_artifact_from_row(row: sqlite3.Row) -> SceneArtifactRecord:
    return SceneArtifactRecord(
        artifact_id=str(row["artifact_id"]),
        scene_id=str(row["scene_id"]),
        kind=str(row["kind"]),
        path=str(row["path"]),
        params=json.loads(str(row["params_json"])),
        sha256=None if row["sha256"] is None else str(row["sha256"]),
        created_at=str(row["created_at"]),
    )


def _group_counts(connection: sqlite3.Connection, column: str) -> dict[str, int]:
    allowed = {"status", "source", "license", "license_tier"}
    if column not in allowed:  # pragma: no cover - internal programming guard
        raise CatalogError(f"unsupported stats column: {column}")
    return {
        str(row[0]): int(row[1])
        for row in connection.execute(
            f"SELECT {column}, count(*) FROM assets GROUP BY {column} ORDER BY {column}"
        )
    }


def _resource_group_counts(connection: sqlite3.Connection, column: str) -> dict[str, int]:
    allowed = {"resource_kind", "status", "source", "license", "license_tier"}
    if column not in allowed:  # pragma: no cover - internal programming guard
        raise CatalogError(f"unsupported resource stats column: {column}")
    return {
        str(row[0]): int(row[1])
        for row in connection.execute(
            f"SELECT {column}, count(*) FROM resources GROUP BY {column} ORDER BY {column}"
        )
    }


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_ASSET_PATH_CHECK = """
length(raw_path) > 0
AND raw_path = trim(raw_path)
AND substr(raw_path, 1, 1) <> '/'
AND raw_path NOT GLOB '[A-Za-z]:*'
AND instr(raw_path, '\\') = 0
AND raw_path <> '.' AND raw_path <> '..'
AND raw_path NOT LIKE './%' AND raw_path NOT LIKE '../%'
AND raw_path NOT LIKE '%/./%' AND raw_path NOT LIKE '%/../%'
AND raw_path NOT LIKE '%/.' AND raw_path NOT LIKE '%/..'
AND raw_path NOT LIKE '%//%'
""".strip()

_ARTIFACT_PATH_CHECK = _ASSET_PATH_CHECK.replace("raw_path", "path")
_SCENE_SOURCE_PATH_CHECK = _ASSET_PATH_CHECK.replace("raw_path", "source_path")

_OPEN_LICENSE_SQL = ", ".join(f"'{value}'" for value in sorted(OPEN_LICENSES))
_NC_LICENSE_SQL = ", ".join(f"'{value}'" for value in sorted(NC_LICENSES))
_UE_ONLY_LICENSE_SQL = ", ".join(f"'{value}'" for value in sorted(UE_ONLY_LICENSES))
_ALL_LICENSE_SQL = ", ".join(f"'{value}'" for value in sorted(SUPPORTED_LICENSES))

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        f"""
        CREATE TABLE assets (
            asset_id TEXT PRIMARY KEY
                CHECK(length(asset_id) BETWEEN 1 AND 64)
                CHECK(substr(asset_id, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(asset_id NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(asset_id, '__') = 0)
                CHECK(substr(asset_id, -1, 1) <> '_'),
            name TEXT NOT NULL CHECK(length(trim(name)) > 0 AND name = trim(name)),
            source TEXT NOT NULL
                CHECK(length(source) BETWEEN 1 AND 64)
                CHECK(substr(source, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(source NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(source, '__') = 0)
                CHECK(substr(source, -1, 1) <> '_'),
            source_id TEXT NOT NULL
                CHECK(length(trim(source_id)) > 0 AND source_id = trim(source_id)),
            source_url TEXT NOT NULL
                CHECK(source_url = trim(source_url))
                CHECK(source_url LIKE 'http://%' OR source_url LIKE 'https://%'
                      OR source_url LIKE 'file://%' OR source_url LIKE 'urn:%'),
            license TEXT NOT NULL CHECK(license IN ({_ALL_LICENSE_SQL})),
            license_tier TEXT NOT NULL CHECK(license_tier IN ('open', 'nc', 'ue-only')),
            license_url TEXT NOT NULL
                CHECK(license_url = trim(license_url))
                CHECK(license_url LIKE 'http://%' OR license_url LIKE 'https://%'
                      OR license_url LIKE 'file://%' OR license_url LIKE 'urn:%'),
            attribution TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('raw', 'imported', 'render_ok', 'failed')),
            tags_json TEXT NOT NULL DEFAULT '[]'
                CHECK(json_valid(tags_json) AND json_type(tags_json) = 'array'),
            raw_path TEXT NOT NULL CHECK({_ASSET_PATH_CHECK}),
            ue_package_path TEXT
                CHECK(ue_package_path IS NULL OR (
                    ue_package_path LIKE '/Game/%'
                    AND instr(ue_package_path, '\\') = 0
                    AND ue_package_path NOT LIKE '%//%'
                    AND ue_package_path NOT LIKE '%/../%'
                    AND ue_package_path NOT LIKE '%/./%'
                    AND ue_package_path NOT LIKE '%/..'
                    AND ue_package_path NOT LIKE '%/.'
                )),
            tri_count INTEGER
                CHECK(tri_count IS NULL OR (typeof(tri_count) = 'integer' AND tri_count >= 0)),
            material_count INTEGER
                CHECK(material_count IS NULL
                      OR (typeof(material_count) = 'integer' AND material_count >= 0)),
            sha256 TEXT NOT NULL UNIQUE
                CHECK(length(sha256) = 64)
                CHECK(sha256 = lower(sha256))
                CHECK(sha256 NOT GLOB '*[^0-9a-f]*'),
            error_json TEXT
                CHECK(CASE WHEN error_json IS NULL THEN 1
                           ELSE json_valid(error_json) AND json_type(error_json) = 'object' END),
            created_at TEXT NOT NULL
                CHECK(created_at GLOB '????-??-??T??:??:??Z')
                CHECK(strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS NOT NULL
                      AND strftime('%Y-%m-%dT%H:%M:%SZ', created_at) = created_at),
            updated_at TEXT NOT NULL
                CHECK(updated_at GLOB '????-??-??T??:??:??Z')
                CHECK(strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS NOT NULL
                      AND strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) = updated_at)
                CHECK(updated_at >= created_at),
            CHECK(
                (license_tier = 'open' AND license IN ({_OPEN_LICENSE_SQL}))
                OR (license_tier = 'nc' AND license IN ({_NC_LICENSE_SQL}))
                OR (license_tier = 'ue-only' AND license IN ({_UE_ONLY_LICENSE_SQL}))
            ),
            CHECK((status = 'failed' AND error_json IS NOT NULL)
                  OR (status <> 'failed' AND error_json IS NULL)),
            CHECK(status <> 'raw' OR (
                ue_package_path IS NULL AND tri_count IS NULL AND material_count IS NULL
            )),
            CHECK(status NOT IN ('imported', 'render_ok')
                  OR (ue_package_path IS NOT NULL AND tri_count > 0))
        )
        """,
        f"""
        CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY
                CHECK(length(artifact_id) BETWEEN 1 AND 96)
                CHECK(substr(artifact_id, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(artifact_id NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(artifact_id, '__') = 0)
                CHECK(substr(artifact_id, -1, 1) <> '_'),
            asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
            kind TEXT NOT NULL
                CHECK(length(kind) BETWEEN 1 AND 96)
                CHECK(substr(kind, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(kind NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(kind, '__') = 0)
                CHECK(substr(kind, -1, 1) <> '_'),
            path TEXT NOT NULL CHECK({_ARTIFACT_PATH_CHECK}),
            params_json TEXT NOT NULL DEFAULT '{{}}'
                CHECK(json_valid(params_json) AND json_type(params_json) = 'object'),
            sha256 TEXT
                CHECK(sha256 IS NULL OR (
                    length(sha256) = 64
                    AND sha256 = lower(sha256)
                    AND sha256 NOT GLOB '*[^0-9a-f]*'
                )),
            created_at TEXT NOT NULL
                CHECK(created_at GLOB '????-??-??T??:??:??Z')
                CHECK(strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS NOT NULL
                      AND strftime('%Y-%m-%dT%H:%M:%SZ', created_at) = created_at),
            UNIQUE(asset_id, kind, path)
        )
        """,
        "CREATE INDEX assets_status_idx ON assets(status)",
        "CREATE INDEX assets_source_idx ON assets(source)",
        "CREATE INDEX assets_license_idx ON assets(license)",
        "CREATE INDEX artifacts_asset_idx ON artifacts(asset_id)",
    ),
    2: (
        f"""
        CREATE TABLE scenes (
            scene_id TEXT PRIMARY KEY
                CHECK(length(scene_id) BETWEEN 1 AND 64)
                CHECK(substr(scene_id, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(scene_id NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(scene_id, '__') = 0)
                CHECK(substr(scene_id, -1, 1) <> '_'),
            name TEXT NOT NULL CHECK(length(trim(name)) > 0 AND name = trim(name)),
            source TEXT NOT NULL
                CHECK(length(source) BETWEEN 1 AND 64)
                CHECK(substr(source, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(source NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(source, '__') = 0)
                CHECK(substr(source, -1, 1) <> '_'),
            source_id TEXT NOT NULL
                CHECK(length(trim(source_id)) > 0 AND source_id = trim(source_id)),
            source_url TEXT NOT NULL
                CHECK(source_url = trim(source_url))
                CHECK(source_url LIKE 'http://%' OR source_url LIKE 'https://%'
                      OR source_url LIKE 'file://%' OR source_url LIKE 'urn:%'),
            license TEXT NOT NULL CHECK(license IN ({_ALL_LICENSE_SQL})),
            license_tier TEXT NOT NULL CHECK(license_tier IN ('open', 'nc', 'ue-only')),
            license_url TEXT NOT NULL
                CHECK(license_url = trim(license_url))
                CHECK(license_url LIKE 'http://%' OR license_url LIKE 'https://%'
                      OR license_url LIKE 'file://%' OR license_url LIKE 'urn:%'),
            attribution TEXT NOT NULL DEFAULT '',
            source_path TEXT NOT NULL CHECK({_SCENE_SOURCE_PATH_CHECK}),
            source_sha256 TEXT NOT NULL
                CHECK(length(source_sha256) = 64)
                CHECK(source_sha256 = lower(source_sha256))
                CHECK(source_sha256 NOT GLOB '*[^0-9a-f]*'),
            spec_sha256 TEXT NOT NULL
                CHECK(length(spec_sha256) = 64)
                CHECK(spec_sha256 = lower(spec_sha256))
                CHECK(spec_sha256 NOT GLOB '*[^0-9a-f]*'),
            status TEXT NOT NULL
                CHECK(status IN ('raw', 'built', 'render_ok', 'failed', 'quarantined')),
            map_path TEXT
                CHECK(map_path IS NULL OR map_path =
                      '/Game/UEF/Scenes/' || scene_id || '/L_' || scene_id),
            actor_count INTEGER
                CHECK(actor_count IS NULL
                      OR (typeof(actor_count) = 'integer' AND actor_count >= 0)),
            static_mesh_count INTEGER
                CHECK(static_mesh_count IS NULL
                      OR (typeof(static_mesh_count) = 'integer' AND static_mesh_count >= 0)),
            triangle_count INTEGER
                CHECK(triangle_count IS NULL
                      OR (typeof(triangle_count) = 'integer' AND triangle_count >= 0)),
            material_count INTEGER
                CHECK(material_count IS NULL
                      OR (typeof(material_count) = 'integer' AND material_count >= 0)),
            bounds_json TEXT
                CHECK(CASE WHEN bounds_json IS NULL THEN 1
                           ELSE json_valid(bounds_json) AND json_type(bounds_json) = 'object' END),
            error_json TEXT
                CHECK(CASE WHEN error_json IS NULL THEN 1
                           ELSE json_valid(error_json) AND json_type(error_json) = 'object' END),
            created_at TEXT NOT NULL
                CHECK(created_at GLOB '????-??-??T??:??:??Z')
                CHECK(strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS NOT NULL
                      AND strftime('%Y-%m-%dT%H:%M:%SZ', created_at) = created_at),
            updated_at TEXT NOT NULL
                CHECK(updated_at GLOB '????-??-??T??:??:??Z')
                CHECK(strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS NOT NULL
                      AND strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) = updated_at)
                CHECK(updated_at >= created_at),
            CHECK(
                (license_tier = 'open' AND license IN ({_OPEN_LICENSE_SQL}))
                OR (license_tier = 'nc' AND license IN ({_NC_LICENSE_SQL}))
                OR (license_tier = 'ue-only' AND license IN ({_UE_ONLY_LICENSE_SQL}))
            ),
            CHECK((status IN ('failed', 'quarantined') AND error_json IS NOT NULL)
                  OR (status NOT IN ('failed', 'quarantined') AND error_json IS NULL)),
            CHECK(status NOT IN ('raw', 'quarantined') OR (
                map_path IS NULL AND actor_count IS NULL AND static_mesh_count IS NULL
                AND triangle_count IS NULL AND material_count IS NULL AND bounds_json IS NULL
            )),
            CHECK(status NOT IN ('built', 'render_ok') OR (
                map_path IS NOT NULL AND actor_count > 0 AND static_mesh_count > 0
                AND static_mesh_count <= actor_count AND triangle_count > 0
                AND material_count IS NOT NULL AND bounds_json IS NOT NULL
            )),
            CHECK(status <> 'failed' OR (
                (map_path IS NULL AND actor_count IS NULL AND static_mesh_count IS NULL
                 AND triangle_count IS NULL AND material_count IS NULL AND bounds_json IS NULL)
                OR
                (map_path IS NOT NULL AND actor_count > 0 AND static_mesh_count > 0
                 AND static_mesh_count <= actor_count AND triangle_count > 0
                 AND material_count IS NOT NULL AND bounds_json IS NOT NULL)
            ))
        )
        """,
        """
        CREATE TABLE scene_objects (
            object_id TEXT PRIMARY KEY
                CHECK(length(object_id) BETWEEN 1 AND 96)
                CHECK(substr(object_id, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(object_id NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(object_id, '__') = 0)
                CHECK(substr(object_id, -1, 1) <> '_'),
            scene_id TEXT NOT NULL REFERENCES scenes(scene_id) ON DELETE CASCADE,
            actor_name TEXT NOT NULL
                CHECK(length(trim(actor_name)) > 0 AND actor_name = trim(actor_name)),
            actor_class TEXT NOT NULL
                CHECK(length(trim(actor_class)) > 0 AND actor_class = trim(actor_class)),
            mesh_path TEXT
                CHECK(mesh_path IS NULL OR (
                    mesh_path LIKE '/Game/%'
                    AND instr(mesh_path, '\\') = 0
                    AND mesh_path NOT LIKE '%//%'
                    AND mesh_path NOT LIKE '%/../%'
                    AND mesh_path NOT LIKE '%/./%'
                    AND mesh_path NOT LIKE '%/..'
                    AND mesh_path NOT LIKE '%/.'
                )),
            transform_json TEXT NOT NULL
                CHECK(json_valid(transform_json) AND json_type(transform_json) = 'object'),
            bounds_json TEXT
                CHECK(CASE WHEN bounds_json IS NULL THEN 1
                           ELSE json_valid(bounds_json) AND json_type(bounds_json) = 'object' END),
            triangle_count INTEGER
                CHECK(triangle_count IS NULL
                      OR (typeof(triangle_count) = 'integer' AND triangle_count >= 0)),
            material_count INTEGER
                CHECK(material_count IS NULL
                      OR (typeof(material_count) = 'integer' AND material_count >= 0)),
            created_at TEXT NOT NULL
                CHECK(created_at GLOB '????-??-??T??:??:??Z')
                CHECK(strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS NOT NULL
                      AND strftime('%Y-%m-%dT%H:%M:%SZ', created_at) = created_at),
            CHECK((mesh_path IS NULL AND triangle_count IS NULL AND material_count IS NULL)
                  OR (mesh_path IS NOT NULL AND triangle_count > 0
                      AND material_count IS NOT NULL))
        )
        """,
        f"""
        CREATE TABLE scene_artifacts (
            artifact_id TEXT PRIMARY KEY
                CHECK(length(artifact_id) BETWEEN 1 AND 96)
                CHECK(substr(artifact_id, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(artifact_id NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(artifact_id, '__') = 0)
                CHECK(substr(artifact_id, -1, 1) <> '_'),
            scene_id TEXT NOT NULL REFERENCES scenes(scene_id) ON DELETE CASCADE,
            kind TEXT NOT NULL
                CHECK(length(kind) BETWEEN 1 AND 96)
                CHECK(substr(kind, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(kind NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(kind, '__') = 0)
                CHECK(substr(kind, -1, 1) <> '_'),
            path TEXT NOT NULL CHECK({_ARTIFACT_PATH_CHECK}),
            params_json TEXT NOT NULL DEFAULT '{{}}'
                CHECK(json_valid(params_json) AND json_type(params_json) = 'object'),
            sha256 TEXT
                CHECK(sha256 IS NULL OR (
                    length(sha256) = 64
                    AND sha256 = lower(sha256)
                    AND sha256 NOT GLOB '*[^0-9a-f]*'
                )),
            created_at TEXT NOT NULL
                CHECK(created_at GLOB '????-??-??T??:??:??Z')
                CHECK(strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS NOT NULL
                      AND strftime('%Y-%m-%dT%H:%M:%SZ', created_at) = created_at),
            UNIQUE(scene_id, kind, path)
        )
        """,
        "CREATE INDEX scenes_status_idx ON scenes(status)",
        "CREATE INDEX scenes_source_idx ON scenes(source)",
        "CREATE INDEX scenes_license_idx ON scenes(license)",
        "CREATE INDEX scene_objects_scene_idx ON scene_objects(scene_id)",
        "CREATE INDEX scene_artifacts_scene_idx ON scene_artifacts(scene_id)",
    ),
    3: (
        """
        ALTER TABLE scenes ADD COLUMN source_file TEXT
            CHECK(source_file IS NULL OR (
                length(trim(source_file)) > 0
                AND source_file = trim(source_file)
                AND instr(source_file, '\\') = 0
                AND instr(source_file, '//') = 0
                AND instr(source_file, '/../') = 0
                AND instr(source_file, '/./') = 0
                AND substr(source_file, -3) <> '/..'
                AND substr(source_file, -2) <> '/.'
            ))
        """,
        """
        ALTER TABLE scenes ADD COLUMN build_sha256 TEXT
            CHECK(build_sha256 IS NULL OR (
                length(build_sha256) = 64
                AND build_sha256 = lower(build_sha256)
                AND build_sha256 NOT GLOB '*[^0-9a-f]*'
            ))
        """,
        """
        ALTER TABLE scenes ADD COLUMN texture_count INTEGER
            CHECK(texture_count IS NULL OR (
                typeof(texture_count) = 'integer' AND texture_count >= 0
            ))
        """,
        """
        UPDATE scenes
        SET source_file = COALESCE(
            (
                SELECT json_extract(scene_artifacts.params_json, '$.source_file')
                FROM scene_artifacts
                WHERE scene_artifacts.scene_id = scenes.scene_id
                  AND scene_artifacts.kind = 'scene_build_manifest'
                ORDER BY scene_artifacts.created_at DESC
                LIMIT 1
            ),
            source_path
        )
        """,
        """
        UPDATE scenes
        SET build_sha256 = (
            SELECT scene_artifacts.sha256
            FROM scene_artifacts
            WHERE scene_artifacts.scene_id = scenes.scene_id
              AND scene_artifacts.kind = 'scene_build_manifest'
              AND scene_artifacts.sha256 IS NOT NULL
            ORDER BY scene_artifacts.created_at DESC
            LIMIT 1
        )
        """,
        """
        UPDATE scenes
        SET texture_count = COALESCE(
            (
                SELECT CAST(json_extract(scene_artifacts.params_json, '$.texture_count') AS INTEGER)
                FROM scene_artifacts
                WHERE scene_artifacts.scene_id = scenes.scene_id
                  AND scene_artifacts.kind = 'scene_build_manifest'
                ORDER BY scene_artifacts.created_at DESC
                LIMIT 1
            ),
            0
        )
        """,
        """
        CREATE TRIGGER scenes_v3_build_fields_insert
        BEFORE INSERT ON scenes
        WHEN (
            NEW.status IN ('built', 'render_ok')
            OR (NEW.status = 'failed' AND NEW.map_path IS NOT NULL)
        ) AND (
            NEW.source_file IS NULL
            OR NEW.build_sha256 IS NULL
            OR NEW.texture_count IS NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'complete scene builds require v3 build fields');
        END
        """,
        """
        CREATE TRIGGER scenes_v3_build_fields_update
        BEFORE UPDATE ON scenes
        WHEN (
            NEW.status IN ('built', 'render_ok')
            OR (NEW.status = 'failed' AND NEW.map_path IS NOT NULL)
        ) AND (
            NEW.source_file IS NULL
            OR NEW.build_sha256 IS NULL
            OR NEW.texture_count IS NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'complete scene builds require v3 build fields');
        END
        """,
        "UPDATE scenes SET status = status",
    ),
    4: (
        f"""
        CREATE TABLE resources (
            resource_id TEXT PRIMARY KEY
                CHECK(length(resource_id) BETWEEN 1 AND 64)
                CHECK(substr(resource_id, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(resource_id NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(resource_id, '__') = 0)
                CHECK(substr(resource_id, -1, 1) <> '_'),
            resource_kind TEXT NOT NULL CHECK(resource_kind IN ('hdri', 'pbr_texture_set')),
            profile TEXT NOT NULL
                CHECK(length(profile) BETWEEN 1 AND 64)
                CHECK(substr(profile, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(profile NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(profile, '__') = 0)
                CHECK(substr(profile, -1, 1) <> '_'),
            resolution TEXT NOT NULL
                CHECK(length(resolution) BETWEEN 1 AND 32)
                CHECK(resolution = lower(resolution))
                CHECK(resolution NOT GLOB '*[^a-z0-9_-]*')
                CHECK(substr(resolution, 1, 1) GLOB '[a-z0-9]')
                CHECK(substr(resolution, -1, 1) NOT IN ('_', '-')),
            name TEXT NOT NULL CHECK(length(trim(name)) > 0 AND name = trim(name)),
            source TEXT NOT NULL
                CHECK(length(source) BETWEEN 1 AND 64)
                CHECK(substr(source, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(source NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(source, '__') = 0)
                CHECK(substr(source, -1, 1) <> '_'),
            source_id TEXT NOT NULL
                CHECK(length(trim(source_id)) > 0 AND source_id = trim(source_id)),
            source_url TEXT NOT NULL
                CHECK(source_url = trim(source_url))
                CHECK(source_url LIKE 'http://%' OR source_url LIKE 'https://%'
                      OR source_url LIKE 'file://%' OR source_url LIKE 'urn:%'),
            source_revision TEXT NOT NULL
                CHECK(length(source_revision) BETWEEN 1 AND 128)
                CHECK(source_revision = trim(source_revision)),
            source_revision_scheme TEXT NOT NULL
                CHECK(length(source_revision_scheme) BETWEEN 1 AND 64)
                CHECK(substr(source_revision_scheme, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(source_revision_scheme NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(source_revision_scheme, '__') = 0)
                CHECK(substr(source_revision_scheme, -1, 1) <> '_'),
            license TEXT NOT NULL CHECK(license IN ({_ALL_LICENSE_SQL})),
            license_tier TEXT NOT NULL CHECK(license_tier IN ('open', 'nc', 'ue-only')),
            license_url TEXT NOT NULL
                CHECK(license_url = trim(license_url))
                CHECK(license_url LIKE 'http://%' OR license_url LIKE 'https://%'
                      OR license_url LIKE 'file://%' OR license_url LIKE 'urn:%'),
            attribution TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('verified', 'ready', 'failed', 'quarantined')),
            tags_json TEXT NOT NULL DEFAULT '[]'
                CHECK(json_valid(tags_json) AND json_type(tags_json) = 'array'),
            bundle_sha256 TEXT
                CHECK(bundle_sha256 IS NULL OR (
                    length(bundle_sha256) = 64 AND bundle_sha256 = lower(bundle_sha256)
                    AND bundle_sha256 NOT GLOB '*[^0-9a-f]*'
                )),
            content_sha256 TEXT
                CHECK(content_sha256 IS NULL OR (
                    length(content_sha256) = 64 AND content_sha256 = lower(content_sha256)
                    AND content_sha256 NOT GLOB '*[^0-9a-f]*'
                )),
            physical_width_mm REAL
                CHECK(physical_width_mm IS NULL OR (
                    typeof(physical_width_mm) IN ('integer', 'real') AND physical_width_mm > 0
                )),
            physical_height_mm REAL
                CHECK(physical_height_mm IS NULL OR (
                    typeof(physical_height_mm) IN ('integer', 'real') AND physical_height_mm > 0
                )),
            error_json TEXT
                CHECK(CASE WHEN error_json IS NULL THEN 1
                           ELSE json_valid(error_json) AND json_type(error_json) = 'object' END),
            created_at TEXT NOT NULL
                CHECK(created_at GLOB '????-??-??T??:??:??Z')
                CHECK(strftime('%Y-%m-%dT%H:%M:%SZ', created_at) = created_at),
            updated_at TEXT NOT NULL
                CHECK(updated_at GLOB '????-??-??T??:??:??Z')
                CHECK(strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) = updated_at)
                CHECK(updated_at >= created_at),
            UNIQUE(source, resource_kind, source_id, source_revision, profile, resolution),
            CHECK(
                (license_tier = 'open' AND license IN ({_OPEN_LICENSE_SQL}))
                OR (license_tier = 'nc' AND license IN ({_NC_LICENSE_SQL}))
                OR (license_tier = 'ue-only' AND license IN ({_UE_ONLY_LICENSE_SQL}))
            ),
            CHECK((status IN ('failed', 'quarantined') AND error_json IS NOT NULL)
                  OR (status NOT IN ('failed', 'quarantined') AND error_json IS NULL)),
            CHECK(status NOT IN ('verified', 'ready')
                  OR (bundle_sha256 IS NOT NULL AND content_sha256 IS NOT NULL)),
            CHECK((physical_width_mm IS NULL) = (physical_height_mm IS NULL)),
            CHECK(resource_kind <> 'hdri'
                  OR (physical_width_mm IS NULL AND physical_height_mm IS NULL)),
            CHECK(resource_kind <> 'pbr_texture_set' OR status <> 'ready'
                  OR (physical_width_mm IS NOT NULL AND physical_height_mm IS NOT NULL))
        )
        """,
        f"""
        CREATE TABLE resource_files (
            file_id TEXT PRIMARY KEY
                CHECK(length(file_id) BETWEEN 1 AND 64)
                CHECK(substr(file_id, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(file_id NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(file_id, '__') = 0)
                CHECK(substr(file_id, -1, 1) <> '_'),
            resource_id TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
            semantic_role TEXT NOT NULL
                CHECK(length(semantic_role) BETWEEN 1 AND 64)
                CHECK(substr(semantic_role, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(semantic_role NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(semantic_role, '__') = 0)
                CHECK(substr(semantic_role, -1, 1) <> '_'),
            provider_role TEXT NOT NULL
                CHECK(length(provider_role) BETWEEN 1 AND 128)
                CHECK(provider_role = trim(provider_role)),
            resolution TEXT NOT NULL
                CHECK(length(resolution) BETWEEN 1 AND 32)
                CHECK(resolution = lower(resolution))
                CHECK(resolution NOT GLOB '*[^a-z0-9_-]*')
                CHECK(substr(resolution, 1, 1) GLOB '[a-z0-9]')
                CHECK(substr(resolution, -1, 1) NOT IN ('_', '-')),
            format TEXT NOT NULL
                CHECK(length(format) BETWEEN 1 AND 64)
                CHECK(substr(format, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(format NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(format, '__') = 0)
                CHECK(substr(format, -1, 1) <> '_'),
            path TEXT NOT NULL CHECK({_ARTIFACT_PATH_CHECK}),
            source_url TEXT NOT NULL
                CHECK(source_url = trim(source_url))
                CHECK(source_url LIKE 'http://%' OR source_url LIKE 'https://%'
                      OR source_url LIKE 'file://%' OR source_url LIKE 'urn:%'),
            byte_size INTEGER NOT NULL CHECK(typeof(byte_size) = 'integer' AND byte_size > 0),
            provider_md5 TEXT
                CHECK(provider_md5 IS NULL OR (
                    length(provider_md5) = 32 AND provider_md5 = lower(provider_md5)
                    AND provider_md5 NOT GLOB '*[^0-9a-f]*'
                )),
            sha256 TEXT NOT NULL
                CHECK(length(sha256) = 64 AND sha256 = lower(sha256)
                      AND sha256 NOT GLOB '*[^0-9a-f]*'),
            color_space TEXT NOT NULL CHECK(color_space IN ('srgb', 'linear', 'data')),
            normal_convention TEXT CHECK(normal_convention IN ('opengl', 'directx')),
            channels_json TEXT NOT NULL DEFAULT '{{}}'
                CHECK(json_valid(channels_json) AND json_type(channels_json) = 'object'),
            width INTEGER CHECK(width IS NULL OR (typeof(width) = 'integer' AND width > 0)),
            height INTEGER CHECK(height IS NULL OR (typeof(height) = 'integer' AND height > 0)),
            is_primary INTEGER NOT NULL CHECK(is_primary IN (0, 1)),
            params_json TEXT NOT NULL DEFAULT '{{}}'
                CHECK(json_valid(params_json) AND json_type(params_json) = 'object'),
            created_at TEXT NOT NULL
                CHECK(created_at GLOB '????-??-??T??:??:??Z')
                CHECK(strftime('%Y-%m-%dT%H:%M:%SZ', created_at) = created_at),
            UNIQUE(resource_id, path),
            CHECK((width IS NULL) = (height IS NULL))
        )
        """,
        f"""
        CREATE TABLE resource_artifacts (
            artifact_id TEXT PRIMARY KEY
                CHECK(length(artifact_id) BETWEEN 1 AND 96)
                CHECK(substr(artifact_id, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(artifact_id NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(artifact_id, '__') = 0)
                CHECK(substr(artifact_id, -1, 1) <> '_'),
            resource_id TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
            kind TEXT NOT NULL
                CHECK(length(kind) BETWEEN 1 AND 96)
                CHECK(substr(kind, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(kind NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(kind, '__') = 0)
                CHECK(substr(kind, -1, 1) <> '_'),
            path TEXT NOT NULL CHECK({_ARTIFACT_PATH_CHECK}),
            params_json TEXT NOT NULL DEFAULT '{{}}'
                CHECK(json_valid(params_json) AND json_type(params_json) = 'object'),
            sha256 TEXT
                CHECK(sha256 IS NULL OR (
                    length(sha256) = 64 AND sha256 = lower(sha256)
                    AND sha256 NOT GLOB '*[^0-9a-f]*'
                )),
            created_at TEXT NOT NULL
                CHECK(created_at GLOB '????-??-??T??:??:??Z')
                CHECK(strftime('%Y-%m-%dT%H:%M:%SZ', created_at) = created_at),
            UNIQUE(resource_id, kind, path)
        )
        """,
        """
        CREATE TABLE resource_bindings (
            binding_id TEXT PRIMARY KEY
                CHECK(length(binding_id) BETWEEN 1 AND 64)
                CHECK(substr(binding_id, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(binding_id NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(binding_id, '__') = 0)
                CHECK(substr(binding_id, -1, 1) <> '_'),
            resource_id TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
            role TEXT NOT NULL
                CHECK(length(role) BETWEEN 1 AND 64)
                CHECK(substr(role, 1, 1) BETWEEN 'a' AND 'z')
                CHECK(role NOT GLOB '*[^a-z0-9_]*')
                CHECK(instr(role, '__') = 0)
                CHECK(substr(role, -1, 1) <> '_'),
            asset_id TEXT REFERENCES assets(asset_id) ON DELETE CASCADE,
            scene_id TEXT REFERENCES scenes(scene_id) ON DELETE CASCADE,
            consumer_resource_id TEXT REFERENCES resources(resource_id) ON DELETE CASCADE,
            params_json TEXT NOT NULL DEFAULT '{}'
                CHECK(json_valid(params_json) AND json_type(params_json) = 'object'),
            created_at TEXT NOT NULL
                CHECK(created_at GLOB '????-??-??T??:??:??Z')
                CHECK(strftime('%Y-%m-%dT%H:%M:%SZ', created_at) = created_at),
            CHECK((asset_id IS NOT NULL) + (scene_id IS NOT NULL)
                  + (consumer_resource_id IS NOT NULL) = 1),
            CHECK(consumer_resource_id IS NULL OR consumer_resource_id <> resource_id)
        )
        """,
        "CREATE INDEX resources_kind_idx ON resources(resource_kind)",
        "CREATE INDEX resources_status_idx ON resources(status)",
        "CREATE INDEX resources_source_idx ON resources(source)",
        "CREATE INDEX resources_license_idx ON resources(license)",
        "CREATE INDEX resource_files_resource_idx ON resource_files(resource_id)",
        "CREATE INDEX resource_files_role_idx ON resource_files(semantic_role)",
        "CREATE UNIQUE INDEX resource_files_primary_idx ON resource_files(resource_id) "
        "WHERE is_primary = 1",
        "CREATE INDEX resource_artifacts_resource_idx ON resource_artifacts(resource_id)",
        "CREATE INDEX resource_bindings_resource_idx ON resource_bindings(resource_id)",
        "CREATE UNIQUE INDEX resource_bindings_asset_role_idx "
        "ON resource_bindings(asset_id, role) WHERE asset_id IS NOT NULL",
        "CREATE UNIQUE INDEX resource_bindings_scene_role_idx "
        "ON resource_bindings(scene_id, role) WHERE scene_id IS NOT NULL",
        "CREATE UNIQUE INDEX resource_bindings_consumer_role_idx "
        "ON resource_bindings(consumer_resource_id, role) "
        "WHERE consumer_resource_id IS NOT NULL",
    ),
    5: (
        """
        ALTER TABLE resources ADD COLUMN published_once INTEGER NOT NULL DEFAULT 0
            CHECK(published_once IN (0, 1))
        """,
        """
        UPDATE resources
        SET published_once = 1
        WHERE status IN ('verified', 'ready')
        """,
        """
        UPDATE resources
        SET published_once = 1
        WHERE status IN ('failed', 'quarantined')
          AND bundle_sha256 IS NOT NULL
          AND content_sha256 IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM resource_files
              WHERE resource_files.resource_id = resources.resource_id
                AND resource_files.is_primary = 1
          )
          AND EXISTS (
              SELECT 1 FROM resource_artifacts
              WHERE resource_artifacts.resource_id = resources.resource_id
                AND resource_artifacts.kind = 'resource_source_manifest'
                AND resource_artifacts.sha256 IS NOT NULL
                AND json_extract(resource_artifacts.params_json, '$.resource_id')
                    = resources.resource_id
                AND json_extract(resource_artifacts.params_json, '$.bundle_sha256')
                    = resources.bundle_sha256
                AND json_extract(resource_artifacts.params_json, '$.content_sha256')
                    = resources.content_sha256
          )
        """,
        """
        CREATE TRIGGER resources_published_once_insert
        BEFORE INSERT ON resources
        WHEN NEW.status IN ('verified', 'ready') AND NEW.published_once <> 1
        BEGIN
            SELECT RAISE(ABORT, 'published resources require publication lineage');
        END
        """,
        """
        CREATE TRIGGER resources_published_once_update
        BEFORE UPDATE ON resources
        WHEN (OLD.published_once = 1 AND NEW.published_once <> 1)
          OR (NEW.status IN ('verified', 'ready') AND NEW.published_once <> 1)
        BEGIN
            SELECT RAISE(ABORT, 'resource publication lineage is append-only');
        END
        """,
    ),
}

_UPSERT_ASSET_SQL = """
INSERT INTO assets (
    asset_id, name, source, source_id, source_url, license, license_tier, license_url,
    attribution, status, tags_json, raw_path, ue_package_path, tri_count, material_count,
    sha256, error_json, created_at, updated_at
) VALUES (
    :asset_id, :name, :source, :source_id, :source_url, :license, :license_tier, :license_url,
    :attribution, :status, :tags_json, :raw_path, :ue_package_path, :tri_count, :material_count,
    :sha256, :error_json, :created_at, :updated_at
)
ON CONFLICT(asset_id) DO UPDATE SET
    name = excluded.name,
    source = excluded.source,
    source_id = excluded.source_id,
    source_url = excluded.source_url,
    license = excluded.license,
    license_tier = excluded.license_tier,
    license_url = excluded.license_url,
    attribution = excluded.attribution,
    status = excluded.status,
    tags_json = excluded.tags_json,
    raw_path = excluded.raw_path,
    ue_package_path = excluded.ue_package_path,
    tri_count = excluded.tri_count,
    material_count = excluded.material_count,
    error_json = excluded.error_json,
    updated_at = excluded.updated_at
"""

_UPSERT_ARTIFACT_SQL = """
INSERT INTO artifacts (
    artifact_id, asset_id, kind, path, params_json, sha256, created_at
) VALUES (
    :artifact_id, :asset_id, :kind, :path, :params_json, :sha256, :created_at
)
ON CONFLICT(artifact_id) DO UPDATE SET
    params_json = excluded.params_json,
    sha256 = excluded.sha256
"""

_UPSERT_RESOURCE_SQL = """
INSERT INTO resources (
    resource_id, resource_kind, profile, resolution, name, source, source_id, source_url,
    source_revision, source_revision_scheme, license, license_tier, license_url, attribution,
    status, tags_json, bundle_sha256, content_sha256, physical_width_mm, physical_height_mm,
    error_json, created_at, updated_at, published_once
) VALUES (
    :resource_id, :resource_kind, :profile, :resolution, :name, :source, :source_id, :source_url,
    :source_revision, :source_revision_scheme, :license, :license_tier, :license_url,
    :attribution, :status, :tags_json, :bundle_sha256, :content_sha256, :physical_width_mm,
    :physical_height_mm, :error_json, :created_at, :updated_at, :published_once
)
ON CONFLICT(resource_id) DO UPDATE SET
    name = excluded.name,
    status = excluded.status,
    tags_json = excluded.tags_json,
    bundle_sha256 = COALESCE(resources.bundle_sha256, excluded.bundle_sha256),
    content_sha256 = COALESCE(resources.content_sha256, excluded.content_sha256),
    physical_width_mm = COALESCE(resources.physical_width_mm, excluded.physical_width_mm),
    physical_height_mm = COALESCE(resources.physical_height_mm, excluded.physical_height_mm),
    published_once = max(resources.published_once, excluded.published_once),
    error_json = excluded.error_json,
    updated_at = excluded.updated_at
"""

_UPSERT_RESOURCE_FILE_SQL = """
INSERT INTO resource_files (
    file_id, resource_id, semantic_role, provider_role, resolution, format, path, source_url,
    byte_size, provider_md5, sha256, color_space, normal_convention, channels_json, width,
    height, is_primary, params_json, created_at
) VALUES (
    :file_id, :resource_id, :semantic_role, :provider_role, :resolution, :format, :path,
    :source_url, :byte_size, :provider_md5, :sha256, :color_space, :normal_convention,
    :channels_json, :width, :height, :is_primary, :params_json, :created_at
)
ON CONFLICT(file_id) DO UPDATE SET
    params_json = excluded.params_json
"""

_UPSERT_RESOURCE_ARTIFACT_SQL = """
INSERT INTO resource_artifacts (
    artifact_id, resource_id, kind, path, params_json, sha256, created_at
) VALUES (
    :artifact_id, :resource_id, :kind, :path, :params_json, :sha256, :created_at
)
ON CONFLICT(artifact_id) DO UPDATE SET
    params_json = excluded.params_json,
    sha256 = excluded.sha256
"""

_UPSERT_RESOURCE_BINDING_SQL = """
INSERT INTO resource_bindings (
    binding_id, resource_id, role, asset_id, scene_id, consumer_resource_id, params_json,
    created_at
) VALUES (
    :binding_id, :resource_id, :role, :asset_id, :scene_id, :consumer_resource_id,
    :params_json, :created_at
)
ON CONFLICT(binding_id) DO UPDATE SET
    params_json = excluded.params_json
"""

_UPSERT_SCENE_SQL = """
INSERT INTO scenes (
    scene_id, name, source, source_id, source_url, license, license_tier, license_url,
    attribution, source_path, source_file, source_sha256, spec_sha256, build_sha256, status,
    map_path, actor_count, static_mesh_count, triangle_count, material_count, texture_count,
    bounds_json, error_json, created_at, updated_at
) VALUES (
    :scene_id, :name, :source, :source_id, :source_url, :license, :license_tier, :license_url,
    :attribution, :source_path, :source_file, :source_sha256, :spec_sha256, :build_sha256,
    :status, :map_path, :actor_count, :static_mesh_count, :triangle_count, :material_count,
    :texture_count, :bounds_json, :error_json, :created_at, :updated_at
)
ON CONFLICT(scene_id) DO UPDATE SET
    name = excluded.name,
    source = excluded.source,
    source_id = excluded.source_id,
    source_url = excluded.source_url,
    license = excluded.license,
    license_tier = excluded.license_tier,
    license_url = excluded.license_url,
    attribution = excluded.attribution,
    source_path = excluded.source_path,
    source_file = excluded.source_file,
    source_sha256 = excluded.source_sha256,
    spec_sha256 = excluded.spec_sha256,
    build_sha256 = excluded.build_sha256,
    status = excluded.status,
    map_path = excluded.map_path,
    actor_count = excluded.actor_count,
    static_mesh_count = excluded.static_mesh_count,
    triangle_count = excluded.triangle_count,
    material_count = excluded.material_count,
    texture_count = excluded.texture_count,
    bounds_json = excluded.bounds_json,
    error_json = excluded.error_json,
    updated_at = excluded.updated_at
"""

_UPSERT_SCENE_OBJECT_SQL = """
INSERT INTO scene_objects (
    object_id, scene_id, actor_name, actor_class, mesh_path, transform_json, bounds_json,
    triangle_count, material_count, created_at
) VALUES (
    :object_id, :scene_id, :actor_name, :actor_class, :mesh_path, :transform_json, :bounds_json,
    :triangle_count, :material_count, :created_at
)
ON CONFLICT(object_id) DO UPDATE SET
    actor_name = excluded.actor_name,
    actor_class = excluded.actor_class,
    mesh_path = excluded.mesh_path,
    transform_json = excluded.transform_json,
    bounds_json = excluded.bounds_json,
    triangle_count = excluded.triangle_count,
    material_count = excluded.material_count,
    created_at = excluded.created_at
"""

_UPSERT_SCENE_ARTIFACT_SQL = """
INSERT INTO scene_artifacts (
    artifact_id, scene_id, kind, path, params_json, sha256, created_at
) VALUES (
    :artifact_id, :scene_id, :kind, :path, :params_json, :sha256, :created_at
)
ON CONFLICT(artifact_id) DO UPDATE SET
    params_json = excluded.params_json,
    sha256 = excluded.sha256
"""
