"""HTTP layer for selective-rebuild planning and execution (v2.5).

Status codes:
- 200: plan built, or execute completed (execute always returns 200 even
  if individual steps were skipped/failed — see each step's own
  `status`; this endpoint's job is to report what happened, not to
  collapse a partial result into an HTTP error)
- 400: unknown artifact_type
- 404: old/new artifact not found, or no such plan_id (plans are
  process-local and short-lived — see app.catalog.rebuild_plan_store)
- 409: incompatible replacement, plan is stale (catalog changed since
  it was built), or a selective rebuild is already running for this root
- 503: the catalog was too busy to acquire a write lock (CATALOG_BUSY)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.routes.catalog import get_catalog_service, raise_busy
from app.api.routes.runs import get_run_service
from app.catalog.errors import (
    ArtifactNotFoundError,
    CatalogBusyError,
    InvalidArtifactTypeError,
    RebuildPlanNotFoundError,
    RebuildPlanStaleError,
    RebuildReplacementIncompatibleError,
    SelectiveRebuildInProgressError,
)
from app.catalog.rebuild_models import RebuildExecuteRequest, RebuildExecuteResponse, RebuildPlanRequest, RebuildPlanResponse
from app.catalog.service import CatalogService
from app.runs.service import RunService

router = APIRouter(prefix="/api/v1/rebuild", tags=["rebuild"])


@router.post("/plan", response_model=RebuildPlanResponse, status_code=status.HTTP_200_OK)
async def build_plan(request: RebuildPlanRequest, service: CatalogService = Depends(get_catalog_service)) -> RebuildPlanResponse:
    try:
        return service.build_rebuild_plan(
            old_type=request.replace.old_type, old_id=request.replace.old_id,
            new_type=request.replace.new_type, new_id=request.replace.new_id,
        )
    except InvalidArtifactTypeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RebuildReplacementIncompatibleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.to_dict()) from exc
    except CatalogBusyError as exc:
        raise_busy(exc)


@router.post("/execute", response_model=RebuildExecuteResponse, status_code=status.HTTP_200_OK)
async def execute_plan(
    request: RebuildExecuteRequest,
    service: CatalogService = Depends(get_catalog_service),
    run_service: RunService = Depends(get_run_service),
) -> RebuildExecuteResponse:
    try:
        response = service.execute_rebuild(plan_id=request.plan_id, configs=request.configs)
    except RebuildPlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.to_dict()) from exc
    except RebuildPlanStaleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.to_dict()) from exc
    except SelectiveRebuildInProgressError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.to_dict()) from exc
    except CatalogBusyError as exc:
        raise_busy(exc)

    # v2.6 Design Requirement 31: observable run state for a rebuild
    # execution, without touching SelectiveRebuildExecutor at all --
    # recorded post-hoc from the already-complete result above.
    try:
        run_service.record_selective_rebuild_run(plan_id=request.plan_id, configs=request.configs, response=response)
    except Exception:
        import logging

        logging.getLogger("app.runs.service").exception("SELECTIVE_REBUILD_RUN_RECORD_FAILED plan_id=%s", request.plan_id)
    return response
