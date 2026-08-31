"""HTTP layer for the logical dataset registry: dataset creation, version
registration, version history, latest-version lookup, and reproducibility
metadata.

Status codes:
- 201: a dataset or dataset version was newly created
- 200: an idempotent re-registration (identical dataset, or identical
  dataset+version+package), or any other successful read
- 400: invalid dataset name / SemVer version
- 404: dataset, version, or referenced package not found
- 409: attempting to reassign an existing (dataset, version) to a
  different package_id, or registering a version for a package that
  isn't an accepted `completed` package
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.routes.catalog import get_catalog_service
from app.catalog.errors import (
    DatasetNotFoundError,
    DatasetVersionImmutableError,
    DatasetVersionNotFoundError,
    InvalidDatasetNameError,
    InvalidDatasetVersionError,
    PackageNotAcceptedError,
    PackageNotFoundError,
)
from app.catalog.models import (
    DatasetCreateRequest,
    DatasetResponse,
    DatasetVersionCreateRequest,
    DatasetVersionResponse,
    ReproducibilityResponse,
)
from app.catalog.service import CatalogService

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])


@router.post("", response_model=DatasetResponse)
async def create_dataset(
    request: DatasetCreateRequest, response: Response, service: CatalogService = Depends(get_catalog_service)
) -> DatasetResponse:
    try:
        result, created = service.create_dataset(
            dataset_name=request.dataset_name, description=request.description, metadata=request.metadata
        )
    except InvalidDatasetNameError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return result


@router.get("", response_model=list[DatasetResponse])
async def list_datasets(service: CatalogService = Depends(get_catalog_service)) -> list[DatasetResponse]:
    return service.list_datasets()


@router.post("/{dataset_name}/versions", response_model=DatasetVersionResponse)
async def register_version(
    dataset_name: str,
    request: DatasetVersionCreateRequest,
    response: Response,
    service: CatalogService = Depends(get_catalog_service),
) -> DatasetVersionResponse:
    try:
        result, created = service.register_version(
            dataset_name,
            version=request.version,
            package_id=request.package_id,
            description=request.description,
            tags=request.tags,
        )
    except DatasetNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PackageNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidDatasetVersionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except PackageNotAcceptedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DatasetVersionImmutableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return result


@router.get("/{dataset_name}/versions", response_model=list[DatasetVersionResponse])
async def list_versions(
    dataset_name: str, service: CatalogService = Depends(get_catalog_service)
) -> list[DatasetVersionResponse]:
    try:
        return service.list_versions(dataset_name)
    except DatasetNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{dataset_name}/latest", response_model=DatasetVersionResponse)
async def get_latest(dataset_name: str, service: CatalogService = Depends(get_catalog_service)) -> DatasetVersionResponse:
    try:
        return service.get_latest(dataset_name)
    except (DatasetNotFoundError, DatasetVersionNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{dataset_name}/versions/{version}", response_model=DatasetVersionResponse)
async def get_version(
    dataset_name: str, version: str, service: CatalogService = Depends(get_catalog_service)
) -> DatasetVersionResponse:
    try:
        return service.get_version(dataset_name, version)
    except (DatasetNotFoundError, DatasetVersionNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{dataset_name}/versions/{version}/reproducibility", response_model=ReproducibilityResponse)
async def get_reproducibility(
    dataset_name: str, version: str, service: CatalogService = Depends(get_catalog_service)
) -> ReproducibilityResponse:
    try:
        return service.reproducibility(dataset_name, version)
    except (DatasetNotFoundError, DatasetVersionNotFoundError, PackageNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
