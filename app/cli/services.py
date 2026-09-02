"""Plain-function equivalents of the FastAPI `Depends(...)` wiring in
`app/api/routes/*.py`, for CLI commands that call the application/service
layer directly instead of going over HTTP (Design Requirement 1's
"CLI may call the application/service layer directly where appropriate").
Every constructor call here is copy-identical to its route's dependency
function -- no new wiring pattern, no shortcut through internals.
"""

from __future__ import annotations

from app.catalog.rebuild_lock import RebuildLock
from app.catalog.repository import CatalogRepository
from app.catalog.scanner import CatalogScanner
from app.catalog.service import CatalogService
from app.catalog.verifier import ArtifactVerifier
from app.core.config import Settings
from app.runs.repository import RunRepository
from app.runs.results import RunResultsService
from app.runs.service import RunService
from app.storage.catalog_store import get_connection
from app.storage.recovery import RecoveryService


def build_catalog_service(settings: Settings) -> CatalogService:
    conn = get_connection(settings.CATALOG_DB_PATH, busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS, journal_mode=settings.CATALOG_JOURNAL_MODE)
    repo = CatalogRepository(conn, db_path=str(settings.CATALOG_DB_PATH), busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS)
    scanner = CatalogScanner(settings)
    verifier = ArtifactVerifier(settings)
    lock_path = settings.CATALOG_DB_PATH.parent / "catalog.rebuild.lock"
    return CatalogService(repo=repo, scanner=scanner, verifier=verifier, rebuild_lock=RebuildLock(lock_path), settings=settings)


def build_run_service(settings: Settings) -> RunService:
    conn = get_connection(settings.CATALOG_DB_PATH, busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS, journal_mode=settings.CATALOG_JOURNAL_MODE)
    repo = RunRepository(conn, db_path=str(settings.CATALOG_DB_PATH), busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS)
    return RunService(repo=repo, settings=settings)


def build_run_results_service(settings: Settings, catalog_service: CatalogService) -> RunResultsService:
    conn = get_connection(settings.CATALOG_DB_PATH, busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS, journal_mode=settings.CATALOG_JOURNAL_MODE)
    catalog_repo = CatalogRepository(conn, db_path=str(settings.CATALOG_DB_PATH), busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS)
    return RunResultsService(catalog_repo=catalog_repo, catalog_service=catalog_service)


def build_recovery_service(settings: Settings) -> RecoveryService:
    return RecoveryService(settings)
