"""CatalogService: orchestrates the repository, scanner, graph, verifier,
and versioning modules. Every catalog API route is a thin wrapper around
one of these methods.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone

from app.catalog import governance, graph, versioning
from app.catalog.errors import (
    ArtifactNotFoundError,
    CatalogBusyError,
    CatalogLockFailedError,
    CatalogRebuildFailedError,
    CatalogRebuildInProgressError,
    CatalogScanFailedError,
    DatasetAlreadyExistsError,
    DatasetNotFoundError,
    DatasetVersionImmutableError,
    DatasetVersionNotFoundError,
    GovernanceTargetNotFoundError,
    InvalidArtifactTypeError,
    PackageNotAcceptedError,
    PackageNotFoundError,
    RebuildPlanNotFoundError,
    RebuildPlanStaleError,
    SelectiveRebuildInProgressError,
)
from app.catalog.governance_models import (
    AffectedDatasetVersion,
    AncestorFlagResponse,
    ArtifactGovernanceHistoryResponse,
    ArtifactGovernanceResponse,
    DatasetVersionGovernanceEvent,
    DatasetVersionGovernanceHistoryResponse,
    DatasetVersionGovernanceResponse,
    EnrichedImpactResponse,
    GovernanceChainResponse,
    GovernanceEvent,
)
from app.catalog.models import (
    ACCEPTED_QC_STATUSES,
    ARTIFACT_TYPES,
    ArtifactDetail,
    ArtifactRef,
    ArtifactSummary,
    CatalogHealth,
    DatasetResponse,
    DatasetVersionResponse,
    HealthIssue,
    ImpactResponse,
    LineageEdge,
    LineageGraphResponse,
    LineageNode,
    RebuildResult,
    ReproducibilityResponse,
    ScanIssue,
    ScanResult,
    VerificationCheck,
    VerificationNodeResult,
    VerificationResponse,
)
from app.catalog.rebuild_lock import RebuildLock
from app.catalog.rebuild_models import (
    PlanStepParent,
    RebuildExecuteResponse,
    RebuildPlanResponse,
    RebuildPlanStep,
    RebuildReplacement,
)
from app.catalog.rebuild_planner import SelectiveRebuildPlanner
from app.catalog.rebuild_plan_store import discard_plan, get_plan, store_plan
from app.catalog.repository import CatalogRepository
from app.catalog.scanner import BrokenLineageError, CatalogScanner
from app.catalog.serialization import canonical_json, compute_lineage_fingerprint
from app.catalog.verifier import ArtifactVerifier
from app.core.config import Settings
from app.core.logging import get_logger
from app.storage.catalog_store import CATALOG_SCHEMA_VERSION

logger = get_logger("app.catalog")

_STATUS_KEY = "last_scan_at"


class CatalogService:
    def __init__(
        self,
        *,
        repo: CatalogRepository,
        scanner: CatalogScanner,
        verifier: ArtifactVerifier,
        rebuild_lock: RebuildLock | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._repo = repo
        self._scanner = scanner
        self._verifier = verifier
        # Optional so tests exercising create_dataset/register_version
        # (which never touch rebuild()) don't need to construct one. The
        # real HTTP dependency (app.api.routes.catalog.get_catalog_service)
        # always passes a real RebuildLock -- see Design Requirement 7.
        self._rebuild_lock_obj = rebuild_lock
        # Only needed by execute_rebuild() (v2.5), which must construct
        # real pipeline-stage services (Design Requirement 18) -- optional
        # for every test/route that never calls it.
        self._settings = settings

    def _rebuild_lock(self):
        if self._rebuild_lock_obj is None:
            return contextlib.nullcontext()
        return self._rebuild_lock_obj.acquire()

    # ------------------------------------------------------------------
    # Scan / rebuild
    # ------------------------------------------------------------------

    def scan(self) -> ScanResult:
        logger.info("CATALOG_SCAN_STARTED")
        try:
            with self._repo.transaction(operation="scan"):
                outcome = self._scanner.scan(self._repo, strict=False)
                self._repo.set_metadata(_STATUS_KEY, datetime.now(timezone.utc).isoformat())
        except CatalogBusyError:
            # Structured contention error, not a scan failure -- the
            # catalog and filesystem are both untouched; let the caller
            # see CATALOG_BUSY (with operation/timeout_ms/db_path) and
            # decide whether to retry, rather than burying it in a
            # generic CatalogScanFailedError.
            logger.warning("CATALOG_SCAN_BUSY")
            raise
        except Exception:
            logger.error("CATALOG_SCAN_FAILED")
            raise CatalogScanFailedError("Catalog scan failed") from None

        logger.info(
            "CATALOG_SCAN_COMPLETED inserted=%d unchanged=%d edges_inserted=%d issues=%d",
            outcome.inserted,
            outcome.unchanged,
            outcome.edges_inserted,
            len(outcome.issues),
        )
        return ScanResult(
            artifacts_registered=outcome.inserted,
            artifacts_updated=outcome.unchanged,
            edges_registered=outcome.edges_inserted,
            issues=[ScanIssue(**issue) for issue in outcome.issues],
        )

    def rebuild(self) -> RebuildResult:
        """Reconstructs the artifact index + lineage edges from the
        filesystem, strictly (broken lineage aborts the whole rebuild,
        leaving the prior catalog state intact). `datasets` and
        `dataset_versions` are NEVER touched — they are user-registered
        metadata, not reconstructible from stage manifests. See
        README "Dataset/version preservation across rebuild"."""
        logger.info("CATALOG_REBUILD_STARTED")
        datasets_before = self._repo.count_datasets()
        versions_before = self._repo.count_dataset_versions()
        # v2.5 -- governance is user-owned catalog state, exactly like
        # datasets/dataset_versions, and clear_artifact_index() never
        # touches these tables. Asserted explicitly (not just "happens
        # to be true") per Design Requirement 25.
        artifact_governance_before = len(self._repo.list_all_artifact_governance())
        version_governance_before = len(self._repo.list_all_dataset_version_governance())
        try:
            with self._rebuild_lock():
                with self._repo.transaction(operation="rebuild"):
                    self._repo.clear_artifact_index()
                    outcome = self._scanner.scan(self._repo, strict=True)
                    self._repo.set_metadata(_STATUS_KEY, datetime.now(timezone.utc).isoformat())
        except (CatalogBusyError, CatalogRebuildInProgressError, CatalogLockFailedError):
            # Structured contention/locking errors, not rebuild failures --
            # the catalog is untouched in every one of these cases (the
            # lock, or the write transaction, was never acquired). Let the
            # caller see the specific structured error rather than a
            # generic CatalogRebuildFailedError.
            logger.warning("CATALOG_REBUILD_BUSY_OR_LOCKED")
            raise
        except BrokenLineageError as exc:
            logger.error("CATALOG_REBUILD_FAILED reason=broken_lineage")
            raise CatalogRebuildFailedError(f"Strict rebuild aborted: {exc}") from exc
        except Exception:
            logger.error("CATALOG_REBUILD_FAILED")
            raise CatalogRebuildFailedError("Catalog rebuild failed") from None

        datasets_after = self._repo.count_datasets()
        versions_after = self._repo.count_dataset_versions()
        logger.info(
            "CATALOG_REBUILD_COMPLETED artifacts=%d edges=%d datasets_preserved=%d versions_preserved=%d",
            outcome.inserted,
            outcome.edges_inserted,
            datasets_after,
            versions_after,
        )
        assert datasets_after == datasets_before and versions_after == versions_before, (
            "rebuild must never alter dataset/version tables"
        )
        artifact_governance_after = len(self._repo.list_all_artifact_governance())
        version_governance_after = len(self._repo.list_all_dataset_version_governance())
        assert artifact_governance_after == artifact_governance_before, "rebuild must never alter artifact governance"
        assert version_governance_after == version_governance_before, "rebuild must never alter dataset-version governance"
        return RebuildResult(
            artifacts_registered=outcome.inserted,
            edges_registered=outcome.edges_inserted,
            issues=[ScanIssue(**issue) for issue in outcome.issues],
            datasets_preserved=datasets_after,
            dataset_versions_preserved=versions_after,
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> CatalogHealth:
        artifacts = self._repo.list_artifacts()
        orphan_count = 0
        for artifact in artifacts:
            if artifact["artifact_type"] == "ingestion":
                continue  # ingestion is the DAG root — having no parent is expected
            if not self._repo.get_parents(artifact["artifact_type"], artifact["artifact_id"]):
                orphan_count += 1

        issues: list[HealthIssue] = []
        for issue in self._repo.list_issues():
            issues.append(HealthIssue(code=issue["issue_code"], detail=issue["detail"]))

        broken_version_refs = 0
        for version in self._repo.list_all_dataset_versions():
            if self._repo.get_artifact("package", version["package_id"]) is None:
                broken_version_refs += 1
                issues.append(
                    HealthIssue(
                        code="BROKEN_DATASET_VERSION_REFERENCE",
                        detail=f"{version['dataset_name']}@{version['version']} references missing package/{version['package_id']}",
                    )
                )

        # v2.5 -- a governance row surviving a rebuild while its target
        # artifact did NOT (e.g. the manifest vanished from disk) is a
        # dangling reference worth surfacing -- but this is a distinct
        # concern from "an artifact is invalid" (that's an intentional,
        # healthy state, never itself a health issue). Governance history
        # is preserved regardless; see Design Requirement 26/27.
        broken_governance_refs = 0
        for gov in self._repo.list_all_artifact_governance():
            if self._repo.get_artifact(gov["artifact_type"], gov["artifact_id"]) is None:
                broken_governance_refs += 1
                issues.append(
                    HealthIssue(
                        code="BROKEN_GOVERNANCE_REFERENCE",
                        detail=f"governance state {gov['state']!r} recorded for {gov['artifact_type']}/{gov['artifact_id']}, "
                        f"which is no longer in the artifact index",
                    )
                )

        schema_version = self._repo.get_metadata("catalog_schema_version") or "unknown"
        if schema_version != CATALOG_SCHEMA_VERSION:
            issues.append(
                HealthIssue(
                    code="CATALOG_SCHEMA_MISMATCH",
                    detail=f"catalog.db reports schema version {schema_version!r}, code expects {CATALOG_SCHEMA_VERSION!r}",
                )
            )

        return CatalogHealth(
            status="healthy" if not issues else "degraded",
            artifacts=len(artifacts),
            edges=self._repo.count_edges(),
            datasets=self._repo.count_datasets(),
            versions=self._repo.count_dataset_versions(),
            orphan_artifacts=orphan_count,
            missing_parent_references=sum(1 for i in self._repo.list_issues() if i["issue_code"] == "MISSING_LINEAGE_PARENT"),
            cycle_count=0,  # enforced at edge-insertion time — see graph.would_create_cycle
            catalog_schema_version=schema_version,
            last_scan_at=self._repo.get_metadata(_STATUS_KEY),
            issues=issues,
        )

    # ------------------------------------------------------------------
    # Artifact lookup
    # ------------------------------------------------------------------

    def _require_valid_type(self, artifact_type: str) -> None:
        if artifact_type not in ARTIFACT_TYPES:
            raise InvalidArtifactTypeError(f"Unknown artifact_type '{artifact_type}'")

    def get_artifact(self, artifact_type: str, artifact_id: str) -> ArtifactDetail:
        self._require_valid_type(artifact_type)
        artifact = self._repo.get_artifact(artifact_type, artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError(f"No {artifact_type} artifact with id '{artifact_id}'")
        parents = [ArtifactRef(artifact_type=e["parent_artifact_type"], artifact_id=e["parent_artifact_id"]) for e in self._repo.get_parents(artifact_type, artifact_id)]
        children = [ArtifactRef(artifact_type=e["child_artifact_type"], artifact_id=e["child_artifact_id"]) for e in self._repo.get_children(artifact_type, artifact_id)]
        return ArtifactDetail(
            artifact=_to_summary(artifact),
            metadata=json.loads(artifact["metadata_json"]),
            parents=parents,
            children=children,
        )

    def list_artifacts(
        self, *, artifact_type: str | None = None, status: str | None = None, session_id: str | None = None
    ) -> list[ArtifactSummary]:
        if artifact_type is not None:
            self._require_valid_type(artifact_type)
        rows = self._repo.list_artifacts(artifact_type=artifact_type, status=status, session_id=session_id)
        return [_to_summary(r) for r in rows]

    # ------------------------------------------------------------------
    # Lineage
    # ------------------------------------------------------------------

    def lineage(
        self, artifact_type: str, artifact_id: str, *, direction: str = "both", max_depth: int | None = None
    ) -> LineageGraphResponse:
        self._require_valid_type(artifact_type)
        if self._repo.get_artifact(artifact_type, artifact_id) is None:
            raise ArtifactNotFoundError(f"No {artifact_type} artifact with id '{artifact_id}'")
        nodes, edges = graph.traverse(
            self._repo, root_type=artifact_type, root_id=artifact_id, direction=direction, max_depth=max_depth
        )
        return LineageGraphResponse(
            root=ArtifactRef(artifact_type=artifact_type, artifact_id=artifact_id),
            direction=direction,
            nodes=[LineageNode(artifact_type=n["artifact_type"], artifact_id=n["artifact_id"], pipeline_stage=n["pipeline_stage"], status=n.get("status")) for n in nodes],
            edges=[LineageEdge(parent=ArtifactRef(artifact_type=e[0], artifact_id=e[1]), child=ArtifactRef(artifact_type=e[2], artifact_id=e[3]), relationship=e[4]) for e in edges],
        )

    def impact(self, artifact_type: str, artifact_id: str) -> ImpactResponse:
        self._require_valid_type(artifact_type)
        if self._repo.get_artifact(artifact_type, artifact_id) is None:
            raise ArtifactNotFoundError(f"No {artifact_type} artifact with id '{artifact_id}'")
        affected = graph.impact_analysis(self._repo, artifact_type=artifact_type, artifact_id=artifact_id)
        return ImpactResponse(artifact_type=artifact_type, artifact_id=artifact_id, affected=affected)

    def enriched_impact(self, artifact_type: str, artifact_id: str) -> EnrichedImpactResponse:
        """Design Requirement 9: everything impact() already returns, plus
        the artifact's own governance state, every affected package,
        and — for every downstream dataset version — its computed
        EFFECTIVE status (never mutating the version's own explicit
        governance to get there; see app.catalog.governance)."""
        self._require_valid_type(artifact_type)
        if self._repo.get_artifact(artifact_type, artifact_id) is None:
            raise ArtifactNotFoundError(f"No {artifact_type} artifact with id '{artifact_id}'")

        affected_counts = graph.impact_analysis(self._repo, artifact_type=artifact_type, artifact_id=artifact_id)
        affected_dataset_version_count = affected_counts.pop("dataset_versions", 0)

        nodes, _ = graph.traverse(self._repo, root_type=artifact_type, root_id=artifact_id, direction="downstream")
        package_ids: list[str] = []
        governance_counts: dict[str, int] = {}
        for node in nodes:
            if node["artifact_type"] == artifact_type and node["artifact_id"] == artifact_id:
                continue
            if node["artifact_type"] == "package":
                package_ids.append(node["artifact_id"])
            gov = self._repo.get_artifact_governance(node["artifact_type"], node["artifact_id"])
            state = gov["state"] if gov else governance.GovernanceState.ACTIVE.value
            governance_counts[state] = governance_counts.get(state, 0) + 1

        version_rows = self._repo.list_dataset_versions_for_packages(package_ids)
        affected_versions: list[AffectedDatasetVersion] = []
        for row in version_rows:
            result = governance.compute_effective_version_status(
                self._repo, dataset_name=row["dataset_name"], version=row["version"], package_id=row["package_id"]
            )
            affected_versions.append(
                AffectedDatasetVersion(
                    dataset_name=row["dataset_name"], version=row["version"], package_id=row["package_id"],
                    effective_status=result.status, reason=result.reason, reason_source=result.source,
                )
            )
        affected_versions.sort(key=lambda v: (v.dataset_name, v.version))
        assert len(affected_versions) == affected_dataset_version_count, "enriched impact must agree with impact() on version count"

        source_gov = self._repo.get_artifact_governance(artifact_type, artifact_id)
        source_state = source_gov["state"] if source_gov else governance.GovernanceState.ACTIVE.value

        return EnrichedImpactResponse(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            source_governance_state=source_state,
            affected_artifacts=affected_counts,
            affected_packages=sorted(set(package_ids)),
            affected_dataset_versions=affected_versions,
            descendant_governance_counts=governance_counts,
        )

    # ------------------------------------------------------------------
    # Artifact governance (v2.5)
    # ------------------------------------------------------------------

    def get_artifact_governance(self, artifact_type: str, artifact_id: str) -> ArtifactGovernanceResponse:
        self._require_valid_type(artifact_type)
        if self._repo.get_artifact(artifact_type, artifact_id) is None:
            raise ArtifactNotFoundError(f"No {artifact_type} artifact with id '{artifact_id}'")
        row = self._repo.get_artifact_governance(artifact_type, artifact_id)
        return self._to_artifact_governance_response(artifact_type, artifact_id, row)

    def _to_artifact_governance_response(self, artifact_type: str, artifact_id: str, row: dict | None) -> ArtifactGovernanceResponse:
        if row is None:
            return ArtifactGovernanceResponse(artifact_type=artifact_type, artifact_id=artifact_id, state=governance.GovernanceState.ACTIVE.value)
        return ArtifactGovernanceResponse(
            artifact_type=artifact_type, artifact_id=artifact_id, state=row["state"], reason=row["reason"],
            actor=row.get("actor"), superseded_by_type=row.get("superseded_by_type"),
            superseded_by_id=row.get("superseded_by_id"), updated_at=row["updated_at"],
        )

    def set_artifact_governance(
        self,
        artifact_type: str,
        artifact_id: str,
        *,
        new_state: str,
        reason: str | None,
        actor: str | None = None,
        superseded_by_type: str | None = None,
        superseded_by_id: str | None = None,
    ) -> ArtifactGovernanceResponse:
        """Design Requirements 1/2/5/6/31/32. Race-safe: the read
        (current state), the transition validation, and the write all
        happen inside ONE already-open BEGIN IMMEDIATE transaction, so a
        concurrent call for the same artifact is fully serialized, never
        racing (see CatalogRepository.transaction())."""
        self._require_valid_type(artifact_type)
        clean_reason = governance.require_reason(reason)
        updated_at = datetime.now(timezone.utc).isoformat()

        with self._repo.transaction(operation="set_artifact_governance"):
            if self._repo.get_artifact(artifact_type, artifact_id) is None:
                raise GovernanceTargetNotFoundError(
                    f"No {artifact_type} artifact with id '{artifact_id}' in the catalog — scan first if it was recently created"
                )
            current = self._repo.get_artifact_governance(artifact_type, artifact_id)
            previous_state = current["state"] if current else governance.GovernanceState.ACTIVE.value
            governance.validate_transition(previous_state, new_state)
            self._repo.set_artifact_governance(
                artifact_type=artifact_type, artifact_id=artifact_id, previous_state=previous_state,
                new_state=new_state, reason=clean_reason, actor=actor,
                superseded_by_type=superseded_by_type, superseded_by_id=superseded_by_id, updated_at=updated_at,
            )
        logger.info(
            "ARTIFACT_GOVERNANCE_CHANGED artifact_type=%s artifact_id=%s previous_state=%s new_state=%s",
            artifact_type, artifact_id, previous_state, new_state,
        )
        return self.get_artifact_governance(artifact_type, artifact_id)

    def get_artifact_governance_history(self, artifact_type: str, artifact_id: str) -> ArtifactGovernanceHistoryResponse:
        self._require_valid_type(artifact_type)
        if self._repo.get_artifact(artifact_type, artifact_id) is None:
            raise ArtifactNotFoundError(f"No {artifact_type} artifact with id '{artifact_id}'")
        current = self._to_artifact_governance_response(artifact_type, artifact_id, self._repo.get_artifact_governance(artifact_type, artifact_id))
        events = [
            GovernanceEvent(
                event_id=e["event_id"], previous_state=e["previous_state"], new_state=e["new_state"], reason=e["reason"],
                actor=e.get("actor"), superseded_by_type=e.get("superseded_by_type"), superseded_by_id=e.get("superseded_by_id"),
                created_at=e["created_at"],
            )
            for e in self._repo.list_artifact_governance_events(artifact_type, artifact_id)
        ]
        return ArtifactGovernanceHistoryResponse(artifact_type=artifact_type, artifact_id=artifact_id, current=current, events=events)

    def get_governance_chain(self, artifact_type: str, artifact_id: str) -> GovernanceChainResponse:
        self._require_valid_type(artifact_type)
        if self._repo.get_artifact(artifact_type, artifact_id) is None:
            raise ArtifactNotFoundError(f"No {artifact_type} artifact with id '{artifact_id}'")
        chain = governance.verify_governance_chain(self._repo, artifact_type=artifact_type, artifact_id=artifact_id)
        return GovernanceChainResponse(
            artifact_type=artifact_type, artifact_id=artifact_id, direct_state=chain.direct_state, direct_reason=chain.direct_reason,
            invalid_ancestors=[AncestorFlagResponse(artifact_type=a.artifact_type, artifact_id=a.artifact_id, reason=a.reason) for a in chain.invalid_ancestors],
            deprecated_ancestors=[AncestorFlagResponse(artifact_type=a.artifact_type, artifact_id=a.artifact_id, reason=a.reason) for a in chain.deprecated_ancestors],
        )

    # ------------------------------------------------------------------
    # Dataset-version governance (v2.5)
    # ------------------------------------------------------------------

    def get_dataset_version_governance(self, dataset_name: str, version: str) -> DatasetVersionGovernanceResponse:
        if self._repo.get_dataset_version(dataset_name, version) is None:
            raise DatasetVersionNotFoundError(f"No version '{version}' registered for dataset '{dataset_name}'")
        row = self._repo.get_dataset_version_governance(dataset_name, version)
        return self._to_version_governance_response(dataset_name, version, row)

    def _to_version_governance_response(self, dataset_name: str, version: str, row: dict | None) -> DatasetVersionGovernanceResponse:
        if row is None:
            return DatasetVersionGovernanceResponse(dataset_name=dataset_name, version=version, state=governance.GovernanceState.ACTIVE.value)
        return DatasetVersionGovernanceResponse(
            dataset_name=dataset_name, version=version, state=row["state"], reason=row["reason"],
            actor=row.get("actor"), updated_at=row["updated_at"],
        )

    def set_dataset_version_governance(
        self, dataset_name: str, version: str, *, new_state: str, reason: str | None, actor: str | None = None
    ) -> DatasetVersionGovernanceResponse:
        """Design Requirement 10: the (dataset_name, version) -> package_id
        mapping itself is NEVER touched here -- this only ever writes to
        the separate dataset_version_governance table."""
        clean_reason = governance.require_reason(reason)
        updated_at = datetime.now(timezone.utc).isoformat()

        with self._repo.transaction(operation="set_dataset_version_governance"):
            if self._repo.get_dataset_version(dataset_name, version) is None:
                raise DatasetVersionNotFoundError(f"No version '{version}' registered for dataset '{dataset_name}'")
            current = self._repo.get_dataset_version_governance(dataset_name, version)
            previous_state = current["state"] if current else governance.GovernanceState.ACTIVE.value
            governance.validate_transition(previous_state, new_state)
            self._repo.set_dataset_version_governance(
                dataset_name=dataset_name, version=version, previous_state=previous_state,
                new_state=new_state, reason=clean_reason, actor=actor, updated_at=updated_at,
            )
        logger.info(
            "DATASET_VERSION_GOVERNANCE_CHANGED dataset_name=%s version=%s previous_state=%s new_state=%s",
            dataset_name, version, previous_state, new_state,
        )
        return self.get_dataset_version_governance(dataset_name, version)

    def get_dataset_version_governance_history(self, dataset_name: str, version: str) -> DatasetVersionGovernanceHistoryResponse:
        if self._repo.get_dataset_version(dataset_name, version) is None:
            raise DatasetVersionNotFoundError(f"No version '{version}' registered for dataset '{dataset_name}'")
        current = self._to_version_governance_response(dataset_name, version, self._repo.get_dataset_version_governance(dataset_name, version))
        events = [
            DatasetVersionGovernanceEvent(
                event_id=e["event_id"], previous_state=e["previous_state"], new_state=e["new_state"],
                reason=e["reason"], actor=e.get("actor"), created_at=e["created_at"],
            )
            for e in self._repo.list_dataset_version_governance_events(dataset_name, version)
        ]
        return DatasetVersionGovernanceHistoryResponse(dataset_name=dataset_name, version=version, current=current, events=events)

    # ------------------------------------------------------------------
    # Selective rebuild planning and execution (v2.5)
    # ------------------------------------------------------------------

    def build_rebuild_plan(self, *, old_type: str, old_id: str, new_type: str, new_id: str) -> RebuildPlanResponse:
        self._require_valid_type(old_type)
        self._require_valid_type(new_type)
        plan = SelectiveRebuildPlanner(self._repo).build_plan(old_type=old_type, old_id=old_id, new_type=new_type, new_id=new_id)
        plan_id = store_plan(plan)
        return RebuildPlanResponse(
            plan_id=plan_id,
            fingerprint=plan.fingerprint,
            replace=RebuildReplacement(old_type=old_type, old_id=old_id, new_type=new_type, new_id=new_id),
            steps=[
                RebuildPlanStep(
                    stage_artifact_type=s.stage_artifact_type, old_artifact_id=s.old_artifact_id,
                    parents=[
                        PlanStepParent(
                            artifact_type=p.artifact_type, original_id=p.original_id, effective_id=p.effective_id,
                            relationship=p.relationship, replaced=p.replaced,
                        )
                        for p in s.parents
                    ],
                    feasible=s.feasible, manual_configuration_required=s.manual_configuration_required,
                    infeasible_reason=s.infeasible_reason,
                )
                for s in plan.steps
            ],
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def execute_rebuild(self, *, plan_id: str, configs: dict[str, dict]) -> RebuildExecuteResponse:
        """Design Requirements 18/19/22/23/24. Re-derives a fresh
        fingerprint from CURRENT catalog state and compares it to the
        stored plan's — if the catalog changed materially since the plan
        was built, this rejects rather than executing against a
        description of the DAG that's no longer accurate."""
        from app.catalog.rebuild_executor import SelectiveRebuildExecutor

        plan = get_plan(plan_id)
        if plan is None:
            raise RebuildPlanNotFoundError(plan_id=plan_id)

        planner = SelectiveRebuildPlanner(self._repo)
        current_fingerprint = planner.fingerprint_now(old_type=plan.old_type, old_id=plan.old_id, new_type=plan.new_type, new_id=plan.new_id)
        if current_fingerprint != plan.fingerprint:
            raise RebuildPlanStaleError(plan_id=plan_id)

        if self._settings is None:
            raise RuntimeError("execute_rebuild requires CatalogService to be constructed with settings=")

        lock = RebuildLock(self._settings.CATALOG_DB_PATH.parent / f"selective_rebuild.{plan.old_type}.{plan.old_id}.lock")
        try:
            with lock.acquire():
                executor = SelectiveRebuildExecutor(repo=self._repo, settings=self._settings)
                results, superseded = executor.execute(plan, configs=configs)
        except CatalogRebuildInProgressError as exc:
            raise SelectiveRebuildInProgressError(old_type=plan.old_type, old_id=plan.old_id) from exc

        discard_plan(plan_id)
        logger.info(
            "SELECTIVE_REBUILD_EXECUTED old_type=%s old_id=%s new_type=%s new_id=%s steps=%d",
            plan.old_type, plan.old_id, plan.new_type, plan.new_id, len(results),
        )
        return RebuildExecuteResponse(
            plan_id=plan_id,
            replace=RebuildReplacement(old_type=plan.old_type, old_id=plan.old_id, new_type=plan.new_type, new_id=plan.new_id),
            results=results,
            superseded=superseded,
        )

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self, artifact_type: str, artifact_id: str, *, recursive: bool = False) -> VerificationResponse:
        self._require_valid_type(artifact_type)
        if self._repo.get_artifact(artifact_type, artifact_id) is None:
            raise ArtifactNotFoundError(f"No {artifact_type} artifact with id '{artifact_id}'")

        outcome = self._verifier.verify(self._repo, artifact_type, artifact_id)
        response = VerificationResponse(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            status=outcome.status,
            checks=[VerificationCheck(name=c.name, status=c.status, detail=c.detail) for c in outcome.checks],
            recursive=recursive,
        )
        logger.info("ARTIFACT_VERIFICATION_COMPLETED artifact_type=%s artifact_id=%s status=%s recursive=%s", artifact_type, artifact_id, outcome.status, recursive)

        if not recursive:
            return response

        nodes, _ = graph.traverse(self._repo, root_type=artifact_type, root_id=artifact_id, direction="upstream")
        verified = failed = missing = 0
        node_results: list[VerificationNodeResult] = []
        for node in nodes:
            node_outcome = self._verifier.verify(self._repo, node["artifact_type"], node["artifact_id"])
            node_results.append(
                VerificationNodeResult(
                    artifact_type=node["artifact_type"],
                    artifact_id=node["artifact_id"],
                    status=node_outcome.status,
                    checks=[VerificationCheck(name=c.name, status=c.status, detail=c.detail) for c in node_outcome.checks],
                )
            )
            if node_outcome.status == "verified":
                verified += 1
            elif node_outcome.status == "missing":
                missing += 1
            else:
                failed += 1

        response.verified_nodes = verified
        response.failed_nodes = failed
        response.missing_nodes = missing
        response.nodes = node_results
        logger.info(
            "ARTIFACT_VERIFICATION_COMPLETED artifact_type=%s artifact_id=%s recursive=true verified_nodes=%d failed_nodes=%d missing_nodes=%d",
            artifact_type, artifact_id, verified, failed, missing,
        )
        return response

    # ------------------------------------------------------------------
    # Dataset registry
    # ------------------------------------------------------------------

    def create_dataset(self, *, dataset_name: str, description: str | None, metadata: dict) -> tuple[DatasetResponse, bool]:
        """Returns (response, created) — created=False means an identical
        dataset already existed (idempotent).

        Race-safe: always attempts the write and lets
        CatalogRepository.create_dataset's dataset_name primary key
        decide who won, rather than checking existence first and racing
        another process between that check and the insert."""
        versioning.validate_dataset_name(dataset_name)
        with self._repo.transaction(operation="create_dataset"):
            created_at = datetime.now(timezone.utc).isoformat()
            created = self._repo.create_dataset(
                dataset_name=dataset_name, description=description, metadata_json=canonical_json(metadata), created_at=created_at
            )
        if created:
            logger.info("DATASET_CREATED dataset_name=%s", dataset_name)
        return self._to_dataset_response(self._repo.get_dataset(dataset_name)), created

    def list_datasets(self) -> list[DatasetResponse]:
        return [self._to_dataset_response(d) for d in self._repo.list_datasets()]

    def _to_dataset_response(self, row: dict) -> DatasetResponse:
        versions = [v["version"] for v in self._repo.list_dataset_versions(row["dataset_name"])]
        return DatasetResponse(
            dataset_name=row["dataset_name"],
            description=row["description"],
            metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else {},
            created_at=row["created_at"],
            version_count=len(versions),
            latest_version=versioning.highest_version(versions),
        )

    def register_version(
        self,
        dataset_name: str,
        *,
        version: str,
        package_id: str,
        description: str | None,
        tags: list[str],
        allow_deprecated: bool = False,
    ) -> tuple[DatasetVersionResponse, bool]:
        """Returns (response, created) — created=False for an idempotent
        re-registration of the exact same (dataset, version, package).

        Design Requirement 33: rejects a package whose effective lineage
        includes an INVALID artifact (no override); rejects a package
        with a DEPRECATED artifact/ancestor unless allow_deprecated=True."""
        dataset = self._repo.get_dataset(dataset_name)
        if dataset is None:
            raise DatasetNotFoundError(f"No dataset named '{dataset_name}' — create it first via POST /api/v1/datasets")
        versioning.validate_semver(version)

        package_artifact = self._repo.get_artifact("package", package_id)
        if package_artifact is None:
            raise PackageNotFoundError(f"No package artifact with id '{package_id}' in the catalog")
        package_metadata = json.loads(package_artifact["metadata_json"])
        if package_artifact.get("status") != "completed":
            raise PackageNotAcceptedError(
                f"Package '{package_id}' has status={package_artifact.get('status')!r}, not 'completed' — "
                f"a rejected package can never become a dataset version"
            )
        qc_status = package_metadata.get("source_qc_status")
        if qc_status not in ACCEPTED_QC_STATUSES:
            raise PackageNotAcceptedError(f"Package '{package_id}' source_qc_status={qc_status!r} is not accepted")

        governance.enforce_upstream_gate(self._repo, artifact_type="package", artifact_id=package_id, allow_deprecated=allow_deprecated)

        # Race-safe: always attempt the write and let
        # CatalogRepository.create_dataset_version's (dataset_name, version)
        # primary key decide the outcome ("created" / idempotent
        # "unchanged" / DatasetVersionImmutableError conflict), rather than
        # checking existence first and racing another process between
        # that check and the insert.
        with self._repo.transaction(operation="register_version"):
            created_at = datetime.now(timezone.utc).isoformat()
            outcome = self._repo.create_dataset_version(
                dataset_name=dataset_name,
                version=version,
                package_id=package_id,
                description=description,
                tags_json=canonical_json(tags),
                status="active",
                created_at=created_at,
            )
        created = outcome == "created"
        if created:
            logger.info("DATASET_VERSION_REGISTERED dataset_name=%s version=%s package_id=%s", dataset_name, version, package_id)
        return self._to_version_response(self._repo.get_dataset_version(dataset_name, version)), created

    def list_versions(self, dataset_name: str) -> list[DatasetVersionResponse]:
        if self._repo.get_dataset(dataset_name) is None:
            raise DatasetNotFoundError(f"No dataset named '{dataset_name}'")
        rows = self._repo.list_dataset_versions(dataset_name)
        ordered = versioning.sort_versions([r["version"] for r in rows])
        by_version = {r["version"]: r for r in rows}
        return [self._to_version_response(by_version[v]) for v in ordered]

    def get_version(self, dataset_name: str, version: str) -> DatasetVersionResponse:
        if self._repo.get_dataset(dataset_name) is None:
            raise DatasetNotFoundError(f"No dataset named '{dataset_name}'")
        row = self._repo.get_dataset_version(dataset_name, version)
        if row is None:
            raise DatasetVersionNotFoundError(f"No version '{version}' registered for dataset '{dataset_name}'")
        return self._to_version_response(row)

    def get_latest(self, dataset_name: str) -> DatasetVersionResponse:
        if self._repo.get_dataset(dataset_name) is None:
            raise DatasetNotFoundError(f"No dataset named '{dataset_name}'")
        rows = self._repo.list_dataset_versions(dataset_name)
        latest = versioning.highest_version([r["version"] for r in rows])
        if latest is None:
            raise DatasetVersionNotFoundError(f"Dataset '{dataset_name}' has no registered versions")
        return self.get_version(dataset_name, latest)

    def _to_version_response(self, row: dict) -> DatasetVersionResponse:
        package_artifact = self._repo.get_artifact("package", row["package_id"])
        package_status = package_artifact.get("status") if package_artifact else None
        package_metadata = json.loads(package_artifact["metadata_json"]) if package_artifact else {}
        fingerprint = None
        if package_artifact is not None:
            fingerprint = compute_lineage_fingerprint(self._fingerprint_payload(row["package_id"]))
        effective = governance.compute_effective_version_status(
            self._repo, dataset_name=row["dataset_name"], version=row["version"], package_id=row["package_id"]
        )
        return DatasetVersionResponse(
            dataset_name=row["dataset_name"],
            version=row["version"],
            package_id=row["package_id"],
            description=row["description"],
            tags=json.loads(row["tags_json"]) if row["tags_json"] else [],
            status=row["status"],
            created_at=row["created_at"],
            package_status=package_status,
            source_qc_status=package_metadata.get("source_qc_status"),
            lineage_fingerprint=fingerprint,
            effective_status=effective.status,
            effective_status_reason=effective.reason,
        )

    # ------------------------------------------------------------------
    # Reproducibility / fingerprint
    # ------------------------------------------------------------------

    def reproducibility(self, dataset_name: str, version: str) -> ReproducibilityResponse:
        if self._repo.get_dataset(dataset_name) is None:
            raise DatasetNotFoundError(f"No dataset named '{dataset_name}'")
        row = self._repo.get_dataset_version(dataset_name, version)
        if row is None:
            raise DatasetVersionNotFoundError(f"No version '{version}' registered for dataset '{dataset_name}'")

        package_id = row["package_id"]
        data = self._collect_reproducibility_data(package_id)
        fingerprint = compute_lineage_fingerprint(self._fingerprint_payload(package_id))

        return ReproducibilityResponse(
            dataset_name=dataset_name,
            version=version,
            package_id=package_id,
            qc_id=data.get("qc_id"),
            source_transformed_sha256=data.get("source_transformed_sha256"),
            qc_config_hash=data.get("qc_config_hash"),
            transformation_config_hash=data.get("transformation_config_hash"),
            cleaning_config_hash=data.get("cleaning_config_hash"),
            synchronization_config_hash=data.get("synchronization_config_hash"),
            normalization_config_hashes=data.get("normalization_config_hashes", []),
            schema_versions=data.get("schema_versions", []),
            source_ingestion_ids=data.get("source_ingestion_ids", []),
            raw_sha256_values=data.get("raw_sha256_values", []),
            package_config_hash=data.get("package_config_hash"),
            split_seed=data.get("split_seed"),
            split_checksums=data.get("split_checksums", {}),
            transform_versions=data.get("transform_versions", {}),
            git_commit=data.get("git_commit"),
            lineage_fingerprint=fingerprint,
        )

    def _collect_reproducibility_data(self, package_id: str) -> dict:
        """Walks the full upstream lineage of a package and extracts every
        reproducibility-relevant field actually present in the indexed
        manifests. Never fabricates a value (e.g. git_commit stays None
        when no manifest recorded one — see README "Code/git provenance")."""
        package_artifact = self._repo.get_artifact("package", package_id)
        if package_artifact is None:
            raise PackageNotFoundError(f"No package artifact with id '{package_id}' in the catalog")

        nodes, _ = graph.traverse(self._repo, root_type="package", root_id=package_id, direction="upstream")
        by_type: dict[str, list[dict]] = {}
        for node in nodes:
            metadata = json.loads(node["metadata_json"]) if node.get("metadata_json") else {}
            by_type.setdefault(node["artifact_type"], []).append(metadata)

        package_metadata = json.loads(package_artifact["metadata_json"])
        result: dict = {
            "source_transformed_sha256": package_metadata.get("source_transformed_sha256"),
            "package_config_hash": package_metadata.get("packaging_config_hash"),
            "split_seed": package_metadata.get("seed"),
            "split_checksums": {name: entry["sha256"] for name, entry in package_metadata.get("splits", {}).items()},
            "qc_id": package_metadata.get("qc_id"),
            "git_commit": package_metadata.get("git_commit"),
        }

        transform_versions: dict[str, str] = {}
        if package_metadata.get("package_engine_version"):
            transform_versions["package"] = package_metadata["package_engine_version"]

        for qc_metadata in by_type.get("qc", []):
            result["qc_config_hash"] = qc_metadata.get("qc_config_hash")
            if qc_metadata.get("qc_engine_version"):
                transform_versions["qc"] = qc_metadata["qc_engine_version"]

        for xform_metadata in by_type.get("transformation", []):
            result["transformation_config_hash"] = xform_metadata.get("transformation_config_hash")
            if xform_metadata.get("transform_version"):
                transform_versions["transformation"] = xform_metadata["transform_version"]

        for cleaning_metadata in by_type.get("cleaning", []):
            result["cleaning_config_hash"] = cleaning_metadata.get("cleaning_config_hash")
            result.setdefault("synchronization_config_hash", cleaning_metadata.get("synchronization_config_hash"))
            if cleaning_metadata.get("transform_version"):
                transform_versions["cleaning"] = cleaning_metadata["transform_version"]

        for sync_metadata in by_type.get("synchronization", []):
            result["synchronization_config_hash"] = sync_metadata.get("synchronization_config_hash")
            if sync_metadata.get("transform_version"):
                transform_versions["synchronization"] = sync_metadata["transform_version"]

        normalization_hashes = set()
        schema_versions = set()
        for norm_metadata in by_type.get("normalization", []):
            if norm_metadata.get("normalization_config_hash"):
                normalization_hashes.add(norm_metadata["normalization_config_hash"])
            schema = norm_metadata.get("schema") or {}
            if schema.get("name") and schema.get("version"):
                schema_versions.add(f"{schema['name']}:{schema['version']}")
            if norm_metadata.get("transform_version"):
                transform_versions["normalization"] = norm_metadata["transform_version"]
        result["normalization_config_hashes"] = sorted(normalization_hashes)
        result["schema_versions"] = sorted(schema_versions)

        ingestion_ids = sorted({m["ingestion_id"] for m in by_type.get("ingestion", []) if m.get("ingestion_id")})
        raw_sha256_values = sorted({m["sha256"] for m in by_type.get("ingestion", []) if m.get("sha256")})
        result["source_ingestion_ids"] = ingestion_ids
        result["raw_sha256_values"] = raw_sha256_values
        result["transform_versions"] = transform_versions
        return result

    def _fingerprint_payload(self, package_id: str) -> dict:
        """The subset of reproducibility data that defines the fingerprint
        — content hashes, config hashes, and versioned logic identifiers
        only. Deliberately excludes package_id/qc_id/ingestion_ids
        (random execution IDs), created_at, and filesystem paths, so two
        equivalent runs over the same data/config produce the SAME
        fingerprint despite having different execution IDs."""
        data = self._collect_reproducibility_data(package_id)
        return {
            "raw_sha256_values": data.get("raw_sha256_values", []),
            "schema_versions": data.get("schema_versions", []),
            "normalization_config_hashes": data.get("normalization_config_hashes", []),
            "synchronization_config_hash": data.get("synchronization_config_hash"),
            "cleaning_config_hash": data.get("cleaning_config_hash"),
            "transformation_config_hash": data.get("transformation_config_hash"),
            "qc_config_hash": data.get("qc_config_hash"),
            "package_config_hash": data.get("package_config_hash"),
            "split_checksums": data.get("split_checksums", {}),
        }


def _to_summary(row: dict) -> ArtifactSummary:
    return ArtifactSummary(
        artifact_type=row["artifact_type"],
        artifact_id=row["artifact_id"],
        pipeline_stage=row["pipeline_stage"],
        status=row.get("status"),
        storage_uri=row.get("storage_uri"),
        content_sha256=row.get("content_sha256"),
        manifest_uri=row.get("manifest_uri"),
        manifest_sha256=row.get("manifest_sha256"),
        created_at=row.get("created_at"),
        session_id=row.get("session_id"),
        registered_at=row["registered_at"],
    )
