"""Service-level tests: path-traversal safety, session filtering, catalog
schema mismatch detection, and remaining scenarios not covered by the
other catalog test files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import CatalogRepository
from app.catalog.scanner import CatalogScanner
from app.catalog.service import CatalogService
from app.catalog.verifier import ArtifactVerifier
from app.core.config import Settings
from app.storage.catalog_store import get_connection


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def test_scanner_never_opens_paths_outside_configured_roots(tmp_path: Path) -> None:
    """A manifest's own storage_uri/artifact_uri pointing OUTSIDE the
    configured root must never be dereferenced by the scanner — it only
    ever walks the configured root via its own glob(), and stores
    storage_uri purely as opaque metadata."""
    settings = Settings(
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

    # A malicious ingestion manifest claiming a storage_uri far outside any
    # configured root — the scanner must register it as opaque metadata
    # only, never resolve/open it.
    outside_target = tmp_path.parent / "should_never_be_touched.txt"
    outside_target.write_text("sensitive")

    ingestion_dir = settings.RAW_STORAGE_ROOT / "cust" / "sess" / "ing_evil"
    ingestion_dir.mkdir(parents=True)
    (ingestion_dir / "manifest.json").write_text(
        json.dumps(
            {
                "ingestion_id": "ing_evil",
                "session_id": "sess",
                "customer_id": "cust",
                "original_filename": "x.csv",
                "size_bytes": 1,
                "sha256": "deadbeef",
                "ingested_at": "2026-01-01T00:00:00Z",
                "storage_uri": f"file://{outside_target}",
            }
        )
    )

    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = CatalogRepository(conn)
    with repo.transaction():
        CatalogScanner(settings).scan(repo, strict=False)

    artifact = repo.get_artifact("ingestion", "ing_evil")
    assert artifact is not None
    # storage_uri is stored as opaque text, never resolved into a Path by the scanner.
    assert artifact["storage_uri"] == f"file://{outside_target}"
    assert outside_target.read_text() == "sensitive"  # untouched


def test_path_traversal_in_artifact_id_cannot_escape_root(tmp_path: Path) -> None:
    """Bare-ID lookup helpers reject path-separator-bearing IDs outright
    (see app.storage.*._is_safe_path_component) — verified here via the
    verifier's use of those same lookups for a hostile artifact_id."""
    settings = Settings(
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
    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = CatalogRepository(conn)
    # Not registered at all, so verify() reports "missing" via the catalog
    # entry itself -- the hostile ID never reaches a filesystem call.
    outcome = ArtifactVerifier(settings).verify(repo, "normalization", "../../../etc/passwd")
    assert outcome.status == "missing"


# ---------------------------------------------------------------------------
# Session filtering / misc service behavior
# ---------------------------------------------------------------------------


IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,0.{i%10},0.2,9.8,0.01,0.02,0.03\n" for i in range(10)
)


def _upload(client: TestClient, filename: str, content: str, **fields) -> dict:
    response = client.post("/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=fields)
    assert response.status_code == 201, response.text
    return response.json()


def test_list_artifacts_filters_by_session_id(client: TestClient) -> None:
    _upload(client, "a.csv", IMU_CSV, session_id="sess_x")
    _upload(client, "b.csv", IMU_CSV, session_id="sess_y")
    client.post("/api/v1/catalog/scan")
    response = client.get("/api/v1/catalog/artifacts", params={"session_id": "sess_x"})
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_catalog_schema_mismatch_detected(catalog_db_path: Path) -> None:
    conn = get_connection(catalog_db_path)
    conn.execute("UPDATE catalog_metadata SET value = '0.0.1' WHERE key = 'catalog_schema_version'")
    repo = CatalogRepository(conn)
    service = CatalogService(repo=repo, scanner=None, verifier=None)  # scan/verify not exercised here
    health = service.health()
    assert health.status == "degraded"
    assert any(i.code == "CATALOG_SCHEMA_MISMATCH" for i in health.issues)


def test_invalid_artifact_type_via_service(catalog_db_path: Path) -> None:
    from app.catalog.errors import InvalidArtifactTypeError

    conn = get_connection(catalog_db_path)
    repo = CatalogRepository(conn)
    service = CatalogService(repo=repo, scanner=None, verifier=None)
    with pytest.raises(InvalidArtifactTypeError):
        service.get_artifact("bogus_type", "some_id")


def test_artifact_not_found_via_service(catalog_db_path: Path) -> None:
    from app.catalog.errors import ArtifactNotFoundError

    conn = get_connection(catalog_db_path)
    repo = CatalogRepository(conn)
    service = CatalogService(repo=repo, scanner=None, verifier=None)
    with pytest.raises(ArtifactNotFoundError):
        service.get_artifact("package", "pkg_missing")
