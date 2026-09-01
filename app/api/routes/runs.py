"""HTTP layer for pipeline runs (v2.6).

`POST /runs` accepts the pipeline config as a JSON string in the `config`
form field, plus one file per stream (matched by list order) as
multipart uploads -- file bytes are written to a temporary file
synchronously in this request handler (so they survive Starlette's
UploadFile lifecycle regardless of exactly when the background task
runs) and never touch run metadata itself (Design Requirement 46).

Status codes:
- 202: a new run was accepted and will execute in the background
- 200: successful read/cancel
- 400: invalid pipeline config (e.g. an unknown sensor_type), or the
  file count doesn't match the stream count
- 404: no run with that id
- 429: local run capacity exceeded (LOCAL_RUN_CAPACITY_EXCEEDED) --
  there is no queue; retry once a slot frees up
"""

from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.runs.errors import InvalidPipelineConfigError, RunCapacityExceededError, RunNotFoundError
from app.runs.executor import StreamFile
from app.runs.local_executor import LocalRunExecutor
from app.runs.models import PipelineRunRequest, PipelineRunResponse, RunEventResponse, RunListResponse
from app.runs.repository import RunRepository
from app.runs.service import RunService
from app.storage.catalog_store import get_connection

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


def get_run_service(settings: Settings = Depends(get_settings)) -> RunService:
    conn = get_connection(settings.CATALOG_DB_PATH, busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS, journal_mode=settings.CATALOG_JOURNAL_MODE)
    repo = RunRepository(conn, db_path=str(settings.CATALOG_DB_PATH), busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS)
    return RunService(repo=repo, settings=settings)


@router.post("", response_model=PipelineRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    background_tasks: BackgroundTasks,
    config: str = Form(..., description="JSON-encoded PipelineRunRequest"),
    files: list[UploadFile] = File(..., description="One raw data file per stream, in the same order as config.streams"),
    settings: Settings = Depends(get_settings),
    run_service: RunService = Depends(get_run_service),
) -> PipelineRunResponse:
    try:
        request = PipelineRunRequest.model_validate_json(config)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if len(files) != len(request.streams):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Expected exactly one file per stream ({len(request.streams)} stream(s)), got {len(files)} file(s)",
        )

    try:
        run_id = run_service.create_run(run_type="pipeline", request=request)
    except InvalidPipelineConfigError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RunCapacityExceededError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=exc.to_dict()) from exc

    temp_paths: list[str] = []
    stream_files: list[StreamFile] = []
    for stream_cfg, upload in zip(request.streams, files):
        content = await upload.read()
        fd, path = tempfile.mkstemp(prefix="forge_run_upload_")
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(content)
        temp_paths.append(path)
        stream_files.append(
            StreamFile(
                sensor_type=stream_cfg.sensor_type, filename=upload.filename or f"{stream_cfg.sensor_type}.dat",
                content_type=upload.content_type, stream=open(path, "rb"), source_units=stream_cfg.source_units,
            )
        )

    def _execute() -> None:
        try:
            LocalRunExecutor(settings=settings).run(run_id, request, stream_files)
        finally:
            for p in temp_paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    background_tasks.add_task(_execute)
    return run_service.get_run(run_id)


@router.get("", response_model=RunListResponse, status_code=status.HTTP_200_OK)
async def list_runs(
    status_filter: str | None = Query(default=None, alias="status"),
    run_type: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    run_service: RunService = Depends(get_run_service),
) -> RunListResponse:
    runs = run_service.list_runs(status=status_filter, run_type=run_type, limit=limit, offset=offset)
    return RunListResponse(runs=runs, limit=limit, offset=offset)


@router.get("/{run_id}", response_model=PipelineRunResponse, status_code=status.HTTP_200_OK)
async def get_run(run_id: str, run_service: RunService = Depends(get_run_service)) -> PipelineRunResponse:
    try:
        return run_service.get_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{run_id}/events", response_model=list[RunEventResponse], status_code=status.HTTP_200_OK)
async def get_run_events(run_id: str, run_service: RunService = Depends(get_run_service)) -> list[RunEventResponse]:
    try:
        return run_service.get_events(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{run_id}/cancel", response_model=PipelineRunResponse, status_code=status.HTTP_200_OK)
async def cancel_run(run_id: str, run_service: RunService = Depends(get_run_service)) -> PipelineRunResponse:
    """Sets cancel_requested if the run is queued/running; idempotent
    (just returns current state) if it has already finished in any way.
    Never blocks waiting for the cancellation to actually take effect --
    see GET /runs/{run_id} to observe the eventual outcome."""
    try:
        return run_service.request_cancel(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
