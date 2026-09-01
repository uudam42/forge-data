"""Process-level exclusive lock guarding CatalogService.rebuild().

Rebuild clears and rebuilds the entire artifact index in one long write
transaction (see CatalogScanner.scan()) -- a "maintenance operation" (v2.4
Design Requirement 6), not a routine write, so at most one process may run
it at a time. This is enforced with a real OS-level lock (`fcntl.flock`),
never by checking "does a lock file exist" -- that check is vulnerable to
a stale file left behind by a crashed process, which would wrongly block
every future rebuild forever. `flock` is released automatically by the
kernel if the holding process dies (including on crash/SIGKILL), so no
stale-lock cleanup logic is needed here at all.

Policy (v2.4): non-blocking, fail-immediately. If another process already
holds the lock, this raises CatalogRebuildInProgressError right away
rather than waiting -- simpler to reason about than a bounded wait, and
the caller (an operator or a script) can just retry the rebuild later.
See CATALOG_REBUILD_LOCK_TIMEOUT_MS in app.core.config (0 = immediate).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.catalog.errors import CatalogLockFailedError, CatalogRebuildInProgressError


def _read_holder(fh) -> dict | None:
    """Best-effort read of the lock file's diagnostic metadata. This is
    NEVER used to decide whether the lock is held -- only the OS flock
    result decides that (see module docstring: PIDs can be reused, so
    trusting file contents for correctness would be unsafe). Purely for a
    human/log message answering "who's rebuilding right now"."""
    try:
        fh.seek(0)
        raw = fh.read()
        return json.loads(raw) if raw else None
    except (OSError, ValueError):
        return None


class RebuildLock:
    def __init__(self, lock_path: Path) -> None:
        self._lock_path = Path(lock_path)

    @contextlib.contextmanager
    def acquire(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fh = open(self._lock_path, "a+")
        except OSError as exc:
            raise CatalogLockFailedError(lock_path=str(self._lock_path), reason=str(exc)) from exc

        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                holder = _read_holder(fh)
                raise CatalogRebuildInProgressError(lock_path=str(self._lock_path), holder=holder) from exc
            except OSError as exc:
                raise CatalogLockFailedError(lock_path=str(self._lock_path), reason=str(exc)) from exc

            # Diagnostic-only metadata -- see module docstring.
            fh.seek(0)
            fh.truncate()
            fh.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "hostname": socket.gethostname(),
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "operation_id": uuid.uuid4().hex,
                    }
                )
            )
            fh.flush()
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
