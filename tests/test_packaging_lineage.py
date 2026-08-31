"""Tests proving packaging lineage and immutability of every upstream
artifact: transformed/transformation, QC, cleaned, synchronized,
normalized, raw, and every report must all be byte-identical before and
after a packaging run — Step 9 is additive and report-only.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

PKG_URL = "/api/v1/packaging"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,0.{i%10},0.2,9.8,0.01,0.02,0.03\n" for i in range(120)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,34.02{i%90:02d},-118.28{i%90:02d},100.0,9.{i%9}\n" for i in range(0, 120, 3)
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


def _setup(client: TestClient, session_id: str = "sess_lineage_pkg") -> dict:
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
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 400},
        },
    ).json()
    cleaned = client.post(
        f"/api/v1/cleaning/{sync['synchronization_id']}",
        json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}},
    ).json()
    xform = client.post(
        f"/api/v1/transformation/{cleaned['cleaning_id']}",
        json={
            "profile_name": "multimodal_window_v1",
            "profile_version": "1.0.0",
            "config": {
                "window": {"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True},
                "features": {"imu": {"statistics": ["mean"]}},
            },
        },
    ).json()
    qc = client.post(
        f"/api/v1/qc/{xform['transformation_id']}",
        json={"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}},
    ).json()
    return {"imu": imu, "gps": gps, "sync": sync, "cleaned": cleaned, "xform": xform, "qc": qc}


def _package(client: TestClient, transformation_id: str, qc_id: str) -> dict:
    request = {
        "qc_id": qc_id,
        "profile_name": "default_ml_package",
        "profile_version": "1.0.0",
        "config": {
            "split": {"strategy": "group_hash", "train_ratio": 0.7, "validation_ratio": 0.15, "test_ratio": 0.15, "seed": 1},
            "grouping": {"mode": "source_overlap"},
            "exports": ["jsonl"],
        },
    }
    response = client.post(f"{PKG_URL}/{transformation_id}", json=request)
    assert response.status_code == 200, response.text
    return response.json()


def test_transformed_artifact_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    artifact_path = Path(setup["xform"]["artifact_uri"].replace("file://", ""))
    bytes_before = artifact_path.read_bytes()

    _package(client, setup["xform"]["transformation_id"], setup["qc"]["qc_id"])

    assert artifact_path.read_bytes() == bytes_before


def test_transformation_manifest_and_report_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    manifest_path = Path(setup["xform"]["artifact_uri"].replace("file://", "")).parent / "manifest.json"
    report_path = Path(setup["xform"]["report_uri"].replace("file://", ""))
    manifest_before = manifest_path.read_text()
    report_before = report_path.read_text()

    _package(client, setup["xform"]["transformation_id"], setup["qc"]["qc_id"])

    assert manifest_path.read_text() == manifest_before
    assert report_path.read_text() == report_before


def test_qc_report_and_manifest_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    report_path = Path(setup["qc"]["report_uri"].replace("file://", ""))
    manifest_path = report_path.parent / "manifest.json"
    report_before = report_path.read_text()
    manifest_before = manifest_path.read_text()

    _package(client, setup["xform"]["transformation_id"], setup["qc"]["qc_id"])

    assert report_path.read_text() == report_before
    assert manifest_path.read_text() == manifest_before


def test_cleaned_artifact_and_reports_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    artifact_path = Path(setup["cleaned"]["artifact_uri"].replace("file://", ""))
    manifest_path = artifact_path.parent / "manifest.json"
    report_path = Path(setup["cleaned"]["report_uri"].replace("file://", ""))
    artifact_before = artifact_path.read_bytes()
    manifest_before = manifest_path.read_text()
    report_before = report_path.read_text()

    _package(client, setup["xform"]["transformation_id"], setup["qc"]["qc_id"])

    assert artifact_path.read_bytes() == artifact_before
    assert manifest_path.read_text() == manifest_before
    assert report_path.read_text() == report_before


def test_synchronized_artifact_and_manifest_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    artifact_path = Path(setup["sync"]["artifact_uri"].replace("file://", ""))
    manifest_path = artifact_path.parent / "manifest.json"
    artifact_before = artifact_path.read_bytes()
    manifest_before = manifest_path.read_text()

    _package(client, setup["xform"]["transformation_id"], setup["qc"]["qc_id"])

    assert artifact_path.read_bytes() == artifact_before
    assert manifest_path.read_text() == manifest_before


def test_normalized_artifacts_unchanged(client: TestClient) -> None:
    setup = _setup(client)
    imu_path = Path(setup["imu"]["normalization"]["artifact_uri"].replace("file://", ""))
    gps_path = Path(setup["gps"]["normalization"]["artifact_uri"].replace("file://", ""))
    imu_before = imu_path.read_bytes()
    gps_before = gps_path.read_bytes()

    _package(client, setup["xform"]["transformation_id"], setup["qc"]["qc_id"])

    assert imu_path.read_bytes() == imu_before
    assert gps_path.read_bytes() == gps_before


def test_raw_files_unchanged(client: TestClient, storage_root: Path) -> None:
    setup = _setup(client)
    ingestion = setup["imu"]["ingestion"]
    raw_path = (
        storage_root / ingestion["customer_id"] / ingestion["session_id"]
        / ingestion["ingestion_id"] / "original" / "imu.csv"
    )
    raw_before = raw_path.read_bytes()

    _package(client, setup["xform"]["transformation_id"], setup["qc"]["qc_id"])

    assert raw_path.read_bytes() == raw_before


def test_validation_and_integrity_reports_unchanged(client: TestClient, validation_root: Path, integrity_root: Path) -> None:
    setup = _setup(client)
    ingestion = setup["imu"]["ingestion"]
    validation = setup["imu"]["validation"]
    integrity = setup["imu"]["integrity"]
    validation_report_path = validation_root / ingestion["ingestion_id"] / validation["validation_id"] / "report.json"
    integrity_report_path = integrity_root / ingestion["ingestion_id"] / integrity["integrity_id"] / "report.json"
    validation_before = validation_report_path.read_text()
    integrity_before = integrity_report_path.read_text()

    _package(client, setup["xform"]["transformation_id"], setup["qc"]["qc_id"])

    assert validation_report_path.read_text() == validation_before
    assert integrity_report_path.read_text() == integrity_before


def test_manifest_contains_full_lineage_chain(client: TestClient, package_root: Path) -> None:
    setup = _setup(client)
    body = _package(client, setup["xform"]["transformation_id"], setup["qc"]["qc_id"])
    manifest = json.loads((package_root / setup["xform"]["transformation_id"] / body["package_id"] / "manifest.json").read_text())
    assert manifest["transformation_id"] == setup["xform"]["transformation_id"]
    assert manifest["qc_id"] == setup["qc"]["qc_id"]
    assert manifest["upstream"]["cleaning_id"] == setup["cleaned"]["cleaning_id"]
    assert manifest["upstream"]["synchronization_id"] == setup["sync"]["synchronization_id"]
    assert manifest["source_transformed_sha256"] == setup["xform"]["transformed_sha256"]


def test_existing_package_not_overwritten_by_second_run(client: TestClient, package_root: Path) -> None:
    setup = _setup(client)
    body1 = _package(client, setup["xform"]["transformation_id"], setup["qc"]["qc_id"])
    report_path = package_root / setup["xform"]["transformation_id"] / body1["package_id"] / "report.json"
    bytes_before = report_path.read_bytes()

    _package(client, setup["xform"]["transformation_id"], setup["qc"]["qc_id"])

    assert report_path.read_bytes() == bytes_before
