"""Unit tests for LocalDatasetPackageStore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.storage.package_store import LocalDatasetPackageStore, PackageAlreadyExistsError


@pytest.fixture
def store(tmp_path: Path) -> LocalDatasetPackageStore:
    return LocalDatasetPackageStore(root=tmp_path / "packages")


def test_staging_dir_created_under_transformation_id(store: LocalDatasetPackageStore) -> None:
    staging = store.staging_dir(transformation_id="xform_a", package_id="pkg_1")
    assert staging.exists()
    assert staging.name == ".tmp-pkg_1"
    assert staging.parent.name == "xform_a"


def test_commit_moves_staging_to_final_location(store: LocalDatasetPackageStore) -> None:
    staging = store.staging_dir(transformation_id="xform_a", package_id="pkg_1")
    (staging / "manifest.json").write_text("{}")
    uri = store.commit(transformation_id="xform_a", package_id="pkg_1", staging_dir=staging)
    assert uri.startswith("file://")
    assert store.exists(transformation_id="xform_a", package_id="pkg_1")
    assert not staging.exists()


def test_commit_refuses_to_overwrite_existing_package(store: LocalDatasetPackageStore) -> None:
    staging1 = store.staging_dir(transformation_id="xform_b", package_id="pkg_1")
    store.commit(transformation_id="xform_b", package_id="pkg_1", staging_dir=staging1)

    staging2 = store.staging_dir(transformation_id="xform_b", package_id="pkg_1_retry")
    with pytest.raises(PackageAlreadyExistsError):
        store.commit(transformation_id="xform_b", package_id="pkg_1", staging_dir=staging2)


def test_discard_removes_staging_dir(store: LocalDatasetPackageStore) -> None:
    staging = store.staging_dir(transformation_id="xform_a", package_id="pkg_1")
    (staging / "train.jsonl").write_text("partial")
    store.discard(staging)
    assert not staging.exists()


def test_failed_run_leaves_no_committed_artifact(store: LocalDatasetPackageStore) -> None:
    staging = store.staging_dir(transformation_id="xform_a", package_id="pkg_1")
    (staging / "train.jsonl").write_text("partial")
    store.discard(staging)
    assert not store.exists(transformation_id="xform_a", package_id="pkg_1")


def test_artifact_manifest_report_paths(store: LocalDatasetPackageStore) -> None:
    train_path = store.artifact_path(transformation_id="xform_a", package_id="pkg_1", filename="train.jsonl")
    optional_path = store.artifact_path(transformation_id="xform_a", package_id="pkg_1", filename="optional/train.parquet")
    manifest_path = store.manifest_path(transformation_id="xform_a", package_id="pkg_1")
    report_path = store.report_path(transformation_id="xform_a", package_id="pkg_1")
    assert train_path.endswith("xform_a/pkg_1/train.jsonl")
    assert optional_path.endswith("xform_a/pkg_1/optional/train.parquet")
    assert manifest_path.endswith("xform_a/pkg_1/manifest.json")
    assert report_path.endswith("xform_a/pkg_1/report.json")


def test_find_manifest_returns_none_when_missing(store: LocalDatasetPackageStore) -> None:
    assert store.find_manifest(transformation_id="xform_a", package_id="pkg_missing") is None


def test_find_manifest_reads_committed_manifest(store: LocalDatasetPackageStore) -> None:
    staging = store.staging_dir(transformation_id="xform_a", package_id="pkg_1")
    (staging / "manifest.json").write_text(json.dumps({"package_id": "pkg_1"}))
    store.commit(transformation_id="xform_a", package_id="pkg_1", staging_dir=staging)
    assert store.find_manifest(transformation_id="xform_a", package_id="pkg_1") == {"package_id": "pkg_1"}
