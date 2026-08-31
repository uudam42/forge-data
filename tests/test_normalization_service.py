"""Unit tests for the generic normalization engine: timestamp normalization,
config-hash determinism, unit-metadata errors, and numeric/optional-field
handling — exercised independently of the API layer.
"""

from __future__ import annotations

import inspect
import io
from pathlib import Path

import pytest

from app.normalization import records as normalization_records
from app.normalization.profiles.base import (
    MissingUnitMetadataError,
    NormalizationConversionError,
    RecordNormalizer,
    UnsupportedSourceUnitError,
)
from app.normalization.profiles.gps import GPS_CANONICAL_V1
from app.normalization.profiles.imu import IMU_CANONICAL_V1
from app.normalization.transforms.timestamps import normalize_timestamp
from app.validation.schemas.registry import SchemaRegistry

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


@pytest.fixture
def schema_registry() -> SchemaRegistry:
    return SchemaRegistry(schema_dir=SCHEMA_DIR)


@pytest.fixture
def imu_schema(schema_registry: SchemaRegistry):
    return schema_registry.get(schema_name="imu", schema_version="1.0.0")


# ---------------------------------------------------------------------------
# Timestamp normalization
# ---------------------------------------------------------------------------


def test_timestamp_converted_to_utc_z() -> None:
    assert normalize_timestamp("2026-08-30T18:00:00-07:00") == "2026-08-31T01:00:00Z"


def test_timestamp_already_utc_z_is_unchanged() -> None:
    assert normalize_timestamp("2026-08-30T18:00:00Z") == "2026-08-30T18:00:00Z"


def test_timestamp_preserves_subsecond_precision() -> None:
    assert normalize_timestamp("2026-08-30T18:00:00.123456-07:00") == "2026-08-31T01:00:00.123456Z"


def test_timestamp_without_subseconds_has_no_fractional_part() -> None:
    result = normalize_timestamp("2026-08-30T18:00:00+00:00")
    assert "." not in result
    assert result == "2026-08-30T18:00:00Z"


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError):
        normalize_timestamp("2026-08-30T18:00:00")


# ---------------------------------------------------------------------------
# Config hash determinism
# ---------------------------------------------------------------------------


def test_config_hash_is_order_independent() -> None:
    h1 = IMU_CANONICAL_V1.config_hash({"acceleration": "g", "angular_velocity": "deg/s"})
    h2 = IMU_CANONICAL_V1.config_hash({"angular_velocity": "deg/s", "acceleration": "g"})
    assert h1 == h2


def test_config_hash_changes_with_different_source_units() -> None:
    h1 = IMU_CANONICAL_V1.config_hash({"acceleration": "g", "angular_velocity": "deg/s"})
    h2 = IMU_CANONICAL_V1.config_hash({"acceleration": "m/s^2", "angular_velocity": "deg/s"})
    assert h1 != h2


def test_config_hash_differs_between_profiles() -> None:
    units = {"acceleration": "g", "angular_velocity": "deg/s"}
    imu_hash = IMU_CANONICAL_V1.config_hash(units)
    gps_hash = GPS_CANONICAL_V1.config_hash({"altitude": "ft", "speed": "mph"})
    assert imu_hash != gps_hash


def test_config_hash_is_a_hex_sha256() -> None:
    h = IMU_CANONICAL_V1.config_hash({"acceleration": "g", "angular_velocity": "deg/s"})
    assert len(h) == 64
    int(h, 16)  # raises ValueError if not valid hex


# ---------------------------------------------------------------------------
# Unit metadata errors (fail fast, before any record is read)
# ---------------------------------------------------------------------------


def test_missing_required_source_unit_fails(imu_schema) -> None:
    with pytest.raises(MissingUnitMetadataError):
        RecordNormalizer(schema=imu_schema, profile=IMU_CANONICAL_V1, source_units={"acceleration": "g"})


def test_unsupported_source_unit_fails(imu_schema) -> None:
    with pytest.raises(UnsupportedSourceUnitError):
        RecordNormalizer(
            schema=imu_schema,
            profile=IMU_CANONICAL_V1,
            source_units={"acceleration": "lbf", "angular_velocity": "deg/s"},
        )


def test_canonical_units_pass_through_unchanged(imu_schema) -> None:
    normalizer = RecordNormalizer(
        schema=imu_schema,
        profile=IMU_CANONICAL_V1,
        source_units={"acceleration": "m/s^2", "angular_velocity": "rad/s"},
    )
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-29T00:00:00Z",
            "accel_x": "9.80665",
            "accel_y": "0.0",
            "accel_z": "-9.80665",
            "gyro_x": "3.141592653589793",
            "gyro_y": "0.0",
            "gyro_z": "0.0",
        },
    )
    assert record["accel_x"] == 9.80665
    assert record["gyro_x"] == 3.141592653589793


# ---------------------------------------------------------------------------
# Optional fields / numeric precision
# ---------------------------------------------------------------------------


def test_optional_missing_field_is_not_imputed(imu_schema) -> None:
    normalizer = RecordNormalizer(
        schema=imu_schema,
        profile=IMU_CANONICAL_V1,
        source_units={"acceleration": "g", "angular_velocity": "deg/s"},
    )
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-29T00:00:00Z",
            "accel_x": "1.0",
            "accel_y": "0.0",
            "accel_z": "0.0",
            # gyro_x/y/z and device_id all absent
        },
    )
    assert record["gyro_x"] is None
    assert record["gyro_y"] is None
    assert record["gyro_z"] is None
    assert record["device_id"] is None


def test_numeric_values_are_not_unnecessarily_rounded(imu_schema) -> None:
    normalizer = RecordNormalizer(
        schema=imu_schema,
        profile=IMU_CANONICAL_V1,
        source_units={"acceleration": "g", "angular_velocity": "deg/s"},
    )
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-29T00:00:00Z",
            "accel_x": "1.0",
            "accel_y": "0.0",
            "accel_z": "0.0",
            "gyro_x": "180",
            "gyro_y": "0",
            "gyro_z": "0",
        },
    )
    # 180 deg/s -> pi rad/s at full float precision, not truncated/rounded.
    import math

    assert record["gyro_x"] == math.pi


def test_padded_numeric_string_is_normalized_deterministically(imu_schema) -> None:
    normalizer = RecordNormalizer(
        schema=imu_schema,
        profile=IMU_CANONICAL_V1,
        source_units={"acceleration": "m/s^2", "angular_velocity": "rad/s"},
    )
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-29T00:00:00Z",
            "accel_x": "001.000",
            "accel_y": "0.0",
            "accel_z": "0.0",
            "gyro_x": "0",
            "gyro_y": "0",
            "gyro_z": "0",
        },
    )
    assert record["accel_x"] == 1.0


# ---------------------------------------------------------------------------
# Streaming source reading
# ---------------------------------------------------------------------------


def test_csv_source_reader_is_a_generator() -> None:
    stream = io.BytesIO(b"timestamp,accel_x\n2026-08-29T00:00:00Z,0.1\n")
    result = normalization_records.iter_records(stream, ".csv")
    assert inspect.isgenerator(result)


def test_jsonl_source_reader_is_a_generator() -> None:
    stream = io.BytesIO(b'{"timestamp": "2026-08-29T00:00:00Z"}\n')
    result = normalization_records.iter_records(stream, ".jsonl")
    assert inspect.isgenerator(result)


def test_jsonl_writer_streams_without_materializing_full_list() -> None:
    """The JSONL writer counts and writes records as it consumes them from
    an iterator — never converting the whole stream to a list first (unlike
    the JSON array writer, which does)."""

    def records():
        for i in range(3):
            yield {"timestamp": f"2026-08-29T00:00:0{i}Z", "accel_x": float(i)}

    destination = io.BytesIO()
    count = normalization_records.write_records(
        destination, ".jsonl", records(), fieldnames=["timestamp", "accel_x"]
    )
    assert count == 3
    assert len(destination.getvalue().splitlines()) == 3


def test_non_finite_numeric_value_fails_normalization(imu_schema) -> None:
    normalizer = RecordNormalizer(
        schema=imu_schema,
        profile=IMU_CANONICAL_V1,
        source_units={"acceleration": "g", "angular_velocity": "deg/s"},
    )
    with pytest.raises(NormalizationConversionError):
        normalizer.normalize_record(
            1,
            {
                "timestamp": "2026-08-29T00:00:00Z",
                "accel_x": "nan",
                "accel_y": "0.0",
                "accel_z": "0.0",
                "gyro_x": "0",
                "gyro_y": "0",
                "gyro_z": "0",
            },
        )
