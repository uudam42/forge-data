"""Unit tests for the imu_canonical normalization profile: unit conversion,
field aliasing, and ambiguous-mapping detection.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.normalization.profiles.base import RecordNormalizer
from app.normalization.profiles.imu import IMU_CANONICAL_V1
from app.normalization.transforms.fields import AmbiguousFieldMappingError
from app.validation.schemas.registry import SchemaRegistry
from app.core.config import _default_schema_dir

SCHEMA_DIR = _default_schema_dir()


@pytest.fixture
def imu_schema():
    registry = SchemaRegistry(schema_dir=SCHEMA_DIR)
    return registry.get(schema_name="imu", schema_version="1.0.0")


def _normalizer(imu_schema, **source_units) -> RecordNormalizer:
    return RecordNormalizer(schema=imu_schema, profile=IMU_CANONICAL_V1, source_units=source_units)


def test_g_to_mps2_conversion_correct(imu_schema) -> None:
    normalizer = _normalizer(imu_schema, acceleration="g", angular_velocity="rad/s")
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-30T18:00:00-07:00",
            "accel_x": "1.0",
            "accel_y": "0.0",
            "accel_z": "-1.0",
            "gyro_x": "0",
            "gyro_y": "0",
            "gyro_z": "0",
        },
    )
    assert record["accel_x"] == 9.80665
    assert record["accel_y"] == 0.0
    assert record["accel_z"] == -9.80665


def test_deg_per_s_to_rad_per_s_conversion_correct(imu_schema) -> None:
    normalizer = _normalizer(imu_schema, acceleration="m/s^2", angular_velocity="deg/s")
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-30T18:00:00-07:00",
            "accel_x": "0.0",
            "accel_y": "0.0",
            "accel_z": "0.0",
            "gyro_x": "180",
            "gyro_y": "0",
            "gyro_z": "-180",
        },
    )
    assert record["gyro_x"] == pytest.approx(math.pi)
    assert record["gyro_y"] == 0.0
    assert record["gyro_z"] == pytest.approx(-math.pi)


def test_full_worked_example_matches_spec(imu_schema) -> None:
    """timestamp,Accel X,Accel Y,Accel Z,gyro_x,gyro_y,gyro_z with g/deg-s."""
    normalizer = _normalizer(imu_schema, acceleration="g", angular_velocity="deg/s")
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-30T18:00:00-07:00",
            "Accel X": "1.0",
            "Accel Y": "0.0",
            "Accel Z": "-1.0",
            "gyro_x": "180",
            "gyro_y": "0",
            "gyro_z": "-180",
        },
    )
    assert record["timestamp"] == "2026-08-31T01:00:00Z"
    assert record["accel_x"] == 9.80665
    assert record["accel_y"] == 0.0
    assert record["accel_z"] == -9.80665
    assert record["gyro_x"] == pytest.approx(math.pi)
    assert record["gyro_y"] == 0.0
    assert record["gyro_z"] == pytest.approx(-math.pi)


def test_field_alias_normalization_works(imu_schema) -> None:
    normalizer = _normalizer(imu_schema, acceleration="g", angular_velocity="deg/s")
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-29T00:00:00Z",
            "acc_x": "1.0",
            "ay": "0.5",
            "Accel Z": "-1.0",
            "Gyro X": "90",
            "gy": "0",
            "gz": "0",
        },
    )
    assert record["accel_x"] == 9.80665
    assert record["accel_y"] == pytest.approx(0.5 * 9.80665)
    assert record["accel_z"] == -9.80665
    assert record["gyro_x"] == pytest.approx(math.pi / 2)


def test_ambiguous_alias_mapping_fails(imu_schema) -> None:
    normalizer = _normalizer(imu_schema, acceleration="g", angular_velocity="deg/s")
    with pytest.raises(AmbiguousFieldMappingError):
        normalizer.normalize_record(
            1,
            {
                "timestamp": "2026-08-29T00:00:00Z",
                "acc_x": "1.0",
                "Accel X": "2.0",  # both map to accel_x
                "accel_y": "0.0",
                "accel_z": "0.0",
                "gyro_x": "0",
                "gyro_y": "0",
                "gyro_z": "0",
            },
        )
