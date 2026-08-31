"""Unit tests for the GPS feature extractor
(app.transformation.features.gps)."""

from __future__ import annotations

import pytest

from app.transformation.features.base import WindowRow
from app.transformation.features.common import UnknownFeatureError
from app.transformation.features.gps import GpsFeatureExtractor
from app.transformation.models import StreamFeatureConfig

EXTRACTOR = GpsFeatureExtractor()


def _row(row_index: int, epoch_us: int, **fields) -> WindowRow:
    return WindowRow(row_index=row_index, epoch_us=epoch_us, payload=fields or None)


def test_validate_config_accepts_known_statistics_and_derived() -> None:
    EXTRACTOR.validate_config(StreamFeatureConfig(statistics=["mean"], derived=["displacement_m"]))


def test_validate_config_rejects_unknown_statistic() -> None:
    with pytest.raises(UnknownFeatureError):
        EXTRACTOR.validate_config(StreamFeatureConfig(statistics=["bogus"]))


def test_validate_config_rejects_unknown_derived_feature() -> None:
    with pytest.raises(UnknownFeatureError):
        EXTRACTOR.validate_config(StreamFeatureConfig(derived=["heading"]))


def test_speed_mean_correctness() -> None:
    rows = [
        _row(0, 0, latitude=1.0, longitude=1.0, altitude=10.0, speed=8.0),
        _row(1, 1000, latitude=1.0, longitude=1.0, altitude=10.0, speed=10.0),
        _row(2, 2000, latitude=1.0, longitude=1.0, altitude=10.0, speed=12.0),
    ]
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(statistics=["mean"]))
    assert result.features["statistics"]["speed_mean"] == 10.0


def test_start_end_position_via_first_last_statistics() -> None:
    rows = [
        _row(0, 0, latitude=34.0, longitude=-118.0, altitude=10.0, speed=8.0),
        _row(1, 1000, latitude=34.1, longitude=-118.1, altitude=10.0, speed=9.0),
    ]
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(statistics=["first", "last"]))
    stats = result.features["statistics"]
    assert stats["latitude_first"] == 34.0
    assert stats["latitude_last"] == 34.1
    assert stats["longitude_first"] == -118.0
    assert stats["longitude_last"] == -118.1


def test_missing_gps_row_within_window_excluded_from_statistics() -> None:
    rows = [
        _row(0, 0, latitude=1.0, longitude=1.0, altitude=10.0, speed=8.0),
        _row(1, 1000),  # missing GPS entirely for this row
        _row(2, 2000, latitude=1.0, longitude=1.0, altitude=10.0, speed=12.0),
    ]
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(statistics=["mean"]))
    assert result.present_count == 2
    assert result.missing_count == 1
    assert result.features["statistics"]["speed_mean"] == 10.0  # (8+12)/2, missing row ignored


def test_window_with_no_gps_data_retains_feature_structure_with_null_statistics() -> None:
    rows = [_row(0, 0), _row(1, 1000)]
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(statistics=["mean"]))
    assert result.present is False
    assert result.features is not None  # structure retained, not dropped
    assert result.features["statistics"]["speed_mean"] is None


def test_raw_sequences_included_when_requested() -> None:
    rows = [
        _row(0, 0, latitude=1.0, longitude=2.0, altitude=3.0, speed=4.0),
        _row(1, 1000, latitude=1.1, longitude=2.1, altitude=3.1, speed=4.1),
    ]
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(include_raw=True))
    assert result.features["raw"]["speed"] == [4.0, 4.1]


def test_displacement_haversine_known_distance() -> None:
    # Roughly 1 degree of longitude at the equator ~ 111.19 km
    rows = [
        _row(0, 0, latitude=0.0, longitude=0.0, altitude=0.0, speed=0.0),
        _row(1, 1000, latitude=0.0, longitude=1.0, altitude=0.0, speed=0.0),
    ]
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(derived=["displacement_m"]))
    displacement = result.features["derived"]["displacement_m"]
    assert displacement == pytest.approx(111_195, rel=0.01)


def test_displacement_none_when_fewer_than_two_positions() -> None:
    rows = [_row(0, 0, latitude=0.0, longitude=0.0, altitude=0.0, speed=0.0)]
    result = EXTRACTOR.extract(rows, StreamFeatureConfig(derived=["displacement_m"]))
    assert result.features["derived"]["displacement_m"] is None


def test_no_features_requested_yields_none() -> None:
    rows = [_row(0, 0, latitude=1.0, longitude=1.0, altitude=1.0, speed=1.0)]
    result = EXTRACTOR.extract(rows, StreamFeatureConfig())
    assert result.features is None
