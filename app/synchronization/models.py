"""Pydantic models for the synchronization API and the persisted
synchronization manifest.

SchemaRef is reused from app.validation.models — a generic {name, version}
pair with no stage-specific meaning.

Individual synchronized JSONL rows are NOT modeled as Pydantic objects —
they are built as plain dicts and serialized directly, the same way
Step 4's CSV/JSON/JSONL writers build plain dicts rather than validating
every record through a model. At potentially high output row counts, that
per-row validation overhead buys nothing here.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.validation.models import SchemaRef

__all__ = [
    "SchemaRef",
    "SynchronizationErrorCode",
    "SynchronizationStatus",
    "StreamRequest",
    "ReferenceConfig",
    "StreamAlignmentOverride",
    "AlignmentConfig",
    "ClockCorrectionConfig",
    "SynchronizationRequest",
    "SynchronizationResponse",
    "StreamLineage",
    "StreamMetrics",
    "SynchronizationManifest",
]


class SynchronizationErrorCode(str, Enum):
    NORMALIZATION_NOT_FOUND = "NORMALIZATION_NOT_FOUND"
    NORMALIZED_ARTIFACT_CHECKSUM_MISMATCH = "NORMALIZED_ARTIFACT_CHECKSUM_MISMATCH"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    DUPLICATE_STREAM_NAME = "DUPLICATE_STREAM_NAME"
    REFERENCE_STREAM_NOT_FOUND = "REFERENCE_STREAM_NOT_FOUND"
    UNSUPPORTED_ALIGNMENT_METHOD = "UNSUPPORTED_ALIGNMENT_METHOD"
    INVALID_SYNC_CONFIGURATION = "INVALID_SYNC_CONFIGURATION"
    NON_MONOTONIC_STREAM = "NON_MONOTONIC_STREAM"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    CLOCK_CORRECTION_ERROR = "CLOCK_CORRECTION_ERROR"
    SYNCHRONIZATION_CONVERSION_ERROR = "SYNCHRONIZATION_CONVERSION_ERROR"


class SynchronizationStatus(str, Enum):
    COMPLETED = "completed"


class StreamRequest(BaseModel):
    name: str
    normalization_id: str


class ReferenceConfig(BaseModel):
    """mode="stream" (the MVP default) drives the output timeline from one
    named input stream's own timestamps. mode="fixed_rate" generates a
    synthetic uniform timeline over the intersection of every stream's
    (corrected) time range. Exactly one of `stream` / `frequency_hz` is
    meaningful depending on `mode` — validated in the service, not here, so
    the resulting error carries a specific SynchronizationErrorCode rather
    than a generic pydantic 422.
    """

    mode: str = "stream"
    stream: str | None = None
    frequency_hz: float | None = None


class StreamAlignmentOverride(BaseModel):
    method: str


class AlignmentConfig(BaseModel):
    default_method: str = "nearest"
    max_time_delta_ms: float | None = None
    streams: dict[str, StreamAlignmentOverride] = Field(default_factory=dict)


class ClockCorrectionConfig(BaseModel):
    offset_ms: float = 0.0
    drift_ppm: float = 0.0


class SynchronizationRequest(BaseModel):
    streams: list[StreamRequest]
    reference: ReferenceConfig = Field(default_factory=ReferenceConfig)
    alignment: AlignmentConfig = Field(default_factory=AlignmentConfig)
    clock_corrections: dict[str, ClockCorrectionConfig] = Field(default_factory=dict)


class SynchronizationResponse(BaseModel):
    synchronization_id: str
    status: SynchronizationStatus
    reference: ReferenceConfig
    streams: list[StreamRequest]
    rows_written: int
    coverage: dict[str, float]
    artifact_uri: str
    synchronized_sha256: str


class StreamLineage(BaseModel):
    """One input stream's full lineage, as recorded in the synchronization manifest."""

    name: str
    normalization_id: str
    normalized_sha256: str
    ingestion_id: str
    session_id: str
    validation_id: str
    integrity_id: str
    source_raw_sha256: str
    schema: SchemaRef
    normalization_profile_name: str
    normalization_profile_version: str
    normalization_config_hash: str
    normalization_transform_version: str


class StreamMetrics(BaseModel):
    source_records: int
    matched_rows: int
    unmatched_rows: int
    coverage_ratio: float
    mean_abs_delta_ms: float | None = None
    max_abs_delta_ms: float | None = None
    exact_match_count: int | None = None
    interpolated_count: int | None = None


class SynchronizationManifest(BaseModel):
    """Persisted alongside the synchronized artifact — carries full lineage
    for every participating stream back to its own raw ingestion:

    synchronized artifact -> normalized artifact -> integrity report
    -> validation report -> raw ingestion   (one such chain per stream)
    """

    synchronization_id: str
    created_at: datetime
    streams: list[StreamLineage]
    reference: ReferenceConfig
    alignment_config: AlignmentConfig
    clock_corrections: dict[str, ClockCorrectionConfig]
    synchronization_config_hash: str
    rows_written: int
    metrics: dict[str, StreamMetrics]
    synchronized_sha256: str
    synchronized_size_bytes: int
    artifact_uri: str
    artifact_filename: str
    pipeline_stage: str = "synchronized"
    transform_version: str = "1.0.0"
