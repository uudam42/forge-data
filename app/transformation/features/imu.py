"""IMU feature extraction: raw sequences, per-axis statistics, and
deterministic derived magnitudes.

Deliberately MVP-scoped: no gravity subtraction, no orientation estimation,
no FFT/spectral features. accel_magnitude/gyro_magnitude are the only
derived features, computed per-row as a plain Euclidean norm of the three
axes for that row (rows missing any one of the three axes contribute no
magnitude sample for that row, rather than a value computed from partial
data).
"""

from __future__ import annotations

import math

from app.transformation.features.base import FeatureExtractor, StreamFeatureResult, WindowRow
from app.transformation.features.common import UnknownFeatureError, require_finite
from app.transformation.features.statistics import compute_statistic, validate_statistic_names
from app.transformation.models import StreamFeatureConfig

AXES = ("accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z")
DERIVED_FEATURES = ("accel_magnitude", "gyro_magnitude")

_ACCEL_AXES = ("accel_x", "accel_y", "accel_z")
_GYRO_AXES = ("gyro_x", "gyro_y", "gyro_z")


class ImuFeatureExtractor(FeatureExtractor):
    stream_name = "imu"

    def validate_config(self, config: StreamFeatureConfig) -> None:
        validate_statistic_names(config.statistics)
        for name in config.derived:
            if name not in DERIVED_FEATURES:
                raise UnknownFeatureError(f"Unknown IMU derived feature '{name}'")

    def extract(self, rows: list[WindowRow], config: StreamFeatureConfig) -> StreamFeatureResult:
        present_rows = [r for r in rows if r.payload is not None]
        present_count = len(present_rows)
        missing_count = len(rows) - present_count

        field_names = list(AXES) + list(config.derived)
        sequences: dict[str, list[float]] = {name: [] for name in field_names}

        for row in present_rows:
            axis_values: dict[str, float | None] = {}
            for axis in AXES:
                value = row.payload.get(axis)
                if value is not None:
                    value = require_finite(float(value), field=f"imu.{axis}", row_index=row.row_index)
                    sequences[axis].append(value)
                axis_values[axis] = value

            if "accel_magnitude" in config.derived and all(axis_values[a] is not None for a in _ACCEL_AXES):
                magnitude = math.sqrt(sum(axis_values[a] ** 2 for a in _ACCEL_AXES))
                sequences["accel_magnitude"].append(
                    require_finite(magnitude, field="imu.accel_magnitude", row_index=row.row_index)
                )
            if "gyro_magnitude" in config.derived and all(axis_values[a] is not None for a in _GYRO_AXES):
                magnitude = math.sqrt(sum(axis_values[a] ** 2 for a in _GYRO_AXES))
                sequences["gyro_magnitude"].append(
                    require_finite(magnitude, field="imu.gyro_magnitude", row_index=row.row_index)
                )

        features: dict = {}
        if config.include_raw:
            features["raw"] = {name: sequences[name] for name in field_names if sequences[name]}

        if config.statistics:
            stats = {}
            for name in field_names:
                values = sequences[name]
                for stat_name in config.statistics:
                    stats[f"{name}_{stat_name}"] = compute_statistic(stat_name, values)
            features["statistics"] = stats

        return StreamFeatureResult(
            present=present_count > 0,
            present_count=present_count,
            missing_count=missing_count,
            features=features or None,
        )
