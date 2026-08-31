"""Unit tests for LocalTransformedArtifactStore and the additive
CleanedArtifactStore.find_manifest_by_cleaning_id lookup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.storage.cleaned_store import LocalCleanedArtifactStore
from app.storage.transformed_store import LocalTransformedArtifactStore, TransformedArtifactAlreadyExistsError


@pytest.fixture
def store(tmp_path: Path) -> LocalTransformedArtifactStore:
    return LocalTransformedArtifactStore(root=tmp_path / "transformed")


def test_staging_dir_created_under_cleaning_id(store: LocalTransformedArtifactStore, tmp_path: Path) -> None:
    staging = store.staging_dir(cleaning_id="clean_a", transformation_id="xform_1")
    assert staging.exists()
    assert staging.name == ".tmp-xform_1"
    assert staging.parent.name == "clean_a"


def test_commit_moves_staging_to_final_location(store: LocalTransformedArtifactStore) -> None:
    staging = store.staging_dir(cleaning_id="clean_a", transformation_id="xform_1")
    (staging / "transformed.jsonl").write_text("{}\n")
    uri = store.commit(cleaning_id="clean_a", transformation_id="xform_1", staging_dir=staging)
    assert uri.startswith("file://")
    assert store.exists(cleaning_id="clean_a", transformation_id="xform_1")
    assert not staging.exists()


def test_commit_refuses_to_overwrite_existing_run(store: LocalTransformedArtifactStore) -> None:
    staging1 = store.staging_dir(cleaning_id="clean_b", transformation_id="xform_1")
    store.commit(cleaning_id="clean_b", transformation_id="xform_1", staging_dir=staging1)

    # A second staging directory whose transformation_id collides with the
    # already-committed run must be refused, even though its own staging
    # directory name (".tmp-xform_1_retry") differs.
    staging2 = store.staging_dir(cleaning_id="clean_b", transformation_id="xform_1_retry")
    with pytest.raises(TransformedArtifactAlreadyExistsError):
        store.commit(cleaning_id="clean_b", transformation_id="xform_1", staging_dir=staging2)


def test_discard_removes_staging_dir(store: LocalTransformedArtifactStore) -> None:
    staging = store.staging_dir(cleaning_id="clean_a", transformation_id="xform_1")
    (staging / "partial.jsonl").write_text("partial")
    store.discard(staging)
    assert not staging.exists()


def test_failed_run_leaves_no_committed_artifact(store: LocalTransformedArtifactStore) -> None:
    staging = store.staging_dir(cleaning_id="clean_a", transformation_id="xform_1")
    (staging / "transformed.jsonl").write_text("partial")
    store.discard(staging)
    assert not store.exists(cleaning_id="clean_a", transformation_id="xform_1")


def test_artifact_manifest_report_paths(store: LocalTransformedArtifactStore) -> None:
    artifact_path = store.artifact_path(cleaning_id="clean_a", transformation_id="xform_1", filename="transformed.jsonl")
    manifest_path = store.manifest_path(cleaning_id="clean_a", transformation_id="xform_1")
    report_path = store.report_path(cleaning_id="clean_a", transformation_id="xform_1")
    assert artifact_path.endswith("clean_a/xform_1/transformed.jsonl")
    assert manifest_path.endswith("clean_a/xform_1/manifest.json")
    assert report_path.endswith("clean_a/xform_1/report.json")


def test_find_manifest_returns_none_when_missing(store: LocalTransformedArtifactStore) -> None:
    assert store.find_manifest(cleaning_id="clean_a", transformation_id="xform_missing") is None


def test_find_manifest_reads_committed_manifest(store: LocalTransformedArtifactStore) -> None:
    staging = store.staging_dir(cleaning_id="clean_a", transformation_id="xform_1")
    (staging / "manifest.json").write_text(json.dumps({"transformation_id": "xform_1"}))
    store.commit(cleaning_id="clean_a", transformation_id="xform_1", staging_dir=staging)
    manifest = store.find_manifest(cleaning_id="clean_a", transformation_id="xform_1")
    assert manifest == {"transformation_id": "xform_1"}


# ---------------------------------------------------------------------------
# CleanedArtifactStore additive lookup method
# ---------------------------------------------------------------------------


@pytest.fixture
def cleaned_store(tmp_path: Path) -> LocalCleanedArtifactStore:
    return LocalCleanedArtifactStore(root=tmp_path / "cleaned")


def test_find_manifest_by_cleaning_id_locates_manifest_without_sync_id(
    cleaned_store: LocalCleanedArtifactStore,
) -> None:
    staging = cleaned_store.staging_dir(synchronization_id="sync_a", cleaning_id="clean_1")
    (staging / "manifest.json").write_text(json.dumps({"cleaning_id": "clean_1", "synchronization_id": "sync_a"}))
    cleaned_store.commit(synchronization_id="sync_a", cleaning_id="clean_1", staging_dir=staging)

    manifest = cleaned_store.find_manifest_by_cleaning_id("clean_1")
    assert manifest is not None
    assert manifest["cleaning_id"] == "clean_1"
    assert manifest["synchronization_id"] == "sync_a"


def test_find_manifest_by_cleaning_id_returns_none_when_missing(
    cleaned_store: LocalCleanedArtifactStore,
) -> None:
    assert cleaned_store.find_manifest_by_cleaning_id("does_not_exist") is None


def test_find_manifest_by_cleaning_id_rejects_unsafe_path_components(
    cleaned_store: LocalCleanedArtifactStore,
) -> None:
    assert cleaned_store.find_manifest_by_cleaning_id("../etc") is None
    assert cleaned_store.find_manifest_by_cleaning_id("a/b") is None
