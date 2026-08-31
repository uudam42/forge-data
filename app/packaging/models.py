"""Pydantic models for the packaging API and the persisted package
report/manifest.

Step 9 turns a QC-accepted transformed dataset into a reproducible,
leakage-safe, versioned train/validation/test package. It reorganizes
existing ML samples into split files — it never changes their semantic
content, generates new features, imputes values, normalizes data, or
re-runs QC.

Individual transformed samples are read but never modeled as Pydantic
objects here, for the same reason every prior stage avoids it at
sample-count scale — see app.transformation.models for the precedent.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.qc.models import QCStatus
from app.transformation.models import ProfileRef

__all__ = [
    "PACKAGE_ENGINE_VERSION",
    "PackagingErrorCode",
    "PackagingRejectionReason",
    "PackageStatus",
    "ProfileRef",
    "SplitConfig",
    "GroupingConfig",
    "PackagingConfig",
    "PackagingRequest",
    "PackagingSummary",
    "SplitStats",
    "SplitManifestEntry",
    "LeakageChecks",
    "SourceQCSummary",
    "PackagingResponse",
    "PackagingReport",
    "UpstreamPackagingLineage",
    "PackageManifest",
    "SPLIT_NAMES",
]

PACKAGE_ENGINE_VERSION = "1.0.0"
SPLIT_NAMES = ("train", "validation", "test")
_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


class PackagingErrorCode(str, Enum):
    TRANSFORMATION_NOT_FOUND = "TRANSFORMATION_NOT_FOUND"
    TRANSFORMED_ARTIFACT_CHECKSUM_MISMATCH = "TRANSFORMED_ARTIFACT_CHECKSUM_MISMATCH"
    QC_NOT_FOUND = "QC_NOT_FOUND"
    QC_TRANSFORMATION_MISMATCH = "QC_TRANSFORMATION_MISMATCH"
    QC_REPORT_CHECKSUM_MISMATCH = "QC_REPORT_CHECKSUM_MISMATCH"
    QC_NOT_ACCEPTED = "QC_NOT_ACCEPTED"
    PACKAGING_PROFILE_NOT_FOUND = "PACKAGING_PROFILE_NOT_FOUND"
    INVALID_PACKAGING_CONFIGURATION = "INVALID_PACKAGING_CONFIGURATION"
    INVALID_SPLIT_RATIOS = "INVALID_SPLIT_RATIOS"
    UNSUPPORTED_SPLIT_STRATEGY = "UNSUPPORTED_SPLIT_STRATEGY"
    UNSUPPORTED_GROUPING_MODE = "UNSUPPORTED_GROUPING_MODE"
    UNSUPPORTED_EXPORT_FORMAT = "UNSUPPORTED_EXPORT_FORMAT"
    MISSING_GROUP_METADATA = "MISSING_GROUP_METADATA"
    PACKAGE_COLLISION = "PACKAGE_COLLISION"
    LEAKAGE_INVARIANT_VIOLATION = "LEAKAGE_INVARIANT_VIOLATION"
    SAMPLE_COUNT_MISMATCH = "SAMPLE_COUNT_MISMATCH"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    # Additive beyond the spec's minimum list: two more internal-invariant
    # codes needed to satisfy "fail packaging clearly" / "fail safely"
    # for sample identity problems, mirrored 1:1 onto HTTP 500 (an
    # engine/data invariant Step 7 is supposed to already guarantee).
    MISSING_SAMPLE_ID = "MISSING_SAMPLE_ID"
    DUPLICATE_SAMPLE_ID = "DUPLICATE_SAMPLE_ID"


class PackagingRejectionReason(str, Enum):
    EMPTY_SOURCE_DATASET = "EMPTY_SOURCE_DATASET"
    INSUFFICIENT_GROUPS_FOR_SPLIT = "INSUFFICIENT_GROUPS_FOR_SPLIT"
    EMPTY_REQUESTED_SPLIT = "EMPTY_REQUESTED_SPLIT"


class PackageStatus(str, Enum):
    COMPLETED = "completed"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Request configuration
# ---------------------------------------------------------------------------


class SplitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = "group_hash"
    train_ratio: float
    validation_ratio: float = 0.0
    test_ratio: float = 0.0
    # A fixed, documented default (0) — never an arbitrary/random seed —
    # so an omitted seed is still fully deterministic and reproducible.
    seed: int = 0


class GroupingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "source_overlap"


class PackagingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    split: SplitConfig
    grouping: GroupingConfig = Field(default_factory=GroupingConfig)
    exports: list[str] = Field(default_factory=lambda: ["jsonl"])


class PackagingRequest(BaseModel):
    qc_id: str
    profile_name: str
    profile_version: str
    config: PackagingConfig
    dataset_name: str | None = None
    dataset_version: str | None = None
    description: str | None = None

    @field_validator("dataset_version")
    @classmethod
    def _validate_semver(cls, value: str | None) -> str | None:
        if value is not None and not _SEMVER_PATTERN.match(value):
            raise ValueError(f"dataset_version '{value}' is not a valid MAJOR.MINOR.PATCH SemVer string")
        return value

    @field_validator("dataset_name")
    @classmethod
    def _validate_dataset_name(cls, value: str | None) -> str | None:
        # Conservative, filesystem-independent validation — storage is
        # always keyed by IDs, never by this name, but it's still
        # persisted metadata and must not carry path-like content.
        if value is not None and ("/" in value or "\\" in value or value.strip() == ""):
            raise ValueError("dataset_name must not be empty or contain path separators")
        return value


# ---------------------------------------------------------------------------
# Response / report / manifest
# ---------------------------------------------------------------------------


class PackagingSummary(BaseModel):
    source_samples: int
    packaged_samples: int
    group_count: int


class SplitStats(BaseModel):
    samples: int
    groups: int
    sample_ratio: float


class SplitManifestEntry(BaseModel):
    samples: int
    sha256: str
    size_bytes: int
    artifact_uri: str


class LeakageChecks(BaseModel):
    duplicate_sample_ids: int
    cross_split_groups: int
    cross_split_overlaps: int
    passed: bool


class SourceQCSummary(BaseModel):
    status: QCStatus
    warning_count: int
    error_count: int


class PackagingResponse(BaseModel):
    package_id: str
    transformation_id: str
    qc_id: str
    status: PackageStatus
    profile: ProfileRef
    summary: PackagingSummary
    report_uri: str
    rejection_reasons: list[str] = Field(default_factory=list)


class PackagingReport(BaseModel):
    """Persisted separately from the split files — bounded aggregate
    metrics only. Per-sample assignment belongs in split_index.jsonl, not
    here."""

    package_id: str
    transformation_id: str
    qc_id: str
    status: PackageStatus
    summary: PackagingSummary
    requested_split_ratios: dict[str, float]
    actual: dict[str, SplitStats]
    leakage_checks: LeakageChecks
    source_qc: SourceQCSummary
    rejection_reasons: list[str] = Field(default_factory=list)


class UpstreamPackagingLineage(BaseModel):
    """Trimmed lineage copied from the transformation/QC manifests — enough
    to make the chain back to cleaning/synchronization unambiguous without
    duplicating either upstream manifest wholesale."""

    cleaning_id: str
    synchronization_id: str
    transformation_config_hash: str
    qc_config_hash: str
    session_ids: list[str] = Field(default_factory=list)
    normalization_ids: list[str] = Field(default_factory=list)


class PackageManifest(BaseModel):
    package_id: str
    transformation_id: str
    qc_id: str
    upstream: UpstreamPackagingLineage
    source_transformed_sha256: str
    source_qc_report_sha256: str
    source_qc_status: QCStatus
    profile: ProfileRef
    packaging_config_hash: str
    package_engine_version: str
    status: PackageStatus
    split_strategy: str
    grouping_mode: str
    seed: int
    requested_ratios: dict[str, float]
    source_samples: int
    splits: dict[str, SplitManifestEntry]
    split_index_sha256: str | None = None
    split_index_size_bytes: int | None = None
    split_index_uri: str | None = None
    exports: dict[str, dict[str, SplitManifestEntry]] | None = None
    report_sha256: str
    report_uri: str
    created_at: datetime
    pipeline_stage: str = "packaged"
    dataset_name: str | None = None
    dataset_version: str | None = None
    description: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
