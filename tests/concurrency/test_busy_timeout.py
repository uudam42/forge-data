"""v2.4 — busy handling is bounded: a writer waits up to its configured
busy_timeout, then either succeeds (the lock freed in time) or gets a
structured CatalogBusyError -- never an infinite wait, never a raw
sqlite3.OperationalError leaking past the repository layer."""

from __future__ import annotations

import pytest

from .helpers import CTX, busy_timeout_writer_worker, settings_for, slow_write_holder_worker

pytestmark = pytest.mark.concurrency


def test_writer_waits_then_succeeds_once_the_lock_frees(tmp_path):
    """busy_timeout (2s) comfortably exceeds the holder's hold time
    (0.5s), so the waiting writer must block and then succeed -- not
    fail -- once the holder commits and releases the write lock."""
    settings = settings_for(str(tmp_path), busy_timeout_ms=2000)
    db_path = str(settings.CATALOG_DB_PATH)

    ready = CTX.Event()
    queue = CTX.Queue()
    holder = CTX.Process(target=slow_write_holder_worker, args=(db_path, 0.5, 2000, ready, queue))
    holder.start()
    assert ready.wait(timeout=5)

    go = CTX.Event()
    go.set()  # attempt immediately, while the holder still has the lock
    writer = CTX.Process(target=busy_timeout_writer_worker, args=(db_path, 2000, go, queue))
    writer.start()
    # Blocking gets BEFORE join -- see helpers.run_workers docstring for
    # why draining after join() can race or deadlock.
    results = [queue.get(timeout=10), queue.get(timeout=10)]
    writer.join(timeout=10)
    holder.join(timeout=10)

    assert results.count(("ok", "held_and_committed")) == 1, results
    assert results.count(("ok", "committed")) == 1, results


def test_writer_exceeds_busy_timeout_gets_structured_catalog_busy_error(tmp_path):
    """busy_timeout (150ms) is far shorter than the holder's hold time
    (2s), so the waiting writer must give up and raise a structured
    CatalogBusyError carrying operation/timeout_ms/db_path -- never hang,
    never a raw 'database is locked' OperationalError."""
    settings = settings_for(str(tmp_path), busy_timeout_ms=150)
    db_path = str(settings.CATALOG_DB_PATH)

    ready = CTX.Event()
    queue = CTX.Queue()
    holder = CTX.Process(target=slow_write_holder_worker, args=(db_path, 2.0, 5000, ready, queue))
    holder.start()
    assert ready.wait(timeout=5)

    go = CTX.Event()
    go.set()
    writer = CTX.Process(target=busy_timeout_writer_worker, args=(db_path, 150, go, queue))
    writer.start()
    results = [queue.get(timeout=10), queue.get(timeout=10)]
    writer.join(timeout=10)
    holder.join(timeout=10)

    statuses = [r[0] for r in results]
    assert "CatalogBusyError" in statuses, results
    assert not any(s.startswith("UNEXPECTED") for s in statuses), results
    # the busy error must carry the structured fields, not just a message
    busy_payload = next(r[1] for r in results if r[0] == "CatalogBusyError")
    assert "150" in busy_payload
    assert "busy_timeout_writer" in busy_payload
