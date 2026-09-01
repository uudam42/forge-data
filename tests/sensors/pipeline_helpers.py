"""Shared helpers for building a real IMU + GPS + Force/Torque pipeline
through the HTTP API. Not a test module itself."""

from __future__ import annotations

from fastapi.testclient import TestClient

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,0.{i%10},0.2,9.8,0.01,0.02,0.03\n" for i in range(40)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,34.02{i%90:02d},-118.28{i%90:02d},100.0,9.{i%9}\n" for i in range(0, 40, 3)
)
FT_CSV = "timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,1.{i%10},2.{i%10},9.8,0.1{i%10},0.2{i%10},0.3{i%10}\n" for i in range(0, 40, 2)
)


def upload(client: TestClient, filename: str, content: str, **fields) -> dict:
    r = client.post("/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=fields)
    assert r.status_code == 201, r.text
    return r.json()


def pipeline_to_normalized(
    client: TestClient, filename: str, content: str, schema_name: str, profile_name: str, source_units: dict, **fields
) -> dict:
    ingestion = upload(client, filename, content, **fields)
    for path in (f"/api/v1/validation/{ingestion['ingestion_id']}", f"/api/v1/integrity/{ingestion['ingestion_id']}"):
        r = client.post(path, json={"schema_name": schema_name, "schema_version": "1.0.0"})
        assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/normalization/{ingestion['ingestion_id']}",
        json={
            "schema_name": schema_name, "schema_version": "1.0.0",
            "profile_name": profile_name, "profile_version": "1.0.0",
            "source_units": source_units,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def three_sensor_normalized(client: TestClient, session_id: str) -> dict:
    imu = pipeline_to_normalized(
        client, "imu.csv", IMU_CSV, "imu", "imu_canonical",
        {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id,
    )
    gps = pipeline_to_normalized(
        client, "gps.csv", GPS_CSV, "gps", "gps_canonical",
        {"altitude": "m", "speed": "m/s"}, session_id=session_id,
    )
    ft = pipeline_to_normalized(
        client, "ft.csv", FT_CSV, "force_torque", "force_torque_canonical",
        {"force": "N", "torque": "N*m"}, session_id=session_id,
    )
    return {"imu": imu, "gps": gps, "force_torque": ft}


def synchronize(client: TestClient, normalized: dict, *, method: str = "nearest", max_time_delta_ms: float = 400) -> dict:
    r = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": normalized["imu"]["normalization_id"]},
                {"name": "gps", "normalization_id": normalized["gps"]["normalization_id"]},
                {"name": "force_torque", "normalization_id": normalized["force_torque"]["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": method, "max_time_delta_ms": max_time_delta_ms},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def clean(client: TestClient, synchronization_id: str, **config_overrides) -> dict:
    config = {"required_streams": ["imu"]}
    config.update(config_overrides)
    r = client.post(
        f"/api/v1/cleaning/{synchronization_id}",
        json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": config},
    )
    assert r.status_code == 200, r.text
    return r.json()


def transform(client: TestClient, cleaning_id: str, *, features: dict, window_size: int = 10, window_stride: int = 10) -> dict:
    r = client.post(
        f"/api/v1/transformation/{cleaning_id}",
        json={
            "profile_name": "multimodal_window_v1", "profile_version": "1.0.0",
            "config": {
                "window": {"mode": "count", "size": window_size, "stride": window_stride, "drop_incomplete": True},
                "features": features,
            },
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def qc(client: TestClient, transformation_id: str, **config_overrides) -> dict:
    config = {"minimum_samples": 1}
    config.update(config_overrides)
    r = client.post(
        f"/api/v1/qc/{transformation_id}",
        json={"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": config},
    )
    assert r.status_code == 200, r.text
    return r.json()


def package(client: TestClient, transformation_id: str, qc_id: str, *, seed: int = 42) -> dict:
    r = client.post(
        f"/api/v1/packaging/{transformation_id}",
        json={
            "qc_id": qc_id, "profile_name": "default_ml_package", "profile_version": "1.0.0",
            "config": {
                "split": {"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": seed},
                "grouping": {"mode": "source_overlap"}, "exports": ["jsonl"],
            },
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


DEFAULT_FEATURES = {
    "imu": {"statistics": ["mean"], "derived": ["accel_magnitude"]},
    "gps": {"statistics": ["mean"]},
    "force_torque": {"statistics": ["mean", "std"], "derived": ["force_magnitude", "torque_magnitude"]},
}


def full_pipeline_to_package(client: TestClient, session_id: str, *, seed: int = 42) -> dict:
    normalized = three_sensor_normalized(client, session_id)
    sync = synchronize(client, normalized)
    cleaned = clean(client, sync["synchronization_id"])
    xform = transform(client, cleaned["cleaning_id"], features=DEFAULT_FEATURES)
    qc_result = qc(client, xform["transformation_id"])
    pkg = package(client, xform["transformation_id"], qc_result["qc_id"], seed=seed)
    return {
        "normalized": normalized, "sync": sync, "cleaned": cleaned,
        "xform": xform, "qc": qc_result, "pkg": pkg,
    }
