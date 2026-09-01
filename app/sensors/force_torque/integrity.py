"""Force/Torque integrity checks for force_torque v1.0.0.

Mirrors ImuIntegrityChecker's structure exactly (see
app.integrity.checks.imu): finiteness on every component, an existing
generic TimestampSequenceChecker for ordering/duplicate detection, and
configurable, optional extreme-value WARNING thresholds — never a
universal physical limit. Real force/torque sensors span an enormous
range depending on hardware (a fingertip sensor vs. a robot base
mount), so there is no single hard-failure threshold this project could
honestly assert; see ForceTorqueThresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from app.integrity.checks.base import (
    IntegrityChecker,
    IntegrityIssueAccumulator,
    IntegrityRecordCounts,
    to_float,
)
from app.integrity.checks.common import TimestampSequenceChecker, check_finite
from app.integrity.models import IntegrityErrorCode, IntegrityIssue, IntegritySeverity

FORCE_FIELDS = ("force_x", "force_y", "force_z")
TORQUE_FIELDS = ("torque_x", "torque_y", "torque_z")


@dataclass(frozen=True)
class ForceTorqueThresholds:
    """Optional plausibility thresholds, not physical limits -- force/
    torque magnitude depends entirely on the specific sensor/mount, so
    these default to None (disabled) rather than an arbitrary guessed
    number. A caller who knows their hardware's real operating range can
    supply one or both."""

    max_abs_force_n: float | None = None
    max_abs_torque_nm: float | None = None


class ForceTorqueIntegrityChecker(IntegrityChecker):
    def __init__(self, thresholds: ForceTorqueThresholds | None = None) -> None:
        self._thresholds = thresholds or ForceTorqueThresholds()

    def check_stream(
        self, records: Iterator[tuple[int, dict]], accumulator: IntegrityIssueAccumulator
    ) -> IntegrityRecordCounts:
        timestamp_checker = TimestampSequenceChecker()
        counts = IntegrityRecordCounts()

        for record_number, record in records:
            counts.total_records += 1
            counts.checked_records += 1
            record_issues: list[IntegrityIssue] = []

            for field_name in FORCE_FIELDS:
                record_issues.extend(
                    self._check_axis(
                        record_number,
                        field_name,
                        record.get(field_name),
                        limit=self._thresholds.max_abs_force_n,
                        extreme_code=IntegrityErrorCode.FORCE_TORQUE_FORCE_EXTREME,
                        extreme_label="Force",
                    )
                )
            for field_name in TORQUE_FIELDS:
                record_issues.extend(
                    self._check_axis(
                        record_number,
                        field_name,
                        record.get(field_name),
                        limit=self._thresholds.max_abs_torque_nm,
                        extreme_code=IntegrityErrorCode.FORCE_TORQUE_TORQUE_EXTREME,
                        extreme_label="Torque",
                    )
                )

            record_issues.extend(timestamp_checker.check(record_number, record))

            accumulator.add_all(record_issues)
            if any(issue.severity is IntegritySeverity.ERROR for issue in record_issues):
                counts.failed_records += 1
            else:
                counts.passed_records += 1

        return counts

    @staticmethod
    def _check_axis(
        record_number: int,
        field_name: str,
        raw_value: object,
        *,
        limit: float | None,
        extreme_code: IntegrityErrorCode,
        extreme_label: str,
    ) -> list[IntegrityIssue]:
        value = to_float(raw_value)
        if value is None:
            return []

        finite_issue = check_finite(record_number, field_name, value, raw_value)
        if finite_issue is not None:
            return [finite_issue]

        if limit is None or abs(value) <= limit:
            return []

        return [
            IntegrityIssue(
                record_number=record_number,
                field=field_name,
                code=extreme_code,
                severity=IntegritySeverity.WARNING,
                message=(
                    f"{extreme_label} field '{field_name}' has magnitude {abs(value)} "
                    f"exceeding the configured plausibility threshold {limit}"
                ),
                value=value,
            )
        ]
