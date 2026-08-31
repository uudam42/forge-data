"""Built-in gps_canonical normalization profile for the gps v1.0.0 schema.

Canonical fields (from schemas/gps_v1.json): timestamp, latitude, longitude,
altitude, speed, device_id. Canonical units: altitude in meters, speed in
m/s. Latitude/longitude are decimal-degree passthroughs — no coordinate
reference system conversion is supported in this MVP (see README).
"""

from __future__ import annotations

from app.normalization.profiles.base import NormalizationProfile
from app.normalization.transforms.units import ALTITUDE, SPEED

GPS_CANONICAL_V1 = NormalizationProfile(
    schema_name="gps",
    schema_version="1.0.0",
    profile_name="gps_canonical",
    profile_version="1.0.0",
    transform_version="1.0.0",
    field_aliases={
        "lat": "latitude",
        "lon": "longitude",
        "lng": "longitude",
        "alt": "altitude",
    },
    field_dimensions={
        "altitude": "altitude",
        "speed": "speed",
    },
    dimensions={
        "altitude": ALTITUDE,
        "speed": SPEED,
    },
    timestamp_field="timestamp",
)
