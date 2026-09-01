"""A stage failure partway through a run-aware pipeline (v2.6, Design
Requirement 41): the failed stage is `failed`, every remaining stage is
`skipped`, the run is `failed`, and whatever artifacts already committed
successfully remain valid and untouched."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.v26_helpers import submit_run, wait_for_run


def test_cleaning_policy_not_found_fails_the_run_and_skips_the_rest(client: TestClient) -> None:
    result = submit_run(
        client, ["imu", "gps"], session_id="sess_v26_fail",
        config_overrides={"cleaning": {"policy_name": "does_not_exist_policy", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}}},
    )
    final = wait_for_run(client, result["run_id"])
    assert final["status"] == "failed"
    assert final["current_stage"] == "cleaning"

    by_stage = {s["stage"]: s for s in final["stage_runs"]}
    assert by_stage["cleaning"]["status"] == "failed"
    assert by_stage["cleaning"]["error_code"] is not None
    for stage in ("transformation", "qc", "package"):
        assert by_stage[stage]["status"] == "skipped", by_stage[stage]

    # Every stage BEFORE the failure point completed and produced a real,
    # valid, still-retrievable artifact -- a run failure never rolls
    # those back (Design Requirement 41).
    for stage in ("ingestion:imu", "validation:imu", "integrity:imu", "normalization:imu", "synchronization"):
        assert by_stage[stage]["status"] == "completed", by_stage[stage]

    produced_types = {a["artifact_type"] for a in final["artifacts"]}
    assert produced_types == {"ingestion", "validation", "integrity", "normalization", "synchronization"}

    sync_artifact_id = next(a["artifact_id"] for a in final["artifacts"] if a["artifact_type"] == "synchronization")
    assert client.post("/api/v1/catalog/scan").status_code == 200
    detail = client.get(f"/api/v1/catalog/artifacts/synchronization/{sync_artifact_id}")
    assert detail.status_code == 200
    assert detail.json()["artifact"]["artifact_id"] == sync_artifact_id


def test_unknown_sensor_type_is_rejected_before_any_stage_runs(client: TestClient) -> None:
    """Config validation happens before the run is even created --
    Design Requirement 6/8's 'do not hardcode modality names' cuts both
    ways: an unregistered sensor_type is a 400, not a run that fails
    mid-flight."""
    import json

    from tests.v26_helpers import DEFAULT_CONFIG, IMU_CSV

    config = dict(DEFAULT_CONFIG)
    config["streams"] = [{"sensor_type": "not_a_real_sensor", "source_units": {}}]
    resp = client.post("/api/v1/runs", data={"config": json.dumps(config)}, files=[("files", ("imu.csv", IMU_CSV.encode(), "text/csv"))])
    assert resp.status_code == 400
    assert "not_a_real_sensor" in resp.text
