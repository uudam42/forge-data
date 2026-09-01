"""v2.4 — Demo F: a real process crash (os._exit, no cleanup) while
holding an open write transaction must not corrupt the catalog. SQLite's
WAL recovery discards the uncommitted transaction on the next connection
open; integrity_check and foreign_key_check must both come back clean,
and a subsequent write from a healthy process must succeed normally."""

from __future__ import annotations

import pytest

from app.storage.catalog_store import get_connection

from .helpers import CTX, crash_mid_write_worker, register_artifact_worker

pytestmark = pytest.mark.concurrency


def test_process_crash_mid_write_leaves_catalog_recoverable(tmp_path):
    db_path = str(tmp_path / "catalog" / "catalog.db")

    # A committed baseline artifact, so we can tell "crash discarded the
    # uncommitted write" apart from "the whole database was wiped".
    conn = get_connection(db_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO datasets (dataset_name, description, metadata_json, created_at) VALUES (?, NULL, '{}', 'x')",
        ("baseline_dataset",),
    )
    conn.execute("COMMIT")
    conn.close()

    ready = CTX.Event()
    crasher = CTX.Process(target=crash_mid_write_worker, args=(db_path, ready))
    crasher.start()
    assert ready.wait(timeout=5), "crasher never signaled it started its write transaction"
    crasher.join(timeout=5)
    assert crasher.exitcode == 9, "the crash worker should have hard-exited with os._exit(9)"

    # A fresh connection from a healthy process must be able to open the
    # database, see the committed baseline, NOT see the crashed
    # transaction's uncommitted insert, and pass both integrity checks.
    conn = get_connection(db_path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT COUNT(*) FROM datasets WHERE dataset_name='baseline_dataset'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM datasets WHERE dataset_name='crash_marker_dataset'").fetchone()[0] == 0
    conn.close()

    # And the catalog must still be fully writable afterward -- the crash
    # left no stale lock behind (flock/SQLite locks are released by the
    # kernel when the holding process dies).
    result = CTX.Queue()
    writer = CTX.Process(target=register_artifact_worker, args=(db_path, "ingestion", "ing_post_crash", "d" * 64, result))
    writer.start()
    writer.join(timeout=10)
    assert result.get(timeout=5) == ("ok", "inserted")
