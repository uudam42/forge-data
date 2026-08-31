"""HTTP layer for the multimodal synchronization API.

Responsible only for request/response translation and mapping domain
errors to HTTP status codes. All orchestration lives in
app.synchronization.service.

Status codes:
- 404: a referenced normalization run (or the schema it was normalized
  against) cannot be found
- 409: lineage/configuration conflicts — a normalized artifact's checksum
  no longer matches its manifest (tampered/stale), participating streams
  belong to different sessions, or a stream's timestamps are non-monotonic
- 415: a normalized artifact's file type isn't one Step 5 can read (should
  be unreachable given Step 4's own guarantees; kept as defense-in-depth)
- 400: the request's own configuration is unusable — fewer than two
  streams, duplicate stream names, an unresolvable reference stream, an
  invalid fixed-rate frequency, an unsupported/mismatched alignment method,
  a drift configuration that would reverse time order, or a record that
  can't be deterministically combined under this config
- 500: reserved for actual internal failures (including a normalized
  timestamp that fails to parse — a violation of Step 4's own guarantee,
  not a normal request-level condition)
- 200: synchronization executed. Alignment gaps are reported in the
  output/coverage, never raised as HTTP errors.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.routes.ingestion import get_storage
from app.api.routes.validation import get_schema_registry
from app.core.config import Settings, get_settings
from app.storage.base import RawStorage
from app.storage.normalized_store import LocalNormalizedArtifactStore, NormalizedArtifactStore
from app.storage.synchronization_store import LocalSynchronizationArtifactStore, SynchronizationArtifactStore
from app.synchronization.models import SynchronizationRequest, SynchronizationResponse
from app.synchronization.readers import NonMonotonicStreamError
from app.synchronization.registry import AlignmentStrategyRegistry, UnsupportedAlignmentMethodError
from app.synchronization.service import (
    ClockCorrectionError,
    DuplicateStreamNameError,
    NormalizationNotFoundError,
    NormalizedArtifactChecksumMismatchError,
    ReferenceStreamNotFoundError,
    SchemaNotFoundError,
    SessionMismatchError,
    SynchronizationConversionError,
    SynchronizationService,
    UnsupportedSyncFileTypeError,
)
from app.synchronization.timeline import InvalidSyncConfigurationError
from app.validation.schemas.registry import SchemaRegistry

router = APIRouter(prefix="/api/v1/synchronization", tags=["synchronization"])


def get_normalized_store(settings: Settings = Depends(get_settings)) -> NormalizedArtifactStore:
    return LocalNormalizedArtifactStore(root=settings.NORMALIZED_STORAGE_ROOT)


def get_strategy_registry() -> AlignmentStrategyRegistry:
    return AlignmentStrategyRegistry()


def get_synchronization_artifact_store(
    settings: Settings = Depends(get_settings),
) -> SynchronizationArtifactStore:
    return LocalSynchronizationArtifactStore(root=settings.SYNCHRONIZED_STORAGE_ROOT)


def get_synchronization_service(
    settings: Settings = Depends(get_settings),
    raw_storage: RawStorage = Depends(get_storage),
    normalized_store: NormalizedArtifactStore = Depends(get_normalized_store),
    schema_registry: SchemaRegistry = Depends(get_schema_registry),
    strategy_registry: AlignmentStrategyRegistry = Depends(get_strategy_registry),
    artifact_store: SynchronizationArtifactStore = Depends(get_synchronization_artifact_store),
) -> SynchronizationService:
    return SynchronizationService(
        raw_storage=raw_storage,
        normalized_store=normalized_store,
        schema_registry=schema_registry,
        strategy_registry=strategy_registry,
        artifact_store=artifact_store,
        settings=settings,
    )


@router.post("", response_model=SynchronizationResponse, status_code=status.HTTP_200_OK)
async def synchronize(
    request: SynchronizationRequest,
    service: SynchronizationService = Depends(get_synchronization_service),
) -> SynchronizationResponse:
    try:
        return service.synchronize(request)
    except (NormalizationNotFoundError, SchemaNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (
        NormalizedArtifactChecksumMismatchError,
        SessionMismatchError,
        NonMonotonicStreamError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except UnsupportedSyncFileTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except (
        DuplicateStreamNameError,
        ReferenceStreamNotFoundError,
        InvalidSyncConfigurationError,
        UnsupportedAlignmentMethodError,
        ClockCorrectionError,
        SynchronizationConversionError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
