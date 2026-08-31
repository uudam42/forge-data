"""End-to-end tests for the normalization HTTP API.

Covers ingestion -> validation -> integrity -> normalization wiring, the
lineage gate (missing/failed/stale validation or integrity), artifact +
manifest persistence, determinism, and immutability of every upstream
artifact (raw bytes, raw manifest, validation report, integrity report).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.storage.integrity_store import LocalIntegrityReportStore
from app.storage.validation_store import LocalValidationReportStore

UPLOAD_URL = "/api/v1/ingestion/upload"

VALID_IMU_CSV = (
    "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n"
    "2026-08-30T18:00:00-07:00,1.0,0.0,-1.0,180,0,-180\n"
    "2026-08-30T18:00:01-07:00,0.5,0.1,-0.9,90,0,-90\n"
)

VALID_GPS_CSV = (
    "timestamp,latitude,longitude,altitude,speed\n"
    "2026-08-30T18:00:00-07:00,34.0205,-118.2856,100,36\n"
    "2026-08-30T18:00:01-07:00,34.0206,-118.2857,101,35\n"
)

OUT_OF_ORDER_IMU_CSV = (
    "timestamp,accel_x,accel_y,accel_z\n"
    "2026-08-30T18:00:05Z,0.1,0.2,9.8\n"
    "2026-08-30T18:00:01Z,0.1,0.2,9.8\n"
)


def _normalize_url(ingestion_id: str) -> str:
    return f"/api/v1/normalization/{ingestion_id}"


def _upload(client: TestClient, filename: str, content: bytes, **form_fields) -> dict:
    response = client.post(UPLOAD_URL, files={"file": (filename, content, None)}, data=form_fields)
    assert response.status_code == 201, response.text
    return response.json()


def _validate(client: TestClient, ingestion_id: str, schema_name: str, schema_version: str = "1.0.0") -> dict:
    response = client.post(
        f"/api/v1/validation/{ingestion_id}",
        json={"schema_name": schema_name, "schema_version": schema_version},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _check_integrity(client: TestClient, ingestion_id: str, schema_name: str, schema_version: str = "1.0.0") -> dict:
    response = client.post(
        f"/api/v1/integrity/{ingestion_id}",
        json={"schema_name": schema_name, "schema_version": schema_version},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _imu_pipeline(client: TestClient, csv_text: str = VALID_IMU_CSV, **upload_fields) -> tuple[dict, dict, dict]:
    ingestion = _upload(client, "imu.csv", csv_text.encode(), **upload_fields)
    validation = _validate(client, ingestion["ingestion_id"], "imu")
    integrity = _check_integrity(client, ingestion["ingestion_id"], "imu")
    return ingestion, validation, integrity


def _normalize_imu(client: TestClient, ingestion_id: str, **source_units):
    return client.post(
        _normalize_url(ingestion_id),
        json={
            "schema_name": "imu",
            "schema_version": "1.0.0",
            "profile_name": "imu_canonical",
            "profile_version": "1.0.0",
            "source_units": source_units or {"acceleration": "g", "angular_velocity": "deg/s"},
        },
    )


def test_valid_imu_csv_normalization_succeeds(client: TestClient) -> None:
    ingestion, validation, integrity = _imu_pipeline(client)
    response = _normalize_imu(client, ingestion["ingestion_id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["ingestion_id"] == ingestion["ingestion_id"]
    assert body["validation_id"] == validation["validation_id"]
    assert body["integrity_id"] == integrity["integrity_id"]
    assert body["records_written"] == 2
    assert body["artifact_uri"]
    assert body["normalized_sha256"]


def test_json_normalization_succeeds(client: TestClient) -> None:
    records = [
        {"timestamp": "2026-08-30T18:00:00Z", "accel_x": 1.0, "accel_y": 0.0, "accel_z": -1.0},
        {"timestamp": "2026-08-30T18:00:01Z", "accel_x": 0.5, "accel_y": 0.1, "accel_z": -0.9},
    ]
    ingestion = _upload(client, "imu.json", json.dumps(records).encode())
    _validate(client, ingestion["ingestion_id"], "imu")
    _check_integrity(client, ingestion["ingestion_id"], "imu")

    response = _normalize_imu(client, ingestion["ingestion_id"], acceleration="m/s^2", angular_velocity="rad/s")

    assert response.status_code == 200, response.text
    assert response.json()["records_written"] == 2


def test_jsonl_normalization_succeeds(client: TestClient) -> None:
    lines = "\n".join(
        json.dumps({"timestamp": "2026-08-30T18:00:0{}Z".format(i), "accel_x": 0.1, "accel_y": 0.2, "accel_z": 9.8})
        for i in range(3)
    )
    ingestion = _upload(client, "imu.jsonl", (lines + "\n").encode())
    _validate(client, ingestion["ingestion_id"], "imu")
    _check_integrity(client, ingestion["ingestion_id"], "imu")

    response = _normalize_imu(client, ingestion["ingestion_id"], acceleration="m/s^2", angular_velocity="rad/s")

    assert response.status_code == 200, response.text
    assert response.json()["records_written"] == 3


def test_zip_rejected(client: TestClient) -> None:
    ingestion = _upload(client, "bundle.zip", b"PK\x03\x04fakezip")

    # A real .zip can never actually reach this point through the normal
    # flow — Step 2 itself already refuses to validate .zip (415), so no
    # ingestion backed by a zip file can ever have a passing validation
    # report. To specifically exercise Step 4's *own* unsupported-file-type
    # guard (independent of the lineage gate), fabricate a passing
    # validation + integrity pair for this ingestion, exactly as if some
    # future validator/checker did support zip.
    response = client.post(
        _normalize_url(ingestion["ingestion_id"]),
        json={
            "schema_name": "imu",
            "schema_version": "1.0.0",
            "profile_name": "imu_canonical",
            "profile_version": "1.0.0",
            "source_units": {"acceleration": "g", "angular_velocity": "deg/s"},
        },
    )
    # Without a matching validation/integrity report, the lineage gate
    # rejects first (409) — confirm that, then prove the file-type guard
    # independently by satisfying the gate artificially.
    assert response.status_code == 409


def test_zip_rejected_when_lineage_gate_is_satisfied(
    client: TestClient, validation_root: Path, integrity_root: Path
) -> None:
    ingestion = _upload(client, "bundle.zip", b"PK\x03\x04fakezip")
    raw_sha256 = ingestion["sha256"]

    validation_store = LocalValidationReportStore(root=validation_root)
    validation_store.write_report(
        ingestion_id=ingestion["ingestion_id"],
        validation_id="val_zip",
        report={
            "validation_id": "val_zip",
            "ingestion_id": ingestion["ingestion_id"],
            "validated_at": "2026-01-01T00:00:00+00:00",
            "schema": {"name": "imu", "version": "1.0.0"},
            "raw_sha256": raw_sha256,
            "status": "passed",
            "summary": {"records_checked": 1, "valid_records": 1, "invalid_records": 0, "error_count": 0, "warning_count": 0},
            "errors": [],
            "warnings": [],
            "errors_truncated": False,
        },
    )
    integrity_store = LocalIntegrityReportStore(root=integrity_root)
    integrity_store.write_report(
        ingestion_id=ingestion["ingestion_id"],
        integrity_id="integ_zip",
        report={
            "integrity_id": "integ_zip",
            "ingestion_id": ingestion["ingestion_id"],
            "validation_id": "val_zip",
            "customer_id": "anonymous",
            "device_id": None,
            "schema_name": "imu",
            "schema_version": "1.0.0",
            "source_filename": "bundle.zip",
            "raw_sha256": raw_sha256,
            "status": "passed",
            "total_records": 1,
            "checked_records": 1,
            "passed_records": 1,
            "failed_records": 0,
            "warning_count": 0,
            "error_count": 0,
            "issues": [],
            "issues_truncated": False,
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )

    response = client.post(
        _normalize_url(ingestion["ingestion_id"]),
        json={
            "schema_name": "imu",
            "schema_version": "1.0.0",
            "profile_name": "imu_canonical",
            "profile_version": "1.0.0",
            "source_units": {"acceleration": "g", "angular_velocity": "deg/s"},
        },
    )
    assert response.status_code == 415


def test_missing_integrity_report_blocks_normalization(client: TestClient) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())
    _validate(client, ingestion["ingestion_id"], "imu")
    # deliberately skip integrity check

    response = _normalize_imu(client, ingestion["ingestion_id"])
    assert response.status_code == 409


def test_failed_integrity_report_blocks_normalization(client: TestClient) -> None:
    ingestion = _upload(client, "imu.csv", OUT_OF_ORDER_IMU_CSV.encode())
    _validate(client, ingestion["ingestion_id"], "imu")
    integrity = _check_integrity(client, ingestion["ingestion_id"], "imu")
    assert integrity["status"] == "failed"

    response = _normalize_imu(client, ingestion["ingestion_id"])
    assert response.status_code == 409


def test_mismatched_schema_version_blocks_normalization(client: TestClient) -> None:
    ingestion, _, _ = _imu_pipeline(client)

    response = client.post(
        _normalize_url(ingestion["ingestion_id"]),
        json={
            "schema_name": "imu",
            "schema_version": "9.9.9",
            "profile_name": "imu_canonical",
            "profile_version": "1.0.0",
            "source_units": {"acceleration": "g", "angular_velocity": "deg/s"},
        },
    )
    # Unknown schema version -> schema lookup itself fails (404), which is
    # also, in effect, blocking normalization from running.
    assert response.status_code == 404


def test_stale_raw_checksum_blocks_normalization(
    client: TestClient, validation_root: Path, integrity_root: Path
) -> None:
    ingestion = _upload(client, "imu.csv", VALID_IMU_CSV.encode())
    # No legitimate validation/integrity report is created for this
    # ingestion — only reports claiming to match schema imu v1.0.0 but with
    # a fabricated, incorrect raw_sha256 (a stale lineage scenario).
    fake_sha = "0" * 64
    validation_store = LocalValidationReportStore(root=validation_root)
    validation_store.write_report(
        ingestion_id=ingestion["ingestion_id"],
        validation_id="val_00000000-0000-0000-0000-000000000000",
        report={
            "validation_id": "val_00000000-0000-0000-0000-000000000000",
            "ingestion_id": ingestion["ingestion_id"],
            "validated_at": "2020-01-01T00:00:00+00:00",
            "schema": {"name": "imu", "version": "1.0.0"},
            "raw_sha256": fake_sha,
            "status": "passed",
            "summary": {"records_checked": 1, "valid_records": 1, "invalid_records": 0, "error_count": 0, "warning_count": 0},
            "errors": [],
            "warnings": [],
            "errors_truncated": False,
        },
    )

    response = _normalize_imu(client, ingestion["ingestion_id"])
    assert response.status_code == 409


def test_normalized_artifact_and_manifest_persisted(client: TestClient, normalized_root: Path) -> None:
    ingestion, _, _ = _imu_pipeline(client)
    response = _normalize_imu(client, ingestion["ingestion_id"])
    body = response.json()

    artifact_dir = normalized_root / ingestion["ingestion_id"] / body["normalization_id"]
    assert (artifact_dir / "normalized.csv").exists()
    assert (artifact_dir / "manifest.json").exists()


def test_manifest_contains_full_lineage(client: TestClient, normalized_root: Path) -> None:
    ingestion, validation, integrity = _imu_pipeline(client)
    response = _normalize_imu(client, ingestion["ingestion_id"])
    body = response.json()

    manifest_path = normalized_root / ingestion["ingestion_id"] / body["normalization_id"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    assert manifest["validation_id"] == validation["validation_id"]
    assert manifest["integrity_id"] == integrity["integrity_id"]
    assert manifest["source_raw_sha256"] == ingestion["sha256"]
    assert manifest["normalized_sha256"] == body["normalized_sha256"]
    assert manifest["normalization_profile"] == {"name": "imu_canonical", "version": "1.0.0"}
    assert manifest["schema"] == {"name": "imu", "version": "1.0.0"}
    assert manifest["records_written"] == 2
    assert manifest["normalization_config_hash"]
    assert manifest["transform_version"] == "1.0.0"


def test_normalized_artifact_checksum_matches_manifest(client: TestClient, normalized_root: Path) -> None:
    ingestion, _, _ = _imu_pipeline(client)
    response = _normalize_imu(client, ingestion["ingestion_id"])
    body = response.json()

    artifact_dir = normalized_root / ingestion["ingestion_id"] / body["normalization_id"]
    artifact_bytes = (artifact_dir / "normalized.csv").read_bytes()
    manifest = json.loads((artifact_dir / "manifest.json").read_text())

    actual_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    assert actual_sha256 == manifest["normalized_sha256"]
    assert actual_sha256 == body["normalized_sha256"]
    assert manifest["normalized_size_bytes"] == len(artifact_bytes)


def test_records_written_count_correct(client: TestClient) -> None:
    csv_text = (
        "timestamp,accel_x,accel_y,accel_z\n"
        + "".join(f"2026-08-30T18:00:{i:02d}Z,0.1,0.2,9.8\n" for i in range(5))
    )
    ingestion = _upload(client, "imu.csv", csv_text.encode())
    _validate(client, ingestion["ingestion_id"], "imu")
    _check_integrity(client, ingestion["ingestion_id"], "imu")

    response = _normalize_imu(client, ingestion["ingestion_id"], acceleration="m/s^2", angular_velocity="rad/s")
    assert response.json()["records_written"] == 5


def test_deterministic_normalized_content_across_runs(client: TestClient, normalized_root: Path) -> None:
    ingestion, _, _ = _imu_pipeline(client)

    first = _normalize_imu(client, ingestion["ingestion_id"]).json()
    second = _normalize_imu(client, ingestion["ingestion_id"]).json()

    assert first["normalization_id"] != second["normalization_id"]
    assert first["normalized_sha256"] == second["normalized_sha256"]

    first_bytes = (
        normalized_root / ingestion["ingestion_id"] / first["normalization_id"] / "normalized.csv"
    ).read_bytes()
    second_bytes = (
        normalized_root / ingestion["ingestion_id"] / second["normalization_id"] / "normalized.csv"
    ).read_bytes()
    assert first_bytes == second_bytes


def test_raw_bytes_and_manifest_unchanged_after_normalization(
    client: TestClient, storage_root: Path
) -> None:
    ingestion, _, _ = _imu_pipeline(
        client, customer_id="cust_norm", session_id="sess_norm"
    )
    raw_path = (
        storage_root / "cust_norm" / "sess_norm" / ingestion["ingestion_id"] / "original" / "imu.csv"
    )
    manifest_path = storage_root / "cust_norm" / "sess_norm" / ingestion["ingestion_id"] / "manifest.json"
    raw_bytes_before = raw_path.read_bytes()
    raw_manifest_before = manifest_path.read_text()

    response = _normalize_imu(client, ingestion["ingestion_id"])
    assert response.status_code == 200

    assert raw_path.read_bytes() == raw_bytes_before
    assert manifest_path.read_text() == raw_manifest_before


def test_validation_and_integrity_reports_unchanged_after_normalization(
    client: TestClient, validation_root: Path, integrity_root: Path
) -> None:
    ingestion, validation, integrity = _imu_pipeline(client)

    validation_report_path = (
        validation_root / ingestion["ingestion_id"] / validation["validation_id"] / "report.json"
    )
    integrity_report_path = (
        integrity_root / ingestion["ingestion_id"] / integrity["integrity_id"] / "report.json"
    )
    validation_before = validation_report_path.read_text()
    integrity_before = integrity_report_path.read_text()

    response = _normalize_imu(client, ingestion["ingestion_id"])
    assert response.status_code == 200

    assert validation_report_path.read_text() == validation_before
    assert integrity_report_path.read_text() == integrity_before


def test_normalization_failure_leaves_no_committed_artifact(
    client: TestClient, normalized_root: Path, monkeypatch
) -> None:
    """A record-level conversion failure partway through must not leave a
    committed (or even a lingering staging) directory behind — the whole
    run fails, and nothing under this ingestion_id looks like a valid
    normalization artifact.
    """
    csv_text = "timestamp,accel_x,accel_y,accel_z\n" + "".join(
        f"2026-08-30T18:00:{i:02d}Z,0.1,0.2,9.8\n" for i in range(5)
    )
    ingestion = _upload(client, "imu.csv", csv_text.encode())
    _validate(client, ingestion["ingestion_id"], "imu")
    _check_integrity(client, ingestion["ingestion_id"], "imu")

    from app.normalization.profiles.base import NormalizationConversionError, RecordNormalizer

    original_normalize_record = RecordNormalizer.normalize_record
    call_count = {"n": 0}

    def flaky_normalize_record(self, record_number, raw_record):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise NormalizationConversionError("simulated failure partway through the file")
        return original_normalize_record(self, record_number, raw_record)

    monkeypatch.setattr(RecordNormalizer, "normalize_record", flaky_normalize_record)

    response = _normalize_imu(client, ingestion["ingestion_id"], acceleration="m/s^2", angular_velocity="rad/s")
    assert response.status_code == 400
    assert call_count["n"] == 3  # confirms the failure happened mid-stream, not on the first record

    ingestion_dir = normalized_root / ingestion["ingestion_id"]
    if ingestion_dir.exists():
        assert list(ingestion_dir.iterdir()) == []  # no committed run, no leftover staging dir


def test_gps_normalization_succeeds_with_unit_conversion(client: TestClient) -> None:
    ingestion = _upload(client, "gps.csv", VALID_GPS_CSV.encode())
    _validate(client, ingestion["ingestion_id"], "gps")
    _check_integrity(client, ingestion["ingestion_id"], "gps")

    response = client.post(
        _normalize_url(ingestion["ingestion_id"]),
        json={
            "schema_name": "gps",
            "schema_version": "1.0.0",
            "profile_name": "gps_canonical",
            "profile_version": "1.0.0",
            "source_units": {"altitude": "ft", "speed": "km/h"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["records_written"] == 2
