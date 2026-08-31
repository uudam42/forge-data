"""Tests for LocalNormalizedArtifactStore: staging, atomic commit, and
overwrite protection — exercised independently of the API layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage.normalized_store import LocalNormalizedArtifactStore, NormalizedArtifactAlreadyExistsError


def _store(tmp_path: Path) -> LocalNormalizedArtifactStore:
    return LocalNormalizedArtifactStore(root=tmp_path / "normalized")


def test_staging_dir_is_created_hidden(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(ingestion_id="ing_a", normalization_id="norm_a")

    assert staging.exists()
    assert staging.name == ".tmp-norm_a"
    # Not discoverable as a committed run.
    assert not store.exists(ingestion_id="ing_a", normalization_id="norm_a")


def test_commit_moves_staging_to_final_location(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(ingestion_id="ing_a", normalization_id="norm_a")
    (staging / "normalized.csv").write_text("timestamp,accel_x\n")
    (staging / "manifest.json").write_text("{}")

    artifact_uri = store.commit(ingestion_id="ing_a", normalization_id="norm_a", staging_dir=staging)

    assert store.exists(ingestion_id="ing_a", normalization_id="norm_a")
    assert not staging.exists()
    final_dir = tmp_path / "normalized" / "ing_a" / "norm_a"
    assert (final_dir / "normalized.csv").exists()
    assert (final_dir / "manifest.json").exists()
    assert artifact_uri == f"file://{final_dir.resolve()}"


def test_partial_content_never_visible_before_commit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(ingestion_id="ing_a", normalization_id="norm_a")
    (staging / "normalized.csv").write_text("partial content")

    # Before commit, nothing is discoverable under the final normalization_id.
    assert not store.exists(ingestion_id="ing_a", normalization_id="norm_a")
    assert not (tmp_path / "normalized" / "ing_a" / "norm_a").exists()


def test_discard_removes_staging_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.staging_dir(ingestion_id="ing_a", normalization_id="norm_a")
    (staging / "normalized.csv").write_text("partial content")

    store.discard(staging)

    assert not staging.exists()
    assert not store.exists(ingestion_id="ing_a", normalization_id="norm_a")


def test_discard_on_missing_directory_does_not_raise(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.discard(tmp_path / "normalized" / "does_not_exist" / ".tmp-nope")  # no exception


def test_commit_cannot_overwrite_existing_run(tmp_path: Path) -> None:
    store = _store(tmp_path)

    staging_1 = store.staging_dir(ingestion_id="ing_a", normalization_id="norm_a")
    (staging_1 / "normalized.csv").write_text("first")
    store.commit(ingestion_id="ing_a", normalization_id="norm_a", staging_dir=staging_1)

    staging_2 = store.staging_dir(ingestion_id="ing_a", normalization_id="norm_a_second")
    # Simulate a second attempt landing on the same normalization_id by
    # renaming its staging dir to collide with the first commit's final dir.
    final_dir = tmp_path / "normalized" / "ing_a" / "norm_a"
    with pytest.raises(NormalizedArtifactAlreadyExistsError):
        store.commit(ingestion_id="ing_a", normalization_id="norm_a", staging_dir=staging_2)

    # Original content must be untouched.
    assert (final_dir / "normalized.csv").read_text() == "first"


def test_staging_dir_collision_fails_safe(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.staging_dir(ingestion_id="ing_a", normalization_id="norm_a")

    with pytest.raises(FileExistsError):
        store.staging_dir(ingestion_id="ing_a", normalization_id="norm_a")


def test_artifact_path_and_manifest_path_are_absolute(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact_path = store.artifact_path(
        ingestion_id="ing_a", normalization_id="norm_a", filename="normalized.csv"
    )
    manifest_path = store.manifest_path(ingestion_id="ing_a", normalization_id="norm_a")

    assert Path(artifact_path).is_absolute()
    assert Path(manifest_path).is_absolute()
    assert artifact_path.endswith("ing_a/norm_a/normalized.csv")
    assert manifest_path.endswith("ing_a/norm_a/manifest.json")
