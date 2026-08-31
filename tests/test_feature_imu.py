"""Unit tests for the IMU feature extractor
(app.transformation.features.imu)."""

from __future__ import annotations

import math

import pytest

from app.transformation.features.base import WindowRow
from app.transformation.features.common import InvalidNumericValueError, UnknownFeatureError
from app.transformation.features.imu import ImuFeatureExtractor
from app.transformation.models import StreamFeatureConfig

EXTRACTOR = ImuFeatureExtractor()


def _row(row_index: int, epoch_us: int, **fields) -> WindowRow:
    payload = fields or None
    return WindowRow(row_index=row_index, epoch_us=epoch_us, payload=payload)


def _full_rows(n: int) -> list[WindowRow]:
    return [
        _row(i, i * 1000, accel_x=0.1 * i, accel_y=0.2, accel_z=9.8, gyro_x=0.01, gyro_y=0.02, gyro_z=0.03)
        for i in range(n)
    ]


def test_validate_config_accepts_known_statistics_and_derived() -> None:
    EXTRACTOR.validate_config(StreamFeatureConfig(statistics=["mean", "std"], derived=["accel_magnitude"]))


def test_validate_config_rejects_unknown_statistic() -> None:
    with pytest.raises(UnknownFeatureError):
        EXTRACTOR.validate_config(StreamFeatureConfig(statistics=["bogus"]))


def test_validate_config_rejects_unknown_derived_feature() -> None:
    with pytest.raises(UnknownFeatureError):
        EXTRACTOR.validate_config(StreamFeatureConfig(derived=["orientation"]))


def test_raw_sequences_included_when_requested() -> None:
    rows = _full_rows(5)
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(include_raw=True))
    assert result.features["raw"]["accel_x"] == pytest.approx([0.0, 0.1, 0.2, 0.3, 0.4])
    assert result.features["raw"]["accel_z"] == [9.8] * 5


def test_raw_omitted_when_not_requested() -> None:
    rows = _full_rows(5)
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(include_raw=False, statistics=["mean"]))
    assert "raw" not in result.features


def test_statistics_mean_std_min_max() -> None:
    rows = [
        _row(0, 0, accel_x=1.0, accel_y=0.0, accel_z=0.0, gyro_x=0.0, gyro_y=0.0, gyro_z=0.0),
        _row(1, 1000, accel_x=3.0, accel_y=0.0, accel_z=0.0, gyro_x=0.0, gyro_y=0.0, gyro_z=0.0),
    ]
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(statistics=["mean", "std", "min", "max"]))
    stats = result.features["statistics"]
    assert stats["accel_x_mean"] == 2.0
    assert stats["accel_x_min"] == 1.0
    assert stats["accel_x_max"] == 3.0
    assert stats["accel_x_std"] == 1.0  # population std of [1,3]


def test_accel_magnitude_correctness() -> None:
    rows = [_row(0, 0, accel_x=3.0, accel_y=4.0, accel_z=0.0, gyro_x=0.0, gyro_y=0.0, gyro_z=0.0)]
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(include_raw=True, derived=["accel_magnitude"]))
    assert result.features["raw"]["accel_magnitude"] == [5.0]


def test_gyro_magnitude_correctness() -> None:
    rows = [_row(0, 0, accel_x=0.0, accel_y=0.0, accel_z=0.0, gyro_x=0.0, gyro_y=3.0, gyro_z=4.0)]
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(include_raw=True, derived=["gyro_magnitude"]))
    assert result.features["raw"]["gyro_magnitude"] == [5.0]


def test_derived_magnitude_statistics_available_alongside_axis_statistics() -> None:
    rows = [
        _row(0, 0, accel_x=3.0, accel_y=4.0, accel_z=0.0, gyro_x=0.0, gyro_y=0.0, gyro_z=0.0),
        _row(1, 1000, accel_x=6.0, accel_y=8.0, accel_z=0.0, gyro_x=0.0, gyro_y=0.0, gyro_z=0.0),
    ]
    result = EXTRACTOR.extract(
        rows, StreamFeatureConfig(statistics=["mean", "max"], derived=["accel_magnitude"])
    )
    stats = result.features["statistics"]
    assert stats["accel_magnitude_mean"] == 7.5
    assert stats["accel_magnitude_max"] == 10.0


def test_window_with_no_imu_rows_present() -> None:
    rows = [_row(0, 0), _row(1, 1000)]  # payload=None for both
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(statistics=["mean"]))
    assert result.present is False
    assert result.present_count == 0
    assert result.missing_count == 2
    assert result.features["statistics"]["accel_x_mean"] is None


def test_partial_axis_missing_within_a_row_excludes_only_that_axis_and_magnitude() -> None:
    row = WindowRow(row_index=0, epoch_us=0, payload={"accel_x": 1.0, "accel_y": None, "accel_z": 2.0})
    result = EXTRACTOR.extract([row], StreamFeatureConfig(include_raw=True, derived=["accel_magnitude"]))
    assert result.features["raw"]["accel_x"] == [1.0]
    assert "accel_y" not in result.features["raw"]  # no values collected
    # magnitude requires all three axes present -> no magnitude sample for this row
    assert "accel_magnitude" not in result.features["raw"]


def test_non_finite_value_raises() -> None:
    row = WindowRow(row_index=0, epoch_us=0, payload={"accel_x": float("nan")})
    with pytest.raises(InvalidNumericValueError):
        EXTRACTOR.extract([row], StreamFeatureConfig(statistics=["mean"]))


def test_infinite_value_raises() -> None:
    row = WindowRow(row_index=0, epoch_us=0, payload={"accel_x": math.inf})
    with pytest.raises(InvalidNumericValueError):
        EXTRACTOR.extract([row], StreamFeatureConfig(statistics=["mean"]))


def test_no_include_raw_and_no_statistics_and_no_derived_yields_no_features() -> None:
    rows = _full_rows(3)
    result = EXTRACTOR.extract(rows, StreamFeatureConfig())
    assert result.features is None


def test_input_rows_not_mutated() -> None:
    rows = _full_rows(3)
    snapshot = [dict(r.payload) for r in rows]
    EXTRACTOR.extract(rows, StreamFeatureConfig(include_raw=True, statistics=["mean"], derived=["accel_magnitude"]))
    assert [r.payload for r in rows] == snapshot
