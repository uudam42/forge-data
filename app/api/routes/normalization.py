"""HTTP layer for the normalization API.

Responsible only for request/response translation and mapping domain errors
to HTTP status codes. All orchestration lives in app.normalization.service.

Status codes:
- 404: ingestion not found, schema not found, or normalization profile not found
- 409: no matching validation/integrity report for this ingestion + schema +
  current raw checksum, a matching report exists but did not pass (or an
  integrity report references a different validation run than the accepted
  one) — collectively "lineage/gate conflicts"
- 415: unsupported file type (e.g. .zip)
- 400: the request's own configuration is unusable — a required source unit
  wasn't supplied or is unsupported, an alias mapping is ambiguous, a record
  cannot be deterministically converted under this config, or the request
  content itself is invalid. These are request/config problems, not lineage
  conflicts and not internal failures.
- 500: reserved for actual internal failures (including a raw-checksum
  mismatch against the ingestion manifest — a storage-invariant violation,
  not a normal request-level condition)
- 200: normalization completed (there is no "failed" status object — a
  failed run commits nothing at all; see NormalizationStatus docstring)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.routes.ingestion import get_storage
from app.api.routes.validation import get_report_store as get_validation_report_store
from app.api.routes.validation import get_schema_registry
from app.api.routes.integrity import get_integrity_report_store
from app.api.routes.governance_gate import enforce_gate, get_governance_repo
from app.catalog.repository import CatalogRepository
from app.core.config import Settings, get_settings
from app.normalization.models import NormalizationRequest, NormalizationResponse
from app.normalization.profiles.base import (
    AmbiguousFieldMappingError,
    MissingUnitMetadataError,
    NormalizationConversionError,
    UnsupportedSourceUnitError,
)
from app.normalization.registry import NormalizationProfileNotFoundError, NormalizationProfileRegistry
from app.normalization.service import (
    IngestionNotFoundError,
    IntegrityLineageMismatchError,
    IntegrityNotPassedError,
    InvalidNormalizationInputError,
    NoMatchingIntegrityReportError,
    NoMatchingValidationReportError,
    NormalizationService,
    SchemaNotFoundError,
    UnsupportedNormalizationFileTypeError,
    ValidationNotPassedError,
)
from app.storage.base import RawStorage
from app.storage.integrity_store import IntegrityReportStore
from app.storage.normalized_store import LocalNormalizedArtifactStore, NormalizedArtifactStore
from app.storage.validation_store import ValidationReportStore
from app.validation.schemas.registry import SchemaRegistry

router = APIRouter(prefix="/api/v1/normalization", tags=["normalization"])


def get_profile_registry() -> NormalizationProfileRegistry:
    return NormalizationProfileRegistry()


def get_artifact_store(settings: Settings = Depends(get_settings)) -> NormalizedArtifactStore:
    return LocalNormalizedArtifactStore(root=settings.NORMALIZED_STORAGE_ROOT)


def get_normalization_service(
    settings: Settings = Depends(get_settings),
    storage: RawStorage = Depends(get_storage),
    schema_registry: SchemaRegistry = Depends(get_schema_registry),
    validation_report_store: ValidationReportStore = Depends(get_validation_report_store),
    integrity_report_store: IntegrityReportStore = Depends(get_integrity_report_store),
    profile_registry: NormalizationProfileRegistry = Depends(get_profile_registry),
    artifact_store: NormalizedArtifactStore = Depends(get_artifact_store),
) -> NormalizationService:
    return NormalizationService(
        storage=storage,
        schema_registry=schema_registry,
        validation_report_store=validation_report_store,
        integrity_report_store=integrity_report_store,
        profile_registry=profile_registry,
        artifact_store=artifact_store,
        settings=settings,
    )


@router.post("/{ingestion_id}", response_model=NormalizationResponse, status_code=status.HTTP_200_OK)
async def normalize(
    ingestion_id: str,
    request: NormalizationRequest,
    allow_deprecated: bool = Query(default=False, description="Allow a deprecated input ingestion/ancestor. Never bypasses an invalid one."),
    service: NormalizationService = Depends(get_normalization_service),
    governance_repo: CatalogRepository = Depends(get_governance_repo),
) -> NormalizationResponse:
    enforce_gate(governance_repo, artifact_type="ingestion", artifact_id=ingestion_id, allow_deprecated=allow_deprecated)
    try:
        return service.normalize(
            ingestion_id=ingestion_id,
            schema_name=request.schema_name,
            schema_version=request.schema_version,
            profile_name=request.profile_name,
            profile_version=request.profile_version,
            source_units=request.source_units,
        )
    except (IngestionNotFoundError, SchemaNotFoundError, NormalizationProfileNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (
        NoMatchingValidationReportError,
        ValidationNotPassedError,
        NoMatchingIntegrityReportError,
        IntegrityLineageMismatchError,
        IntegrityNotPassedError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except UnsupportedNormalizationFileTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except (
        MissingUnitMetadataError,
        UnsupportedSourceUnitError,
        AmbiguousFieldMappingError,
        NormalizationConversionError,
        InvalidNormalizationInputError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
