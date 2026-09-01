"""HTTP layer for the dataset QC API.

Responsible only for request/response translation and mapping domain
errors to HTTP status codes. All orchestration lives in app.qc.service.

Status codes:
- 404: the transformation run, the requested QC profile, or an explicitly
  supplied baseline_qc_id doesn't exist
- 409: the transformed artifact's checksum no longer matches its manifest
  (tampered/stale) — never QC a stale or modified artifact
- 415: the transformed artifact's format isn't one Step 8 can read (JSONL
  only for this MVP)
- 400: the request's own configuration is unusable
- 200: QC executed successfully — including status "failed", which is a
  QC finding, not a server error
- 500: reserved for actual internal failures
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.routes.governance_gate import enforce_gate, get_governance_repo
from app.api.routes.transformation import get_transformed_store
from app.catalog.repository import CatalogRepository
from app.core.config import Settings, get_settings
from app.qc.checks.drift import BaselineNotFoundError
from app.qc.models import QCRequest, QCResponse
from app.qc.profiles.base import InvalidQCConfigurationError
from app.qc.registry import QCProfileNotFoundError, QCProfileRegistry
from app.qc.service import (
    QCService,
    TransformationNotFoundError,
    TransformedArtifactChecksumMismatchError,
    UnsupportedQCFileTypeError,
)
from app.storage.qc_store import LocalQCReportStore, QCReportStore
from app.storage.transformed_store import TransformedArtifactStore

router = APIRouter(prefix="/api/v1/qc", tags=["qc"])


def get_qc_profile_registry() -> QCProfileRegistry:
    return QCProfileRegistry()


def get_qc_store(settings: Settings = Depends(get_settings)) -> QCReportStore:
    return LocalQCReportStore(root=settings.QC_STORAGE_ROOT)


def get_qc_service(
    settings: Settings = Depends(get_settings),
    transformed_store: TransformedArtifactStore = Depends(get_transformed_store),
    profile_registry: QCProfileRegistry = Depends(get_qc_profile_registry),
    qc_store: QCReportStore = Depends(get_qc_store),
) -> QCService:
    return QCService(
        transformed_store=transformed_store,
        profile_registry=profile_registry,
        qc_store=qc_store,
        settings=settings,
    )


@router.post("/{transformation_id}", response_model=QCResponse, status_code=status.HTTP_200_OK)
async def run_qc(
    transformation_id: str,
    request: QCRequest,
    allow_deprecated: bool = Query(default=False, description="Allow a deprecated input transformation run/ancestor. Never bypasses an invalid one."),
    service: QCService = Depends(get_qc_service),
    governance_repo: CatalogRepository = Depends(get_governance_repo),
) -> QCResponse:
    enforce_gate(governance_repo, artifact_type="transformation", artifact_id=transformation_id, allow_deprecated=allow_deprecated)
    try:
        return service.run_qc(transformation_id=transformation_id, request=request)
    except (TransformationNotFoundError, QCProfileNotFoundError, BaselineNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except TransformedArtifactChecksumMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except UnsupportedQCFileTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except InvalidQCConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
