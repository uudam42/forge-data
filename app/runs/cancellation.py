"""Cooperative cancellation (v2.6, Design Requirements 16/17).

`CancellationToken.check()` is meant to be called often -- at every
pipeline stage boundary, and (for the one deep-instrumented example, see
app.validation.service) inside a hot per-record loop. It is cheap to
call often because it only actually reads the run's status from SQLite
at most once per `poll_interval_s` (a monotonic-clock-throttled read,
same throttling shape as DatabaseProgressReporter's writes) -- every
call in between is a plain in-memory boolean check.

Cancellation is COOPERATIVE: `check()` raises RunCancellationRequested,
a normal Python exception the pipeline executor catches to unwind
cleanly (finishing v2.1's atomic staging/commit machinery on whatever
was in flight, never leaving a partial artifact). This is deliberately
not SIGKILL-based -- SIGKILL remains crash-recovery territory (v2.1/
v2.4/v2.5's fault-injection and real-process-kill tests already cover
that), not the normal cancellation path.
"""

from __future__ import annotations

import time

from app.runs.errors import RunCancellationRequested


class CancellationToken:
    def __init__(self, repo, run_id: str, *, poll_interval_s: float = 0.5) -> None:
        self._repo = repo
        self._run_id = run_id
        self._poll_interval_s = poll_interval_s
        self._last_poll = 0.0
        self._cancelled = False

    def check(self, *, force: bool = False) -> None:
        if self._cancelled:
            raise RunCancellationRequested(self._run_id)
        now = time.monotonic()
        if not force and (now - self._last_poll) < self._poll_interval_s:
            return
        self._last_poll = now
        row = self._repo.get_run(self._run_id)
        if row is not None and row["status"] == "cancel_requested":
            self._cancelled = True
            raise RunCancellationRequested(self._run_id)

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled


class NullCancellationToken:
    """Used for run_type='stage' style executions (or any caller that
    doesn't want cancellation support) -- check() never raises."""

    def check(self, *, force: bool = False) -> None:
        pass

    @property
    def is_cancelled(self) -> bool:
        return False


NULL_CANCELLATION_TOKEN = NullCancellationToken()
