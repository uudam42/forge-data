"""Smoke test for the shared 3-sensor pipeline helper -- also doubles as
test item 36 (IMU + GPS + Force/Torque combined synchronization)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.sensors.pipeline_helpers import full_pipeline_to_package


def test_full_three_sensor_pipeline_reaches_packaging(client: TestClient) -> None:
    result = full_pipeline_to_package(client, "sess_ft_smoke")
    assert result["sync"]["status"] == "completed"
    assert result["cleaned"]["status"] == "completed"
    assert result["xform"]["status"] == "completed"
    assert result["qc"]["status"] in ("passed", "passed_with_warnings")
    assert result["pkg"]["status"] == "completed"
