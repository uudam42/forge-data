"""Run and stage state machines (v2.6, Design Requirements 10/11).

Kept intentionally small: no `paused`/`retrying`/`zombie`/`unknown`. A
retry is always a brand-new run (see `retry_of_run_id`), never a mutation
of a finished one back into `running`.
"""

from __future__ import annotations

from enum import Enum

from app.runs.errors import InvalidRunTransitionError


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


RUN_TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value})

# queued -> running -> {completed, failed, cancel_requested}
# cancel_requested -> {cancelled, completed, failed} -- the race where work
# finishes (successfully or not) before the cancellation is ever observed
# is legal and NOT forced to retroactively become "cancelled" (Design
# Requirement 40: a cancellation never undoes successfully committed work).
_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCEL_REQUESTED}),
    RunStatus.RUNNING: frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCEL_REQUESTED}),
    RunStatus.CANCEL_REQUESTED: frozenset({RunStatus.CANCELLED, RunStatus.COMPLETED, RunStatus.FAILED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def validate_run_transition(previous: str, new: str) -> None:
    try:
        prev_status = RunStatus(previous)
        new_status = RunStatus(new)
    except ValueError as exc:
        raise InvalidRunTransitionError(f"Unknown run status in transition {previous!r} -> {new!r}") from exc
    if new_status not in _RUN_TRANSITIONS[prev_status]:
        raise InvalidRunTransitionError(f"{previous} -> {new} is not an allowed run transition")


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


_STAGE_TRANSITIONS: dict[StageStatus, frozenset[StageStatus]] = {
    StageStatus.PENDING: frozenset({StageStatus.RUNNING, StageStatus.SKIPPED, StageStatus.CANCELLED}),
    StageStatus.RUNNING: frozenset({StageStatus.COMPLETED, StageStatus.FAILED, StageStatus.CANCELLED}),
    StageStatus.COMPLETED: frozenset(),
    StageStatus.FAILED: frozenset(),
    StageStatus.SKIPPED: frozenset(),
    StageStatus.CANCELLED: frozenset(),
}


def validate_stage_transition(previous: str, new: str) -> None:
    try:
        prev_status = StageStatus(previous)
        new_status = StageStatus(new)
    except ValueError as exc:
        raise InvalidRunTransitionError(f"Unknown stage status in transition {previous!r} -> {new!r}") from exc
    if new_status not in _STAGE_TRANSITIONS[prev_status]:
        raise InvalidRunTransitionError(f"{previous} -> {new} is not an allowed stage transition")
