"""Shared downstream-processing gate (v2.5, Design Requirement 7/8).

Injected into every pipeline stage's create route so a request whose
direct input (or one of its transitive ancestors) is governed
invalid/deprecated is rejected BEFORE the (otherwise completely
unmodified) stage service runs. This is the only place these six routes
touch the catalog at all -- see docs/DETAILED_GUIDE.md's v2.5 section,
"Downstream gating", for why that's a deliberate, scan-driven
limitation: an artifact never registered into the catalog (no scan run
yet) has nothing to gate against and is silently let through.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.catalog import governance
from app.catalog.errors import (
    ArtifactDeprecatedError,
    ArtifactInvalidError,
    CatalogBusyError,
    UpstreamArtifactDeprecatedError,
    UpstreamArtifactInvalidError,
)
from app.catalog.repository import CatalogRepository
from app.core.config import Settings, get_settings
from app.storage.catalog_store import get_connection


def get_governance_repo(settings: Settings = Depends(get_settings)) -> CatalogRepository:
    conn = get_connection(
        settings.CATALOG_DB_PATH, busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS, journal_mode=settings.CATALOG_JOURNAL_MODE
    )
    return CatalogRepository(conn, db_path=str(settings.CATALOG_DB_PATH), busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS)


def enforce_gate(repo: CatalogRepository, *, artifact_type: str, artifact_id: str, allow_deprecated: bool) -> None:
    try:
        governance.enforce_upstream_gate(repo, artifact_type=artifact_type, artifact_id=artifact_id, allow_deprecated=allow_deprecated)
    except (ArtifactInvalidError, UpstreamArtifactInvalidError, ArtifactDeprecatedError, UpstreamArtifactDeprecatedError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.to_dict()) from exc
    except CatalogBusyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.to_dict()
        ) from exc
