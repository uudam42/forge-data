"""Dataset name / SemVer validation and version ordering.

Kept separate from CatalogService so validation rules are testable in
isolation and never duplicated across API routes.
"""

from __future__ import annotations

import re

from app.catalog.errors import InvalidDatasetNameError, InvalidDatasetVersionError

_DATASET_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_SEMVER_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def validate_dataset_name(name: str) -> None:
    if not _DATASET_NAME_PATTERN.match(name):
        raise InvalidDatasetNameError(
            f"dataset_name {name!r} must match {_DATASET_NAME_PATTERN.pattern} "
            f"(conservative, filesystem-independent metadata — never used as an unchecked path)"
        )


def validate_semver(version: str) -> tuple[int, int, int]:
    match = _SEMVER_PATTERN.match(version)
    if not match:
        raise InvalidDatasetVersionError(
            f"version {version!r} is not a valid MAJOR.MINOR.PATCH SemVer string "
            f"(pre-release/build-metadata suffixes are not supported in this MVP)"
        )
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def sort_versions(versions: list[str]) -> list[str]:
    """Sorted ascending by SEMANTIC version, never by string or creation
    order — 1.9.0 sorts before 1.10.0."""
    return sorted(versions, key=validate_semver)


def highest_version(versions: list[str]) -> str | None:
    """'Latest' means highest semantic version, NOT most recently created
    — documented explicitly since the two can disagree (a hotfix release
    numbered lower than an already-registered version, registered later)."""
    if not versions:
        return None
    return sort_versions(versions)[-1]
