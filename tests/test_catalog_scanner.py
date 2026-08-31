"""Tests for CatalogScanner: manifest discovery, staging-dir/.gitkeep
skipping, and idempotent re-scans, against a real pipeline run."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.catalog.models import ArtifactType
from app.catalog.repository import CatalogRepository
from app.catalog.scanner import CatalogScanner
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


def _build_pipeline(client: TestClient, session_id: str = "sess_scanner") -> dict:
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


def _scan(test_settings: Settings, catalog_db_path: Path) -> tuple[CatalogRepository, object]:
    conn = get_connection(catalog_db_path)
    repo = CatalogRepository(conn)
    scanner = CatalogScanner(test_settings)
    with repo.transaction():
        outcome = scanner.scan(repo, strict=False)
    return repo, outcome


def test_ingestion_indexed(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    _build_pipeline(client)
    repo, _ = _scan(test_settings, catalog_db_path)
    assert len(repo.list_artifacts(artifact_type="ingestion")) == 2


def test_validation_indexed(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    _build_pipeline(client)
    repo, _ = _scan(test_settings, catalog_db_path)
    assert len(repo.list_artifacts(artifact_type="validation")) == 2


def test_integrity_indexed(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    _build_pipeline(client)
    repo, _ = _scan(test_settings, catalog_db_path)
    assert len(repo.list_artifacts(artifact_type="integrity")) == 2


def test_normalization_indexed(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    _build_pipeline(client)
    repo, _ = _scan(test_settings, catalog_db_path)
    assert len(repo.list_artifacts(artifact_type="normalization")) == 2


def test_synchronization_indexed(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    setup = _build_pipeline(client)
    repo, _ = _scan(test_settings, catalog_db_path)
    artifact = repo.get_artifact("synchronization", setup["sync"]["synchronization_id"])
    assert artifact is not None
    assert artifact["content_sha256"] == setup["sync"]["synchronized_sha256"]


def test_cleaning_indexed(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    setup = _build_pipeline(client)
    repo, _ = _scan(test_settings, catalog_db_path)
    assert repo.get_artifact("cleaning", setup["cleaned"]["cleaning_id"]) is not None


def test_transformation_indexed(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    setup = _build_pipeline(client)
    repo, _ = _scan(test_settings, catalog_db_path)
    assert repo.get_artifact("transformation", setup["xform"]["transformation_id"]) is not None


def test_qc_indexed(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    setup = _build_pipeline(client)
    repo, _ = _scan(test_settings, catalog_db_path)
    artifact = repo.get_artifact("qc", setup["qc"]["qc_id"])
    assert artifact is not None
    assert artifact["status"] == "passed"


def test_package_indexed(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    setup = _build_pipeline(client)
    repo, _ = _scan(test_settings, catalog_db_path)
    artifact = repo.get_artifact("package", setup["pkg"]["package_id"])
    assert artifact is not None
    assert artifact["status"] == "completed"


def test_direct_edges_created(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    setup = _build_pipeline(client)
    repo, _ = _scan(test_settings, catalog_db_path)

    # ingestion -> validation
    validations = repo.list_artifacts(artifact_type="validation")
    for v in validations:
        parents = repo.get_parents("validation", v["artifact_id"])
        assert len(parents) == 1
        assert parents[0]["parent_artifact_type"] == "ingestion"
        assert parents[0]["relationship"] == "validated_from"

    # validation -> integrity
    for i in repo.list_artifacts(artifact_type="integrity"):
        parents = repo.get_parents("integrity", i["artifact_id"])
        assert parents[0]["parent_artifact_type"] == "validation"
        assert parents[0]["relationship"] == "checked_from"

    # integrity -> normalization
    for n in repo.list_artifacts(artifact_type="normalization"):
        parents = repo.get_parents("normalization", n["artifact_id"])
        assert parents[0]["parent_artifact_type"] == "integrity"
        assert parents[0]["relationship"] == "normalized_from"

    # normalization(s) -> synchronization: multiple parents
    sync_parents = repo.get_parents("synchronization", setup["sync"]["synchronization_id"])
    assert len(sync_parents) == 2
    assert all(p["parent_artifact_type"] == "normalization" and p["relationship"] == "synchronized_from" for p in sync_parents)

    # synchronization -> cleaning
    clean_parents = repo.get_parents("cleaning", setup["cleaned"]["cleaning_id"])
    assert clean_parents[0] == {"parent_artifact_type": "synchronization", "parent_artifact_id": setup["sync"]["synchronization_id"], "child_artifact_type": "cleaning", "child_artifact_id": setup["cleaned"]["cleaning_id"], "relationship": "cleaned_from"}

    # cleaning -> transformation
    xform_parents = repo.get_parents("transformation", setup["xform"]["transformation_id"])
    assert xform_parents[0]["parent_artifact_type"] == "cleaning"
    assert xform_parents[0]["relationship"] == "transformed_from"

    # transformation -> qc
    qc_parents = repo.get_parents("qc", setup["qc"]["qc_id"])
    assert qc_parents[0]["parent_artifact_type"] == "transformation"
    assert qc_parents[0]["relationship"] == "qc_of"

    # transformation -> package AND qc -> package
    pkg_parents = repo.get_parents("package", setup["pkg"]["package_id"])
    relationships = {(p["parent_artifact_type"], p["relationship"]) for p in pkg_parents}
    assert relationships == {("transformation", "packaged_from"), ("qc", "approved_by_qc")}


def test_scan_idempotent(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    _build_pipeline(client)
    repo, outcome1 = _scan(test_settings, catalog_db_path)
    with repo.transaction():
        outcome2 = CatalogScanner(test_settings).scan(repo, strict=False)
    assert outcome2.inserted == 0
    assert outcome2.unchanged == outcome1.inserted
    assert outcome2.edges_inserted == 0


def test_scanner_ignores_staging_and_gitkeep(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    setup = _build_pipeline(client)
    (test_settings.TRANSFORMED_STORAGE_ROOT).mkdir(parents=True, exist_ok=True)
    stray_staging = test_settings.TRANSFORMED_STORAGE_ROOT / setup["cleaned"]["cleaning_id"] / ".tmp-fake123"
    stray_staging.mkdir(parents=True, exist_ok=True)
    (stray_staging / "manifest.json").write_text('{"transformation_id": "xform_fake", "cleaning_id": "does_not_matter"}')
    (test_settings.RAW_STORAGE_ROOT / ".gitkeep").touch()

    repo, outcome = _scan(test_settings, catalog_db_path)
    assert repo.get_artifact("transformation", "xform_fake") is None
