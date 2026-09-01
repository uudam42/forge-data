"""Tests for the crash-recovery scanner/cleanup service: classification
(ACTIVE / STALE / INVALID_STAGING_ENTRY), conservative cleanup (only
STALE is ever removed, never guessed at from a bare PID), and that a
finalized artifact is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.storage.atomic import commit_staging_dir, create_staging_dir, write_manifest_file
from app.storage.local import LocalRawStorage
from app.storage.normalized_store import LocalNormalizedArtifactStore
from app.storage.recovery import ACTIVE, INVALID, STALE, RecoveryService


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


def _backdate(staging_dir: Path, iso_timestamp: str) -> None:
    state_path = staging_dir / "staging_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["started_at"] = iso_timestamp
    state_path.write_text(json.dumps(state), encoding="utf-8")


def test_fresh_staging_entry_is_active(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    staging = settings.NORMALIZED_STORAGE_ROOT / "ing_a" / ".tmp-norm_1"
    create_staging_dir(staging, operation_id="norm_1", artifact_id="norm_1", stage="normalization", final_destination=tmp_path / "x")

    result = RecoveryService(settings).scan()
    assert result.active_count == 1
    assert result.stale_count == 0
    assert result.invalid_count == 0
    assert result.entries[0].classification == ACTIVE
    assert result.entries[0].stage == "normalization"


def test_old_staging_entry_is_stale(tmp_path: Path) -> None:
    settings = _settings(tmp_path, STALE_STAGING_AFTER_SECONDS=60.0)
    staging = settings.RAW_STORAGE_ROOT / ".staging" / "ing_1"
    create_staging_dir(staging, operation_id="ing_1", artifact_id="ing_1", stage="ingestion", final_destination=tmp_path / "x")
    _backdate(staging, "2000-01-01T00:00:00+00:00")

    result = RecoveryService(settings).scan()
    assert result.stale_count == 1
    assert result.entries[0].classification == STALE
    assert result.entries[0].operation_id == "ing_1"


def test_missing_metadata_is_invalid(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ghost = settings.RAW_STORAGE_ROOT / ".staging" / "ing_ghost"
    ghost.mkdir(parents=True)  # no staging_state.json at all

    result = RecoveryService(settings).scan()
    assert result.invalid_count == 1
    assert result.entries[0].classification == INVALID
    assert "missing" in result.entries[0].reason


def test_unparseable_metadata_is_invalid(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ghost = settings.RAW_STORAGE_ROOT / ".staging" / "ing_ghost"
    ghost.mkdir(parents=True)
    (ghost / "staging_state.json").write_text("{not json", encoding="utf-8")

    result = RecoveryService(settings).scan()
    assert result.invalid_count == 1
    assert result.entries[0].classification == INVALID


def test_cleanup_stale_removes_only_stale_entries(tmp_path: Path) -> None:
    settings = _settings(tmp_path, STALE_STAGING_AFTER_SECONDS=60.0)

    active = settings.NORMALIZED_STORAGE_ROOT / "ing_a" / ".tmp-norm_active"
    create_staging_dir(active, operation_id="norm_active", artifact_id="norm_active", stage="normalization", final_destination=tmp_path / "x")

    stale = settings.RAW_STORAGE_ROOT / ".staging" / "ing_stale"
    create_staging_dir(stale, operation_id="ing_stale", artifact_id="ing_stale", stage="ingestion", final_destination=tmp_path / "y")
    _backdate(stale, "2000-01-01T00:00:00+00:00")

    invalid = settings.VALIDATION_STORAGE_ROOT / ".staging" / "val_invalid"
    invalid.mkdir(parents=True)

    service = RecoveryService(settings)
    removed = service.cleanup_stale()

    assert len(removed) == 1
    assert removed[0].operation_id == "ing_stale"
    assert not stale.exists()
    assert active.exists()  # untouched
    assert invalid.exists()  # untouched -- never guessed at


def test_cleanup_stale_dry_run_reports_without_removing(tmp_path: Path) -> None:
    settings = _settings(tmp_path, STALE_STAGING_AFTER_SECONDS=60.0)
    stale = settings.RAW_STORAGE_ROOT / ".staging" / "ing_stale"
    create_staging_dir(stale, operation_id="ing_stale", artifact_id="ing_stale", stage="ingestion", final_destination=tmp_path / "y")
    _backdate(stale, "2000-01-01T00:00:00+00:00")

    removed = RecoveryService(settings).cleanup_stale(dry_run=True)
    assert len(removed) == 1
    assert stale.exists()  # still there -- dry_run never deletes


def test_cleanup_never_touches_finalized_artifacts(tmp_path: Path) -> None:
    """A committed artifact has no staging_state.json and doesn't match
    the .staging/.tmp- naming the scanner looks for -- cleanup_stale must
    never find, let alone remove, it."""
    settings = _settings(tmp_path, STALE_STAGING_AFTER_SECONDS=0.0)
    store = LocalNormalizedArtifactStore(root=settings.NORMALIZED_STORAGE_ROOT)
    staging = store.staging_dir(ingestion_id="ing_a", normalization_id="norm_a")
    (staging / "normalized.csv").write_text("data", encoding="utf-8")
    write_manifest_file(staging, "manifest.json", "{}")
    store.commit(ingestion_id="ing_a", normalization_id="norm_a", staging_dir=staging)

    final_dir = settings.NORMALIZED_STORAGE_ROOT / "ing_a" / "norm_a"
    assert final_dir.exists()

    removed = RecoveryService(settings).cleanup_stale()
    assert removed == []
    assert final_dir.exists()
    assert (final_dir / "manifest.json").exists()


def test_recovery_api_scan_and_cleanup(client: TestClient, test_settings: Settings) -> None:
    staging = test_settings.RAW_STORAGE_ROOT / ".staging" / "ing_stale"
    create_staging_dir(staging, operation_id="ing_stale", artifact_id="ing_stale", stage="ingestion", final_destination=test_settings.RAW_STORAGE_ROOT / "x")
    state_path = staging / "staging_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["started_at"] = "2000-01-01T00:00:00+00:00"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    scan_response = client.get("/api/v1/recovery/scan")
    assert scan_response.status_code == 200
    body = scan_response.json()
    assert body["stale_count"] == 1

    cleanup_response = client.post("/api/v1/recovery/cleanup")
    assert cleanup_response.status_code == 200
    assert len(cleanup_response.json()) == 1
    assert not staging.exists()
