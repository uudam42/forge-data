"""Unit tests for dataset name / SemVer validation and ordering
(app.catalog.versioning)."""

from __future__ import annotations

import pytest

from app.catalog.errors import InvalidDatasetNameError, InvalidDatasetVersionError
from app.catalog.versioning import highest_version, sort_versions, validate_dataset_name, validate_semver


def test_valid_dataset_name_accepted() -> None:
    validate_dataset_name("warehouse_robot_imu_gps")  # no raise
    validate_dataset_name("a")
    validate_dataset_name("dataset-1.2_3")


def test_invalid_dataset_name_rejected() -> None:
    with pytest.raises(InvalidDatasetNameError):
        validate_dataset_name("")
    with pytest.raises(InvalidDatasetNameError):
        validate_dataset_name("../etc/passwd")
    with pytest.raises(InvalidDatasetNameError):
        validate_dataset_name("has spaces")
    with pytest.raises(InvalidDatasetNameError):
        validate_dataset_name("-starts-with-dash")


def test_valid_semver_accepted() -> None:
    assert validate_semver("1.0.0") == (1, 0, 0)
    assert validate_semver("1.2.3") == (1, 2, 3)
    assert validate_semver("2.0.0") == (2, 0, 0)


def test_invalid_semver_rejected() -> None:
    with pytest.raises(InvalidDatasetVersionError):
        validate_semver("1.0")
    with pytest.raises(InvalidDatasetVersionError):
        validate_semver("v1.0.0")
    with pytest.raises(InvalidDatasetVersionError):
        validate_semver("1.0.0-beta")
    with pytest.raises(InvalidDatasetVersionError):
        validate_semver("not-a-version")


def test_sort_versions_semantic_not_lexicographic() -> None:
    versions = ["1.9.0", "1.10.0", "1.2.0"]
    assert sort_versions(versions) == ["1.2.0", "1.9.0", "1.10.0"]


def test_highest_version_chooses_max_semver() -> None:
    assert highest_version(["1.0.0", "2.0.0", "1.5.0"]) == "2.0.0"


def test_highest_version_empty_list_returns_none() -> None:
    assert highest_version([]) is None


def test_latest_does_not_use_creation_order() -> None:
    # Registered in an order where the LAST-added version is numerically
    # smaller — highest_version must still pick the semantically largest.
    versions_in_registration_order = ["2.0.0", "1.0.0"]
    assert highest_version(versions_in_registration_order) == "2.0.0"
