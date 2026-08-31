"""Reusable, schema-agnostic integrity checks.

Timestamp ordering and duplicate-timestamp detection need only the previous
record's timestamp — O(1) state regardless of file size — so one stateful
checker (TimestampSequenceChecker) does both in a single pass. Comparisons
are only ever made within a single file, never across ingestions.

Numeric finiteness (check_finite) is a plain function, not stateful, and is
shared by every numeric-field check in the GPS and IMU checkers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from app.integrity.checks.base import parse_timestamp
from app.integrity.models import IntegrityErrorCode, IntegrityIssue, IntegritySeverity


@dataclass
class TimestampSequenceChecker:
    field_name: str = "timestamp"
    _previous_value: datetime | None = field(default=None, init=False, repr=False)

    def check(self, record_number: int, record: dict) -> list[IntegrityIssue]:
        raw_value = record.get(self.field_name)
        current = parse_timestamp(raw_value)
        if current is None:
            return []

        issues: list[IntegrityIssue] = []
        if self._previous_value is not None:
            if current < self._previous_value:
                issues.append(
                    IntegrityIssue(
                        record_number=record_number,
                        field=self.field_name,
                        code=IntegrityErrorCode.TIMESTAMP_OUT_OF_ORDER,
                        severity=IntegritySeverity.ERROR,
                        message=(
                            f"Timestamp '{raw_value}' is earlier than the previous "
                            "record's timestamp"
                        ),
                        value=raw_value,
                    )
                )
            elif current == self._previous_value:
                issues.append(
                    IntegrityIssue(
                        record_number=record_number,
                        field=self.field_name,
                        code=IntegrityErrorCode.DUPLICATE_TIMESTAMP,
                        severity=IntegritySeverity.WARNING,
                        message=(
                            f"Timestamp '{raw_value}' duplicates the previous "
                            "record's timestamp"
                        ),
                        value=raw_value,
                    )
                )

        self._previous_value = current
        return issues


def check_finite(
    record_number: int, field_name: str, value: float, raw_value: object
) -> IntegrityIssue | None:
    """Returns a NON_FINITE_VALUE error if value is NaN/+Inf/-Inf, else None.

    Necessary even though Step 2 already type-checked this field: Python's
    float() and json.loads() both accept "nan"/"inf"/"-inf" (CSV) and bare
    NaN/Infinity tokens (JSON) as valid floats, so Step 2's INVALID_TYPE
    check does not catch them. This is a genuine gap Step 2 cannot close
    without becoming a semantic checker itself.
    """
    if math.isfinite(value):
        return None
    # Never persist a literal NaN/Infinity into the JSON report (Python's
    # json.dumps would emit non-standard NaN/Infinity tokens) — store the
    # string form instead.
    return IntegrityIssue(
        record_number=record_number,
        field=field_name,
        code=IntegrityErrorCode.NON_FINITE_VALUE,
        severity=IntegritySeverity.ERROR,
        message=f"Field '{field_name}' is not a finite number (got {raw_value!r})",
        value=str(raw_value),
    )
