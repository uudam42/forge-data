"""Process-local background execution for pipeline runs (v2.6, Design
Requirements 37/38).

Deliberately NOT a durable job queue: run ownership is tied to whichever
process accepted the `POST /runs` request and started executing it (via
FastAPI `BackgroundTasks` for the live API; directly/synchronously in
tests). If that process crashes, the run's row is left "running" with a
stale heartbeat -- app.runs.recovery.RunRecoveryService (run at startup)
is what reconciles that into a clean "failed" state; there is no
automatic resume (Design Requirement 36) and no worker pool, priority
queue, or retry queue (Design Requirement 38) -- this one class is the
entire local execution mechanism.
"""

from __future__ import annotations

import logging

from app.catalog.repository import CatalogRepository
from app.core.config import Settings
from app.runs.cancellation import CancellationToken
from app.runs.error_adapter import normalize_stage_error
from app.runs.errors import RunCancellationRequested
from app.runs.executor import PipelineExecutionFailed, PipelineRunner, StreamFile
from app.runs.models import PipelineRunRequest
from app.runs.repository import RunRepository
from app.runs.service import RunService, new_executor_id
from app.storage.catalog_store import get_connection

logger = logging.getLogger("app.runs.local_executor")


class LocalRunExecutor:
    """One instance per invocation. Opens its OWN connection (per-
    process/per-call, per v2.4 policy -- never a connection shared across
    threads or reused from the request that created the run) and runs
    entirely synchronously within whatever thread calls `.run()`."""

    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings

    def run(self, run_id: str, request: PipelineRunRequest, stream_files: list[StreamFile]) -> None:
        conn = get_connection(
            self._settings.CATALOG_DB_PATH, busy_timeout_ms=self._settings.CATALOG_BUSY_TIMEOUT_MS, journal_mode=self._settings.CATALOG_JOURNAL_MODE
        )
        run_repo = RunRepository(conn, db_path=str(self._settings.CATALOG_DB_PATH), busy_timeout_ms=self._settings.CATALOG_BUSY_TIMEOUT_MS)
        catalog_repo = CatalogRepository(conn, db_path=str(self._settings.CATALOG_DB_PATH), busy_timeout_ms=self._settings.CATALOG_BUSY_TIMEOUT_MS)
        run_service = RunService(repo=run_repo, settings=self._settings)

        current = run_service.get_run(run_id)
        if current.status == "cancel_requested":
            # Cancelled before this process ever started executing it --
            # never attempt cancel_requested -> running (not a legal
            # transition); go straight to cancelled with nothing run.
            for f in stream_files:
                try:
                    f.stream.close()
                except Exception:
                    pass
            run_service.mark_cancelled(run_id)
            logger.info("RUN_CANCELLED_BEFORE_START run_id=%s", run_id)
            return

        try:
            run_service.mark_running(run_id, executor_id=new_executor_id())
        except Exception:
            logger.exception("RUN_MARK_RUNNING_FAILED run_id=%s", run_id)
            return

        cancellation = CancellationToken(run_repo, run_id, poll_interval_s=self._settings.RUN_HEARTBEAT_INTERVAL_SECONDS)
        runner = PipelineRunner(settings=self._settings, repo=run_repo, catalog_repo=catalog_repo)

        try:
            runner.execute(run_id=run_id, request=request, stream_files=stream_files, cancellation=cancellation)
        except RunCancellationRequested:
            run_service.mark_cancelled(run_id)
            logger.info("RUN_CANCELLED run_id=%s", run_id)
            return
        except PipelineExecutionFailed as exc:
            run_service.mark_failed(run_id, error_code=exc.failure.code, error_message=exc.failure.message)
            logger.info("RUN_FAILED run_id=%s stage=%s code=%s", run_id, exc.failure.stage, exc.failure.code)
            return
        except Exception as exc:
            failure = normalize_stage_error("pipeline", exc)
            run_service.mark_failed(run_id, error_code=failure.code, error_message=failure.message)
            logger.exception("RUN_FAILED_UNEXPECTED run_id=%s", run_id)
            return
        finally:
            for f in stream_files:
                try:
                    f.stream.close()
                except Exception:
                    pass

        try:
            run_service.mark_completed(run_id)
        except Exception:
            logger.exception("RUN_MARK_COMPLETED_FAILED run_id=%s", run_id)
