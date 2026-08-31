"""HTTP layer for the cleaning API.

Responsible only for request/response translation and mapping domain
errors to HTTP status codes. All orchestration lives in
app.cleaning.service.

Status codes:
- 404: the synchronization run, or the requested cleaning policy, doesn't exist
- 409: the synchronized artifact's checksum no longer matches its manifest
  (tampered/stale) — never clean a stale or modified artifact
- 415: the synchronized artifact's format isn't one Step 6 can read
  (JSONL only for this MVP)
- 400: the request's own configuration is unusable — a negative
  min_present_streams/minimum_retained_rows, or a structurally invalid
  redaction path
- 200: cleaning executed, whether the result is "completed" or
  policy-"rejected" — a rejected run is not a server error, and still
  produces a committed, auditable artifact + report
- 500: reserved for actual internal failures
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.cleaning.models import CleaningRequest, CleaningResponse
from app.cleaning.registry import CleaningPolicyNotFoundError, CleaningPolicyRegistry
from app.cleaning.service import (
    CleaningService,
    InvalidCleaningConfigurationError,
    InvalidRedactionPathError,
    SynchronizationNotFoundError,
    SynchronizedArtifactChecksumMismatchError,
    UnsupportedCleaningFileTypeError,
)
from app.core.config import Settings, get_settings
from app.storage.cleaned_store import CleanedArtifactStore, LocalCleanedArtifactStore
from app.storage.synchronization_store import SynchronizationArtifactStore
from app.api.routes.synchronization import get_synchronization_artifact_store

router = APIRouter(prefix="/api/v1/cleaning", tags=["cleaning"])


def get_policy_registry() -> CleaningPolicyRegistry:
    return CleaningPolicyRegistry()


def get_cleaned_store(settings: Settings = Depends(get_settings)) -> CleanedArtifactStore:
    return LocalCleanedArtifactStore(root=settings.CLEANED_STORAGE_ROOT)


def get_cleaning_service(
    settings: Settings = Depends(get_settings),
    sync_store: SynchronizationArtifactStore = Depends(get_synchronization_artifact_store),
    policy_registry: CleaningPolicyRegistry = Depends(get_policy_registry),
    cleaned_store: CleanedArtifactStore = Depends(get_cleaned_store),
) -> CleaningService:
    return CleaningService(
        sync_store=sync_store,
        policy_registry=policy_registry,
        cleaned_store=cleaned_store,
        settings=settings,
    )


@router.post("/{synchronization_id}", response_model=CleaningResponse, status_code=status.HTTP_200_OK)
async def clean(
    synchronization_id: str,
    request: CleaningRequest,
    service: CleaningService = Depends(get_cleaning_service),
) -> CleaningResponse:
    try:
        return service.clean(synchronization_id=synchronization_id, request=request)
    except (SynchronizationNotFoundError, CleaningPolicyNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SynchronizedArtifactChecksumMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except UnsupportedCleaningFileTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except (InvalidCleaningConfigurationError, InvalidRedactionPathError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
