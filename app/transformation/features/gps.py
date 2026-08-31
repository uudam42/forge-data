"""GPS feature extraction: raw sequences, per-field statistics, and an
optional self-contained Haversine displacement.

"Start position"/"end position" are deliberately NOT a separate derived
feature — requesting statistics=["first","last"] already yields
latitude_first/latitude_last/longitude_first/longitude_last via the same
generic per-field statistics mechanism used for every other field.
"""

from __future__ import annotations

import math

from app.transformation.features.base import FeatureExtractor, StreamFeatureResult, WindowRow
from app.transformation.features.common import UnknownFeatureError, require_finite
from app.transformation.features.statistics import compute_statistic, validate_statistic_names
from app.transformation.models import StreamFeatureConfig

FIELDS = ("latitude", "longitude", "altitude", "speed")
DERIVED_FEATURES = ("displacement_m",)

_EARTH_RADIUS_M = 6_371_000.0  # mean Earth radius, meters


def _haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Self-contained great-circle distance — no GIS dependency. Uses a
    fixed mean Earth radius; accurate to roughly 0.5% for terrestrial
    distances, sufficient for an MVP displacement estimate, not precision
    geodesy."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


class GpsFeatureExtractor(FeatureExtractor):
    stream_name = "gps"

    def validate_config(self, config: StreamFeatureConfig) -> None:
        validate_statistic_names(config.statistics)
        for name in config.derived:
            if name not in DERIVED_FEATURES:
                raise UnknownFeatureError(f"Unknown GPS derived feature '{name}'")

    def extract(self, rows: list[WindowRow], config: StreamFeatureConfig) -> StreamFeatureResult:
        present_rows = [r for r in rows if r.payload is not None]
        present_count = len(present_rows)
        missing_count = len(rows) - present_count

        sequences: dict[str, list[float]] = {field: [] for field in FIELDS}
        for row in present_rows:
            for field in FIELDS:
                value = row.payload.get(field)
                if value is not None:
                    sequences[field].append(
                        require_finite(float(value), field=f"gps.{field}", row_index=row.row_index)
                    )

        features: dict = {}
        if config.include_raw:
            features["raw"] = {field: sequences[field] for field in FIELDS if sequences[field]}

        if config.statistics:
            stats = {}
            for field in FIELDS:
                values = sequences[field]
                for stat_name in config.statistics:
                    stats[f"{field}_{stat_name}"] = compute_statistic(stat_name, values)
            features["statistics"] = stats

        if "displacement_m" in config.derived:
            lat_values, lon_values = sequences["latitude"], sequences["longitude"]
            displacement = None
            if len(lat_values) >= 2 and len(lon_values) >= 2:
                displacement = require_finite(
                    _haversine_distance_m(lat_values[0], lon_values[0], lat_values[-1], lon_values[-1]),
                    field="gps.displacement_m",
                    row_index=present_rows[-1].row_index,
                )
            features["derived"] = {"displacement_m": displacement}

        return StreamFeatureResult(
            present=present_count > 0,
            present_count=present_count,
            missing_count=missing_count,
            features=features or None,
        )
