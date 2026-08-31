"""Unit tests for GPS/IMU integrity checkers, common checks, and the
streaming record readers — exercised independently of the API layer.
"""

from __future__ import annotations

import inspect
import io
import math

from app.integrity.checks.base import IntegrityIssueAccumulator
from app.integrity.checks.gps import GpsIntegrityChecker
from app.integrity.checks.imu import ImuIntegrityChecker, ImuThresholds
from app.integrity import records as integrity_records


def _run(checker, records: list[dict], max_issues: int = 1000):
    accumulator = IntegrityIssueAccumulator(max_issues=max_issues)
    counts = checker.check_stream(enumerate(records, start=1), accumulator)
    return counts, accumulator


def _codes(accumulator) -> list[str]:
    return [issue.code.value for issue in accumulator.issues]


# ---------------------------------------------------------------------------
# GPS
# ---------------------------------------------------------------------------


def test_valid_latitude_accepted() -> None:
    counts, acc = _run(GpsIntegrityChecker(), [{"timestamp": "2026-08-29T00:00:00Z", "latitude": 45.0, "longitude": 10.0}])
    assert acc.error_count == 0
    assert counts.passed_records == 1


def test_latitude_above_90_rejected() -> None:
    counts, acc = _run(GpsIntegrityChecker(), [{"timestamp": "2026-08-29T00:00:00Z", "latitude": 120.0, "longitude": 10.0}])
    assert "GPS_LATITUDE_OUT_OF_RANGE" in _codes(acc)
    assert counts.failed_records == 1


def test_latitude_below_negative_90_rejected() -> None:
    counts, acc = _run(GpsIntegrityChecker(), [{"timestamp": "2026-08-29T00:00:00Z", "latitude": -91.0, "longitude": 10.0}])
    assert "GPS_LATITUDE_OUT_OF_RANGE" in _codes(acc)
    assert counts.failed_records == 1


def test_valid_longitude_accepted() -> None:
    counts, acc = _run(GpsIntegrityChecker(), [{"timestamp": "2026-08-29T00:00:00Z", "latitude": 0.0, "longitude": -120.0}])
    assert acc.error_count == 0
    assert counts.passed_records == 1


def test_longitude_above_180_rejected() -> None:
    counts, acc = _run(GpsIntegrityChecker(), [{"timestamp": "2026-08-29T00:00:00Z", "latitude": 0.0, "longitude": 200.0}])
    assert "GPS_LONGITUDE_OUT_OF_RANGE" in _codes(acc)


def test_longitude_below_negative_180_rejected() -> None:
    counts, acc = _run(GpsIntegrityChecker(), [{"timestamp": "2026-08-29T00:00:00Z", "latitude": 0.0, "longitude": -200.0}])
    assert "GPS_LONGITUDE_OUT_OF_RANGE" in _codes(acc)


def test_boundary_latitude_longitude_are_valid() -> None:
    # inclusive bounds: exactly -90/90 and -180/180 must be accepted
    counts, acc = _run(
        GpsIntegrityChecker(),
        [
            {"timestamp": "2026-08-29T00:00:00Z", "latitude": 90.0, "longitude": 180.0},
            {"timestamp": "2026-08-29T00:00:01Z", "latitude": -90.0, "longitude": -180.0},
        ],
    )
    assert acc.error_count == 0
    assert counts.passed_records == 2


def test_non_negative_speed_accepted() -> None:
    counts, acc = _run(
        GpsIntegrityChecker(),
        [{"timestamp": "2026-08-29T00:00:00Z", "latitude": 0.0, "longitude": 0.0, "speed": 12.5}],
    )
    assert acc.error_count == 0


def test_negative_speed_rejected() -> None:
    counts, acc = _run(
        GpsIntegrityChecker(),
        [{"timestamp": "2026-08-29T00:00:00Z", "latitude": 0.0, "longitude": 0.0, "speed": -1.0}],
    )
    assert "GPS_NEGATIVE_SPEED" in _codes(acc)
    assert counts.failed_records == 1


def test_gps_ordered_timestamps_accepted() -> None:
    counts, acc = _run(
        GpsIntegrityChecker(),
        [
            {"timestamp": "2026-08-29T00:00:00Z", "latitude": 0.0, "longitude": 0.0},
            {"timestamp": "2026-08-29T00:00:01Z", "latitude": 0.0, "longitude": 0.0},
            {"timestamp": "2026-08-29T00:00:02Z", "latitude": 0.0, "longitude": 0.0},
        ],
    )
    assert acc.error_count == 0
    assert acc.warning_count == 0


def test_gps_out_of_order_timestamp_rejected() -> None:
    counts, acc = _run(
        GpsIntegrityChecker(),
        [
            {"timestamp": "2026-08-29T00:00:05Z", "latitude": 0.0, "longitude": 0.0},
            {"timestamp": "2026-08-29T00:00:01Z", "latitude": 0.0, "longitude": 0.0},
        ],
    )
    assert "TIMESTAMP_OUT_OF_ORDER" in _codes(acc)
    issue = next(i for i in acc.issues if i.code.value == "TIMESTAMP_OUT_OF_ORDER")
    assert issue.severity.value == "error"
    assert issue.record_number == 2


def test_gps_duplicate_timestamp_is_warning_not_error() -> None:
    counts, acc = _run(
        GpsIntegrityChecker(),
        [
            {"timestamp": "2026-08-29T00:00:00Z", "latitude": 0.0, "longitude": 0.0},
            {"timestamp": "2026-08-29T00:00:00Z", "latitude": 0.0, "longitude": 0.0},
        ],
    )
    assert "DUPLICATE_TIMESTAMP" in _codes(acc)
    issue = next(i for i in acc.issues if i.code.value == "DUPLICATE_TIMESTAMP")
    assert issue.severity.value == "warning"
    assert acc.error_count == 0


# ---------------------------------------------------------------------------
# IMU
# ---------------------------------------------------------------------------


def _imu_record(**overrides) -> dict:
    record = {
        "timestamp": "2026-08-29T00:00:00Z",
        "accel_x": 0.1,
        "accel_y": 0.2,
        "accel_z": 9.8,
        "gyro_x": 0.01,
        "gyro_y": 0.02,
        "gyro_z": 0.03,
    }
    record.update(overrides)
    return record


def test_plausible_acceleration_accepted() -> None:
    counts, acc = _run(ImuIntegrityChecker(), [_imu_record()])
    assert acc.error_count == 0
    assert acc.warning_count == 0
    assert counts.passed_records == 1


def test_extreme_acceleration_produces_warning_not_error() -> None:
    counts, acc = _run(ImuIntegrityChecker(), [_imu_record(accel_x=500.0)])
    assert "IMU_ACCELERATION_EXTREME" in _codes(acc)
    issue = next(i for i in acc.issues if i.code.value == "IMU_ACCELERATION_EXTREME")
    assert issue.severity.value == "warning"
    assert acc.error_count == 0
    assert counts.passed_records == 1  # warnings don't fail a record


def test_plausible_gyro_accepted() -> None:
    counts, acc = _run(ImuIntegrityChecker(), [_imu_record(gyro_x=1.0)])
    assert acc.error_count == 0
    assert acc.warning_count == 0


def test_extreme_gyro_produces_warning() -> None:
    counts, acc = _run(ImuIntegrityChecker(), [_imu_record(gyro_z=75.0)])
    assert "IMU_GYRO_EXTREME" in _codes(acc)
    issue = next(i for i in acc.issues if i.code.value == "IMU_GYRO_EXTREME")
    assert issue.severity.value == "warning"


def test_imu_out_of_order_timestamp_rejected() -> None:
    counts, acc = _run(
        ImuIntegrityChecker(),
        [
            _imu_record(timestamp="2026-08-29T00:00:05Z"),
            _imu_record(timestamp="2026-08-29T00:00:01Z"),
        ],
    )
    assert "TIMESTAMP_OUT_OF_ORDER" in _codes(acc)
    assert counts.failed_records == 1


def test_imu_duplicate_timestamp_is_warning() -> None:
    counts, acc = _run(
        ImuIntegrityChecker(),
        [_imu_record(), _imu_record()],
    )
    assert "DUPLICATE_TIMESTAMP" in _codes(acc)
    issue = next(i for i in acc.issues if i.code.value == "DUPLICATE_TIMESTAMP")
    assert issue.severity.value == "warning"


def test_non_finite_acceleration_rejected() -> None:
    counts, acc = _run(ImuIntegrityChecker(), [_imu_record(accel_y=math.nan)])
    assert "NON_FINITE_VALUE" in _codes(acc)
    issue = next(i for i in acc.issues if i.code.value == "NON_FINITE_VALUE")
    assert issue.severity.value == "error"
    assert issue.field == "accel_y"
    assert counts.failed_records == 1


def test_non_finite_gyro_rejected() -> None:
    counts, acc = _run(ImuIntegrityChecker(), [_imu_record(gyro_x=math.inf)])
    assert "NON_FINITE_VALUE" in _codes(acc)
    issue = next(i for i in acc.issues if i.code.value == "NON_FINITE_VALUE")
    assert issue.field == "gyro_x"


def test_non_finite_value_from_csv_string_is_rejected() -> None:
    # CSV cells arrive as strings; Python's float() happily parses "nan"/"inf",
    # so Step 2 would not catch this — the integrity check must.
    counts, acc = _run(ImuIntegrityChecker(), [_imu_record(accel_x="nan")])
    assert "NON_FINITE_VALUE" in _codes(acc)


def test_custom_imu_thresholds_are_respected() -> None:
    lenient = ImuIntegrityChecker(thresholds=ImuThresholds(max_abs_acceleration_mps2=1000.0))
    counts, acc = _run(lenient, [_imu_record(accel_x=500.0)])
    assert acc.warning_count == 0


def test_issue_cap_truncates_but_keeps_counting() -> None:
    records = [
        _imu_record(timestamp=f"2026-08-29T00:00:{i:02d}Z", accel_x=500.0 + i) for i in range(10)
    ]
    counts, acc = _run(ImuIntegrityChecker(), records, max_issues=2)

    assert acc.warning_count == 10
    assert len(acc.issues) == 2
    assert acc.issues_truncated is True


# ---------------------------------------------------------------------------
# Streaming record readers
# ---------------------------------------------------------------------------


def test_csv_record_reader_is_a_generator() -> None:
    stream = io.BytesIO(b"timestamp,accel_x\n2026-08-29T00:00:00Z,0.1\n")
    result = integrity_records.iter_records(stream, ".csv")
    assert inspect.isgenerator(result)


def test_jsonl_record_reader_is_a_generator() -> None:
    stream = io.BytesIO(b'{"timestamp": "2026-08-29T00:00:00Z"}\n')
    result = integrity_records.iter_records(stream, ".jsonl")
    assert inspect.isgenerator(result)


def test_csv_record_reader_yields_expected_records() -> None:
    stream = io.BytesIO(b"timestamp,accel_x\n2026-08-29T00:00:00Z,0.1\n2026-08-29T00:00:01Z,0.2\n")
    results = list(integrity_records.iter_records(stream, ".csv"))
    assert results == [
        (1, {"timestamp": "2026-08-29T00:00:00Z", "accel_x": "0.1"}),
        (2, {"timestamp": "2026-08-29T00:00:01Z", "accel_x": "0.2"}),
    ]


def test_jsonl_record_reader_yields_expected_records() -> None:
    stream = io.BytesIO(b'{"a": 1}\n{"a": 2}\n')
    results = list(integrity_records.iter_records(stream, ".jsonl"))
    assert results == [(1, {"a": 1}), (2, {"a": 2})]


def test_json_record_reader_handles_array() -> None:
    stream = io.BytesIO(b'[{"a": 1}, {"a": 2}]')
    results = list(integrity_records.iter_records(stream, ".json"))
    assert results == [(1, {"a": 1}), (2, {"a": 2})]
