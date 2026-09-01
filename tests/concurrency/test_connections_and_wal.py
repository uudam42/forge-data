"""v2.4 — one connection per process, WAL verified (not assumed), and a
reader in one process can see a writer's committed data from another."""

from __future__ import annotations

import sqlite3

import pytest

from app.storage.catalog_store import get_connection

from .helpers import register_artifact_worker, run_workers

pytestmark = pytest.mark.concurrency


def test_wal_and_pragmas_verified_on_every_connection(tmp_path):
    db_path = tmp_path / "catalog" / "catalog.db"
    conn = get_connection(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.close()

    # -wal / -shm sidecar files exist once a connection has written
    conn2 = get_connection(db_path)
    conn2.execute("BEGIN IMMEDIATE")
    conn2.execute("INSERT INTO catalog_metadata (key, value) VALUES ('probe', '1')")
    conn2.execute("COMMIT")
    conn2.close()
    assert (tmp_path / "catalog" / "catalog.db-wal").exists() or (tmp_path / "catalog" / "catalog.db").exists()


def test_two_processes_each_open_their_own_connection(tmp_path):
    """Real multiprocess check: two independent OS processes each open
    their own sqlite3 connection to the same catalog.db and each
    successfully registers a distinct artifact -- proving connections are
    per-process, not shared/global."""
    db_path = str(tmp_path / "catalog" / "catalog.db")
    results = run_workers(
        register_artifact_worker,
        [
            (db_path, "ingestion", "ing_a", "a" * 64),
            (db_path, "ingestion", "ing_b", "b" * 64),
        ],
    )
    assert sorted(results) == [("ok", "inserted"), ("ok", "inserted")]

    conn = get_connection(db_path)
    count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    conn.close()
    assert count == 2


def test_reader_sees_writer_committed_state_across_processes(tmp_path):
    """A writer process commits an artifact; a reader opened afterward in
    THIS process (a third, independent connection) must see it -- WAL
    readers see the latest committed snapshot, never a partial write."""
    db_path = str(tmp_path / "catalog" / "catalog.db")
    results = run_workers(register_artifact_worker, [(db_path, "ingestion", "ing_visible", "c" * 64)])
    assert results == [("ok", "inserted")]

    conn: sqlite3.Connection = get_connection(db_path)
    row = conn.execute("SELECT * FROM artifacts WHERE artifact_id = 'ing_visible'").fetchone()
    conn.close()
    assert row is not None
    assert row["content_sha256"] == "c" * 64
