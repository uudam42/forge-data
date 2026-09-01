"""Run-level error normalization (v2.6, Design Requirement 23).

Every stage subsystem already raises its own well-named, specific
exception classes -- some rooted at one common base in that stage's
`service.py` (e.g. `CleaningError`), others defined independently in a
`registry.py`/`profiles/base.py`/etc (e.g. `CleaningPolicyNotFoundError`,
which does NOT inherit `CleaningError` -- a real, pre-existing quirk
across several stages). Rather than hand-enumerating every one of those
across 9 stages (fragile, and silently incomplete for exception classes
added later), this adapter classifies by MODULE: any exception whose
class lives anywhere under that stage's own package (`app.cleaning.*`,
`app.qc.*`, ...) is treated as a recognized, well-typed domain error and
reported using its own class name as the code. Anything else --
genuinely unexpected, or raised from outside that package entirely --
becomes INTERNAL_STAGE_ERROR with a safe, generic user-facing message;
the real exception type/message is still recorded in `details` and
logged in full via `logger.exception()` at the call site.
"""

from __future__ import annotations

from app.runs.errors import RunErrorCode, StageFailure

# Maps a pipeline stage name (as used in pipeline_stage_runs.stage,
# without any ":sensor_type" qualifier) to the Python package that
# stage's own code lives under.
_STAGE_PACKAGE_PREFIX: dict[str, str] = {
    "ingestion": "app.ingestion.",
    "validation": "app.validation.",
    "integrity": "app.integrity.",
    "normalization": "app.normalization.",
    "synchronization": "app.synchronization.",
    "cleaning": "app.cleaning.",
    "transformation": "app.transformation.",
    "qc": "app.qc.",
    "package": "app.packaging.",
}


def normalize_stage_error(stage: str, exc: Exception) -> StageFailure:
    base_stage = stage.split(":", 1)[0]  # "ingestion:imu" -> "ingestion"
    prefix = _STAGE_PACKAGE_PREFIX.get(base_stage)
    module = type(exc).__module__ or ""
    if prefix is not None and module.startswith(prefix):
        return StageFailure(stage=stage, code=type(exc).__name__, message=str(exc) or type(exc).__name__)
    return StageFailure(
        stage=stage,
        code=RunErrorCode.INTERNAL_STAGE_ERROR.value,
        message="An internal error occurred while executing this stage",
        details={"exception_type": type(exc).__name__},
    )
