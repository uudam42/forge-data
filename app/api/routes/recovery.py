"""HTTP layer for the crash-recovery scan/cleanup service (v2.1).

Minimal by design — this is not a CLI, not a scheduler, and does not run
automatically. GET /scan is read-only; POST /cleanup only ever removes
entries the scan itself classified STALE (see app.storage.recovery).

Status codes:
- 200: scan or cleanup executed successfully, including a scan that
  reports stale/invalid entries — reporting a real finding is not a
  server error, mirroring the catalog health-check convention.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.core.config import Settings, get_settings
from app.storage.recovery import RecoveryEntry, RecoveryScanResult, RecoveryService

router = APIRouter(prefix="/api/v1/recovery", tags=["recovery"])


def get_recovery_service(settings: Settings = Depends(get_settings)) -> RecoveryService:
    return RecoveryService(settings)


@router.get("/scan", response_model=RecoveryScanResult, status_code=status.HTTP_200_OK)
async def scan(service: RecoveryService = Depends(get_recovery_service)) -> RecoveryScanResult:
    return service.scan()


@router.post("/cleanup", response_model=list[RecoveryEntry], status_code=status.HTTP_200_OK)
async def cleanup(
    dry_run: bool = Query(default=False, description="Report what would be removed without removing it"),
    service: RecoveryService = Depends(get_recovery_service),
) -> list[RecoveryEntry]:
    return service.cleanup_stale(dry_run=dry_run)
