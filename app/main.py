"""FastAPI application entrypoint."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.routes.catalog import router as catalog_router
from app.api.routes.cleaning import router as cleaning_router
from app.api.routes.datasets import router as datasets_router
from app.api.routes.ingestion import router as ingestion_router
from app.api.routes.integrity import router as integrity_router
from app.api.routes.lineage import router as lineage_router
from app.api.routes.normalization import router as normalization_router
from app.api.routes.packaging import router as packaging_router
from app.api.routes.qc import router as qc_router
from app.api.routes.recovery import router as recovery_router
from app.api.routes.sensors import router as sensors_router
from app.api.routes.synchronization import router as synchronization_router
from app.api.routes.transformation import router as transformation_router
from app.api.routes.validation import router as validation_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.ingestion.models import HealthResponse

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
    version="0.10.0",
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


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")
