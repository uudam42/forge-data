"""GPS-specific integrity checks for gps v1.0.0 (and compatible schemas).

Latitude/longitude range checks live here — not in Step 2 — because they
are semantic ("is this value physically possible on Earth"), not structural
("is this a float"). See the Step 2 vs Step 3 boundary in the README.
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


@dataclass(frozen=True)
class GpsLimits:
    """Latitude/longitude bounds are physical facts, not tunable heuristics
    (unlike ImuThresholds) — a real Earth latitude cannot exceed 90. Kept
    configurable anyway for consistency and to support a future schema with
    a different coordinate convention.
    """

    latitude_min: float = -90.0
    latitude_max: float = 90.0
    longitude_min: float = -180.0
    longitude_max: float = 180.0


class GpsIntegrityChecker(IntegrityChecker):
    def __init__(self, limits: GpsLimits | None = None) -> None:
        self._limits = limits or GpsLimits()

    def check_stream(
        self, records: Iterator[tuple[int, dict]], accumulator: IntegrityIssueAccumulator
    ) -> IntegrityRecordCounts:
        timestamp_checker = TimestampSequenceChecker()
        counts = IntegrityRecordCounts()

        for record_number, record in records:
            counts.total_records += 1
            counts.checked_records += 1
            record_issues: list[IntegrityIssue] = []

            record_issues.extend(self._check_latitude(record_number, record))
            record_issues.extend(self._check_longitude(record_number, record))
            record_issues.extend(self._check_speed(record_number, record))
            record_issues.extend(self._check_altitude(record_number, record))
            record_issues.extend(timestamp_checker.check(record_number, record))

            accumulator.add_all(record_issues)
            if any(issue.severity is IntegritySeverity.ERROR for issue in record_issues):
                counts.failed_records += 1
            else:
                counts.passed_records += 1

        return counts

    def _check_latitude(self, record_number: int, record: dict) -> list[IntegrityIssue]:
        latitude = to_float(record.get("latitude"))
        if latitude is None:
            return []
        finite_issue = check_finite(record_number, "latitude", latitude, record.get("latitude"))
        if finite_issue is not None:
            return [finite_issue]
        if self._limits.latitude_min <= latitude <= self._limits.latitude_max:
            return []
        return [
            IntegrityIssue(
                record_number=record_number,
                field="latitude",
                code=IntegrityErrorCode.GPS_LATITUDE_OUT_OF_RANGE,
                severity=IntegritySeverity.ERROR,
                message=(
                    f"Latitude {latitude} is outside the valid range "
                    f"[{self._limits.latitude_min}, {self._limits.latitude_max}]"
                ),
                value=latitude,
            )
        ]

    def _check_longitude(self, record_number: int, record: dict) -> list[IntegrityIssue]:
        longitude = to_float(record.get("longitude"))
        if longitude is None:
            return []
        finite_issue = check_finite(record_number, "longitude", longitude, record.get("longitude"))
        if finite_issue is not None:
            return [finite_issue]
        if self._limits.longitude_min <= longitude <= self._limits.longitude_max:
            return []
        return [
            IntegrityIssue(
                record_number=record_number,
                field="longitude",
                code=IntegrityErrorCode.GPS_LONGITUDE_OUT_OF_RANGE,
                severity=IntegritySeverity.ERROR,
                message=(
                    f"Longitude {longitude} is outside the valid range "
                    f"[{self._limits.longitude_min}, {self._limits.longitude_max}]"
                ),
                value=longitude,
            )
        ]

    def _check_speed(self, record_number: int, record: dict) -> list[IntegrityIssue]:
        if "speed" not in record:
            return []
        speed = to_float(record.get("speed"))
        if speed is None:
            return []
        finite_issue = check_finite(record_number, "speed", speed, record.get("speed"))
        if finite_issue is not None:
            return [finite_issue]
        if speed >= 0:
            return []
        return [
            IntegrityIssue(
                record_number=record_number,
                field="speed",
                code=IntegrityErrorCode.GPS_NEGATIVE_SPEED,
                severity=IntegritySeverity.ERROR,
                message=f"Speed {speed} is negative",
                value=speed,
            )
        ]

    def _check_altitude(self, record_number: int, record: dict) -> list[IntegrityIssue]:
        if "altitude" not in record:
            return []
        altitude = to_float(record.get("altitude"))
        if altitude is None:
            return []
        finite_issue = check_finite(record_number, "altitude", altitude, record.get("altitude"))
        return [finite_issue] if finite_issue is not None else []
