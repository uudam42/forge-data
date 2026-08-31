"""IMU-specific integrity checks for imu v1.0.0 (and compatible schemas).

Extreme-value thresholds are configurable plausibility defaults, not
physical laws — see ImuThresholds docstring.
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

_ACCEL_FIELDS = ("accel_x", "accel_y", "accel_z")
_GYRO_FIELDS = ("gyro_x", "gyro_y", "gyro_z")


@dataclass(frozen=True)
class ImuThresholds:
    """Sanity-check thresholds, not universal physical guarantees.

    A real IMU on a rocket sled or in a crash test could legitimately
    exceed these — they exist to flag data that is *usually* worth a second
    look, not to assert a hard physical limit. That's why exceeding them is
    a warning, not an error, and why they're a parameter here rather than a
    literal buried in the check logic: tune per fleet/use-case as needed.
    """

    max_abs_acceleration_mps2: float = 200.0
    max_abs_gyro_rad_s: float = 50.0


class ImuIntegrityChecker(IntegrityChecker):
    def __init__(self, thresholds: ImuThresholds | None = None) -> None:
        self._thresholds = thresholds or ImuThresholds()

    def check_stream(
        self, records: Iterator[tuple[int, dict]], accumulator: IntegrityIssueAccumulator
    ) -> IntegrityRecordCounts:
        timestamp_checker = TimestampSequenceChecker()
        counts = IntegrityRecordCounts()

        for record_number, record in records:
            counts.total_records += 1
            counts.checked_records += 1
            record_issues: list[IntegrityIssue] = []

            for field_name in _ACCEL_FIELDS:
                record_issues.extend(
                    self._check_axis(
                        record_number,
                        field_name,
                        record.get(field_name),
                        limit=self._thresholds.max_abs_acceleration_mps2,
                        extreme_code=IntegrityErrorCode.IMU_ACCELERATION_EXTREME,
                        extreme_label="Acceleration",
                    )
                )
            for field_name in _GYRO_FIELDS:
                record_issues.extend(
                    self._check_axis(
                        record_number,
                        field_name,
                        record.get(field_name),
                        limit=self._thresholds.max_abs_gyro_rad_s,
                        extreme_code=IntegrityErrorCode.IMU_GYRO_EXTREME,
                        extreme_label="Gyro",
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
        limit: float,
        extreme_code: IntegrityErrorCode,
        extreme_label: str,
    ) -> list[IntegrityIssue]:
        value = to_float(raw_value)
        if value is None:
            return []

        finite_issue = check_finite(record_number, field_name, value, raw_value)
        if finite_issue is not None:
            return [finite_issue]

        if abs(value) <= limit:
            return []

        return [
            IntegrityIssue(
                record_number=record_number,
                field=field_name,
                code=extreme_code,
                severity=IntegritySeverity.WARNING,
                message=(
                    f"{extreme_label} field '{field_name}' has magnitude {abs(value)} "
                    f"exceeding the plausibility threshold {limit}"
                ),
                value=value,
            )
        ]
