"""Shared crash-safety primitive for every per-stage artifact store.

Forge Data v2.1 — Crash Safety & Atomic Artifacts. Full guarantee and
limitation documentation lives in
docs/DETAILED_GUIDE.md#crash-consistency-and-atomic-artifacts; this module
docstring covers only what the code itself does.

INVARIANT: no partially written artifact may ever appear at a finalized
storage location. Every store builds its writes on top of two primitives
in this module:

    create_staging_dir(...)   -- makes a hidden staging directory visible
                                  only to the writer, plus a small
                                  "staging_state.json" run-state journal
    commit_staging_dir(...)   -- fsyncs what was written, then atomically
                                  renames the staging directory into its
                                  final, immutable location

Between those two calls, the caller writes ordinary files into the
staging directory using ordinary file I/O — this module does not wrap
individual writes, since Forge Data's stores already stream large files
directly to disk (never buffering full uploads in memory) and that
behavior must not regress.

Durability model (be precise about what is and isn't guaranteed):
  - ATOMIC VISIBILITY is mandatory and always provided: `os.rename`/
    `Path.rename` on the same filesystem is atomic, so a reader can only
    ever observe the artifact directory in its pre-existing state or its
    fully-committed state, never partially written.
  - Full power-loss DURABILITY (surviving an OS crash / power loss with
    zero data loss) is BEST-EFFORT: this module fsyncs written files, the
    staging directory, and the destination's parent directory, but data
    durability ultimately also depends on the underlying filesystem and
    storage hardware honoring fsync, which this module cannot verify.
    Directory fsync is skipped, not fatal, on platforms/filesystems that
    don't support it (e.g. it silently no-ops on unsupported platforms).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.storage.errors import (
    ArtifactCommitFailedError,
    ArtifactDestinationExistsError,
    StagingCreateFailedError,
)

STAGING_STATE_FILENAME = "staging_state.json"

# Filenames treated as "the manifest" for fsync-ordering purposes: fsynced
# (and, before that, written) last, so that a reader who somehow observed
# a directory before rename would see data before the manifest that
# declares it final -- see docs/DETAILED_GUIDE.md.
_MANIFEST_LIKE_FILENAMES = ("manifest.json", "report.json")


# ---------------------------------------------------------------------------
# Fault injection -- deterministic test hooks, no-op in production.
# ---------------------------------------------------------------------------


class FaultInjector:
    """Named checkpoints a test can hook to force a crash at an exact point
    in the staging/commit lifecycle, instead of relying on real (flaky)
    process kills for every scenario. Production code paths call
    `hit(checkpoint)` unconditionally; with no hook installed this is a
    no-op, so there is no test-only branching in the storage code itself.

    Checkpoints fired by this module's own commit path:
        AFTER_STAGING_CREATED, AFTER_MANIFEST_WRITE, AFTER_DATA_FSYNC,
        AFTER_MANIFEST_FSYNC, BEFORE_RENAME, AFTER_RENAME,
        BEFORE_PARENT_FSYNC
    """

    def __init__(self) -> None:
        self._hooks: dict[str, Callable[[], None]] = {}

    def install(self, checkpoint: str, hook: Callable[[], None]) -> None:
        self._hooks[checkpoint] = hook

    def clear(self, checkpoint: str | None = None) -> None:
        if checkpoint is None:
            self._hooks.clear()
        else:
            self._hooks.pop(checkpoint, None)

    def hit(self, checkpoint: str) -> None:
        hook = self._hooks.get(checkpoint)
        if hook is not None:
            hook()


fault_injector = FaultInjector()


# ---------------------------------------------------------------------------
# fsync helpers -- best-effort, degrade gracefully.
# ---------------------------------------------------------------------------


def fsync_file(path: Path) -> None:
    """Best-effort fsync of a single already-written file.

    Swallows OSError deliberately: fsync support is a filesystem/platform
    property this module cannot control, and durability beyond atomic
    visibility is documented as best-effort, not mandatory.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory's own metadata (its entries).

    Not supported on every platform (notably Windows); failures are
    swallowed for the same reason as `fsync_file`.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _iter_regular_files(root: Path):
    for candidate in sorted(root.rglob("*")):
        if candidate.is_file():
            yield candidate


# ---------------------------------------------------------------------------
# Staging metadata (run-state journal) -- NOT a finalized artifact.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StagingMetadata:
    """Describes an in-progress write. Lives only inside a staging
    directory and is removed before that directory is renamed into its
    final location -- it never becomes part of a finalized artifact, and
    a finalized artifact's own manifest never carries a "running" status
    (see docs/DETAILED_GUIDE.md, "finalized vs. run state")."""

    operation_id: str
    artifact_id: str
    stage: str
    started_at: str
    pid: int
    state: str  # "writing" | "committing"
    final_destination: str


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def write_staging_metadata(staging_dir: Path, metadata: StagingMetadata) -> None:
    _write_json_atomic(staging_dir / STAGING_STATE_FILENAME, asdict(metadata))


def read_staging_metadata(staging_dir: Path) -> StagingMetadata | None:
    """Returns None (never raises) if the metadata file is missing or
    malformed -- callers (the recovery scanner) treat that as its own
    classification, not a crash."""
    path = staging_dir / STAGING_STATE_FILENAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return StagingMetadata(**raw)
    except (json.JSONDecodeError, TypeError, OSError):
        return None


def _mark_state(staging_dir: Path, state: str) -> None:
    existing = read_staging_metadata(staging_dir)
    if existing is None:
        return
    _write_json_atomic(staging_dir / STAGING_STATE_FILENAME, {**asdict(existing), "state": state})


# ---------------------------------------------------------------------------
# Lifecycle: create_staging_dir -> (caller writes files) -> commit_staging_dir
# ---------------------------------------------------------------------------


def create_staging_dir(
    path: Path,
    *,
    operation_id: str,
    artifact_id: str,
    stage: str,
    final_destination: Path | str,
) -> Path:
    """Creates `path` as a fresh staging directory and drops a
    `staging_state.json` run-state journal inside it.

    Raises FileExistsError (uncaught, deliberately) if `path` already
    exists -- an artifact_id collision is a caller bug, not a recoverable
    condition, and every existing store test asserts this exact exception
    type. Raises StagingCreateFailedError only for a genuinely distinct
    failure: the directory was created but its metadata journal could not
    be written.
    """
    path.mkdir(parents=True, exist_ok=False)
    fault_injector.hit("AFTER_STAGING_CREATED")

    metadata = StagingMetadata(
        operation_id=operation_id,
        artifact_id=artifact_id,
        stage=stage,
        started_at=datetime.now(timezone.utc).isoformat(),
        pid=os.getpid(),
        state="writing",
        final_destination=str(final_destination),
    )
    try:
        write_staging_metadata(path, metadata)
    except OSError as exc:
        raise StagingCreateFailedError(f"Failed to write staging metadata under {path}: {exc}") from exc
    return path


def write_manifest_file(staging_dir: Path, filename: str, content: str | bytes) -> Path:
    """Writes a manifest/report file inside a staging directory. A thin
    wrapper (not a new write mechanism) so the AFTER_MANIFEST_WRITE fault
    checkpoint has one call site per store instead of none."""
    target = staging_dir / filename
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    fault_injector.hit("AFTER_MANIFEST_WRITE")
    return target


def commit_staging_dir(
    staging_dir: Path,
    final_dir: Path,
    *,
    fsync_enabled: bool = True,
    verify: Callable[[Path], None] | None = None,
) -> None:
    """Publishes `staging_dir` at `final_dir` — the only way any store in
    this codebase makes a derived artifact visible.

    No partial state is ever observable at `final_dir`: either this
    function raises and `final_dir` never exists, or it returns and
    `final_dir` is exactly what was staged, fully written.

    `verify`, if given, is called with `staging_dir` before anything is
    fsynced or renamed — it should raise (conventionally
    ArtifactChecksumMismatchError) to abort the commit. A verification
    failure leaves `final_dir` unpublished and `staging_dir` intact for
    inspection, exactly like any other exception raised before rename;
    this function never discards on failure, callers do (mirroring every
    store's existing try/except Exception: discard() pattern).
    """
    if final_dir.exists():
        raise ArtifactDestinationExistsError(f"Artifact already exists at {final_dir}")

    # Staging and the final destination aren't always siblings (ingestion
    # stages into a dedicated .staging/ subtree, not next to
    # customer/session/ingestion_id) -- ensure the destination's parent
    # chain exists so rename() has somewhere to land. exist_ok=True: for
    # stores where staging IS a sibling of final_dir, this is already a
    # no-op side effect of creating the staging directory itself.
    final_dir.parent.mkdir(parents=True, exist_ok=True)

    if verify is not None:
        _mark_state(staging_dir, "verifying")
        verify(staging_dir)

    _mark_state(staging_dir, "committing")

    if fsync_enabled:
        manifest_paths = []
        data_paths = []
        for candidate in _iter_regular_files(staging_dir):
            if candidate.parent == staging_dir and candidate.name in _MANIFEST_LIKE_FILENAMES:
                manifest_paths.append(candidate)
            elif candidate.name != STAGING_STATE_FILENAME:
                data_paths.append(candidate)
        for candidate in data_paths:
            fsync_file(candidate)
        fault_injector.hit("AFTER_DATA_FSYNC")
        for candidate in manifest_paths:
            fsync_file(candidate)
        fault_injector.hit("AFTER_MANIFEST_FSYNC")

    # The run-state journal is only meaningful while the artifact is still
    # in staging; a finalized artifact directory carries only the files
    # the stage itself produced (manifest/report/data), never this
    # journal -- see the module docstring's crash-classification note in
    # docs/DETAILED_GUIDE.md for what happens if a crash lands exactly
    # between this line and the rename below.
    (staging_dir / STAGING_STATE_FILENAME).unlink(missing_ok=True)

    if fsync_enabled:
        fsync_dir(staging_dir)

    fault_injector.hit("BEFORE_RENAME")
    try:
        staging_dir.rename(final_dir)
    except OSError as exc:
        raise ArtifactCommitFailedError(f"Failed to publish {staging_dir} to {final_dir}: {exc}") from exc
    fault_injector.hit("AFTER_RENAME")

    fault_injector.hit("BEFORE_PARENT_FSYNC")
    if fsync_enabled:
        fsync_dir(final_dir.parent)


def discard_staging_dir(staging_dir: Path) -> None:
    """Removes a staging directory and everything under it. Safe to call
    on a directory that doesn't exist (no-op) -- mirrors every store's
    existing `discard()` behavior."""
    shutil.rmtree(staging_dir, ignore_errors=True)
