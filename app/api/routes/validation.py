"""HTTP layer for the schema validation API.

Responsible only for request/response translation and mapping domain errors
to HTTP status codes. All orchestration lives in app.validation.service.

A structurally invalid dataset is not a server error: it returns HTTP 200
with status="failed". 4xx/5xx are reserved for request-level problems
(ingestion not found, schema not found, unsupported file type) and genuine
system failures.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.routes.ingestion import get_storage
from app.core.config import Settings, get_settings
from app.storage.base import RawStorage
from app.storage.validation_store import LocalValidationReportStore, ValidationReportStore
from app.validation.models import ValidationRequest, ValidationResponse
from app.validation.registry import ValidatorRegistry
from app.validation.schemas.registry import SchemaRegistry
from app.validation.service import (
    IngestionNotFoundError,
    SchemaNotFoundError,
    UnsupportedValidationFileTypeError,
    ValidationService,
)

router = APIRouter(prefix="/api/v1/validation", tags=["validation"])


def get_schema_registry(settings: Settings = Depends(get_settings)) -> SchemaRegistry:
    return SchemaRegistry(schema_dir=settings.SCHEMA_DIR)


def get_validator_registry() -> ValidatorRegistry:
    return ValidatorRegistry()


def get_report_store(settings: Settings = Depends(get_settings)) -> ValidationReportStore:
    return LocalValidationReportStore(root=settings.VALIDATION_STORAGE_ROOT)


def get_validation_service(
    settings: Settings = Depends(get_settings),
    storage: RawStorage = Depends(get_storage),
    schema_registry: SchemaRegistry = Depends(get_schema_registry),
    validator_registry: ValidatorRegistry = Depends(get_validator_registry),
    report_store: ValidationReportStore = Depends(get_report_store),
) -> ValidationService:
    return ValidationService(
        storage=storage,
        schema_registry=schema_registry,
        validator_registry=validator_registry,
        report_store=report_store,
        settings=settings,
    )


@router.post("/{ingestion_id}", response_model=ValidationResponse, status_code=status.HTTP_200_OK)
async def validate(
    ingestion_id: str,
    request: ValidationRequest,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationResponse:
    try:
        return service.validate(
            ingestion_id=ingestion_id,
            schema_name=request.schema_name,
            schema_version=request.schema_version,
        )
    except IngestionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SchemaNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UnsupportedValidationFileTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
