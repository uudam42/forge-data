"""HTTP layer for the catalog scan/rebuild/health/artifact/verify APIs.

Responsible only for request/response translation and mapping domain
errors to HTTP status codes. All orchestration lives in
app.catalog.service.

Status codes:
- 200: successful read/scan/rebuild/verify — including a verification
  that finds a checksum mismatch, which is a successfully-executed
  verification reporting a real finding, not a server error (mirrors the
  QC philosophy)
- 400: an unknown artifact_type
- 404: the artifact doesn't exist in the catalog
- 500: an actual scan/rebuild failure (e.g. strict rebuild aborted on
  broken lineage)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.catalog.errors import (
    ArtifactNotFoundError,
    CatalogRebuildFailedError,
    CatalogScanFailedError,
    InvalidArtifactTypeError,
)
from app.catalog.models import ArtifactDetail, ArtifactSummary, CatalogHealth, RebuildResult, ScanResult, VerificationResponse
from app.catalog.repository import CatalogRepository
from app.catalog.scanner import CatalogScanner
from app.catalog.service import CatalogService
from app.catalog.verifier import ArtifactVerifier
from app.core.config import Settings, get_settings
from app.storage.catalog_store import get_connection

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])


def get_catalog_service(settings: Settings = Depends(get_settings)) -> CatalogService:
    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = CatalogRepository(conn)
    scanner = CatalogScanner(settings)
    verifier = ArtifactVerifier(settings)
    return CatalogService(repo=repo, scanner=scanner, verifier=verifier)


@router.post("/scan", response_model=ScanResult, status_code=status.HTTP_200_OK)
async def scan(service: CatalogService = Depends(get_catalog_service)) -> ScanResult:
    try:
        return service.scan()
    except CatalogScanFailedError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@router.post("/rebuild", response_model=RebuildResult, status_code=status.HTTP_200_OK)
async def rebuild(service: CatalogService = Depends(get_catalog_service)) -> RebuildResult:
    try:
        return service.rebuild()
    except CatalogRebuildFailedError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@router.get("/health", response_model=CatalogHealth, status_code=status.HTTP_200_OK)
async def health(service: CatalogService = Depends(get_catalog_service)) -> CatalogHealth:
    return service.health()


@router.get("/artifacts", response_model=list[ArtifactSummary], status_code=status.HTTP_200_OK)
async def list_artifacts(
    stage: str | None = Query(default=None, description="Filter by artifact_type, e.g. 'qc'"),
    status_filter: str | None = Query(default=None, alias="status"),
    session_id: str | None = Query(default=None),
    service: CatalogService = Depends(get_catalog_service),
) -> list[ArtifactSummary]:
    try:
        return service.list_artifacts(artifact_type=stage, status=status_filter, session_id=session_id)
    except InvalidArtifactTypeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/artifacts/{artifact_type}/{artifact_id}", response_model=ArtifactDetail, status_code=status.HTTP_200_OK)
async def get_artifact(
    artifact_type: str, artifact_id: str, service: CatalogService = Depends(get_catalog_service)
) -> ArtifactDetail:
    try:
        return service.get_artifact(artifact_type, artifact_id)
    except InvalidArtifactTypeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/verify/{artifact_type}/{artifact_id}", response_model=VerificationResponse, status_code=status.HTTP_200_OK
)
async def verify_artifact(
    artifact_type: str,
    artifact_id: str,
    recursive: bool = Query(default=False),
    service: CatalogService = Depends(get_catalog_service),
) -> VerificationResponse:
    try:
        return service.verify(artifact_type, artifact_id, recursive=recursive)
    except InvalidArtifactTypeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
