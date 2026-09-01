"""Recovery scanning for staging directories left behind by abnormal
termination (SIGKILL, power loss, OOM, an uncaught Python exception that
somehow escaped a service's own cleanup, ...).

This module is read-only by default: `scan()` only classifies what it
finds. `cleanup_stale()` is a separate, explicit call, and it only ever
removes entries it classified STALE by elapsed time — never entries it
couldn't confidently classify (INVALID_STAGING_ENTRY), and never by
guessing from a PID alone (PIDs can be reused, so a staging entry's
recorded `pid` is informational only, never used to decide liveness —
see docs/DETAILED_GUIDE.md, "stale staging detection").

v2.1 does not attempt record-level resume: a STALE or INVALID entry is
never "continued", only reported and, for STALE entries, optionally
discarded so the stage can be safely rerun from the beginning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.storage.atomic import discard_staging_dir, read_staging_metadata

ACTIVE = "ACTIVE"
STALE = "STALE"
INVALID = "INVALID_STAGING_ENTRY"


@dataclass(frozen=True)
class RecoveryEntry:
    stage: str
    staging_path: str
    operation_id: str | None
    artifact_id: str | None
    started_at: str | None
    classification: str
    reason: str
    size_bytes: int


@dataclass(frozen=True)
class RecoveryScanResult:
    active_count: int
    stale_count: int
    invalid_count: int
    entries: list[RecoveryEntry]


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for candidate in path.rglob("*"):
        if candidate.is_file():
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
    return total


def _staging_roots(settings: Settings) -> dict[str, Path]:
    return {
        "ingestion": settings.RAW_STORAGE_ROOT,
        "validation": settings.VALIDATION_STORAGE_ROOT,
        "integrity": settings.INTEGRITY_STORAGE_ROOT,
        "normalization": settings.NORMALIZED_STORAGE_ROOT,
        "synchronization": settings.SYNCHRONIZED_STORAGE_ROOT,
        "cleaning": settings.CLEANED_STORAGE_ROOT,
        "transformation": settings.TRANSFORMED_STORAGE_ROOT,
        "qc": settings.QC_STORAGE_ROOT,
        "package": settings.PACKAGE_STORAGE_ROOT,
    }


def _find_staging_dirs(root: Path) -> list[Path]:
    """Every leaf staging directory under `root`, regardless of which
    on-disk staging convention a store uses: `root/.staging/<op_id>/`
    (ingestion, validation, integrity) or `root/**/.tmp-<id>/` (the other
    six stores, staged as a sibling of their eventual final directory).
    """
    found: list[Path] = []
    staging_subtree = root / ".staging"
    if staging_subtree.is_dir():
        found.extend(p for p in staging_subtree.iterdir() if p.is_dir())
    if root.is_dir():
        found.extend(p for p in root.rglob(".tmp-*") if p.is_dir())
    return found


class RecoveryService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def scan(self) -> RecoveryScanResult:
        now = datetime.now(timezone.utc)
        entries: list[RecoveryEntry] = []
        for stage, root in _staging_roots(self._settings).items():
            if not root.exists():
                continue
            for staging_dir in sorted(_find_staging_dirs(root)):
                entries.append(self._classify(stage, staging_dir, now))

        return RecoveryScanResult(
            active_count=sum(1 for e in entries if e.classification == ACTIVE),
            stale_count=sum(1 for e in entries if e.classification == STALE),
            invalid_count=sum(1 for e in entries if e.classification == INVALID),
            entries=entries,
        )

    def _classify(self, stage: str, staging_dir: Path, now: datetime) -> RecoveryEntry:
        size_bytes = _dir_size_bytes(staging_dir)
        metadata = read_staging_metadata(staging_dir)
        if metadata is None:
            return RecoveryEntry(
                stage=stage,
                staging_path=str(staging_dir),
                operation_id=None,
                artifact_id=None,
                started_at=None,
                classification=INVALID,
                reason="staging_state.json missing or unparseable",
                size_bytes=size_bytes,
            )

        try:
            started_at = datetime.fromisoformat(metadata.started_at)
        except ValueError:
            return RecoveryEntry(
                stage=stage,
                staging_path=str(staging_dir),
                operation_id=metadata.operation_id,
                artifact_id=metadata.artifact_id,
                started_at=metadata.started_at,
                classification=INVALID,
                reason="staging_state.json has an unparseable started_at",
                size_bytes=size_bytes,
            )

        age_seconds = (now - started_at).total_seconds()
        threshold = self._settings.STALE_STAGING_AFTER_SECONDS
        if age_seconds < threshold:
            classification, reason = ACTIVE, f"started {age_seconds:.0f}s ago, within the {threshold:.0f}s active window"
        else:
            classification, reason = STALE, f"no observed activity for {age_seconds:.0f}s (threshold {threshold:.0f}s)"

        return RecoveryEntry(
            stage=stage,
            staging_path=str(staging_dir),
            operation_id=metadata.operation_id,
            artifact_id=metadata.artifact_id,
            started_at=metadata.started_at,
            classification=classification,
            reason=reason,
            size_bytes=size_bytes,
        )

    def cleanup_stale(self, *, dry_run: bool = False) -> list[RecoveryEntry]:
        """Removes every currently-STALE staging entry (by elapsed time
        since `started_at`). Never touches ACTIVE entries, and never
        touches INVALID entries -- a staging directory this module can't
        confidently date is reported, not guessed at."""
        stale_entries = [e for e in self.scan().entries if e.classification == STALE]
        if not dry_run:
            for entry in stale_entries:
                discard_staging_dir(Path(entry.staging_path))
        return stale_entries
