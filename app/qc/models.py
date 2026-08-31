"""Pydantic models for the QC API and the persisted qc report/manifest.

Step 8 asks a dataset-wide question Steps 1-7 never ask: "is this
transformed dataset, as a dataset, healthy enough for ML use?" It never
repeats Step 3's per-value plausibility judgment, Step 6's per-row
keep/drop judgment, or Step 7's per-window feature computation — it only
*reports* on the dataset that already exists. QC never mutates, filters,
imputes, or regenerates anything; it produces a report and a status.

Individual transformed samples are read but never modeled as Pydantic
objects here, for the same reason every prior stage avoids it at
row/sample-count scale — see app.transformation.models for the precedent.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.transformation.models import ProfileRef

__all__ = [
    "QCErrorCode",
    "QCStatus",
    "Severity",
    "ProfileRef",
    "ModalityCoverageThreshold",
    "FeatureThresholdOverride",
    "FeatureCompletenessConfig",
    "VarianceConfig",
    "FeatureRangeConfig",
    "DriftConfig",
    "QCConfig",
    "QCRequest",
    "QCIssue",
    "QCSummary",
    "QCResponse",
    "DatasetSummary",
    "ModalityCoverageSummary",
    "FeatureDistributionSummary",
    "WindowSizeSummary",
    "DriftFeatureResult",
    "DriftSummary",
    "QCReport",
    "UpstreamTransformationLineage",
    "QCManifest",
]

QC_ENGINE_VERSION = "1.0.0"


class QCErrorCode(str, Enum):
    TRANSFORMATION_NOT_FOUND = "TRANSFORMATION_NOT_FOUND"
    TRANSFORMED_ARTIFACT_CHECKSUM_MISMATCH = "TRANSFORMED_ARTIFACT_CHECKSUM_MISMATCH"
    QC_PROFILE_NOT_FOUND = "QC_PROFILE_NOT_FOUND"
    INVALID_QC_CONFIGURATION = "INVALID_QC_CONFIGURATION"
    EMPTY_DATASET = "EMPTY_DATASET"
    DATASET_TOO_SMALL = "DATASET_TOO_SMALL"
    LOW_MODALITY_COVERAGE = "LOW_MODALITY_COVERAGE"
    LOW_FEATURE_COMPLETENESS = "LOW_FEATURE_COMPLETENESS"
    LOW_FEATURE_VARIANCE = "LOW_FEATURE_VARIANCE"
    NON_FINITE_FEATURE_VALUE = "NON_FINITE_FEATURE_VALUE"
    FEATURE_RANGE_VIOLATION = "FEATURE_RANGE_VIOLATION"
    DUPLICATE_SAMPLE_ID = "DUPLICATE_SAMPLE_ID"
    NON_MONOTONIC_SAMPLE_TIME = "NON_MONOTONIC_SAMPLE_TIME"
    GROUP_IMBALANCE = "GROUP_IMBALANCE"
    BASELINE_QC_NOT_FOUND = "BASELINE_QC_NOT_FOUND"
    BASELINE_INCOMPATIBLE = "BASELINE_INCOMPATIBLE"
    FEATURE_DISTRIBUTION_DRIFT = "FEATURE_DISTRIBUTION_DRIFT"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"


class QCStatus(str, Enum):
    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed_with_warnings"
    FAILED = "failed"


class Severity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Request configuration
# ---------------------------------------------------------------------------


class ModalityCoverageThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_ratio: float
    severity: Severity = Severity.WARNING


class FeatureThresholdOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maximum_missing_ratio: float | None = None
    severity: Severity | None = None


class FeatureCompletenessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maximum_missing_ratio: float | None = None
    severity: Severity = Severity.WARNING
    # Per-feature-path overrides, e.g. {"features.gps.statistics.speed_mean": {...}}
    per_feature: dict[str, FeatureThresholdOverride] = Field(default_factory=dict)


class VarianceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    minimum_variance: float = 1e-12
    severity: Severity = Severity.WARNING


class FeatureRangeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: float | None = None
    max: float | None = None
    severity: Severity = Severity.WARNING


class DriftConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_abs_standardized_mean_difference: float = 1.0
    severity: Severity = Severity.WARNING


class QCConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_samples: int | None = None
    dataset_size_severity: Severity = Severity.ERROR

    modality_coverage: dict[str, ModalityCoverageThreshold] = Field(default_factory=dict)
    feature_completeness: FeatureCompletenessConfig | None = None
    variance: VarianceConfig | None = None
    feature_ranges: dict[str, FeatureRangeConfig] = Field(default_factory=dict)

    max_group_fraction: float | None = None
    group_imbalance_severity: Severity = Severity.WARNING

    drift: DriftConfig | None = None
    # A baseline must always be named explicitly — Step 8 never auto-selects
    # "the previous run" as a baseline (see README "Explicit baseline
    # selection").
    baseline_qc_id: str | None = None


class QCRequest(BaseModel):
    profile_name: str
    profile_version: str
    config: QCConfig = Field(default_factory=QCConfig)


# ---------------------------------------------------------------------------
# Response / report / manifest
# ---------------------------------------------------------------------------


class QCIssue(BaseModel):
    code: str
    severity: Severity
    path: str | None = None
    observed: float | int | str | None = None
    threshold: float | int | str | None = None
    message: str


class QCSummary(BaseModel):
    samples_checked: int
    issue_count: int
    warning_count: int
    error_count: int


class QCResponse(BaseModel):
    qc_id: str
    transformation_id: str
    status: QCStatus
    profile: ProfileRef
    summary: QCSummary
    report_uri: str


class DatasetSummary(BaseModel):
    sample_count: int
    earliest_timestamp: str | None = None
    latest_timestamp: str | None = None
    duration_seconds: float | None = None


class ModalityCoverageSummary(BaseModel):
    samples_present: int
    coverage_ratio: float
    # Dataset-level aggregate of Step 7's per-window modality_coverage
    # ratios (informational — richer than the boolean mask alone).
    mean_window_coverage: float | None = None
    median_window_coverage: float | None = None
    minimum_window_coverage: float | None = None
    maximum_window_coverage: float | None = None


class FeatureDistributionSummary(BaseModel):
    count: int
    missing_count: int
    null_count: int
    non_finite_count: int = 0
    mean: float | None = None
    std: float | None = None
    min: float | None = None
    max: float | None = None
    median: float | None = None
    p01: float | None = None
    p05: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    p95: float | None = None
    p99: float | None = None
    percentiles_truncated: bool = False
    skewness: float | None = None


class WindowSizeSummary(BaseModel):
    row_count_min: int | None = None
    row_count_max: int | None = None
    row_count_mean: float | None = None
    row_count_std: float | None = None


class DriftFeatureResult(BaseModel):
    compared: bool
    baseline_mean: float | None = None
    baseline_std: float | None = None
    current_mean: float | None = None
    current_std: float | None = None
    standardized_mean_difference: float | None = None
    reason: str | None = None


class DriftSummary(BaseModel):
    baseline_qc_id: str
    compatible: bool
    features: dict[str, DriftFeatureResult] = Field(default_factory=dict)
    incompatibility_reason: str | None = None


class QCReport(BaseModel):
    qc_id: str
    transformation_id: str
    status: QCStatus
    summary: QCSummary
    dataset: DatasetSummary
    modality_coverage: dict[str, ModalityCoverageSummary]
    features: dict[str, FeatureDistributionSummary]
    window_size: WindowSizeSummary
    session_distribution: dict[str, int] | None = None
    drift: DriftSummary | None = None
    issues: list[QCIssue]
    issues_truncated: bool = False


class UpstreamTransformationLineage(BaseModel):
    """Trimmed lineage copied from the transformation manifest — enough to
    make the chain back to cleaning/synchronization unambiguous without
    duplicating the entire upstream manifest wholesale."""

    transformation_id: str
    cleaning_id: str
    synchronization_id: str
    transformation_config_hash: str
    session_ids: list[str] = Field(default_factory=list)


class QCManifest(BaseModel):
    qc_id: str
    transformation_id: str
    upstream: UpstreamTransformationLineage
    source_transformed_sha256: str
    profile: ProfileRef
    qc_config_hash: str
    qc_engine_version: str
    status: QCStatus
    samples_checked: int
    warning_count: int
    error_count: int
    report_sha256: str
    report_uri: str
    created_at: datetime
    pipeline_stage: str = "qc"
