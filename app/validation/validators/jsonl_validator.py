"""JSONL structural validator — one JSON object per line.

Processes lines incrementally — never loads the whole file into memory.
Line numbers (1-indexed) are used as the record identifier so report errors
map directly back to the source file. Blank lines are ignored and not
counted as records. A malformed line produces an INVALID_RECORD issue but
does not stop validation of subsequent lines.
"""

from __future__ import annotations

import io
import json
from typing import BinaryIO

from app.validation.models import ValidationErrorCode, ValidationIssue
from app.validation.schemas.base import SchemaDefinition
from app.validation.validators.base import ErrorAccumulator, RecordCounts, RecordEvaluator, Validator


class JsonlValidator(Validator):
    def validate(
        self, stream: BinaryIO, schema: SchemaDefinition, accumulator: ErrorAccumulator
    ) -> RecordCounts:
        evaluator = RecordEvaluator(schema, values_are_strings=False)
        counts = RecordCounts()
        text_stream = io.TextIOWrapper(stream, encoding="utf-8")

        for line_number, raw_line in enumerate(text_stream, start=1):
            line = raw_line.strip()
            if not line:
                continue

            counts.records_checked += 1

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                counts.invalid_records += 1
                accumulator.add_error(
                    ValidationIssue(
                        record=line_number,
                        field=None,
                        code=ValidationErrorCode.INVALID_RECORD,
                        message="Malformed JSON on this line",
                    )
                )
                continue

            if not isinstance(record, dict):
                counts.invalid_records += 1
                accumulator.add_error(
                    ValidationIssue(
                        record=line_number,
                        field=None,
                        code=ValidationErrorCode.INVALID_RECORD,
                        message="Line is not a JSON object",
                    )
                )
                continue

            keys = record.keys()
            issues = (
                evaluator.check_missing_required(keys, line_number)
                + evaluator.check_values(line_number, record)
                + evaluator.check_unexpected(keys, line_number)
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
