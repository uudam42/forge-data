"""SQLite-backed CRUD access for the catalog. Repository methods assume an
already-open transaction (see `transaction()`) — they never open their own,
so a caller can group an artifact registration with its lineage edges (or
an entire rebuild) into one atomic unit.

This is the only module that speaks raw SQL. Every other catalog module
goes through here.
"""

from __future__ import annotations

import contextlib
import sqlite3

from app.catalog.errors import ArtifactRegistryConflictError, CatalogBusyError, DatasetVersionImmutableError

# Fields that must never silently change once an artifact is registered —
# if a re-scan observes a different value here, the underlying manifest
# was mutated after indexing (impossible under every stage's own
# immutability guarantees unless something external tampered with it), so
# this is treated as a hard conflict, never a silent overwrite. This is
# also the mechanism that satisfies "registry stale metadata conflict
# detected".
_CONFLICT_FIELDS = ("content_sha256", "manifest_uri", "manifest_sha256", "storage_uri", "metadata_json")


def _is_unique_violation(exc: sqlite3.IntegrityError) -> bool:
    """True for a primary-key/UNIQUE conflict (another writer got there
    first) — false for anything else (e.g. a FOREIGN KEY violation),
    which callers must still let propagate."""
    message = str(exc)
    return "UNIQUE constraint failed" in message or "PRIMARY KEY constraint failed" in message


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


class CatalogRepository:
    def __init__(self, conn: sqlite3.Connection, *, db_path: str = "", busy_timeout_ms: int = 5000) -> None:
        self._conn = conn
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms

    @contextlib.contextmanager
    def transaction(self, *, operation: str = "transaction"):
        """Opens a write transaction. Uses BEGIN IMMEDIATE (not the
        bare/deferred BEGIN SQLite defaults to) so the write lock is
        acquired up front, at the start of the transaction, rather than
        lazily on the first write statement — this is what makes the
        "act, then let a UNIQUE/PK conflict decide" pattern in
        upsert_artifact/insert_edge/create_dataset/create_dataset_version
        race-safe: only one process can hold this lock at a time, so a
        concurrent writer either finishes-and-commits before this one
        starts, or blocks (up to busy_timeout_ms) and never interleaves
        with it.

        If the lock can't be acquired within busy_timeout_ms, SQLite
        raises "database is locked" -- this is caught here and re-raised
        as a structured CatalogBusyError rather than a raw
        sqlite3.OperationalError, per the v2.4 error model."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_locked_error(exc):
                raise CatalogBusyError(
                    operation=operation, timeout_ms=self._busy_timeout_ms, db_path=self._db_path
                ) from exc
            raise
        try:
            yield
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    def get_artifact(self, artifact_type: str, artifact_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_type = ? AND artifact_id = ?",
            (artifact_type, artifact_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_artifact(self, record: dict) -> str:
        """Returns "inserted" or "unchanged". Raises
        ArtifactRegistryConflictError if an existing entry's identity
        fields disagree with the new record — never silently overwritten.

        Race-safe: attempts the INSERT first and lets the
        (artifact_type, artifact_id) primary key be the final authority,
        rather than trusting a prior SELECT that a concurrent writer may
        have invalidated in the meantime (see transaction() for why this
        is safe under BEGIN IMMEDIATE)."""
        try:
            self._conn.execute(
                """INSERT INTO artifacts
                   (artifact_type, artifact_id, pipeline_stage, status, storage_uri,
                    content_sha256, manifest_uri, manifest_sha256, created_at,
                    session_id, metadata_json, registered_at)
                   VALUES (:artifact_type, :artifact_id, :pipeline_stage, :status, :storage_uri,
                           :content_sha256, :manifest_uri, :manifest_sha256, :created_at,
                           :session_id, :metadata_json, :registered_at)""",
                record,
            )
            return "inserted"
        except sqlite3.IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            existing = self.get_artifact(record["artifact_type"], record["artifact_id"])

        for field in _CONFLICT_FIELDS:
            if existing.get(field) != record.get(field):
                raise ArtifactRegistryConflictError(
                    f"{record['artifact_type']}/{record['artifact_id']}: field '{field}' would change "
                    f"from {existing.get(field)!r} to {record.get(field)!r} — refusing to overwrite"
                ) from None
        return "unchanged"

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        stage: int | None = None,
        status: str | None = None,
        session_id: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM artifacts WHERE 1=1"
        params: list = []
        if artifact_type is not None:
            query += " AND artifact_type = ?"
            params.append(artifact_type)
        if stage is not None:
            query += " AND pipeline_stage = ?"
            params.append(stage)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        query += " ORDER BY pipeline_stage, artifact_type, artifact_id"
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def count_artifacts(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    def clear_artifact_index(self) -> None:
        """Deletes artifacts + lineage edges + lineage issues — never
        datasets or dataset_versions (user-registered metadata, not
        reconstructible from filesystem manifests)."""
        self._conn.execute("DELETE FROM lineage_edges")
        self._conn.execute("DELETE FROM lineage_issues")
        self._conn.execute("DELETE FROM artifacts")

    # ------------------------------------------------------------------
    # Lineage edges
    # ------------------------------------------------------------------

    def insert_edge(
        self, *, parent_type: str, parent_id: str, child_type: str, child_id: str, relationship: str
    ) -> bool:
        """Idempotent. Returns True if newly inserted, False if it already
        existed unchanged.

        Race-safe: attempts the INSERT first and lets the 5-column
        primary key be the final authority instead of a prior SELECT. A
        FOREIGN KEY violation (parent/child artifact genuinely missing)
        is a different failure mode and is left to propagate."""
        try:
            self._conn.execute(
                """INSERT INTO lineage_edges
                   (parent_artifact_type, parent_artifact_id, child_artifact_type, child_artifact_id, relationship)
                   VALUES (?, ?, ?, ?, ?)""",
                (parent_type, parent_id, child_type, child_id, relationship),
            )
            return True
        except sqlite3.IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            return False

    def get_parents(self, artifact_type: str, artifact_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM lineage_edges WHERE child_artifact_type=? AND child_artifact_id=?
               ORDER BY parent_artifact_type, parent_artifact_id, relationship""",
            (artifact_type, artifact_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_children(self, artifact_type: str, artifact_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM lineage_edges WHERE parent_artifact_type=? AND parent_artifact_id=?
               ORDER BY child_artifact_type, child_artifact_id, relationship""",
            (artifact_type, artifact_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_edges(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM lineage_edges").fetchone()[0]

    # ------------------------------------------------------------------
    # Lineage issues (non-strict scan findings)
    # ------------------------------------------------------------------

    def record_issue(self, *, artifact_type: str, artifact_id: str, issue_code: str, detail: str, detected_at: str) -> None:
        self._conn.execute(
            "INSERT INTO lineage_issues (artifact_type, artifact_id, issue_code, detail, detected_at) VALUES (?, ?, ?, ?, ?)",
            (artifact_type, artifact_id, issue_code, detail, detected_at),
        )

    def list_issues(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM lineage_issues ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Datasets
    # ------------------------------------------------------------------

    def get_dataset(self, dataset_name: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM datasets WHERE dataset_name = ?", (dataset_name,)).fetchone()
        return dict(row) if row is not None else None

    def create_dataset(self, *, dataset_name: str, description: str | None, metadata_json: str, created_at: str) -> bool:
        """Returns True if this call created the dataset, False if it
        already existed (idempotent by name — a repeat call never
        compares or overwrites description/metadata).

        Race-safe: attempts the INSERT first and lets the dataset_name
        primary key be the final authority instead of a prior SELECT, so
        two processes creating the same dataset concurrently never raise
        a raw sqlite3.IntegrityError — the loser simply learns it lost."""
        try:
            self._conn.execute(
                "INSERT INTO datasets (dataset_name, description, metadata_json, created_at) VALUES (?, ?, ?, ?)",
                (dataset_name, description, metadata_json, created_at),
            )
            return True
        except sqlite3.IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            return False

    def list_datasets(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM datasets ORDER BY dataset_name").fetchall()]

    def count_datasets(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]

    def get_dataset_version(self, dataset_name: str, version: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM dataset_versions WHERE dataset_name = ? AND version = ?", (dataset_name, version)
        ).fetchone()
        return dict(row) if row is not None else None

    def create_dataset_version(
        self,
        *,
        dataset_name: str,
        version: str,
        package_id: str,
        description: str | None,
        tags_json: str,
        status: str,
        created_at: str,
    ) -> str:
        """Returns "created" or "unchanged" (idempotent re-registration of
        the exact same dataset+version+package). Raises
        DatasetVersionImmutableError if an existing entry already points
        to a DIFFERENT package — a version can never be reassigned.

        Race-safe: attempts the INSERT first and lets the
        (dataset_name, version) primary key be the final authority
        instead of a prior SELECT, so two processes racing to register
        the same dataset+version resolve deterministically: exactly one
        "created", and the other either "unchanged" (same package — an
        idempotent retry) or a DatasetVersionImmutableError (different
        package — a genuine conflict), never a raw IntegrityError."""
        try:
            self._conn.execute(
                """INSERT INTO dataset_versions
                   (dataset_name, version, package_id, description, tags_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (dataset_name, version, package_id, description, tags_json, status, created_at),
            )
            return "created"
        except sqlite3.IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            existing = self.get_dataset_version(dataset_name, version)
            if existing is not None and existing["package_id"] == package_id:
                return "unchanged"
            raise DatasetVersionImmutableError(
                f"{dataset_name}@{version} already points to package "
                f"'{existing['package_id'] if existing else '?'}' — it can never be reassigned to '{package_id}'"
            ) from None

    def list_dataset_versions(self, dataset_name: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM dataset_versions WHERE dataset_name = ? ORDER BY version", (dataset_name,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_all_dataset_versions(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM dataset_versions ORDER BY dataset_name, version").fetchall()
        return [dict(r) for r in rows]

    def list_dataset_versions_for_packages(self, package_ids: list[str]) -> list[dict]:
        if not package_ids:
            return []
        placeholders = ",".join("?" for _ in package_ids)
        rows = self._conn.execute(
            f"SELECT * FROM dataset_versions WHERE package_id IN ({placeholders}) ORDER BY dataset_name, version",
            package_ids,
        ).fetchall()
        return [dict(r) for r in rows]

    def count_dataset_versions(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM dataset_versions").fetchone()[0]

    # ------------------------------------------------------------------
    # Artifact governance (v2.5)
    #
    # Concurrency note: unlike upsert_artifact/create_dataset/etc, these
    # methods use a plain read-then-write, not INSERT+catch-IntegrityError.
    # That's safe here specifically because every caller performs the
    # read (via get_artifact_governance, to compute previous_state and
    # validate the transition) and the write inside the SAME already-open
    # BEGIN IMMEDIATE transaction (see CatalogService.set_artifact_governance)
    # -- the write lock is held for the whole read-decide-write sequence,
    # so no other process's write can interleave. See transaction()'s
    # docstring for why BEGIN IMMEDIATE makes this safe.
    # ------------------------------------------------------------------

    def get_artifact_governance(self, artifact_type: str, artifact_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM artifact_governance WHERE artifact_type = ? AND artifact_id = ?",
            (artifact_type, artifact_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_artifact_governance(
        self,
        *,
        artifact_type: str,
        artifact_id: str,
        previous_state: str,
        new_state: str,
        reason: str,
        actor: str | None,
        superseded_by_type: str | None,
        superseded_by_id: str | None,
        updated_at: str,
    ) -> None:
        """Writes the current-state row (or deletes it if new_state is
        "active" -- absence means active, see Design Requirement 3) and
        appends an immutable event recording the transition. Both writes
        happen together so the current-state table and the event log can
        never drift apart."""
        if new_state == "active":
            self._conn.execute(
                "DELETE FROM artifact_governance WHERE artifact_type = ? AND artifact_id = ?",
                (artifact_type, artifact_id),
            )
        else:
            self._conn.execute(
                """INSERT INTO artifact_governance
                   (artifact_type, artifact_id, state, reason, actor, superseded_by_type, superseded_by_id, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(artifact_type, artifact_id) DO UPDATE SET
                       state = excluded.state, reason = excluded.reason, actor = excluded.actor,
                       superseded_by_type = excluded.superseded_by_type,
                       superseded_by_id = excluded.superseded_by_id, updated_at = excluded.updated_at""",
                (artifact_type, artifact_id, new_state, reason, actor, superseded_by_type, superseded_by_id, updated_at),
            )
        self._conn.execute(
            """INSERT INTO artifact_governance_events
               (artifact_type, artifact_id, previous_state, new_state, reason, actor,
                superseded_by_type, superseded_by_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (artifact_type, artifact_id, previous_state, new_state, reason, actor, superseded_by_type, superseded_by_id, updated_at),
        )

    def list_artifact_governance_events(self, artifact_type: str, artifact_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM artifact_governance_events WHERE artifact_type = ? AND artifact_id = ? ORDER BY event_id",
            (artifact_type, artifact_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_all_artifact_governance(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM artifact_governance ORDER BY artifact_type, artifact_id").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Dataset-version governance (v2.5) -- same shape/concurrency
    # argument as artifact governance above.
    # ------------------------------------------------------------------

    def get_dataset_version_governance(self, dataset_name: str, version: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM dataset_version_governance WHERE dataset_name = ? AND version = ?",
            (dataset_name, version),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_dataset_version_governance(
        self,
        *,
        dataset_name: str,
        version: str,
        previous_state: str,
        new_state: str,
        reason: str,
        actor: str | None,
        updated_at: str,
    ) -> None:
        if new_state == "active":
            self._conn.execute(
                "DELETE FROM dataset_version_governance WHERE dataset_name = ? AND version = ?",
                (dataset_name, version),
            )
        else:
            self._conn.execute(
                """INSERT INTO dataset_version_governance
                   (dataset_name, version, state, reason, actor, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(dataset_name, version) DO UPDATE SET
                       state = excluded.state, reason = excluded.reason, actor = excluded.actor,
                       updated_at = excluded.updated_at""",
                (dataset_name, version, new_state, reason, actor, updated_at),
            )
        self._conn.execute(
            """INSERT INTO dataset_version_governance_events
               (dataset_name, version, previous_state, new_state, reason, actor, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (dataset_name, version, previous_state, new_state, reason, actor, updated_at),
        )

    def list_dataset_version_governance_events(self, dataset_name: str, version: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM dataset_version_governance_events WHERE dataset_name = ? AND version = ? ORDER BY event_id",
            (dataset_name, version),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_all_dataset_version_governance(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM dataset_version_governance ORDER BY dataset_name, version"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Run-table preservation check (v2.6, Design Requirement 57) --
    # read-only row counts so CatalogService.rebuild() can assert these
    # tables are untouched, mirroring the v2.5 governance-preservation
    # pattern. app.runs.repository.RunRepository owns all actual
    # read/write access to these tables; this is deliberately the only
    # overlap, kept here so rebuild() doesn't need a second repository
    # dependency just to verify four COUNT(*) queries.
    # ------------------------------------------------------------------

    def list_all_run_artifacts(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM run_artifacts").fetchall()]

    def count_run_tables(self) -> tuple[int, int, int, int]:
        return (
            self._conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0],
            self._conn.execute("SELECT COUNT(*) FROM pipeline_stage_runs").fetchone()[0],
            self._conn.execute("SELECT COUNT(*) FROM run_artifacts").fetchone()[0],
            self._conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0],
        )

    # ------------------------------------------------------------------
    # Catalog metadata (build info, schema version)
    # ------------------------------------------------------------------

    def get_metadata(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM catalog_metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else None

    def set_metadata(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO catalog_metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
