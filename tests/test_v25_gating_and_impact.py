"""HTTP-level tests for the v2.5 downstream-processing gate (deprecated
behavior + override, reactivated chains) and enriched impact analysis
(multi-parent synchronization, unaffected sibling exclusion)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.v25_helpers import GPS_CSV, IMU_CSV, build_downstream_from_normalizations, normalize_stream


def _sync_request(imu_id: str, gps_id: str) -> dict:
    return {
        "streams": [{"name": "imu", "normalization_id": imu_id}, {"name": "gps", "normalization_id": gps_id}],
        "reference": {"mode": "stream", "stream": "imu"},
        "alignment": {"default_method": "nearest", "max_time_delta_ms": 400},
    }


def test_deprecated_direct_input_blocked_by_default_and_allowed_with_override(client: TestClient) -> None:
    imu = normalize_stream(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id="sess_dep")
    gps = normalize_stream(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id="sess_dep")
    imu_id = imu["normalization"]["normalization_id"]
    gps_id = gps["normalization"]["normalization_id"]
    assert client.post("/api/v1/catalog/scan").status_code == 200

    dep = client.post(f"/api/v1/catalog/artifacts/normalization/{imu_id}/deprecate", json={"reason": "superseded by a newer profile"})
    assert dep.status_code == 200

    blocked = client.post("/api/v1/synchronization", json=_sync_request(imu_id, gps_id))
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "ARTIFACT_DEPRECATED"

    allowed = client.post("/api/v1/synchronization?allow_deprecated=true", json=_sync_request(imu_id, gps_id))
    assert allowed.status_code == 200, allowed.text


def test_deprecated_ancestor_blocked_by_default_and_allowed_with_override(client: TestClient) -> None:
    imu = normalize_stream(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id="sess_dep_anc")
    gps = normalize_stream(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id="sess_dep_anc")
    imu_id = imu["normalization"]["normalization_id"]
    gps_id = gps["normalization"]["normalization_id"]
    downstream = build_downstream_from_normalizations(client, imu_normalization_id=imu_id, gps_normalization_id=gps_id)
    sync_id = downstream["sync"]["synchronization_id"]
    assert client.post("/api/v1/catalog/scan").status_code == 200

    dep = client.post(f"/api/v1/catalog/artifacts/normalization/{imu_id}/deprecate", json={"reason": "deprecated after the fact"})
    assert dep.status_code == 200

    blocked = client.post(
        f"/api/v1/cleaning/{sync_id}", json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}}
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "UPSTREAM_ARTIFACT_DEPRECATED"

    allowed = client.post(
        f"/api/v1/cleaning/{sync_id}?allow_deprecated=true",
        json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}},
    )
    assert allowed.status_code == 200, allowed.text


def test_reactivated_artifact_allows_new_downstream_work_again(client: TestClient) -> None:
    imu = normalize_stream(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id="sess_react")
    gps = normalize_stream(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id="sess_react")
    imu_id = imu["normalization"]["normalization_id"]
    gps_id = gps["normalization"]["normalization_id"]
    assert client.post("/api/v1/catalog/scan").status_code == 200

    client.post(f"/api/v1/catalog/artifacts/normalization/{imu_id}/invalidate", json={"reason": "calibration bug suspected"})
    blocked = client.post("/api/v1/synchronization", json=_sync_request(imu_id, gps_id))
    assert blocked.status_code == 409

    reactivate = client.post(f"/api/v1/catalog/artifacts/normalization/{imu_id}/reactivate", json={"reason": "investigation cleared artifact"})
    assert reactivate.status_code == 200
    assert reactivate.json()["state"] == "active"

    allowed = client.post("/api/v1/synchronization", json=_sync_request(imu_id, gps_id))
    assert allowed.status_code == 200, allowed.text


def test_enriched_impact_excludes_unaffected_sibling_branch(client: TestClient) -> None:
    """Marking the GPS normalization invalid must show up in
    synchronization's impact, but a completely SEPARATE, unrelated
    normalization (different session) must never appear anywhere in it."""
    gps = normalize_stream(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id="sess_impact_a")
    imu = normalize_stream(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id="sess_impact_a")
    downstream = build_downstream_from_normalizations(client, imu_normalization_id=imu["normalization"]["normalization_id"], gps_normalization_id=gps["normalization"]["normalization_id"])

    # A fully independent pipeline branch that shares nothing with the one above.
    unrelated = normalize_stream(client, "imu2.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id="sess_impact_unrelated")

    assert client.post("/api/v1/catalog/scan").status_code == 200
    gps_id = gps["normalization"]["normalization_id"]
    client.post(f"/api/v1/catalog/artifacts/normalization/{gps_id}/invalidate", json={"reason": "GPS clock drift discovered"})

    impact = client.get(f"/api/v1/lineage/normalization/{gps_id}/impact/enriched").json()
    assert impact["affected_artifacts"]["synchronization"] == 1
    assert impact["affected_artifacts"]["package"] == 1
    assert impact["affected_packages"] == [downstream["pkg"]["package_id"]]

    # The unrelated branch's normalization must never appear as affected by GPS.
    unrelated_impact = client.get(f"/api/v1/lineage/normalization/{unrelated['normalization']['normalization_id']}/impact/enriched").json()
    assert unrelated_impact["affected_artifacts"] == {}
    assert unrelated_impact["affected_packages"] == []
