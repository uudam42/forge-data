"""Unit tests for the gps_canonical normalization profile: unit conversion
and field aliasing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.normalization.profiles.base import RecordNormalizer
from app.normalization.profiles.gps import GPS_CANONICAL_V1
from app.validation.schemas.registry import SchemaRegistry
from app.core.config import _default_schema_dir

SCHEMA_DIR = _default_schema_dir()


@pytest.fixture
def gps_schema():
    registry = SchemaRegistry(schema_dir=SCHEMA_DIR)
    return registry.get(schema_name="gps", schema_version="1.0.0")


def _normalizer(gps_schema, **source_units) -> RecordNormalizer:
    return RecordNormalizer(schema=gps_schema, profile=GPS_CANONICAL_V1, source_units=source_units)


def test_feet_to_meters_conversion_correct(gps_schema) -> None:
    normalizer = _normalizer(gps_schema, altitude="ft", speed="m/s")
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-30T18:00:00-07:00",
            "latitude": "34.0205",
            "longitude": "-118.2856",
            "altitude": "100",
            "speed": "10",
        },
    )
    assert record["altitude"] == pytest.approx(30.48)


def test_kmh_to_mps_conversion_correct(gps_schema) -> None:
    normalizer = _normalizer(gps_schema, altitude="m", speed="km/h")
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-30T18:00:00-07:00",
            "latitude": "34.0205",
            "longitude": "-118.2856",
            "altitude": "30.48",
            "speed": "36",
        },
    )
    assert record["speed"] == pytest.approx(10.0)


def test_mph_to_mps_conversion_correct(gps_schema) -> None:
    normalizer = _normalizer(gps_schema, altitude="m", speed="mph")
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-30T18:00:00-07:00",
            "latitude": "34.0205",
            "longitude": "-118.2856",
            "altitude": "0",
            "speed": "1",
        },
    )
    assert record["speed"] == pytest.approx(0.44704)


def test_full_worked_example_matches_spec(gps_schema) -> None:
    """timestamp,latitude,longitude,altitude,speed with altitude=ft, speed=km/h."""
    normalizer = _normalizer(gps_schema, altitude="ft", speed="km/h")
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-30T18:00:00-07:00",
            "latitude": "34.0205",
            "longitude": "-118.2856",
            "altitude": "100",
            "speed": "36",
        },
    )
    assert record["timestamp"] == "2026-08-31T01:00:00Z"
    assert record["latitude"] == 34.0205
    assert record["longitude"] == -118.2856
    assert record["altitude"] == pytest.approx(30.48)
    assert record["speed"] == pytest.approx(10.0)


def test_latitude_longitude_are_decimal_degree_passthrough(gps_schema) -> None:
    normalizer = _normalizer(gps_schema, altitude="m", speed="m/s")
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-29T00:00:00Z",
            "latitude": "-33.8688",
            "longitude": "151.2093",
        },
    )
    assert record["latitude"] == -33.8688
    assert record["longitude"] == 151.2093
    assert record["altitude"] is None
    assert record["speed"] is None


def test_gps_field_alias_normalization_works(gps_schema) -> None:
    normalizer = _normalizer(gps_schema, altitude="ft", speed="mph")
    record = normalizer.normalize_record(
        1,
        {
            "timestamp": "2026-08-29T00:00:00Z",
            "lat": "34.0205",
            "lon": "-118.2856",
            "alt": "0",
        },
    )
    assert record["latitude"] == 34.0205
    assert record["longitude"] == -118.2856
    assert record["altitude"] == 0.0
