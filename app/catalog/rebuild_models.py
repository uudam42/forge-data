"""Request/response models for the v2.5 selective-rebuild planner/executor."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RebuildReplacement(BaseModel):
    old_type: str
    old_id: str
    new_type: str
    new_id: str


class RebuildPlanRequest(BaseModel):
    replace: RebuildReplacement


class PlanStepParent(BaseModel):
    artifact_type: str
    original_id: str
    effective_id: str | None = Field(
        default=None, description="The id this step will actually use as this parent. Same as original_id when reused unchanged; null when replaced (not known until execution)."
    )
    relationship: str
    replaced: bool


class RebuildPlanStep(BaseModel):
    stage_artifact_type: str
    old_artifact_id: str
    new_artifact_id: str | None = None
    parents: list[PlanStepParent]
    feasible: bool
    manual_configuration_required: bool
    infeasible_reason: str | None = None


class RebuildPlanResponse(BaseModel):
    plan_id: str
    fingerprint: str
    replace: RebuildReplacement
    steps: list[RebuildPlanStep]
    created_at: str


class RebuildExecuteRequest(BaseModel):
    plan_id: str
    # Keyed by "<stage_artifact_type>/<old_artifact_id>" -> the raw config
    # dict for that stage's request. Required only for steps the plan
    # flagged manual_configuration_required=true (Design Requirement 17).
    configs: dict[str, dict] = Field(default_factory=dict)


class RebuildStepResult(BaseModel):
    stage_artifact_type: str
    old_artifact_id: str
    new_artifact_id: str | None = None
    status: str = Field(description="rebuilt | skipped_manual_configuration_required | skipped_upstream_not_rebuilt | failed")
    detail: str | None = None


class SupersessionRecord(BaseModel):
    old_type: str
    old_id: str
    new_type: str
    new_id: str


class RebuildExecuteResponse(BaseModel):
    plan_id: str
    replace: RebuildReplacement
    results: list[RebuildStepResult]
    superseded: list[SupersessionRecord]
