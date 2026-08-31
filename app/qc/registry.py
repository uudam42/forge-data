"""Maps (profile_name, profile_version) to a QCProfile.

Lookup is always explicit — never an implicit "latest" — mirroring every
other registry in this project.
"""

from __future__ import annotations

from app.qc.profiles.base import QCProfile
from app.qc.profiles.default import DEFAULT_DATASET_QC

_BUILTIN_PROFILES = (DEFAULT_DATASET_QC,)


class QCProfileNotFoundError(Exception):
    pass


class QCProfileRegistry:
    def __init__(self) -> None:
        self._profiles: dict[tuple[str, str], QCProfile] = {
            (p.profile_name, p.profile_version): p for p in _BUILTIN_PROFILES
        }

    def get(self, profile_name: str, profile_version: str) -> QCProfile:
        profile = self._profiles.get((profile_name, profile_version))
        if profile is None:
            raise QCProfileNotFoundError(f"No QC profile '{profile_name}' v{profile_version} registered")
        return profile

    def list_profiles(self) -> list[tuple[str, str]]:
        return sorted(self._profiles.keys())
