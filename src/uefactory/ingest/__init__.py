"""Strict asset manifest parsing and local bundle staging."""

from uefactory.ingest.executor import IngestResult, ingest_asset
from uefactory.ingest.pipeline import BatchAssetResult, BatchIngestResult, ingest_batch
from uefactory.ingest.source_structure import (
    SourceStructureError,
    SourceStructureEvidence,
    inspect_source_structure,
    is_valid_source_structure_evidence,
    source_structure_sha256,
)
from uefactory.ingest.spec import (
    LICENSE_TIERS,
    SUPPORTED_ASSET_EXTENSIONS,
    IngestAssetSpec,
    IngestBatchSpec,
    IngestNormalizationSpec,
    IngestSpecError,
    load_ingest_spec,
    parse_ingest_spec,
    validate_asset_id,
)
from uefactory.ingest.staging import (
    StagedAsset,
    StagingError,
    bundle_sha256,
    content_sha256,
    gltf_dependency_paths,
    stage_asset,
    stage_batch,
)

__all__ = [
    "LICENSE_TIERS",
    "SUPPORTED_ASSET_EXTENSIONS",
    "IngestAssetSpec",
    "IngestBatchSpec",
    "IngestNormalizationSpec",
    "IngestResult",
    "IngestSpecError",
    "StagedAsset",
    "StagingError",
    "SourceStructureError",
    "SourceStructureEvidence",
    "BatchAssetResult",
    "BatchIngestResult",
    "bundle_sha256",
    "content_sha256",
    "gltf_dependency_paths",
    "ingest_asset",
    "ingest_batch",
    "inspect_source_structure",
    "is_valid_source_structure_evidence",
    "load_ingest_spec",
    "parse_ingest_spec",
    "stage_asset",
    "stage_batch",
    "source_structure_sha256",
    "validate_asset_id",
]
