"""Unit/service-level tests for the transformation registry, profile
validation, and settings-driven window limits that aren't already covered
by the HTTP-level test files."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.transformation.models import TransformationConfig
from app.transformation.profiles.base import InvalidTransformationConfigurationError, UnsupportedWindowModeError
from app.transformation.profiles.multimodal_window import MULTIMODAL_WINDOW_V1
from app.transformation.registry import TransformationProfileNotFoundError, TransformationProfileRegistry

XFORM_URL = "/api/v1/transformation"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,0.{i},0.2,9.8,0.01,0.02,0.03\n" for i in range(20)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,34.02{i:02d},-118.28{i:02d},100.0,9.{i}\n" for i in range(0, 20, 4)
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_finds_builtin_profile() -> None:
    registry = TransformationProfileRegistry()
    profile = registry.get("multimodal_window_v1", "1.0.0")
    assert profile is MULTIMODAL_WINDOW_V1


def test_registry_raises_for_unknown_profile() -> None:
    registry = TransformationProfileRegistry()
    with pytest.raises(TransformationProfileNotFoundError):
        registry.get("does_not_exist", "1.0.0")


def test_registry_raises_for_wrong_version() -> None:
    registry = TransformationProfileRegistry()
    with pytest.raises(TransformationProfileNotFoundError):
        registry.get("multimodal_window_v1", "9.9.9")


def test_registry_list_profiles() -> None:
    registry = TransformationProfileRegistry()
    assert ("multimodal_window_v1", "1.0.0") in registry.list_profiles()


# ---------------------------------------------------------------------------
# Profile validation — unit level
# ---------------------------------------------------------------------------


def test_validate_config_rejects_unsupported_window_mode() -> None:
    config = TransformationConfig.model_validate({"window": {"mode": "frequency"}})
    with pytest.raises(UnsupportedWindowModeError):
        MULTIMODAL_WINDOW_V1.validate_config(
            config, known_streams=["imu"], max_window_size=1000, max_time_window_ms=60000
        )


def test_validate_config_rejects_missing_count_fields() -> None:
    config = TransformationConfig.model_validate({"window": {"mode": "count"}})
    with pytest.raises(InvalidTransformationConfigurationError):
        MULTIMODAL_WINDOW_V1.validate_config(
            config, known_streams=["imu"], max_window_size=1000, max_time_window_ms=60000
        )


def test_validate_config_rejects_missing_time_fields() -> None:
    config = TransformationConfig.model_validate({"window": {"mode": "time"}})
    with pytest.raises(InvalidTransformationConfigurationError):
        MULTIMODAL_WINDOW_V1.validate_config(
            config, known_streams=["imu"], max_window_size=1000, max_time_window_ms=60000
        )


def test_validate_config_enforces_max_window_size() -> None:
    config = TransformationConfig.model_validate({"window": {"mode": "count", "size": 500, "stride": 10}})
    with pytest.raises(InvalidTransformationConfigurationError):
        MULTIMODAL_WINDOW_V1.validate_config(
            config, known_streams=["imu"], max_window_size=100, max_time_window_ms=60000
        )


def test_validate_config_enforces_max_time_window_ms() -> None:
    config = TransformationConfig.model_validate(
        {"window": {"mode": "time", "duration_ms": 999999, "stride_ms": 1000}}
    )
    with pytest.raises(InvalidTransformationConfigurationError):
        MULTIMODAL_WINDOW_V1.validate_config(
            config, known_streams=["imu"], max_window_size=1000, max_time_window_ms=60000
        )


def test_validate_config_accepts_within_limits() -> None:
    config = TransformationConfig.model_validate({"window": {"mode": "count", "size": 50, "stride": 10}})
    MULTIMODAL_WINDOW_V1.validate_config(
        config, known_streams=["imu"], max_window_size=100, max_time_window_ms=60000
    )  # no raise


def test_validate_config_rejects_features_for_stream_not_in_known_streams() -> None:
    config = TransformationConfig.model_validate(
        {"window": {"mode": "count", "size": 10, "stride": 5}, "features": {"gps": {"statistics": ["mean"]}}}
    )
    with pytest.raises(InvalidTransformationConfigurationError):
        MULTIMODAL_WINDOW_V1.validate_config(
            config, known_streams=["imu"], max_window_size=1000, max_time_window_ms=60000
        )


# ---------------------------------------------------------------------------
# Settings-driven limits enforced end-to-end via the API
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


def _cleaned(client: TestClient, session_id: str = "sess_xform_service") -> dict:
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


def test_window_size_exceeding_configured_max_returns_400(client: TestClient) -> None:
    cleaned = _cleaned(client)
    request = {
        "profile_name": "multimodal_window_v1",
        "profile_version": "1.0.0",
        "config": {"window": {"mode": "count", "size": 200_000, "stride": 10}},  # exceeds default MAX_WINDOW_SIZE
    }
    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request)
    assert response.status_code == 400


def test_time_window_exceeding_configured_max_returns_400(client: TestClient) -> None:
    cleaned = _cleaned(client)
    request = {
        "profile_name": "multimodal_window_v1",
        "profile_version": "1.0.0",
        "config": {"window": {"mode": "time", "duration_ms": 10_000_000, "stride_ms": 1000}},
    }
    response = client.post(f"{XFORM_URL}/{cleaned['cleaning_id']}", json=request)
    assert response.status_code == 400
