"""End-to-end run-aware pipeline execution through the real HTTP API
(v2.6): IMU+GPS and IMU+GPS+Force/Torque, verifying every stage status,
artifact association, and that the final package's content is
byte-identical to running the exact same pipeline through the legacy,
non-run-aware stage-by-stage API (Design Requirement 55: observability
must never change determinism)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.v25_helpers import build_downstream_from_normalizations, normalize_stream
from tests.v26_helpers import GPS_CSV, IMU_CSV, submit_run, wait_for_run


def test_imu_gps_full_run(client: TestClient) -> None:
    result = submit_run(client, ["imu", "gps"], session_id="sess_v26_imu_gps")
    run_id = result["run_id"]
    assert result["status"] == "queued"

    final = wait_for_run(client, run_id)
    assert final["status"] == "completed", final
    assert final["stages_total"] == 13  # 4*2 streams + 5 downstream
    assert final["stages_completed"] == 13
    assert all(s["status"] == "completed" for s in final["stage_runs"])

    artifact_types = {a["artifact_type"] for a in final["artifacts"]}
    assert artifact_types == {"ingestion", "validation", "integrity", "normalization", "synchronization", "cleaning", "transformation", "qc", "package"}
    package_artifacts = [a for a in final["artifacts"] if a["artifact_type"] == "package"]
    assert len(package_artifacts) == 1


def test_imu_gps_force_torque_full_run(client: TestClient) -> None:
    result = submit_run(client, ["imu", "gps", "force_torque"], session_id="sess_v26_triple")
    final = wait_for_run(client, result["run_id"])
    assert final["status"] == "completed", final
    assert final["stages_total"] == 4 * 3 + 5
    assert final["stages_completed"] == final["stages_total"]
    norm_artifacts = [a for a in final["artifacts"] if a["artifact_type"] == "normalization"]
    assert len(norm_artifacts) == 3


def test_run_aware_output_is_byte_identical_to_legacy_stage_by_stage_pipeline(client: TestClient) -> None:
    """Design Requirement 55: run_id/stage_run_id/progress/heartbeat must
    never leak into artifact content or reproducibility identity."""
    legacy_imu = normalize_stream(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id="sess_v26_legacy")
    legacy_gps = normalize_stream(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id="sess_v26_legacy")
    legacy_downstream = build_downstream_from_normalizations(
        client, imu_normalization_id=legacy_imu["normalization"]["normalization_id"], gps_normalization_id=legacy_gps["normalization"]["normalization_id"]
    )

    run_result = submit_run(client, ["imu", "gps"], session_id="sess_v26_run_aware")
    final = wait_for_run(client, run_result["run_id"])
    assert final["status"] == "completed"

    # Compare via the catalog's own artifact content hash -- meaningful
    # despite the two runs using different session_ids/ingestion_ids/
    # timestamps/run_ids/stage_run_ids, because none of those are baked
    # into the deterministic transformation of the same input bytes
    # under the same config.
    assert client.post("/api/v1/catalog/scan").status_code == 200
    legacy_pkg_id = legacy_downstream["pkg"]["package_id"]
    run_pkg_id = next(a["artifact_id"] for a in final["artifacts"] if a["artifact_type"] == "package")

    legacy_artifact = client.get(f"/api/v1/catalog/artifacts/package/{legacy_pkg_id}").json()
    run_artifact = client.get(f"/api/v1/catalog/artifacts/package/{run_pkg_id}").json()
    assert legacy_artifact["artifact"]["content_sha256"] == run_artifact["artifact"]["content_sha256"]
