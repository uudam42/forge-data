"""Maps a schema_name to its IntegrityChecker implementation.

This is a second, orthogonal axis from app.validation.registry's
ValidatorRegistry: that one is keyed by file extension (how to read the
file); this one is keyed by schema_name (what semantics to check). File
reading for integrity lives in app.integrity.records instead.

(v2.3) The map is built from the sensor plugin registry
(app.sensors.registry) instead of hardcoding IMU/GPS imports directly —
one sensor plugin registration now makes its integrity checker available
here automatically, so this registry can never silently disagree with
normalization or transformation about which sensors exist. The public
interface (`supports`, `get`) is unchanged from v1.0.
"""

from __future__ import annotations

from app.integrity.checks.base import IntegrityChecker
from app.sensors.registry import SensorPluginRegistry, get_default_registry


class UnsupportedIntegrityCheckerError(Exception):
    pass


class IntegrityCheckerRegistry:
    def __init__(self, sensor_registry: SensorPluginRegistry | None = None) -> None:
        registry = sensor_registry or get_default_registry()
        self._checkers: dict[str, IntegrityChecker] = {
            plugin.sensor_type: plugin.integrity_checker for plugin in registry.list_plugins()
        }

    def supports(self, schema_name: str) -> bool:
        return schema_name.lower() in self._checkers

    def get(self, schema_name: str) -> IntegrityChecker:
        checker = self._checkers.get(schema_name.lower())
        if checker is None:
            raise UnsupportedIntegrityCheckerError(
                f"No integrity checker registered for schema '{schema_name}'"
            )
        return checker
