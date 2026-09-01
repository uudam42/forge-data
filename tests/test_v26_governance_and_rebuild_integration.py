"""v2.6 integration with v2.5: the run executor's stage-boundary
governance gate (Design Requirement 30), and observable run state for a
selective rebuild execution (Design Requirement 31)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.catalog.errors import ArtifactInvalidError
from app.catalog.repository import CatalogRepository
from app.core.config import Settings
from app.runs.executor import PipelineRunner
from app.runs.models import PipelineRunRequest
from app.runs.repository import RunRepository
from app.storage.catalog_store import get_connection
from tests.v25_helpers import CLEANING_CONFIG, GPS_CSV, IMU_CSV, PACKAGING_CONFIG, QC_CONFIG, TRANSFORMATION_CONFIG, build_downstream_from_normalizations, normalize_stream
from tests.v26_helpers import submit_run, wait_for_run


def test_run_executor_gate_blocks_synchronization_through_an_invalid_normalization(client: TestClient, test_settings: Settings) -> None:
    """A real completed run's normalization artifact is marked invalid
    after the fact; calling the executor's own synchronization step
    directly against it (the exact code path a run would take were it
    to reference this normalization) must raise ArtifactInvalidError --
    proving the v2.5 gate really is wired into v2.6's executor, not just
    into the HTTP routes it otherwise duplicates for direct-service
    orchestration."""
    imu = normalize_stream(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id="sess_v26_gov")
    gps = normalize_stream(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id="sess_v26_gov")
    imu_id = imu["normalization"]["normalization_id"]
    gps_id = gps["normalization"]["normalization_id"]
    assert client.post("/api/v1/catalog/scan").status_code == 200
    assert client.post(f"/api/v1/catalog/artifacts/normalization/{imu_id}/invalidate", json={"reason": "bad calibration"}).status_code == 200

    settings = test_settings
    conn = get_connection(settings.CATALOG_DB_PATH, busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS, journal_mode=settings.CATALOG_JOURNAL_MODE)
    run_repo = RunRepository(conn)
    catalog_repo = CatalogRepository(conn)
    runner = PipelineRunner(settings=settings, repo=run_repo, catalog_repo=catalog_repo)

    request = PipelineRunRequest(streams=[], cleaning={"policy_name": "x", "config": {}}, transformation={"profile_name": "x", "config": {}}, qc={"profile_name": "x", "config": {}}, packaging={"profile_name": "x", "config": {"split": {"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}, "grouping": {"mode": "source_overlap"}}})
    try:
        runner._do_synchronize(request, {"imu": imu_id, "gps": gps_id}, allow_deprecated=False)  # noqa: SLF001 -- exercising the exact executor code path directly
        assert False, "expected ArtifactInvalidError"
    except ArtifactInvalidError as exc:
        assert exc.artifact_id == imu_id


def test_selective_rebuild_execution_creates_an_observable_run_record(client: TestClient) -> None:
    imu = normalize_stream(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id="sess_v26_rebuild_run")
    gps = normalize_stream(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id="sess_v26_rebuild_run")
    imu_id = imu["normalization"]["normalization_id"]
    gps_id = gps["normalization"]["normalization_id"]
    downstream = build_downstream_from_normalizations(client, imu_normalization_id=imu_id, gps_normalization_id=gps_id)
    assert client.post("/api/v1/catalog/scan").status_code == 200
    assert client.post(f"/api/v1/catalog/artifacts/normalization/{imu_id}/invalidate", json={"reason": "v2.6 rebuild-run integration test"}).status_code == 200

    imu_ingestion_id = imu["ingestion"]["ingestion_id"]
    new_norm = client.post(
        f"/api/v1/normalization/{imu_ingestion_id}",
        json={"schema_name": "imu", "schema_version": "1.0.0", "profile_name": "imu_canonical", "profile_version": "1.0.0", "source_units": {"acceleration": "m/s^2", "angular_velocity": "rad/s"}},
    ).json()
    assert client.post("/api/v1/catalog/scan").status_code == 200

    plan = client.post("/api/v1/rebuild/plan", json={"replace": {"old_type": "normalization", "old_id": imu_id, "new_type": "normalization", "new_id": new_norm["normalization_id"]}}).json()
    execute_resp = client.post(
        "/api/v1/rebuild/execute",
        json={
            "plan_id": plan["plan_id"],
            "configs": {
                f"cleaning/{downstream['cleaned']['cleaning_id']}": CLEANING_CONFIG,
                f"transformation/{downstream['xform']['transformation_id']}": TRANSFORMATION_CONFIG,
                f"qc/{downstream['qc']['qc_id']}": QC_CONFIG,
                f"package/{downstream['pkg']['package_id']}": PACKAGING_CONFIG,
            },
        },
    )
    assert execute_resp.status_code == 200, execute_resp.text

    runs = client.get("/api/v1/runs", params={"run_type": "selective_rebuild"}).json()["runs"]
    assert len(runs) == 1
    summary = runs[0]
    assert summary["status"] == "completed"
    assert summary["stages_total"] == 5
    assert summary["stages_completed"] == 5

    detail = client.get(f"/api/v1/runs/{summary['run_id']}").json()
    stages = {s["stage"] for s in detail["stage_runs"]}
    assert stages == {"synchronization", "cleaning", "transformation", "qc", "package"}
    assert all(s["status"] == "completed" for s in detail["stage_runs"])
    assert len(detail["artifacts"]) == 5
