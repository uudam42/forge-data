"""Tests for the transformation config hash and byte-for-byte determinism
of the transformed artifact and its sample IDs.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.transformation.models import TransformationConfig
from app.transformation.profiles.multimodal_window import MULTIMODAL_WINDOW_V1
from app.transformation.serialization import canonical_json, compute_sample_id

XFORM_URL = "/api/v1/transformation"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,0.{i},0.2,9.8,0.01,0.02,0.03\n" for i in range(20)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,34.02{i:02d},-118.28{i:02d},100.0,9.{i}\n" for i in range(0, 20, 4)
)


# ---------------------------------------------------------------------------
# Config hash — unit level
# ---------------------------------------------------------------------------


def test_config_hash_deterministic() -> None:
    config = TransformationConfig.model_validate(
        {"window": {"mode": "count", "size": 10, "stride": 5}, "features": {"imu": {"statistics": ["mean"]}}}
    )
    h1 = MULTIMODAL_WINDOW_V1.config_hash(config)
    h2 = MULTIMODAL_WINDOW_V1.config_hash(
        TransformationConfig.model_validate(
            {"window": {"mode": "count", "size": 10, "stride": 5}, "features": {"imu": {"statistics": ["mean"]}}}
        )
    )
    assert h1 == h2
    assert len(h1) == 64


def test_changing_window_size_changes_config_hash() -> None:
    h1 = MULTIMODAL_WINDOW_V1.config_hash(
        TransformationConfig.model_validate({"window": {"mode": "count", "size": 10, "stride": 5}})
    )
    h2 = MULTIMODAL_WINDOW_V1.config_hash(
        TransformationConfig.model_validate({"window": {"mode": "count", "size": 20, "stride": 5}})
    )
    assert h1 != h2


def test_changing_feature_set_changes_config_hash() -> None:
    h1 = MULTIMODAL_WINDOW_V1.config_hash(
        TransformationConfig.model_validate(
            {"window": {"mode": "count", "size": 10, "stride": 5}, "features": {"imu": {"statistics": ["mean"]}}}
        )
    )
    h2 = MULTIMODAL_WINDOW_V1.config_hash(
        TransformationConfig.model_validate(
            {
                "window": {"mode": "count", "size": 10, "stride": 5},
                "features": {"imu": {"statistics": ["mean", "std"]}},
            }
        )
    )
    assert h1 != h2


def test_config_hash_independent_of_dict_key_order() -> None:
    payload_a = {"b": 2, "a": 1}
    payload_b = {"a": 1, "b": 2}
    assert canonical_json(payload_a) == canonical_json(payload_b)


def test_sample_id_deterministic_for_same_inputs() -> None:
    id1 = compute_sample_id(cleaned_sha256="abc", config_hash="def", window_index=0, start_epoch_us=0, end_epoch_us=1000)
    id2 = compute_sample_id(cleaned_sha256="abc", config_hash="def", window_index=0, start_epoch_us=0, end_epoch_us=1000)
    assert id1 == id2


def test_sample_id_differs_for_different_window_index() -> None:
    id1 = compute_sample_id(cleaned_sha256="abc", config_hash="def", window_index=0, start_epoch_us=0, end_epoch_us=1000)
    id2 = compute_sample_id(cleaned_sha256="abc", config_hash="def", window_index=1, start_epoch_us=1000, end_epoch_us=2000)
    assert id1 != id2


def test_sample_id_differs_for_different_config_hash() -> None:
    id1 = compute_sample_id(cleaned_sha256="abc", config_hash="def", window_index=0, start_epoch_us=0, end_epoch_us=1000)
    id2 = compute_sample_id(cleaned_sha256="abc", config_hash="ghi", window_index=0, start_epoch_us=0, end_epoch_us=1000)
    assert id1 != id2


def test_sample_id_differs_for_different_cleaned_sha256() -> None:
    id1 = compute_sample_id(cleaned_sha256="abc", config_hash="def", window_index=0, start_epoch_us=0, end_epoch_us=1000)
    id2 = compute_sample_id(cleaned_sha256="xyz", config_hash="def", window_index=0, start_epoch_us=0, end_epoch_us=1000)
    assert id1 != id2


# ---------------------------------------------------------------------------
# End-to-end byte determinism
# ---------------------------------------------------------------------------


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


def _cleaned(client: TestClient, session_id: str = "sess_xform_determinism") -> dict:
    imu = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    sync = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": imu["normalization_id"]},
                {"name": "gps", "normalization_id": gps["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 500},
        },
    ).json()
    return client.post(
        f"/api/v1/cleaning/{sync['synchronization_id']}",
        json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}},
    ).json()


def test_same_input_and_config_creates_byte_identical_transformed_artifact(
    client: TestClient, transformed_root: Path
) -> None:
    cleaned = _cleaned(client)
    request = {
        "profile_name": "multimodal_window_v1",
        "profile_version": "1.0.0",
        "config": {
            "window": {"mode": "count", "size": 10, "stride": 5, "drop_incomplete": True},
            "features": {"imu": {"include_raw": True, "statistics": ["mean", "std"], "derived": ["accel_magnitude"]}},
        },
    }

    body1 = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request).json()
    body2 = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request).json()

    assert body1["transformation_id"] != body2["transformation_id"]
    assert body1["transformed_sha256"] == body2["transformed_sha256"]

    bytes1 = (transformed_root / cleaned["cleaning_id"] / body1["transformation_id"] / "transformed.jsonl").read_bytes()
    bytes2 = (transformed_root / cleaned["cleaning_id"] / body2["transformation_id"] / "transformed.jsonl").read_bytes()
    assert bytes1 == bytes2


def test_deterministic_sample_ids_across_runs(client: TestClient, transformed_root: Path) -> None:
    cleaned = _cleaned(client)
    request = {
        "profile_name": "multimodal_window_v1",
        "profile_version": "1.0.0",
        "config": {"window": {"mode": "count", "size": 10, "stride": 5, "drop_incomplete": True}},
    }
    body1 = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request).json()
    body2 = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request).json()

    lines1 = (transformed_root / cleaned["cleaning_id"] / body1["transformation_id"] / "transformed.jsonl").read_text().splitlines()
    lines2 = (transformed_root / cleaned["cleaning_id"] / body2["transformation_id"] / "transformed.jsonl").read_text().splitlines()
    ids1 = [json.loads(line)["sample_id"] for line in lines1]
    ids2 = [json.loads(line)["sample_id"] for line in lines2]
    assert ids1 == ids2
    assert len(ids1) == len(set(ids1))  # sample IDs unique within a run


def test_different_config_produces_different_sample_ids(client: TestClient, transformed_root: Path) -> None:
    cleaned = _cleaned(client)
    request_a = {
        "profile_name": "multimodal_window_v1",
        "profile_version": "1.0.0",
        "config": {"window": {"mode": "count", "size": 10, "stride": 5, "drop_incomplete": True}},
    }
    request_b = {
        "profile_name": "multimodal_window_v1",
        "profile_version": "1.0.0",
        "config": {"window": {"mode": "count", "size": 10, "stride": 5, "drop_incomplete": True}, "features": {"imu": {"statistics": ["mean"]}}},
    }
    body_a = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request_a).json()
    body_b = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request_b).json()

    line_a = (transformed_root / cleaned["cleaning_id"] / body_a["transformation_id"] / "transformed.jsonl").read_text().splitlines()[0]
    line_b = (transformed_root / cleaned["cleaning_id"] / body_b["transformation_id"] / "transformed.jsonl").read_text().splitlines()[0]
    assert json.loads(line_a)["sample_id"] != json.loads(line_b)["sample_id"]


def test_canonical_json_serialization_used_for_output(client: TestClient) -> None:
    cleaned = _cleaned(client)
    request = {
        "profile_name": "multimodal_window_v1",
        "profile_version": "1.0.0",
        "config": {"window": {"mode": "count", "size": 10, "stride": 5, "drop_incomplete": True}},
    }
    body = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request).json()

    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    first_line = artifact_path.read_text().splitlines()[0]

    assert ": " not in first_line
    assert ", " not in first_line
    parsed = json.loads(first_line)
    row_keys = list(parsed.keys())
    assert row_keys == sorted(row_keys)
