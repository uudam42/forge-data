"""Built-in force_torque_canonical normalization profile for the
force_torque v1.0.0 schema.

Purely declarative — reuses the same generic RecordNormalizer engine
every other profile uses (see app.normalization.profiles.base); no new
normalization code was needed to support a new sensor type, only two new
UnitDimension constants (FORCE, TORQUE) in the already-generic
app.normalization.transforms.units module.

Canonical fields: timestamp, force_x/y/z (N), torque_x/y/z (N·m),
device_id. Aliases are explicit only (fx/fy/fz/tx/ty/tz) -- no fuzzy
matching, matching this project's existing alias policy.
"""

from __future__ import annotations

from app.normalization.profiles.base import NormalizationProfile
from app.normalization.transforms.units import FORCE, TORQUE

FORCE_TORQUE_CANONICAL_V1 = NormalizationProfile(
    schema_name="force_torque",
    schema_version="1.0.0",
    profile_name="force_torque_canonical",
    profile_version="1.0.0",
    transform_version="1.0.0",
    field_aliases={
        "fx": "force_x",
        "fy": "force_y",
        "fz": "force_z",
        "tx": "torque_x",
        "ty": "torque_y",
        "tz": "torque_z",
    },
    field_dimensions={
        "force_x": "force",
        "force_y": "force",
        "force_z": "force",
        "torque_x": "torque",
        "torque_y": "torque",
        "torque_z": "torque",
    },
    dimensions={
        "force": FORCE,
        "torque": TORQUE,
    },
    timestamp_field="timestamp",
)
