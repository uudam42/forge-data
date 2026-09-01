"""HTTP-level tests for selective-rebuild execution edge cases not
already covered by the flagship scenario: a stale plan, a step skipped
for missing manual configuration, and an unknown plan_id."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.v25_helpers import GPS_CSV, IMU_CSV, build_downstream_from_normalizations, normalize_stream


def _build_replaceable_scenario(client: TestClient, session_id: str) -> dict:
    imu = normalize_stream(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = normalize_stream(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    downstream = build_downstream_from_normalizations(client, imu_normalization_id=imu["normalization"]["normalization_id"], gps_normalization_id=gps["normalization"]["normalization_id"])
    assert client.post("/api/v1/catalog/scan").status_code == 200

    imu_ingestion_id = imu["ingestion"]["ingestion_id"]
    new_norm = client.post(
        f"/api/v1/normalization/{imu_ingestion_id}",
        json={"schema_name": "imu", "schema_version": "1.0.0", "profile_name": "imu_canonical", "profile_version": "1.0.0", "source_units": {"acceleration": "m/s^2", "angular_velocity": "rad/s"}},
    ).json()
    assert client.post("/api/v1/catalog/scan").status_code == 200
    return {"imu": imu, "gps": gps, "downstream": downstream, "new_norm_id": new_norm["normalization_id"]}


def test_plan_not_found(client: TestClient) -> None:
    resp = client.post("/api/v1/rebuild/execute", json={"plan_id": "nonexistent_plan_id", "configs": {}})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "REBUILD_PLAN_NOT_FOUND"


def test_stale_plan_rejected_when_catalog_changes_after_planning(client: TestClient) -> None:
    scenario = _build_replaceable_scenario(client, "sess_stale")
    imu_id = scenario["imu"]["normalization"]["normalization_id"]

    plan_resp = client.post(
        "/api/v1/rebuild/plan",
        json={"replace": {"old_type": "normalization", "old_id": imu_id, "new_type": "normalization", "new_id": scenario["new_norm_id"]}},
    )
    assert plan_resp.status_code == 200
    plan_id = plan_resp.json()["plan_id"]

    # Catalog changes materially: a fresh, unrelated downstream branch is
    # added to the SAME old normalization after the plan was built.
    extra = build_downstream_from_normalizations(
        client, imu_normalization_id=imu_id, gps_normalization_id=scenario["gps"]["normalization"]["normalization_id"]
    )
    assert client.post("/api/v1/catalog/scan").status_code == 200

    execute_resp = client.post("/api/v1/rebuild/execute", json={"plan_id": plan_id, "configs": {}})
    assert execute_resp.status_code == 409, execute_resp.text
    assert execute_resp.json()["detail"]["code"] == "REBUILD_PLAN_STALE"


def test_step_skipped_when_manual_configuration_not_supplied(client: TestClient) -> None:
    scenario = _build_replaceable_scenario(client, "sess_skip")
    imu_id = scenario["imu"]["normalization"]["normalization_id"]

    plan_resp = client.post(
        "/api/v1/rebuild/plan",
        json={"replace": {"old_type": "normalization", "old_id": imu_id, "new_type": "normalization", "new_id": scenario["new_norm_id"]}},
    )
    assert plan_resp.status_code == 200
    plan = plan_resp.json()

    execute_resp = client.post("/api/v1/rebuild/execute", json={"plan_id": plan["plan_id"], "configs": {}})
    assert execute_resp.status_code == 200, execute_resp.text
    results = execute_resp.json()["results"]
    assert len(results) == 5  # every planned step is reported, even ones never attempted
    by_stage = {r["stage_artifact_type"]: r for r in results}
    assert by_stage["synchronization"]["status"] == "rebuilt"  # fully auto-reconstructable
    assert by_stage["cleaning"]["status"] == "skipped_manual_configuration_required"
    # Everything downstream of the skipped cleaning step is cascaded-skipped
    # too, rather than being attempted against a parent that was never produced.
    assert by_stage["transformation"]["status"] == "skipped_upstream_not_rebuilt"
    assert by_stage["qc"]["status"] == "skipped_upstream_not_rebuilt"
    assert by_stage["package"]["status"] == "skipped_upstream_not_rebuilt"
