"""HTTP layer for the transformation API.

Responsible only for request/response translation and mapping domain
errors to HTTP status codes. All orchestration lives in
app.transformation.service.

Status codes:
- 404: the cleaning run, or the requested transformation profile, doesn't exist
- 409: the cleaning run is not "completed" (e.g. "rejected"), or the
  cleaned artifact's checksum no longer matches its manifest
  (tampered/stale) — never transform a stale, modified, or rejected artifact
- 415: the cleaned artifact's format isn't one Step 7 can read (JSONL only
  for this MVP)
- 400: the request's own configuration is unusable — an invalid window
  mode/size/stride/duration, an unknown feature/statistic name, a
  non-finite numeric value encountered during feature computation, or an
  unparseable timestamp
- 200: transformation executed successfully — including the zero-sample
  case for an empty cleaned dataset, which is a valid, auditable outcome,
  not a server error
- 500: reserved for actual internal failures
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.routes.cleaning import get_cleaned_store
from app.api.routes.governance_gate import enforce_gate, get_governance_repo
from app.catalog.repository import CatalogRepository
from app.core.config import Settings, get_settings
from app.storage.cleaned_store import CleanedArtifactStore
from app.storage.transformed_store import LocalTransformedArtifactStore, TransformedArtifactStore
from app.transformation.features.common import InvalidNumericValueError, UnknownFeatureError
from app.transformation.models import TransformationRequest, TransformationResponse
from app.transformation.profiles.base import InvalidTransformationConfigurationError, UnsupportedWindowModeError
from app.transformation.registry import TransformationProfileNotFoundError, TransformationProfileRegistry
from app.transformation.service import (
    CleaningNotAcceptedError,
    CleaningNotFoundError,
    CleanedArtifactChecksumMismatchError,
    InvalidTimestampTransformationError,
    TransformationService,
    UnsupportedTransformationFileTypeError,
)
from app.transformation.windowing import InvalidWindowConfigurationError, NonMonotonicRowOrderError

router = APIRouter(prefix="/api/v1/transformation", tags=["transformation"])


def get_profile_registry() -> TransformationProfileRegistry:
    return TransformationProfileRegistry()


def get_transformed_store(settings: Settings = Depends(get_settings)) -> TransformedArtifactStore:
    return LocalTransformedArtifactStore(root=settings.TRANSFORMED_STORAGE_ROOT)


def get_transformation_service(
    settings: Settings = Depends(get_settings),
    cleaned_store: CleanedArtifactStore = Depends(get_cleaned_store),
    profile_registry: TransformationProfileRegistry = Depends(get_profile_registry),
    transformed_store: TransformedArtifactStore = Depends(get_transformed_store),
) -> TransformationService:
    return TransformationService(
        cleaned_store=cleaned_store,
        profile_registry=profile_registry,
        transformed_store=transformed_store,
        settings=settings,
    )


@router.post("/{cleaning_id}", response_model=TransformationResponse, status_code=status.HTTP_200_OK)
async def transform(
    cleaning_id: str,
    request: TransformationRequest,
    allow_deprecated: bool = Query(default=False, description="Allow a deprecated input cleaning run/ancestor. Never bypasses an invalid one."),
    service: TransformationService = Depends(get_transformation_service),
    governance_repo: CatalogRepository = Depends(get_governance_repo),
) -> TransformationResponse:
    enforce_gate(governance_repo, artifact_type="cleaning", artifact_id=cleaning_id, allow_deprecated=allow_deprecated)
    try:
        return service.transform(cleaning_id=cleaning_id, request=request)
    except (CleaningNotFoundError, TransformationProfileNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (CleaningNotAcceptedError, CleanedArtifactChecksumMismatchError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except UnsupportedTransformationFileTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except (
        InvalidTransformationConfigurationError,
        UnsupportedWindowModeError,
        UnknownFeatureError,
        InvalidNumericValueError,
        InvalidTimestampTransformationError,
        InvalidWindowConfigurationError,
        NonMonotonicRowOrderError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
