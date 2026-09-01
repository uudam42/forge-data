"""The single authoritative registry for built-in sensor plugins.

Registering a plugin here is what makes it available, coherently, to
integrity checking, normalization, and transformation feature extraction
— see app/integrity/registry.py, app/normalization/registry.py, and
app/transformation/profiles/multimodal_window.py, each of which now
builds its own internal map FROM this registry instead of hardcoding an
independent one. This is the mechanism that prevents those three
subsystems from silently disagreeing about which sensors exist.

Discovery is explicit and static for v2.3 (see Design Requirement 22) —
no filesystem scanning, no importlib entry points, no dynamic module
execution. `register_builtin_plugins()` is a plain function that calls
`.register()` once per built-in plugin; a future third-party plugin
mechanism can build on this same registry without changing it.
"""

from __future__ import annotations

from app.sensors.base import DuplicateSensorPluginError, SensorPlugin, SensorPluginNotFoundError


class SensorPluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, SensorPlugin] = {}

    def register(self, plugin: SensorPlugin) -> None:
        if plugin.sensor_type in self._plugins:
            raise DuplicateSensorPluginError(f"A plugin is already registered for sensor_type={plugin.sensor_type!r}")
        self._plugins[plugin.sensor_type] = plugin

    def is_registered(self, sensor_type: str) -> bool:
        return sensor_type in self._plugins

    def get(self, sensor_type: str) -> SensorPlugin:
        plugin = self._plugins.get(sensor_type)
        if plugin is None:
            raise SensorPluginNotFoundError(
                f"Unknown sensor type {sensor_type!r}. Available: {sorted(self._plugins)}"
            )
        return plugin

    def list_plugins(self) -> list[SensorPlugin]:
        """Deterministic ordering (sorted by sensor_type) -- callers that
        enumerate every built-in (the /api/v1/sensors endpoint, the other
        registries' map-building) never depend on registration order."""
        return [self._plugins[key] for key in sorted(self._plugins)]


def register_builtin_plugins(registry: SensorPluginRegistry) -> None:
    from app.sensors.force_torque.plugin import FORCE_TORQUE_PLUGIN
    from app.sensors.gps import GPS_PLUGIN
    from app.sensors.imu import IMU_PLUGIN

    for plugin in (IMU_PLUGIN, GPS_PLUGIN, FORCE_TORQUE_PLUGIN):
        registry.register(plugin)


_default_registry: SensorPluginRegistry | None = None


def get_default_registry() -> SensorPluginRegistry:
    """The one process-wide registry of built-in plugins, built once on
    first use. A test that needs an isolated registry should construct
    its own SensorPluginRegistry() and call register_builtin_plugins()
    (or register a subset) directly rather than using this singleton."""
    global _default_registry
    if _default_registry is None:
        _default_registry = SensorPluginRegistry()
        register_builtin_plugins(_default_registry)
    return _default_registry
