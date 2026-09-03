"""Unit tests for SelectiveRebuildPlanner: topological ordering,
multi-parent selective reuse, compatibility checks, and the plan
fingerprint (Design Requirements 12-17, 23)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.catalog.errors import ArtifactNotFoundError, RebuildReplacementIncompatibleError
from app.catalog.rebuild_planner import SelectiveRebuildPlanner
from app.catalog.repository import CatalogRepository
from app.catalog.serialization import canonical_json
from app.storage.catalog_store import get_connection


@pytest.fixture
def repo(tmp_path: Path) -> CatalogRepository:
    conn = get_connection(tmp_path / "catalog.db")
    return CatalogRepository(conn)


def _add(repo: CatalogRepository, artifact_type: str, artifact_id: str, stage: int, *, metadata: dict | None = None, session_id: str | None = None) -> None:
    repo.upsert_artifact(
        {
            "artifact_type": artifact_type, "artifact_id": artifact_id, "pipeline_stage": stage, "status": "completed",
            "storage_uri": None, "content_sha256": artifact_id, "manifest_uri": f"file:///{artifact_id}",
            "manifest_sha256": f"sha_{artifact_id}", "created_at": "2026-01-01T00:00:00Z", "session_id": session_id,
            "metadata_json": canonical_json(metadata or {}), "registered_at": "2026-01-01T00:00:00Z",
        }
    )


def _build_multiparent_dag(repo: CatalogRepository) -> None:
    """imu_norm, gps_norm -> sync -> clean -> xform -> {qc, package}, package also <- qc."""
    with repo.transaction():
        _add(repo, "normalization", "imu_norm_old", 4, metadata={"schema": {"name": "imu"}, "ingestion_id": "ing_imu"}, session_id="sess_1")
        _add(repo, "normalization", "imu_norm_new", 4, metadata={"schema": {"name": "imu"}, "ingestion_id": "ing_imu"}, session_id="sess_1")
        _add(repo, "normalization", "gps_norm", 4, metadata={"schema": {"name": "gps"}, "ingestion_id": "ing_gps"}, session_id="sess_1")
        _add(repo, "synchronization", "sync_old", 5, session_id="sess_1")
        _add(repo, "cleaning", "clean_old", 6, session_id="sess_1")
        _add(repo, "transformation", "xform_old", 7, session_id="sess_1")
        _add(repo, "qc", "qc_old", 8, session_id="sess_1")
        _add(repo, "package", "pkg_old", 9, session_id="sess_1")
        repo.insert_edge(parent_type="normalization", parent_id="imu_norm_old", child_type="synchronization", child_id="sync_old", relationship="synchronized_from")
        repo.insert_edge(parent_type="normalization", parent_id="gps_norm", child_type="synchronization", child_id="sync_old", relationship="synchronized_from")
        repo.insert_edge(parent_type="synchronization", parent_id="sync_old", child_type="cleaning", child_id="clean_old", relationship="cleaned_from")
        repo.insert_edge(parent_type="cleaning", parent_id="clean_old", child_type="transformation", child_id="xform_old", relationship="transformed_from")
        repo.insert_edge(parent_type="transformation", parent_id="xform_old", child_type="qc", child_id="qc_old", relationship="qc_of")
        repo.insert_edge(parent_type="transformation", parent_id="xform_old", child_type="package", child_id="pkg_old", relationship="packaged_from")
        repo.insert_edge(parent_type="qc", parent_id="qc_old", child_type="package", child_id="pkg_old", relationship="approved_by_qc")


def test_simple_chain_plan(repo: CatalogRepository) -> None:
    with repo.transaction():
        _add(repo, "normalization", "n_old", 4)
        _add(repo, "normalization", "n_new", 4)
        _add(repo, "synchronization", "s_old", 5)
        repo.insert_edge(parent_type="normalization", parent_id="n_old", child_type="synchronization", child_id="s_old", relationship="synchronized_from")

    plan = SelectiveRebuildPlanner(repo).build_plan(old_type="normalization", old_id="n_old", new_type="normalization", new_id="n_new")
    assert [s.stage_artifact_type for s in plan.steps] == ["synchronization"]
    assert plan.steps[0].old_artifact_id == "s_old"
    assert plan.fingerprint  # non-empty


def test_multiparent_dag_topological_order_and_selective_reuse(repo: CatalogRepository) -> None:
    _build_multiparent_dag(repo)
    plan = SelectiveRebuildPlanner(repo).build_plan(old_type="normalization", old_id="imu_norm_old", new_type="normalization", new_id="imu_norm_new")

    stages = [s.stage_artifact_type for s in plan.steps]
    assert stages == ["synchronization", "cleaning", "transformation", "qc", "package"]  # true topological order, not alphabetical

    sync_step = plan.steps[0]
    gps_parent = next(p for p in sync_step.parents if p.original_id == "gps_norm")
    imu_parent = next(p for p in sync_step.parents if p.original_id == "imu_norm_old")
    assert gps_parent.replaced is False and gps_parent.effective_id == "gps_norm"  # untouched sibling reused
    assert imu_parent.replaced is True and imu_parent.effective_id is None

    package_step = plan.steps[-1]
    assert {p.artifact_type for p in package_step.parents} == {"transformation", "qc"}
    assert all(p.replaced for p in package_step.parents)  # both transformation and qc are downstream of the replaced normalization


def test_replacement_anchor_before_normalization_is_rejected_at_plan_time(repo: CatalogRepository) -> None:
    """Release-hardening regression: app.catalog.rebuild_executor has no
    execution path for validation/integrity/ingestion stages, so a plan
    anchored earlier than 'normalization' can never actually execute --
    it used to build "successfully" (every step marked feasible=true)
    only to fail at execute time on step one with a confusing "no
    rebuild executor is defined" error. Now rejected up front, at plan
    time, with an actionable message."""
    with repo.transaction():
        _add(repo, "ingestion", "ing_old", 1, session_id="s1")
        _add(repo, "ingestion", "ing_new", 1, session_id="s1")
    with pytest.raises(RebuildReplacementIncompatibleError, match="does not support replacing a 'ingestion'"):
        SelectiveRebuildPlanner(repo).build_plan(old_type="ingestion", old_id="ing_old", new_type="ingestion", new_id="ing_new")


def test_incompatible_replacement_different_type(repo: CatalogRepository) -> None:
    with repo.transaction():
        _add(repo, "normalization", "n_old", 4)
        _add(repo, "synchronization", "not_a_normalization", 5)
    with pytest.raises(RebuildReplacementIncompatibleError):
        SelectiveRebuildPlanner(repo).build_plan(old_type="normalization", old_id="n_old", new_type="synchronization", new_id="not_a_normalization")


def test_incompatible_replacement_different_schema(repo: CatalogRepository) -> None:
    with repo.transaction():
        _add(repo, "normalization", "imu_old", 4, metadata={"schema": {"name": "imu"}, "ingestion_id": "ing_1"}, session_id="s1")
        _add(repo, "normalization", "gps_new", 4, metadata={"schema": {"name": "gps"}, "ingestion_id": "ing_2"}, session_id="s1")
    with pytest.raises(RebuildReplacementIncompatibleError, match="schema mismatch"):
        SelectiveRebuildPlanner(repo).build_plan(old_type="normalization", old_id="imu_old", new_type="normalization", new_id="gps_new")


def test_incompatible_replacement_different_session(repo: CatalogRepository) -> None:
    with repo.transaction():
        _add(repo, "normalization", "n_old", 4, session_id="sess_a")
        _add(repo, "normalization", "n_new", 4, session_id="sess_b")
    with pytest.raises(RebuildReplacementIncompatibleError, match="session_id"):
        SelectiveRebuildPlanner(repo).build_plan(old_type="normalization", old_id="n_old", new_type="normalization", new_id="n_new")


def test_missing_replacement_artifact_raises_not_found(repo: CatalogRepository) -> None:
    with repo.transaction():
        _add(repo, "normalization", "n_old", 4)
    with pytest.raises(ArtifactNotFoundError):
        SelectiveRebuildPlanner(repo).build_plan(old_type="normalization", old_id="n_old", new_type="normalization", new_id="n_never_registered")


def test_manual_configuration_required_flags(repo: CatalogRepository) -> None:
    _build_multiparent_dag(repo)
    plan = SelectiveRebuildPlanner(repo).build_plan(old_type="normalization", old_id="imu_norm_old", new_type="normalization", new_id="imu_norm_new")
    by_stage = {s.stage_artifact_type: s for s in plan.steps}
    assert by_stage["synchronization"].manual_configuration_required is False
    assert by_stage["synchronization"].infeasible_reason is None
    for stage in ("cleaning", "transformation", "qc", "package"):
        assert by_stage[stage].manual_configuration_required is True
        assert by_stage[stage].infeasible_reason is not None


def test_fingerprint_is_deterministic_and_changes_with_catalog_state(repo: CatalogRepository) -> None:
    with repo.transaction():
        _add(repo, "normalization", "n_old", 4)
        _add(repo, "normalization", "n_new", 4)
        _add(repo, "synchronization", "s_old", 5)
        repo.insert_edge(parent_type="normalization", parent_id="n_old", child_type="synchronization", child_id="s_old", relationship="synchronized_from")

    planner = SelectiveRebuildPlanner(repo)
    fp1 = planner.build_plan(old_type="normalization", old_id="n_old", new_type="normalization", new_id="n_new").fingerprint
    fp2 = planner.build_plan(old_type="normalization", old_id="n_old", new_type="normalization", new_id="n_new").fingerprint
    assert fp1 == fp2  # deterministic given unchanged catalog state

    # A new descendant appearing changes the affected DAG -> fingerprint must change.
    with repo.transaction():
        _add(repo, "cleaning", "c_new", 6)
        repo.insert_edge(parent_type="synchronization", parent_id="s_old", child_type="cleaning", child_id="c_new", relationship="cleaned_from")
    fp3 = planner.build_plan(old_type="normalization", old_id="n_old", new_type="normalization", new_id="n_new").fingerprint
    assert fp3 != fp1
