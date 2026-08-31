"""End-to-end tests for the data integrity HTTP API.

Covers ingestion -> validation -> integrity wiring, lineage enforcement
(validation must exist, must have passed, and must match the current raw
checksum), report persistence, and raw-data immutability.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.storage.validation_store import LocalValidationReportStore

UPLOAD_URL = "/api/v1/ingestion/upload"


def _validate_url(ingestion_id: str) -> str:
    return f"/api/v1/validation/{ingestion_id}"


def _integrity_url(ingestion_id: str) -> str:
    return f"/api/v1/integrity/{ingestion_id}"


def _upload(client: TestClient, filename: str, content: bytes, **form_fields) -> dict:
    response = client.post(UPLOAD_URL, files={"file": (filename, content, None)}, data=form_fields)
    assert response.status_code == 201, response.text
    return response.json()


def _validate(client: TestClient, ingestion_id: str, schema_name: str, schema_version: str = "1.0.0") -> dict:
    response = client.post(
        _validate_url(ingestion_id), json={"schema_name": schema_name, "schema_version": schema_version}
    )
    assert response.status_code == 200, response.text
    return response.json()


VALID_GPS_CSV = (
    "timestamp,latitude,longitude,speed\n"
    "2026-08-29T18:00:00Z,37.7749,-122.4194,5.0\n"
    "2026-08-29T18:00:01Z,37.7750,-122.4195,5.2\n"
    "2026-08-29T18:00:02Z,37.7751,-122.4196,5.1\n"
)

VALID_IMU_CSV = (
    "timestamp,accel_x,accel_y,accel_z\n"
    "2026-08-29T18:00:00Z,0.10,0.20,9.81\n"
    "2026-08-29T18:00:01Z,0.11,0.19,9.80\n"
)

EXTREME_IMU_CSV = (
    "timestamp,accel_x,accel_y,accel_z\n"
    "2026-08-29T18:00:00Z,0.10,0.20,9.81\n"
    "2026-08-29T18:00:01Z,500.0,0.19,9.80\n"  # extreme but syntactically valid
)

INVALID_GPS_CSV = (
    "timestamp,latitude,longitude,speed\n"
    "2026-08-29T18:00:05Z,37.7749,-122.4194,5.0\n"
    "2026-08-29T18:00:01Z,120.0,-122.4195,-3.0\n"  # backward timestamp, bad lat, negative speed
)


def test_valid_imu_integrity_request_succeeds(client: TestClient) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())
    _validate(client, ingestion["ingestion_id"], "imu")

    response = client.post(
        _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "imu", "schema_version": "1.0.0"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "passed"
    assert body["ingestion_id"] == ingestion["ingestion_id"]
    assert body["total_records"] == 2
    assert body["error_count"] == 0


def test_valid_gps_integrity_request_succeeds(client: TestClient) -> None:
    ingestion = _upload(client, "gps.csv", VALID_GPS_CSV.encode())
    _validate(client, ingestion["ingestion_id"], "gps")

    response = client.post(
        _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "gps", "schema_version": "1.0.0"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "passed"
    assert body["total_records"] == 3


def test_unknown_ingestion_returns_404(client: TestClient) -> None:
    response = client.post(
        _integrity_url("ing_does_not_exist"), json={"schema_name": "imu", "schema_version": "1.0.0"}
    )
    assert response.status_code == 404


def test_unknown_schema_returns_404(client: TestClient) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())
    response = client.post(
        _integrity_url(ingestion["ingestion_id"]),
        json={"schema_name": "does_not_exist", "schema_version": "9.9.9"},
    )
    assert response.status_code == 404


def test_integrity_cannot_run_without_validation(client: TestClient) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())
    # deliberately skip the /validation call

    response = client.post(
        _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "imu", "schema_version": "1.0.0"}
    )
    assert response.status_code == 409


def test_integrity_cannot_run_when_validation_failed(client: TestClient) -> None:
    bad_csv = "timestamp,accel_x\n2026-08-29T18:00:00Z,0.1\n"  # missing required accel_y/accel_z
    ingestion = _upload(client, "imu.csv", bad_csv.encode())

    validation = _validate_impl_allow_failure(client, ingestion["ingestion_id"])
    assert validation["status"] == "failed"

    response = client.post(
        _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "imu", "schema_version": "1.0.0"}
    )
    assert response.status_code == 409


def _validate_impl_allow_failure(client: TestClient, ingestion_id: str) -> dict:
    response = client.post(
        _validate_url(ingestion_id), json={"schema_name": "imu", "schema_version": "1.0.0"}
    )
    assert response.status_code == 200
    return response.json()


def test_stale_raw_sha256_lineage_mismatch_is_rejected(client: TestClient, validation_root: Path) -> None:
    ingestion = _upload(client, "gps.csv", VALID_GPS_CSV.encode())
    # No legitimate validation report is created for this ingestion — only a
    # report claiming to match schema gps v1.0.0 but with a fabricated,
    # incorrect raw_sha256 (simulating a validation report from a different,
    # now-stale version of "the same" logical file).
    store = LocalValidationReportStore(root=validation_root)
    stale_validation_id = "val_00000000-0000-0000-0000-000000000000"
    store.write_report(
        ingestion_id=ingestion["ingestion_id"],
        validation_id=stale_validation_id,
        report={
            "validation_id": stale_validation_id,
            "ingestion_id": ingestion["ingestion_id"],
            "validated_at": "2020-01-01T00:00:00+00:00",
            "schema": {"name": "gps", "version": "1.0.0"},
            "raw_sha256": "0" * 64,  # deliberately does not match the real raw file
            "status": "passed",
            "summary": {
                "records_checked": 3,
                "valid_records": 3,
                "invalid_records": 0,
                "error_count": 0,
                "warning_count": 0,
            },
            "errors": [],
            "warnings": [],
            "errors_truncated": False,
        },
    )

    response = client.post(
        _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "gps", "schema_version": "1.0.0"}
    )
    assert response.status_code == 409


def test_report_is_persisted(client: TestClient, integrity_root: Path) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())
    _validate(client, ingestion["ingestion_id"], "imu")

    response = client.post(
        _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "imu", "schema_version": "1.0.0"}
    )
    body = response.json()

    report_path = integrity_root / ingestion["ingestion_id"] / body["integrity_id"] / "report.json"
    assert report_path.exists()


def test_api_response_matches_persisted_report(client: TestClient, integrity_root: Path) -> None:
    ingestion = _upload(client, "gps.csv", VALID_GPS_CSV.encode())
    validation = _validate(client, ingestion["ingestion_id"], "gps")

    response = client.post(
        _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "gps", "schema_version": "1.0.0"}
    )
    body = response.json()

    report_path = integrity_root / ingestion["ingestion_id"] / body["integrity_id"] / "report.json"
    report = json.loads(report_path.read_text())

    assert report["integrity_id"] == body["integrity_id"]
    assert report["ingestion_id"] == body["ingestion_id"]
    assert report["status"] == body["status"]
    assert report["validation_id"] == validation["validation_id"]
    assert report["raw_sha256"] == ingestion["sha256"]
    assert report["schema_name"] == "gps"
    assert report["schema_version"] == "1.0.0"
    assert report["customer_id"] == ingestion["customer_id"]


def test_raw_file_unchanged_after_integrity_check(client: TestClient, storage_root: Path) -> None:
    ingestion = _upload(
        client, "gps.csv", VALID_GPS_CSV.encode(), customer_id="cust_integ", session_id="sess_integ"
    )
    _validate(client, ingestion["ingestion_id"], "gps")

    raw_path = (
        storage_root / "cust_integ" / "sess_integ" / ingestion["ingestion_id"] / "original" / "gps.csv"
    )
    manifest_path = storage_root / "cust_integ" / "sess_integ" / ingestion["ingestion_id"] / "manifest.json"
    bytes_before = raw_path.read_bytes()
    manifest_before = manifest_path.read_text()

    response = client.post(
        _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "gps", "schema_version": "1.0.0"}
    )
    assert response.status_code == 200

    assert raw_path.read_bytes() == bytes_before
    assert manifest_path.read_text() == manifest_before


def test_warnings_produce_passed_with_warnings(client: TestClient) -> None:
    ingestion = _upload(client, "imu.csv", EXTREME_IMU_CSV.encode())
    _validate(client, ingestion["ingestion_id"], "imu")

    response = client.post(
        _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "imu", "schema_version": "1.0.0"}
    )
    body = response.json()

    assert body["status"] == "passed_with_warnings"
    assert body["error_count"] == 0
    assert body["warning_count"] >= 1


def test_errors_produce_failed(client: TestClient) -> None:
    ingestion = _upload(client, "gps.csv", INVALID_GPS_CSV.encode())
    _validate(client, ingestion["ingestion_id"], "gps")

    response = client.post(
        _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "gps", "schema_version": "1.0.0"}
    )
    body = response.json()

    assert body["status"] == "failed"
    assert body["error_count"] >= 1


def test_clean_dataset_produces_passed(client: TestClient) -> None:
    ingestion = _upload(client, "gps.csv", VALID_GPS_CSV.encode())
    _validate(client, ingestion["ingestion_id"], "gps")

    response = client.post(
        _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "gps", "schema_version": "1.0.0"}
    )
    body = response.json()

    assert body["status"] == "passed"
    assert body["error_count"] == 0
    assert body["warning_count"] == 0


def test_issue_cap_works_via_api(test_settings: Settings, integrity_root: Path) -> None:
    truncated_settings = test_settings.model_copy(update={"MAX_INTEGRITY_ISSUES": 1})
    app.dependency_overrides[get_settings] = lambda: truncated_settings

    try:
        with TestClient(app) as client:
            rows = "\n".join(f"2026-08-29T18:00:{i:02d}Z,999.0,999.0,999.0" for i in range(5))
            csv_text = f"timestamp,accel_x,accel_y,accel_z\n{rows}\n"
            ingestion = _upload(client, "imu.csv", csv_text.encode())
            _validate(client, ingestion["ingestion_id"], "imu")

            response = client.post(
                _integrity_url(ingestion["ingestion_id"]),
                json={"schema_name": "imu", "schema_version": "1.0.0"},
            )
            body = response.json()

            # 5 records * 3 extreme accel axes = 15 warnings, but only 1 detailed issue stored
            assert body["warning_count"] == 15

            report_path = integrity_root / ingestion["ingestion_id"] / body["integrity_id"] / "report.json"
            report = json.loads(report_path.read_text())
            assert len(report["issues"]) == 1
            assert report["issues_truncated"] is True
    finally:
        app.dependency_overrides.clear()


def test_report_lineage_contains_validation_id_and_raw_sha256(
    client: TestClient, integrity_root: Path
) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())
    validation = _validate(client, ingestion["ingestion_id"], "imu")

    response = client.post(
        _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "imu", "schema_version": "1.0.0"}
    )
    body = response.json()

    report_path = integrity_root / ingestion["ingestion_id"] / body["integrity_id"] / "report.json"
    report = json.loads(report_path.read_text())

    assert report["validation_id"] == validation["validation_id"]
    assert report["raw_sha256"] == ingestion["sha256"]


def test_no_raw_values_appear_in_logs(client: TestClient, caplog) -> None:
    sentinel_latitude = "37.948271"  # a distinctive, easily-searchable raw value
    csv_text = f"timestamp,latitude,longitude\n2026-08-29T18:00:00Z,{sentinel_latitude},-122.0\n"
    ingestion = _upload(client, "gps.csv", csv_text.encode())
    _validate(client, ingestion["ingestion_id"], "gps")

    with caplog.at_level(logging.INFO, logger="app.integrity"):
        response = client.post(
            _integrity_url(ingestion["ingestion_id"]), json={"schema_name": "gps", "schema_version": "1.0.0"}
        )
    assert response.status_code == 200

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert sentinel_latitude not in log_text
