"""Shared validation primitives used by every format-specific validator.

Keeping type-checking and record-evaluation logic here — rather than
duplicating it per format — is what keeps the engine schema-driven: no
validator hardcodes field names, only the mechanics of "is this value valid
for this FieldType". csv_validator.py / json_validator.py / jsonl_validator.py
differ only in how they turn a file into a sequence of (index, record) pairs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO, Iterable

from app.validation.models import ValidationErrorCode, ValidationIssue
from app.validation.schemas.base import FieldType, SchemaDefinition

# CSV boolean literals accepted, case-insensitively. Anything else (e.g.
# "1"/"0"/"yes"/"no") is rejected rather than silently coerced.
_CSV_BOOLEAN_TRUE = "true"
_CSV_BOOLEAN_FALSE = "false"


def is_valid_iso8601_with_timezone(value: str) -> bool:
    """True if value is an ISO-8601 datetime string that includes timezone info.

    Naive timestamps (no UTC offset, no 'Z') are rejected — this MVP requires
    timezone-aware timestamps.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return False
    return parsed.tzinfo is not None


def native_value_matches_type(value: object, field_type: FieldType) -> bool:
    """Type-check a value that already has a native Python type (JSON/JSONL)."""
    if field_type is FieldType.STRING:
        return isinstance(value, str)
    if field_type is FieldType.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type is FieldType.FLOAT:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if field_type is FieldType.BOOLEAN:
        return isinstance(value, bool)
    if field_type is FieldType.DATETIME:
        return isinstance(value, str)
    return False


def csv_value_matches_type(value: str, field_type: FieldType) -> bool:
    """Type-check a raw CSV cell string via explicit, deterministic conversion.

    Values are never silently coerced: "123.4" is not a valid integer, and
    only the literal strings "true"/"false" (case-insensitive) are booleans.
    """
    if field_type is FieldType.STRING:
        return True
    if field_type is FieldType.INTEGER:
        try:
            int(value)
        except ValueError:
            return False
        return True
    if field_type is FieldType.FLOAT:
        try:
            float(value)
        except ValueError:
            return False
        return True
    if field_type is FieldType.BOOLEAN:
        return value.strip().lower() in (_CSV_BOOLEAN_TRUE, _CSV_BOOLEAN_FALSE)
    if field_type is FieldType.DATETIME:
        return True  # datetime format is checked separately, not here
    return False


class RecordEvaluator:
    """Checks one record's fields against a SchemaDefinition.

    `values_are_strings` switches between CSV-style string coercion and
    native-type checking for JSON/JSONL records. Presence checks
    (missing-required / unexpected) are split out from value checks because
    CSV only needs to run them once against the header, not once per row.
    """

    def __init__(self, schema: SchemaDefinition, *, values_are_strings: bool) -> None:
        self._schema = schema
        self._values_are_strings = values_are_strings

    def check_missing_required(
        self, present_fields: Iterable[str], record_index: int | None = None
    ) -> list[ValidationIssue]:
        present = set(present_fields)
        issues: list[ValidationIssue] = []
        for field_name, field_def in self._schema.fields.items():
            if field_def.required and field_name not in present:
                issues.append(
                    ValidationIssue(
                        record=record_index,
                        field=field_name,
                        code=ValidationErrorCode.MISSING_REQUIRED_FIELD,
                        message=f"Required field '{field_name}' is missing",
                    )
                )
        return issues

    def check_unexpected(
        self, present_fields: Iterable[str], record_index: int | None = None
    ) -> list[ValidationIssue]:
        if self._schema.allow_extra_fields:
            return []
        issues: list[ValidationIssue] = []
        for field_name in present_fields:
            if field_name not in self._schema.fields:
                issues.append(
                    ValidationIssue(
                        record=record_index,
                        field=field_name,
                        code=ValidationErrorCode.UNEXPECTED_FIELD,
                        message=f"Unexpected field '{field_name}' is not defined in schema",
                    )
                )
        return issues

    def check_values(self, record_index: int | None, record: dict) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for field_name, field_def in self._schema.fields.items():
            if field_name not in record:
                continue
            value = record[field_name]

            is_null = value is None or (
                self._values_are_strings and isinstance(value, str) and value.strip() == ""
            )
            if is_null:
                if not field_def.nullable:
                    issues.append(
                        ValidationIssue(
                            record=record_index,
                            field=field_name,
                            code=ValidationErrorCode.NULL_NOT_ALLOWED,
                            message=f"Field '{field_name}' is null but nullable=false",
                        )
                    )
                continue

            if field_def.type is FieldType.DATETIME:
                if not isinstance(value, str):
                    issues.append(
                        ValidationIssue(
                            record=record_index,
                            field=field_name,
                            code=ValidationErrorCode.INVALID_TYPE,
                            message=f"Expected datetime string but received {type(value).__name__}",
                        )
                    )
                elif not is_valid_iso8601_with_timezone(value):
                    issues.append(
                        ValidationIssue(
                            record=record_index,
                            field=field_name,
                            code=ValidationErrorCode.INVALID_TIMESTAMP,
                            message=f"Expected ISO-8601 datetime with timezone but received '{value}'",
                        )
                    )
                continue

            type_ok = (
                csv_value_matches_type(value, field_def.type)
                if self._values_are_strings
                else native_value_matches_type(value, field_def.type)
            )
            if not type_ok:
                issues.append(
                    ValidationIssue(
                        record=record_index,
                        field=field_name,
                        code=ValidationErrorCode.INVALID_TYPE,
                        message=f"Expected {field_def.type.value} but received '{value}'",
                    )
                )
        return issues


@dataclass
class ErrorAccumulator:
    """Collects issues across metadata checks + record validation, with a cap.

    Once `max_errors` detailed error objects have been stored, further
    errors still increment error_count (so counts stay accurate) but are no
    longer appended to `errors`, and errors_truncated is set.
    """

    max_errors: int
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    errors_truncated: bool = False

    def add_error(self, issue: ValidationIssue) -> None:
        self.error_count += 1
        if len(self.errors) < self.max_errors:
            self.errors.append(issue)
        else:
            self.errors_truncated = True

    def add_errors(self, issues: Iterable[ValidationIssue]) -> None:
        for issue in issues:
            self.add_error(issue)

    def add_warning(self, issue: ValidationIssue) -> None:
        self.warning_count += 1
        self.warnings.append(issue)


@dataclass
class RecordCounts:
    records_checked: int = 0
    valid_records: int = 0
    invalid_records: int = 0


class Validator(ABC):
    """A format-specific reader that feeds records through a RecordEvaluator."""

    @abstractmethod
    def validate(
        self, stream: BinaryIO, schema: SchemaDefinition, accumulator: ErrorAccumulator
    ) -> RecordCounts:
        raise NotImplementedError
