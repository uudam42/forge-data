"""Structured errors for pipeline runs (v2.6).

Mirrors app.catalog.errors's pattern: an Enum of codes plus a base
exception class per code, so every run-facing error carries a stable
`code` string the API layer maps to an HTTP status.
"""

from __future__ import annotations

from enum import Enum


class RunErrorCode(str, Enum):
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    INVALID_RUN_TRANSITION = "INVALID_RUN_TRANSITION"
    LOCAL_RUN_CAPACITY_EXCEEDED = "LOCAL_RUN_CAPACITY_EXCEEDED"
    RUN_ALREADY_FINISHED = "RUN_ALREADY_FINISHED"
    INVALID_PIPELINE_CONFIG = "INVALID_PIPELINE_CONFIG"
    RUN_CANCELLED = "RUN_CANCELLED"
    RUN_PROCESS_LOST = "RUN_PROCESS_LOST"
    INTERNAL_STAGE_ERROR = "INTERNAL_STAGE_ERROR"


class RunError(Exception):
    """Base class for run-service failures mapped to HTTP by the API layer."""


class RunNotFoundError(RunError):
    pass


class InvalidRunTransitionError(RunError):
    pass


class RunCapacityExceededError(RunError):
    def __init__(self, *, limit: int, current: int) -> None:
        self.limit = limit
        self.current = current
        super().__init__(f"{current} run(s) already active; the configured limit is {limit}")

    def to_dict(self) -> dict:
        return {"code": RunErrorCode.LOCAL_RUN_CAPACITY_EXCEEDED.value, "limit": self.limit, "current": self.current}


class InvalidPipelineConfigError(RunError):
    pass


# ---------------------------------------------------------------------------
# Cooperative cancellation
# ---------------------------------------------------------------------------


class RunCancellationRequested(Exception):
    """Raised by CancellationToken.check() when the owning run's status has
    become cancel_requested -- caught by the pipeline executor at stage
    boundaries (and, for the one deep-instrumented example, inside
    validation's row loop) to unwind cleanly rather than via SIGKILL."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"Run {run_id} cancellation was requested")


# ---------------------------------------------------------------------------
# Structured stage-failure summary (Design Requirement 22/23)
# ---------------------------------------------------------------------------


class StageFailure:
    """A normalized failure summary for one stage, built by mapping
    whatever subsystem-specific exception a stage service raised into a
    common shape -- never a raw traceback (that still goes to logs)."""

    __slots__ = ("stage", "code", "message", "details")

    def __init__(self, *, stage: str, code: str, message: str, details: dict | None = None) -> None:
        self.stage = stage
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"stage": self.stage, "code": self.code, "message": self.message, "details": self.details}
