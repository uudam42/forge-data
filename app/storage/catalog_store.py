"""SQLite connection and schema management for the Step 10 metadata
catalog.

This is deliberately the FIRST stage in the project to use a database.
Every prior stage's manifests/reports on the filesystem remain the source
of truth — this catalog is an INDEX over them, built for queries that
would otherwise require repeated full-filesystem scans ("find every
package derived from ingestion X", "show every version of dataset Y").
Deleting `catalog.db` and running a rebuild must fully restore artifact
and lineage-edge state from the filesystem; only `datasets` and
`dataset_versions` (user-registered, not reconstructible from manifests)
are preserved across an artifact-index rebuild — see
app.catalog.repository.CatalogRepository.rebuild_artifact_index.

Uses stdlib `sqlite3` only — no ORM — to avoid unnecessary dependency
weight for an MVP metadata index. Foreign keys are enabled explicitly
(off by default in SQLite).

Concurrency model (v2.4): this remains local-first, no distributed
locking, no cross-machine coordination — but it is explicitly
MULTIPROCESS-SAFE for concurrent local processes sharing one
`catalog.db` (multiple `uvicorn` workers, concurrent pipeline requests,
independent scripts). Every process opens its OWN connection (never
shared across processes, never a long-lived module-global); every
connection gets WAL journaling (verified, not assumed — see
`get_connection()`) and a bounded busy timeout, so SQLite's own reader/
writer semantics handle concurrent access predictably instead of a raw
"database is locked" exception ever reaching a caller. See
docs/DETAILED_GUIDE.md#multiprocess-concurrency-model-v24.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_JOURNAL_MODE = "WAL"

CATALOG_SCHEMA_VERSION = "1.0.0"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_type   TEXT NOT NULL,
    artifact_id     TEXT NOT NULL,
    pipeline_stage  INTEGER NOT NULL,
    status          TEXT,
    storage_uri     TEXT,
    content_sha256  TEXT,
    manifest_uri    TEXT,
    manifest_sha256 TEXT,
    created_at      TEXT,
    session_id      TEXT,
    metadata_json   TEXT NOT NULL,
    registered_at   TEXT NOT NULL,
    PRIMARY KEY (artifact_type, artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_stage ON artifacts(pipeline_stage);
CREATE INDEX IF NOT EXISTS idx_artifacts_status ON artifacts(status);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id);

CREATE TABLE IF NOT EXISTS lineage_edges (
    parent_artifact_type TEXT NOT NULL,
    parent_artifact_id   TEXT NOT NULL,
    child_artifact_type  TEXT NOT NULL,
    child_artifact_id    TEXT NOT NULL,
    relationship         TEXT NOT NULL,
    PRIMARY KEY (parent_artifact_type, parent_artifact_id, child_artifact_type, child_artifact_id, relationship),
    FOREIGN KEY (parent_artifact_type, parent_artifact_id) REFERENCES artifacts(artifact_type, artifact_id),
    FOREIGN KEY (child_artifact_type, child_artifact_id) REFERENCES artifacts(artifact_type, artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_edges_parent ON lineage_edges(parent_artifact_type, parent_artifact_id);
CREATE INDEX IF NOT EXISTS idx_edges_child ON lineage_edges(child_artifact_type, child_artifact_id);

CREATE TABLE IF NOT EXISTS lineage_issues (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_type TEXT NOT NULL,
    artifact_id   TEXT NOT NULL,
    issue_code    TEXT NOT NULL,
    detail        TEXT,
    detected_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_issues_artifact ON lineage_issues(artifact_type, artifact_id);

CREATE TABLE IF NOT EXISTS datasets (
    dataset_name  TEXT PRIMARY KEY,
    description   TEXT,
    metadata_json TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset_name TEXT NOT NULL,
    version      TEXT NOT NULL,
    package_id   TEXT NOT NULL,
    description  TEXT,
    tags_json    TEXT,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (dataset_name, version),
    FOREIGN KEY (dataset_name) REFERENCES datasets(dataset_name)
);

CREATE INDEX IF NOT EXISTS idx_versions_package ON dataset_versions(package_id);

-- Data governance (v2.5). Deliberately NOT foreign-keyed to `artifacts`:
-- an artifact can be transiently absent from the artifacts table (before
-- a first scan, or if its manifest vanished from disk) without that
-- ever being allowed to cascade-delete governance history -- see
-- docs/DETAILED_GUIDE.md "Data governance and selective rebuild (v2.5)".
-- Absence of a row here means ACTIVE; only DEPRECATED/INVALID states are
-- ever stored, to keep this table proportional to actual bad data, not
-- every artifact ever registered.
CREATE TABLE IF NOT EXISTS artifact_governance (
    artifact_type      TEXT NOT NULL,
    artifact_id        TEXT NOT NULL,
    state              TEXT NOT NULL,
    reason             TEXT NOT NULL,
    actor              TEXT,
    superseded_by_type TEXT,
    superseded_by_id   TEXT,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (artifact_type, artifact_id)
);

-- Append-only. Never updated or deleted -- a reactivation is a new event,
-- not an erasure of the invalidation event that preceded it.
CREATE TABLE IF NOT EXISTS artifact_governance_events (
    event_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_type      TEXT NOT NULL,
    artifact_id        TEXT NOT NULL,
    previous_state     TEXT NOT NULL,
    new_state          TEXT NOT NULL,
    reason             TEXT NOT NULL,
    actor              TEXT,
    superseded_by_type TEXT,
    superseded_by_id   TEXT,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gov_events_artifact
    ON artifact_governance_events(artifact_type, artifact_id, event_id);

-- Dataset-version governance. FK-safe unlike artifact_governance because
-- dataset_versions rows are never deleted by anything, including a
-- catalog rebuild (same guarantee as `datasets`/`dataset_versions`
-- themselves -- see CatalogService.rebuild()).
CREATE TABLE IF NOT EXISTS dataset_version_governance (
    dataset_name TEXT NOT NULL,
    version      TEXT NOT NULL,
    state        TEXT NOT NULL,
    reason       TEXT NOT NULL,
    actor        TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (dataset_name, version),
    FOREIGN KEY (dataset_name, version) REFERENCES dataset_versions(dataset_name, version)
);

CREATE TABLE IF NOT EXISTS dataset_version_governance_events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_name   TEXT NOT NULL,
    version        TEXT NOT NULL,
    previous_state TEXT NOT NULL,
    new_state      TEXT NOT NULL,
    reason         TEXT NOT NULL,
    actor          TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gov_version_events
    ON dataset_version_governance_events(dataset_name, version, event_id);

-- Pipeline runs (v2.6). Operational execution metadata -- NEVER the
-- source of truth for artifact content (that stays filesystem manifests,
-- per v2.1). A run may fail, be cancelled, or produce zero artifacts
-- without that implying anything about artifacts already published.
-- See docs/DETAILED_GUIDE.md "Pipeline runs and observability (v2.6)".
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id            TEXT PRIMARY KEY,
    run_type          TEXT NOT NULL,   -- 'stage' | 'pipeline' | 'selective_rebuild'
    status            TEXT NOT NULL,   -- queued|running|completed|failed|cancel_requested|cancelled
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    current_stage     TEXT,
    request_json      TEXT NOT NULL,   -- canonical JSON snapshot of the effective request -- never raw file bytes
    config_hash       TEXT NOT NULL,
    error_code        TEXT,
    error_message     TEXT,
    executor_id       TEXT,            -- hostname:pid:uuid of the process currently owning execution
    last_heartbeat_at TEXT,
    retry_of_run_id   TEXT             -- optional; a retry is always a NEW run_id, never a mutation of the old one
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON pipeline_runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_created ON pipeline_runs(created_at);

-- Deliberately NOT foreign-keyed to `artifacts` -- a run's own life
-- (queued/running/...) is independent of whether any given artifact it
-- touches happens to be currently indexed.
CREATE TABLE IF NOT EXISTS pipeline_stage_runs (
    stage_run_id       TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL,
    stage              TEXT NOT NULL,
    status             TEXT NOT NULL,  -- pending|running|completed|failed|skipped|cancelled
    started_at         TEXT,
    finished_at        TEXT,
    records_total      INTEGER,
    records_processed  INTEGER,
    bytes_total        INTEGER,
    bytes_processed    INTEGER,
    artifacts_created  INTEGER NOT NULL DEFAULT 0,
    error_code         TEXT,
    error_message      TEXT,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_stage_runs_run ON pipeline_stage_runs(run_id);

-- Operational provenance ("which run produced this"), NOT causal lineage
-- -- app.catalog.graph's edges remain the sole source of truth for
-- upstream/downstream relationships. Deliberately NOT foreign-keyed to
-- `artifacts`: an artifact a run produced may not be scanned/indexed yet
-- (catalog population stays scan-driven, per v2.1-v2.5), and must never
-- have its run history silently dropped because of that.
CREATE TABLE IF NOT EXISTS run_artifacts (
    run_id         TEXT NOT NULL,
    stage          TEXT NOT NULL,
    artifact_type  TEXT NOT NULL,
    artifact_id    TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, artifact_type, artifact_id),
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_run_artifacts_run ON run_artifacts(run_id);

-- Append-only, meaningful lifecycle transitions only -- never one row
-- per progress update (those live as mutable current-state columns on
-- pipeline_stage_runs instead; see Design Requirement 21).
CREATE TABLE IF NOT EXISTS run_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, event_id);

CREATE TABLE IF NOT EXISTS catalog_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class JournalModeNotAppliedError(Exception):
    """Raised when the requested journal_mode could not be verified —
    e.g. WAL requires a real filesystem that supports shared memory
    (mmap); some network filesystems silently refuse it. Fail loudly
    rather than silently running without the concurrency guarantees the
    rest of v2.4 assumes."""


_JOURNAL_MODE_RETRY_ATTEMPTS = 10
_JOURNAL_MODE_RETRY_BASE_DELAY_S = 0.02


def _set_journal_mode_with_retry(conn: sqlite3.Connection, journal_mode: str, db_path: Path) -> str:
    """`PRAGMA journal_mode = WAL`, the FIRST time it runs against a given
    database file, briefly needs exclusive access to rewrite the file
    header. SQLite's own `busy_timeout` PRAGMA does not reliably cover
    this one-time switch -- observed directly: two processes opening a
    brand-new catalog.db at the same instant can each get "database is
    locked" from this specific statement even with busy_timeout already
    configured on the connection. Every connection after the first sees
    the mode already applied and never takes this path at all.

    This is a bounded retry, not the unbounded/hidden kind Design
    Requirement 32 forbids: a fixed, small number of short sleeps,
    capped well under a second in the worst case, and it only ever
    retries "database is locked"/"database is busy" -- anything else
    propagates immediately."""
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(_JOURNAL_MODE_RETRY_ATTEMPTS):
        try:
            return conn.execute(f"PRAGMA journal_mode = {journal_mode}").fetchone()[0]
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            last_exc = exc
            time.sleep(_JOURNAL_MODE_RETRY_BASE_DELAY_S * (attempt + 1))
    raise last_exc  # pragma: no cover -- exhausting 10 bounded retries needs pathological contention


def get_connection(
    db_path: Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    journal_mode: str = DEFAULT_JOURNAL_MODE,
) -> sqlite3.Connection:
    """Opens (creating if needed) the catalog database, configured for
    concurrent multiprocess access:

      - one connection per call, never shared across processes or
        stored as a long-lived global (callers are responsible for this
        — see app.api.routes.catalog.get_catalog_service, which already
        opens one fresh connection per request)
      - WAL journaling by default, VERIFIED via the PRAGMA's own return
        value rather than assumed (see JournalModeNotAppliedError)
      - a bounded busy_timeout, so a writer blocked behind another
        writer waits up to this long before SQLite raises "database is
        locked" -- CatalogRepository.transaction() catches that and
        raises a structured CatalogBusyError, never a raw
        sqlite3.OperationalError
      - foreign keys enabled (off by default in SQLite)

    Callers manage their own transactions explicitly (`isolation_level=
    None` — autocommit off, manual BEGIN/COMMIT/ROLLBACK) so a
    multi-statement registration (an artifact plus its edges) commits or
    rolls back as one unit.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI resolves a sync dependency (this
    # connection is created inside one) and the async route body that
    # consumes it on potentially different threadpool threads for the
    # same request. Safe here because each request gets its own
    # short-lived connection — never shared/concurrent across requests.
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")

    if journal_mode:
        applied = _set_journal_mode_with_retry(conn, journal_mode, db_path)
        if applied.lower() != journal_mode.lower():
            raise JournalModeNotAppliedError(
                f"Requested journal_mode={journal_mode!r} but SQLite reports {applied!r} "
                f"for {db_path} — the filesystem hosting the catalog may not support it "
                f"(e.g. some network filesystems reject WAL's shared-memory file)."
            )

    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    # A cheap read first (never blocks on a concurrent writer under WAL)
    # so that every connection opened AFTER the schema-version row has
    # already been seeded -- i.e. virtually every connection, in every
    # long-running process -- never attempts a write here at all. Only
    # the very first connection(s) ever opened against a brand-new
    # catalog.db reach the INSERT below.
    row = conn.execute("SELECT 1 FROM catalog_metadata WHERE key = 'catalog_schema_version'").fetchone()
    if row is None:
        # Race-safe even so: ON CONFLICT DO NOTHING instead of trusting
        # the SELECT above, so two processes opening a brand-new
        # catalog.db at the same instant never raise a raw
        # IntegrityError racing to seed this row -- every writer would
        # insert the exact same CATALOG_SCHEMA_VERSION value anyway, so
        # "first one wins, everyone else is a no-op" is always correct.
        conn.execute(
            "INSERT INTO catalog_metadata (key, value) VALUES ('catalog_schema_version', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (CATALOG_SCHEMA_VERSION,),
        )
