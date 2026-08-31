"""Tests for rebuild semantics: strict broken-lineage rejection, dataset/
version preservation across rebuild, and full reconstruction after
deleting catalog.db entirely."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import CatalogRepository
from app.catalog.scanner import BrokenLineageError, CatalogScanner
from app.core.config import Settings
from app.storage.catalog_store import get_connection

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,0.{i%10},0.2,9.8,0.01,0.02,0.03\n" for i in range(40)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,34.02{i%90:02d},-118.28{i%90:02d},100.0,9.{i%9}\n" for i in range(0, 40, 3)
)


def _upload(client: TestClient, filename: str, content: str, **fields) -> dict:
    response = client.post("/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=fields)
    assert response.status_code == 201, response.text
    return response.json()


def _pipeline(client: TestClient, filename, content, schema_name, profile_name, source_units, **fields) -> dict:
    ingestion = _upload(client, filename, content, **fields)
    for path, body in (
        (f"/api/v1/validation/{ingestion['ingestion_id']}", {"schema_name": schema_name, "schema_version": "1.0.0"}),
        (f"/api/v1/integrity/{ingestion['ingestion_id']}", {"schema_name": schema_name, "schema_version": "1.0.0"}),
    ):
        r = client.post(path, json=body)
        assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/normalization/{ingestion['ingestion_id']}",
        json={"schema_name": schema_name, "schema_version": "1.0.0", "profile_name": profile_name, "profile_version": "1.0.0", "source_units": source_units},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _full_pipeline(client: TestClient, session_id: str = "sess_rebuild") -> dict:
    imu = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    sync = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [{"name": "imu", "normalization_id": imu["normalization_id"]}, {"name": "gps", "normalization_id": gps["normalization_id"]}],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 400},
        },
    ).json()
    cleaned = client.post(
        f"/api/v1/cleaning/{sync['synchronization_id']}",
        json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}},
    ).json()
    xform = client.post(
        f"/api/v1/transformation/{cleaned['cleaning_id']}",
        json={"profile_name": "multimodal_window_v1", "profile_version": "1.0.0", "config": {"window": {"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True}}},
    ).json()
    qc = client.post(
        f"/api/v1/qc/{xform['transformation_id']}",
        json={"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}},
    ).json()
    pkg = client.post(
        f"/api/v1/packaging/{xform['transformation_id']}",
        json={
            "qc_id": qc["qc_id"], "profile_name": "default_ml_package", "profile_version": "1.0.0",
            "config": {"split": {"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}, "grouping": {"mode": "source_overlap"}, "exports": ["jsonl"]},
        },
    ).json()
    return {"imu": imu, "gps": gps, "sync": sync, "cleaned": cleaned, "xform": xform, "qc": qc, "pkg": pkg}


def test_rebuild_works(client: TestClient) -> None:
    _full_pipeline(client)
    response = client.post("/api/v1/catalog/rebuild")
    assert response.status_code == 200
    assert response.json()["artifacts_registered"] == 13


def test_rebuild_repeat_produces_same_logical_catalog(client: TestClient) -> None:
    _full_pipeline(client)
    r1 = client.post("/api/v1/catalog/rebuild").json()
    r2 = client.post("/api/v1/catalog/rebuild").json()
    assert r1["artifacts_registered"] == r2["artifacts_registered"]
    assert r1["edges_registered"] == r2["edges_registered"]

    artifacts1 = client.get("/api/v1/catalog/artifacts").json()
    artifacts2 = client.get("/api/v1/catalog/artifacts").json()
    assert artifacts1 == artifacts2


def test_rebuilt_lineage_matches_original(client: TestClient) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    lineage_before = client.get(f"/api/v1/lineage/package/{setup['pkg']['package_id']}").json()
    client.post("/api/v1/catalog/rebuild")
    lineage_after = client.get(f"/api/v1/lineage/package/{setup['pkg']['package_id']}").json()
    assert lineage_before["nodes"] == lineage_after["nodes"]
    assert lineage_before["edges"] == lineage_after["edges"]


def test_dataset_registrations_survive_rebuild(client: TestClient) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    client.post("/api/v1/datasets", json={"dataset_name": "robotics_demo"})
    client.post("/api/v1/datasets/robotics_demo/versions", json={"version": "1.0.0", "package_id": setup["pkg"]["package_id"]})

    response = client.post("/api/v1/catalog/rebuild")
    assert response.json()["datasets_preserved"] == 1
    assert response.json()["dataset_versions_preserved"] == 1

    versions = client.get("/api/v1/datasets/robotics_demo/versions").json()
    assert [v["version"] for v in versions] == ["1.0.0"]
    assert versions[0]["package_id"] == setup["pkg"]["package_id"]


def test_catalog_rebuild_works_after_db_deletion(client: TestClient, catalog_db_path: Path) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    assert catalog_db_path.exists()

    catalog_db_path.unlink()
    response = client.post("/api/v1/catalog/rebuild")
    assert response.status_code == 200
    assert response.json()["artifacts_registered"] == 13

    artifact = client.get(f"/api/v1/catalog/artifacts/package/{setup['pkg']['package_id']}")
    assert artifact.status_code == 200


def test_broken_dataset_version_reference_reported_by_health(client: TestClient, package_root: Path, transformed_root: Path) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    client.post("/api/v1/datasets", json={"dataset_name": "robotics_demo"})
    client.post("/api/v1/datasets/robotics_demo/versions", json={"version": "1.0.0", "package_id": setup["pkg"]["package_id"]})

    # Simulate the package artifact disappearing from the filesystem entirely
    # (e.g. manual deletion) and rebuild the artifact index -- the dataset
    # version record must survive, but health must flag the dangling reference.
    shutil.rmtree(package_root / setup["xform"]["transformation_id"] / setup["pkg"]["package_id"])
    client.post("/api/v1/catalog/rebuild")

    health = client.get("/api/v1/catalog/health").json()
    assert health["status"] == "degraded"
    assert any(i["code"] == "BROKEN_DATASET_VERSION_REFERENCE" for i in health["issues"])

    # The version record itself must NOT have been silently deleted.
    versions = client.get("/api/v1/datasets/robotics_demo/versions").json()
    assert len(versions) == 1


def test_missing_parent_detected_non_strict(tmp_path: Path) -> None:
    """A manifest referencing a non-existent parent is registered (its own
    metadata is valid) but recorded as a MISSING_LINEAGE_PARENT issue,
    never silently dropped, in non-strict scan mode."""
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
    import json

    validation_dir = settings.VALIDATION_STORAGE_ROOT / "ing_orphan" / "val_orphan"
    validation_dir.mkdir(parents=True)
    (validation_dir / "report.json").write_text(
        json.dumps({"validation_id": "val_orphan", "ingestion_id": "ing_orphan", "status": "passed", "validated_at": "2026-01-01T00:00:00Z"})
    )

    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = CatalogRepository(conn)
    scanner = CatalogScanner(settings)
    with repo.transaction():
        outcome = scanner.scan(repo, strict=False)

    assert repo.get_artifact("validation", "val_orphan") is not None
    assert len(outcome.issues) == 1
    assert outcome.issues[0]["issue_code"] == "MISSING_LINEAGE_PARENT"


def test_strict_rebuild_rejects_broken_lineage(tmp_path: Path) -> None:
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
    import json

    validation_dir = settings.VALIDATION_STORAGE_ROOT / "ing_orphan" / "val_orphan"
    validation_dir.mkdir(parents=True)
    (validation_dir / "report.json").write_text(
        json.dumps({"validation_id": "val_orphan", "ingestion_id": "ing_orphan", "status": "passed", "validated_at": "2026-01-01T00:00:00Z"})
    )

    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = CatalogRepository(conn)
    scanner = CatalogScanner(settings)
    with pytest.raises(BrokenLineageError):
        with repo.transaction():
            scanner.scan(repo, strict=True)

    # Rollback must leave the catalog empty (no half-written state).
    assert repo.count_artifacts() == 0


def test_failed_strict_rebuild_leaves_prior_catalog_intact(client: TestClient, validation_root: Path) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    before = client.get("/api/v1/catalog/artifacts").json()

    # Corrupt lineage by writing a validation report with a bogus ingestion_id.
    stray_dir = validation_root / "ing_bogus" / "val_bogus"
    stray_dir.mkdir(parents=True)
    (stray_dir / "report.json").write_text(
        '{"validation_id": "val_bogus", "ingestion_id": "ing_bogus", "status": "passed", "validated_at": "2026-01-01T00:00:00Z"}'
    )

    response = client.post("/api/v1/catalog/rebuild")
    assert response.status_code == 500

    after = client.get("/api/v1/catalog/artifacts").json()
    assert before == after
