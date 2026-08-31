"""Unit tests for DAG traversal and cycle detection (app.catalog.graph)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.catalog import graph
from app.catalog.repository import CatalogRepository
from app.storage.catalog_store import get_connection


@pytest.fixture
def repo(tmp_path: Path) -> CatalogRepository:
    conn = get_connection(tmp_path / "catalog.db")
    return CatalogRepository(conn)


def _add_artifact(repo: CatalogRepository, artifact_type: str, artifact_id: str, stage: int) -> None:
    repo.upsert_artifact(
        {
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "pipeline_stage": stage,
            "status": None,
            "storage_uri": None,
            "content_sha256": None,
            "manifest_uri": f"file:///{artifact_id}",
            "manifest_sha256": "x",
            "created_at": None,
            "session_id": None,
            "metadata_json": "{}",
            "registered_at": "2026-01-01T00:00:00Z",
        }
    )


def _build_linear_chain(repo: CatalogRepository) -> None:
    with repo.transaction():
        _add_artifact(repo, "ingestion", "ing_1", 1)
        _add_artifact(repo, "validation", "val_1", 2)
        _add_artifact(repo, "integrity", "integ_1", 3)
        repo.insert_edge(parent_type="ingestion", parent_id="ing_1", child_type="validation", child_id="val_1", relationship="validated_from")
        repo.insert_edge(parent_type="validation", parent_id="val_1", child_type="integrity", child_id="integ_1", relationship="checked_from")


def test_upstream_traversal(repo: CatalogRepository) -> None:
    _build_linear_chain(repo)
    nodes, edges = graph.traverse(repo, root_type="integrity", root_id="integ_1", direction="upstream")
    ids = {n["artifact_id"] for n in nodes}
    assert ids == {"ing_1", "val_1", "integ_1"}
    assert len(edges) == 2


def test_downstream_traversal(repo: CatalogRepository) -> None:
    _build_linear_chain(repo)
    nodes, edges = graph.traverse(repo, root_type="ingestion", root_id="ing_1", direction="downstream")
    ids = {n["artifact_id"] for n in nodes}
    assert ids == {"ing_1", "val_1", "integ_1"}


def test_direction_both_includes_everything(repo: CatalogRepository) -> None:
    _build_linear_chain(repo)
    nodes, _ = graph.traverse(repo, root_type="validation", root_id="val_1", direction="both")
    ids = {n["artifact_id"] for n in nodes}
    assert ids == {"ing_1", "val_1", "integ_1"}


def test_branching_dag_represented_correctly(repo: CatalogRepository) -> None:
    with repo.transaction():
        _add_artifact(repo, "normalization", "norm_imu", 4)
        _add_artifact(repo, "normalization", "norm_gps", 4)
        _add_artifact(repo, "synchronization", "sync_1", 5)
        repo.insert_edge(parent_type="normalization", parent_id="norm_imu", child_type="synchronization", child_id="sync_1", relationship="synchronized_from")
        repo.insert_edge(parent_type="normalization", parent_id="norm_gps", child_type="synchronization", child_id="sync_1", relationship="synchronized_from")

    nodes, edges = graph.traverse(repo, root_type="synchronization", root_id="sync_1", direction="upstream")
    ids = {n["artifact_id"] for n in nodes}
    assert ids == {"norm_imu", "norm_gps", "sync_1"}
    assert len(edges) == 2


def test_deterministic_node_ordering(repo: CatalogRepository) -> None:
    _build_linear_chain(repo)
    nodes, _ = graph.traverse(repo, root_type="integrity", root_id="integ_1", direction="upstream")
    stages = [n["pipeline_stage"] for n in nodes]
    assert stages == sorted(stages)


def test_deterministic_edge_ordering(repo: CatalogRepository) -> None:
    with repo.transaction():
        _add_artifact(repo, "normalization", "norm_b", 4)
        _add_artifact(repo, "normalization", "norm_a", 4)
        _add_artifact(repo, "synchronization", "sync_1", 5)
        repo.insert_edge(parent_type="normalization", parent_id="norm_b", child_type="synchronization", child_id="sync_1", relationship="synchronized_from")
        repo.insert_edge(parent_type="normalization", parent_id="norm_a", child_type="synchronization", child_id="sync_1", relationship="synchronized_from")

    _, edges1 = graph.traverse(repo, root_type="synchronization", root_id="sync_1", direction="upstream")
    _, edges2 = graph.traverse(repo, root_type="synchronization", root_id="sync_1", direction="upstream")
    assert edges1 == edges2
    assert edges1[0][1] == "norm_a"  # sorted by parent artifact_id


def test_max_depth_limits_traversal(repo: CatalogRepository) -> None:
    _build_linear_chain(repo)
    nodes, _ = graph.traverse(repo, root_type="integrity", root_id="integ_1", direction="upstream", max_depth=1)
    ids = {n["artifact_id"] for n in nodes}
    assert ids == {"integ_1", "val_1"}  # depth 1 only reaches immediate parent


def test_cycle_detection_rejects_direct_cycle(repo: CatalogRepository) -> None:
    with repo.transaction():
        _add_artifact(repo, "ingestion", "a", 1)
        _add_artifact(repo, "validation", "b", 2)
        repo.insert_edge(parent_type="ingestion", parent_id="a", child_type="validation", child_id="b", relationship="validated_from")
    assert graph.would_create_cycle(repo, parent_type="validation", parent_id="b", child_type="ingestion", child_id="a") is True


def test_cycle_detection_allows_non_cyclic_edge(repo: CatalogRepository) -> None:
    _build_linear_chain(repo)
    assert graph.would_create_cycle(repo, parent_type="integrity", parent_id="integ_1", child_type="ingestion", child_id="ing_1") is True
    assert graph.would_create_cycle(repo, parent_type="ingestion", parent_id="ing_1", child_type="integrity", child_id="integ_1") is False


def test_self_loop_detected_as_cycle(repo: CatalogRepository) -> None:
    with repo.transaction():
        _add_artifact(repo, "ingestion", "a", 1)
    assert graph.would_create_cycle(repo, parent_type="ingestion", parent_id="a", child_type="ingestion", child_id="a") is True


def test_impact_analysis_counts_downstream_by_stage(repo: CatalogRepository) -> None:
    _build_linear_chain(repo)
    affected = graph.impact_analysis(repo, artifact_type="ingestion", artifact_id="ing_1")
    assert affected["validation"] == 1
    assert affected["integrity"] == 1
    assert "ingestion" not in affected


def test_impact_analysis_counts_dataset_versions(repo: CatalogRepository) -> None:
    with repo.transaction():
        _add_artifact(repo, "ingestion", "ing_1", 1)
        _add_artifact(repo, "package", "pkg_1", 9)
        repo.insert_edge(parent_type="ingestion", parent_id="ing_1", child_type="package", child_id="pkg_1", relationship="packaged_from")
        repo.create_dataset(dataset_name="ds1", description=None, metadata_json="{}", created_at="2026-01-01T00:00:00Z")
        repo.create_dataset_version(dataset_name="ds1", version="1.0.0", package_id="pkg_1", description=None, tags_json="[]", status="active", created_at="2026-01-01T00:00:00Z")
    affected = graph.impact_analysis(repo, artifact_type="ingestion", artifact_id="ing_1")
    assert affected["package"] == 1
    assert affected["dataset_versions"] == 1


def test_artifact_parent_list_correct(repo: CatalogRepository) -> None:
    _build_linear_chain(repo)
    parents = repo.get_parents("validation", "val_1")
    assert [(p["parent_artifact_type"], p["parent_artifact_id"]) for p in parents] == [("ingestion", "ing_1")]


def test_artifact_child_list_correct(repo: CatalogRepository) -> None:
    _build_linear_chain(repo)
    children = repo.get_children("ingestion", "ing_1")
    assert [(c["child_artifact_type"], c["child_artifact_id"]) for c in children] == [("validation", "val_1")]
