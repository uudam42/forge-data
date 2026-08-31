"""CSV structural validator.

Processes rows incrementally via csv.DictReader — never loads the whole file
into memory. CSV cells always arrive as strings, so type-checking goes
through explicit, documented conversion rules (see csv_value_matches_type in
base.py) rather than silent coercion.

Missing-required-column and unexpected-column checks run once against the
header, not once per row — every row shares the same columns, so re-running
those checks per row would just repeat the same finding thousands of times.
"""

from __future__ import annotations

import csv
import io
from typing import BinaryIO

from app.validation.models import ValidationErrorCode, ValidationIssue
from app.validation.schemas.base import SchemaDefinition
from app.validation.validators.base import ErrorAccumulator, RecordCounts, RecordEvaluator, Validator


class CsvValidator(Validator):
    def validate(
        self, stream: BinaryIO, schema: SchemaDefinition, accumulator: ErrorAccumulator
    ) -> RecordCounts:
        evaluator = RecordEvaluator(schema, values_are_strings=True)
        text_stream = io.TextIOWrapper(stream, encoding="utf-8", newline="")
        reader = csv.DictReader(text_stream)
        header_fields = reader.fieldnames or []

        accumulator.add_errors(evaluator.check_missing_required(header_fields))
        accumulator.add_errors(evaluator.check_unexpected(header_fields))

        counts = RecordCounts()
        for row in reader:
            counts.records_checked += 1
            row_issues: list[ValidationIssue] = []

            # csv.DictReader's default restkey (None) collects any columns
            # beyond the header count — a genuinely malformed row.
            if row.get(None):
                row_issues.append(
                    ValidationIssue(
                        record=counts.records_checked,
                        field=None,
                        code=ValidationErrorCode.INVALID_RECORD,
                        message="Row has more columns than the header",
                    )
                )

            row_issues.extend(evaluator.check_values(counts.records_checked, row))

            if row_issues:
                counts.invalid_records += 1
                accumulator.add_errors(row_issues)
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
