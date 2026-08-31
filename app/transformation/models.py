"""Pydantic models for the transformation API and the persisted transformed
report/manifest.

Step 7 turns cleaned, synchronized rows (Step 6's cleaned.jsonl) into
ML-oriented windowed samples with deterministic handcrafted features. It is
not dataset QC (Step 8 — a distributional judgment about the whole
dataset), not labeling, and not train/val/test splitting — Step 7 only
groups rows into windows and computes explicit, deterministic, configured
features for each window.

Individual transformed samples are NOT modeled as Pydantic objects for the
same reason cleaned/synchronized rows aren't in Steps 5-6 — plain dicts,
serialized via app.transformation.serialization.canonical_json, avoids
per-row validation overhead at row-count scale.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.cleaning.models import PolicyRef

__all__ = [
    "TransformationErrorCode",
    "TransformationStatus",
    "ProfileRef",
    "WindowConfig",
    "StreamFeatureConfig",
    "FeaturesConfig",
    "TransformationConfig",
    "TransformationRequest",
    "TransformationSummary",
    "TransformationResponse",
    "ModalityCoverageStat",
    "TransformationReport",
    "UpstreamCleaningLineage",
    "TransformationManifest",
]


class TransformationErrorCode(str, Enum):
    CLEANING_NOT_FOUND = "CLEANING_NOT_FOUND"
    CLEANING_NOT_ACCEPTED = "CLEANING_NOT_ACCEPTED"
    CLEANED_ARTIFACT_CHECKSUM_MISMATCH = "CLEANED_ARTIFACT_CHECKSUM_MISMATCH"
    TRANSFORMATION_PROFILE_NOT_FOUND = "TRANSFORMATION_PROFILE_NOT_FOUND"
    INVALID_TRANSFORMATION_CONFIGURATION = "INVALID_TRANSFORMATION_CONFIGURATION"
    UNSUPPORTED_WINDOW_MODE = "UNSUPPORTED_WINDOW_MODE"
    UNKNOWN_FEATURE = "UNKNOWN_FEATURE"
    INVALID_NUMERIC_VALUE = "INVALID_NUMERIC_VALUE"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    EMPTY_CLEANED_DATASET = "EMPTY_CLEANED_DATASET"
    TRANSFORMATION_FAILED = "TRANSFORMATION_FAILED"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"


class TransformationStatus(str, Enum):
    COMPLETED = "completed"


class ProfileRef(BaseModel):
    name: str
    version: str


class WindowConfig(BaseModel):
    # extra="forbid": a misspelled window field (e.g. "durration_ms") must
    # fail configuration validation, never be silently dropped.
    model_config = ConfigDict(extra="forbid")

    mode: str = "count"
    # count mode
    size: int | None = None
    stride: int | None = None
    # time mode
    duration_ms: float | None = None
    stride_ms: float | None = None
    # shared
    drop_incomplete: bool = True


class StreamFeatureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_raw: bool = False
    statistics: list[str] = Field(default_factory=list)
    derived: list[str] = Field(default_factory=list)


class FeaturesConfig(BaseModel):
    # extra="forbid": a feature block for a stream this profile doesn't
    # model (e.g. "lidar") — or a misspelled key — must fail configuration
    # validation, never be silently dropped (see Step 7's "no unknown
    # feature is ever silently ignored" requirement).
    model_config = ConfigDict(extra="forbid")

    imu: StreamFeatureConfig | None = None
    gps: StreamFeatureConfig | None = None
    include_modality_mask: bool = True
    include_relative_time: bool = False


class TransformationConfig(BaseModel):
    window: WindowConfig = Field(default_factory=WindowConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)


class TransformationRequest(BaseModel):
    profile_name: str
    profile_version: str
    config: TransformationConfig = Field(default_factory=TransformationConfig)


class TransformationSummary(BaseModel):
    input_rows: int
    samples_written: int
    window_mode: str
    average_rows_per_window: float | None = None


class TransformationResponse(BaseModel):
    transformation_id: str
    cleaning_id: str
    status: TransformationStatus
    profile: ProfileRef
    summary: TransformationSummary
    artifact_uri: str
    report_uri: str
    transformed_sha256: str


class ModalityCoverageStat(BaseModel):
    windows_present: int
    coverage_ratio: float


class TransformationReport(BaseModel):
    """Persisted separately from transformed.jsonl — bounded summary
    statistics only, never every sample dumped into report.json."""

    transformation_id: str
    summary: TransformationSummary
    modality_coverage: dict[str, ModalityCoverageStat]
    feature_counts: dict[str, int]


class UpstreamCleaningLineage(BaseModel):
    """Trimmed lineage copied from the cleaning manifest into the
    transformation manifest — enough to make the chain back to
    synchronization/normalization/ingestion unambiguous without duplicating
    the entire upstream manifest wholesale."""

    cleaning_id: str
    synchronization_id: str
    cleaning_policy: PolicyRef
    cleaning_config_hash: str
    session_ids: list[str] = Field(default_factory=list)
    normalization_ids: list[str] = Field(default_factory=list)


class TransformationManifest(BaseModel):
    """Persisted alongside the transformed artifact — completes the full
    chain:

    transformed artifact -> cleaned artifact -> synchronized artifact
    -> normalized artifact -> integrity report -> validation report
    -> raw ingestion
    """

    transformation_id: str
    cleaning_id: str
    upstream: UpstreamCleaningLineage
    source_cleaned_sha256: str
    profile: ProfileRef
    transform_version: str
    transformation_config_hash: str
    input_rows: int
    samples_written: int
    transformed_sha256: str
    transformed_size_bytes: int
    artifact_uri: str
    report_uri: str
    created_at: datetime
    pipeline_stage: str = "transformed"
