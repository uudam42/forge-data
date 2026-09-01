"""Force/Torque feature extraction: raw sequences, per-axis statistics,
and deterministic derived magnitudes.

Mirrors ImuFeatureExtractor's structure exactly (see
app.transformation.features.imu). Deliberately MVP-scoped, matching the
project's existing feature-extraction philosophy: no learned models, no
contact/grasp inference, no fabricated labels — force_magnitude and
torque_magnitude are the only derived features, each a plain Euclidean
norm of that row's three axes (a row missing any one axis contributes no
magnitude sample for that row, never a value computed from partial
data).
"""

from __future__ import annotations

import math

from app.transformation.features.base import FeatureExtractor, StreamFeatureResult, WindowRow
from app.transformation.features.common import UnknownFeatureError, require_finite
from app.transformation.features.statistics import compute_statistic, validate_statistic_names
from app.transformation.models import StreamFeatureConfig

AXES = ("force_x", "force_y", "force_z", "torque_x", "torque_y", "torque_z")
DERIVED_FEATURES = ("force_magnitude", "torque_magnitude")

_FORCE_AXES = ("force_x", "force_y", "force_z")
_TORQUE_AXES = ("torque_x", "torque_y", "torque_z")


class ForceTorqueFeatureExtractor(FeatureExtractor):
    stream_name = "force_torque"

    def validate_config(self, config: StreamFeatureConfig) -> None:
        validate_statistic_names(config.statistics)
        for name in config.derived:
            if name not in DERIVED_FEATURES:
                raise UnknownFeatureError(f"Unknown force_torque derived feature '{name}'")

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
                    value = require_finite(float(value), field=f"force_torque.{axis}", row_index=row.row_index)
                    sequences[axis].append(value)
                axis_values[axis] = value

            if "force_magnitude" in config.derived and all(axis_values[a] is not None for a in _FORCE_AXES):
                magnitude = math.sqrt(sum(axis_values[a] ** 2 for a in _FORCE_AXES))
                sequences["force_magnitude"].append(
                    require_finite(magnitude, field="force_torque.force_magnitude", row_index=row.row_index)
                )
            if "torque_magnitude" in config.derived and all(axis_values[a] is not None for a in _TORQUE_AXES):
                magnitude = math.sqrt(sum(axis_values[a] ** 2 for a in _TORQUE_AXES))
                sequences["torque_magnitude"].append(
                    require_finite(magnitude, field="force_torque.torque_magnitude", row_index=row.row_index)
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
