"""Pydantic models for the cleaning API and the persisted cleaning report/manifest.

Individual synchronized/cleaned JSONL rows are NOT modeled as Pydantic
objects — plain dicts, serialized via the one canonical JSON convention
(see app.cleaning.rules.common.canonical_json), the same reasoning as
Steps 4-5: per-row validation overhead buys nothing at row-count scale.

There are exactly two CleaningStatus values, unlike Steps 2-3's richer
status sets: "completed" (the run's own rules didn't reject it) and
"rejected" (a policy-level judgment — e.g. every row was dropped, or too
few rows survived). Both are committed artifacts; a "rejected" cleaning
run is not a processing failure (see CleaningService).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

__all__ = [
    "CleaningErrorCode",
    "CleaningStatus",
    "PolicyRef",
    "DuplicatePolicyConfig",
    "PrivacyConfig",
    "CleaningConfig",
    "CleaningRequest",
    "CleaningSummary",
    "CleaningResponse",
    "DropReasonModel",
    "RedactionRecordModel",
    "DroppedRowExample",
    "RedactionExample",
    "CleaningReport",
    "UpstreamStreamLineage",
    "CleaningManifest",
]


class CleaningErrorCode(str, Enum):
    CLEANING_POLICY_NOT_FOUND = "CLEANING_POLICY_NOT_FOUND"
    SYNCHRONIZATION_NOT_FOUND = "SYNCHRONIZATION_NOT_FOUND"
    SYNCHRONIZED_ARTIFACT_CHECKSUM_MISMATCH = "SYNCHRONIZED_ARTIFACT_CHECKSUM_MISMATCH"
    INVALID_CLEANING_CONFIGURATION = "INVALID_CLEANING_CONFIGURATION"
    MISSING_REQUIRED_STREAM = "MISSING_REQUIRED_STREAM"
    INSUFFICIENT_MODALITY_COVERAGE = "INSUFFICIENT_MODALITY_COVERAGE"
    ALL_OPTIONAL_STREAMS_MISSING = "ALL_OPTIONAL_STREAMS_MISSING"
    DUPLICATE_ROW = "DUPLICATE_ROW"
    FIELD_REDACTED = "FIELD_REDACTED"
    EMPTY_SYNCHRONIZED_DATASET = "EMPTY_SYNCHRONIZED_DATASET"
    INSUFFICIENT_RETAINED_ROWS = "INSUFFICIENT_RETAINED_ROWS"
    INVALID_REDACTION_PATH = "INVALID_REDACTION_PATH"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"


class CleaningStatus(str, Enum):
    COMPLETED = "completed"
    REJECTED = "rejected"


class PolicyRef(BaseModel):
    name: str
    version: str


class DuplicatePolicyConfig(BaseModel):
    enabled: bool = True


class PrivacyConfig(BaseModel):
    # Dot-separated paths, e.g. "streams.gps.latitude" — explicit only,
    # never inferred/guessed. See rules/privacy.py.
    redact_fields: list[str] = Field(default_factory=list)


class CleaningConfig(BaseModel):
    required_streams: list[str] = Field(default_factory=list)
    min_present_streams: int | None = None
    drop_if_all_optional_streams_missing: bool = False
    duplicate_policy: DuplicatePolicyConfig = Field(default_factory=DuplicatePolicyConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    # Session-level rejection: if fewer rows survive than this, the run is
    # committed but marked "rejected" rather than "completed".
    minimum_retained_rows: int | None = None


class CleaningRequest(BaseModel):
    policy_name: str
    policy_version: str
    config: CleaningConfig = Field(default_factory=CleaningConfig)


class CleaningSummary(BaseModel):
    input_rows: int
    retained_rows: int
    dropped_rows: int
    redacted_rows: int
    retention_ratio: float


class CleaningResponse(BaseModel):
    cleaning_id: str
    synchronization_id: str
    status: CleaningStatus
    policy: PolicyRef
    summary: CleaningSummary
    artifact_uri: str
    report_uri: str
    cleaned_sha256: str
    rejection_reasons: list[str] = Field(default_factory=list)


class DropReasonModel(BaseModel):
    code: CleaningErrorCode
    stream: str | None = None
    duplicate_of_row_index: int | None = None


class RedactionRecordModel(BaseModel):
    code: CleaningErrorCode
    field: str


class DroppedRowExample(BaseModel):
    row_index: int
    timestamp: str | None = None
    reasons: list[DropReasonModel]


class RedactionExample(BaseModel):
    row_index: int
    timestamp: str | None = None
    redactions: list[RedactionRecordModel]


class CleaningReport(BaseModel):
    """Persisted separately from cleaned.jsonl — cleaning reasons/stats
    never contaminate model-facing rows (see cleaned.jsonl format)."""

    cleaning_id: str
    synchronization_id: str
    status: CleaningStatus
    summary: CleaningSummary
    reason_counts: dict[str, int]
    dropped_examples: list[DroppedRowExample]
    redaction_examples: list[RedactionExample]
    details_truncated: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)


class UpstreamStreamLineage(BaseModel):
    """Trimmed per-stream lineage copied from the synchronization manifest
    into the cleaning manifest — enough to make the chain back to raw
    ingestion unambiguous without duplicating the entire upstream manifest
    wholesale."""

    name: str
    normalization_id: str
    ingestion_id: str
    session_id: str


class CleaningManifest(BaseModel):
    """Persisted alongside the cleaned artifact — completes the full chain:

    cleaned artifact -> synchronized artifact -> normalized artifact
    -> integrity report -> validation report -> raw ingestion
    """

    cleaning_id: str
    synchronization_id: str
    source_synchronized_sha256: str
    synchronization_config_hash: str
    streams: list[UpstreamStreamLineage]
    policy: PolicyRef
    cleaning_config_hash: str
    transform_version: str
    status: CleaningStatus
    input_rows: int
    retained_rows: int
    dropped_rows: int
    redacted_rows: int
    cleaned_sha256: str
    cleaned_size_bytes: int
    artifact_uri: str
    report_uri: str
    created_at: datetime
    pipeline_stage: str = "cleaned"
    rejection_reasons: list[str] = Field(default_factory=list)
