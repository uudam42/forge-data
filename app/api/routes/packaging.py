"""HTTP layer for the packaging API.

Responsible only for request/response translation and mapping domain
errors to HTTP status codes. All orchestration lives in
app.packaging.service.

Status codes:
- 404: the transformation run, the exact `qc_id` supplied, or the
  requested packaging profile doesn't exist
- 409: the transformed artifact's checksum no longer matches its
  manifest, the supplied QC run belongs to a different transformation,
  the QC report's checksum no longer matches its manifest, or the QC
  status isn't accepted (`failed`) — never package a stale, tampered, or
  QC-rejected dataset
- 415: the transformed artifact's format isn't one Step 9 can read
  (JSONL only for this MVP)
- 400: the request's own configuration is unusable — invalid split
  ratios, an unsupported split strategy/grouping mode/export format
- 200: packaging executed — including a policy-`rejected` package (e.g.
  insufficient leakage groups for the requested splits), which is not a
  server error
- 500: reserved for actual internal failures (a leakage invariant
  violation, a missing/duplicate sample_id — engine/data invariants Step
  7/8 are supposed to already guarantee)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.routes.qc import get_qc_store
from app.api.routes.transformation import get_transformed_store
from app.core.config import Settings, get_settings
from app.packaging.exporters.base import ExportDependencyMissingError
from app.packaging.grouping import MissingGroupMetadataError
from app.packaging.leakage import LeakageInvariantViolation, SampleCountMismatch
from app.packaging.models import PackagingRequest, PackagingResponse
from app.packaging.profiles.base import (
    InvalidPackagingConfigurationError,
    InvalidSplitRatiosError,
    UnsupportedExportFormatError,
    UnsupportedGroupingModeError,
    UnsupportedSplitStrategyError,
)
from app.packaging.registry import PackagingProfileNotFoundError, PackagingProfileRegistry
from app.packaging.service import (
    DuplicateSampleIdError,
    MissingSampleIdError,
    PackagingService,
    QCNotAcceptedError,
    QCNotFoundError,
    QCReportChecksumMismatchError,
    QCTransformationMismatchError,
    TransformationNotFoundError,
    TransformedArtifactChecksumMismatchError,
    UnsupportedPackagingFileTypeError,
)
from app.storage.package_store import DatasetPackageStore, LocalDatasetPackageStore
from app.storage.qc_store import QCReportStore
from app.storage.transformed_store import TransformedArtifactStore

router = APIRouter(prefix="/api/v1/packaging", tags=["packaging"])


def get_packaging_profile_registry() -> PackagingProfileRegistry:
    return PackagingProfileRegistry()


def get_package_store(settings: Settings = Depends(get_settings)) -> DatasetPackageStore:
    return LocalDatasetPackageStore(root=settings.PACKAGE_STORAGE_ROOT)


def get_packaging_service(
    settings: Settings = Depends(get_settings),
    transformed_store: TransformedArtifactStore = Depends(get_transformed_store),
    qc_store: QCReportStore = Depends(get_qc_store),
    profile_registry: PackagingProfileRegistry = Depends(get_packaging_profile_registry),
    package_store: DatasetPackageStore = Depends(get_package_store),
) -> PackagingService:
    return PackagingService(
        transformed_store=transformed_store,
        qc_store=qc_store,
        profile_registry=profile_registry,
        package_store=package_store,
        settings=settings,
    )


@router.post("/{transformation_id}", response_model=PackagingResponse, status_code=status.HTTP_200_OK)
async def package_dataset(
    transformation_id: str,
    request: PackagingRequest,
    service: PackagingService = Depends(get_packaging_service),
) -> PackagingResponse:
    try:
        return service.package(transformation_id=transformation_id, request=request)
    except (TransformationNotFoundError, QCNotFoundError, PackagingProfileNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (
        TransformedArtifactChecksumMismatchError,
        QCTransformationMismatchError,
        QCReportChecksumMismatchError,
        QCNotAcceptedError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (UnsupportedPackagingFileTypeError, ExportDependencyMissingError) as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except (
        InvalidPackagingConfigurationError,
        InvalidSplitRatiosError,
        UnsupportedSplitStrategyError,
        UnsupportedGroupingModeError,
        UnsupportedExportFormatError,
        MissingGroupMetadataError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except (MissingSampleIdError, DuplicateSampleIdError, LeakageInvariantViolation, SampleCountMismatch) as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
