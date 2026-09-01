"""Real process-kill tests (not fault-injection simulation): an actual
child process is SIGKILLed mid-write, and the parent verifies storage
state directly from disk. Two scenarios, matching the two crash-safety
mechanisms this codebase relies on:

  1. A filesystem store (LocalNormalizedArtifactStore) — proves the
     staging/atomic-rename primitive itself survives a real OS-level
     kill, not just a Python exception.
  2. The SQLite catalog — proves a killed mid-rebuild process leaves the
     catalog in its pre-rebuild state, via SQLite's own rollback-journal
     recovery (no bespoke crash-safety code needed here — see
     docs/DETAILED_GUIDE.md, "catalog rebuild crash behavior").

Synchronization between parent and child is via multiprocessing.Event,
never sleep-based polling, so these tests are deterministic.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import multiprocessing as mp

from app.core.config import Settings
from app.storage.atomic import write_manifest_file
from app.storage.normalized_store import LocalNormalizedArtifactStore
from app.storage.recovery import STALE, RecoveryService


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        RAW_STORAGE_ROOT=tmp_path / "raw",
        VALIDATION_STORAGE_ROOT=tmp_path / "validation",
        INTEGRITY_STORAGE_ROOT=tmp_path / "integrity",
        NORMALIZED_STORAGE_ROOT=tmp_path / "normalized",
        SYNCHRONIZED_STORAGE_ROOT=tmp_path / "synchronized",
        CLEANED_STORAGE_ROOT=tmp_path / "cleaned",
        TRANSFORMED_STORAGE_ROOT=tmp_path / "transformed",
        QC_STORAGE_ROOT=tmp_path / "qc",
        PACKAGE_STORAGE_ROOT=tmp_path / "packages",
        CATALOG_DB_PATH=tmp_path / "catalog.db",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ---------------------------------------------------------------------------
# Scenario 1: filesystem store, killed after data is written, before commit.
# ---------------------------------------------------------------------------


def _child_write_then_wait_for_kill(root_str: str, ready) -> None:
    store = LocalNormalizedArtifactStore(root=Path(root_str))
    staging = store.staging_dir(ingestion_id="ing_kill", normalization_id="norm_kill")
    (staging / "normalized.csv").write_text("x" * 10_000, encoding="utf-8")
    write_manifest_file(staging, "manifest.json", "{}")
    ready.set()
    # Deliberately never calls commit() — the parent kills us here.
    signal.pause()


def test_subprocess_kill_before_commit_leaves_no_final_artifact(tmp_path: Path) -> None:
    root = tmp_path / "normalized"
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(target=_child_write_then_wait_for_kill, args=(str(root), ready))
    proc.start()
    try:
        assert ready.wait(timeout=15), "child never signaled readiness"
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=15)
        assert proc.exitcode is not None and proc.exitcode != 0
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=5)

    final_dir = root / "ing_kill" / "norm_kill"
    staging_dir = root / "ing_kill" / ".tmp-norm_kill"
    assert not final_dir.exists(), "a killed process must never leave a valid-looking final artifact"
    assert staging_dir.exists(), "the abandoned staging directory itself is expected to remain for recovery"
    assert (staging_dir / "manifest.json").exists()  # confirms the child really did write before being killed

    # Recovery scan classifies it (threshold=0 makes it immediately STALE
    # rather than waiting out a real clock) and cleanup removes it safely.
    settings = _settings(tmp_path, STALE_STAGING_AFTER_SECONDS=0.0)
    scan_result = RecoveryService(settings).scan()
    matching = [e for e in scan_result.entries if e.artifact_id == "norm_kill"]
    assert len(matching) == 1
    assert matching[0].classification == STALE

    RecoveryService(settings).cleanup_stale()
    assert not staging_dir.exists()

    # The stage is safely rerunnable from the beginning.
    store = LocalNormalizedArtifactStore(root=root)
    staging2 = store.staging_dir(ingestion_id="ing_kill", normalization_id="norm_kill_retry")
    (staging2 / "normalized.csv").write_text("full data", encoding="utf-8")
    write_manifest_file(staging2, "manifest.json", "{}")
    store.commit(ingestion_id="ing_kill", normalization_id="norm_kill_retry", staging_dir=staging2)
    assert (root / "ing_kill" / "norm_kill_retry").exists()


# ---------------------------------------------------------------------------
# Scenario 2: catalog rebuild, killed mid-transaction (before COMMIT).
# ---------------------------------------------------------------------------


def _child_start_rebuild_then_wait_for_kill(db_path_str: str, ready) -> None:
    from app.storage.catalog_store import get_connection

    conn = get_connection(Path(db_path_str))
    conn.execute("BEGIN")
    conn.execute("DELETE FROM artifacts")
    conn.execute("DELETE FROM lineage_edges")
    conn.execute(
        "INSERT INTO artifacts (artifact_type, artifact_id, pipeline_stage, status, storage_uri, "
        "content_sha256, manifest_uri, manifest_sha256, created_at, session_id, metadata_json, registered_at) "
        "VALUES ('ingestion', 'ing_should_never_persist', 1, 'stored', NULL, NULL, 'file:///x', 'deadbeef', "
        "'2026-01-01T00:00:00Z', NULL, '{}', '2026-01-01T00:00:00Z')"
    )
    ready.set()
    # Never reaches COMMIT — the parent kills us here, mid-transaction.
    signal.pause()


def test_subprocess_kill_mid_catalog_rebuild_rolls_back(tmp_path: Path) -> None:
    from app.catalog.repository import CatalogRepository
    from app.storage.catalog_store import get_connection

    db_path = tmp_path / "catalog.db"
    conn = get_connection(db_path)
    repo = CatalogRepository(conn)
    with repo.transaction():
        repo.upsert_artifact(
            {
                "artifact_type": "ingestion", "artifact_id": "ing_baseline", "pipeline_stage": 1,
                "status": "stored", "storage_uri": None, "content_sha256": None,
                "manifest_uri": "file:///baseline", "manifest_sha256": "abc123",
                "created_at": "2026-01-01T00:00:00Z", "session_id": None,
                "metadata_json": "{}", "registered_at": "2026-01-01T00:00:00Z",
            }
        )
    baseline_count = repo.count_artifacts()
    assert baseline_count == 1
    conn.close()

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(target=_child_start_rebuild_then_wait_for_kill, args=(str(db_path), ready))
    proc.start()
    try:
        assert ready.wait(timeout=15), "child never signaled readiness"
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=15)
        assert proc.exitcode is not None and proc.exitcode != 0
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=5)

    # A fresh connection must see the catalog exactly as it was before the
    # killed transaction -- SQLite's own rollback journal discards the
    # incomplete write automatically; no bespoke recovery code runs here.
    fresh_conn = get_connection(db_path)
    fresh_repo = CatalogRepository(fresh_conn)
    assert fresh_repo.count_artifacts() == baseline_count
    assert fresh_repo.get_artifact("ingestion", "ing_baseline") is not None
    assert fresh_repo.get_artifact("ingestion", "ing_should_never_persist") is None
