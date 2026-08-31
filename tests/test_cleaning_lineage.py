"""Tests proving cleaning lineage and immutability of every upstream
artifact: synchronized/normalized/raw artifacts and every report must all
be byte-identical before and after a cleaning run — Step 6 is additive only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

CLEAN_URL = "/api/v1/cleaning"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,0.1,0.2,9.8\n" for i in range(5)
)
GPS_CSV = "timestamp,latitude,longitude\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,34.020{i},-118.285{i}\n" for i in range(5)
)


def _upload(client: TestClient, filename: str, content: str, **fields) -> dict:
    response = client.post(
        "/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=fields
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


def _setup(client: TestClient, session_id: str = "sess_lineage_clean") -> dict:
    imu = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    sync = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": imu["normalization"]["normalization_id"]},
                {"name": "gps", "normalization_id": gps["normalization"]["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 500},
        },
    ).json()
    return {"imu": imu, "gps": gps, "sync": sync}


def _clean(client: TestClient, synchronization_id: str) -> dict:
    request = {
        "policy_name": "default_multimodal",
        "policy_version": "1.0.0",
        "config": {"required_streams": ["imu"], "duplicate_policy": {"enabled": True}},
    }
    response = client.post(f"{CLEAN_URL}/{synchronization_id}", json=request)
    assert response.status_code == 200, response.text
    return response.json()


def test_manifest_contains_synchronization_id(client: TestClient, cleaned_root: Path) -> None:
    setup = _setup(client)
    body = _clean(client, setup["sync"]["synchronization_id"])
    manifest = json.loads((cleaned_root / setup["sync"]["synchronization_id"] / body["cleaning_id"] / "manifest.json").read_text())
    assert manifest["synchronization_id"] == setup["sync"]["synchronization_id"]


def test_manifest_contains_source_synchronized_checksum(client: TestClient, cleaned_root: Path) -> None:
    setup = _setup(client)
    body = _clean(client, setup["sync"]["synchronization_id"])
    manifest = json.loads((cleaned_root / setup["sync"]["synchronization_id"] / body["cleaning_id"] / "manifest.json").read_text())
    assert manifest["source_synchronized_sha256"] == setup["sync"]["synchronized_sha256"]


def test_manifest_contains_policy_name_and_version(client: TestClient, cleaned_root: Path) -> None:
    setup = _setup(client)
    body = _clean(client, setup["sync"]["synchronization_id"])
    manifest = json.loads((cleaned_root / setup["sync"]["synchronization_id"] / body["cleaning_id"] / "manifest.json").read_text())
    assert manifest["policy"] == {"name": "default_multimodal", "version": "1.0.0"}


def test_manifest_contains_config_hash(client: TestClient, cleaned_root: Path) -> None:
    setup = _setup(client)
    body = _clean(client, setup["sync"]["synchronization_id"])
    manifest = json.loads((cleaned_root / setup["sync"]["synchronization_id"] / body["cleaning_id"] / "manifest.json").read_text())
    assert manifest["cleaning_config_hash"]
    assert len(manifest["cleaning_config_hash"]) == 64


def test_manifest_contains_upstream_normalization_and_ingestion_ids(client: TestClient, cleaned_root: Path) -> None:
    setup = _setup(client)
    body = _clean(client, setup["sync"]["synchronization_id"])
    manifest = json.loads((cleaned_root / setup["sync"]["synchronization_id"] / body["cleaning_id"] / "manifest.json").read_text())

    by_name = {s["name"]: s for s in manifest["streams"]}
    assert by_name["imu"]["normalization_id"] == setup["imu"]["normalization"]["normalization_id"]
    assert by_name["imu"]["ingestion_id"] == setup["imu"]["ingestion"]["ingestion_id"]
    assert by_name["gps"]["normalization_id"] == setup["gps"]["normalization"]["normalization_id"]
    assert by_name["imu"]["session_id"] == by_name["gps"]["session_id"] == "sess_lineage_clean"


def test_cleaned_artifact_checksum_matches_manifest(client: TestClient, cleaned_root: Path) -> None:
    setup = _setup(client)
    body = _clean(client, setup["sync"]["synchronization_id"])

    artifact_dir = cleaned_root / setup["sync"]["synchronization_id"] / body["cleaning_id"]
    artifact_bytes = (artifact_dir / "cleaned.jsonl").read_bytes()
    manifest = json.loads((artifact_dir / "manifest.json").read_text())

    actual_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    assert actual_sha256 == manifest["cleaned_sha256"]
    assert actual_sha256 == body["cleaned_sha256"]
    assert manifest["cleaned_size_bytes"] == len(artifact_bytes)


def test_synchronized_artifact_unchanged_after_cleaning(client: TestClient) -> None:
    setup = _setup(client)
    sync_artifact_path = Path(setup["sync"]["artifact_uri"].replace("file://", ""))
    bytes_before = sync_artifact_path.read_bytes()

    _clean(client, setup["sync"]["synchronization_id"])

    assert sync_artifact_path.read_bytes() == bytes_before


def test_synchronization_manifest_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    sync_manifest_path = Path(setup["sync"]["artifact_uri"].replace("file://", "")).parent / "manifest.json"
    manifest_before = sync_manifest_path.read_text()

    _clean(client, setup["sync"]["synchronization_id"])

    assert sync_manifest_path.read_text() == manifest_before


def test_normalized_inputs_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    imu_artifact_path = Path(setup["imu"]["normalization"]["artifact_uri"].replace("file://", ""))
    gps_artifact_path = Path(setup["gps"]["normalization"]["artifact_uri"].replace("file://", ""))
    imu_before = imu_artifact_path.read_bytes()
    gps_before = gps_artifact_path.read_bytes()

    _clean(client, setup["sync"]["synchronization_id"])

    assert imu_artifact_path.read_bytes() == imu_before
    assert gps_artifact_path.read_bytes() == gps_before


def test_raw_files_unchanged(client: TestClient, storage_root: Path) -> None:
    setup = _setup(client)
    ingestion = setup["imu"]["ingestion"]
    raw_path = (
        storage_root / ingestion["customer_id"] / ingestion["session_id"]
        / ingestion["ingestion_id"] / "original" / "imu.csv"
    )
    raw_before = raw_path.read_bytes()

    _clean(client, setup["sync"]["synchronization_id"])

    assert raw_path.read_bytes() == raw_before


def test_validation_reports_unchanged(client: TestClient, validation_root: Path) -> None:
    setup = _setup(client)
    ingestion = setup["imu"]["ingestion"]
    validation = setup["imu"]["validation"]
    report_path = validation_root / ingestion["ingestion_id"] / validation["validation_id"] / "report.json"
    report_before = report_path.read_text()

    _clean(client, setup["sync"]["synchronization_id"])

    assert report_path.read_text() == report_before


def test_integrity_reports_unchanged(client: TestClient, integrity_root: Path) -> None:
    setup = _setup(client)
    ingestion = setup["imu"]["ingestion"]
    integrity = setup["imu"]["integrity"]
    report_path = integrity_root / ingestion["ingestion_id"] / integrity["integrity_id"] / "report.json"
    report_before = report_path.read_text()

    _clean(client, setup["sync"]["synchronization_id"])

    assert report_path.read_text() == report_before
