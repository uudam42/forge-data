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
  isn't an accepted `completed` package, or the package's effective
  lineage includes an invalid/deprecated artifact (v2.5 governance gate
  — see `allow_deprecated`)
- 503: the catalog was too busy to acquire a write lock within the
  configured timeout (CATALOG_BUSY) — transient, safe to retry
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.routes.catalog import get_catalog_service, raise_busy
from app.catalog.errors import (
    ArtifactDeprecatedError,
    ArtifactInvalidError,
    CatalogBusyError,
    DatasetNotFoundError,
    DatasetVersionImmutableError,
    DatasetVersionNotFoundError,
    GovernanceReasonRequiredError,
    InvalidDatasetNameError,
    InvalidDatasetVersionError,
    InvalidGovernanceTransitionError,
    PackageNotAcceptedError,
    PackageNotFoundError,
    UpstreamArtifactDeprecatedError,
    UpstreamArtifactInvalidError,
)
from app.catalog.governance_models import (
    DatasetVersionGovernanceActionRequest,
    DatasetVersionGovernanceHistoryResponse,
    DatasetVersionGovernanceResponse,
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
    except CatalogBusyError as exc:
        raise_busy(exc)
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
    allow_deprecated: bool = Query(default=False, description="Allow a package with a deprecated artifact/ancestor. Never bypasses an INVALID artifact/ancestor."),
    service: CatalogService = Depends(get_catalog_service),
) -> DatasetVersionResponse:
    try:
        result, created = service.register_version(
            dataset_name,
            version=request.version,
            package_id=request.package_id,
            description=request.description,
            tags=request.tags,
            allow_deprecated=allow_deprecated,
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
    except (ArtifactInvalidError, UpstreamArtifactInvalidError, ArtifactDeprecatedError, UpstreamArtifactDeprecatedError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.to_dict()) from exc
    except CatalogBusyError as exc:
        raise_busy(exc)
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


# ---------------------------------------------------------------------------
# Dataset-version governance (v2.5) — the (dataset, version) -> package_id
# mapping itself is NEVER touched by any of these; see Design Requirement 10.
# ---------------------------------------------------------------------------


def _version_governance_error_map(exc: Exception):
    if isinstance(exc, (DatasetNotFoundError, DatasetVersionNotFoundError)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, (InvalidGovernanceTransitionError, GovernanceReasonRequiredError)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if isinstance(exc, CatalogBusyError):
        raise_busy(exc)
    raise exc


@router.get("/{dataset_name}/versions/{version}/governance", response_model=DatasetVersionGovernanceResponse)
async def get_version_governance(
    dataset_name: str, version: str, service: CatalogService = Depends(get_catalog_service)
) -> DatasetVersionGovernanceResponse:
    try:
        return service.get_dataset_version_governance(dataset_name, version)
    except Exception as exc:
        _version_governance_error_map(exc)


@router.get("/{dataset_name}/versions/{version}/governance/history", response_model=DatasetVersionGovernanceHistoryResponse)
async def get_version_governance_history(
    dataset_name: str, version: str, service: CatalogService = Depends(get_catalog_service)
) -> DatasetVersionGovernanceHistoryResponse:
    try:
        return service.get_dataset_version_governance_history(dataset_name, version)
    except Exception as exc:
        _version_governance_error_map(exc)


def _set_version_governance(
    dataset_name: str, version: str, *, new_state: str, request: DatasetVersionGovernanceActionRequest, service: CatalogService
) -> DatasetVersionGovernanceResponse:
    try:
        return service.set_dataset_version_governance(dataset_name, version, new_state=new_state, reason=request.reason, actor=request.actor)
    except Exception as exc:
        _version_governance_error_map(exc)


@router.post("/{dataset_name}/versions/{version}/deprecate", response_model=DatasetVersionGovernanceResponse)
async def deprecate_version(
    dataset_name: str, version: str, request: DatasetVersionGovernanceActionRequest, service: CatalogService = Depends(get_catalog_service)
) -> DatasetVersionGovernanceResponse:
    return _set_version_governance(dataset_name, version, new_state="deprecated", request=request, service=service)


@router.post("/{dataset_name}/versions/{version}/invalidate", response_model=DatasetVersionGovernanceResponse)
async def invalidate_version(
    dataset_name: str, version: str, request: DatasetVersionGovernanceActionRequest, service: CatalogService = Depends(get_catalog_service)
) -> DatasetVersionGovernanceResponse:
    return _set_version_governance(dataset_name, version, new_state="invalid", request=request, service=service)


@router.post("/{dataset_name}/versions/{version}/reactivate", response_model=DatasetVersionGovernanceResponse)
async def reactivate_version(
    dataset_name: str, version: str, request: DatasetVersionGovernanceActionRequest, service: CatalogService = Depends(get_catalog_service)
) -> DatasetVersionGovernanceResponse:
    return _set_version_governance(dataset_name, version, new_state="active", request=request, service=service)
