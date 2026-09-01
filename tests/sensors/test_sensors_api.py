"""Sensor plugin discovery API tests (Design Requirements 23, 24)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_list_sensors_returns_all_three_builtins(client: TestClient) -> None:
    r = client.get("/api/v1/sensors")
    assert r.status_code == 200
    sensor_types = {s["sensor_type"] for s in r.json()}
    assert sensor_types == {"imu", "gps", "force_torque"}


def test_list_sensors_is_metadata_only(client: TestClient) -> None:
    r = client.get("/api/v1/sensors")
    for entry in r.json():
        assert set(entry.keys()) == {
            "sensor_type", "plugin_version", "display_name", "schema_name", "schema_version",
            "normalization_profile", "normalization_profile_version", "timestamp_field",
            "numeric_fields", "required_fields", "canonical_units", "has_feature_extractor",
        }


def test_get_force_torque_sensor(client: TestClient) -> None:
    r = client.get("/api/v1/sensors/force_torque")
    assert r.status_code == 200
    body = r.json()
    assert body["sensor_type"] == "force_torque"
    assert body["canonical_units"] == {"force": "N", "torque": "N·m"}
    assert body["has_feature_extractor"] is True


def test_get_unknown_sensor_returns_structured_404(client: TestClient) -> None:
    r = client.get("/api/v1/sensors/lidar")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "lidar" in detail
    assert "imu" in detail and "gps" in detail and "force_torque" in detail


def test_deterministic_listing_order(client: TestClient) -> None:
    r1 = client.get("/api/v1/sensors").json()
    r2 = client.get("/api/v1/sensors").json()
    assert [s["sensor_type"] for s in r1] == [s["sensor_type"] for s in r2] == ["force_torque", "gps", "imu"]


def test_transformation_features_for_unregistered_sensor_is_structured_400(client: TestClient) -> None:
    """Requesting features for a stream name that has no registered
    sensor plugin fails with a clear, structured 400 -- never a generic
    KeyError/500."""
    from tests.sensors.pipeline_helpers import clean, synchronize, three_sensor_normalized

    normalized = three_sensor_normalized(client, "sess_unknown_sensor_features")
    sync = synchronize(client, normalized)
    cleaned = clean(client, sync["synchronization_id"])

    r = client.post(
        f"/api/v1/transformation/{cleaned['cleaning_id']}",
        json={
            "profile_name": "multimodal_window_v1", "profile_version": "1.0.0",
            "config": {
                "window": {"mode": "count", "size": 5, "stride": 5, "drop_incomplete": True},
                "features": {"lidar": {"statistics": ["mean"]}},
            },
        },
    )
    assert r.status_code == 400, r.text
    assert "lidar" in r.text
