"""Tests for LocalSynchronizationArtifactStore: staging, atomic commit, and
overwrite protection — exercised independently of the API layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage.synchronization_store import (
    LocalSynchronizationArtifactStore,
    SynchronizationArtifactAlreadyExistsError,
)


def _store(tmp_path: Path) -> LocalSynchronizationArtifactStore:
    return LocalSynchronizationArtifactStore(root=tmp_path / "synchronized")


def test_staging_dir_is_hidden_and_not_discoverable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(synchronization_id="sync_a")

    assert staging.exists()
    assert staging.name == ".tmp-sync_a"
    assert not store.exists(synchronization_id="sync_a")


def test_commit_moves_staging_to_final_location(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(synchronization_id="sync_a")
    (staging / "synchronized.jsonl").write_text('{"a": 1}\n')
    (staging / "manifest.json").write_text("{}")

    artifact_uri = store.commit(synchronization_id="sync_a", staging_dir=staging)

    assert store.exists(synchronization_id="sync_a")
    assert not staging.exists()
    final_dir = tmp_path / "synchronized" / "sync_a"
    assert (final_dir / "synchronized.jsonl").exists()
    assert artifact_uri == f"file://{final_dir.resolve()}"


def test_existing_synchronization_run_cannot_be_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)

    staging_1 = store.staging_dir(synchronization_id="sync_a")
    (staging_1 / "synchronized.jsonl").write_text("first")
    store.commit(synchronization_id="sync_a", staging_dir=staging_1)

    staging_2 = store.staging_dir(synchronization_id="sync_a_second")
    with pytest.raises(SynchronizationArtifactAlreadyExistsError):
        store.commit(synchronization_id="sync_a", staging_dir=staging_2)

    final_dir = tmp_path / "synchronized" / "sync_a"
    assert (final_dir / "synchronized.jsonl").read_text() == "first"


def test_discard_removes_staging_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(synchronization_id="sync_a")
    (staging / "synchronized.jsonl").write_text("partial")

    store.discard(staging)

    assert not staging.exists()
    assert not store.exists(synchronization_id="sync_a")


def test_failed_run_leaves_no_committed_partial_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(synchronization_id="sync_a")
    (staging / "synchronized.jsonl").write_text("partial content that never finished")

    # Simulate a failure path: discard instead of commit.
    store.discard(staging)

    assert not store.exists(synchronization_id="sync_a")
    assert not (tmp_path / "synchronized" / "sync_a").exists()
    assert not staging.exists()


def test_find_manifest_returns_none_for_unknown_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.find_manifest("sync_does_not_exist") is None


def test_find_manifest_reads_committed_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(synchronization_id="sync_a")
    (staging / "manifest.json").write_text('{"synchronization_id": "sync_a"}')
    (staging / "synchronized.jsonl").write_text("")
    store.commit(synchronization_id="sync_a", staging_dir=staging)

    manifest = store.find_manifest("sync_a")
    assert manifest == {"synchronization_id": "sync_a"}


def test_open_artifact_reads_committed_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(synchronization_id="sync_a")
    (staging / "synchronized.jsonl").write_bytes(b"line1\nline2\n")
    (staging / "manifest.json").write_text("{}")
    store.commit(synchronization_id="sync_a", staging_dir=staging)

    with store.open_artifact(synchronization_id="sync_a", filename="synchronized.jsonl") as f:
        assert f.read() == b"line1\nline2\n"
