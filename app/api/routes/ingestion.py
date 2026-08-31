"""HTTP layer for the ingestion API.

Responsible only for request/response translation and mapping domain errors
to HTTP status codes. All business logic lives in app.ingestion.service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.core.config import Settings, get_settings
from app.ingestion.models import IngestionResponse
from app.ingestion.service import (
    EmptyFileError,
    FileTooLargeError,
    IngestionConflictError,
    IngestionService,
    UnsupportedFileTypeError,
    UploadRequest,
)
from app.storage.base import RawStorage
from app.storage.local import LocalRawStorage

router = APIRouter(prefix="/api/v1/ingestion", tags=["ingestion"])


def get_storage(settings: Settings = Depends(get_settings)) -> RawStorage:
    return LocalRawStorage(root=settings.RAW_STORAGE_ROOT)


def get_ingestion_service(
    settings: Settings = Depends(get_settings),
    storage: RawStorage = Depends(get_storage),
) -> IngestionService:
    return IngestionService(storage=storage, settings=settings)


@router.post(
    "/upload",
    response_model=IngestionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload(
    file: UploadFile = File(...),
    customer_id: str | None = Form(default=None),
    device_id: str | None = Form(default=None),
    session_id: str | None = Form(default=None),
    source_type: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionResponse:
    request = UploadRequest(
        filename=file.filename,
        content_type=file.content_type,
        stream=file.file,
        customer_id=customer_id,
        device_id=device_id,
        session_id=session_id,
        source_type=source_type,
        notes=notes,
    )

    try:
        return service.ingest(request)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)) from exc
    except EmptyFileError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FileTooLargeError as exc:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)) from exc
    except IngestionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    finally:
        await file.close()
