"""Request/response models for the v2.5 data-governance API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class GovernanceActionRequest(BaseModel):
    """Body for the explicit /deprecate, /invalidate, /reactivate
    endpoints — the target state is implied by which endpoint was
    called, so this only ever carries the reason/actor/supersession."""

    reason: str
    actor: str | None = Field(default=None, description="Optional free-text identifier of who/what made this call — never invented if absent")
    superseded_by_type: str | None = None
    superseded_by_id: str | None = None


class ArtifactGovernanceResponse(BaseModel):
    artifact_type: str
    artifact_id: str
    state: str
    reason: str | None = None
    actor: str | None = None
    superseded_by_type: str | None = None
    superseded_by_id: str | None = None
    updated_at: str | None = None


class GovernanceEvent(BaseModel):
    event_id: int
    previous_state: str
    new_state: str
    reason: str
    actor: str | None = None
    superseded_by_type: str | None = None
    superseded_by_id: str | None = None
    created_at: str


class ArtifactGovernanceHistoryResponse(BaseModel):
    artifact_type: str
    artifact_id: str
    current: ArtifactGovernanceResponse
    events: list[GovernanceEvent]


class AncestorFlagResponse(BaseModel):
    artifact_type: str
    artifact_id: str
    reason: str


class GovernanceChainResponse(BaseModel):
    artifact_type: str
    artifact_id: str
    direct_state: str
    direct_reason: str | None = None
    invalid_ancestors: list[AncestorFlagResponse] = Field(default_factory=list)
    deprecated_ancestors: list[AncestorFlagResponse] = Field(default_factory=list)


class DatasetVersionGovernanceActionRequest(BaseModel):
    reason: str
    actor: str | None = None


class DatasetVersionGovernanceResponse(BaseModel):
    dataset_name: str
    version: str
    state: str
    reason: str | None = None
    actor: str | None = None
    updated_at: str | None = None


class DatasetVersionGovernanceEvent(BaseModel):
    event_id: int
    previous_state: str
    new_state: str
    reason: str
    actor: str | None = None
    created_at: str


class DatasetVersionGovernanceHistoryResponse(BaseModel):
    dataset_name: str
    version: str
    current: DatasetVersionGovernanceResponse
    events: list[DatasetVersionGovernanceEvent]


# ---------------------------------------------------------------------------
# Enriched impact (Design Requirement 9)
# ---------------------------------------------------------------------------


class AffectedDatasetVersion(BaseModel):
    dataset_name: str
    version: str
    package_id: str
    effective_status: str
    reason: str | None = None
    reason_source: str | None = None


class EnrichedImpactResponse(BaseModel):
    artifact_type: str
    artifact_id: str
    source_governance_state: str
    affected_artifacts: dict[str, int]
    affected_packages: list[str] = Field(default_factory=list)
    affected_dataset_versions: list[AffectedDatasetVersion] = Field(default_factory=list)
    descendant_governance_counts: dict[str, int] = Field(default_factory=dict)
