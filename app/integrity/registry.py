"""Maps a schema_name to its IntegrityChecker implementation.

This is a second, orthogonal axis from app.validation.registry's
ValidatorRegistry: that one is keyed by file extension (how to read the
file); this one is keyed by schema_name (what semantics to check). File
reading for integrity lives in app.integrity.records instead.
"""

from __future__ import annotations

from app.integrity.checks.base import IntegrityChecker
from app.integrity.checks.gps import GpsIntegrityChecker
from app.integrity.checks.imu import ImuIntegrityChecker


class UnsupportedIntegrityCheckerError(Exception):
    pass


class IntegrityCheckerRegistry:
    def __init__(self) -> None:
        self._checkers: dict[str, IntegrityChecker] = {
            "imu": ImuIntegrityChecker(),
            "gps": GpsIntegrityChecker(),
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
