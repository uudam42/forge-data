"""End-to-end tests for the schema validation HTTP API.

Covers ingestion -> validation wiring, report persistence, raw-data
immutability, and the request-level error cases (unknown ingestion, unknown
schema, unsupported file type).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app

UPLOAD_URL = "/api/v1/ingestion/upload"


def _validation_url(ingestion_id: str) -> str:
    return f"/api/v1/validation/{ingestion_id}"


def _upload(client: TestClient, filename: str, content: bytes, **form_fields) -> dict:
    response = client.post(UPLOAD_URL, files={"file": (filename, content, None)}, data=form_fields)
    assert response.status_code == 201, response.text
    return response.json()


VALID_IMU_CSV = (
    "timestamp,accel_x,accel_y,accel_z,device_id\n"
    "2026-08-29T18:34:22Z,0.10,0.20,9.81,imu_01\n"
    "2026-08-29T18:34:23Z,0.11,0.19,9.80,imu_01\n"
)


def test_valid_imu_csv_passes(client: TestClient) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())

    response = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "passed"
    assert body["ingestion_id"] == ingestion["ingestion_id"]
    assert body["schema"] == {"name": "imu", "version": "1.0.0"}
    assert body["summary"]["records_checked"] == 2
    assert body["summary"]["valid_records"] == 2
    assert body["summary"]["error_count"] == 0
    assert body["report_uri"]


def test_missing_required_column_fails_via_api(client: TestClient) -> None:
    bad_csv = "timestamp,accel_x,accel_y,device_id\n2026-08-29T18:34:22Z,0.1,0.2,imu_01\n"
    ingestion = _upload(client, "imu.csv", bad_csv.encode())

    response = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["summary"]["error_count"] >= 1


def test_unknown_ingestion_returns_404(client: TestClient) -> None:
    response = client.post(
        _validation_url("ing_does_not_exist"),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )

    assert response.status_code == 404


def test_unknown_schema_returns_404(client: TestClient) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())

    response = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "does_not_exist", "schema_version": "9.9.9"},
    )

    assert response.status_code == 404


def test_provided_schema_version_is_respected(client: TestClient) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())

    ok = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )
    wrong_version = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "2.5.0"},
    )

    assert ok.status_code == 200
    assert wrong_version.status_code == 404


def test_zip_validation_is_unsupported(client: TestClient) -> None:
    ingestion = _upload(client, "bundle.zip", b"PK\x03\x04fakezipcontent")

    response = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )

    assert response.status_code == 415


def test_raw_bytes_and_manifest_unchanged_after_validation(client: TestClient, storage_root: Path) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode(), customer_id="cust_immut", session_id="sess_immut")

    raw_path = (
        storage_root
        / "cust_immut"
        / "sess_immut"
        / ingestion["ingestion_id"]
        / "original"
        / "imu.csv"
    )
    manifest_path = (
        storage_root / "cust_immut" / "sess_immut" / ingestion["ingestion_id"] / "manifest.json"
    )
    bytes_before = raw_path.read_bytes()
    manifest_before = manifest_path.read_text()

    response = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )
    assert response.status_code == 200

    assert raw_path.read_bytes() == bytes_before
    assert manifest_path.read_text() == manifest_before


def test_report_is_persisted(client: TestClient, validation_root: Path) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())

    response = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )
    body = response.json()

    report_path = (
        validation_root / ingestion["ingestion_id"] / body["validation_id"] / "report.json"
    )
    assert report_path.exists()


def test_report_raw_sha256_matches_ingestion_manifest(client: TestClient, validation_root: Path) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())

    response = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )
    body = response.json()

    report_path = (
        validation_root / ingestion["ingestion_id"] / body["validation_id"] / "report.json"
    )
    report = json.loads(report_path.read_text())

    assert report["raw_sha256"] == ingestion["sha256"]
    assert report["ingestion_id"] == ingestion["ingestion_id"]
    assert report["schema"] == {"name": "imu", "version": "1.0.0"}


def test_empty_dataset_fails_via_api(client: TestClient) -> None:
    header_only_csv = "timestamp,accel_x,accel_y,accel_z\n"
    ingestion = _upload(client, "imu.csv", header_only_csv.encode())

    response = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )

    body = response.json()
    assert body["status"] == "failed"
    assert body["summary"]["records_checked"] == 0


def test_valid_json_array_passes_via_api(client: TestClient) -> None:
    records = [
        {"timestamp": "2026-08-29T18:34:22Z", "accel_x": 0.1, "accel_y": 0.2, "accel_z": 9.8},
    ]
    ingestion = _upload(client, "imu.json", json.dumps(records).encode())

    response = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )

    assert response.json()["status"] == "passed"


def test_valid_jsonl_passes_via_api(client: TestClient) -> None:
    lines = "\n".join(
        json.dumps({"timestamp": "2026-08-29T18:34:22Z", "accel_x": 0.1, "accel_y": 0.2, "accel_z": 9.8})
        for _ in range(3)
    )
    ingestion = _upload(client, "imu.jsonl", (lines + "\n").encode())

    response = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )

    body = response.json()
    assert body["status"] == "passed"
    assert body["summary"]["records_checked"] == 3


def test_error_truncation_via_api(test_settings: Settings) -> None:
    truncated_settings = test_settings.model_copy(update={"MAX_VALIDATION_ERRORS": 2})
    app.dependency_overrides[get_settings] = lambda: truncated_settings

    try:
        with TestClient(app) as client:
            bad_rows = "\n".join(
                f"2026-08-29T18:34:22Z,abc{i},0.2,9.8" for i in range(10)
            )
            csv_text = f"timestamp,accel_x,accel_y,accel_z\n{bad_rows}\n"
            ingestion = _upload(client, "imu.csv", csv_text.encode())

            response = client.post(
                _validation_url(ingestion["ingestion_id"]),
                json={"schema_name": "imu", "schema_version": "1.0.0"},
            )
            body = response.json()

            assert body["status"] == "failed"
            assert body["summary"]["error_count"] == 10
    finally:
        app.dependency_overrides.clear()


def test_metadata_requirement_mismatch_is_flagged(client: TestClient) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode(), source_type="gps")

    response = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )

    body = response.json()
    assert body["status"] == "failed"


def test_metadata_requirement_not_checked_when_source_type_absent(client: TestClient) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())  # no source_type provided

    response = client.post(
        _validation_url(ingestion["ingestion_id"]),
        json={"schema_name": "imu", "schema_version": "1.0.0"},
    )

    assert response.json()["status"] == "passed"
