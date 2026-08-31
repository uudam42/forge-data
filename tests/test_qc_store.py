"""Unit tests for LocalQCReportStore and the additive
TransformedArtifactStore.find_manifest_by_transformation_id lookup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.storage.qc_store import LocalQCReportStore, QCArtifactAlreadyExistsError
from app.storage.transformed_store import LocalTransformedArtifactStore


@pytest.fixture
def store(tmp_path: Path) -> LocalQCReportStore:
    return LocalQCReportStore(root=tmp_path / "qc")


def test_staging_dir_created_under_transformation_id(store: LocalQCReportStore) -> None:
    staging = store.staging_dir(transformation_id="xform_a", qc_id="qc_1")
    assert staging.exists()
    assert staging.name == ".tmp-qc_1"
    assert staging.parent.name == "xform_a"


def test_commit_moves_staging_to_final_location(store: LocalQCReportStore) -> None:
    staging = store.staging_dir(transformation_id="xform_a", qc_id="qc_1")
    (staging / "report.json").write_text("{}")
    uri = store.commit(transformation_id="xform_a", qc_id="qc_1", staging_dir=staging)
    assert uri.startswith("file://")
    assert store.exists(transformation_id="xform_a", qc_id="qc_1")
    assert not staging.exists()


def test_commit_refuses_to_overwrite_existing_run(store: LocalQCReportStore) -> None:
    staging1 = store.staging_dir(transformation_id="xform_b", qc_id="qc_1")
    store.commit(transformation_id="xform_b", qc_id="qc_1", staging_dir=staging1)

    staging2 = store.staging_dir(transformation_id="xform_b", qc_id="qc_1_retry")
    with pytest.raises(QCArtifactAlreadyExistsError):
        store.commit(transformation_id="xform_b", qc_id="qc_1", staging_dir=staging2)


def test_discard_removes_staging_dir(store: LocalQCReportStore) -> None:
    staging = store.staging_dir(transformation_id="xform_a", qc_id="qc_1")
    (staging / "partial.json").write_text("partial")
    store.discard(staging)
    assert not staging.exists()


def test_failed_run_leaves_no_committed_artifact(store: LocalQCReportStore) -> None:
    staging = store.staging_dir(transformation_id="xform_a", qc_id="qc_1")
    (staging / "report.json").write_text("partial")
    store.discard(staging)
    assert not store.exists(transformation_id="xform_a", qc_id="qc_1")


def test_report_and_manifest_paths(store: LocalQCReportStore) -> None:
    report_path = store.report_path(transformation_id="xform_a", qc_id="qc_1")
    manifest_path = store.manifest_path(transformation_id="xform_a", qc_id="qc_1")
    assert report_path.endswith("xform_a/qc_1/report.json")
    assert manifest_path.endswith("xform_a/qc_1/manifest.json")


def test_find_manifest_returns_none_when_missing(store: LocalQCReportStore) -> None:
    assert store.find_manifest(transformation_id="xform_a", qc_id="qc_missing") is None


def test_find_manifest_reads_committed_manifest(store: LocalQCReportStore) -> None:
    staging = store.staging_dir(transformation_id="xform_a", qc_id="qc_1")
    (staging / "manifest.json").write_text(json.dumps({"qc_id": "qc_1"}))
    store.commit(transformation_id="xform_a", qc_id="qc_1", staging_dir=staging)
    assert store.find_manifest(transformation_id="xform_a", qc_id="qc_1") == {"qc_id": "qc_1"}


def test_find_manifest_by_qc_id_locates_manifest_without_transformation_id(store: LocalQCReportStore) -> None:
    staging = store.staging_dir(transformation_id="xform_a", qc_id="qc_1")
    (staging / "manifest.json").write_text(json.dumps({"qc_id": "qc_1", "transformation_id": "xform_a"}))
    store.commit(transformation_id="xform_a", qc_id="qc_1", staging_dir=staging)

    manifest = store.find_manifest_by_qc_id("qc_1")
    assert manifest is not None
    assert manifest["qc_id"] == "qc_1"
    assert manifest["transformation_id"] == "xform_a"


def test_find_manifest_by_qc_id_returns_none_when_missing(store: LocalQCReportStore) -> None:
    assert store.find_manifest_by_qc_id("does_not_exist") is None


def test_find_manifest_by_qc_id_rejects_unsafe_path_components(store: LocalQCReportStore) -> None:
    assert store.find_manifest_by_qc_id("../etc") is None
    assert store.find_manifest_by_qc_id("a/b") is None


# ---------------------------------------------------------------------------
# TransformedArtifactStore additive lookup method
# ---------------------------------------------------------------------------


@pytest.fixture
def transformed_store(tmp_path: Path) -> LocalTransformedArtifactStore:
    return LocalTransformedArtifactStore(root=tmp_path / "transformed")


def test_find_manifest_by_transformation_id_locates_manifest_without_cleaning_id(
    transformed_store: LocalTransformedArtifactStore,
) -> None:
    staging = transformed_store.staging_dir(cleaning_id="clean_1", transformation_id="xform_1")
    (staging / "manifest.json").write_text(
        json.dumps({"transformation_id": "xform_1", "cleaning_id": "clean_1"})
    )
    transformed_store.commit(cleaning_id="clean_1", transformation_id="xform_1", staging_dir=staging)

    manifest = transformed_store.find_manifest_by_transformation_id("xform_1")
    assert manifest is not None
    assert manifest["transformation_id"] == "xform_1"
    assert manifest["cleaning_id"] == "clean_1"


def test_find_manifest_by_transformation_id_returns_none_when_missing(
    transformed_store: LocalTransformedArtifactStore,
) -> None:
    assert transformed_store.find_manifest_by_transformation_id("does_not_exist") is None
