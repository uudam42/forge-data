"""Unit tests for LinearInterpolationStrategy."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.synchronization.strategies.base import AlignmentContext, StreamCursor
from app.synchronization.strategies.linear import LinearInterpolationStrategy
from app.validation.schemas.registry import SchemaRegistry
from app.core.config import _default_schema_dir

SCHEMA_DIR = _default_schema_dir()


@pytest.fixture
def imu_schema():
    registry = SchemaRegistry(schema_dir=SCHEMA_DIR)
    return registry.get(schema_name="imu", schema_version="1.0.0")


def _cursor(samples: list[tuple[int, dict]]) -> StreamCursor:
    return StreamCursor(iter((i, epoch_us, record) for i, (epoch_us, record) in enumerate(samples, start=1)))


def _align(cursor, target_us, tolerance_us, schema):
    context = AlignmentContext(target_epoch_us=target_us, tolerance_us=tolerance_us, schema=schema)
    cursor.advance_to(target_us)
    return LinearInterpolationStrategy().align(cursor, context)


def test_linear_interpolation_correct(imu_schema) -> None:
    t0 = {"accel_x": 0.0, "accel_y": 0.0, "accel_z": 0.0, "device_id": "d1"}
    t1 = {"accel_x": 10.0, "accel_y": 0.0, "accel_z": 0.0, "device_id": "d1"}
    cursor = _cursor([(0, t0), (10_000_000, t1)])  # 0s and 10s

    record, outcome = _align(cursor, 5_000_000, tolerance_us=1_000_000, schema=imu_schema)  # midpoint

    assert record["accel_x"] == pytest.approx(5.0)
    assert outcome.matched is True
    assert outcome.method == "linear"


def test_linear_interpolation_refuses_extrapolation_before_range(imu_schema) -> None:
    cursor = _cursor([(5_000_000, {"accel_x": 1.0}), (10_000_000, {"accel_x": 2.0})])

    record, outcome = _align(cursor, 0, tolerance_us=1_000_000, schema=imu_schema)  # before first sample

    assert record is None
    assert outcome.matched is False
    assert outcome.reason == "NO_EXTRAPOLATION"


def test_linear_interpolation_refuses_extrapolation_after_range(imu_schema) -> None:
    cursor = _cursor([(0, {"accel_x": 1.0}), (5_000_000, {"accel_x": 2.0})])

    record, outcome = _align(cursor, 10_000_000, tolerance_us=1_000_000, schema=imu_schema)  # after last

    assert record is None
    assert outcome.matched is False
    assert outcome.reason == "NO_EXTRAPOLATION"


def test_linear_numeric_fields_interpolate(imu_schema) -> None:
    t0 = {"accel_x": 1.0, "accel_y": 2.0, "accel_z": 3.0}
    t1 = {"accel_x": 3.0, "accel_y": 4.0, "accel_z": 9.0}
    cursor = _cursor([(0, t0), (4_000_000, t1)])

    record, _ = _align(cursor, 1_000_000, tolerance_us=1_000_000, schema=imu_schema)  # 25% of the way

    assert record["accel_x"] == pytest.approx(1.5)
    assert record["accel_y"] == pytest.approx(2.5)
    assert record["accel_z"] == pytest.approx(4.5)


def test_linear_non_numeric_fields_use_nearest_not_interpolation(imu_schema) -> None:
    t0 = {"accel_x": 0.0, "device_id": "device_a"}
    t1 = {"accel_x": 10.0, "device_id": "device_b"}
    cursor = _cursor([(0, t0), (10_000_000, t1)])

    # target closer to t0 (3s of 10s) -> device_id should be "device_a", not
    # an interpolated/invented value.
    record, _ = _align(cursor, 3_000_000, tolerance_us=10_000_000, schema=imu_schema)

    assert record["device_id"] == "device_a"
    assert record["accel_x"] == pytest.approx(3.0)


def test_linear_non_numeric_field_outside_tolerance_is_null(imu_schema) -> None:
    t0 = {"accel_x": 0.0, "device_id": "device_a"}
    t1 = {"accel_x": 10.0, "device_id": "device_b"}
    cursor = _cursor([(0, t0), (10_000_000, t1)])

    # nearest bracket to target is 4_000_000us away — outside a 1ms tolerance.
    record, _ = _align(cursor, 5_000_000, tolerance_us=1_000, schema=imu_schema)

    assert record["device_id"] is None
    assert record["accel_x"] == pytest.approx(5.0)  # numeric fields aren't tolerance-gated


def test_exact_timestamp_match_short_circuits_interpolation(imu_schema) -> None:
    t0 = {"accel_x": 1.0}
    t1 = {"accel_x": 999.0}
    cursor = _cursor([(0, t0), (10_000_000, t1)])

    record, outcome = _align(cursor, 0, tolerance_us=100, schema=imu_schema)

    assert record == t0
    assert outcome.delta_ms == 0.0
    assert outcome.is_exact is True


def test_exact_match_on_second_bracket(imu_schema) -> None:
    t0 = {"accel_x": 1.0}
    t1 = {"accel_x": 999.0}
    cursor = _cursor([(0, t0), (10_000_000, t1)])

    record, outcome = _align(cursor, 10_000_000, tolerance_us=100, schema=imu_schema)

    assert record == t1
    assert outcome.is_exact is True


def test_linear_missing_numeric_endpoint_yields_null_for_that_field(imu_schema) -> None:
    t0 = {"accel_x": None}
    t1 = {"accel_x": 10.0}
    cursor = _cursor([(0, t0), (10_000_000, t1)])

    record, outcome = _align(cursor, 5_000_000, tolerance_us=100, schema=imu_schema)

    assert record["accel_x"] is None
    assert outcome.matched is True  # the row itself still matches; only that field is null
