"""Progress reporting abstraction (v2.6, Design Requirements 12-15, 52-54).

Pure stages/services never need to know SQLite exists: they're handed a
ProgressReporter (a NoOpProgressReporter by default -- see Design
Requirement 53's "zero overhead for legacy callers") and call its small
interface. Run-aware execution swaps in a DatabaseProgressReporter,
which batches/throttles the actual SQLite writes by wall-clock time
(never by record count, so behavior doesn't depend on per-record cost)
using `time.monotonic()` -- never wall-clock `datetime.now()`, which can
jump backward/forward and would make throttling unreliable.

Progress is honest: a stage that doesn't know its total record count in
advance (Design Requirement 54 -- never scan a file twice just to learn
its size first) reports indeterminate progress (records_processed only,
records_total stays None); `progress_fraction` in the API response is
computed only when both numbers are actually known, never fabricated
from stage position.
"""

from __future__ import annotations

import time
from typing import Protocol


class ProgressReporter(Protocol):
    def start_stage(self, *, records_total: int | None = None, bytes_total: int | None = None) -> None: ...

    def update(self, *, records_delta: int = 0, bytes_delta: int = 0) -> None: ...

    def complete_stage(self, *, records_processed: int | None = None, bytes_processed: int | None = None, artifacts_created: int = 0) -> None: ...

    def fail_stage(self, *, error_code: str, error_message: str) -> None: ...

    def skip_stage(self) -> None: ...

    def cancel_stage(self) -> None: ...


class NoOpProgressReporter:
    """The default for every legacy, non-run-aware stage invocation --
    every method is a no-op, so the existing single-stage APIs incur
    zero SQLite overhead from this abstraction ever existing."""

    def start_stage(self, *, records_total: int | None = None, bytes_total: int | None = None) -> None:
        pass

    def update(self, *, records_delta: int = 0, bytes_delta: int = 0) -> None:
        pass

    def complete_stage(self, *, records_processed: int | None = None, bytes_processed: int | None = None, artifacts_created: int = 0) -> None:
        pass

    def fail_stage(self, *, error_code: str, error_message: str) -> None:
        pass

    def skip_stage(self) -> None:
        pass

    def cancel_stage(self) -> None:
        pass


NOOP_PROGRESS_REPORTER = NoOpProgressReporter()


class DatabaseProgressReporter:
    """Backs one StageRun row. `update()` accumulates records/bytes deltas
    in memory and only issues a real SQLite UPDATE when
    `flush_interval_s` has elapsed since the last flush (Design
    Requirement 15) -- never on every call, regardless of how often a hot
    loop calls `update()`. The accumulated total is always flushed
    unconditionally on start/complete/fail/skip/cancel, so the final
    persisted numbers are always exact even though intermediate polling
    reads may lag slightly behind the true in-memory count."""

    def __init__(self, repo, stage_run_id: str, *, flush_interval_s: float = 0.5) -> None:
        self._repo = repo
        self._stage_run_id = stage_run_id
        self._flush_interval_s = flush_interval_s
        self._records_processed = 0
        self._bytes_processed = 0
        self._last_flush = 0.0

    def start_stage(self, *, records_total: int | None = None, bytes_total: int | None = None) -> None:
        with self._repo.transaction(operation="stage_progress_start"):
            self._repo.update_stage_run(
                self._stage_run_id, status="running", started_at=_now(), records_total=records_total, bytes_total=bytes_total,
            )
        self._last_flush = time.monotonic()

    def update(self, *, records_delta: int = 0, bytes_delta: int = 0) -> None:
        self._records_processed += records_delta
        self._bytes_processed += bytes_delta
        now = time.monotonic()
        if (now - self._last_flush) < self._flush_interval_s:
            return
        self._flush()
        self._last_flush = now

    def _flush(self) -> None:
        with self._repo.transaction(operation="stage_progress_update"):
            self._repo.update_stage_run(
                self._stage_run_id, records_processed=self._records_processed, bytes_processed=self._bytes_processed,
            )

    def complete_stage(self, *, records_processed: int | None = None, bytes_processed: int | None = None, artifacts_created: int = 0) -> None:
        if records_processed is not None:
            self._records_processed = records_processed
        if bytes_processed is not None:
            self._bytes_processed = bytes_processed
        with self._repo.transaction(operation="stage_progress_complete"):
            self._repo.update_stage_run(
                self._stage_run_id, status="completed", finished_at=_now(),
                records_processed=self._records_processed, bytes_processed=self._bytes_processed,
                artifacts_created=artifacts_created,
            )

    def fail_stage(self, *, error_code: str, error_message: str) -> None:
        with self._repo.transaction(operation="stage_progress_fail"):
            self._repo.update_stage_run(
                self._stage_run_id, status="failed", finished_at=_now(),
                records_processed=self._records_processed, bytes_processed=self._bytes_processed,
                error_code=error_code, error_message=error_message,
            )

    def skip_stage(self) -> None:
        with self._repo.transaction(operation="stage_progress_skip"):
            self._repo.update_stage_run(self._stage_run_id, status="skipped")

    def cancel_stage(self) -> None:
        with self._repo.transaction(operation="stage_progress_cancel"):
            self._repo.update_stage_run(self._stage_run_id, status="cancelled", finished_at=_now())


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
