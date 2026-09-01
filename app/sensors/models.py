"""Response models for the sensor plugin discovery API. Metadata only —
never exposes an implementation object (a checker/profile/extractor
instance) directly."""

from __future__ import annotations

from pydantic import BaseModel


class SensorPluginSummary(BaseModel):
    sensor_type: str
    plugin_version: str
    display_name: str
    schema_name: str
    schema_version: str
    normalization_profile: str
    normalization_profile_version: str
    timestamp_field: str
    numeric_fields: list[str]
    required_fields: list[str]
    canonical_units: dict[str, str]
    has_feature_extractor: bool
