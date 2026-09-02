"""FastAPI application entrypoint."""

from __future__ import annotations

import importlib.resources
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes.catalog import router as catalog_router
from app.api.routes.cleaning import router as cleaning_router
from app.api.routes.datasets import router as datasets_router
from app.api.routes.ingestion import router as ingestion_router
from app.api.routes.integrity import router as integrity_router
from app.api.routes.lineage import router as lineage_router
from app.api.routes.normalization import router as normalization_router
from app.api.routes.packages import router as packages_router
from app.api.routes.packaging import router as packaging_router
from app.api.routes.qc import router as qc_router
from app.api.routes.rebuild import router as rebuild_router
from app.api.routes.recovery import router as recovery_router
from app.api.routes.runs import router as runs_router
from app.api.routes.sensors import router as sensors_router
from app.api.routes.synchronization import router as synchronization_router
from app.api.routes.transformation import router as transformation_router
from app.api.routes.validation import router as validation_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.ingestion.models import HealthResponse
from app.runs.recovery import RunRecoveryService
from app.runs.repository import RunRepository
from app.storage.catalog_store import get_connection
from app.version import __version__

settings = get_settings()
configure_logging(settings.LOG_LEVEL)

app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Ingestion + schema validation + data integrity + normalization + multimodal "
        "synchronization + cleaning/filtering + transformation/feature-generation layers for a "
        "robotics / physical AI / multimodal sensor data pipeline (Step 1: raw ingestion, "
        "Step 2: schema validation, Step 3: data integrity checks, Step 4: normalization, "
        "Step 5: multimodal synchronization, Step 6: cleaning/filtering, "
        "Step 7: transformation/feature generation, Step 8: dataset QC, "
        "Step 9: dataset packaging/export, Step 10: versioning + global lineage catalog). "
        "v2.1 adds crash-safe, atomically-published artifacts and a staging recovery service "
        "across every stage's storage layer — a cross-cutting reliability upgrade, not a new stage."
    ),
    version=__version__,
)

app.include_router(ingestion_router)
app.include_router(validation_router)
app.include_router(integrity_router)
app.include_router(normalization_router)
app.include_router(synchronization_router)
app.include_router(cleaning_router)
app.include_router(transformation_router)
app.include_router(qc_router)
app.include_router(packaging_router)
app.include_router(catalog_router)
app.include_router(lineage_router)
app.include_router(datasets_router)
app.include_router(recovery_router)
app.include_router(sensors_router)
app.include_router(rebuild_router)
app.include_router(runs_router)
app.include_router(packages_router)


@app.on_event("startup")
def _reconcile_stale_runs() -> None:
    """v2.6 Design Requirement 34: any run left `running`/
    `cancel_requested` from a process that crashed before this restart is
    marked `failed` with RUN_PROCESS_LOST here, once, at startup --
    never automatically resumed or retried.

    Resolves settings via `app.dependency_overrides` (falling back to the
    real cached `get_settings()`) rather than closing over the module-level
    `settings` bound at import time. `@app.on_event("startup")` does not
    support `Depends()` injection, but `TestClient(app)` used as a context
    manager -- the established pattern in every test's `client` fixture --
    does fire this handler, so without this it would silently ignore each
    test's `app.dependency_overrides[get_settings]` override and touch the
    real project `data/catalog/catalog.db` on every such test.
    """
    current_settings = app.dependency_overrides.get(get_settings, get_settings)()
    conn = get_connection(current_settings.CATALOG_DB_PATH, busy_timeout_ms=current_settings.CATALOG_BUSY_TIMEOUT_MS, journal_mode=current_settings.CATALOG_JOURNAL_MODE)
    repo = RunRepository(conn, db_path=str(current_settings.CATALOG_DB_PATH), busy_timeout_ms=current_settings.CATALOG_BUSY_TIMEOUT_MS)
    reconciled = RunRecoveryService(repo=repo, stale_after_seconds=current_settings.RUN_STALE_HEARTBEAT_SECONDS).reconcile()
    if reconciled:
        import logging

        logging.getLogger("app.runs.recovery").warning("STARTUP_RUN_RECONCILIATION reconciled=%d", reconciled)
    conn.close()


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


# ---------------------------------------------------------------------------
# Local GUI static serving (v2.7). `app/web/dist/` holds the built React/
# Vite production output -- present in an installed wheel (packaged via
# `[tool.setuptools.package-data]`), absent in a plain source checkout that
# never ran the frontend build. Registered LAST so every `/api/v1/*` route
# above always wins the match; only a path Starlette couldn't otherwise
# resolve reaches the SPA fallback, and `full_path.startswith("api/")` is a
# second, explicit guard against ever serving `index.html` for an API path.
# ---------------------------------------------------------------------------

_WEB_DIST = Path(str(importlib.resources.files("app.web"))) / "dist"
_WEB_INDEX = _WEB_DIST / "index.html"

if _WEB_INDEX.is_file():
    _assets_dir = _WEB_DIST / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="frontend-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = _WEB_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_WEB_INDEX))
