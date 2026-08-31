"""HTTP layer for the data integrity API.

Responsible only for request/response translation and mapping domain errors
to HTTP status codes. All orchestration lives in app.integrity.service.

Status codes:
- 404: ingestion not found, schema not found
- 409: no matching validation report for this ingestion/schema/raw checksum,
  or a matching report exists but did not pass
- 415: unsupported file type or no integrity checker for this schema
- 200: integrity checks executed (status may be "passed", "passed_with_warnings",
  or "failed" — a failed integrity run is a normal, successful API call, exactly
  like a failed validation run in Step 2)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.routes.ingestion import get_storage
from app.api.routes.validation import get_report_store as get_validation_report_store
from app.api.routes.validation import get_schema_registry
from app.core.config import Settings, get_settings
from app.integrity.models import IntegrityRequest, IntegrityResponse
from app.integrity.registry import IntegrityCheckerRegistry
from app.integrity.service import (
    IngestionNotFoundError,
    IntegrityService,
    NoMatchingValidationReportError,
    SchemaNotFoundError,
    UnsupportedIntegrityCheckerError,
    UnsupportedIntegrityFileTypeError,
    ValidationNotPassedError,
)
from app.storage.base import RawStorage
from app.storage.integrity_store import IntegrityReportStore, LocalIntegrityReportStore
from app.storage.validation_store import ValidationReportStore
from app.validation.schemas.registry import SchemaRegistry

router = APIRouter(prefix="/api/v1/integrity", tags=["integrity"])


def get_checker_registry() -> IntegrityCheckerRegistry:
    return IntegrityCheckerRegistry()


def get_integrity_report_store(settings: Settings = Depends(get_settings)) -> IntegrityReportStore:
    return LocalIntegrityReportStore(root=settings.INTEGRITY_STORAGE_ROOT)


def get_integrity_service(
    settings: Settings = Depends(get_settings),
    storage: RawStorage = Depends(get_storage),
    schema_registry: SchemaRegistry = Depends(get_schema_registry),
    validation_report_store: ValidationReportStore = Depends(get_validation_report_store),
    checker_registry: IntegrityCheckerRegistry = Depends(get_checker_registry),
    report_store: IntegrityReportStore = Depends(get_integrity_report_store),
) -> IntegrityService:
    return IntegrityService(
        storage=storage,
        schema_registry=schema_registry,
        validation_report_store=validation_report_store,
        checker_registry=checker_registry,
        report_store=report_store,
        settings=settings,
    )


@router.post("/{ingestion_id}", response_model=IntegrityResponse, status_code=status.HTTP_200_OK)
async def run_integrity_checks(
    ingestion_id: str,
    request: IntegrityRequest,
    service: IntegrityService = Depends(get_integrity_service),
) -> IntegrityResponse:
    try:
        return service.run(
            ingestion_id=ingestion_id,
            schema_name=request.schema_name,
            schema_version=request.schema_version,
        )
    except IngestionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SchemaNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (NoMatchingValidationReportError, ValidationNotPassedError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (UnsupportedIntegrityFileTypeError, UnsupportedIntegrityCheckerError) as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
