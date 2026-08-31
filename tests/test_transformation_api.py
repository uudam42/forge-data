"""End-to-end tests for the transformation HTTP API.

Covers the full ingest -> validate -> integrity -> normalize -> synchronize
-> clean -> transform pipeline, request validation, and the request-level
error cases.
"""

from __future__ import annotations

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


def _synchronized(client: TestClient, session_id: str = "sess_xform_api") -> dict:
    imu = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    response = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": imu["normalization_id"]},
                {"name": "gps", "normalization_id": gps["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 500},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _cleaned(client: TestClient, synchronization_id: str, **config_overrides) -> dict:
    config = {"required_streams": ["imu"]}
    config.update(config_overrides)
    response = client.post(
        f"/api/v1/cleaning/{synchronization_id}",
        json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": config},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _default_transform_request(**window_overrides) -> dict:
    window = {"mode": "count", "size": 10, "stride": 5, "drop_incomplete": True}
    window.update(window_overrides)
    return {
        "profile_name": "multimodal_window_v1",
        "profile_version": "1.0.0",
        "config": {
            "window": window,
            "features": {
                "imu": {"include_raw": True, "statistics": ["mean", "std", "min", "max"], "derived": ["accel_magnitude"]},
                "gps": {"include_raw": False, "statistics": ["mean"]},
                "include_modality_mask": True,
                "include_relative_time": True,
            },
        },
    }


def test_valid_cleaned_dataset_transforms_successfully(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["summary"]["input_rows"] == 20
    assert body["summary"]["samples_written"] == 3  # size=10 stride=5 drop_incomplete -> windows at 0,5,10


def test_jsonl_output_valid(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    body = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request()).json()

    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    lines = artifact_path.read_text().splitlines()
    assert len(lines) == body["summary"]["samples_written"]
    for line in lines:
        parsed = json.loads(line)
        assert "sample_id" in parsed
        assert "window" in parsed
        assert "features" in parsed
        assert "metadata" in parsed


def test_sample_contains_no_label_fields(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    body = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request()).json()
    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    for line in artifact_path.read_text().splitlines():
        parsed = json.loads(line)
        assert "label" not in parsed
        assert "labels" not in parsed
        assert "split" not in parsed


def test_include_raw_true_includes_raw_sequences(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    body = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request()).json()
    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    sample = json.loads(artifact_path.read_text().splitlines()[0])
    assert "raw" in sample["features"]["imu"]
    assert len(sample["features"]["imu"]["raw"]["accel_x"]) == 10


def test_include_raw_false_omits_raw_sequences(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    request = _default_transform_request()
    request["config"]["features"]["imu"]["include_raw"] = False
    body = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request).json()
    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    sample = json.loads(artifact_path.read_text().splitlines()[0])
    assert "raw" not in sample["features"]["imu"]
    assert "statistics" in sample["features"]["imu"]


def test_modality_mask_present_when_requested(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    body = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request()).json()
    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    sample = json.loads(artifact_path.read_text().splitlines()[0])
    assert sample["modality_mask"] == {"imu": True, "gps": True}


def test_modality_mask_absent_when_not_requested(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    request = _default_transform_request()
    request["config"]["features"]["include_modality_mask"] = False
    body = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request).json()
    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    sample = json.loads(artifact_path.read_text().splitlines()[0])
    assert "modality_mask" not in sample


def test_relative_time_correctness(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    body = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request()).json()
    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    sample = json.loads(artifact_path.read_text().splitlines()[0])
    offsets = sample["metadata"]["relative_time_ms"]
    assert offsets[0] == 0.0
    assert offsets == sorted(offsets)


def test_overlapping_windows_share_rows(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    body = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request()).json()
    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    lines = [json.loads(line) for line in artifact_path.read_text().splitlines()]
    sample0, sample1 = lines[0], lines[1]
    assert sample0["metadata"]["source_row_end"] >= sample1["metadata"]["source_row_start"]


def test_window_provenance_fields(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    body = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request()).json()
    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    sample = json.loads(artifact_path.read_text().splitlines()[0])
    assert sample["window"]["index"] == 0
    assert sample["window"]["row_count"] == 10
    assert sample["metadata"]["source_row_start"] == 0
    assert sample["metadata"]["source_row_end"] == 9


def test_gps_missing_data_handling(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    body = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request()).json()
    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    lines = [json.loads(line) for line in artifact_path.read_text().splitlines()]
    coverages = [line["modality_coverage"]["gps"] for line in lines]
    assert any(c < 1.0 for c in coverages)  # GPS only every 4th row -> partial coverage somewhere
    assert all(0.0 <= c <= 1.0 for c in coverages)


def test_report_persisted(client: TestClient, transformed_root: Path) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    body = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request()).json()

    report_path = transformed_root / cleaned["cleaning_id"] / body["transformation_id"] / "report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert report["transformation_id"] == body["transformation_id"]
    assert report["summary"]["samples_written"] == body["summary"]["samples_written"]
    assert "imu" in report["modality_coverage"]
    assert "gps" in report["modality_coverage"]


def test_cleaning_not_found_returns_404(client: TestClient) -> None:
    response = client.post(f"{XFORM_URL}/clean_does_not_exist", json=_default_transform_request())
    assert response.status_code == 404


def test_profile_not_found_returns_404(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    request = _default_transform_request()
    request["profile_name"] = "does_not_exist"
    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request)
    assert response.status_code == 404


def test_rejected_cleaning_returns_409(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"], minimum_retained_rows=1000)
    assert cleaned["status"] == "rejected"
    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request())
    assert response.status_code == 409


def test_cleaned_checksum_mismatch_returns_409(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    artifact_path = Path(cleaned["artifact_uri"].replace("file://", ""))
    original = artifact_path.read_bytes()
    artifact_path.write_bytes(original + b"tampered")
    try:
        response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request())
        assert response.status_code == 409
    finally:
        artifact_path.write_bytes(original)


def test_invalid_window_size_returns_400(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request(size=0))
    assert response.status_code == 400


def test_invalid_window_stride_returns_400(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request(stride=-1))
    assert response.status_code == 400


def test_unsupported_window_mode_returns_400(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request(mode="bogus"))
    assert response.status_code == 400


def test_unknown_statistic_returns_400(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    request = _default_transform_request()
    request["config"]["features"]["imu"]["statistics"] = ["bogus_stat"]
    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request)
    assert response.status_code == 400


def test_unknown_derived_feature_returns_400(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    request = _default_transform_request()
    request["config"]["features"]["imu"]["derived"] = ["orientation"]
    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request)
    assert response.status_code == 400


def test_features_requested_for_unknown_stream_returns_400(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    request = _default_transform_request()
    request["config"]["features"]["imu"] = {"statistics": ["mean"]}
    # "lidar" isn't a stream this profile even models, so requesting it is
    # rejected by pydantic (extra field) rather than reaching the service —
    # assert the request is rejected either way (422 from pydantic or 400
    # from the service), proving unknown-stream features are never silently
    # accepted.
    request["config"]["features"]["lidar"] = {"statistics": ["mean"]}
    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request)
    assert response.status_code in (400, 422)


def test_empty_cleaned_dataset_returns_200_with_zero_samples(client: TestClient) -> None:
    import hashlib

    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    artifact_path = Path(cleaned["artifact_uri"].replace("file://", ""))
    artifact_path.write_text("")
    manifest_path = artifact_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["cleaned_sha256"] = hashlib.sha256(b"").hexdigest()
    manifest["cleaned_size_bytes"] = 0
    manifest_path.write_text(json.dumps(manifest))

    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=_default_transform_request())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["summary"]["samples_written"] == 0
    assert body["summary"]["input_rows"] == 0


def test_time_window_mode_end_to_end(client: TestClient) -> None:
    sync = _synchronized(client)
    cleaned = _cleaned(client, sync["synchronization_id"])
    request = {
        "profile_name": "multimodal_window_v1",
        "profile_version": "1.0.0",
        "config": {
            "window": {"mode": "time", "duration_ms": 3000, "stride_ms": 2000, "drop_incomplete": True},
            "features": {"imu": {"statistics": ["mean"]}},
        },
    }
    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["window_mode"] == "time"
    assert body["summary"]["samples_written"] > 0
