"""Unit tests for JsonlValidator, exercised independently of the API layer."""

from __future__ import annotations

import io

from app.validation.schemas.base import FieldDefinition, FieldType, SchemaDefinition
from app.validation.validators.base import ErrorAccumulator
from app.validation.validators.jsonl_validator import JsonlValidator


def _schema() -> SchemaDefinition:
    return SchemaDefinition(
        schema_name="test_imu",
        schema_version="1.0.0",
        fields={
            "timestamp": FieldDefinition(type=FieldType.DATETIME, required=True, nullable=False),
            "accel_x": FieldDefinition(type=FieldType.FLOAT, required=True, nullable=False),
        },
    )


def _run(jsonl_text: str, max_errors: int = 1000) -> tuple:
    accumulator = ErrorAccumulator(max_errors=max_errors)
    counts = JsonlValidator().validate(io.BytesIO(jsonl_text.encode("utf-8")), _schema(), accumulator)
    return counts, accumulator


def test_valid_jsonl_passes() -> None:
    jsonl_text = (
        '{"timestamp": "2026-08-29T18:34:22Z", "accel_x": 0.1}\n'
        '{"timestamp": "2026-08-29T18:34:23Z", "accel_x": 0.2}\n'
    )
    counts, accumulator = _run(jsonl_text)

    assert counts.records_checked == 2
    assert counts.valid_records == 2
    assert accumulator.error_count == 0


def test_malformed_line_reports_correct_line_number() -> None:
    jsonl_text = (
        '{"timestamp": "2026-08-29T18:34:22Z", "accel_x": 0.1}\n'
        "{not valid json}\n"
        '{"timestamp": "2026-08-29T18:34:24Z", "accel_x": 0.3}\n'
    )
    counts, accumulator = _run(jsonl_text)

    invalid_record_issues = [i for i in accumulator.errors if i.code.value == "INVALID_RECORD"]
    assert len(invalid_record_issues) == 1
    assert invalid_record_issues[0].record == 2


def test_validation_continues_after_malformed_line() -> None:
    jsonl_text = (
        '{"timestamp": "2026-08-29T18:34:22Z", "accel_x": 0.1}\n'
        "{not valid json}\n"
        '{"timestamp": "2026-08-29T18:34:24Z", "accel_x": 0.3}\n'
    )
    counts, accumulator = _run(jsonl_text)

    assert counts.records_checked == 3
    assert counts.valid_records == 2
    assert counts.invalid_records == 1


def test_blank_lines_are_ignored() -> None:
    jsonl_text = (
        '{"timestamp": "2026-08-29T18:34:22Z", "accel_x": 0.1}\n'
        "\n"
        "   \n"
        '{"timestamp": "2026-08-29T18:34:23Z", "accel_x": 0.2}\n'
    )
    counts, accumulator = _run(jsonl_text)

    assert counts.records_checked == 2
    assert accumulator.error_count == 0


def test_non_object_line_is_invalid_record() -> None:
    jsonl_text = '"just a string"\n'
    counts, accumulator = _run(jsonl_text)

    assert counts.records_checked == 1
    assert counts.invalid_records == 1
    assert any(issue.code.value == "INVALID_RECORD" and issue.record == 1 for issue in accumulator.errors)


def test_empty_file_content_is_empty_dataset() -> None:
    counts, accumulator = _run("")

    assert counts.records_checked == 0
    assert any(issue.code.value == "EMPTY_DATASET" for issue in accumulator.errors)
