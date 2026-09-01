"""Startup run-crash reconciliation (v2.6, Design Requirement 34).

A process that dies mid-run leaves its row `running` (or
`cancel_requested`) forever unless something notices. This scanner runs
once at application startup: any run in one of those two statuses whose
`last_heartbeat_at` is older than `RUN_STALE_HEARTBEAT_SECONDS` is
presumed to have lost its owning process and is marked `failed` with
`RUN_PROCESS_LOST` -- never silently left "running", and never
automatically resumed or retried (that stays the user's explicit
decision -- start a new run).

Deliberately NOT PID-based liveness (a PID can be reused by an unrelated
process after the original exits, which would make a check like "is this
PID still alive" both a false negative and, worse, a false positive) --
purely a heartbeat-staleness check against a monotonically-advancing
wall-clock timestamp the owning process itself was responsible for
refreshing while it was alive.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.runs.errors import RunErrorCode
from app.runs.state_machine import RunStatus

logger = logging.getLogger("app.runs.recovery")


class RunRecoveryService:
    def __init__(self, *, repo, stale_after_seconds: float) -> None:
        self._repo = repo
        self._stale_after_seconds = stale_after_seconds

    def reconcile(self) -> int:
        """Returns the number of runs marked failed. Safe to call
        repeatedly (e.g. once per startup) -- a run already reconciled is
        no longer `running`/`cancel_requested`, so it's never revisited."""
        threshold = (datetime.now(timezone.utc) - timedelta(seconds=self._stale_after_seconds)).isoformat()
        stale = self._repo.find_stale_running_runs(older_than=threshold)
        reconciled = 0
        for row in stale:
            with self._repo.transaction(operation="run_reconcile_stale"):
                # Re-fetch inside the transaction -- a heartbeat may have
                # landed between the SELECT above and acquiring the write
                # lock here, in which case this run is healthy and must
                # be left alone (avoids a race where a slow-but-alive
                # process gets wrongly marked lost).
                current = self._repo.get_run(row["run_id"])
                if current is None or current["status"] not in (RunStatus.RUNNING.value, RunStatus.CANCEL_REQUESTED.value):
                    continue
                if current.get("last_heartbeat_at") is not None and current["last_heartbeat_at"] >= threshold:
                    continue
                self._repo.update_run_status(
                    row["run_id"], status=RunStatus.FAILED.value, finished_at=datetime.now(timezone.utc).isoformat(),
                    error_code=RunErrorCode.RUN_PROCESS_LOST.value,
                    error_message="No heartbeat observed within the configured staleness threshold -- the owning process is presumed lost",
                )
                self._repo.record_event(
                    run_id=row["run_id"], event_type="RUN_FAILED",
                    detail=RunErrorCode.RUN_PROCESS_LOST.value, created_at=datetime.now(timezone.utc).isoformat(),
                )
                reconciled += 1
                logger.warning("RUN_PROCESS_LOST run_id=%s last_heartbeat_at=%s", row["run_id"], current.get("last_heartbeat_at"))
        return reconciled
