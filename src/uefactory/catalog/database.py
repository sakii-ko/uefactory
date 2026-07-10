from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

SCHEMA_VERSION = 3
DEFAULT_BUSY_TIMEOUT_MS = 5_000

ASSET_STATUSES = frozenset({"raw", "imported", "render_ok", "failed"})
SCENE_STATUSES = frozenset({"raw", "built", "render_ok", "failed", "quarantined"})
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


def _reject_batch_duplicates(rows: tuple[dict[str, Any], ...], key: str) -> None:
    counts = Counter(str(row[key]) for row in rows)
    duplicates = sorted(value for value, count in counts.items() if count > 1)
    if duplicates:
        raise CatalogConflictError(f"duplicate {key} in transaction: {', '.join(duplicates)}")


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
