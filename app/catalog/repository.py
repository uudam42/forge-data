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

from app.catalog.errors import ArtifactRegistryConflictError

# Fields that must never silently change once an artifact is registered —
# if a re-scan observes a different value here, the underlying manifest
# was mutated after indexing (impossible under every stage's own
# immutability guarantees unless something external tampered with it), so
# this is treated as a hard conflict, never a silent overwrite. This is
# also the mechanism that satisfies "registry stale metadata conflict
# detected".
_CONFLICT_FIELDS = ("content_sha256", "manifest_uri", "manifest_sha256", "storage_uri", "metadata_json")


class CatalogRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextlib.contextmanager
    def transaction(self):
        self._conn.execute("BEGIN")
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
        fields disagree with the new record — never silently overwritten."""
        existing = self.get_artifact(record["artifact_type"], record["artifact_id"])
        if existing is None:
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

        for field in _CONFLICT_FIELDS:
            if existing.get(field) != record.get(field):
                raise ArtifactRegistryConflictError(
                    f"{record['artifact_type']}/{record['artifact_id']}: field '{field}' would change "
                    f"from {existing.get(field)!r} to {record.get(field)!r} — refusing to overwrite"
                )
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
        existed unchanged."""
        existing = self._conn.execute(
            """SELECT 1 FROM lineage_edges WHERE parent_artifact_type=? AND parent_artifact_id=?
               AND child_artifact_type=? AND child_artifact_id=? AND relationship=?""",
            (parent_type, parent_id, child_type, child_id, relationship),
        ).fetchone()
        if existing is not None:
            return False
        self._conn.execute(
            """INSERT INTO lineage_edges
               (parent_artifact_type, parent_artifact_id, child_artifact_type, child_artifact_id, relationship)
               VALUES (?, ?, ?, ?, ?)""",
            (parent_type, parent_id, child_type, child_id, relationship),
        )
        return True

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

    def create_dataset(self, *, dataset_name: str, description: str | None, metadata_json: str, created_at: str) -> None:
        self._conn.execute(
            "INSERT INTO datasets (dataset_name, description, metadata_json, created_at) VALUES (?, ?, ?, ?)",
            (dataset_name, description, metadata_json, created_at),
        )

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
    ) -> None:
        self._conn.execute(
            """INSERT INTO dataset_versions
               (dataset_name, version, package_id, description, tags_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (dataset_name, version, package_id, description, tags_json, status, created_at),
        )

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
