"""Force/Torque sensor plugin descriptor -- the proof sensor for v2.3.

This file (plus schema.py's schemas/force_torque_v1.json,
integrity.py, normalization.py, and features.py) is the ENTIRE
Force/Torque-specific contribution to this project. No synchronization,
cleaning, QC, packaging, or catalog file was touched to add this sensor
— see docs/DETAILED_GUIDE.md#sensor-plugin-architecture-v23, "Extension
cost."
"""

from __future__ import annotations

from app.sensors.base import SensorPlugin
from app.sensors.force_torque.features import AXES as _FT_AXES
from app.sensors.force_torque.features import ForceTorqueFeatureExtractor
from app.sensors.force_torque.integrity import ForceTorqueIntegrityChecker
from app.sensors.force_torque.normalization import FORCE_TORQUE_CANONICAL_V1

FORCE_TORQUE_PLUGIN = SensorPlugin(
    sensor_type="force_torque",
    plugin_version="1.0.0",
    display_name="6-axis Force/Torque sensor",
    schema_version="1.0.0",
    integrity_checker=ForceTorqueIntegrityChecker(),
    normalization_profile=FORCE_TORQUE_CANONICAL_V1,
    feature_extractor=ForceTorqueFeatureExtractor(),
    timestamp_field="timestamp",
    numeric_fields=_FT_AXES,
    required_fields=("timestamp", "force_x", "force_y", "force_z", "torque_x", "torque_y", "torque_z"),
    canonical_units={"force": "N", "torque": "N·m"},
)
