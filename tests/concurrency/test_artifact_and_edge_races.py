"""v2.4 — concurrent artifact/edge registration races resolved by DB
constraints as final authority, never a raw sqlite3.IntegrityError."""

from __future__ import annotations

import pytest

from app.storage.catalog_store import get_connection

from .helpers import insert_edge_worker, register_artifact_worker, run_workers

pytestmark = pytest.mark.concurrency


def test_four_processes_register_distinct_artifacts_concurrently(tmp_path):
    db_path = str(tmp_path / "catalog" / "catalog.db")
    results = run_workers(
        register_artifact_worker,
        [(db_path, "ingestion", f"ing_{i}", f"{i}" * 64) for i in range(4)],
    )
    assert results == [("ok", "inserted")] * 4

    conn = get_connection(db_path)
    assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 4
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_same_artifact_same_content_race_is_idempotent(tmp_path):
    """N processes racing to register the IDENTICAL artifact (same id,
    same content) must all succeed -- exactly one "inserted", the rest
    "unchanged" -- never an uncaught IntegrityError."""
    db_path = str(tmp_path / "catalog" / "catalog.db")
    results = run_workers(
        register_artifact_worker,
        [(db_path, "ingestion", "ing_shared", "same" * 16) for _ in range(6)],
    )
    statuses = [r[0] for r in results]
    assert statuses == ["ok"] * 6, results
    outcomes = [r[1] for r in results]
    assert outcomes.count("inserted") == 1, outcomes
    assert outcomes.count("unchanged") == 5, outcomes

    conn = get_connection(db_path)
    assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    conn.close()


def test_same_artifact_different_content_race_is_a_structured_conflict(tmp_path):
    """N processes racing to register the SAME artifact_id with
    DIFFERENT content must resolve to exactly one winner and the rest a
    structured ArtifactRegistryConflictError -- never a raw
    sqlite3.IntegrityError leaking out of the repository layer."""
    db_path = str(tmp_path / "catalog" / "catalog.db")
    results = run_workers(
        register_artifact_worker,
        [(db_path, "ingestion", "ing_conflict", f"content_{i}".ljust(64, "0")) for i in range(5)],
    )
    statuses = [r[0] for r in results]
    assert statuses.count("ok") == 1, results
    assert statuses.count("ArtifactRegistryConflictError") == 4, results
    assert "UNEXPECTED" not in "".join(statuses)

    conn = get_connection(db_path)
    assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE artifact_id = 'ing_conflict'").fetchone()[0] == 1
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_concurrent_identical_edge_registration_is_idempotent(tmp_path):
    """Two artifacts are pre-registered sequentially; N processes then
    race to insert the SAME lineage edge between them -- exactly one
    "inserted", the rest "already_existed", never a raw PK violation."""
    db_path = str(tmp_path / "catalog" / "catalog.db")
    seed = run_workers(
        register_artifact_worker,
        [(db_path, "ingestion", "ing_p", "p" * 64), (db_path, "validation", "val_c", "c" * 64)],
    )
    assert seed == [("ok", "inserted")] * 2

    results = run_workers(
        insert_edge_worker,
        [(db_path, "ingestion", "ing_p", "validation", "val_c") for _ in range(5)],
    )
    statuses = [r[0] for r in results]
    assert statuses == ["ok"] * 5, results
    outcomes = [r[1] for r in results]
    assert outcomes.count("inserted") == 1, outcomes
    assert outcomes.count("already_existed") == 4, outcomes

    conn = get_connection(db_path)
    assert conn.execute("SELECT COUNT(*) FROM lineage_edges").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
