"""SQLite-backed CRUD access for pipeline runs (v2.6).

Shares the same catalog.db file/connection-factory as
app.catalog.repository.CatalogRepository (same WAL/busy_timeout/
BEGIN IMMEDIATE machinery from v2.4 -- see that module's transaction()
docstring for why the read-decide-write pattern used throughout this
file is race-safe under concurrent processes), but is its own class:
runs are their own bounded domain, not catalog-artifact metadata.

This is the only module that speaks raw SQL for run tables.
"""

from __future__ import annotations

import contextlib
import sqlite3

from app.catalog.errors import CatalogBusyError


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


class RunRepository:
    def __init__(self, conn: sqlite3.Connection, *, db_path: str = "", busy_timeout_ms: int = 5000) -> None:
        self._conn = conn
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms

    @contextlib.contextmanager
    def transaction(self, *, operation: str = "transaction"):
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_locked_error(exc):
                raise CatalogBusyError(operation=operation, timeout_ms=self._busy_timeout_ms, db_path=self._db_path) from exc
            raise
        try:
            yield
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # pipeline_runs
    # ------------------------------------------------------------------

    def create_run(self, *, run_id: str, run_type: str, status: str, created_at: str, request_json: str, config_hash: str) -> None:
        self._conn.execute(
            """INSERT INTO pipeline_runs (run_id, run_type, status, created_at, request_json, config_hash)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, run_type, status, created_at, request_json, config_hash),
        )

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_runs(self, *, status: str | None = None, run_type: str | None = None, limit: int = 20, offset: int = 0) -> list[dict]:
        query = "SELECT * FROM pipeline_runs WHERE 1=1"
        params: list = []
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if run_type is not None:
            query += " AND run_type = ?"
            params.append(run_type)
        query += " ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def count_active_runs(self) -> int:
        """Runs currently occupying local execution capacity -- see
        Design Requirement 39. 'queued' counts too: in this design a
        queued run starts executing immediately (there is no real
        waiting queue -- see Design Requirement 33), so treating it as
        reserved capacity from the moment it's created is what actually
        closes the race between two concurrent create_run calls; 'running'
        and 'cancel_requested' both count too (a cancellation hasn't
        freed the slot until it's actually observed)."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM pipeline_runs WHERE status IN ('queued', 'running', 'cancel_requested')"
        ).fetchone()
        return row[0]

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        current_stage: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        executor_id: str | None = None,
        last_heartbeat_at: str | None = None,
    ) -> None:
        """Only overwrites the columns explicitly passed as non-None --
        callers pass exactly the fields relevant to the transition they're
        recording (see app.runs.service for the state machine)."""
        sets = ["status = :status"]
        params: dict = {"run_id": run_id, "status": status}
        for column, value in (
            ("current_stage", current_stage), ("started_at", started_at), ("finished_at", finished_at),
            ("error_code", error_code), ("error_message", error_message),
            ("executor_id", executor_id), ("last_heartbeat_at", last_heartbeat_at),
        ):
            if value is not None:
                sets.append(f"{column} = :{column}")
                params[column] = value
        self._conn.execute(f"UPDATE pipeline_runs SET {', '.join(sets)} WHERE run_id = :run_id", params)

    def touch_heartbeat(self, run_id: str, *, last_heartbeat_at: str) -> None:
        self._conn.execute(
            "UPDATE pipeline_runs SET last_heartbeat_at = ? WHERE run_id = ? AND status IN ('running', 'cancel_requested')",
            (last_heartbeat_at, run_id),
        )

    def find_stale_running_runs(self, *, older_than: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM pipeline_runs
               WHERE status IN ('running', 'cancel_requested')
                 AND (last_heartbeat_at IS NULL OR last_heartbeat_at < ?)""",
            (older_than,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_all_runs(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]

    # ------------------------------------------------------------------
    # pipeline_stage_runs
    # ------------------------------------------------------------------

    def create_stage_run(self, *, stage_run_id: str, run_id: str, stage: str, status: str) -> None:
        self._conn.execute(
            "INSERT INTO pipeline_stage_runs (stage_run_id, run_id, stage, status, artifacts_created) VALUES (?, ?, ?, ?, 0)",
            (stage_run_id, run_id, stage, status),
        )

    def get_stage_run(self, stage_run_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM pipeline_stage_runs WHERE stage_run_id = ?", (stage_run_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_stage_runs(self, run_id: str) -> list[dict]:
        # ORDER BY rowid (not stage_run_id, a UUID with no relation to
        # insertion order) so stages come back in the order
        # `_create_all_stage_runs` created them -- real execution order,
        # not lexicographic UUID order.
        rows = self._conn.execute(
            "SELECT * FROM pipeline_stage_runs WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def update_stage_run(
        self,
        stage_run_id: str,
        *,
        status: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        records_total: int | None = None,
        records_processed: int | None = None,
        bytes_total: int | None = None,
        bytes_processed: int | None = None,
        artifacts_created: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        sets = []
        params: dict = {"stage_run_id": stage_run_id}
        for column, value in (
            ("status", status), ("started_at", started_at), ("finished_at", finished_at),
            ("records_total", records_total), ("records_processed", records_processed),
            ("bytes_total", bytes_total), ("bytes_processed", bytes_processed),
            ("artifacts_created", artifacts_created), ("error_code", error_code), ("error_message", error_message),
        ):
            if value is not None:
                sets.append(f"{column} = :{column}")
                params[column] = value
        if not sets:
            return
        self._conn.execute(f"UPDATE pipeline_stage_runs SET {', '.join(sets)} WHERE stage_run_id = :stage_run_id", params)

    # ------------------------------------------------------------------
    # run_artifacts
    # ------------------------------------------------------------------

    def record_run_artifact(self, *, run_id: str, stage: str, artifact_type: str, artifact_id: str, created_at: str) -> None:
        self._conn.execute(
            """INSERT INTO run_artifacts (run_id, stage, artifact_type, artifact_id, created_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(run_id, artifact_type, artifact_id) DO NOTHING""",
            (run_id, stage, artifact_type, artifact_id, created_at),
        )

    def list_run_artifacts(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM run_artifacts WHERE run_id = ? ORDER BY created_at, artifact_type, artifact_id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_all_run_artifacts(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM run_artifacts").fetchall()]

    # ------------------------------------------------------------------
    # run_events (append-only)
    # ------------------------------------------------------------------

    def record_event(self, *, run_id: str, event_type: str, detail: str | None, created_at: str) -> None:
        self._conn.execute(
            "INSERT INTO run_events (run_id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
            (run_id, event_type, detail, created_at),
        )

    def list_events(self, run_id: str) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM run_events WHERE run_id = ? ORDER BY event_id", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Preservation helpers (catalog rebuild must never touch these — v2.6
    # Design Requirement 57; used by CatalogService.rebuild()'s before/
    # after assertions, mirroring the v2.5 governance-preservation pattern)
    # ------------------------------------------------------------------

    def count_run_tables(self) -> tuple[int, int, int, int]:
        return (
            self.count_all_runs(),
            self._conn.execute("SELECT COUNT(*) FROM pipeline_stage_runs").fetchone()[0],
            self._conn.execute("SELECT COUNT(*) FROM run_artifacts").fetchone()[0],
            self._conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0],
        )
