"""Selective rebuild execution (v2.5).

Reuses the existing per-stage service layer DIRECTLY -- never through
HTTP, never duplicating stage logic (Design Requirement 18). No
background queue, no scheduler, no retry engine: executes exactly the
plan's steps, once, synchronously, in this process.

Every stage service this calls already has v2.1's atomic staging/commit
guarantee built in, so a crash mid-step can never leave a partial
artifact for that step — that guarantee is inherited for free, not
reimplemented here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.catalog.errors import RebuildConfigUnavailableError
from app.catalog.rebuild_planner import PlanStep, RebuildPlan
from app.catalog.rebuild_models import RebuildStepResult, SupersessionRecord
from app.core.config import Settings
from app.storage.base import RawStorage
from app.storage.cleaned_store import LocalCleanedArtifactStore
from app.storage.local import LocalRawStorage
from app.storage.normalized_store import LocalNormalizedArtifactStore
from app.storage.package_store import LocalDatasetPackageStore
from app.storage.qc_store import LocalQCReportStore
from app.storage.synchronization_store import LocalSynchronizationArtifactStore
from app.storage.transformed_store import LocalTransformedArtifactStore
from app.validation.schemas.registry import SchemaRegistry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SelectiveRebuildExecutor:
    """Stateless aside from the repo/settings handed in at construction —
    builds each stage service fresh per call, exactly like each route's
    own `get_X_service` dependency does."""

    def __init__(self, *, repo, settings: Settings) -> None:
        self._repo = repo
        self._settings = settings

    def execute(self, plan: RebuildPlan, *, configs: dict[str, dict]) -> tuple[list[RebuildStepResult], list[SupersessionRecord]]:
        resolved: dict[tuple[str, str], str] = {(plan.old_type, plan.old_id): plan.new_id}
        # Any step that was skipped or failed -- every step that
        # transitively depends on it must ALSO be skipped rather than
        # attempted against a parent that was never produced (which
        # would otherwise surface as a confusing KeyError/failure deep in
        # per-stage reconstruction instead of the honest "an earlier
        # required step never ran").
        unavailable: set[tuple[str, str]] = set()
        results: list[RebuildStepResult] = []
        superseded: list[SupersessionRecord] = []

        for step in plan.steps:
            step_key = (step.stage_artifact_type, step.old_artifact_id)
            blocking_parent = next(
                (p for p in step.parents if p.replaced and (p.artifact_type, p.original_id) in unavailable), None
            )
            if blocking_parent is not None:
                unavailable.add(step_key)
                results.append(
                    RebuildStepResult(
                        stage_artifact_type=step.stage_artifact_type,
                        old_artifact_id=step.old_artifact_id,
                        new_artifact_id=None,
                        status="skipped_upstream_not_rebuilt",
                        detail=f"parent {blocking_parent.artifact_type}/{blocking_parent.original_id} was never rebuilt",
                    )
                )
                continue

            config_key = f"{step.stage_artifact_type}/{step.old_artifact_id}"
            try:
                if step.manual_configuration_required and config_key not in configs:
                    unavailable.add(step_key)
                    results.append(
                        RebuildStepResult(
                            stage_artifact_type=step.stage_artifact_type,
                            old_artifact_id=step.old_artifact_id,
                            new_artifact_id=None,
                            status="skipped_manual_configuration_required",
                            detail=step.infeasible_reason,
                        )
                    )
                    continue

                new_id = self._execute_step(step, resolved=resolved, raw_config=configs.get(config_key))
                resolved[step_key] = new_id
                results.append(
                    RebuildStepResult(
                        stage_artifact_type=step.stage_artifact_type,
                        old_artifact_id=step.old_artifact_id,
                        new_artifact_id=new_id,
                        status="rebuilt",
                    )
                )
                self._mark_superseded(step.stage_artifact_type, step.old_artifact_id, new_id)
                superseded.append(
                    SupersessionRecord(
                        old_type=step.stage_artifact_type, old_id=step.old_artifact_id,
                        new_type=step.stage_artifact_type, new_id=new_id,
                    )
                )
            except Exception as exc:
                unavailable.add(step_key)
                results.append(
                    RebuildStepResult(
                        stage_artifact_type=step.stage_artifact_type,
                        old_artifact_id=step.old_artifact_id,
                        new_artifact_id=None,
                        status="failed",
                        detail=str(exc),
                    )
                )
                # Keep going rather than stopping outright: any step that
                # actually depends on this one is excluded correctly by
                # the `unavailable` cascade at the top of the loop, so
                # continuing here never attempts a step against a parent
                # that was never produced -- it only still attempts a
                # step that turns out not to depend on this failure at
                # all (not possible in a single-root plan today, but the
                # cascade check makes it safe even if that ever changes).
                continue

        return results, superseded

    def _resolve_parent_id(self, parent, resolved: dict[tuple[str, str], str]) -> str:
        if not parent.replaced:
            return parent.original_id
        return resolved[(parent.artifact_type, parent.original_id)]

    def _mark_superseded(self, artifact_type: str, artifact_id: str, new_id: str) -> None:
        current = self._repo.get_artifact_governance(artifact_type, artifact_id)
        previous_state = current["state"] if current else "active"
        with self._repo.transaction(operation="mark_superseded"):
            self._repo.set_artifact_governance(
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                previous_state=previous_state,
                new_state="deprecated",
                reason=f"superseded by {artifact_type}/{new_id} via selective rebuild",
                actor=None,
                superseded_by_type=artifact_type,
                superseded_by_id=new_id,
                updated_at=_now(),
            )

    # ------------------------------------------------------------------
    # Per-stage reconstruction and execution
    # ------------------------------------------------------------------

    def _old_manifest(self, artifact_type: str, artifact_id: str) -> dict:
        row = self._repo.get_artifact(artifact_type, artifact_id)
        return json.loads(row["metadata_json"]) if row and row.get("metadata_json") else {}

    def _execute_step(self, step: PlanStep, *, resolved: dict[tuple[str, str], str], raw_config: dict | None) -> str:
        if step.stage_artifact_type == "synchronization":
            return self._execute_synchronization(step, resolved=resolved)
        if step.stage_artifact_type == "cleaning":
            return self._execute_cleaning(step, resolved=resolved, raw_config=raw_config)
        if step.stage_artifact_type == "transformation":
            return self._execute_transformation(step, resolved=resolved, raw_config=raw_config)
        if step.stage_artifact_type == "qc":
            return self._execute_qc(step, resolved=resolved, raw_config=raw_config)
        if step.stage_artifact_type == "package":
            return self._execute_packaging(step, resolved=resolved, raw_config=raw_config)
        raise RebuildConfigUnavailableError(
            artifact_type=step.stage_artifact_type, artifact_id=step.old_artifact_id,
            reason=f"no rebuild executor is defined for stage '{step.stage_artifact_type}'",
        )

    def _execute_synchronization(self, step: PlanStep, *, resolved: dict[tuple[str, str], str]) -> str:
        from app.storage.integrity_store import IntegrityReportStore  # noqa: F401 (documents dep chain; not used directly)
        from app.synchronization.models import AlignmentConfig, ClockCorrectionConfig, ReferenceConfig, StreamRequest, SynchronizationRequest
        from app.synchronization.registry import AlignmentStrategyRegistry
        from app.synchronization.service import SynchronizationService

        manifest = self._old_manifest("synchronization", step.old_artifact_id)
        parent_by_original_id = {p.original_id: p for p in step.parents if p.artifact_type == "normalization"}

        streams = []
        for s in manifest["streams"]:
            parent = parent_by_original_id.get(s["normalization_id"])
            normalization_id = self._resolve_parent_id(parent, resolved) if parent else s["normalization_id"]
            streams.append(StreamRequest(name=s["name"], normalization_id=normalization_id))

        request = SynchronizationRequest(
            streams=streams,
            reference=ReferenceConfig(**manifest["reference"]),
            alignment=AlignmentConfig(**manifest["alignment_config"]),
            clock_corrections={k: ClockCorrectionConfig(**v) for k, v in (manifest.get("clock_corrections") or {}).items()},
        )

        service = SynchronizationService(
            raw_storage=LocalRawStorage(root=self._settings.RAW_STORAGE_ROOT),
            normalized_store=LocalNormalizedArtifactStore(root=self._settings.NORMALIZED_STORAGE_ROOT),
            schema_registry=SchemaRegistry(schema_dir=self._settings.SCHEMA_DIR),
            strategy_registry=AlignmentStrategyRegistry(),
            artifact_store=LocalSynchronizationArtifactStore(root=self._settings.SYNCHRONIZED_STORAGE_ROOT),
            settings=self._settings,
        )
        response = service.synchronize(request)
        return response.synchronization_id

    def _require_config(self, step: PlanStep, raw_config: dict | None) -> dict:
        if raw_config is None:
            raise RebuildConfigUnavailableError(
                artifact_type=step.stage_artifact_type, artifact_id=step.old_artifact_id,
                reason="no config override was supplied for a manual_configuration_required step",
            )
        return raw_config

    def _execute_cleaning(self, step: PlanStep, *, resolved: dict[tuple[str, str], str], raw_config: dict | None) -> str:
        from app.cleaning.models import CleaningConfig, CleaningRequest
        from app.cleaning.registry import CleaningPolicyRegistry
        from app.cleaning.service import CleaningService

        manifest = self._old_manifest("cleaning", step.old_artifact_id)
        sync_parent = next(p for p in step.parents if p.artifact_type == "synchronization")
        synchronization_id = self._resolve_parent_id(sync_parent, resolved)
        config_dict = self._require_config(step, raw_config)

        service = CleaningService(
            settings=self._settings,
            sync_store=LocalSynchronizationArtifactStore(root=self._settings.SYNCHRONIZED_STORAGE_ROOT),
            policy_registry=CleaningPolicyRegistry(),
            cleaned_store=LocalCleanedArtifactStore(root=self._settings.CLEANED_STORAGE_ROOT),
        )
        request = CleaningRequest(
            policy_name=manifest["policy"]["name"], policy_version=manifest["policy"]["version"],
            config=CleaningConfig(**config_dict),
        )
        response = service.clean(synchronization_id=synchronization_id, request=request)
        return response.cleaning_id

    def _execute_transformation(self, step: PlanStep, *, resolved: dict[tuple[str, str], str], raw_config: dict | None) -> str:
        from app.transformation.models import TransformationConfig, TransformationRequest
        from app.transformation.registry import TransformationProfileRegistry
        from app.transformation.service import TransformationService

        manifest = self._old_manifest("transformation", step.old_artifact_id)
        cleaning_parent = next(p for p in step.parents if p.artifact_type == "cleaning")
        cleaning_id = self._resolve_parent_id(cleaning_parent, resolved)
        config_dict = self._require_config(step, raw_config)

        service = TransformationService(
            settings=self._settings,
            cleaned_store=LocalCleanedArtifactStore(root=self._settings.CLEANED_STORAGE_ROOT),
            profile_registry=TransformationProfileRegistry(),
            transformed_store=LocalTransformedArtifactStore(root=self._settings.TRANSFORMED_STORAGE_ROOT),
        )
        request = TransformationRequest(
            profile_name=manifest["profile"]["name"], profile_version=manifest["profile"]["version"],
            config=TransformationConfig(**config_dict),
        )
        response = service.transform(cleaning_id=cleaning_id, request=request)
        return response.transformation_id

    def _execute_qc(self, step: PlanStep, *, resolved: dict[tuple[str, str], str], raw_config: dict | None) -> str:
        from app.qc.models import QCConfig, QCRequest
        from app.qc.registry import QCProfileRegistry
        from app.qc.service import QCService

        manifest = self._old_manifest("qc", step.old_artifact_id)
        transformation_parent = next(p for p in step.parents if p.artifact_type == "transformation")
        transformation_id = self._resolve_parent_id(transformation_parent, resolved)
        config_dict = self._require_config(step, raw_config)

        service = QCService(
            settings=self._settings,
            transformed_store=LocalTransformedArtifactStore(root=self._settings.TRANSFORMED_STORAGE_ROOT),
            profile_registry=QCProfileRegistry(),
            qc_store=LocalQCReportStore(root=self._settings.QC_STORAGE_ROOT),
        )
        request = QCRequest(
            profile_name=manifest["profile"]["name"], profile_version=manifest["profile"]["version"],
            config=QCConfig(**config_dict),
        )
        response = service.run_qc(transformation_id=transformation_id, request=request)
        return response.qc_id

    def _execute_packaging(self, step: PlanStep, *, resolved: dict[tuple[str, str], str], raw_config: dict | None) -> str:
        from app.packaging.models import PackagingConfig, PackagingRequest
        from app.packaging.registry import PackagingProfileRegistry
        from app.packaging.service import PackagingService

        manifest = self._old_manifest("package", step.old_artifact_id)
        transformation_parent = next(p for p in step.parents if p.artifact_type == "transformation")
        qc_parent = next(p for p in step.parents if p.artifact_type == "qc")
        transformation_id = self._resolve_parent_id(transformation_parent, resolved)
        qc_id = self._resolve_parent_id(qc_parent, resolved)
        config_dict = self._require_config(step, raw_config)

        service = PackagingService(
            settings=self._settings,
            transformed_store=LocalTransformedArtifactStore(root=self._settings.TRANSFORMED_STORAGE_ROOT),
            qc_store=LocalQCReportStore(root=self._settings.QC_STORAGE_ROOT),
            profile_registry=PackagingProfileRegistry(),
            package_store=LocalDatasetPackageStore(root=self._settings.PACKAGE_STORAGE_ROOT),
        )
        # Never auto-registers a dataset version (Design Requirement 21) --
        # dataset_name/dataset_version stay unset; the caller registers
        # the corrected version explicitly via the normal datasets API.
        request = PackagingRequest(
            qc_id=qc_id, profile_name=manifest["profile"]["name"], profile_version=manifest["profile"]["version"],
            config=PackagingConfig(**config_dict),
        )
        response = service.package(transformation_id=transformation_id, request=request)
        return response.package_id
