"""Tests proving transformation lineage and immutability of every upstream
artifact: cleaned/synchronized/normalized/raw artifacts and every report
must all be byte-identical before and after a transformation run — Step 7
is additive only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

XFORM_URL = "/api/v1/transformation"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,0.{i},0.2,9.8,0.01,0.02,0.03\n" for i in range(20)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,34.02{i:02d},-118.28{i:02d},100.0,9.{i}\n" for i in range(0, 20, 4)
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


def _setup(client: TestClient, session_id: str = "sess_lineage_xform") -> dict:
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
    cleaned = client.post(
        f"/api/v1/cleaning/{sync['synchronization_id']}",
        json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}},
    ).json()
    return {"imu": imu, "gps": gps, "sync": sync, "cleaned": cleaned}


def _transform(client: TestClient, cleaning_id: str) -> dict:
    request = {
        "profile_name": "multimodal_window_v1",
        "profile_version": "1.0.0",
        "config": {
            "window": {"mode": "count", "size": 10, "stride": 5, "drop_incomplete": True},
            "features": {"imu": {"include_raw": True, "statistics": ["mean"]}},
        },
    }
    response = client.post(f"{XFORM_URL}/{cleaning_id}", json=request)
    assert response.status_code == 200, response.text
    return response.json()


def test_manifest_contains_cleaning_id_and_synchronization_id(client: TestClient, transformed_root: Path) -> None:
    setup = _setup(client)
    body = _transform(client, setup["cleaned"]["cleaning_id"])
    manifest = json.loads(
        (transformed_root / setup["cleaned"]["cleaning_id"] / body["transformation_id"] / "manifest.json").read_text()
    )
    assert manifest["cleaning_id"] == setup["cleaned"]["cleaning_id"]
    assert manifest["upstream"]["synchronization_id"] == setup["sync"]["synchronization_id"]


def test_manifest_contains_source_cleaned_checksum(client: TestClient, transformed_root: Path) -> None:
    setup = _setup(client)
    body = _transform(client, setup["cleaned"]["cleaning_id"])
    manifest = json.loads(
        (transformed_root / setup["cleaned"]["cleaning_id"] / body["transformation_id"] / "manifest.json").read_text()
    )
    assert manifest["source_cleaned_sha256"] == setup["cleaned"]["cleaned_sha256"]


def test_manifest_contains_profile_and_config_hash(client: TestClient, transformed_root: Path) -> None:
    setup = _setup(client)
    body = _transform(client, setup["cleaned"]["cleaning_id"])
    manifest = json.loads(
        (transformed_root / setup["cleaned"]["cleaning_id"] / body["transformation_id"] / "manifest.json").read_text()
    )
    assert manifest["profile"] == {"name": "multimodal_window_v1", "version": "1.0.0"}
    assert len(manifest["transformation_config_hash"]) == 64


def test_manifest_contains_upstream_cleaning_policy_and_session_ids(client: TestClient, transformed_root: Path) -> None:
    setup = _setup(client)
    body = _transform(client, setup["cleaned"]["cleaning_id"])
    manifest = json.loads(
        (transformed_root / setup["cleaned"]["cleaning_id"] / body["transformation_id"] / "manifest.json").read_text()
    )
    assert manifest["upstream"]["cleaning_policy"] == {"name": "default_multimodal", "version": "1.0.0"}
    assert "sess_lineage_xform" in manifest["upstream"]["session_ids"]
    assert setup["imu"]["normalization"]["normalization_id"] in manifest["upstream"]["normalization_ids"]


def test_transformed_artifact_checksum_matches_manifest(client: TestClient, transformed_root: Path) -> None:
    setup = _setup(client)
    body = _transform(client, setup["cleaned"]["cleaning_id"])
    artifact_dir = transformed_root / setup["cleaned"]["cleaning_id"] / body["transformation_id"]
    artifact_bytes = (artifact_dir / "transformed.jsonl").read_bytes()
    manifest = json.loads((artifact_dir / "manifest.json").read_text())

    actual_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    assert actual_sha256 == manifest["transformed_sha256"]
    assert actual_sha256 == body["transformed_sha256"]
    assert manifest["transformed_size_bytes"] == len(artifact_bytes)


def test_cleaned_artifact_unchanged_after_transformation(client: TestClient) -> None:
    setup = _setup(client)
    cleaned_artifact_path = Path(setup["cleaned"]["artifact_uri"].replace("file://", ""))
    bytes_before = cleaned_artifact_path.read_bytes()

    _transform(client, setup["cleaned"]["cleaning_id"])

    assert cleaned_artifact_path.read_bytes() == bytes_before


def test_cleaning_manifest_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    cleaning_manifest_path = Path(setup["cleaned"]["artifact_uri"].replace("file://", "")).parent / "manifest.json"
    manifest_before = cleaning_manifest_path.read_text()

    _transform(client, setup["cleaned"]["cleaning_id"])

    assert cleaning_manifest_path.read_text() == manifest_before


def test_cleaning_report_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    report_path = Path(setup["cleaned"]["report_uri"].replace("file://", ""))
    report_before = report_path.read_text()

    _transform(client, setup["cleaned"]["cleaning_id"])

    assert report_path.read_text() == report_before


def test_synchronized_artifact_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    sync_artifact_path = Path(setup["sync"]["artifact_uri"].replace("file://", ""))
    bytes_before = sync_artifact_path.read_bytes()

    _transform(client, setup["cleaned"]["cleaning_id"])

    assert sync_artifact_path.read_bytes() == bytes_before


def test_synchronization_manifest_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    sync_manifest_path = Path(setup["sync"]["artifact_uri"].replace("file://", "")).parent / "manifest.json"
    manifest_before = sync_manifest_path.read_text()

    _transform(client, setup["cleaned"]["cleaning_id"])

    assert sync_manifest_path.read_text() == manifest_before


def test_normalized_artifacts_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    imu_artifact_path = Path(setup["imu"]["normalization"]["artifact_uri"].replace("file://", ""))
    gps_artifact_path = Path(setup["gps"]["normalization"]["artifact_uri"].replace("file://", ""))
    imu_before = imu_artifact_path.read_bytes()
    gps_before = gps_artifact_path.read_bytes()

    _transform(client, setup["cleaned"]["cleaning_id"])

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

    _transform(client, setup["cleaned"]["cleaning_id"])

    assert raw_path.read_bytes() == raw_before


def test_validation_reports_unchanged(client: TestClient, validation_root: Path) -> None:
    setup = _setup(client)
    ingestion = setup["imu"]["ingestion"]
    validation = setup["imu"]["validation"]
    report_path = validation_root / ingestion["ingestion_id"] / validation["validation_id"] / "report.json"
    report_before = report_path.read_text()

    _transform(client, setup["cleaned"]["cleaning_id"])

    assert report_path.read_text() == report_before


def test_integrity_reports_unchanged(client: TestClient, integrity_root: Path) -> None:
    setup = _setup(client)
    ingestion = setup["imu"]["ingestion"]
    integrity = setup["imu"]["integrity"]
    report_path = integrity_root / ingestion["ingestion_id"] / integrity["integrity_id"] / "report.json"
    report_before = report_path.read_text()

    _transform(client, setup["cleaned"]["cleaning_id"])

    assert report_path.read_text() == report_before


def test_existing_transformation_run_cannot_be_overwritten(client: TestClient, transformed_root: Path) -> None:
    setup = _setup(client)
    body = _transform(client, setup["cleaned"]["cleaning_id"])
    final_dir = transformed_root / setup["cleaned"]["cleaning_id"] / body["transformation_id"]
    assert final_dir.exists()
    # A second transformation run gets its own fresh transformation_id
    # (UUID4), so it can never collide with or overwrite the first.
    body2 = _transform(client, setup["cleaned"]["cleaning_id"])
    assert body2["transformation_id"] != body["transformation_id"]
    assert (transformed_root / setup["cleaned"]["cleaning_id"] / body2["transformation_id"]).exists()
