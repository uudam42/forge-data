"""HTTP layer for lineage traversal and downstream impact analysis.

Status codes: 200 (successful traversal), 400 (unknown artifact_type),
404 (artifact not found).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.routes.catalog import get_catalog_service
from app.catalog.errors import ArtifactNotFoundError, CatalogScanFailedError, InvalidArtifactTypeError
from app.catalog.governance_models import EnrichedImpactResponse
from app.catalog.models import ImpactResponse, LineageGraphResponse
from app.catalog.service import CatalogService

router = APIRouter(prefix="/api/v1/lineage", tags=["lineage"])


@router.get("/{artifact_type}/{artifact_id}", response_model=LineageGraphResponse, status_code=status.HTTP_200_OK)
async def get_lineage(
    artifact_type: str,
    artifact_id: str,
    direction: str = Query(default="both", pattern="^(upstream|downstream|both)$"),
    max_depth: int | None = Query(default=None, ge=1),
    service: CatalogService = Depends(get_catalog_service),
) -> LineageGraphResponse:
    try:
        return service.lineage(artifact_type, artifact_id, direction=direction, max_depth=max_depth)
    except InvalidArtifactTypeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ArtifactNotFoundError:
        # v2.7: see app.api.routes.catalog.verify_artifact -- same
        # scan-once-and-retry pattern for a not-yet-indexed artifact.
        try:
            service.scan()
        except CatalogScanFailedError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
        try:
            return service.lineage(artifact_type, artifact_id, direction=direction, max_depth=max_depth)
        except ArtifactNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{artifact_type}/{artifact_id}/impact", response_model=ImpactResponse, status_code=status.HTTP_200_OK)
async def get_impact(
    artifact_type: str, artifact_id: str, service: CatalogService = Depends(get_catalog_service)
) -> ImpactResponse:
    try:
        return service.impact(artifact_type, artifact_id)
    except InvalidArtifactTypeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/{artifact_type}/{artifact_id}/impact/enriched",
    response_model=EnrichedImpactResponse,
    status_code=status.HTTP_200_OK,
)
async def get_enriched_impact(
    artifact_type: str, artifact_id: str, service: CatalogService = Depends(get_catalog_service)
) -> EnrichedImpactResponse:
    """Design Requirement 9: everything /impact already returns, plus the
    source artifact's own governance state, every affected package, and
    each affected dataset version's computed effective status. Kept as a
    separate endpoint from /impact (rather than changing that response
    shape) so existing callers of /impact are never broken."""
    try:
        return service.enriched_impact(artifact_type, artifact_id)
    except InvalidArtifactTypeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
