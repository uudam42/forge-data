"""GPS sensor plugin descriptor.

Pure composition over pre-existing, unchanged objects
(GpsIntegrityChecker, GPS_CANONICAL_V1, GpsFeatureExtractor) — v2.3's
migration of GPS into the plugin architecture is metadata-only. No GPS
behavior, format, threshold, or determinism changed.
"""

from __future__ import annotations

from app.integrity.checks.gps import GpsIntegrityChecker
from app.normalization.profiles.gps import GPS_CANONICAL_V1
from app.sensors.base import SensorPlugin
from app.transformation.features.gps import FIELDS as _GPS_FIELDS
from app.transformation.features.gps import GpsFeatureExtractor

GPS_PLUGIN = SensorPlugin(
    sensor_type="gps",
    plugin_version="1.0.0",
    display_name="GPS (latitude/longitude/altitude/speed)",
    schema_version="1.0.0",
    integrity_checker=GpsIntegrityChecker(),
    normalization_profile=GPS_CANONICAL_V1,
    feature_extractor=GpsFeatureExtractor(),
    timestamp_field="timestamp",
    numeric_fields=_GPS_FIELDS,
    required_fields=("timestamp", "latitude", "longitude"),
    canonical_units={"altitude": "m", "speed": "m/s"},
)
