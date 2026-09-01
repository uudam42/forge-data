"""v2.5 — a fault injected partway through a multi-step selective
rebuild must never leave a partial artifact, must never touch anything
already-committed (the old branch, or an earlier step's freshly-rebuilt
artifact), and the rebuild must be safely retryable afterward. Uses
v2.1's real fault-injection mechanism (app.storage.atomic.fault_injector)
-- the same "inject a crash at an exact commit checkpoint" approach
already established and proven there, rather than a timing-based real
process kill (which can't deterministically land mid-way through a
multi-step, sub-millisecond-fast in-memory pipeline)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.storage.atomic import fault_injector
from tests.v25_helpers import CLEANING_CONFIG, GPS_CSV, IMU_CSV, build_downstream_from_normalizations, normalize_stream


class _InjectedCrash(Exception):
    pass


@pytest.fixture(autouse=True)
def _clear_fault_injector():
    fault_injector.clear()
    yield
    fault_injector.clear()


def test_crash_during_second_step_leaves_first_step_valid_and_nothing_partial(client: TestClient) -> None:
    imu = normalize_stream(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id="sess_crash")
    gps = normalize_stream(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id="sess_crash")
    imu_id = imu["normalization"]["normalization_id"]
    gps_id = gps["normalization"]["normalization_id"]
    downstream = build_downstream_from_normalizations(client, imu_normalization_id=imu_id, gps_normalization_id=gps_id)
    old_sync_id = downstream["sync"]["synchronization_id"]
    old_cleaning_id = downstream["cleaned"]["cleaning_id"]
    assert client.post("/api/v1/catalog/scan").status_code == 200

    client.post(f"/api/v1/catalog/artifacts/normalization/{imu_id}/invalidate", json={"reason": "bad calibration"})
    imu_ingestion_id = imu["ingestion"]["ingestion_id"]
    new_norm = client.post(
        f"/api/v1/normalization/{imu_ingestion_id}",
        json={"schema_name": "imu", "schema_version": "1.0.0", "profile_name": "imu_canonical", "profile_version": "1.0.0", "source_units": {"acceleration": "m/s^2", "angular_velocity": "rad/s"}},
    ).json()
    assert client.post("/api/v1/catalog/scan").status_code == 200

    plan = client.post(
        "/api/v1/rebuild/plan",
        json={"replace": {"old_type": "normalization", "old_id": imu_id, "new_type": "normalization", "new_id": new_norm["normalization_id"]}},
    ).json()

    # First BEFORE_RENAME hit is synchronization's own commit -- let it
    # through untouched. The second is cleaning's -- crash it.
    hit_count = {"n": 0}

    def _crash_on_second_commit():
        hit_count["n"] += 1
        if hit_count["n"] == 2:
            raise _InjectedCrash("simulated crash during cleaning's atomic commit")

    fault_injector.install("BEFORE_RENAME", _crash_on_second_commit)

    execute_resp = client.post(
        "/api/v1/rebuild/execute",
        json={"plan_id": plan["plan_id"], "configs": {f"cleaning/{old_cleaning_id}": CLEANING_CONFIG}},
    )
    fault_injector.clear()
    assert execute_resp.status_code == 200, execute_resp.text
    results = execute_resp.json()["results"]
    by_stage = {r["stage_artifact_type"]: r for r in results}

    # --- Step 1 (synchronization) succeeded and is fully valid ---------
    assert by_stage["synchronization"]["status"] == "rebuilt"
    new_sync_id = by_stage["synchronization"]["new_artifact_id"]
    assert new_sync_id != old_sync_id
    assert client.post("/api/v1/catalog/scan").status_code == 200
    new_sync_detail = client.get(f"/api/v1/catalog/artifacts/synchronization/{new_sync_id}").json()
    assert new_sync_detail["artifact"]["artifact_id"] == new_sync_id

    # --- Step 2 (cleaning) failed; nothing after it was attempted ------
    assert by_stage["cleaning"]["status"] == "failed"
    assert "simulated crash" in by_stage["cleaning"]["detail"]
    assert by_stage["transformation"]["status"] == "skipped_upstream_not_rebuilt"
    assert by_stage["qc"]["status"] == "skipped_upstream_not_rebuilt"
    assert by_stage["package"]["status"] == "skipped_upstream_not_rebuilt"

    # --- No partial cleaning artifact ever became visible/discoverable --
    # (v2.1's atomic staging/commit guarantee: an interrupted commit
    # either never renamed into place, or the recovery/scan path simply
    # never treats a staging leftover as a real artifact -- verified here
    # via the authoritative check: does the catalog see a second cleaning
    # artifact at all after a fresh scan?)
    cleaning_artifacts = client.get("/api/v1/catalog/artifacts", params={"stage": "cleaning"}).json()
    assert len(cleaning_artifacts) == 1
    assert cleaning_artifacts[0]["artifact_id"] == old_cleaning_id

    # --- Old lineage is completely untouched -----------------------------
    old_cleaning_detail = client.get(f"/api/v1/catalog/artifacts/cleaning/{old_cleaning_id}").json()
    assert old_cleaning_detail["artifact"]["content_sha256"] == downstream["cleaned"]["cleaned_sha256"]
    old_cleaning_gov = client.get(f"/api/v1/catalog/artifacts/cleaning/{old_cleaning_id}/governance").json()
    assert old_cleaning_gov["state"] == "active"  # never superseded -- its replacement never committed

    # The step that DID succeed (synchronization) is correctly marked superseded.
    old_sync_gov = client.get(f"/api/v1/catalog/artifacts/synchronization/{old_sync_id}/governance").json()
    assert old_sync_gov["state"] == "deprecated"
    assert old_sync_gov["superseded_by_id"] == new_sync_id

    # --- Retry: a fresh plan (the catalog changed, so the old plan_id is
    # gone/stale) with the config supplied this time completes fully -----
    retry_plan = client.post(
        "/api/v1/rebuild/plan",
        json={"replace": {"old_type": "normalization", "old_id": imu_id, "new_type": "normalization", "new_id": new_norm["normalization_id"]}},
    ).json()
    retry_execute = client.post(
        "/api/v1/rebuild/execute",
        json={"plan_id": retry_plan["plan_id"], "configs": {f"cleaning/{old_cleaning_id}": CLEANING_CONFIG}},
    )
    assert retry_execute.status_code == 200, retry_execute.text
    retry_results = {r["stage_artifact_type"]: r["status"] for r in retry_execute.json()["results"]}
    assert retry_results["cleaning"] == "rebuilt"
