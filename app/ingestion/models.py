"""Pydantic models for the ingestion API and manifest schema."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

_ANONYMOUS_CUSTOMER = "anonymous"


class IngestionResponse(BaseModel):
    ingestion_id: str
    session_id: str
    customer_id: str
    device_id: str | None = None
    source_type: str | None = None
    original_filename: str
    content_type: str | None = None
    size_bytes: int
    sha256: str
    storage_uri: str
    status: str = "stored"


class Manifest(BaseModel):
    """Persisted alongside every raw artifact for future lineage tracking."""

    ingestion_id: str
    session_id: str
    customer_id: str
    device_id: str | None = None
    source_type: str | None = None
    notes: str | None = None
    original_filename: str
    content_type: str | None = None
    size_bytes: int
    sha256: str
    ingested_at: datetime
    storage_uri: str
    pipeline_stage: str = "raw"


class HealthResponse(BaseModel):
    status: str = "ok"


def resolve_customer_id(customer_id: str | None) -> str:
    if customer_id is None or not customer_id.strip():
        return _ANONYMOUS_CUSTOMER
    return customer_id.strip()
