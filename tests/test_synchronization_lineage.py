"""Tests proving synchronization lineage and immutability of every upstream
artifact: raw bytes, raw manifest, validation reports, integrity reports,
and normalized artifacts must all be byte-identical before and after a
synchronization run — Step 5 is additive only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

SYNC_URL = "/api/v1/synchronization"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,0.1,0.2,9.8\n" for i in range(5)
)
GPS_CSV = "timestamp,latitude,longitude\n2026-08-30T18:00:00Z,34.0,-118.0\n2026-08-30T18:00:04Z,34.1,-118.1\n"


def _upload(client: TestClient, filename: str, content: str, **form_fields) -> dict:
    response = client.post(
        "/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=form_fields
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pipeline(client: TestClient, filename, content, schema_name, profile_name, source_units, **fields) -> dict:
    ingestion = _upload(client, filename, content, **fields)
    validation = client.post(
        f"/api/v1/validation/{ingestion['ingestion_id']}",
        json={"schema_name": schema_name, "schema_version": "1.0.0"},
    ).json()
    integrity = client.post(
        f"/api/v1/integrity/{ingestion['ingestion_id']}",
        json={"schema_name": schema_name, "schema_version": "1.0.0"},
    ).json()
    normalization = client.post(
        f"/api/v1/normalization/{ingestion['ingestion_id']}",
        json={
            "schema_name": schema_name,
            "schema_version": "1.0.0",
            "profile_name": profile_name,
            "profile_version": "1.0.0",
            "source_units": source_units,
        },
    ).json()
    return {"ingestion": ingestion, "validation": validation, "integrity": integrity, "normalization": normalization}


def _sync(client: TestClient, imu: dict, gps: dict) -> dict:
    req = {
        "streams": [
            {"name": "imu", "normalization_id": imu["normalization"]["normalization_id"]},
            {"name": "gps", "normalization_id": gps["normalization"]["normalization_id"]},
        ],
        "reference": {"mode": "stream", "stream": "imu"},
        "alignment": {"default_method": "nearest", "max_time_delta_ms": 5000},
    }
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 200, response.text
    return response.json()


def _setup_pair(client: TestClient, session_id: str = "sess_lineage") -> tuple[dict, dict]:
    imu = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    return imu, gps


def test_manifest_contains_every_normalization_id(client: TestClient, synchronized_root: Path) -> None:
    imu, gps = _setup_pair(client)
    body = _sync(client, imu, gps)

    manifest = json.loads((synchronized_root / body["synchronization_id"] / "manifest.json").read_text())
    normalization_ids = {s["normalization_id"] for s in manifest["streams"]}
    assert normalization_ids == {
        imu["normalization"]["normalization_id"],
        gps["normalization"]["normalization_id"],
    }


def test_manifest_contains_normalized_checksums(client: TestClient, synchronized_root: Path) -> None:
    imu, gps = _setup_pair(client)
    body = _sync(client, imu, gps)

    manifest = json.loads((synchronized_root / body["synchronization_id"] / "manifest.json").read_text())
    checksums = {s["name"]: s["normalized_sha256"] for s in manifest["streams"]}
    assert checksums["imu"] == imu["normalization"]["normalized_sha256"]
    assert checksums["gps"] == gps["normalization"]["normalized_sha256"]


def test_manifest_contains_source_ingestion_ids(client: TestClient, synchronized_root: Path) -> None:
    imu, gps = _setup_pair(client)
    body = _sync(client, imu, gps)

    manifest = json.loads((synchronized_root / body["synchronization_id"] / "manifest.json").read_text())
    ingestion_ids = {s["name"]: s["ingestion_id"] for s in manifest["streams"]}
    assert ingestion_ids["imu"] == imu["ingestion"]["ingestion_id"]
    assert ingestion_ids["gps"] == gps["ingestion"]["ingestion_id"]


def test_manifest_contains_session_id(client: TestClient, synchronized_root: Path) -> None:
    imu, gps = _setup_pair(client, session_id="sess_check_123")
    body = _sync(client, imu, gps)

    manifest = json.loads((synchronized_root / body["synchronization_id"] / "manifest.json").read_text())
    for stream in manifest["streams"]:
        assert stream["session_id"] == "sess_check_123"


def test_manifest_contains_validation_and_integrity_ids(client: TestClient, synchronized_root: Path) -> None:
    imu, gps = _setup_pair(client)
    body = _sync(client, imu, gps)

    manifest = json.loads((synchronized_root / body["synchronization_id"] / "manifest.json").read_text())
    lineage = {s["name"]: s for s in manifest["streams"]}
    assert lineage["imu"]["validation_id"] == imu["validation"]["validation_id"]
    assert lineage["imu"]["integrity_id"] == imu["integrity"]["integrity_id"]
    assert lineage["gps"]["validation_id"] == gps["validation"]["validation_id"]
    assert lineage["gps"]["integrity_id"] == gps["integrity"]["integrity_id"]


def test_artifact_checksum_matches_manifest(client: TestClient, synchronized_root: Path) -> None:
    imu, gps = _setup_pair(client)
    body = _sync(client, imu, gps)

    artifact_dir = synchronized_root / body["synchronization_id"]
    artifact_bytes = (artifact_dir / "synchronized.jsonl").read_bytes()
    manifest = json.loads((artifact_dir / "manifest.json").read_text())

    actual_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    assert actual_sha256 == manifest["synchronized_sha256"]
    assert actual_sha256 == body["synchronized_sha256"]
    assert manifest["synchronized_size_bytes"] == len(artifact_bytes)


def test_rows_written_correct(client: TestClient) -> None:
    imu, gps = _setup_pair(client)
    body = _sync(client, imu, gps)
    assert body["rows_written"] == 5  # imu has 5 records, is the reference


def test_metrics_persisted(client: TestClient, synchronized_root: Path) -> None:
    imu, gps = _setup_pair(client)
    body = _sync(client, imu, gps)

    manifest = json.loads((synchronized_root / body["synchronization_id"] / "manifest.json").read_text())
    assert "imu" in manifest["metrics"]
    assert "gps" in manifest["metrics"]
    for name in ("imu", "gps"):
        metrics = manifest["metrics"][name]
        assert "source_records" in metrics
        assert "matched_rows" in metrics
        assert "unmatched_rows" in metrics
        assert "coverage_ratio" in metrics


def test_normalized_artifacts_unchanged_after_synchronization(client: TestClient) -> None:
    imu, gps = _setup_pair(client)
    imu_artifact_path = Path(imu["normalization"]["artifact_uri"].replace("file://", ""))
    gps_artifact_path = Path(gps["normalization"]["artifact_uri"].replace("file://", ""))
    imu_bytes_before = imu_artifact_path.read_bytes()
    gps_bytes_before = gps_artifact_path.read_bytes()

    _sync(client, imu, gps)

    assert imu_artifact_path.read_bytes() == imu_bytes_before
    assert gps_artifact_path.read_bytes() == gps_bytes_before


def test_normalization_manifests_unchanged(client: TestClient) -> None:
    imu, gps = _setup_pair(client)
    imu_manifest_path = Path(imu["normalization"]["artifact_uri"].replace("file://", "")).parent / "manifest.json"
    manifest_before = imu_manifest_path.read_text()

    _sync(client, imu, gps)

    assert imu_manifest_path.read_text() == manifest_before


def test_raw_artifacts_unchanged(client: TestClient, storage_root: Path) -> None:
    imu, gps = _setup_pair(client)
    raw_path = (
        storage_root / imu["ingestion"]["customer_id"] / imu["ingestion"]["session_id"]
        / imu["ingestion"]["ingestion_id"] / "original" / "imu.csv"
    )
    raw_bytes_before = raw_path.read_bytes()

    _sync(client, imu, gps)

    assert raw_path.read_bytes() == raw_bytes_before


def test_validation_reports_unchanged(client: TestClient, validation_root: Path) -> None:
    imu, gps = _setup_pair(client)
    report_path = (
        validation_root / imu["ingestion"]["ingestion_id"] / imu["validation"]["validation_id"] / "report.json"
    )
    report_before = report_path.read_text()

    _sync(client, imu, gps)

    assert report_path.read_text() == report_before


def test_integrity_reports_unchanged(client: TestClient, integrity_root: Path) -> None:
    imu, gps = _setup_pair(client)
    report_path = (
        integrity_root / imu["ingestion"]["ingestion_id"] / imu["integrity"]["integrity_id"] / "report.json"
    )
    report_before = report_path.read_text()

    _sync(client, imu, gps)

    assert report_path.read_text() == report_before
