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
(off by default in SQLite). This is a single-process, local-filesystem
design: no distributed locking, no multi-writer coordination beyond
SQLite's own transaction semantics.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

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

CREATE TABLE IF NOT EXISTS catalog_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Opens (creating if needed) the catalog database with foreign keys
    enabled and the schema applied. Callers manage their own transactions
    explicitly (`isolation_level=None` — autocommit off, manual
    BEGIN/COMMIT/ROLLBACK) so a multi-statement registration (an artifact
    plus its edges) can be committed or rolled back as one unit."""
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
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM catalog_metadata WHERE key = 'catalog_schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO catalog_metadata (key, value) VALUES ('catalog_schema_version', ?)",
            (CATALOG_SCHEMA_VERSION,),
        )
