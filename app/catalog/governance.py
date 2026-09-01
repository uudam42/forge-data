"""Data governance state machine and governance-aware lineage checks (v2.5).

Turns lineage from passive observability into active governance: an
artifact can be marked `deprecated` or `invalid` WITHOUT touching its
(immutable) filesystem manifest, and that judgment then gates whether it
can be used as an input to NEW downstream work.

Two distinct concepts, deliberately never conflated (Design Requirement
34):
  - QC status ("passed"/"failed") is an analytical quality RESULT
    recorded by the QC stage at creation time, baked into that stage's
    own manifest, immutable.
  - Governance state (this module) is a human/system TRUST decision,
    recorded separately, that can change at any time after the fact,
    completely independent of QC. A QC-failed package can exist
    ungoverned (active); a QC-passed package can later be marked
    invalid (e.g. a calibration bug discovered after the fact).

No governance row for an artifact means ACTIVE (Design Requirement 3) --
this table only ever holds rows for artifacts someone has flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.catalog.errors import (
    ArtifactDeprecatedError,
    ArtifactInvalidError,
    GovernanceReasonRequiredError,
    InvalidGovernanceTransitionError,
    UpstreamArtifactDeprecatedError,
    UpstreamArtifactInvalidError,
)


class GovernanceState(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    INVALID = "invalid"


# The only states ever persisted as a row -- ACTIVE is represented by a
# row's ABSENCE, never stored explicitly (Design Requirement 3).
STORED_STATES = (GovernanceState.DEPRECATED.value, GovernanceState.INVALID.value)

# Allowed (previous -> new) transitions (Design Requirement 31). A
# same-state transition (e.g. invalid -> invalid) is allowed on purpose:
# it lets a caller update the reason or attach superseded_by without
# first reactivating, and still lands as a new, honest audit event.
ALLOWED_TRANSITIONS: dict[GovernanceState, frozenset[GovernanceState]] = {
    GovernanceState.ACTIVE: frozenset({GovernanceState.ACTIVE, GovernanceState.DEPRECATED, GovernanceState.INVALID}),
    GovernanceState.DEPRECATED: frozenset({GovernanceState.DEPRECATED, GovernanceState.ACTIVE, GovernanceState.INVALID}),
    GovernanceState.INVALID: frozenset({GovernanceState.INVALID, GovernanceState.ACTIVE, GovernanceState.DEPRECATED}),
}


def validate_transition(previous: str, new: str) -> None:
    try:
        prev_state = GovernanceState(previous)
        new_state = GovernanceState(new)
    except ValueError as exc:
        raise InvalidGovernanceTransitionError(
            f"Unknown governance state in transition {previous!r} -> {new!r}; "
            f"valid states are {[s.value for s in GovernanceState]}"
        ) from exc
    if new_state not in ALLOWED_TRANSITIONS[prev_state]:
        raise InvalidGovernanceTransitionError(f"{previous} -> {new} is not an allowed governance transition")


def require_reason(reason: str | None) -> str:
    """Every deprecate/invalidate/reactivate call requires a non-empty
    reason -- this is what lets the audit trail answer "why", not just
    "what changed" (Design Requirement 4/5)."""
    if reason is None or not reason.strip():
        raise GovernanceReasonRequiredError("A non-empty reason is required for this governance transition")
    return reason.strip()


# ---------------------------------------------------------------------------
# Governance-aware lineage chain check (Design Requirement 8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AncestorFlag:
    artifact_type: str
    artifact_id: str
    reason: str


@dataclass(frozen=True)
class GovernanceChainResult:
    direct_state: str
    direct_reason: str | None
    invalid_ancestors: list[AncestorFlag] = field(default_factory=list)
    deprecated_ancestors: list[AncestorFlag] = field(default_factory=list)


def verify_governance_chain(repo, *, artifact_type: str, artifact_id: str) -> GovernanceChainResult:
    """Walks the FULL upstream lineage (not just the direct parent) and
    reports the artifact's own state plus every invalid/deprecated
    ancestor found. A direct input can look perfectly active while an
    ancestor several stages back is invalid -- this is what catches
    that, rather than trusting a descendant's active-looking state at
    face value."""
    from app.catalog import graph  # local import: graph never imports governance, avoids a cycle

    direct_gov = repo.get_artifact_governance(artifact_type, artifact_id)
    direct_state = direct_gov["state"] if direct_gov else GovernanceState.ACTIVE.value
    direct_reason = direct_gov["reason"] if direct_gov else None

    nodes, _ = graph.traverse(repo, root_type=artifact_type, root_id=artifact_id, direction="upstream")
    invalid_ancestors: list[AncestorFlag] = []
    deprecated_ancestors: list[AncestorFlag] = []
    for node in nodes:
        if node["artifact_type"] == artifact_type and node["artifact_id"] == artifact_id:
            continue  # the root itself is `direct_state`, not an ancestor
        gov = repo.get_artifact_governance(node["artifact_type"], node["artifact_id"])
        if gov is None:
            continue
        flag = AncestorFlag(artifact_type=node["artifact_type"], artifact_id=node["artifact_id"], reason=gov["reason"])
        if gov["state"] == GovernanceState.INVALID.value:
            invalid_ancestors.append(flag)
        elif gov["state"] == GovernanceState.DEPRECATED.value:
            deprecated_ancestors.append(flag)

    invalid_ancestors.sort(key=lambda a: (a.artifact_type, a.artifact_id))
    deprecated_ancestors.sort(key=lambda a: (a.artifact_type, a.artifact_id))
    return GovernanceChainResult(
        direct_state=direct_state,
        direct_reason=direct_reason,
        invalid_ancestors=invalid_ancestors,
        deprecated_ancestors=deprecated_ancestors,
    )


def enforce_upstream_gate(repo, *, artifact_type: str, artifact_id: str, allow_deprecated: bool = False) -> None:
    """The downstream-processing gate (Design Requirement 7/8). Raises a
    structured error if `artifact_type`/`artifact_id` cannot be used as
    an input to NEW downstream work right now:

      - direct state invalid            -> ArtifactInvalidError (no override)
      - an ancestor is invalid          -> UpstreamArtifactInvalidError (no override)
      - direct state deprecated         -> ArtifactDeprecatedError (allow_deprecated=True bypasses)
      - an ancestor is deprecated       -> UpstreamArtifactDeprecatedError (allow_deprecated=True bypasses)

    If the artifact isn't in the catalog at all (never scanned), this
    silently passes -- governance can only see what's been scanned; see
    docs/DETAILED_GUIDE.md's v2.5 section for why that's a deliberate,
    documented limitation rather than an oversight."""
    if repo.get_artifact(artifact_type, artifact_id) is None:
        return

    chain = verify_governance_chain(repo, artifact_type=artifact_type, artifact_id=artifact_id)

    if chain.direct_state == GovernanceState.INVALID.value:
        raise ArtifactInvalidError(artifact_type=artifact_type, artifact_id=artifact_id, reason=chain.direct_reason or "")
    if chain.invalid_ancestors:
        anc = chain.invalid_ancestors[0]
        raise UpstreamArtifactInvalidError(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            invalid_ancestor_type=anc.artifact_type,
            invalid_ancestor_id=anc.artifact_id,
            reason=anc.reason,
        )

    if allow_deprecated:
        return

    if chain.direct_state == GovernanceState.DEPRECATED.value:
        raise ArtifactDeprecatedError(artifact_type=artifact_type, artifact_id=artifact_id, reason=chain.direct_reason or "")
    if chain.deprecated_ancestors:
        anc = chain.deprecated_ancestors[0]
        raise UpstreamArtifactDeprecatedError(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            deprecated_ancestor_type=anc.artifact_type,
            deprecated_ancestor_id=anc.artifact_id,
            reason=anc.reason,
        )


# ---------------------------------------------------------------------------
# Effective dataset-version status (Design Requirement 11)
# ---------------------------------------------------------------------------


class EffectiveVersionStatus(str, Enum):
    HEALTHY = "healthy"
    DEPRECATED = "deprecated"
    INVALID = "invalid"
    AFFECTED = "affected"


@dataclass(frozen=True)
class EffectiveVersionStatusResult:
    status: str
    reason: str | None
    source: str | None  # "explicit" (governed directly) | "<artifact_type>/<artifact_id>" (derived) | None


def compute_effective_version_status(repo, *, dataset_name: str, version: str, package_id: str) -> EffectiveVersionStatusResult:
    """Explicit governance on the version itself always wins over
    anything derived from upstream. Otherwise, derived purely from
    whether the package's upstream chain contains an INVALID artifact --
    a deprecated ancestor alone does NOT make a version "affected" (a
    deprecated artifact's existing descendants remain historically
    intact per Design Requirement 2)."""
    version_gov = repo.get_dataset_version_governance(dataset_name, version)
    if version_gov is not None:
        return EffectiveVersionStatusResult(status=version_gov["state"], reason=version_gov["reason"], source="explicit")

    chain = verify_governance_chain(repo, artifact_type="package", artifact_id=package_id)
    if chain.direct_state == GovernanceState.INVALID.value:
        return EffectiveVersionStatusResult(
            status=EffectiveVersionStatus.AFFECTED.value, reason=chain.direct_reason, source=f"package/{package_id}"
        )
    if chain.invalid_ancestors:
        anc = chain.invalid_ancestors[0]
        return EffectiveVersionStatusResult(
            status=EffectiveVersionStatus.AFFECTED.value,
            reason=anc.reason,
            source=f"{anc.artifact_type}/{anc.artifact_id}",
        )
    return EffectiveVersionStatusResult(status=EffectiveVersionStatus.HEALTHY.value, reason=None, source=None)
