"""Pydantic models for the validation API and the persisted validation report."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class ValidationErrorCode(str, Enum):
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_TYPE = "INVALID_TYPE"
    NULL_NOT_ALLOWED = "NULL_NOT_ALLOWED"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    UNEXPECTED_FIELD = "UNEXPECTED_FIELD"
    INVALID_RECORD = "INVALID_RECORD"
    EMPTY_DATASET = "EMPTY_DATASET"
    SCHEMA_NOT_FOUND = "SCHEMA_NOT_FOUND"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    # Not in the minimum required set, but needed to report schema
    # metadata_requirements mismatches (see ValidationService).
    METADATA_REQUIREMENT_NOT_MET = "METADATA_REQUIREMENT_NOT_MET"


class ValidationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


class ValidationIssue(BaseModel):
    record: int | None = None
    field: str | None = None
    code: ValidationErrorCode
    message: str


class ValidationSummary(BaseModel):
    records_checked: int
    valid_records: int
    invalid_records: int
    error_count: int
    warning_count: int


class SchemaRef(BaseModel):
    name: str
    version: str


class ValidationRequest(BaseModel):
    schema_name: str
    schema_version: str


class ValidationResponse(BaseModel):
    validation_id: str
    ingestion_id: str
    schema: SchemaRef
    status: ValidationStatus
    summary: ValidationSummary
    report_uri: str


class ValidationReport(BaseModel):
    """Persisted report — includes raw_sha256 for lineage back to ingestion."""

    validation_id: str
    ingestion_id: str
    validated_at: datetime
    schema: SchemaRef
    raw_sha256: str
    status: ValidationStatus
    summary: ValidationSummary
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]
    errors_truncated: bool = False
