"""Exact duplicate-row detection.

Two rows are duplicates only if their canonical (timestamp + streams)
content is identical — the `alignment` diagnostic block (matched/method/
delta_ms) is deliberately excluded from the identity hash: two rows
describing the same observation are still duplicates regardless of how
they happened to be aligned. Timestamp IS part of the identity — two rows
with identical sensor values but different timestamps are never
duplicates (see canonical_row_key()).

Two interchangeable exact-match backends (v2.2), selected by
`CleaningConfig.duplicate_policy.backend` — both give byte-identical
first-occurrence-retained results, they only differ in where the seen-set
lives:

  - "memory" (default): a plain dict of {content_hash: first_row_index}.
    O(unique_rows) memory — fine for the overwhelming majority of runs,
    and unchanged from v1.0/v2.1 behavior when a caller doesn't opt in.
  - "sqlite": the same seen-set, backed by a temporary on-disk SQLite
    file inside the cleaning run's own staging directory instead of
    process memory — for datasets whose unique-row count would otherwise
    make the in-memory dict too large. Exact semantics are identical;
    this is not an approximation (no Bloom filter — a false positive
    would silently change which rows get dropped, which this project
    will never accept for an exact-dedup guarantee).

Never uses Python's built-in hash() — it is not stable across processes
and would make cleaned output non-reproducible.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.cleaning.rules.base import CleaningRule, DropReason, RuleContext, RuleOutcome
from app.cleaning.rules.common import canonical_json

DUPLICATE_ROW = "DUPLICATE_ROW"

_DEDUP_DB_FILENAME = ".dedup_index.sqlite3"


def canonical_row_key(row: dict) -> str:
    payload = {"timestamp": row.get("timestamp"), "streams": row.get("streams")}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class _SeenIndex(Protocol):
    def first_seen_or_record(self, key: str, row_index: int) -> int | None:
        """Returns the row_index this key was FIRST seen at if it's
        already known; otherwise records row_index as first-seen and
        returns None."""
        ...

    def close(self) -> None: ...


class _InMemorySeenIndex:
    """O(unique_rows) memory — the original, still-default backend."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def first_seen_or_record(self, key: str, row_index: int) -> int | None:
        existing = self._seen.get(key)
        if existing is not None:
            return existing
        self._seen[key] = row_index
        return None

    def close(self) -> None:
        self._seen.clear()


class DedupIndexCreateFailedError(Exception):
    pass


class _SqliteSeenIndex:
    """Same exact semantics as _InMemorySeenIndex, backed by a temporary
    on-disk SQLite file instead of a process-memory dict.

    The file lives inside the caller-supplied `temp_dir` (the cleaning
    run's own v2.1 staging directory) so it is: (a) automatically removed
    by `discard_staging_dir()` if the run fails before `close()` runs,
    and (b) never present at commit time otherwise, since `close()` is
    always called before the staging directory is published — see
    CleaningService._run_cleaning's `finally` block. It never becomes
    part of a finalized artifact and is never visible to catalog scans
    or artifact discovery (it lives and dies entirely within staging).
    """

    def __init__(self, temp_dir: Path) -> None:
        self._db_path = temp_dir / _DEDUP_DB_FILENAME
        try:
            self._conn = sqlite3.connect(str(self._db_path))
            # Throwaway index for the lifetime of one request -- no
            # durability requirement, so trade it for bulk-insert speed.
            self._conn.execute("PRAGMA journal_mode = MEMORY")
            self._conn.execute("PRAGMA synchronous = OFF")
            self._conn.execute("CREATE TABLE seen (key TEXT PRIMARY KEY, first_index INTEGER NOT NULL)")
        except sqlite3.Error as exc:
            raise DedupIndexCreateFailedError(f"Failed to create dedup index at {self._db_path}: {exc}") from exc

    def first_seen_or_record(self, key: str, row_index: int) -> int | None:
        try:
            self._conn.execute("INSERT INTO seen (key, first_index) VALUES (?, ?)", (key, row_index))
            return None
        except sqlite3.IntegrityError:
            cur = self._conn.execute("SELECT first_index FROM seen WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0]

    def close(self) -> None:
        self._conn.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(f"{self._db_path}{suffix}").unlink(missing_ok=True)


@dataclass
class DuplicateRowRule(CleaningRule):
    code: str = DUPLICATE_ROW
    backend: str = "memory"
    temp_dir: Path | None = None
    _index: _SeenIndex = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.backend == "sqlite":
            if self.temp_dir is None:
                raise ValueError("DuplicateRowRule(backend='sqlite') requires temp_dir")
            self._index = _SqliteSeenIndex(self.temp_dir)
        elif self.backend == "memory":
            self._index = _InMemorySeenIndex()
        else:
            raise ValueError(f"Unknown duplicate-detection backend: {self.backend!r}")

    def evaluate(self, row: dict, *, context: RuleContext) -> RuleOutcome:
        key = canonical_row_key(row)
        first_index = self._index.first_seen_or_record(key, context.row_index)
        if first_index is None:
            return RuleOutcome()
        return RuleOutcome(
            drop_reasons=[DropReason(code=self.code, duplicate_of_row_index=first_index)]
        )

    def close(self) -> None:
        self._index.close()
