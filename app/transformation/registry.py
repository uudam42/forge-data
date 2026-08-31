"""Maps (profile_name, profile_version) to a TransformationProfile.

Lookup is always explicit — never an implicit "latest" — mirroring every
other registry in this project (schemas, normalization profiles, alignment
strategies, cleaning policies).
"""

from __future__ import annotations

from app.transformation.profiles.base import TransformationProfile
from app.transformation.profiles.multimodal_window import MULTIMODAL_WINDOW_V1

_BUILTIN_PROFILES = (MULTIMODAL_WINDOW_V1,)


class TransformationProfileNotFoundError(Exception):
    pass


class TransformationProfileRegistry:
    def __init__(self) -> None:
        self._profiles: dict[tuple[str, str], TransformationProfile] = {
            (p.profile_name, p.profile_version): p for p in _BUILTIN_PROFILES
        }

    def get(self, profile_name: str, profile_version: str) -> TransformationProfile:
        profile = self._profiles.get((profile_name, profile_version))
        if profile is None:
            raise TransformationProfileNotFoundError(
                f"No transformation profile '{profile_name}' v{profile_version} registered"
            )
        return profile

    def list_profiles(self) -> list[tuple[str, str]]:
        return sorted(self._profiles.keys())
