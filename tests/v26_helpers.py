"""Shared helper for driving the v2.6 run-aware pipeline API
(`POST /api/v1/runs`) through the real HTTP layer in tests."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,0.{i%10},0.2,9.8,0.01,0.02,0.03\n" for i in range(40)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,34.02{i%90:02d},-118.28{i%90:02d},100.0,9.{i%9}\n" for i in range(0, 40, 3)
)
FORCE_TORQUE_CSV = "timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,0.{i%10},0.2,9.8,0.01,0.02,0.03\n" for i in range(40)
)

DEFAULT_CONFIG = {
    "synchronization": {"reference": {"mode": "stream", "stream": "imu"}, "alignment": {"default_method": "nearest", "max_time_delta_ms": 400}},
    "cleaning": {"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}},
    "transformation": {"profile_name": "multimodal_window_v1", "profile_version": "1.0.0", "config": {"window": {"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True}}},
    "qc": {"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}},
    "packaging": {
        "profile_name": "default_ml_package", "profile_version": "1.0.0",
        "config": {"split": {"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}, "grouping": {"mode": "source_overlap"}, "exports": ["jsonl"]},
    },
}

STREAM_FILES = {
    "imu": ("imu.csv", IMU_CSV, {"acceleration": "m/s^2", "angular_velocity": "rad/s"}),
    "gps": ("gps.csv", GPS_CSV, {"altitude": "m", "speed": "m/s"}),
    "force_torque": ("ft.csv", FORCE_TORQUE_CSV, {"force": "N", "torque": "N*m"}),
}


def submit_run(client: TestClient, sensor_types: list[str], *, config_overrides: dict | None = None, session_id: str | None = None) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if config_overrides:
        config.update(config_overrides)
    if session_id is not None:
        config["session_id"] = session_id
    config["streams"] = [{"sensor_type": s, "source_units": STREAM_FILES[s][2]} for s in sensor_types]

    files = [("files", (STREAM_FILES[s][0], STREAM_FILES[s][1].encode(), "text/csv")) for s in sensor_types]
    resp = client.post("/api/v1/runs", data={"config": json.dumps(config)}, files=files)
    assert resp.status_code == 202, resp.text
    return resp.json()


def wait_for_run(client: TestClient, run_id: str, *, max_polls: int = 100) -> dict:
    """With TestClient, BackgroundTasks execute synchronously as part of
    the request/response cycle, so in practice the run is already
    finished by the time submit_run() returns -- this poll loop exists
    for readability/robustness (and works unmodified against a real
    live server, where it actually would need to poll)."""
    body = client.get(f"/api/v1/runs/{run_id}").json()
    for _ in range(max_polls):
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        body = client.get(f"/api/v1/runs/{run_id}").json()
    return body
