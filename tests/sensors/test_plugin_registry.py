"""Sensor plugin infrastructure tests (Design Requirements 3, 18)."""

from __future__ import annotations

import pytest

from app.sensors.base import DuplicateSensorPluginError, InvalidSensorPluginError, SensorPlugin, SensorPluginNotFoundError
from app.sensors.force_torque.plugin import FORCE_TORQUE_PLUGIN
from app.sensors.gps import GPS_PLUGIN
from app.sensors.imu import IMU_PLUGIN
from app.sensors.registry import SensorPluginRegistry, get_default_registry, register_builtin_plugins
from tests.sensors.contract import assert_plugin_contract

_BUILTINS = (IMU_PLUGIN, GPS_PLUGIN, FORCE_TORQUE_PLUGIN)


def test_register_and_get() -> None:
    registry = SensorPluginRegistry()
    registry.register(IMU_PLUGIN)
    assert registry.get("imu") is IMU_PLUGIN
    assert registry.is_registered("imu")
    assert not registry.is_registered("gps")


def test_duplicate_plugin_key_rejected() -> None:
    registry = SensorPluginRegistry()
    registry.register(IMU_PLUGIN)
    with pytest.raises(DuplicateSensorPluginError):
        registry.register(IMU_PLUGIN)


def test_unknown_plugin_raises_structured_error() -> None:
    registry = SensorPluginRegistry()
    registry.register(IMU_PLUGIN)
    with pytest.raises(SensorPluginNotFoundError) as exc_info:
        registry.get("lidar")
    assert "lidar" in str(exc_info.value)
    assert "imu" in str(exc_info.value)  # available types listed


def test_deterministic_plugin_listing() -> None:
    registry = SensorPluginRegistry()
    register_builtin_plugins(registry)
    listing_1 = [p.sensor_type for p in registry.list_plugins()]
    listing_2 = [p.sensor_type for p in registry.list_plugins()]
    assert listing_1 == listing_2 == sorted(listing_1)


def test_default_registry_has_all_three_builtins() -> None:
    registry = get_default_registry()
    sensor_types = {p.sensor_type for p in registry.list_plugins()}
    assert sensor_types == {"imu", "gps", "force_torque"}


def test_plugin_rejects_mismatched_schema_name() -> None:
    from app.integrity.checks.imu import ImuIntegrityChecker
    from app.normalization.profiles.gps import GPS_CANONICAL_V1

    with pytest.raises(InvalidSensorPluginError):
        SensorPlugin(
            sensor_type="imu",
            plugin_version="1.0.0",
            display_name="broken",
            schema_version="1.0.0",
            integrity_checker=ImuIntegrityChecker(),
            normalization_profile=GPS_CANONICAL_V1,  # schema_name="gps" != "imu"
        )


def test_plugin_rejects_mismatched_feature_extractor_stream_name() -> None:
    from app.integrity.checks.imu import ImuIntegrityChecker
    from app.normalization.profiles.imu import IMU_CANONICAL_V1
    from app.transformation.features.gps import GpsFeatureExtractor

    with pytest.raises(InvalidSensorPluginError):
        SensorPlugin(
            sensor_type="imu",
            plugin_version="1.0.0",
            display_name="broken",
            schema_version="1.0.0",
            integrity_checker=ImuIntegrityChecker(),
            normalization_profile=IMU_CANONICAL_V1,
            feature_extractor=GpsFeatureExtractor(),  # stream_name="gps" != "imu"
        )


@pytest.mark.parametrize("plugin", _BUILTINS, ids=lambda p: p.sensor_type)
def test_builtin_plugin_metadata_is_valid(plugin) -> None:
    assert plugin.sensor_type
    assert plugin.plugin_version
    assert plugin.display_name
    assert plugin.canonical_units


@pytest.mark.parametrize("plugin", _BUILTINS, ids=lambda p: p.sensor_type)
def test_builtin_plugin_passes_contract_suite(plugin) -> None:
    registry_sensor_types = [p.sensor_type for p in get_default_registry().list_plugins()]
    assert_plugin_contract(plugin, registry_sensor_types)
