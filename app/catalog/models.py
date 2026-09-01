"""Shared constants and Pydantic models for the Step 10 catalog: artifact
types, canonical pipeline-stage ordering, the constrained lineage-edge
relationship vocabulary, and every API request/response shape.

Deliberately generic core fields (see ArtifactSummary) plus a free-form
`metadata_json` blob per artifact — Step 10 does not force every stage
into identical semantics; a package's metadata looks nothing like an
ingestion's, and that's fine.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

__all__ = [
    "ArtifactType",
    "STAGE_RANK",
    "RelationshipType",
    "VerificationStatus",
    "DatasetVersionStatus",
    "ACCEPTED_QC_STATUSES",
    "ARTIFACT_TYPES",
    "ArtifactRef",
    "ArtifactSummary",
    "LineageEdge",
    "ArtifactDetail",
    "ScanIssue",
    "ScanResult",
    "RebuildResult",
    "HealthIssue",
    "CatalogHealth",
    "LineageNode",
    "LineageGraphResponse",
    "VerificationCheck",
    "VerificationNodeResult",
    "VerificationResponse",
    "ImpactResponse",
    "DatasetCreateRequest",
    "DatasetResponse",
    "DatasetVersionCreateRequest",
    "DatasetVersionResponse",
    "ReproducibilityResponse",
]


class ArtifactType(str, Enum):
    INGESTION = "ingestion"
    VALIDATION = "validation"
    INTEGRITY = "integrity"
    NORMALIZATION = "normalization"
    SYNCHRONIZATION = "synchronization"
    CLEANING = "cleaning"
    TRANSFORMATION = "transformation"
    QC = "qc"
    PACKAGE = "package"


ARTIFACT_TYPES: tuple[str, ...] = tuple(t.value for t in ArtifactType)

# Canonical stage rank — never inferred alphabetically. Dataset versions
# are conceptually "stage 10" but live in their own tables, not the
# artifacts table, so they have no rank entry here.
STAGE_RANK: dict[str, int] = {
    ArtifactType.INGESTION.value: 1,
    ArtifactType.VALIDATION.value: 2,
    ArtifactType.INTEGRITY.value: 3,
    ArtifactType.NORMALIZATION.value: 4,
    ArtifactType.SYNCHRONIZATION.value: 5,
    ArtifactType.CLEANING.value: 6,
    ArtifactType.TRANSFORMATION.value: 7,
    ArtifactType.QC.value: 8,
    ArtifactType.PACKAGE.value: 9,
}


class RelationshipType(str, Enum):
    VALIDATED_FROM = "validated_from"
    CHECKED_FROM = "checked_from"
    NORMALIZED_FROM = "normalized_from"
    SYNCHRONIZED_FROM = "synchronized_from"
    CLEANED_FROM = "cleaned_from"
    TRANSFORMED_FROM = "transformed_from"
    QC_OF = "qc_of"
    PACKAGED_FROM = "packaged_from"
    APPROVED_BY_QC = "approved_by_qc"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    MISSING = "missing"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    MANIFEST_MISMATCH = "manifest_mismatch"


class DatasetVersionStatus(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


# Step 8's own accepted-status set, duplicated here (as a plain tuple, not
# an import) so the catalog never has a hard dependency on app.qc for
# something this small and stable.
ACCEPTED_QC_STATUSES = ("passed", "passed_with_warnings")


# ---------------------------------------------------------------------------
# Artifact / lineage response models
# ---------------------------------------------------------------------------


class ArtifactRef(BaseModel):
    artifact_type: str
    artifact_id: str


class ArtifactSummary(BaseModel):
    artifact_type: str
    artifact_id: str
    pipeline_stage: int
    status: str | None = None
    storage_uri: str | None = None
    content_sha256: str | None = None
    manifest_uri: str | None = None
    manifest_sha256: str | None = None
    created_at: str | None = None
    session_id: str | None = None
    registered_at: str


class LineageEdge(BaseModel):
    parent: ArtifactRef
    child: ArtifactRef
    relationship: str


class ArtifactDetail(BaseModel):
    artifact: ArtifactSummary
    metadata: dict
    parents: list[ArtifactRef]
    children: list[ArtifactRef]


class ScanIssue(BaseModel):
    artifact_type: str
    artifact_id: str
    issue_code: str
    detail: str | None = None


class ScanResult(BaseModel):
    artifacts_registered: int
    artifacts_updated: int
    edges_registered: int
    issues: list[ScanIssue] = Field(default_factory=list)


class RebuildResult(BaseModel):
    artifacts_registered: int
    edges_registered: int
    issues: list[ScanIssue] = Field(default_factory=list)
    datasets_preserved: int
    dataset_versions_preserved: int


class HealthIssue(BaseModel):
    code: str
    detail: str


class CatalogHealth(BaseModel):
    status: str
    artifacts: int
    edges: int
    datasets: int
    versions: int
    orphan_artifacts: int
    missing_parent_references: int
    cycle_count: int
    catalog_schema_version: str
    last_scan_at: str | None = None
    issues: list[HealthIssue] = Field(default_factory=list)


class LineageNode(BaseModel):
    artifact_type: str
    artifact_id: str
    pipeline_stage: int
    status: str | None = None


class LineageGraphResponse(BaseModel):
    root: ArtifactRef
    direction: str
    nodes: list[LineageNode]
    edges: list[LineageEdge]


class VerificationCheck(BaseModel):
    name: str
    status: str
    detail: str | None = None


class VerificationNodeResult(BaseModel):
    artifact_type: str
    artifact_id: str
    status: str
    checks: list[VerificationCheck]


class VerificationResponse(BaseModel):
    artifact_type: str
    artifact_id: str
    status: str
    checks: list[VerificationCheck]
    recursive: bool = False
    verified_nodes: int | None = None
    failed_nodes: int | None = None
    missing_nodes: int | None = None
    nodes: list[VerificationNodeResult] | None = None


class ImpactResponse(BaseModel):
    artifact_type: str
    artifact_id: str
    affected: dict[str, int]


# ---------------------------------------------------------------------------
# Dataset registry models
# ---------------------------------------------------------------------------


class DatasetCreateRequest(BaseModel):
    dataset_name: str
    description: str | None = None
    metadata: dict = Field(default_factory=dict)


class DatasetResponse(BaseModel):
    dataset_name: str
    description: str | None = None
    metadata: dict
    created_at: str
    version_count: int
    latest_version: str | None = None


class DatasetVersionCreateRequest(BaseModel):
    version: str
    package_id: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class DatasetVersionResponse(BaseModel):
    dataset_name: str
    version: str
    package_id: str
    description: str | None = None
    tags: list[str]
    status: str
    created_at: str
    package_status: str | None = None
    source_qc_status: str | None = None
    lineage_fingerprint: str | None = None
    # v2.5 — computed, never mutates `status` or the (dataset, version) ->
    # package_id mapping above; see app.catalog.governance. Kept separate
    # from `lineage_fingerprint` on purpose (Design Requirement 28):
    # governance never changes reproducibility.
    effective_status: str = "healthy"
    effective_status_reason: str | None = None


class ReproducibilityResponse(BaseModel):
    dataset_name: str
    version: str
    package_id: str
    qc_id: str | None = None
    source_transformed_sha256: str | None = None
    qc_config_hash: str | None = None
    transformation_config_hash: str | None = None
    cleaning_config_hash: str | None = None
    synchronization_config_hash: str | None = None
    normalization_config_hashes: list[str] = Field(default_factory=list)
    schema_versions: list[str] = Field(default_factory=list)
    source_ingestion_ids: list[str] = Field(default_factory=list)
    raw_sha256_values: list[str] = Field(default_factory=list)
    package_config_hash: str | None = None
    split_seed: int | None = None
    split_checksums: dict[str, str] = Field(default_factory=dict)
    transform_versions: dict[str, str] = Field(default_factory=dict)
    git_commit: str | None = None
    lineage_fingerprint: str
