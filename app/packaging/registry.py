"""Maps (profile_name, profile_version) to a PackagingProfile.

Lookup is always explicit — never an implicit "latest" — mirroring every
other registry in this project.
"""

from __future__ import annotations

from app.packaging.profiles.base import PackagingProfile
from app.packaging.profiles.default import DEFAULT_ML_PACKAGE

_BUILTIN_PROFILES = (DEFAULT_ML_PACKAGE,)


class PackagingProfileNotFoundError(Exception):
    pass


class PackagingProfileRegistry:
    def __init__(self) -> None:
        self._profiles: dict[tuple[str, str], PackagingProfile] = {
            (p.profile_name, p.profile_version): p for p in _BUILTIN_PROFILES
        }

    def get(self, profile_name: str, profile_version: str) -> PackagingProfile:
        profile = self._profiles.get((profile_name, profile_version))
        if profile is None:
            raise PackagingProfileNotFoundError(
                f"No packaging profile '{profile_name}' v{profile_version} registered"
            )
        return profile

    def list_profiles(self) -> list[tuple[str, str]]:
        return sorted(self._profiles.keys())
