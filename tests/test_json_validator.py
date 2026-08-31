"""Unit tests for JsonValidator, exercised independently of the API layer."""

from __future__ import annotations

import io
import json

from app.validation.schemas.base import FieldDefinition, FieldType, SchemaDefinition
from app.validation.validators.base import ErrorAccumulator
from app.validation.validators.json_validator import JsonValidator


def _schema() -> SchemaDefinition:
    return SchemaDefinition(
        schema_name="test_imu",
        schema_version="1.0.0",
        fields={
            "timestamp": FieldDefinition(type=FieldType.DATETIME, required=True, nullable=False),
            "accel_x": FieldDefinition(type=FieldType.FLOAT, required=True, nullable=False),
            "device_id": FieldDefinition(type=FieldType.STRING, required=False, nullable=True),
        },
    )


def _run(payload, max_errors: int = 1000) -> tuple:
    accumulator = ErrorAccumulator(max_errors=max_errors)
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    if isinstance(body, str):
        body = body.encode("utf-8")
    counts = JsonValidator().validate(io.BytesIO(body), _schema(), accumulator)
    return counts, accumulator


def test_valid_json_array_passes() -> None:
    records = [
        {"timestamp": "2026-08-29T18:34:22Z", "accel_x": 0.1, "device_id": "imu_01"},
        {"timestamp": "2026-08-29T18:34:23Z", "accel_x": 0.2, "device_id": "imu_01"},
    ]
    counts, accumulator = _run(records)

    assert counts.records_checked == 2
    assert counts.valid_records == 2
    assert accumulator.error_count == 0


def test_valid_json_single_object_passes() -> None:
    record = {"timestamp": "2026-08-29T18:34:22Z", "accel_x": 0.1}
    counts, accumulator = _run(record)

    assert counts.records_checked == 1
    assert counts.valid_records == 1
    assert accumulator.error_count == 0


def test_malformed_json_fails_with_invalid_record() -> None:
    counts, accumulator = _run("{not valid json")

    assert counts.records_checked == 0
    assert any(issue.code.value == "INVALID_RECORD" for issue in accumulator.errors)


def test_top_level_scalar_is_rejected() -> None:
    counts, accumulator = _run(json.dumps(42))

    assert counts.records_checked == 0
    assert any(issue.code.value == "INVALID_RECORD" for issue in accumulator.errors)


def test_non_object_array_element_is_invalid_record() -> None:
    counts, accumulator = _run([{"timestamp": "2026-08-29T18:34:22Z", "accel_x": 0.1}, "not an object"])

    assert counts.records_checked == 2
    assert counts.invalid_records == 1
    assert counts.valid_records == 1
    assert any(
        issue.code.value == "INVALID_RECORD" and issue.record == 2 for issue in accumulator.errors
    )


def test_empty_array_fails_as_empty_dataset() -> None:
    counts, accumulator = _run([])

    assert counts.records_checked == 0
    assert any(issue.code.value == "EMPTY_DATASET" for issue in accumulator.errors)


def test_native_int_is_valid_for_float_field() -> None:
    counts, accumulator = _run({"timestamp": "2026-08-29T18:34:22Z", "accel_x": 1})

    assert accumulator.error_count == 0
    assert counts.valid_records == 1


def test_native_bool_is_not_a_valid_float() -> None:
    counts, accumulator = _run({"timestamp": "2026-08-29T18:34:22Z", "accel_x": True})

    assert counts.invalid_records == 1
    assert any(issue.code.value == "INVALID_TYPE" and issue.field == "accel_x" for issue in accumulator.errors)


def test_json_null_is_null_regardless_of_type() -> None:
    counts, accumulator = _run({"timestamp": "2026-08-29T18:34:22Z", "accel_x": None})

    assert any(issue.code.value == "NULL_NOT_ALLOWED" and issue.field == "accel_x" for issue in accumulator.errors)
