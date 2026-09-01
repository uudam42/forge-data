"""IMU sensor plugin descriptor.

Pure composition over pre-existing, unchanged objects
(ImuIntegrityChecker, IMU_CANONICAL_V1, ImuFeatureExtractor) — v2.3's
migration of IMU into the plugin architecture is metadata-only. No IMU
behavior, format, threshold, or determinism changed; see
docs/DETAILED_GUIDE.md#sensor-plugin-architecture-v23, "IMU/GPS
migration."
"""

from __future__ import annotations

from app.integrity.checks.imu import ImuIntegrityChecker
from app.normalization.profiles.imu import IMU_CANONICAL_V1
from app.sensors.base import SensorPlugin
from app.transformation.features.imu import AXES as _IMU_AXES
from app.transformation.features.imu import ImuFeatureExtractor

IMU_PLUGIN = SensorPlugin(
    sensor_type="imu",
    plugin_version="1.0.0",
    display_name="6-axis IMU (accelerometer + gyroscope)",
    schema_version="1.0.0",
    integrity_checker=ImuIntegrityChecker(),
    normalization_profile=IMU_CANONICAL_V1,
    feature_extractor=ImuFeatureExtractor(),
    timestamp_field="timestamp",
    numeric_fields=_IMU_AXES,
    required_fields=("timestamp", "accel_x", "accel_y", "accel_z"),
    canonical_units={"acceleration": "m/s^2", "angular_velocity": "rad/s"},
)
