"""Selective rebuild planning (v2.5).

Given a bad artifact and an already-created replacement for it, computes
an ORDERED plan of exactly the downstream descendants that need to be
rebuilt to incorporate the replacement — reusing every unaffected sibling
parent unchanged (Design Requirement 16). This is pure, read-only
planning logic: it never calls a pipeline stage service and never writes
to the catalog. See app.catalog.rebuild_executor for actually running a
plan.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.catalog import graph
from app.catalog.errors import ArtifactNotFoundError, LineageCycleDetectedError, RebuildReplacementIncompatibleError
from app.catalog.serialization import compute_lineage_fingerprint

# Stages whose manifest embeds the FULL effective request config, not
# just a hash of it -- see docs/DETAILED_GUIDE.md's v2.5 section,
# "Config reuse" -- so a rebuild of these can be fully automatic.
# Every other downstream stage (cleaning/transformation/qc/packaging)
# only ever recorded a *_config_hash, never the raw config dict, so
# their config genuinely cannot be reconstructed -- Design Requirement
# 17 requires being honest about that rather than pretending otherwise.
AUTO_RECONSTRUCTABLE_STAGES = frozenset({"synchronization"})
MANUAL_CONFIG_STAGES = frozenset({"cleaning", "transformation", "qc", "package"})


@dataclass(frozen=True)
class PlanStepParent:
    artifact_type: str
    original_id: str
    effective_id: str | None  # None when replaced (unknown until execution)
    relationship: str
    replaced: bool


@dataclass(frozen=True)
class PlanStep:
    stage_artifact_type: str
    old_artifact_id: str
    parents: list[PlanStepParent]
    feasible: bool
    manual_configuration_required: bool
    infeasible_reason: str | None = None


@dataclass(frozen=True)
class RebuildPlan:
    old_type: str
    old_id: str
    new_type: str
    new_id: str
    steps: list[PlanStep] = field(default_factory=list)
    fingerprint: str = ""


class SelectiveRebuildPlanner:
    def __init__(self, repo) -> None:
        self._repo = repo

    # ------------------------------------------------------------------
    # Compatibility (Design Requirement 14)
    # ------------------------------------------------------------------

    def _check_compatibility(self, *, old_type: str, old_id: str, new_type: str, new_id: str) -> tuple[dict, dict]:
        old = self._repo.get_artifact(old_type, old_id)
        if old is None:
            raise ArtifactNotFoundError(f"No {old_type} artifact with id '{old_id}' (the artifact being replaced)")
        new = self._repo.get_artifact(new_type, new_id)
        if new is None:
            raise ArtifactNotFoundError(f"No {new_type} artifact with id '{new_id}' (the proposed replacement)")

        if old_type != new_type:
            raise RebuildReplacementIncompatibleError(
                old_type=old_type, old_id=old_id, new_type=new_type, new_id=new_id,
                reason=f"replacement artifact_type '{new_type}' must match the original's '{old_type}'",
            )

        old_meta = json.loads(old["metadata_json"]) if old.get("metadata_json") else {}
        new_meta = json.loads(new["metadata_json"]) if new.get("metadata_json") else {}

        if old.get("session_id") and new.get("session_id") and old["session_id"] != new["session_id"]:
            raise RebuildReplacementIncompatibleError(
                old_type=old_type, old_id=old_id, new_type=new_type, new_id=new_id,
                reason=f"different session_id ('{old['session_id']}' vs '{new['session_id']}') — a replacement must belong to the same session",
            )

        if old_type == "normalization":
            old_schema = (old_meta.get("schema") or {}).get("name")
            new_schema = (new_meta.get("schema") or {}).get("name")
            if old_schema and new_schema and old_schema != new_schema:
                raise RebuildReplacementIncompatibleError(
                    old_type=old_type, old_id=old_id, new_type=new_type, new_id=new_id,
                    reason=f"schema mismatch: original uses '{old_schema}', replacement uses '{new_schema}' "
                    f"(e.g. a GPS normalization cannot replace an IMU normalization)",
                )
            if old_meta.get("ingestion_id") and new_meta.get("ingestion_id") and old_meta["ingestion_id"] != new_meta["ingestion_id"]:
                raise RebuildReplacementIncompatibleError(
                    old_type=old_type, old_id=old_id, new_type=new_type, new_id=new_id,
                    reason="replacement must normalize the same source ingestion as the original",
                )

        return old, new

    # ------------------------------------------------------------------
    # Topological order over the downstream closure (Design Requirement 15)
    # ------------------------------------------------------------------

    def _topological_order(self, node_keys: list[tuple[str, str]]) -> list[tuple[str, str]]:
        node_set = set(node_keys)
        in_degree: dict[tuple[str, str], int] = dict.fromkeys(node_keys, 0)
        children_map: dict[tuple[str, str], list[tuple[str, str]]] = {k: [] for k in node_keys}
        for atype, aid in node_keys:
            for edge in self._repo.get_parents(atype, aid):
                pkey = (edge["parent_artifact_type"], edge["parent_artifact_id"])
                if pkey in node_set:
                    in_degree[(atype, aid)] += 1
                    children_map[pkey].append((atype, aid))

        queue = deque(sorted(k for k in node_keys if in_degree[k] == 0))
        order: list[tuple[str, str]] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for child in sorted(children_map[node]):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        if len(order) != len(node_keys):
            # Should be impossible given catalog invariants (cycles are
            # rejected at edge-insertion time — see graph.would_create_cycle)
            # but checked defensively per Design Requirement 15.
            raise LineageCycleDetectedError(
                "Cycle detected while planning a selective rebuild — this should be unreachable given catalog invariants"
            )
        return order

    # ------------------------------------------------------------------
    # Plan construction
    # ------------------------------------------------------------------

    def build_plan(self, *, old_type: str, old_id: str, new_type: str, new_id: str) -> RebuildPlan:
        self._check_compatibility(old_type=old_type, old_id=old_id, new_type=new_type, new_id=new_id)

        nodes, _ = graph.traverse(self._repo, root_type=old_type, root_id=old_id, direction="downstream")
        node_keys = [(n["artifact_type"], n["artifact_id"]) for n in nodes]
        order = self._topological_order(node_keys)

        replacement_map: dict[tuple[str, str], tuple[str, str]] = {(old_type, old_id): (new_type, new_id)}
        steps: list[PlanStep] = []

        for atype, aid in order:
            if (atype, aid) == (old_type, old_id):
                continue

            parent_edges = self._repo.get_parents(atype, aid)
            step_parents: list[PlanStepParent] = []
            any_replaced = False
            for edge in parent_edges:
                pkey = (edge["parent_artifact_type"], edge["parent_artifact_id"])
                if pkey in replacement_map:
                    any_replaced = True
                    step_parents.append(
                        PlanStepParent(
                            artifact_type=pkey[0], original_id=pkey[1], effective_id=None,
                            relationship=edge["relationship"], replaced=True,
                        )
                    )
                else:
                    step_parents.append(
                        PlanStepParent(
                            artifact_type=pkey[0], original_id=pkey[1], effective_id=pkey[1],
                            relationship=edge["relationship"], replaced=False,
                        )
                    )

            if not any_replaced:
                # Reachable via traverse() but not actually downstream of
                # a replaced parent on THIS node (can't normally happen,
                # since traverse(downstream) only visits nodes reachable
                # by following edges FROM the replaced root) — skip
                # defensively rather than rebuild something unaffected.
                continue

            manual = atype in MANUAL_CONFIG_STAGES
            steps.append(
                PlanStep(
                    stage_artifact_type=atype,
                    old_artifact_id=aid,
                    parents=step_parents,
                    feasible=True,
                    manual_configuration_required=manual,
                    infeasible_reason=(
                        "manifest only recorded a config hash, not the raw config — must be supplied at execute time"
                        if manual
                        else None
                    ),
                )
            )
            # Chain forward: descendants of this node should treat it as
            # replaced too, once it's rebuilt.
            replacement_map[(atype, aid)] = (atype, f"<pending:{atype}:{aid}>")

        fingerprint = self._fingerprint(old_type=old_type, old_id=old_id, new_type=new_type, new_id=new_id, steps=steps)
        return RebuildPlan(old_type=old_type, old_id=old_id, new_type=new_type, new_id=new_id, steps=steps, fingerprint=fingerprint)

    # ------------------------------------------------------------------
    # Fingerprint (Design Requirement 23)
    # ------------------------------------------------------------------

    def _fingerprint(self, *, old_type: str, old_id: str, new_type: str, new_id: str, steps: list[PlanStep]) -> str:
        payload = {
            "old": [old_type, old_id],
            "new": [new_type, new_id],
            "steps": [
                {
                    "stage": s.stage_artifact_type,
                    "old_id": s.old_artifact_id,
                    "manifest_sha256": self._repo.get_artifact(s.stage_artifact_type, s.old_artifact_id)["manifest_sha256"],
                    "parents": sorted((p.artifact_type, p.original_id, p.replaced) for p in s.parents),
                }
                for s in steps
            ],
        }
        return compute_lineage_fingerprint(payload)

    def fingerprint_now(self, *, old_type: str, old_id: str, new_type: str, new_id: str) -> str:
        """Recomputes the fingerprint from CURRENT catalog state — used
        at execute time to detect drift (Design Requirement 23:
        REBUILD_PLAN_STALE)."""
        return self.build_plan(old_type=old_type, old_id=old_id, new_type=new_type, new_id=new_id).fingerprint
