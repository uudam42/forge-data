"""Maps (schema_name, schema_version, profile_name, profile_version) to a
NormalizationProfile.

Profile versioning is kept explicitly separate from schema versioning:
normalization logic can evolve (a new profile_version) without touching the
schema it targets, and lookup is always explicit — never "latest" or
implicit fallback.

(v2.3) The map is built from the sensor plugin registry
(app.sensors.registry) instead of hardcoding IMU/GPS profile imports
directly. The public interface (`get`, `list_profiles`) is unchanged
from v1.0.
"""

from __future__ import annotations

from app.normalization.profiles.base import NormalizationProfile
from app.sensors.registry import SensorPluginRegistry, get_default_registry


class NormalizationProfileNotFoundError(Exception):
    pass


class NormalizationProfileRegistry:
    def __init__(self, sensor_registry: SensorPluginRegistry | None = None) -> None:
        registry = sensor_registry or get_default_registry()
        self._profiles: dict[tuple[str, str, str, str], NormalizationProfile] = {
            (p.schema_name, p.schema_version, p.profile_name, p.profile_version): p
            for p in (plugin.normalization_profile for plugin in registry.list_plugins())
        }

    def get(
        self, *, schema_name: str, schema_version: str, profile_name: str, profile_version: str
    ) -> NormalizationProfile:
        key = (schema_name, schema_version, profile_name, profile_version)
        profile = self._profiles.get(key)
        if profile is None:
            raise NormalizationProfileNotFoundError(
                f"No normalization profile '{profile_name}' v{profile_version} registered for "
                f"schema '{schema_name}' v{schema_version}"
            )
        return profile

    def list_profiles(self) -> list[tuple[str, str, str, str]]:
        return sorted(self._profiles.keys())
