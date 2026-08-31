"""Unit tests for the SQLite-backed CatalogRepository."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.catalog.errors import ArtifactRegistryConflictError
from app.catalog.repository import CatalogRepository
from app.storage.catalog_store import CATALOG_SCHEMA_VERSION, get_connection


@pytest.fixture
def repo(tmp_path: Path) -> CatalogRepository:
    conn = get_connection(tmp_path / "catalog.db")
    return CatalogRepository(conn)


def _artifact_record(**overrides) -> dict:
    record = {
        "artifact_type": "ingestion",
        "artifact_id": "ing_1",
        "pipeline_stage": 1,
        "status": None,
        "storage_uri": "file:///a",
        "content_sha256": "abc123",
        "manifest_uri": "file:///a/manifest.json",
        "manifest_sha256": "def456",
        "created_at": "2026-01-01T00:00:00Z",
        "session_id": "sess_1",
        "metadata_json": '{"ingestion_id":"ing_1"}',
        "registered_at": "2026-01-01T00:00:01Z",
    }
    record.update(overrides)
    return record


def test_empty_catalog_initializes(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "new_catalog.db")
    repo = CatalogRepository(conn)
    assert repo.count_artifacts() == 0
    assert repo.count_edges() == 0


def test_sqlite_schema_created(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "catalog.db")
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"artifacts", "lineage_edges", "lineage_issues", "datasets", "dataset_versions", "catalog_metadata"} <= tables


def test_foreign_keys_enabled(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "catalog.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_catalog_schema_version_recorded(repo: CatalogRepository) -> None:
    assert repo.get_metadata("catalog_schema_version") == CATALOG_SCHEMA_VERSION


def test_artifact_registration_succeeds(repo: CatalogRepository) -> None:
    with repo.transaction():
        result = repo.upsert_artifact(_artifact_record())
    assert result == "inserted"
    assert repo.get_artifact("ingestion", "ing_1") is not None


def test_artifact_registration_idempotent(repo: CatalogRepository) -> None:
    with repo.transaction():
        repo.upsert_artifact(_artifact_record())
    with repo.transaction():
        result = repo.upsert_artifact(_artifact_record())
    assert result == "unchanged"
    assert repo.count_artifacts() == 1


def test_conflicting_immutable_artifact_rejected(repo: CatalogRepository) -> None:
    with repo.transaction():
        repo.upsert_artifact(_artifact_record())
    with pytest.raises(ArtifactRegistryConflictError):
        with repo.transaction():
            repo.upsert_artifact(_artifact_record(content_sha256="different"))


def test_conflict_on_manifest_sha256_change(repo: CatalogRepository) -> None:
    with repo.transaction():
        repo.upsert_artifact(_artifact_record())
    with pytest.raises(ArtifactRegistryConflictError):
        with repo.transaction():
            repo.upsert_artifact(_artifact_record(manifest_sha256="tampered"))


def test_insert_edge_idempotent(repo: CatalogRepository) -> None:
    with repo.transaction():
        repo.upsert_artifact(_artifact_record())
        repo.upsert_artifact(_artifact_record(artifact_type="validation", artifact_id="val_1", pipeline_stage=2))
        first = repo.insert_edge(parent_type="ingestion", parent_id="ing_1", child_type="validation", child_id="val_1", relationship="validated_from")
        second = repo.insert_edge(parent_type="ingestion", parent_id="ing_1", child_type="validation", child_id="val_1", relationship="validated_from")
    assert first is True
    assert second is False
    assert repo.count_edges() == 1


def test_get_parents_and_children(repo: CatalogRepository) -> None:
    with repo.transaction():
        repo.upsert_artifact(_artifact_record())
        repo.upsert_artifact(_artifact_record(artifact_type="validation", artifact_id="val_1", pipeline_stage=2))
        repo.insert_edge(parent_type="ingestion", parent_id="ing_1", child_type="validation", child_id="val_1", relationship="validated_from")
    assert len(repo.get_children("ingestion", "ing_1")) == 1
    assert len(repo.get_parents("validation", "val_1")) == 1
    assert repo.get_parents("ingestion", "ing_1") == []


def test_list_artifacts_filtering(repo: CatalogRepository) -> None:
    with repo.transaction():
        repo.upsert_artifact(_artifact_record())
        repo.upsert_artifact(_artifact_record(artifact_type="qc", artifact_id="qc_1", pipeline_stage=8, status="passed"))
        repo.upsert_artifact(_artifact_record(artifact_type="qc", artifact_id="qc_2", pipeline_stage=8, status="failed"))
    assert len(repo.list_artifacts(artifact_type="qc")) == 2
    assert len(repo.list_artifacts(artifact_type="qc", status="failed")) == 1
    assert len(repo.list_artifacts(stage=8)) == 2


def test_clear_artifact_index_preserves_datasets(repo: CatalogRepository) -> None:
    with repo.transaction():
        repo.upsert_artifact(_artifact_record())
        repo.create_dataset(dataset_name="ds1", description=None, metadata_json="{}", created_at="2026-01-01T00:00:00Z")
    with repo.transaction():
        repo.clear_artifact_index()
    assert repo.count_artifacts() == 0
    assert repo.count_datasets() == 1


def test_record_and_list_issues(repo: CatalogRepository) -> None:
    with repo.transaction():
        repo.record_issue(artifact_type="validation", artifact_id="val_x", issue_code="MISSING_LINEAGE_PARENT", detail="x", detected_at="2026-01-01T00:00:00Z")
    issues = repo.list_issues()
    assert len(issues) == 1
    assert issues[0]["issue_code"] == "MISSING_LINEAGE_PARENT"


def test_transaction_rollback_on_error(repo: CatalogRepository) -> None:
    with pytest.raises(ValueError):
        with repo.transaction():
            repo.upsert_artifact(_artifact_record())
            raise ValueError("boom")
    assert repo.count_artifacts() == 0


def test_dataset_create_and_get(repo: CatalogRepository) -> None:
    with repo.transaction():
        repo.create_dataset(dataset_name="ds1", description="d", metadata_json="{}", created_at="2026-01-01T00:00:00Z")
    dataset = repo.get_dataset("ds1")
    assert dataset is not None
    assert dataset["description"] == "d"


def test_dataset_version_create_and_list(repo: CatalogRepository) -> None:
    with repo.transaction():
        repo.create_dataset(dataset_name="ds1", description=None, metadata_json="{}", created_at="2026-01-01T00:00:00Z")
        repo.create_dataset_version(
            dataset_name="ds1", version="1.0.0", package_id="pkg_1", description=None, tags_json="[]",
            status="active", created_at="2026-01-01T00:00:00Z",
        )
    versions = repo.list_dataset_versions("ds1")
    assert len(versions) == 1
    assert versions[0]["package_id"] == "pkg_1"


def test_list_dataset_versions_for_packages(repo: CatalogRepository) -> None:
    with repo.transaction():
        repo.create_dataset(dataset_name="ds1", description=None, metadata_json="{}", created_at="2026-01-01T00:00:00Z")
        repo.create_dataset_version(dataset_name="ds1", version="1.0.0", package_id="pkg_1", description=None, tags_json="[]", status="active", created_at="2026-01-01T00:00:00Z")
    result = repo.list_dataset_versions_for_packages(["pkg_1", "pkg_2"])
    assert len(result) == 1
    assert repo.list_dataset_versions_for_packages([]) == []


def test_metadata_json_not_pickle(repo: CatalogRepository) -> None:
    """metadata_json must be a plain JSON string — never a pickled blob."""
    with repo.transaction():
        repo.upsert_artifact(_artifact_record())
    artifact = repo.get_artifact("ingestion", "ing_1")
    import json

    parsed = json.loads(artifact["metadata_json"])  # must not raise
    assert parsed == {"ingestion_id": "ing_1"}
