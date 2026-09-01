"""Request/response models for pipeline runs (v2.6).

The pipeline request reuses each downstream stage's OWN config models
directly (Design Requirement 8) rather than inventing a second parallel
config schema. Per-stream schema/profile identity is never hardcoded
here -- it's resolved from the v2.3 SensorPluginRegistry by sensor_type
at execution time (Design Requirement 9), so adding a new sensor plugin
never requires touching this module.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.cleaning.models import CleaningConfig
from app.packaging.models import PackagingConfig
from app.qc.models import QCConfig
from app.synchronization.models import AlignmentConfig, ClockCorrectionConfig, ReferenceConfig
from app.transformation.models import TransformationConfig


class PipelineStreamConfig(BaseModel):
    sensor_type: str = Field(description="Resolved via the sensor plugin registry -- e.g. 'imu', 'gps', 'force_torque'")
    source_units: dict[str, str] = Field(default_factory=dict)


class PipelineSynchronizationConfig(BaseModel):
    reference: ReferenceConfig = Field(default_factory=ReferenceConfig)
    alignment: AlignmentConfig = Field(default_factory=AlignmentConfig)
    clock_corrections: dict[str, ClockCorrectionConfig] = Field(default_factory=dict)


class PipelineCleaningConfig(BaseModel):
    policy_name: str
    policy_version: str = "1.0.0"
    config: CleaningConfig = Field(default_factory=CleaningConfig)


class PipelineTransformationConfig(BaseModel):
    profile_name: str
    profile_version: str = "1.0.0"
    config: TransformationConfig = Field(default_factory=TransformationConfig)


class PipelineQCConfig(BaseModel):
    profile_name: str
    profile_version: str = "1.0.0"
    config: QCConfig = Field(default_factory=QCConfig)


class PipelinePackagingConfig(BaseModel):
    profile_name: str
    profile_version: str = "1.0.0"
    config: PackagingConfig
    dataset_name: str | None = None
    dataset_version: str | None = None
    description: str | None = None


class PipelineRunRequest(BaseModel):
    """The JSON `config` form field of `POST /api/v1/runs` (files are
    supplied separately as multipart uploads, one per stream, matched by
    list order -- see Design Requirement 46: file bytes never enter run
    metadata, only this config does)."""

    session_id: str | None = None
    customer_id: str | None = None
    device_id: str | None = None
    streams: list[PipelineStreamConfig]
    synchronization: PipelineSynchronizationConfig = Field(default_factory=PipelineSynchronizationConfig)
    cleaning: PipelineCleaningConfig
    transformation: PipelineTransformationConfig
    qc: PipelineQCConfig
    packaging: PipelinePackagingConfig


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class StageRunResponse(BaseModel):
    stage_run_id: str
    run_id: str
    stage: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    records_total: int | None = None
    records_processed: int | None = None
    bytes_total: int | None = None
    bytes_processed: int | None = None
    progress_fraction: float | None = Field(
        default=None, description="records_processed / records_total when both are known; null (never fabricated) otherwise"
    )
    artifacts_created: int = 0
    error_code: str | None = None
    error_message: str | None = None


class RunArtifactResponse(BaseModel):
    stage: str
    artifact_type: str
    artifact_id: str
    created_at: str


class RunEventResponse(BaseModel):
    event_id: int
    event_type: str
    detail: str | None = None
    created_at: str


class PipelineRunSummary(BaseModel):
    """Cheap-to-construct shape for GET /runs (list) — no per-run stage/
    artifact sub-queries, so listing/pagination stays O(1) per row."""

    run_id: str
    run_type: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    current_stage: str | None = None
    stages_total: int
    stages_completed: int
    error_code: str | None = None


class PipelineRunResponse(BaseModel):
    run_id: str
    run_type: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    current_stage: str | None = None
    config_hash: str
    retry_of_run_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    stages_total: int
    stages_completed: int
    stage_runs: list[StageRunResponse] = Field(default_factory=list)
    artifacts: list[RunArtifactResponse] = Field(default_factory=list)


class RunListResponse(BaseModel):
    runs: list[PipelineRunSummary]
    limit: int
    offset: int


class CancelRunRequest(BaseModel):
    reason: str | None = None
