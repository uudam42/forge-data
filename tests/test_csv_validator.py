"""Unit tests for CsvValidator, exercised independently of the API layer."""

from __future__ import annotations

import io

from app.validation.schemas.base import FieldDefinition, FieldType, SchemaDefinition
from app.validation.validators.base import ErrorAccumulator
from app.validation.validators.csv_validator import CsvValidator


def _schema(allow_extra_fields: bool = False) -> SchemaDefinition:
    return SchemaDefinition(
        schema_name="test_imu",
        schema_version="1.0.0",
        fields={
            "timestamp": FieldDefinition(type=FieldType.DATETIME, required=True, nullable=False),
            "accel_x": FieldDefinition(type=FieldType.FLOAT, required=True, nullable=False),
            "device_id": FieldDefinition(type=FieldType.STRING, required=False, nullable=True),
        },
        allow_extra_fields=allow_extra_fields,
    )


def _run(csv_text: str, schema: SchemaDefinition, max_errors: int = 1000) -> tuple:
    accumulator = ErrorAccumulator(max_errors=max_errors)
    counts = CsvValidator().validate(io.BytesIO(csv_text.encode("utf-8")), schema, accumulator)
    return counts, accumulator


def test_valid_csv_passes() -> None:
    csv_text = (
        "timestamp,accel_x,device_id\n"
        "2026-08-29T18:34:22Z,0.1,imu_01\n"
        "2026-08-29T18:34:23Z,0.2,imu_01\n"
    )
    counts, accumulator = _run(csv_text, _schema())

    assert counts.records_checked == 2
    assert counts.valid_records == 2
    assert counts.invalid_records == 0
    assert accumulator.error_count == 0


def test_missing_required_column_fails() -> None:
    csv_text = "timestamp,device_id\n2026-08-29T18:34:22Z,imu_01\n"
    counts, accumulator = _run(csv_text, _schema())

    codes = [issue.code.value for issue in accumulator.errors]
    assert "MISSING_REQUIRED_FIELD" in codes
    assert any(issue.field == "accel_x" and issue.record is None for issue in accumulator.errors)


def test_unexpected_column_fails() -> None:
    csv_text = "timestamp,accel_x,pressure\n2026-08-29T18:34:22Z,0.1,1013\n"
    counts, accumulator = _run(csv_text, _schema(allow_extra_fields=False))

    assert any(
        issue.code.value == "UNEXPECTED_FIELD" and issue.field == "pressure"
        for issue in accumulator.errors
    )


def test_allow_extra_fields_ignores_unexpected_column() -> None:
    csv_text = "timestamp,accel_x,pressure\n2026-08-29T18:34:22Z,0.1,1013\n"
    counts, accumulator = _run(csv_text, _schema(allow_extra_fields=True))

    assert accumulator.error_count == 0
    assert counts.valid_records == 1


def test_invalid_float_fails() -> None:
    csv_text = "timestamp,accel_x\n2026-08-29T18:34:22Z,abc\n"
    counts, accumulator = _run(csv_text, _schema())

    assert counts.invalid_records == 1
    assert any(
        issue.code.value == "INVALID_TYPE" and issue.field == "accel_x"
        for issue in accumulator.errors
    )


def test_integer_field_rejects_decimal_string() -> None:
    schema = SchemaDefinition(
        schema_name="int_test",
        schema_version="1.0.0",
        fields={"count": FieldDefinition(type=FieldType.INTEGER, required=True, nullable=False)},
    )
    valid_counts, valid_acc = _run("count\n123\n", schema)
    invalid_counts, invalid_acc = _run("count\n123.4\n", schema)

    assert valid_acc.error_count == 0
    assert valid_counts.valid_records == 1
    assert invalid_acc.error_count == 1
    assert invalid_counts.invalid_records == 1


def test_null_non_nullable_field_fails_with_null_not_allowed() -> None:
    csv_text = "timestamp,accel_x\n2026-08-29T18:34:22Z,\n"
    counts, accumulator = _run(csv_text, _schema())

    assert counts.invalid_records == 1
    assert any(
        issue.code.value == "NULL_NOT_ALLOWED" and issue.field == "accel_x"
        for issue in accumulator.errors
    )
    # Must not be misreported as a missing field — the column exists, the value is empty.
    assert not any(issue.code.value == "MISSING_REQUIRED_FIELD" for issue in accumulator.errors)


def test_invalid_timestamp_fails() -> None:
    csv_text = "timestamp,accel_x\n2026-08-29 18:34:22,0.1\n"  # no timezone
    counts, accumulator = _run(csv_text, _schema())

    assert counts.invalid_records == 1
    assert any(
        issue.code.value == "INVALID_TIMESTAMP" and issue.field == "timestamp"
        for issue in accumulator.errors
    )


def test_optional_field_may_be_absent() -> None:
    csv_text = "timestamp,accel_x\n2026-08-29T18:34:22Z,0.1\n"  # no device_id column at all
    counts, accumulator = _run(csv_text, _schema())

    assert accumulator.error_count == 0
    assert counts.valid_records == 1


def test_nullable_field_accepts_null() -> None:
    csv_text = "timestamp,accel_x,device_id\n2026-08-29T18:34:22Z,0.1,\n"
    counts, accumulator = _run(csv_text, _schema())

    assert accumulator.error_count == 0
    assert counts.valid_records == 1


def test_empty_dataset_fails() -> None:
    csv_text = "timestamp,accel_x,device_id\n"  # header only, no data rows
    counts, accumulator = _run(csv_text, _schema())

    assert counts.records_checked == 0
    assert any(issue.code.value == "EMPTY_DATASET" for issue in accumulator.errors)


def test_error_truncation() -> None:
    rows = "\n".join(f"2026-08-29T18:34:22Z,abc{i}" for i in range(10))
    csv_text = f"timestamp,accel_x\n{rows}\n"
    counts, accumulator = _run(csv_text, _schema(), max_errors=2)

    assert counts.invalid_records == 10
    assert accumulator.error_count == 10
    assert len(accumulator.errors) == 2
    assert accumulator.errors_truncated is True
