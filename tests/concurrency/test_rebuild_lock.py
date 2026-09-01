"""v2.4 — Demo E/F: the exclusive rebuild lock is a real OS-level flock,
not a "does a lock file exist" check, and it is always released -- on
normal completion, on an exception inside the locked block, and (because
flock is process-scoped) automatically by the kernel if the holder dies."""

from __future__ import annotations

import time

import pytest

from app.catalog.rebuild_lock import RebuildLock

from .helpers import CTX, hold_rebuild_lock_worker, rebuild_worker, settings_for, slow_write_holder_worker

pytestmark = pytest.mark.concurrency


def test_competing_rebuilds_one_lock_owner_one_structured_conflict(tmp_path):
    """Demo E: while one real process holds the rebuild lock, a second
    process's rebuild attempt fails immediately (this project's
    non-blocking, fail-fast policy) with a structured
    CatalogRebuildInProgressError -- never a hang, never silent
    corruption from two rebuilds running at once."""
    data_root = str(tmp_path)
    ready = CTX.Event()
    queue = CTX.Queue()
    holder = CTX.Process(target=hold_rebuild_lock_worker, args=(data_root, 2.0, ready, queue))
    holder.start()
    assert ready.wait(timeout=5), "holder never signaled it acquired the lock"

    second = CTX.Process(target=rebuild_worker, args=(data_root, 5000, queue))
    second.start()
    results = [queue.get(timeout=10)]
    second.join(timeout=10)
    holder.kill()  # release the held lock immediately rather than waiting out its 2s hold
    holder.join(timeout=5)

    statuses = [r[0] for r in results]
    assert statuses.count("CatalogRebuildInProgressError") == 1, results


def test_rebuild_lock_released_after_exception(tmp_path):
    """The lock's release lives in a `finally`, so a rebuild that raises
    partway through must still release it -- verified by successfully
    re-acquiring immediately afterward in the same process."""
    lock = RebuildLock(tmp_path / "catalog.rebuild.lock")
    with pytest.raises(RuntimeError):
        with lock.acquire():
            raise RuntimeError("simulated failure mid-rebuild")

    # If the lock weren't released, this would raise CatalogRebuildInProgressError.
    with lock.acquire():
        pass


def test_rebuild_lock_released_when_holder_process_is_killed(tmp_path):
    """flock is tied to the OS process, not to an in-band 'unlock'
    message -- if the holder is killed outright (SIGKILL, no cleanup
    code runs at all), the kernel releases the lock anyway. A subsequent
    rebuild in a fresh process must succeed, not report a stale
    conflict forever."""
    data_root = str(tmp_path)
    ready = CTX.Event()
    queue = CTX.Queue()
    holder = CTX.Process(target=hold_rebuild_lock_worker, args=(data_root, 30.0, ready, queue))
    holder.start()
    assert ready.wait(timeout=5), "holder never signaled it acquired the lock"

    holder.kill()  # SIGKILL -- no Python cleanup code runs in the child at all
    holder.join(timeout=5)

    second = CTX.Process(target=rebuild_worker, args=(data_root, 5000, queue))
    second.start()
    results = [queue.get(timeout=10)]
    second.join(timeout=10)
    assert results == [("ok", "artifacts=0 edges=0")], results


def test_rebuild_transaction_colliding_with_a_writer_yields_structured_busy(tmp_path):
    """Design Requirement F: rebuild's write transaction (BEGIN IMMEDIATE,
    held for the whole scan) colliding with an in-progress writer must
    produce a structured CatalogBusyError, never a raw sqlite3
    OperationalError or a hang -- verified with a real second process
    deterministically holding the write lock across rebuild's attempt to
    acquire it."""
    data_root = str(tmp_path)
    settings = settings_for(data_root, busy_timeout_ms=300)
    db_path = str(settings.CATALOG_DB_PATH)

    ready = CTX.Event()
    queue = CTX.Queue()
    holder = CTX.Process(target=slow_write_holder_worker, args=(db_path, 2.0, 5000, ready, queue))
    holder.start()
    assert ready.wait(timeout=5), "holder never signaled it started its write transaction"

    rebuilder = CTX.Process(target=rebuild_worker, args=(data_root, 300, queue))
    rebuilder.start()
    results = [queue.get(timeout=10), queue.get(timeout=10)]
    rebuilder.join(timeout=10)
    holder.join(timeout=10)

    statuses = [r[0] for r in results]
    assert "CatalogBusyError" in statuses, results
    assert "ok" in statuses, results  # the holder itself always commits cleanly
    assert not any(s.startswith("UNEXPECTED") for s in statuses), results
