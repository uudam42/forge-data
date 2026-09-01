"""v2.5 — real multiprocess tests for governance-update race safety
(Design Requirement 6) and the per-root selective-rebuild lock (Design
Requirement 24)."""

from __future__ import annotations

import pytest

from app.catalog.repository import CatalogRepository
from app.storage.catalog_store import get_connection

from .helpers import CTX, hold_selective_rebuild_lock_worker, run_workers, set_governance_worker

pytestmark = pytest.mark.concurrency


def _seed_artifact(db_path: str, artifact_type: str, artifact_id: str) -> None:
    conn = get_connection(db_path)
    repo = CatalogRepository(conn)
    with repo.transaction():
        repo.upsert_artifact(
            {
                "artifact_type": artifact_type, "artifact_id": artifact_id, "pipeline_stage": 4, "status": "completed",
                "storage_uri": None, "content_sha256": "a" * 64, "manifest_uri": "file:///x", "manifest_sha256": "b" * 64,
                "created_at": "2026-01-01T00:00:00Z", "session_id": None, "metadata_json": "{}",
                "registered_at": "2026-01-01T00:00:00Z",
            }
        )
    conn.close()


def test_concurrent_governance_updates_on_same_artifact_never_lose_an_event(tmp_path):
    """N real processes racing to invalidate the SAME artifact: every
    call must succeed (the read-decide-write happens inside one
    BEGIN IMMEDIATE transaction, serializing them completely -- see
    CatalogRepository.set_artifact_governance), and every one of the N
    attempts must land as its own append-only event -- none silently
    dropped."""
    db_path = str(tmp_path / "catalog" / "catalog.db")
    _seed_artifact(db_path, "normalization", "norm_race")

    results = run_workers(
        set_governance_worker,
        [(db_path, "normalization", "norm_race", "invalid", f"reason from worker {i}") for i in range(6)],
    )
    assert [r[0] for r in results] == ["ok"] * 6, results

    conn = get_connection(db_path)
    repo = CatalogRepository(conn)
    events = repo.list_artifact_governance_events("normalization", "norm_race")
    assert len(events) == 6  # every concurrent call recorded its own event, none lost
    assert all(e["new_state"] == "invalid" for e in events)
    current = repo.get_artifact_governance("normalization", "norm_race")
    assert current["state"] == "invalid"


def test_concurrent_deprecate_and_invalidate_race_resolves_deterministically(tmp_path):
    """A mix of deprecate/invalidate calls racing on the same artifact:
    every call still succeeds and is recorded; the final state is
    whichever call's transaction committed last -- never a corrupted or
    missing current-state row."""
    db_path = str(tmp_path / "catalog" / "catalog.db")
    _seed_artifact(db_path, "normalization", "norm_mixed")

    calls = [(db_path, "normalization", "norm_mixed", state, f"{state} reason") for state in ["deprecated", "invalid", "deprecated", "invalid"]]
    results = run_workers(set_governance_worker, calls)
    assert [r[0] for r in results] == ["ok"] * 4, results

    conn = get_connection(db_path)
    repo = CatalogRepository(conn)
    events = repo.list_artifact_governance_events("normalization", "norm_mixed")
    assert len(events) == 4
    current = repo.get_artifact_governance("normalization", "norm_mixed")
    assert current["state"] in ("deprecated", "invalid")  # deterministically one of the attempted states


def test_selective_rebuild_lock_is_a_real_per_root_process_lock(tmp_path):
    """Demo-E-style: while one real process holds the selective-rebuild
    lock for a given (old_type, old_id) root, a second process's attempt
    to acquire the SAME root's lock fails immediately (non-blocking,
    fail-fast — same policy as the v2.4 catalog-wide rebuild lock)."""
    data_root = str(tmp_path)
    ready = CTX.Event()
    queue = CTX.Queue()
    holder = CTX.Process(target=hold_selective_rebuild_lock_worker, args=(data_root, "normalization", "norm_bad", 6.0, ready, queue))
    holder.start()
    assert ready.wait(timeout=10), "holder never signaled it acquired the lock"

    second_ready = CTX.Event()
    second = CTX.Process(target=hold_selective_rebuild_lock_worker, args=(data_root, "normalization", "norm_bad", 0.1, second_ready, queue))
    second.start()
    results = [queue.get(timeout=10)]
    second.join(timeout=10)
    holder.join(timeout=10)

    assert results[0][0] == "CatalogRebuildInProgressError", results


def test_selective_rebuild_locks_for_different_roots_do_not_contend(tmp_path):
    """A lock held for one replacement root must never block a
    completely different root's rebuild -- these are per-root locks, not
    one global selective-rebuild lock."""
    data_root = str(tmp_path)
    ready = CTX.Event()
    queue = CTX.Queue()
    holder = CTX.Process(target=hold_selective_rebuild_lock_worker, args=(data_root, "normalization", "norm_a", 2.0, ready, queue))
    holder.start()
    assert ready.wait(timeout=5)

    other_ready = CTX.Event()
    other = CTX.Process(target=hold_selective_rebuild_lock_worker, args=(data_root, "normalization", "norm_b", 0.2, other_ready, queue))
    other.start()
    results = [queue.get(timeout=10)]
    other.join(timeout=10)
    holder.join(timeout=10)

    assert results[0] == ("ok", "held_and_released"), results
