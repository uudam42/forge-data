"""Shared worker functions for real multiprocess concurrency tests (v2.4).

Every worker below is a plain, picklable, MODULE-LEVEL function that opens
its own sqlite3 connection via app.storage.catalog_store.get_connection --
never a connection, CatalogRepository, or CatalogService instance passed
across a process boundary (which isn't even possible: sqlite3.Connection
objects aren't picklable). Each worker is run inside a real
multiprocessing.Process (a genuine separate OS process with its own
Python interpreter and its own memory space) so these tests exercise
actual multiprocess contention, not a sequential-call simulation of it.

Results are communicated back through a multiprocessing.Queue as plain
(status, payload) string tuples -- never by returning a value from the
worker function itself (which multiprocessing.Process discards) and never
by raising an exception across the process boundary (which is not
supported).
"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from app.catalog.errors import CatalogError
from app.catalog.rebuild_lock import RebuildLock
from app.catalog.repository import CatalogRepository
from app.catalog.scanner import CatalogScanner
from app.catalog.service import CatalogService
from app.catalog.verifier import ArtifactVerifier
from app.core.config import Settings, _default_schema_dir
from app.storage.catalog_store import get_connection

# Real, separate OS processes on every platform this runs on (macOS/Linux
# CI): 'spawn' is already the default on macOS/Windows; forcing it
# everywhere keeps behavior identical across platforms and guarantees a
# worker never accidentally inherits a parent-process sqlite3 connection
# or other unpicklable state via a copy-on-write fork.
CTX = mp.get_context("spawn")
_CTX = CTX


def settings_for(data_root: str, *, busy_timeout_ms: int = 5000) -> Settings:
    root = Path(data_root)
    return Settings(
        RAW_STORAGE_ROOT=root / "raw",
        SCHEMA_DIR=_default_schema_dir(),
        VALIDATION_STORAGE_ROOT=root / "validation",
        INTEGRITY_STORAGE_ROOT=root / "integrity",
        NORMALIZED_STORAGE_ROOT=root / "normalized",
        SYNCHRONIZED_STORAGE_ROOT=root / "synchronized",
        CLEANED_STORAGE_ROOT=root / "cleaned",
        TRANSFORMED_STORAGE_ROOT=root / "transformed",
        QC_STORAGE_ROOT=root / "qc",
        PACKAGE_STORAGE_ROOT=root / "packages",
        CATALOG_DB_PATH=root / "catalog" / "catalog.db",
        CATALOG_BUSY_TIMEOUT_MS=busy_timeout_ms,
    )


def _repo(db_path: str, *, busy_timeout_ms: int = 5000) -> CatalogRepository:
    conn = get_connection(Path(db_path), busy_timeout_ms=busy_timeout_ms)
    return CatalogRepository(conn, db_path=db_path, busy_timeout_ms=busy_timeout_ms)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artifact_record(artifact_type: str, artifact_id: str, *, content_sha256: str = "a" * 64) -> dict:
    return {
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "pipeline_stage": 1,
        "status": "completed",
        "storage_uri": f"data/{artifact_type}/{artifact_id}",
        "content_sha256": content_sha256,
        "manifest_uri": f"data/{artifact_type}/{artifact_id}.manifest.json",
        "manifest_sha256": "b" * 64,
        "created_at": _now(),
        "session_id": None,
        "metadata_json": "{}",
        "registered_at": _now(),
    }


def run_workers(target, arg_tuples: list[tuple], *, timeout: float = 30.0) -> list[tuple[str, str]]:
    """Spawns one real process per entry in arg_tuples (each already
    ending with nothing -- a result queue is appended automatically),
    starts them as close to simultaneously as this platform allows, and
    returns their (status, payload) results (order not guaranteed to
    match input order -- whichever process finishes first reports first).

    Drains the queue with blocking gets BEFORE joining the processes, per
    the multiprocessing docs: a child that has put() an item does not
    fully terminate until that item is flushed to the underlying pipe, so
    joining first can race the parent's read of it (this was an actual
    flaky-test bug caught here during development, not theoretical) or
    even deadlock outright once the pipe buffer fills."""
    queue: mp.Queue = _CTX.Queue()
    procs = []
    for args in arg_tuples:
        p = _CTX.Process(target=target, args=(*args, queue))
        procs.append(p)
    for p in procs:
        p.start()
    results = [queue.get(timeout=timeout) for _ in procs]
    for p in procs:
        p.join(timeout=timeout)
    return results


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def register_artifact_worker(db_path: str, artifact_type: str, artifact_id: str, content_sha256: str, out_queue) -> None:
    try:
        repo = _repo(db_path)
        with repo.transaction(operation="register_artifact"):
            outcome = repo.upsert_artifact(_artifact_record(artifact_type, artifact_id, content_sha256=content_sha256))
        out_queue.put(("ok", outcome))
    except CatalogError as exc:
        out_queue.put((type(exc).__name__, str(exc)))
    except Exception as exc:  # anything un-structured is itself the finding
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


def insert_edge_worker(
    db_path: str, parent_type: str, parent_id: str, child_type: str, child_id: str, out_queue
) -> None:
    try:
        repo = _repo(db_path)
        with repo.transaction(operation="insert_edge"):
            inserted = repo.insert_edge(
                parent_type=parent_type, parent_id=parent_id, child_type=child_type, child_id=child_id, relationship="derived_from"
            )
        out_queue.put(("ok", "inserted" if inserted else "already_existed"))
    except CatalogError as exc:
        out_queue.put((type(exc).__name__, str(exc)))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


def create_dataset_worker(db_path: str, dataset_name: str, out_queue) -> None:
    try:
        repo = _repo(db_path)
        with repo.transaction(operation="create_dataset"):
            created = repo.create_dataset(dataset_name=dataset_name, description=None, metadata_json="{}", created_at=_now())
        out_queue.put(("ok", "created" if created else "already_existed"))
    except CatalogError as exc:
        out_queue.put((type(exc).__name__, str(exc)))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


def register_version_worker(
    db_path: str, dataset_name: str, version: str, package_id: str, out_queue
) -> None:
    """Talks straight to the repository (skips CatalogService's
    package-status validation, which is orthogonal to the race being
    tested) so the test isolates exactly the (dataset_name, version)
    contention behavior."""
    try:
        repo = _repo(db_path)
        with repo.transaction(operation="register_version"):
            outcome = repo.create_dataset_version(
                dataset_name=dataset_name,
                version=version,
                package_id=package_id,
                description=None,
                tags_json="[]",
                status="active",
                created_at=_now(),
            )
        out_queue.put(("ok", outcome))
    except CatalogError as exc:
        out_queue.put((type(exc).__name__, str(exc)))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


def rebuild_worker(data_root: str, busy_timeout_ms: int, out_queue) -> None:
    try:
        settings = settings_for(data_root, busy_timeout_ms=busy_timeout_ms)
        conn = get_connection(settings.CATALOG_DB_PATH, busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS)
        repo = CatalogRepository(conn, db_path=str(settings.CATALOG_DB_PATH), busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS)
        scanner = CatalogScanner(settings)
        verifier = ArtifactVerifier(settings)
        lock = RebuildLock(settings.CATALOG_DB_PATH.parent / "catalog.rebuild.lock")
        service = CatalogService(repo=repo, scanner=scanner, verifier=verifier, rebuild_lock=lock)
        result = service.rebuild()
        out_queue.put(("ok", f"artifacts={result.artifacts_registered} edges={result.edges_registered}"))
    except CatalogError as exc:
        out_queue.put((type(exc).__name__, str(exc)))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


def hold_rebuild_lock_worker(data_root: str, hold_seconds: float, ready_event, out_queue) -> None:
    """Acquires the maintenance rebuild lock directly (not via a full
    rebuild()) and holds it for hold_seconds -- gives a deterministic
    contention window for another process's rebuild()/lock attempt to
    collide with, instead of racing two near-instant rebuilds and hoping
    they overlap."""
    try:
        settings = settings_for(data_root)
        lock = RebuildLock(settings.CATALOG_DB_PATH.parent / "catalog.rebuild.lock")
        with lock.acquire():
            ready_event.set()
            time.sleep(hold_seconds)
        out_queue.put(("ok", "held_and_released"))
    except CatalogError as exc:
        out_queue.put((type(exc).__name__, str(exc)))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


def slow_write_holder_worker(db_path: str, hold_seconds: float, busy_timeout_ms: int, ready_event, out_queue) -> None:
    """Holds a write transaction open for hold_seconds -- used to force a
    real, deterministic lock-contention window for another process to
    collide with, rather than relying on timing luck."""
    try:
        repo = _repo(db_path, busy_timeout_ms=busy_timeout_ms)
        with repo.transaction(operation="slow_write_holder"):
            repo.create_dataset(dataset_name="holder_marker_dataset", description=None, metadata_json="{}", created_at=_now())
            ready_event.set()
            time.sleep(hold_seconds)
        out_queue.put(("ok", "held_and_committed"))
    except CatalogError as exc:
        out_queue.put((type(exc).__name__, str(exc)))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


def crash_mid_write_worker(db_path: str, ready_event) -> None:
    """Opens a write transaction, signals readiness, then hard-exits
    (os._exit -- no cleanup, no atexit, no exception unwinding) to
    simulate a real process crash while holding the SQLite write lock.
    Deliberately does not report through a queue: the whole point is
    that this process disappears without a trace, the way a real crash
    would, and the parent must survive/verify via the database itself."""
    repo = _repo(db_path)
    conn = repo._conn  # noqa: SLF001 -- test-only, simulating a crash needs raw connection access
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO datasets (dataset_name, description, metadata_json, created_at) VALUES (?, ?, ?, ?)",
        ("crash_marker_dataset", None, "{}", _now()),
    )
    ready_event.set()
    time.sleep(0.2)
    os._exit(9)  # SIGKILL-equivalent: no rollback, no __del__, nothing


def busy_timeout_writer_worker(db_path: str, busy_timeout_ms: int, go_event, out_queue) -> None:
    try:
        repo = _repo(db_path, busy_timeout_ms=busy_timeout_ms)
        go_event.wait(timeout=10)
        with repo.transaction(operation="busy_timeout_writer"):
            repo.create_dataset(dataset_name="contended_dataset", description=None, metadata_json="{}", created_at=_now())
        out_queue.put(("ok", "committed"))
    except CatalogError as exc:
        out_queue.put((type(exc).__name__, str(exc)))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


# ---------------------------------------------------------------------------
# v2.5 -- governance / selective-rebuild-lock concurrency workers
# ---------------------------------------------------------------------------


def set_governance_worker(db_path: str, artifact_type: str, artifact_id: str, new_state: str, reason: str, out_queue) -> None:
    """Real multiprocess governance update -- exercises
    CatalogService.set_artifact_governance's read-decide-write-inside-
    one-BEGIN-IMMEDIATE-transaction race safety (see repository.py's
    set_artifact_governance docstring) from a genuinely separate process."""
    try:
        repo = _repo(db_path)
        settings = None  # rebuild lock/executor not needed for a plain governance write
        service = CatalogService(repo=repo, scanner=None, verifier=None, settings=settings)
        result = service.set_artifact_governance(artifact_type, artifact_id, new_state=new_state, reason=reason)
        out_queue.put(("ok", result.state))
    except CatalogError as exc:
        out_queue.put((type(exc).__name__, str(exc)))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


def hold_selective_rebuild_lock_worker(data_root: str, old_type: str, old_id: str, hold_seconds: float, ready_event, out_queue) -> None:
    """Acquires the SAME lock path CatalogService.execute_rebuild() uses
    for a given (old_type, old_id) replacement root, directly -- proves
    the selective-rebuild lock is a real, per-root OS lock (Design
    Requirement 24), without needing two processes to share an in-memory
    plan (which is deliberately process-local; see rebuild_plan_store.py)."""
    try:
        settings = settings_for(data_root)
        lock = RebuildLock(settings.CATALOG_DB_PATH.parent / f"selective_rebuild.{old_type}.{old_id}.lock")
        with lock.acquire():
            ready_event.set()
            time.sleep(hold_seconds)
        out_queue.put(("ok", "held_and_released"))
    except CatalogError as exc:
        out_queue.put((type(exc).__name__, str(exc)))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


# ---------------------------------------------------------------------------
# v2.6 -- pipeline run concurrency workers
# ---------------------------------------------------------------------------


def _run_service_for(data_root: str, *, max_runs: int = 10):
    from app.runs.repository import RunRepository
    from app.runs.service import RunService

    settings = settings_for(data_root)
    settings = settings.model_copy(update={"MAX_LOCAL_PIPELINE_RUNS": max_runs})
    conn = get_connection(settings.CATALOG_DB_PATH, busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS)
    repo = RunRepository(conn, db_path=str(settings.CATALOG_DB_PATH), busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS)
    return RunService(repo=repo, settings=settings)


def _minimal_run_request():
    from app.cleaning.models import CleaningConfig
    from app.packaging.models import PackagingConfig
    from app.qc.models import QCConfig
    from app.runs.models import PipelineCleaningConfig, PipelinePackagingConfig, PipelineQCConfig, PipelineRunRequest, PipelineStreamConfig, PipelineTransformationConfig
    from app.transformation.models import TransformationConfig

    return PipelineRunRequest(
        streams=[PipelineStreamConfig(sensor_type="imu")],
        cleaning=PipelineCleaningConfig(policy_name="default_multimodal", config=CleaningConfig(required_streams=["imu"])),
        transformation=PipelineTransformationConfig(profile_name="multimodal_window_v1", config=TransformationConfig(window={"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True})),
        qc=PipelineQCConfig(profile_name="default_dataset_qc", config=QCConfig(minimum_samples=1)),
        packaging=PipelinePackagingConfig(profile_name="default_ml_package", config=PackagingConfig(split={"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}, grouping={"mode": "source_overlap"})),
    )


def create_run_worker(data_root: str, out_queue) -> None:
    try:
        service = _run_service_for(data_root, max_runs=10)
        run_id = service.create_run(run_type="pipeline", request=_minimal_run_request())
        out_queue.put(("ok", run_id))
    except CatalogError as exc:
        out_queue.put((type(exc).__name__, str(exc)))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


def poll_run_worker(data_root: str, run_id: str, out_queue) -> None:
    try:
        service = _run_service_for(data_root)
        run = service.get_run(run_id)
        out_queue.put(("ok", run.status))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


def cancel_run_worker(data_root: str, run_id: str, out_queue) -> None:
    try:
        service = _run_service_for(data_root)
        result = service.request_cancel(run_id)
        out_queue.put(("ok", result.status))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))


def mark_running_and_completed_worker(data_root: str, run_id: str, out_queue) -> None:
    """Simulates a run's full lifecycle from a real separate process --
    used to build up real run history before a concurrent catalog
    rebuild in the same test."""
    try:
        service = _run_service_for(data_root)
        service.mark_running(run_id, executor_id=f"proc:{os.getpid()}")
        with service._repo.transaction():  # noqa: SLF001 -- test-only direct stage bookkeeping
            service._repo.create_stage_run(stage_run_id=f"stagerun_{run_id}", run_id=run_id, stage="ingestion:imu", status="pending")
            service._repo.update_stage_run(f"stagerun_{run_id}", status="completed", records_processed=1)
            service._repo.record_run_artifact(run_id=run_id, stage="ingestion:imu", artifact_type="ingestion", artifact_id=f"ing_{run_id}", created_at=_now())
        service.mark_completed(run_id)
        out_queue.put(("ok", "completed"))
    except Exception as exc:
        out_queue.put(("UNEXPECTED_" + type(exc).__name__, str(exc)))
