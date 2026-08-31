"""Unit tests for ArtifactVerifier — checksum/manifest verification per
stage, run directly against a real pipeline's storage roots."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.catalog.repository import CatalogRepository
from app.catalog.scanner import CatalogScanner
from app.catalog.verifier import ArtifactVerifier
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


def _full_pipeline(client: TestClient, session_id: str = "sess_verifier") -> dict:
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


def _scanned_repo(test_settings: Settings, catalog_db_path: Path) -> CatalogRepository:
    conn = get_connection(catalog_db_path)
    repo = CatalogRepository(conn)
    with repo.transaction():
        CatalogScanner(test_settings).scan(repo, strict=False)
    return repo


def test_package_verification_succeeds(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    setup = _full_pipeline(client)
    repo = _scanned_repo(test_settings, catalog_db_path)
    outcome = ArtifactVerifier(test_settings).verify(repo, "package", setup["pkg"]["package_id"])
    assert outcome.status == "verified"
    assert any(c.name.startswith("split_") for c in outcome.checks)


def test_normalization_verification_succeeds(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    setup = _full_pipeline(client)
    repo = _scanned_repo(test_settings, catalog_db_path)
    outcome = ArtifactVerifier(test_settings).verify(repo, "normalization", setup["imu"]["normalization_id"])
    assert outcome.status == "verified"


def test_missing_artifact_file_detected(client: TestClient, test_settings: Settings, catalog_db_path: Path, transformed_root: Path) -> None:
    setup = _full_pipeline(client)
    repo = _scanned_repo(test_settings, catalog_db_path)
    matches = list(transformed_root.glob(f"*/{setup['xform']['transformation_id']}/transformed.jsonl"))
    original = matches[0].read_bytes()
    matches[0].unlink()
    try:
        outcome = ArtifactVerifier(test_settings).verify(repo, "transformation", setup["xform"]["transformation_id"])
        assert outcome.status == "missing"
    finally:
        matches[0].write_bytes(original)


def test_checksum_mismatch_detected(client: TestClient, test_settings: Settings, catalog_db_path: Path, transformed_root: Path) -> None:
    setup = _full_pipeline(client)
    repo = _scanned_repo(test_settings, catalog_db_path)
    matches = list(transformed_root.glob(f"*/{setup['xform']['transformation_id']}/transformed.jsonl"))
    original = matches[0].read_bytes()
    matches[0].write_bytes(original + b"tampered")
    try:
        outcome = ArtifactVerifier(test_settings).verify(repo, "transformation", setup["xform"]["transformation_id"])
        assert outcome.status == "checksum_mismatch"
    finally:
        matches[0].write_bytes(original)


def test_manifest_checksum_mismatch_detected(client: TestClient, test_settings: Settings, catalog_db_path: Path, transformed_root: Path) -> None:
    setup = _full_pipeline(client)
    repo = _scanned_repo(test_settings, catalog_db_path)
    matches = list(transformed_root.glob(f"*/{setup['xform']['transformation_id']}/manifest.json"))
    original = matches[0].read_bytes()
    matches[0].write_bytes(original + b" ")
    try:
        outcome = ArtifactVerifier(test_settings).verify(repo, "transformation", setup["xform"]["transformation_id"])
        assert outcome.status == "manifest_mismatch"
    finally:
        matches[0].write_bytes(original)


def test_missing_artifact_returns_missing_status(test_settings: Settings, catalog_db_path: Path) -> None:
    conn = get_connection(catalog_db_path)
    repo = CatalogRepository(conn)
    outcome = ArtifactVerifier(test_settings).verify(repo, "package", "pkg_does_not_exist")
    assert outcome.status == "missing"


def test_recursive_verification_does_not_loop(client: TestClient, test_settings: Settings, catalog_db_path: Path) -> None:
    """Verifying every upstream node exactly once even though the DAG has
    branching parents (two normalization streams) — never double-counts
    or infinitely loops."""
    setup = _full_pipeline(client)
    repo = _scanned_repo(test_settings, catalog_db_path)
    from app.catalog import graph

    nodes, _ = graph.traverse(repo, root_type="package", root_id=setup["pkg"]["package_id"], direction="upstream")
    assert len(nodes) == len({(n["artifact_type"], n["artifact_id"]) for n in nodes})
