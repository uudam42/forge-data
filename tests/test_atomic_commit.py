"""Unit tests for the shared crash-safety primitive (app.storage.atomic):
staging creation, the staging_state.json run-state journal, atomic
commit, destination-collision handling, and fault injection at every
named checkpoint.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.storage.atomic import (
    STAGING_STATE_FILENAME,
    commit_staging_dir,
    create_staging_dir,
    discard_staging_dir,
    fault_injector,
    read_staging_metadata,
    write_manifest_file,
)
from app.storage.errors import ArtifactCommitFailedError, ArtifactDestinationExistsError, StagingCreateFailedError
from tests.crash_safety_helpers import assert_final_artifact_checksums_valid, assert_no_partial_final_artifacts


@pytest.fixture(autouse=True)
def _clear_fault_injector():
    fault_injector.clear()
    yield
    fault_injector.clear()


def test_create_staging_dir_writes_metadata(tmp_path: Path) -> None:
    staging = tmp_path / ".staging" / "op_1"
    final_dir = tmp_path / "final" / "op_1"

    create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=final_dir)

    assert staging.is_dir()
    metadata = read_staging_metadata(staging)
    assert metadata is not None
    assert metadata.operation_id == "op_1"
    assert metadata.artifact_id == "art_1"
    assert metadata.stage == "normalization"
    assert metadata.state == "writing"
    assert metadata.final_destination == str(final_dir)
    assert metadata.pid > 0
    assert metadata.started_at  # non-empty ISO timestamp


def test_create_staging_dir_collision_raises_file_exists_error(tmp_path: Path) -> None:
    staging = tmp_path / ".staging" / "op_1"
    create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=tmp_path / "x")

    with pytest.raises(FileExistsError):
        create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=tmp_path / "x")


def test_successful_commit_publishes_atomically(tmp_path: Path) -> None:
    import hashlib

    staging = tmp_path / ".staging" / "op_1"
    final_dir = tmp_path / "final" / "art_1"
    create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=final_dir)

    content = "hello\n"
    (staging / "data.jsonl").write_text(content, encoding="utf-8")
    write_manifest_file(
        staging, "manifest.json", json.dumps({"status": "completed", "sha256": hashlib.sha256(content.encode()).hexdigest()})
    )

    assert not final_dir.exists()  # invisible before commit
    commit_staging_dir(staging, final_dir)

    assert final_dir.is_dir()
    assert (final_dir / "data.jsonl").read_text(encoding="utf-8") == content
    # The run-state journal never becomes part of a finalized artifact.
    assert not (final_dir / STAGING_STATE_FILENAME).exists()
    assert_no_partial_final_artifacts(final_dir)
    assert_final_artifact_checksums_valid(final_dir, data_filename="data.jsonl", manifest_field="sha256")


def test_commit_creates_missing_parent_directories(tmp_path: Path) -> None:
    """Ingestion stages into a dedicated .staging/ subtree, not a sibling
    of its (deeply nested) final location -- commit must create that
    parent chain itself."""
    staging = tmp_path / ".staging" / "ing_1"
    final_dir = tmp_path / "cust_a" / "sess_a" / "ing_1"
    create_staging_dir(staging, operation_id="ing_1", artifact_id="ing_1", stage="ingestion", final_destination=final_dir)
    (staging / "file.csv").write_bytes(b"data")

    commit_staging_dir(staging, final_dir)

    assert final_dir.is_dir()
    assert (final_dir / "file.csv").read_bytes() == b"data"


def test_commit_rejects_existing_destination(tmp_path: Path) -> None:
    staging = tmp_path / ".staging" / "op_1"
    final_dir = tmp_path / "final" / "art_1"
    final_dir.mkdir(parents=True)
    (final_dir / "manifest.json").write_text("{}", encoding="utf-8")

    create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=final_dir)

    with pytest.raises(ArtifactDestinationExistsError):
        commit_staging_dir(staging, final_dir)

    # The pre-existing finalized artifact is completely untouched.
    assert (final_dir / "manifest.json").read_text(encoding="utf-8") == "{}"
    # And the staging directory is left in place -- commit never discards
    # on failure; that's the caller's job (mirrors every store's own
    # try/except Exception: discard() pattern).
    assert staging.exists()


def test_discard_staging_dir_is_idempotent(tmp_path: Path) -> None:
    staging = tmp_path / ".staging" / "op_1"
    create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=tmp_path / "x")
    discard_staging_dir(staging)
    assert not staging.exists()
    discard_staging_dir(staging)  # no exception on a directory that doesn't exist


def test_write_manifest_file_fires_after_manifest_write_checkpoint(tmp_path: Path) -> None:
    staging = tmp_path / ".staging" / "op_1"
    create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=tmp_path / "x")

    hits = []
    fault_injector.install("AFTER_MANIFEST_WRITE", lambda: hits.append(True))
    write_manifest_file(staging, "manifest.json", "{}")
    assert hits == [True]


@pytest.mark.parametrize(
    "checkpoint",
    ["AFTER_STAGING_CREATED", "AFTER_DATA_FSYNC", "AFTER_MANIFEST_FSYNC", "BEFORE_RENAME", "AFTER_RENAME", "BEFORE_PARENT_FSYNC"],
)
def test_fault_injection_at_each_checkpoint_prevents_or_follows_commit(tmp_path: Path, checkpoint: str) -> None:
    """Injecting a crash before BEFORE_RENAME must leave no final artifact.
    Injecting at/after AFTER_RENAME means the artifact IS already
    published (rename succeeded) -- this simply proves every named
    checkpoint is actually reachable and fires exactly once per commit.
    """
    staging = tmp_path / ".staging" / "op_1"
    final_dir = tmp_path / "final" / "art_1"

    class _Boom(Exception):
        pass

    def _raise():
        raise _Boom(checkpoint)

    if checkpoint == "AFTER_STAGING_CREATED":
        fault_injector.install(checkpoint, _raise)
        with pytest.raises(_Boom):
            create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=final_dir)
        assert not final_dir.exists()
        return

    create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=final_dir)
    write_manifest_file(staging, "manifest.json", "{}")
    fault_injector.install(checkpoint, _raise)

    with pytest.raises(_Boom):
        commit_staging_dir(staging, final_dir)

    if checkpoint in ("AFTER_DATA_FSYNC", "AFTER_MANIFEST_FSYNC", "BEFORE_RENAME"):
        assert not final_dir.exists()
    else:  # AFTER_RENAME / BEFORE_PARENT_FSYNC: rename already happened
        assert final_dir.exists()


def test_commit_failed_error_when_rename_itself_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staging = tmp_path / ".staging" / "op_1"
    final_dir = tmp_path / "final" / "art_1"
    create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=final_dir)
    write_manifest_file(staging, "manifest.json", "{}")

    def _boom(self, target):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(Path, "rename", _boom)
    with pytest.raises(ArtifactCommitFailedError):
        commit_staging_dir(staging, final_dir)
    assert not final_dir.exists()


def test_staging_create_failed_when_metadata_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.storage.atomic as atomic_module

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_module, "write_staging_metadata", _boom)
    staging = tmp_path / ".staging" / "op_1"
    with pytest.raises(StagingCreateFailedError):
        create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=tmp_path / "x")


def test_read_staging_metadata_returns_none_for_missing_or_malformed(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert read_staging_metadata(missing) is None

    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / STAGING_STATE_FILENAME).write_text("not json", encoding="utf-8")
    assert read_staging_metadata(malformed) is None


def test_checksum_mismatch_verify_hook_prevents_publish(tmp_path: Path) -> None:
    import hashlib

    from app.storage.errors import ArtifactChecksumMismatchError

    staging = tmp_path / ".staging" / "op_1"
    final_dir = tmp_path / "final" / "art_1"
    create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=final_dir)
    (staging / "data.jsonl").write_text("hello\n", encoding="utf-8")
    write_manifest_file(staging, "manifest.json", "{}")

    expected_sha256 = "0" * 64  # deliberately wrong

    def _verify(dir_path: Path) -> None:
        actual = hashlib.sha256((dir_path / "data.jsonl").read_bytes()).hexdigest()
        if actual != expected_sha256:
            raise ArtifactChecksumMismatchError(f"expected {expected_sha256}, got {actual}")

    with pytest.raises(ArtifactChecksumMismatchError):
        commit_staging_dir(staging, final_dir, verify=_verify)

    assert not final_dir.exists()
    assert staging.exists()  # left intact for inspection, exactly like any other pre-rename failure


def test_checksum_match_verify_hook_allows_publish(tmp_path: Path) -> None:
    import hashlib

    staging = tmp_path / ".staging" / "op_1"
    final_dir = tmp_path / "final" / "art_1"
    create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=final_dir)
    content = b"hello\n"
    (staging / "data.jsonl").write_bytes(content)
    write_manifest_file(staging, "manifest.json", "{}")
    expected_sha256 = hashlib.sha256(content).hexdigest()

    def _verify(dir_path: Path) -> None:
        actual = hashlib.sha256((dir_path / "data.jsonl").read_bytes()).hexdigest()
        assert actual == expected_sha256

    commit_staging_dir(staging, final_dir, verify=_verify)
    assert final_dir.exists()


def test_fsync_disabled_still_commits_successfully(tmp_path: Path) -> None:
    staging = tmp_path / ".staging" / "op_1"
    final_dir = tmp_path / "final" / "art_1"
    create_staging_dir(staging, operation_id="op_1", artifact_id="art_1", stage="normalization", final_destination=final_dir)
    write_manifest_file(staging, "manifest.json", "{}")

    commit_staging_dir(staging, final_dir, fsync_enabled=False)
    assert final_dir.is_dir()
