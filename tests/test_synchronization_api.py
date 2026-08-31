"""End-to-end tests for the multimodal synchronization HTTP API.

Covers the full ingest -> validate -> integrity -> normalize -> synchronize
pipeline, request validation, and the request-level error cases.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

SYNC_URL = "/api/v1/synchronization"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:00:{i:02d}.000000Z,0.1,0.2,9.8,0.01,0.02,0.03\n" for i in range(10)
)
GPS_CSV = (
    "timestamp,latitude,longitude,altitude,speed\n"
    "2026-08-30T18:00:00.000000Z,34.0205,-118.2856,30.48,10.0\n"
    "2026-08-30T18:00:05.000000Z,34.0206,-118.2857,30.50,10.1\n"
)


def _upload(client: TestClient, filename: str, content: str, **form_fields) -> dict:
    response = client.post(
        "/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=form_fields
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pipeline(client: TestClient, filename, content, schema_name, profile_name, source_units, **upload_fields) -> dict:
    ingestion = _upload(client, filename, content, **upload_fields)
    r = client.post(
        f"/api/v1/validation/{ingestion['ingestion_id']}",
        json={"schema_name": schema_name, "schema_version": "1.0.0"},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/integrity/{ingestion['ingestion_id']}",
        json={"schema_name": schema_name, "schema_version": "1.0.0"},
    )
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
    return {"ingestion": ingestion, "normalization": r.json()}


def _imu_gps_pair(client: TestClient, session_id: str = "sess_demo") -> tuple[dict, dict]:
    imu = _pipeline(
        client, "imu.csv", IMU_CSV, "imu", "imu_canonical",
        {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id,
    )
    gps = _pipeline(
        client, "gps.csv", GPS_CSV, "gps", "gps_canonical",
        {"altitude": "m", "speed": "m/s"}, session_id=session_id,
    )
    return imu, gps


def _sync_request(imu_norm_id: str, gps_norm_id: str, **overrides) -> dict:
    request = {
        "streams": [
            {"name": "imu", "normalization_id": imu_norm_id},
            {"name": "gps", "normalization_id": gps_norm_id},
        ],
        "reference": {"mode": "stream", "stream": "imu"},
        "alignment": {"default_method": "nearest", "max_time_delta_ms": 3000},
    }
    request.update(overrides)
    return request


def test_two_stream_synchronization_succeeds(client: TestClient) -> None:
    imu, gps = _imu_gps_pair(client)
    req = _sync_request(imu["normalization"]["normalization_id"], gps["normalization"]["normalization_id"])

    response = client.post(SYNC_URL, json=req)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["rows_written"] == 10
    assert set(body["coverage"]) == {"imu", "gps"}
    assert body["coverage"]["imu"] == 1.0


def test_reference_stream_timeline_correct(client: TestClient) -> None:
    imu, gps = _imu_gps_pair(client)
    req = _sync_request(imu["normalization"]["normalization_id"], gps["normalization"]["normalization_id"])
    response = client.post(SYNC_URL, json=req)
    body = response.json()

    artifact_path = body["artifact_uri"].replace("file://", "")
    lines = [json.loads(line) for line in Path(artifact_path).read_text().splitlines()]

    # Output timeline must be exactly the reference (imu) stream's own 10 timestamps.
    timestamps = [line["timestamp"] for line in lines]
    expected = [f"2026-08-30T18:00:{i:02d}Z" for i in range(10)]
    assert timestamps == expected
    for line in lines:
        assert line["alignment"]["imu"] == {"matched": True, "method": "reference", "delta_ms": 0.0}


def test_jsonl_output_is_valid_one_object_per_line(client: TestClient) -> None:
    imu, gps = _imu_gps_pair(client)
    req = _sync_request(imu["normalization"]["normalization_id"], gps["normalization"]["normalization_id"])
    body = client.post(SYNC_URL, json=req).json()

    artifact_path = body["artifact_uri"].replace("file://", "")
    lines = Path(artifact_path).read_text().splitlines()
    assert len(lines) == body["rows_written"]
    for line in lines:
        parsed = json.loads(line)  # raises if not valid JSON
        assert "timestamp" in parsed
        assert "streams" in parsed
        assert "alignment" in parsed


def test_timestamps_remain_canonical_utc(client: TestClient) -> None:
    imu, gps = _imu_gps_pair(client)
    req = _sync_request(imu["normalization"]["normalization_id"], gps["normalization"]["normalization_id"])
    body = client.post(SYNC_URL, json=req).json()

    artifact_path = body["artifact_uri"].replace("file://", "")
    first_line = json.loads(Path(artifact_path).read_text().splitlines()[0])
    assert first_line["timestamp"].endswith("Z")


def test_missing_alignment_produces_null_not_dropped_row(client: TestClient) -> None:
    imu, gps = _imu_gps_pair(client)
    # Tight tolerance: only exact/near-exact gps hits at t=0 and t=5 will match.
    req = _sync_request(
        imu["normalization"]["normalization_id"],
        gps["normalization"]["normalization_id"],
        alignment={"default_method": "nearest", "max_time_delta_ms": 10},
    )
    body = client.post(SYNC_URL, json=req).json()
    assert body["rows_written"] == 10  # every reference row still present

    artifact_path = body["artifact_uri"].replace("file://", "")
    lines = [json.loads(line) for line in Path(artifact_path).read_text().splitlines()]
    unmatched = [line for line in lines if not line["alignment"]["gps"]["matched"]]
    assert unmatched  # some rows should be unmatched for gps given the tight tolerance
    for line in unmatched:
        assert line["streams"]["gps"] is None
        assert line["alignment"]["gps"]["reason"] == "OUTSIDE_TOLERANCE"
        # the row itself is still present with the other stream populated
        assert line["streams"]["imu"] is not None


def test_per_stream_strategy_override_works(client: TestClient) -> None:
    imu, gps = _imu_gps_pair(client)
    req = _sync_request(
        imu["normalization"]["normalization_id"],
        gps["normalization"]["normalization_id"],
        alignment={
            "default_method": "nearest",
            "max_time_delta_ms": 6000,
            "streams": {"gps": {"method": "linear"}},
        },
    )
    body = client.post(SYNC_URL, json=req).json()
    assert body["status"] == "completed"

    artifact_path = body["artifact_uri"].replace("file://", "")
    lines = [json.loads(line) for line in Path(artifact_path).read_text().splitlines()]
    methods_used = {line["alignment"]["gps"]["method"] for line in lines if line["alignment"]["gps"]["matched"]}
    assert methods_used == {"linear"}


def test_duplicate_stream_names_rejected(client: TestClient) -> None:
    imu, gps = _imu_gps_pair(client)
    req = {
        "streams": [
            {"name": "imu", "normalization_id": imu["normalization"]["normalization_id"]},
            {"name": "imu", "normalization_id": gps["normalization"]["normalization_id"]},
        ],
        "reference": {"mode": "stream", "stream": "imu"},
    }
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 400


def test_fewer_than_two_streams_rejected(client: TestClient) -> None:
    imu, _ = _imu_gps_pair(client)
    req = {
        "streams": [{"name": "imu", "normalization_id": imu["normalization"]["normalization_id"]}],
        "reference": {"mode": "stream", "stream": "imu"},
    }
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 400


def test_zero_streams_rejected(client: TestClient) -> None:
    response = client.post(SYNC_URL, json={"streams": [], "reference": {"mode": "stream", "stream": "imu"}})
    assert response.status_code == 400


def test_reference_stream_must_exist(client: TestClient) -> None:
    imu, gps = _imu_gps_pair(client)
    req = _sync_request(imu["normalization"]["normalization_id"], gps["normalization"]["normalization_id"])
    req["reference"]["stream"] = "does_not_exist"
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 400


def test_missing_normalization_artifact_returns_404(client: TestClient) -> None:
    imu, _ = _imu_gps_pair(client)
    req = _sync_request(imu["normalization"]["normalization_id"], "norm_00000000-0000-0000-0000-000000000000")
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 404


def test_mismatched_session_ids_rejected(client: TestClient) -> None:
    imu, _ = _imu_gps_pair(client, session_id="session_123")
    _, gps_other = _imu_gps_pair(client, session_id="session_456")
    req = _sync_request(
        imu["normalization"]["normalization_id"], gps_other["normalization"]["normalization_id"]
    )
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 409


def test_normalized_checksum_mismatch_returns_409(client: TestClient) -> None:
    imu, gps = _imu_gps_pair(client)
    artifact_path = Path(gps["normalization"]["artifact_uri"].replace("file://", ""))
    original_bytes = artifact_path.read_bytes()
    artifact_path.write_bytes(original_bytes + b"tampered")
    try:
        req = _sync_request(
            imu["normalization"]["normalization_id"], gps["normalization"]["normalization_id"]
        )
        response = client.post(SYNC_URL, json=req)
        assert response.status_code == 409
    finally:
        artifact_path.write_bytes(original_bytes)


def test_unsupported_alignment_method_rejected(client: TestClient) -> None:
    imu, gps = _imu_gps_pair(client)
    req = _sync_request(
        imu["normalization"]["normalization_id"],
        gps["normalization"]["normalization_id"],
        alignment={"default_method": "cubic_spline"},
    )
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 400


def test_fixed_rate_invalid_frequency_rejected(client: TestClient) -> None:
    imu, gps = _imu_gps_pair(client)
    req = _sync_request(
        imu["normalization"]["normalization_id"],
        gps["normalization"]["normalization_id"],
        reference={"mode": "fixed_rate", "frequency_hz": 0.0},
    )
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 400


def test_excessive_configured_frequency_rejected(client: TestClient) -> None:
    imu, gps = _imu_gps_pair(client)
    req = _sync_request(
        imu["normalization"]["normalization_id"],
        gps["normalization"]["normalization_id"],
        reference={"mode": "fixed_rate", "frequency_hz": 999_999.0},
    )
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 400


def test_sub_second_timestamp_precision_preserved(client: TestClient) -> None:
    imu_csv = (
        "timestamp,accel_x,accel_y,accel_z\n"
        "2026-08-30T18:00:00.123456Z,0.1,0.2,9.8\n"
        "2026-08-30T18:00:01.654321Z,0.1,0.2,9.8\n"
    )
    gps_csv = (
        "timestamp,latitude,longitude\n"
        "2026-08-30T18:00:00.123456Z,34.0,-118.0\n"
        "2026-08-30T18:00:01.654321Z,34.1,-118.1\n"
    )
    imu = _pipeline(client, "imu.csv", imu_csv, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id="sess_subsecond")
    gps = _pipeline(client, "gps.csv", gps_csv, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id="sess_subsecond")

    req = _sync_request(imu["normalization"]["normalization_id"], gps["normalization"]["normalization_id"])
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 200, response.text
    body = response.json()

    artifact_path = body["artifact_uri"].replace("file://", "")
    lines = [json.loads(line) for line in Path(artifact_path).read_text().splitlines()]
    assert lines[0]["timestamp"] == "2026-08-30T18:00:00.123456Z"
    assert lines[1]["timestamp"] == "2026-08-30T18:00:01.654321Z"
