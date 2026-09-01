"""RunService: create/get/list/cancel orchestration for pipeline runs
(v2.6). Thin API-facing layer over RunRepository -- the actual
stage-by-stage execution lives in app.runs.executor.PipelineRunner;
LocalRunExecutor (app.runs.local_executor) is what actually invokes it
in the background and drives this service's state transitions.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import uuid
from datetime import datetime, timezone

from app.catalog.serialization import canonical_json
from app.runs.errors import InvalidPipelineConfigError, RunCapacityExceededError, RunNotFoundError
from app.runs.models import (
    PipelineRunRequest,
    PipelineRunResponse,
    PipelineRunSummary,
    RunArtifactResponse,
    RunEventResponse,
    StageRunResponse,
)
from app.runs.state_machine import RunStatus, validate_run_transition
from app.sensors.registry import SensorPluginRegistry, get_default_registry


def compute_config_hash(request: PipelineRunRequest) -> str:
    """Deterministic canonical-JSON hash of the effective request --
    same logical config always hashes the same; never salted with
    run_id/timestamp (Design Requirement 45)."""
    payload = json.loads(request.model_dump_json())
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def new_executor_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunService:
    def __init__(self, *, repo, settings, sensor_registry: SensorPluginRegistry | None = None) -> None:
        self._repo = repo
        self._settings = settings
        self._sensor_registry = sensor_registry or get_default_registry()

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def validate_request(self, request: PipelineRunRequest) -> None:
        if not request.streams:
            raise InvalidPipelineConfigError("A pipeline run requires at least one stream")
        for stream in request.streams:
            if not self._sensor_registry.is_registered(stream.sensor_type):
                raise InvalidPipelineConfigError(
                    f"Unknown sensor_type {stream.sensor_type!r}. Available: "
                    f"{sorted(p.sensor_type for p in self._sensor_registry.list_plugins())}"
                )

    def create_run(self, *, run_type: str, request: PipelineRunRequest) -> str:
        """Validates the config and enforces local run capacity, then
        creates the run row, all inside one transaction -- the capacity
        check-then-insert is race-safe under v2.4's BEGIN IMMEDIATE the
        same way every other read-decide-write in this codebase is."""
        self.validate_request(request)
        config_hash = compute_config_hash(request)
        request_json = canonical_json(json.loads(request.model_dump_json()))
        run_id = f"run_{uuid.uuid4().hex}"

        with self._repo.transaction(operation="create_run"):
            active = self._repo.count_active_runs()
            if active >= self._settings.MAX_LOCAL_PIPELINE_RUNS:
                raise RunCapacityExceededError(limit=self._settings.MAX_LOCAL_PIPELINE_RUNS, current=active)
            self._repo.create_run(
                run_id=run_id, run_type=run_type, status=RunStatus.QUEUED.value, created_at=_now(),
                request_json=request_json, config_hash=config_hash,
            )
            self._repo.record_event(run_id=run_id, event_type="RUN_CREATED", detail=None, created_at=_now())
        return run_id

    # ------------------------------------------------------------------
    # State transitions -- each is its own short transaction (Design
    # Requirement 32: updates to one run must never block unrelated work
    # for long).
    # ------------------------------------------------------------------

    def mark_running(self, run_id: str, *, executor_id: str) -> None:
        with self._repo.transaction(operation="run_mark_running"):
            row = self._require_run(run_id)
            validate_run_transition(row["status"], RunStatus.RUNNING.value)
            self._repo.update_run_status(run_id, status=RunStatus.RUNNING.value, started_at=_now(), executor_id=executor_id, last_heartbeat_at=_now())
            self._repo.record_event(run_id=run_id, event_type="RUN_STARTED", detail=None, created_at=_now())

    def mark_completed(self, run_id: str) -> None:
        with self._repo.transaction(operation="run_mark_completed"):
            row = self._require_run(run_id)
            validate_run_transition(row["status"], RunStatus.COMPLETED.value)
            self._repo.update_run_status(run_id, status=RunStatus.COMPLETED.value, finished_at=_now())
            self._repo.record_event(run_id=run_id, event_type="RUN_COMPLETED", detail=None, created_at=_now())

    def mark_failed(self, run_id: str, *, error_code: str, error_message: str) -> None:
        with self._repo.transaction(operation="run_mark_failed"):
            row = self._require_run(run_id)
            validate_run_transition(row["status"], RunStatus.FAILED.value)
            self._repo.update_run_status(run_id, status=RunStatus.FAILED.value, finished_at=_now(), error_code=error_code, error_message=error_message)
            self._repo.record_event(run_id=run_id, event_type="RUN_FAILED", detail=error_message, created_at=_now())

    def mark_cancelled(self, run_id: str) -> None:
        with self._repo.transaction(operation="run_mark_cancelled"):
            row = self._require_run(run_id)
            validate_run_transition(row["status"], RunStatus.CANCELLED.value)
            self._repo.update_run_status(run_id, status=RunStatus.CANCELLED.value, finished_at=_now())
            self._repo.record_event(run_id=run_id, event_type="RUN_CANCELLED", detail=None, created_at=_now())

    def touch_heartbeat(self, run_id: str) -> None:
        with self._repo.transaction(operation="run_heartbeat"):
            self._repo.touch_heartbeat(run_id, last_heartbeat_at=_now())

    def request_cancel(self, run_id: str) -> PipelineRunResponse:
        """Design Requirement 19: sets cancel_requested if queued/running;
        idempotent (just returns current state, no error) if already
        finished in any way. Never blocks waiting for the cancellation
        to actually take effect."""
        with self._repo.transaction(operation="run_request_cancel"):
            row = self._require_run(run_id)
            if row["status"] in (RunStatus.QUEUED.value, RunStatus.RUNNING.value):
                validate_run_transition(row["status"], RunStatus.CANCEL_REQUESTED.value)
                self._repo.update_run_status(run_id, status=RunStatus.CANCEL_REQUESTED.value)
                self._repo.record_event(run_id=run_id, event_type="CANCEL_REQUESTED", detail=None, created_at=_now())
        return self.get_run(run_id)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _require_run(self, run_id: str) -> dict:
        row = self._repo.get_run(run_id)
        if row is None:
            raise RunNotFoundError(f"No run with id {run_id!r}")
        return row

    def get_run(self, run_id: str) -> PipelineRunResponse:
        row = self._require_run(run_id)
        stage_rows = self._repo.list_stage_runs(run_id)
        artifact_rows = self._repo.list_run_artifacts(run_id)
        stage_runs = [_to_stage_response(r) for r in stage_rows]
        return PipelineRunResponse(
            run_id=row["run_id"], run_type=row["run_type"], status=row["status"], created_at=row["created_at"],
            started_at=row.get("started_at"), finished_at=row.get("finished_at"), current_stage=row.get("current_stage"),
            config_hash=row["config_hash"], retry_of_run_id=row.get("retry_of_run_id"),
            error_code=row.get("error_code"), error_message=row.get("error_message"),
            stages_total=len(stage_runs), stages_completed=sum(1 for s in stage_runs if s.status == "completed"),
            stage_runs=stage_runs,
            artifacts=[RunArtifactResponse(stage=a["stage"], artifact_type=a["artifact_type"], artifact_id=a["artifact_id"], created_at=a["created_at"]) for a in artifact_rows],
        )

    def get_events(self, run_id: str) -> list[RunEventResponse]:
        self._require_run(run_id)
        return [RunEventResponse(event_id=e["event_id"], event_type=e["event_type"], detail=e.get("detail"), created_at=e["created_at"]) for e in self._repo.list_events(run_id)]

    def record_selective_rebuild_run(self, *, plan_id: str, configs: dict, response) -> str:
        """Post-hoc run observability for a v2.5 selective rebuild
        (Design Requirement 31): wraps the ALREADY-COMPLETE result of
        CatalogService.execute_rebuild() into a run_type=
        'selective_rebuild' PipelineRun plus one StageRun per rebuild
        step -- without touching SelectiveRebuildExecutor at all. This
        is necessarily synchronous/after-the-fact (the rebuild has
        already finished by the time this is called), unlike
        run_type='pipeline' runs, which execute in the background."""
        request_json = canonical_json({"plan_id": plan_id, "configs": configs})
        config_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        run_id = f"run_{uuid.uuid4().hex}"
        now = _now()

        status_map = {
            "rebuilt": "completed", "skipped_manual_configuration_required": "skipped",
            "skipped_upstream_not_rebuilt": "skipped", "failed": "failed",
        }
        any_failed = any(r.status == "failed" for r in response.results)

        with self._repo.transaction(operation="create_selective_rebuild_run"):
            self._repo.create_run(run_id=run_id, run_type="selective_rebuild", status=RunStatus.QUEUED.value, created_at=now, request_json=request_json, config_hash=config_hash)
            self._repo.record_event(run_id=run_id, event_type="RUN_CREATED", detail=None, created_at=now)
            self._repo.update_run_status(run_id, status=RunStatus.RUNNING.value, started_at=now, executor_id=new_executor_id(), last_heartbeat_at=now)
            self._repo.record_event(run_id=run_id, event_type="RUN_STARTED", detail=None, created_at=now)

            for result in response.results:
                stage_run_id = f"stagerun_{uuid.uuid4().hex}"
                stage_status = status_map[result.status]
                self._repo.create_stage_run(stage_run_id=stage_run_id, run_id=run_id, stage=result.stage_artifact_type, status="pending")
                if stage_status == "completed":
                    self._repo.update_stage_run(stage_run_id, status="completed", started_at=now, finished_at=now, artifacts_created=1)
                    self._repo.record_run_artifact(run_id=run_id, stage=result.stage_artifact_type, artifact_type=result.stage_artifact_type, artifact_id=result.new_artifact_id, created_at=now)
                elif stage_status == "skipped":
                    self._repo.update_stage_run(stage_run_id, status="skipped")
                else:
                    self._repo.update_stage_run(stage_run_id, status="failed", started_at=now, finished_at=now, error_code="REBUILD_STEP_FAILED", error_message=result.detail or "rebuild step failed")

            final_status = RunStatus.FAILED.value if any_failed else RunStatus.COMPLETED.value
            self._repo.update_run_status(run_id, status=final_status, finished_at=now)
            self._repo.record_event(run_id=run_id, event_type=("RUN_FAILED" if any_failed else "RUN_COMPLETED"), detail=None, created_at=now)

        return run_id

    def list_runs(self, *, status: str | None = None, run_type: str | None = None, limit: int = 20, offset: int = 0) -> list[PipelineRunSummary]:
        rows = self._repo.list_runs(status=status, run_type=run_type, limit=limit, offset=offset)
        summaries = []
        for row in rows:
            stage_rows = self._repo.list_stage_runs(row["run_id"])
            summaries.append(
                PipelineRunSummary(
                    run_id=row["run_id"], run_type=row["run_type"], status=row["status"], created_at=row["created_at"],
                    started_at=row.get("started_at"), finished_at=row.get("finished_at"), current_stage=row.get("current_stage"),
                    stages_total=len(stage_rows), stages_completed=sum(1 for s in stage_rows if s["status"] == "completed"),
                    error_code=row.get("error_code"),
                )
            )
        return summaries


def _to_stage_response(row: dict) -> StageRunResponse:
    fraction = None
    if row.get("records_total") is not None and row.get("records_processed") is not None and row["records_total"] > 0:
        fraction = min(1.0, row["records_processed"] / row["records_total"])
    return StageRunResponse(
        stage_run_id=row["stage_run_id"], run_id=row["run_id"], stage=row["stage"], status=row["status"],
        started_at=row.get("started_at"), finished_at=row.get("finished_at"),
        records_total=row.get("records_total"), records_processed=row.get("records_processed"),
        bytes_total=row.get("bytes_total"), bytes_processed=row.get("bytes_processed"),
        progress_fraction=fraction, artifacts_created=row.get("artifacts_created") or 0,
        error_code=row.get("error_code"), error_message=row.get("error_message"),
    )
