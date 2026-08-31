"""Pydantic models for the integrity API and the persisted integrity report.

SchemaRef is reused from app.validation.models rather than duplicated —
it's a generic {name, version} pair with no validation-specific meaning.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel

from app.validation.models import SchemaRef

__all__ = [
    "SchemaRef",
    "IntegritySeverity",
    "IntegrityErrorCode",
    "IntegrityStatus",
    "IntegrityIssue",
    "IntegrityRequest",
    "IntegrityResponse",
    "IntegrityReport",
]


class IntegritySeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class IntegrityErrorCode(str, Enum):
    GPS_LATITUDE_OUT_OF_RANGE = "GPS_LATITUDE_OUT_OF_RANGE"
    GPS_LONGITUDE_OUT_OF_RANGE = "GPS_LONGITUDE_OUT_OF_RANGE"
    GPS_NEGATIVE_SPEED = "GPS_NEGATIVE_SPEED"
    TIMESTAMP_OUT_OF_ORDER = "TIMESTAMP_OUT_OF_ORDER"
    DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"
    NON_FINITE_VALUE = "NON_FINITE_VALUE"
    IMU_ACCELERATION_EXTREME = "IMU_ACCELERATION_EXTREME"
    IMU_GYRO_EXTREME = "IMU_GYRO_EXTREME"
    EMPTY_DATASET = "EMPTY_DATASET"


class IntegrityStatus(str, Enum):
    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed_with_warnings"
    FAILED = "failed"


class IntegrityIssue(BaseModel):
    record_number: int | None = None
    field: str | None = None
    code: IntegrityErrorCode
    severity: IntegritySeverity
    message: str
    # A single offending scalar only — never a full record (see module docstring
    # in service.py: integrity reports must not leak raw sensor data).
    value: float | int | str | bool | None = None


class IntegrityRequest(BaseModel):
    schema_name: str
    schema_version: str


class IntegrityResponse(BaseModel):
    integrity_id: str
    ingestion_id: str
    validation_id: str
    schema: SchemaRef
    status: IntegrityStatus
    total_records: int
    checked_records: int
    passed_records: int
    failed_records: int
    warning_count: int
    error_count: int
    report_uri: str


class IntegrityReport(BaseModel):
    """Persisted report — carries the full lineage chain back to ingestion.

    validation_id + raw_sha256 together establish:
    raw ingestion -> validation report -> integrity report
    """

    integrity_id: str
    ingestion_id: str
    validation_id: str
    customer_id: str
    device_id: str | None = None
    schema_name: str
    schema_version: str
    source_filename: str
    raw_sha256: str
    status: IntegrityStatus
    total_records: int
    checked_records: int
    passed_records: int
    failed_records: int
    warning_count: int
    error_count: int
    issues: list[IntegrityIssue]
    issues_truncated: bool = False
    created_at: datetime
