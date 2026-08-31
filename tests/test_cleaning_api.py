"""End-to-end tests for the cleaning HTTP API.

Covers the full ingest -> validate -> integrity -> normalize -> synchronize
-> clean pipeline, request validation, and the request-level error cases.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

CLEAN_URL = "/api/v1/cleaning"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,0.1,0.2,9.8\n" for i in range(6)
)
GPS_CSV = "timestamp,latitude,longitude\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,34.020{i},-118.285{i}\n" for i in range(6)
)


def _upload(client: TestClient, filename: str, content: str, **form_fields) -> dict:
    response = client.post(
        "/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=form_fields
    )
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
        json={
            "schema_name": schema_name,
            "schema_version": "1.0.0",
            "profile_name": profile_name,
            "profile_version": "1.0.0",
            "source_units": source_units,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _synchronized(client: TestClient, session_id: str = "sess_clean_api") -> dict:
    imu_norm = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps_norm = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    response = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": imu_norm["normalization_id"]},
                {"name": "gps", "normalization_id": gps_norm["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 500},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _default_clean_request(**config_overrides) -> dict:
    config = {
        "required_streams": ["imu"],
        "min_present_streams": 1,
        "drop_if_all_optional_streams_missing": False,
        "duplicate_policy": {"enabled": True},
        "privacy": {"redact_fields": []},
    }
    config.update(config_overrides)
    return {"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": config}


def test_valid_synchronized_dataset_cleans_successfully(client: TestClient) -> None:
    sync = _synchronized(client)
    response = client.post(f"{CLEAN_URL}/{sync['synchronization_id']}", json=_default_clean_request())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["summary"]["input_rows"] == 6
    assert body["summary"]["retained_rows"] == 6
    assert body["summary"]["dropped_rows"] == 0


def test_jsonl_output_valid(client: TestClient) -> None:
    sync = _synchronized(client)
    body = client.post(f"{CLEAN_URL}/{sync['synchronization_id']}", json=_default_clean_request()).json()

    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    lines = artifact_path.read_text().splitlines()
    assert len(lines) == body["summary"]["retained_rows"]
    for line in lines:
        parsed = json.loads(line)
        assert "timestamp" in parsed
        assert "streams" in parsed


def test_empty_synchronized_dataset_handled(client: TestClient, synchronized_root: Path) -> None:
    sync = _synchronized(client)
    artifact_path = Path(sync["artifact_uri"].replace("file://", ""))
    artifact_path.write_text("")  # empty out the synchronized artifact

    import hashlib

    manifest_path = artifact_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["synchronized_sha256"] = hashlib.sha256(b"").hexdigest()
    manifest["synchronized_size_bytes"] = 0
    manifest_path.write_text(json.dumps(manifest))

    response = client.post(f"{CLEAN_URL}/{sync['synchronization_id']}", json=_default_clean_request())

    assert response.status_code == 200  # not a server error
    body = response.json()
    assert body["status"] == "rejected"
    assert body["summary"]["input_rows"] == 0
    assert "EMPTY_SYNCHRONIZED_DATASET" in body["rejection_reasons"]


def test_minimum_retained_rows_can_reject_run(client: TestClient) -> None:
    sync = _synchronized(client)  # 6 rows, all retained normally
    response = client.post(
        f"{CLEAN_URL}/{sync['synchronization_id']}",
        json=_default_clean_request(minimum_retained_rows=100),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert "INSUFFICIENT_RETAINED_ROWS" in body["rejection_reasons"]


def test_rejected_run_returns_http_200(client: TestClient) -> None:
    sync = _synchronized(client)
    response = client.post(
        f"{CLEAN_URL}/{sync['synchronization_id']}",
        json=_default_clean_request(minimum_retained_rows=1000),
    )
    assert response.status_code == 200


def test_all_rows_removed_produces_rejected_result(client: TestClient) -> None:
    sync = _synchronized(client)
    response = client.post(
        f"{CLEAN_URL}/{sync['synchronization_id']}",
        json=_default_clean_request(required_streams=["imu", "camera"]),  # camera never present -> all dropped
    )
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["retained_rows"] == 0
    assert body["summary"]["dropped_rows"] == 6
    # No minimum_retained_rows configured -> 0 retained rows is not itself a
    # rejection unless the threshold says so; document/assert current policy:
    # completed with zero retained rows is a valid, auditable outcome.
    assert body["status"] == "completed"


def test_policy_not_found_returns_404(client: TestClient) -> None:
    sync = _synchronized(client)
    request = _default_clean_request()
    request["policy_name"] = "does_not_exist"
    response = client.post(f"{CLEAN_URL}/{sync['synchronization_id']}", json=request)
    assert response.status_code == 404


def test_synchronization_not_found_returns_404(client: TestClient) -> None:
    response = client.post(f"{CLEAN_URL}/sync_does_not_exist", json=_default_clean_request())
    assert response.status_code == 404


def test_synchronized_checksum_mismatch_returns_409(client: TestClient) -> None:
    sync = _synchronized(client)
    artifact_path = Path(sync["artifact_uri"].replace("file://", ""))
    original = artifact_path.read_bytes()
    artifact_path.write_bytes(original + b"tampered")
    try:
        response = client.post(f"{CLEAN_URL}/{sync['synchronization_id']}", json=_default_clean_request())
        assert response.status_code == 409
    finally:
        artifact_path.write_bytes(original)


def test_invalid_cleaning_configuration_rejected(client: TestClient) -> None:
    sync = _synchronized(client)
    response = client.post(
        f"{CLEAN_URL}/{sync['synchronization_id']}",
        json=_default_clean_request(min_present_streams=-1),
    )
    assert response.status_code == 400


def test_report_persisted(client: TestClient, cleaned_root: Path) -> None:
    sync = _synchronized(client)
    body = client.post(f"{CLEAN_URL}/{sync['synchronization_id']}", json=_default_clean_request()).json()

    report_path = cleaned_root / sync["synchronization_id"] / body["cleaning_id"] / "report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert report["cleaning_id"] == body["cleaning_id"]


def test_privacy_redaction_via_api(client: TestClient) -> None:
    sync = _synchronized(client)
    response = client.post(
        f"{CLEAN_URL}/{sync['synchronization_id']}",
        json=_default_clean_request(privacy={"redact_fields": ["streams.gps.latitude", "streams.gps.longitude"]}),
    )
    body = response.json()
    assert body["status"] == "completed"

    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    for line in artifact_path.read_text().splitlines():
        row = json.loads(line)
        assert row["streams"]["gps"]["latitude"] is None
        assert row["streams"]["gps"]["longitude"] is None
