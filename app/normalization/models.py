"""Pydantic models for the normalization API and the persisted normalization manifest.

SchemaRef is reused from app.validation.models — a generic {name, version}
pair with no stage-specific meaning. ProfileRef is the same shape, defined
here since profiles are a Step 4 concept.

There is deliberately only one NormalizationStatus value: a normalization
run either fully succeeds and is committed, or fails and commits nothing
(see NormalizedArtifactStore's staging/commit design) — there is no
"partial" or "failed" status object to represent, unlike Steps 2-3 where a
structurally-invalid dataset still produces a persisted report.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.validation.models import SchemaRef

__all__ = [
    "SchemaRef",
    "ProfileRef",
    "NormalizationErrorCode",
    "NormalizationStatus",
    "NormalizationRequest",
    "NormalizationResponse",
    "NormalizationManifest",
]


class NormalizationErrorCode(str, Enum):
    NORMALIZATION_PROFILE_NOT_FOUND = "NORMALIZATION_PROFILE_NOT_FOUND"
    UNSUPPORTED_SOURCE_UNIT = "UNSUPPORTED_SOURCE_UNIT"
    MISSING_UNIT_METADATA = "MISSING_UNIT_METADATA"
    AMBIGUOUS_FIELD_MAPPING = "AMBIGUOUS_FIELD_MAPPING"
    NORMALIZATION_CONVERSION_ERROR = "NORMALIZATION_CONVERSION_ERROR"
    INVALID_NORMALIZATION_INPUT = "INVALID_NORMALIZATION_INPUT"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    STALE_LINEAGE = "STALE_LINEAGE"
    INTEGRITY_NOT_PASSED = "INTEGRITY_NOT_PASSED"


class NormalizationStatus(str, Enum):
    COMPLETED = "completed"


class ProfileRef(BaseModel):
    name: str
    version: str


class NormalizationRequest(BaseModel):
    schema_name: str
    schema_version: str
    profile_name: str
    profile_version: str
    # e.g. {"acceleration": "g", "angular_velocity": "deg/s"} for IMU, or
    # {"altitude": "ft", "speed": "mph"} for GPS. Never guessed — a
    # dimension the profile needs but this doesn't supply fails explicitly.
    source_units: dict[str, str] = Field(default_factory=dict)


class NormalizationResponse(BaseModel):
    normalization_id: str
    ingestion_id: str
    validation_id: str
    integrity_id: str
    schema: SchemaRef
    profile: ProfileRef
    status: NormalizationStatus
    records_written: int
    artifact_uri: str
    normalized_sha256: str


class NormalizationManifest(BaseModel):
    """Persisted alongside the normalized artifact — carries the full
    lineage chain back to ingestion:

    normalized artifact -> integrity report -> validation report -> raw ingestion
    """

    normalization_id: str
    ingestion_id: str
    validation_id: str
    integrity_id: str
    customer_id: str
    device_id: str | None = None
    schema: SchemaRef
    source_raw_sha256: str
    normalization_profile: ProfileRef
    source_units: dict[str, str]
    normalization_config_hash: str
    transform_version: str
    normalized_sha256: str
    normalized_size_bytes: int
    records_written: int
    source_filename: str
    artifact_filename: str
    created_at: datetime
    artifact_uri: str
    pipeline_stage: str = "normalized"
