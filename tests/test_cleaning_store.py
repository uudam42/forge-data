"""Tests for LocalCleanedArtifactStore: staging, atomic commit, and
overwrite protection — exercised independently of the API layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage.cleaned_store import CleanedArtifactAlreadyExistsError, LocalCleanedArtifactStore


def _store(tmp_path: Path) -> LocalCleanedArtifactStore:
    return LocalCleanedArtifactStore(root=tmp_path / "cleaned")


def test_staging_dir_is_hidden_and_not_discoverable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(synchronization_id="sync_a", cleaning_id="clean_a")

    assert staging.exists()
    assert staging.name == ".tmp-clean_a"
    assert not store.exists(synchronization_id="sync_a", cleaning_id="clean_a")


def test_commit_moves_staging_to_final_location(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(synchronization_id="sync_a", cleaning_id="clean_a")
    (staging / "cleaned.jsonl").write_text('{"a": 1}\n')
    (staging / "report.json").write_text("{}")
    (staging / "manifest.json").write_text("{}")

    artifact_uri = store.commit(synchronization_id="sync_a", cleaning_id="clean_a", staging_dir=staging)

    assert store.exists(synchronization_id="sync_a", cleaning_id="clean_a")
    assert not staging.exists()
    final_dir = tmp_path / "cleaned" / "sync_a" / "clean_a"
    assert (final_dir / "cleaned.jsonl").exists()
    assert artifact_uri == f"file://{final_dir.resolve()}"


def test_existing_cleaning_run_cannot_be_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)

    staging_1 = store.staging_dir(synchronization_id="sync_a", cleaning_id="clean_a")
    (staging_1 / "cleaned.jsonl").write_text("first")
    store.commit(synchronization_id="sync_a", cleaning_id="clean_a", staging_dir=staging_1)

    staging_2 = store.staging_dir(synchronization_id="sync_a", cleaning_id="clean_a_second")
    with pytest.raises(CleanedArtifactAlreadyExistsError):
        store.commit(synchronization_id="sync_a", cleaning_id="clean_a", staging_dir=staging_2)

    final_dir = tmp_path / "cleaned" / "sync_a" / "clean_a"
    assert (final_dir / "cleaned.jsonl").read_text() == "first"


def test_discard_removes_staging_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(synchronization_id="sync_a", cleaning_id="clean_a")
    (staging / "cleaned.jsonl").write_text("partial")

    store.discard(staging)

    assert not staging.exists()
    assert not store.exists(synchronization_id="sync_a", cleaning_id="clean_a")


def test_failed_run_leaves_no_committed_partial_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(synchronization_id="sync_a", cleaning_id="clean_a")
    (staging / "cleaned.jsonl").write_text("partial content that never finished")

    store.discard(staging)

    assert not store.exists(synchronization_id="sync_a", cleaning_id="clean_a")
    assert not (tmp_path / "cleaned" / "sync_a" / "clean_a").exists()
    assert not staging.exists()


def test_two_cleaning_runs_for_same_synchronization_coexist(tmp_path: Path) -> None:
    store = _store(tmp_path)

    staging_1 = store.staging_dir(synchronization_id="sync_a", cleaning_id="clean_1")
    (staging_1 / "cleaned.jsonl").write_text("first")
    store.commit(synchronization_id="sync_a", cleaning_id="clean_1", staging_dir=staging_1)

    staging_2 = store.staging_dir(synchronization_id="sync_a", cleaning_id="clean_2")
    (staging_2 / "cleaned.jsonl").write_text("second")
    store.commit(synchronization_id="sync_a", cleaning_id="clean_2", staging_dir=staging_2)

    assert store.exists(synchronization_id="sync_a", cleaning_id="clean_1")
    assert store.exists(synchronization_id="sync_a", cleaning_id="clean_2")


def test_find_manifest_returns_none_for_unknown_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.find_manifest(synchronization_id="sync_a", cleaning_id="clean_missing") is None


def test_find_manifest_reads_committed_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(synchronization_id="sync_a", cleaning_id="clean_a")
    (staging / "manifest.json").write_text('{"cleaning_id": "clean_a"}')
    (staging / "cleaned.jsonl").write_text("")
    store.commit(synchronization_id="sync_a", cleaning_id="clean_a", staging_dir=staging)

    manifest = store.find_manifest(synchronization_id="sync_a", cleaning_id="clean_a")
    assert manifest == {"cleaning_id": "clean_a"}
