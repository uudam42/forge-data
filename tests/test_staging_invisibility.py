"""Design requirement 10: no staging artifact — however valid-looking —
can ever be resolved by ID, discovered by a store's find_* lookup, or
picked up by a catalog scan. Covers both on-disk staging conventions this
codebase uses: `.staging/<op_id>/` (ingestion/validation/integrity) and
`.tmp-<id>/` (the other six stores).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.catalog.repository import CatalogRepository
from app.catalog.scanner import CatalogScanner
from app.core.config import Settings
from app.storage.atomic import create_staging_dir
from app.storage.catalog_store import get_connection
from app.storage.integrity_store import LocalIntegrityReportStore
from app.storage.local import LocalRawStorage
from app.storage.normalized_store import LocalNormalizedArtifactStore
from app.storage.validation_store import LocalValidationReportStore
from tests.crash_safety_helpers import assert_staging_not_discoverable


def _settings(tmp_path: Path) -> Settings:
    return Settings(
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


def _plant_valid_looking_manifest(staging_leaf: Path, filename: str, payload: dict) -> None:
    staging_leaf.mkdir(parents=True, exist_ok=True)
    (staging_leaf / filename).write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# .staging/<operation_id>/ convention: ingestion, validation, integrity
# ---------------------------------------------------------------------------


def test_ingestion_find_manifest_ignores_staging_entry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = LocalRawStorage(root=settings.RAW_STORAGE_ROOT)

    fake_id = "ing_fake"
    staging_leaf = settings.RAW_STORAGE_ROOT / ".staging" / fake_id / "original"
    _plant_valid_looking_manifest(
        settings.RAW_STORAGE_ROOT / ".staging" / fake_id,
        "manifest.json",
        {"ingestion_id": fake_id, "session_id": "s", "customer_id": "c"},
    )
    staging_leaf.mkdir(parents=True, exist_ok=True)

    assert_staging_not_discoverable(lambda: storage.find_manifest(fake_id))


def test_validation_find_reports_ignores_staging_entry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalValidationReportStore(root=settings.VALIDATION_STORAGE_ROOT)

    ingestion_id = "ing_a"
    fake_validation_id = "val_fake"
    _plant_valid_looking_manifest(
        settings.VALIDATION_STORAGE_ROOT / ".staging" / fake_validation_id,
        "report.json",
        {"validation_id": fake_validation_id, "ingestion_id": ingestion_id, "status": "passed"},
    )

    assert store.find_reports(ingestion_id) == []


def test_integrity_find_reports_ignores_staging_entry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalIntegrityReportStore(root=settings.INTEGRITY_STORAGE_ROOT)

    ingestion_id = "ing_a"
    fake_integrity_id = "integ_fake"
    _plant_valid_looking_manifest(
        settings.INTEGRITY_STORAGE_ROOT / ".staging" / fake_integrity_id,
        "report.json",
        {"integrity_id": fake_integrity_id, "ingestion_id": ingestion_id, "status": "passed"},
    )

    assert store.find_reports(ingestion_id) == []


# ---------------------------------------------------------------------------
# .tmp-<id>/ convention: the six already-atomic stores (normalization shown
# as the representative case; scanner test below covers all nine).
# ---------------------------------------------------------------------------


def test_normalization_find_manifest_ignores_tmp_entry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalNormalizedArtifactStore(root=settings.NORMALIZED_STORAGE_ROOT)

    ingestion_id = "ing_a"
    fake_normalization_id = "norm_fake"
    staging = settings.NORMALIZED_STORAGE_ROOT / ingestion_id / f".tmp-{fake_normalization_id}"
    _plant_valid_looking_manifest(staging, "manifest.json", {"normalization_id": fake_normalization_id, "status": "completed"})

    assert store.find_manifest(fake_normalization_id) is None


# ---------------------------------------------------------------------------
# Catalog scan: neither staging convention is ever indexed.
# ---------------------------------------------------------------------------


def test_catalog_scan_ignores_both_staging_conventions(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    # .staging/<op_id>/ convention (ingestion-style)
    _plant_valid_looking_manifest(
        settings.RAW_STORAGE_ROOT / ".staging" / "ing_fake",
        "manifest.json",
        {"ingestion_id": "ing_fake", "session_id": "s", "customer_id": "c", "sha256": "0" * 64, "size_bytes": 1, "original_filename": "x.csv", "ingested_at": "2026-01-01T00:00:00Z"},
    )
    # .tmp-<id>/ convention (normalization-style)
    _plant_valid_looking_manifest(
        settings.NORMALIZED_STORAGE_ROOT / "ing_real" / ".tmp-norm_fake",
        "manifest.json",
        {"normalization_id": "norm_fake", "ingestion_id": "ing_real", "status": "completed"},
    )

    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = CatalogRepository(conn)
    with repo.transaction():
        outcome = CatalogScanner(settings).scan(repo, strict=False)

    assert outcome.inserted == 0
    assert repo.count_artifacts() == 0
    assert repo.get_artifact("ingestion", "ing_fake") is None
    assert repo.get_artifact("normalization", "norm_fake") is None


def test_malformed_staging_metadata_does_not_break_scan_or_lookup(tmp_path: Path) -> None:
    """A corrupted staging_state.json (or any other garbage file dropped
    under .staging/.tmp-) must never crash discovery -- it's simply
    invisible, exactly like a well-formed staging entry."""
    settings = _settings(tmp_path)
    garbage_dir = settings.RAW_STORAGE_ROOT / ".staging" / "ing_garbage"
    garbage_dir.mkdir(parents=True)
    (garbage_dir / "staging_state.json").write_text("{not valid json", encoding="utf-8")
    (garbage_dir / "manifest.json").write_text("also not valid json{", encoding="utf-8")

    storage = LocalRawStorage(root=settings.RAW_STORAGE_ROOT)
    assert storage.find_manifest("ing_garbage") is None

    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = CatalogRepository(conn)
    with repo.transaction():
        outcome = CatalogScanner(settings).scan(repo, strict=False)
    assert outcome.inserted == 0


def test_staging_only_ingestion_cannot_pass_a_downstream_lineage_gate(client, tmp_path: Path) -> None:
    """An ingestion that only exists as an abandoned staging entry (never
    committed) must be rejected by a downstream stage's lineage gate
    exactly like a nonexistent ingestion_id — never silently accepted."""
    from app.core.config import Settings, get_settings
    from app.main import app

    settings: Settings = app.dependency_overrides[get_settings]()
    fake_ingestion_id = "ing_staging_only"
    _plant_valid_looking_manifest(
        settings.RAW_STORAGE_ROOT / ".staging" / fake_ingestion_id,
        "manifest.json",
        {"ingestion_id": fake_ingestion_id, "session_id": "s", "customer_id": "c"},
    )

    response = client.post(
        f"/api/v1/validation/{fake_ingestion_id}",
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )
    assert response.status_code == 404


def test_stale_staging_entry_remains_invisible_to_discovery(tmp_path: Path) -> None:
    """An old (stale-by-time) staging entry is a recovery concern, not a
    discovery concern -- it must stay exactly as invisible as a fresh one."""
    settings = _settings(tmp_path)
    staging = settings.NORMALIZED_STORAGE_ROOT / "ing_a" / ".tmp-norm_stale"
    create_staging_dir(
        staging, operation_id="norm_stale", artifact_id="norm_stale", stage="normalization",
        final_destination=settings.NORMALIZED_STORAGE_ROOT / "ing_a" / "norm_stale",
    )
    # Backdate started_at far beyond any plausible staleness threshold.
    state_path = staging / "staging_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["started_at"] = "2000-01-01T00:00:00+00:00"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    store = LocalNormalizedArtifactStore(root=settings.NORMALIZED_STORAGE_ROOT)
    assert store.find_manifest("norm_stale") is None
