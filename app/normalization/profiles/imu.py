"""Built-in imu_canonical normalization profile for the imu v1.0.0 schema.

Canonical fields (from schemas/imu_v1.json): timestamp, accel_x, accel_y,
accel_z, gyro_x, gyro_y, gyro_z, device_id. Canonical units: acceleration in
m/s^2, angular velocity in rad/s.
"""

from __future__ import annotations

from app.normalization.profiles.base import NormalizationProfile
from app.normalization.transforms.units import ACCELERATION, ANGULAR_VELOCITY

IMU_CANONICAL_V1 = NormalizationProfile(
    schema_name="imu",
    schema_version="1.0.0",
    profile_name="imu_canonical",
    profile_version="1.0.0",
    transform_version="1.0.0",
    field_aliases={
        "Accel X": "accel_x",
        "Accel Y": "accel_y",
        "Accel Z": "accel_z",
        "acc_x": "accel_x",
        "acc_y": "accel_y",
        "acc_z": "accel_z",
        "ax": "accel_x",
        "ay": "accel_y",
        "az": "accel_z",
        "Gyro X": "gyro_x",
        "Gyro Y": "gyro_y",
        "Gyro Z": "gyro_z",
        "gx": "gyro_x",
        "gy": "gyro_y",
        "gz": "gyro_z",
    },
    field_dimensions={
        "accel_x": "acceleration",
        "accel_y": "acceleration",
        "accel_z": "acceleration",
        "gyro_x": "angular_velocity",
        "gyro_y": "angular_velocity",
        "gyro_z": "angular_velocity",
    },
    dimensions={
        "acceleration": ACCELERATION,
        "angular_velocity": ANGULAR_VELOCITY,
    },
    timestamp_field="timestamp",
)
