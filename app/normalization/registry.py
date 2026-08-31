"""Maps (schema_name, schema_version, profile_name, profile_version) to a
NormalizationProfile.

Profile versioning is kept explicitly separate from schema versioning:
normalization logic can evolve (a new profile_version) without touching the
schema it targets, and lookup is always explicit — never "latest" or
implicit fallback.
"""

from __future__ import annotations

from app.normalization.profiles.base import NormalizationProfile
from app.normalization.profiles.gps import GPS_CANONICAL_V1
from app.normalization.profiles.imu import IMU_CANONICAL_V1

_BUILTIN_PROFILES = (IMU_CANONICAL_V1, GPS_CANONICAL_V1)


class NormalizationProfileNotFoundError(Exception):
    pass


class NormalizationProfileRegistry:
    def __init__(self) -> None:
        self._profiles: dict[tuple[str, str, str, str], NormalizationProfile] = {
            (p.schema_name, p.schema_version, p.profile_name, p.profile_version): p
            for p in _BUILTIN_PROFILES
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
