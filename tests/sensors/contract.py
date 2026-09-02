"""Reusable sensor-plugin contract assertions (v2.3, Design Requirement
18). Every built-in plugin (IMU, GPS, Force/Torque) is run through the
exact same checks — see test_contract_all_builtins.py. Not a test module
itself (no test_ prefix), imported by the ones that are.
"""

from __future__ import annotations

import math

from app.core.config import _default_schema_dir
from app.sensors.base import SensorPlugin
from app.validation.schemas.registry import SchemaRegistry

SCHEMA_DIR = _default_schema_dir()


def assert_plugin_contract(plugin: SensorPlugin, registry_sensor_types: list[str]) -> None:
    # 1. unique sensor key (checked against the full registry listing)
    assert registry_sensor_types.count(plugin.sensor_type) == 1

    # 2. valid version metadata
    assert plugin.plugin_version, "plugin_version must be non-empty"
    assert plugin.schema_version, "schema_version must be non-empty"
    assert plugin.normalization_profile.profile_version, "profile_version must be non-empty"

    # 3. schema definition is loadable
    schema_registry = SchemaRegistry(SCHEMA_DIR)
    schema = schema_registry.get(schema_name=plugin.sensor_type, schema_version=plugin.schema_version)

    # 4. required canonical timestamp exists
    assert plugin.timestamp_field in schema.fields
    assert schema.fields[plugin.timestamp_field].required

    # 5. canonical numeric fields declared consistently
    assert len(plugin.numeric_fields) > 0
    for numeric_field in plugin.numeric_fields:
        assert numeric_field in schema.fields, f"{numeric_field} declared as numeric but not in schema"

    # 6. integrity checker resolves (already a live instance on the plugin)
    assert plugin.integrity_checker is not None

    # 7. normalization profile resolves and targets this plugin's schema
    assert plugin.normalization_profile.schema_name == plugin.sensor_type
    assert plugin.normalization_profile.schema_version == plugin.schema_version

    # 8. canonical output fields match declared metadata: every numeric
    # field needing unit conversion must be one of the schema's own
    # fields, and every dimension referenced must be declared.
    for canonical_field, dimension_name in plugin.normalization_profile.field_dimensions.items():
        assert canonical_field in schema.fields
        assert dimension_name in plugin.normalization_profile.dimensions

    # 13. feature extractors resolve (when declared) and target this sensor
    if plugin.feature_extractor is not None:
        assert plugin.feature_extractor.stream_name == plugin.sensor_type


def assert_normalization_deterministic(normalize_once) -> None:
    """`normalize_once` is a zero-arg callable returning a normalized
    record dict; calling it twice must produce byte-identical output."""
    first = normalize_once()
    second = normalize_once()
    assert first == second


def assert_all_finite(record: dict, numeric_fields: tuple[str, ...]) -> None:
    # 10. normalized numeric values are finite
    for field_name in numeric_fields:
        value = record.get(field_name)
        if value is not None:
            assert math.isfinite(value), f"{field_name}={value!r} is not finite"


def assert_canonical_timestamp(value: str) -> None:
    # 11. timestamps canonical (UTC, "Z" suffix, ISO-8601)
    assert isinstance(value, str)
    assert value.endswith("Z")
    from datetime import datetime

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
