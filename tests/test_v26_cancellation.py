"""Cooperative cancellation (v2.6, Design Requirements 16-19/40).

FastAPI's TestClient runs BackgroundTasks synchronously as part of the
request/response cycle (not truly in the background), so a pipeline
fast enough to fit in a test fixture's tiny CSVs always finishes before
a *second* HTTP request could ever reach it -- there is no real window
to send a cancel to an in-flight TestClient-driven run. The
deterministic way to test the cancellation MECHANISM itself (not
timing) is to request cancellation on a run BEFORE its executor ever
starts (still fully exercises CancellationToken/the stage-boundary
check/the cascading-cancel path -- the only thing that differs from a
"real" mid-run cancel is which stage-boundary check happens to observe
it), and to verify a completion/cancellation race's outcome is always
one of the two legal final states (Design Requirement 40), never an
inconsistent third possibility. The live demo (see the final report)
exercises this against a real, non-synchronous uvicorn server instead."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.runs.cancellation import CancellationToken
from app.runs.errors import RunCancellationRequested
from app.runs.executor import PipelineRunner, StreamFile
from app.runs.local_executor import LocalRunExecutor
from app.runs.models import PipelineRunRequest
from app.runs.repository import RunRepository
from app.runs.service import RunService
from app.storage.catalog_store import get_connection
from tests.v26_helpers import GPS_CSV, IMU_CSV, submit_run


def test_cancel_requested_before_start_yields_cancelled_run_with_no_artifacts(tmp_path: Path) -> None:
    settings = Settings(
        RAW_STORAGE_ROOT=tmp_path / "raw", VALIDATION_STORAGE_ROOT=tmp_path / "validation",
        INTEGRITY_STORAGE_ROOT=tmp_path / "integrity", NORMALIZED_STORAGE_ROOT=tmp_path / "normalized",
        SYNCHRONIZED_STORAGE_ROOT=tmp_path / "synchronized", CLEANED_STORAGE_ROOT=tmp_path / "cleaned",
        TRANSFORMED_STORAGE_ROOT=tmp_path / "transformed", QC_STORAGE_ROOT=tmp_path / "qc",
        PACKAGE_STORAGE_ROOT=tmp_path / "packages", CATALOG_DB_PATH=tmp_path / "catalog" / "catalog.db",
        SCHEMA_DIR=Path(__file__).resolve().parent.parent / "schemas",
    )
    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = RunRepository(conn)
    run_service = RunService(repo=repo, settings=settings)

    from app.cleaning.models import CleaningConfig
    from app.packaging.models import PackagingConfig
    from app.qc.models import QCConfig
    from app.runs.models import PipelineCleaningConfig, PipelinePackagingConfig, PipelineQCConfig, PipelineStreamConfig, PipelineTransformationConfig
    from app.transformation.models import TransformationConfig

    request = PipelineRunRequest(
        session_id="sess_cancel_pre",
        streams=[PipelineStreamConfig(sensor_type="imu"), PipelineStreamConfig(sensor_type="gps")],
        cleaning=PipelineCleaningConfig(policy_name="default_multimodal", config=CleaningConfig(required_streams=["imu"])),
        transformation=PipelineTransformationConfig(profile_name="multimodal_window_v1", config=TransformationConfig(window={"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True})),
        qc=PipelineQCConfig(profile_name="default_dataset_qc", config=QCConfig(minimum_samples=1)),
        packaging=PipelinePackagingConfig(profile_name="default_ml_package", config=PackagingConfig(split={"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}, grouping={"mode": "source_overlap"})),
    )
    request.synchronization.reference.mode = "stream"
    request.synchronization.reference.stream = "imu"

    run_id = run_service.create_run(run_type="pipeline", request=request)
    cancelled_response = run_service.request_cancel(run_id)
    assert cancelled_response.status == "cancel_requested"

    stream_files = [
        StreamFile(sensor_type="imu", filename="imu.csv", content_type="text/csv", stream=_bytes_stream(IMU_CSV), source_units={"acceleration": "m/s^2", "angular_velocity": "rad/s"}),
        StreamFile(sensor_type="gps", filename="gps.csv", content_type="text/csv", stream=_bytes_stream(GPS_CSV), source_units={"altitude": "m", "speed": "m/s"}),
    ]
    LocalRunExecutor(settings=settings).run(run_id, request, stream_files)

    final = run_service.get_run(run_id)
    assert final.status == "cancelled"
    assert all(s.status == "cancelled" for s in final.stage_runs)
    assert final.artifacts == []  # nothing was ever produced


def _bytes_stream(text: str):
    import io

    return io.BytesIO(text.encode())


def test_completion_cancellation_race_always_lands_in_a_legal_final_state(client: TestClient) -> None:
    """Fires cancel immediately after submitting a run that, under
    TestClient, will already have finished by the time submit_run()
    returns -- so this always lands on the "completed" side of the race,
    which is itself the point: a request to cancel that arrives after
    the work already committed must never retroactively become
    "cancelled" or corrupt anything (Design Requirement 40)."""
    result = submit_run(client, ["imu", "gps"], session_id="sess_cancel_race")
    run_id = result["run_id"]

    cancel_resp = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert cancel_resp.status_code == 200

    final = client.get(f"/api/v1/runs/{run_id}").json()
    assert final["status"] in ("completed", "cancelled")  # the only two legal outcomes
    if final["status"] == "completed":
        assert any(a["artifact_type"] == "package" for a in final["artifacts"])
    else:
        assert not any(a["artifact_type"] == "package" for a in final["artifacts"])

    # Idempotent: cancelling an already-finished run never errors or changes it again.
    second_cancel = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert second_cancel.status_code == 200
    assert second_cancel.json()["status"] == final["status"]


def test_cancellation_token_raises_once_status_is_cancel_requested(tmp_path: Path) -> None:
    """Unit-level proof of the polling mechanism itself, independent of
    any pipeline: force=True always re-reads; the throttled path (no
    force) only re-reads after poll_interval_s -- both are exercised
    here without a real sleep by calling force=True explicitly."""
    settings = Settings(
        RAW_STORAGE_ROOT=tmp_path / "raw", VALIDATION_STORAGE_ROOT=tmp_path / "validation",
        INTEGRITY_STORAGE_ROOT=tmp_path / "integrity", NORMALIZED_STORAGE_ROOT=tmp_path / "normalized",
        SYNCHRONIZED_STORAGE_ROOT=tmp_path / "synchronized", CLEANED_STORAGE_ROOT=tmp_path / "cleaned",
        TRANSFORMED_STORAGE_ROOT=tmp_path / "transformed", QC_STORAGE_ROOT=tmp_path / "qc",
        PACKAGE_STORAGE_ROOT=tmp_path / "packages", CATALOG_DB_PATH=tmp_path / "catalog" / "catalog.db",
    )
    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = RunRepository(conn)

    from tests.test_v26_run_state_machine import _minimal_request

    run_service = RunService(repo=repo, settings=settings)
    run_id = run_service.create_run(run_type="pipeline", request=_minimal_request())
    run_service.mark_running(run_id, executor_id="x")

    token = CancellationToken(repo, run_id, poll_interval_s=999)  # never throttle-expires on its own
    token.check(force=True)  # active -- must not raise

    run_service.request_cancel(run_id)
    try:
        token.check(force=True)  # forced re-read observes the new status -- raises
        assert False, "expected RunCancellationRequested"
    except RunCancellationRequested:
        pass

    try:
        token.check()  # already latched cancelled -- raises regardless of throttle, no force needed
        assert False, "expected RunCancellationRequested"
    except RunCancellationRequested:
        pass


class _RaiseOnNthCheck:
    """A fake CancellationToken that raises RunCancellationRequested on
    its Nth call to check() -- used to deterministically simulate
    "cancellation observed right as stage K is about to start" without
    real timing/threads."""

    def __init__(self, run_id: str, raise_on_call: int) -> None:
        self._run_id = run_id
        self._raise_on_call = raise_on_call
        self._calls = 0

    def check(self, *, force: bool = False) -> None:
        self._calls += 1
        if self._calls >= self._raise_on_call:
            raise RunCancellationRequested(self._run_id)


def test_the_stage_where_cancellation_is_first_observed_becomes_cancelled_not_stuck_pending(tmp_path: Path) -> None:
    """Regression test for a real bug caught during development: the
    stage boundary check that OBSERVES cancel_requested must mark that
    same stage `cancelled`, not leave it `pending` forever while only
    later stages get cancelled."""
    settings = Settings(
        RAW_STORAGE_ROOT=tmp_path / "raw", VALIDATION_STORAGE_ROOT=tmp_path / "validation",
        INTEGRITY_STORAGE_ROOT=tmp_path / "integrity", NORMALIZED_STORAGE_ROOT=tmp_path / "normalized",
        SYNCHRONIZED_STORAGE_ROOT=tmp_path / "synchronized", CLEANED_STORAGE_ROOT=tmp_path / "cleaned",
        TRANSFORMED_STORAGE_ROOT=tmp_path / "transformed", QC_STORAGE_ROOT=tmp_path / "qc",
        PACKAGE_STORAGE_ROOT=tmp_path / "packages", CATALOG_DB_PATH=tmp_path / "catalog" / "catalog.db",
        SCHEMA_DIR=Path(__file__).resolve().parent.parent / "schemas",
    )
    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = RunRepository(conn)
    from app.catalog.repository import CatalogRepository

    catalog_repo = CatalogRepository(conn)
    run_service = RunService(repo=repo, settings=settings)

    from app.cleaning.models import CleaningConfig
    from app.packaging.models import PackagingConfig
    from app.qc.models import QCConfig
    from app.runs.models import PipelineCleaningConfig, PipelinePackagingConfig, PipelineQCConfig, PipelineStreamConfig, PipelineTransformationConfig
    from app.transformation.models import TransformationConfig

    request = PipelineRunRequest(
        session_id="sess_cancel_boundary",
        streams=[PipelineStreamConfig(sensor_type="imu"), PipelineStreamConfig(sensor_type="gps")],
        cleaning=PipelineCleaningConfig(policy_name="default_multimodal", config=CleaningConfig(required_streams=["imu"])),
        transformation=PipelineTransformationConfig(profile_name="multimodal_window_v1", config=TransformationConfig(window={"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True})),
        qc=PipelineQCConfig(profile_name="default_dataset_qc", config=QCConfig(minimum_samples=1)),
        packaging=PipelinePackagingConfig(profile_name="default_ml_package", config=PackagingConfig(split={"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}, grouping={"mode": "source_overlap"})),
    )
    request.synchronization.reference.mode = "stream"
    request.synchronization.reference.stream = "imu"

    run_id = run_service.create_run(run_type="pipeline", request=request)
    run_service.mark_running(run_id, executor_id="x")

    stream_files = [
        StreamFile(sensor_type="imu", filename="imu.csv", content_type="text/csv", stream=_bytes_stream(IMU_CSV), source_units={"acceleration": "m/s^2", "angular_velocity": "rad/s"}),
        StreamFile(sensor_type="gps", filename="gps.csv", content_type="text/csv", stream=_bytes_stream(GPS_CSV), source_units={"altitude": "m", "speed": "m/s"}),
    ]
    runner = PipelineRunner(settings=settings, repo=repo, catalog_repo=catalog_repo)
    # Raise on the 5th boundary check -- i.e. right as the 5th planned
    # stage (normalization:gps) is about to start, after ingestion/
    # validation/integrity/normalization:imu have already genuinely run.
    fake_token = _RaiseOnNthCheck(run_id, raise_on_call=5)
    try:
        runner.execute(run_id=run_id, request=request, stream_files=stream_files, cancellation=fake_token)
        assert False, "expected RunCancellationRequested"
    except RunCancellationRequested:
        pass

    stage_runs = {s["stage"]: s["status"] for s in repo.list_stage_runs(run_id)}
    assert stage_runs["ingestion:imu"] == "completed"
    assert stage_runs["validation:imu"] == "completed"
    assert stage_runs["integrity:imu"] == "completed"
    assert stage_runs["normalization:imu"] == "completed"
    # The 5th stage -- where cancellation was observed -- must be
    # `cancelled`, never left `pending`.
    assert stage_runs["ingestion:gps"] == "cancelled"
    for stage in ("validation:gps", "integrity:gps", "normalization:gps", "synchronization", "cleaning", "transformation", "qc", "package"):
        assert stage_runs[stage] == "cancelled", (stage, stage_runs[stage])
    assert "pending" not in stage_runs.values()


def test_real_cancellation_token_actually_stops_a_run_requested_mid_flight(tmp_path: Path) -> None:
    """Regression test for a more serious bug caught live (against a
    real uvicorn server, not just unit tests): an earlier version of
    _run_stage unconditionally set the run's status back to "running" at
    the top of EVERY stage boundary, BEFORE checking cancellation --
    which silently overwrote a genuine `cancel_requested` status back to
    `running` every time, so a real cancellation request could never
    actually take effect. This test uses the REAL CancellationToken
    (reading real DB state) and triggers a real request_cancel() call as
    a side effect partway through the run, then asserts execution
    actually stops instead of running to completion."""
    settings = Settings(
        RAW_STORAGE_ROOT=tmp_path / "raw", VALIDATION_STORAGE_ROOT=tmp_path / "validation",
        INTEGRITY_STORAGE_ROOT=tmp_path / "integrity", NORMALIZED_STORAGE_ROOT=tmp_path / "normalized",
        SYNCHRONIZED_STORAGE_ROOT=tmp_path / "synchronized", CLEANED_STORAGE_ROOT=tmp_path / "cleaned",
        TRANSFORMED_STORAGE_ROOT=tmp_path / "transformed", QC_STORAGE_ROOT=tmp_path / "qc",
        PACKAGE_STORAGE_ROOT=tmp_path / "packages", CATALOG_DB_PATH=tmp_path / "catalog" / "catalog.db",
        SCHEMA_DIR=Path(__file__).resolve().parent.parent / "schemas",
    )
    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = RunRepository(conn)
    from app.catalog.repository import CatalogRepository

    catalog_repo = CatalogRepository(conn)
    run_service = RunService(repo=repo, settings=settings)

    from app.cleaning.models import CleaningConfig
    from app.packaging.models import PackagingConfig
    from app.qc.models import QCConfig
    from app.runs.models import PipelineCleaningConfig, PipelinePackagingConfig, PipelineQCConfig, PipelineStreamConfig, PipelineTransformationConfig
    from app.transformation.models import TransformationConfig

    request = PipelineRunRequest(
        session_id="sess_real_cancel_midflight",
        streams=[PipelineStreamConfig(sensor_type="imu"), PipelineStreamConfig(sensor_type="gps")],
        cleaning=PipelineCleaningConfig(policy_name="default_multimodal", config=CleaningConfig(required_streams=["imu"])),
        transformation=PipelineTransformationConfig(profile_name="multimodal_window_v1", config=TransformationConfig(window={"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True})),
        qc=PipelineQCConfig(profile_name="default_dataset_qc", config=QCConfig(minimum_samples=1)),
        packaging=PipelinePackagingConfig(profile_name="default_ml_package", config=PackagingConfig(split={"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}, grouping={"mode": "source_overlap"})),
    )
    request.synchronization.reference.mode = "stream"
    request.synchronization.reference.stream = "imu"

    run_id = run_service.create_run(run_type="pipeline", request=request)
    run_service.mark_running(run_id, executor_id="x")

    stream_files = [
        StreamFile(sensor_type="imu", filename="imu.csv", content_type="text/csv", stream=_bytes_stream(IMU_CSV), source_units={"acceleration": "m/s^2", "angular_velocity": "rad/s"}),
        StreamFile(sensor_type="gps", filename="gps.csv", content_type="text/csv", stream=_bytes_stream(GPS_CSV), source_units={"altitude": "m", "speed": "m/s"}),
    ]
    runner = PipelineRunner(settings=settings, repo=repo, catalog_repo=catalog_repo)

    # Trigger a REAL cancel request as a side effect right after the
    # first stage (ingestion:imu) genuinely completes -- simulating an
    # operator calling POST /runs/{id}/cancel while stage 2 onward is
    # still to come.
    real_do_ingest = runner._do_ingest  # noqa: SLF001

    def _ingest_then_cancel(*args, **kwargs):
        result = real_do_ingest(*args, **kwargs)
        run_service.request_cancel(run_id)
        return result

    runner._do_ingest = _ingest_then_cancel  # noqa: SLF001

    real_token = CancellationToken(repo, run_id, poll_interval_s=0.0)  # no throttle -- check every boundary for real
    try:
        runner.execute(run_id=run_id, request=request, stream_files=stream_files, cancellation=real_token)
        assert False, "expected RunCancellationRequested -- the run must not complete"
    except RunCancellationRequested:
        pass

    final = run_service.get_run(run_id)
    stage_runs = {s.stage: s.status for s in final.stage_runs}
    assert stage_runs["ingestion:imu"] == "completed"  # already done before cancel was requested
    assert stage_runs["validation:imu"] == "cancelled"  # the very next stage boundary must observe it
    assert not any(a.artifact_type == "package" for a in final.artifacts)
