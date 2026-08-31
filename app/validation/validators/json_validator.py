"""JSON structural validator.

Accepts either a single JSON object (treated as one record) or a top-level
array of record objects. The whole payload is parsed into memory for this
MVP — see README for the documented limitation on very large JSON arrays
(unlike CSV/JSONL, which are processed incrementally).
"""

from __future__ import annotations

import io
import json
from typing import BinaryIO

from app.validation.models import ValidationErrorCode, ValidationIssue
from app.validation.schemas.base import SchemaDefinition
from app.validation.validators.base import ErrorAccumulator, RecordCounts, RecordEvaluator, Validator


class JsonValidator(Validator):
    def validate(
        self, stream: BinaryIO, schema: SchemaDefinition, accumulator: ErrorAccumulator
    ) -> RecordCounts:
        evaluator = RecordEvaluator(schema, values_are_strings=False)
        counts = RecordCounts()
        text_stream = io.TextIOWrapper(stream, encoding="utf-8")

        try:
            payload = json.load(text_stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            accumulator.add_error(
                ValidationIssue(
                    record=None,
                    field=None,
                    code=ValidationErrorCode.INVALID_RECORD,
                    message=f"Malformed JSON: {exc}",
                )
            )
            return counts

        if isinstance(payload, dict):
            records = [payload]
        elif isinstance(payload, list):
            records = payload
        else:
            accumulator.add_error(
                ValidationIssue(
                    record=None,
                    field=None,
                    code=ValidationErrorCode.INVALID_RECORD,
                    message="Top-level JSON must be an object or an array of objects",
                )
            )
            return counts

        for index, record in enumerate(records, start=1):
            counts.records_checked += 1

            if not isinstance(record, dict):
                counts.invalid_records += 1
                accumulator.add_error(
                    ValidationIssue(
                        record=index,
                        field=None,
                        code=ValidationErrorCode.INVALID_RECORD,
                        message="Record is not a JSON object",
                    )
                )
                continue

            keys = record.keys()
            issues = (
                evaluator.check_missing_required(keys, index)
                + evaluator.check_values(index, record)
                + evaluator.check_unexpected(keys, index)
            )
            if issues:
                counts.invalid_records += 1
                accumulator.add_errors(issues)
            else:
                counts.valid_records += 1

        if counts.records_checked == 0:
            accumulator.add_error(
                ValidationIssue(
                    record=None,
                    field=None,
                    code=ValidationErrorCode.EMPTY_DATASET,
                    message="No records found in the uploaded file",
                )
            )

        return counts
