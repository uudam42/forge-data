"""Structured error codes and exception classes for the catalog stage.

API/system errors (things that map to an HTTP error status) are kept
separate from health/verification FINDINGS (which are normal, successful
responses describing a problem observed in the data — mirroring the QC
philosophy: a verification that finds a checksum mismatch is a
successfully-executed verification, not a server error).
"""

from __future__ import annotations

from enum import Enum


class CatalogErrorCode(str, Enum):
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    ARTIFACT_REGISTRY_CONFLICT = "ARTIFACT_REGISTRY_CONFLICT"
    INVALID_ARTIFACT_TYPE = "INVALID_ARTIFACT_TYPE"
    CATALOG_REBUILD_FAILED = "CATALOG_REBUILD_FAILED"
    CATALOG_SCAN_FAILED = "CATALOG_SCAN_FAILED"
    LINEAGE_CYCLE_DETECTED = "LINEAGE_CYCLE_DETECTED"
    MISSING_LINEAGE_PARENT = "MISSING_LINEAGE_PARENT"
    BROKEN_LINEAGE = "BROKEN_LINEAGE"
    ARTIFACT_CHECKSUM_MISMATCH = "ARTIFACT_CHECKSUM_MISMATCH"
    ARTIFACT_FILE_MISSING = "ARTIFACT_FILE_MISSING"
    DATASET_NOT_FOUND = "DATASET_NOT_FOUND"
    DATASET_ALREADY_EXISTS = "DATASET_ALREADY_EXISTS"
    INVALID_DATASET_NAME = "INVALID_DATASET_NAME"
    INVALID_DATASET_VERSION = "INVALID_DATASET_VERSION"
    DATASET_VERSION_NOT_FOUND = "DATASET_VERSION_NOT_FOUND"
    DATASET_VERSION_IMMUTABLE = "DATASET_VERSION_IMMUTABLE"
    PACKAGE_NOT_FOUND = "PACKAGE_NOT_FOUND"
    PACKAGE_NOT_ACCEPTED = "PACKAGE_NOT_ACCEPTED"
    PACKAGE_CHECKSUM_MISMATCH = "PACKAGE_CHECKSUM_MISMATCH"
    CATALOG_SCHEMA_MISMATCH = "CATALOG_SCHEMA_MISMATCH"
    # v2.4 — multiprocess concurrency
    CATALOG_BUSY = "CATALOG_BUSY"
    CATALOG_REBUILD_IN_PROGRESS = "CATALOG_REBUILD_IN_PROGRESS"
    CATALOG_LOCK_FAILED = "CATALOG_LOCK_FAILED"
    # v2.5 — data governance and selective rebuild
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    ARTIFACT_DEPRECATED = "ARTIFACT_DEPRECATED"
    UPSTREAM_ARTIFACT_INVALID = "UPSTREAM_ARTIFACT_INVALID"
    UPSTREAM_ARTIFACT_DEPRECATED = "UPSTREAM_ARTIFACT_DEPRECATED"
    GOVERNANCE_TARGET_NOT_FOUND = "GOVERNANCE_TARGET_NOT_FOUND"
    INVALID_GOVERNANCE_TRANSITION = "INVALID_GOVERNANCE_TRANSITION"
    GOVERNANCE_REASON_REQUIRED = "GOVERNANCE_REASON_REQUIRED"
    REBUILD_REPLACEMENT_INCOMPATIBLE = "REBUILD_REPLACEMENT_INCOMPATIBLE"
    REBUILD_CONFIG_UNAVAILABLE = "REBUILD_CONFIG_UNAVAILABLE"
    REBUILD_PLAN_STALE = "REBUILD_PLAN_STALE"
    REBUILD_PLAN_NOT_FOUND = "REBUILD_PLAN_NOT_FOUND"
    SELECTIVE_REBUILD_IN_PROGRESS = "SELECTIVE_REBUILD_IN_PROGRESS"


class CatalogError(Exception):
    """Base class for catalog-service failures mapped to HTTP by the API layer."""


class ArtifactNotFoundError(CatalogError):
    pass


class ArtifactRegistryConflictError(CatalogError):
    pass


class InvalidArtifactTypeError(CatalogError):
    pass


class CatalogRebuildFailedError(CatalogError):
    pass


class CatalogScanFailedError(CatalogError):
    pass


class LineageCycleDetectedError(CatalogError):
    pass


class DatasetNotFoundError(CatalogError):
    pass


class DatasetAlreadyExistsError(CatalogError):
    pass


class InvalidDatasetNameError(CatalogError):
    pass


class InvalidDatasetVersionError(CatalogError):
    pass


class DatasetVersionNotFoundError(CatalogError):
    pass


class DatasetVersionImmutableError(CatalogError):
    pass


class PackageNotFoundError(CatalogError):
    pass


class PackageNotAcceptedError(CatalogError):
    pass


# ---------------------------------------------------------------------------
# v2.4 — multiprocess concurrency
# ---------------------------------------------------------------------------


class CatalogBusyError(CatalogError):
    """A write transaction could not acquire the SQLite write lock within
    the configured busy_timeout — another process (or another request in
    this process) held it too long. Never raised for a raw
    sqlite3.OperationalError the caller didn't cause; this wraps
    specifically "database is locked"/"database is busy" conditions.
    The underlying artifact/catalog state is unaffected — filesystem
    manifests remain the source of truth, and a later request/scan can
    retry."""

    def __init__(self, *, operation: str, timeout_ms: int, db_path: str) -> None:
        self.operation = operation
        self.timeout_ms = timeout_ms
        self.db_path = db_path
        super().__init__(
            f"Catalog busy: '{operation}' could not acquire a write lock within {timeout_ms}ms "
            f"(db={db_path}). Another process is writing to the catalog; retry shortly."
        )

    def to_dict(self) -> dict:
        return {
            "code": CatalogErrorCode.CATALOG_BUSY.value,
            "operation": self.operation,
            "timeout_ms": self.timeout_ms,
            "db_path": self.db_path,
        }


class CatalogRebuildInProgressError(CatalogError):
    """Another process already holds the exclusive rebuild lock. This
    project's chosen policy (v2.4) is explicit, immediate failure rather
    than a blocking wait — see docs/DETAILED_GUIDE.md, "Rebuild lock
    design"."""

    def __init__(self, *, lock_path: str, holder: dict | None = None) -> None:
        self.lock_path = lock_path
        self.holder = holder
        detail = f" (held by: {holder})" if holder else ""
        super().__init__(f"A catalog rebuild is already in progress{detail} (lock={lock_path})")

    def to_dict(self) -> dict:
        return {
            "code": CatalogErrorCode.CATALOG_REBUILD_IN_PROGRESS.value,
            "lock_path": self.lock_path,
            "holder": self.holder,
        }


class CatalogLockFailedError(CatalogError):
    """The rebuild lock file itself could not be created/opened/locked
    for a reason OTHER than "already held" (e.g. a permissions error,
    disk full, or the lock directory missing) -- distinct from
    CatalogRebuildInProgressError so a caller can tell "someone else is
    rebuilding" apart from "the locking mechanism itself is broken"."""

    def __init__(self, *, lock_path: str, reason: str) -> None:
        self.lock_path = lock_path
        self.reason = reason
        super().__init__(f"Failed to acquire the catalog rebuild lock at {lock_path}: {reason}")

    def to_dict(self) -> dict:
        return {"code": CatalogErrorCode.CATALOG_LOCK_FAILED.value, "lock_path": self.lock_path, "reason": self.reason}


# ---------------------------------------------------------------------------
# v2.5 — data governance and selective rebuild
# ---------------------------------------------------------------------------


class GovernanceTargetNotFoundError(CatalogError):
    """The artifact/dataset-version a governance operation targets isn't
    in the catalog at all (never scanned, or a typo) -- distinct from
    "artifact exists but is active" (no governance row)."""


class InvalidGovernanceTransitionError(CatalogError):
    """The requested state transition isn't in the allowed set (see
    app.catalog.governance.ALLOWED_TRANSITIONS) -- e.g. there is no
    transition into a state that isn't active/deprecated/invalid."""


class GovernanceReasonRequiredError(CatalogError):
    """deprecate/invalidate/reactivate all require a non-empty reason --
    this is what makes the audit trail answer "why", not just "what"."""


class ArtifactInvalidError(CatalogError):
    """The DIRECT artifact this request targets is itself marked invalid.
    Distinct from UpstreamArtifactInvalidError, which is about an
    ancestor further up the chain."""

    def __init__(self, *, artifact_type: str, artifact_id: str, reason: str) -> None:
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.reason = reason
        super().__init__(f"{artifact_type}/{artifact_id} is marked invalid: {reason}")

    def to_dict(self) -> dict:
        return {
            "code": CatalogErrorCode.ARTIFACT_INVALID.value,
            "artifact_type": self.artifact_type,
            "artifact_id": self.artifact_id,
            "reason": self.reason,
        }


class ArtifactDeprecatedError(CatalogError):
    """The direct artifact is deprecated and the caller did not pass
    allow_deprecated=true. Never raised for invalid artifacts (those
    always raise ArtifactInvalidError / UpstreamArtifactInvalidError,
    which have no override)."""

    def __init__(self, *, artifact_type: str, artifact_id: str, reason: str) -> None:
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.reason = reason
        super().__init__(
            f"{artifact_type}/{artifact_id} is deprecated: {reason} "
            f"(pass allow_deprecated=true to use it for new downstream work anyway)"
        )

    def to_dict(self) -> dict:
        return {
            "code": CatalogErrorCode.ARTIFACT_DEPRECATED.value,
            "artifact_type": self.artifact_type,
            "artifact_id": self.artifact_id,
            "reason": self.reason,
        }


class UpstreamArtifactInvalidError(CatalogError):
    """A direct input artifact is itself active, but one of ITS ancestors
    is marked invalid -- see Design Requirement 8's governance-chain
    check. There is no override for this; an invalid ancestor always
    blocks new downstream work through it."""

    def __init__(self, *, artifact_type: str, artifact_id: str, invalid_ancestor_type: str, invalid_ancestor_id: str, reason: str) -> None:
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.invalid_ancestor_type = invalid_ancestor_type
        self.invalid_ancestor_id = invalid_ancestor_id
        self.reason = reason
        super().__init__(
            f"{artifact_type}/{artifact_id} has an invalid ancestor "
            f"{invalid_ancestor_type}/{invalid_ancestor_id}: {reason}"
        )

    def to_dict(self) -> dict:
        return {
            "code": CatalogErrorCode.UPSTREAM_ARTIFACT_INVALID.value,
            "artifact_type": self.artifact_type,
            "artifact_id": self.artifact_id,
            "invalid_ancestor_type": self.invalid_ancestor_type,
            "invalid_ancestor_id": self.invalid_ancestor_id,
            "reason": self.reason,
        }


class UpstreamArtifactDeprecatedError(CatalogError):
    """A direct input artifact is active, but one of its ancestors is
    deprecated, and the caller did not pass allow_deprecated=true."""

    def __init__(self, *, artifact_type: str, artifact_id: str, deprecated_ancestor_type: str, deprecated_ancestor_id: str, reason: str) -> None:
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.deprecated_ancestor_type = deprecated_ancestor_type
        self.deprecated_ancestor_id = deprecated_ancestor_id
        self.reason = reason
        super().__init__(
            f"{artifact_type}/{artifact_id} has a deprecated ancestor "
            f"{deprecated_ancestor_type}/{deprecated_ancestor_id}: {reason} "
            f"(pass allow_deprecated=true to use it for new downstream work anyway)"
        )

    def to_dict(self) -> dict:
        return {
            "code": CatalogErrorCode.UPSTREAM_ARTIFACT_DEPRECATED.value,
            "artifact_type": self.artifact_type,
            "artifact_id": self.artifact_id,
            "deprecated_ancestor_type": self.deprecated_ancestor_type,
            "deprecated_ancestor_id": self.deprecated_ancestor_id,
            "reason": self.reason,
        }


class RebuildReplacementIncompatibleError(CatalogError):
    """The proposed (old, new) replacement pair fails a compatibility
    check -- different artifact_type, different upstream scope, or an
    otherwise nonsensical substitution (e.g. a GPS normalization
    replacing an IMU normalization)."""

    def __init__(self, *, old_type: str, old_id: str, new_type: str, new_id: str, reason: str) -> None:
        self.old_type = old_type
        self.old_id = old_id
        self.new_type = new_type
        self.new_id = new_id
        self.reason = reason
        super().__init__(f"{new_type}/{new_id} cannot replace {old_type}/{old_id}: {reason}")

    def to_dict(self) -> dict:
        return {
            "code": CatalogErrorCode.REBUILD_REPLACEMENT_INCOMPATIBLE.value,
            "old_type": self.old_type,
            "old_id": self.old_id,
            "new_type": self.new_type,
            "new_id": self.new_id,
            "reason": self.reason,
        }


class RebuildConfigUnavailableError(CatalogError):
    """Raised only by the EXECUTOR (never the planner, which reports this
    honestly as a plan-step flag instead) if execution is attempted for a
    manual_configuration_required step without an explicit config
    override supplied."""

    def __init__(self, *, artifact_type: str, artifact_id: str, reason: str) -> None:
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.reason = reason
        super().__init__(f"Cannot auto-execute rebuild of {artifact_type}/{artifact_id}: {reason}")

    def to_dict(self) -> dict:
        return {
            "code": CatalogErrorCode.REBUILD_CONFIG_UNAVAILABLE.value,
            "artifact_type": self.artifact_type,
            "artifact_id": self.artifact_id,
            "reason": self.reason,
        }


class RebuildPlanStaleError(CatalogError):
    """The catalog changed materially between plan and execute (the
    fingerprint no longer matches) -- reject and require a fresh plan
    rather than executing against a description of the DAG that's no
    longer accurate."""

    def __init__(self, *, plan_id: str) -> None:
        self.plan_id = plan_id
        super().__init__(f"Rebuild plan {plan_id} is stale (the catalog changed since it was built) — request a new plan")

    def to_dict(self) -> dict:
        return {"code": CatalogErrorCode.REBUILD_PLAN_STALE.value, "plan_id": self.plan_id}


class RebuildPlanNotFoundError(CatalogError):
    """No plan with this plan_id is known to this process. Plans are
    held in-memory only (see Design Requirement 18 — no background
    queue, no persistence) -- a plan built by a different process, or
    before a restart, cannot be executed elsewhere."""

    def __init__(self, *, plan_id: str) -> None:
        self.plan_id = plan_id
        super().__init__(f"No rebuild plan found with id {plan_id!r}")

    def to_dict(self) -> dict:
        return {"code": CatalogErrorCode.REBUILD_PLAN_NOT_FOUND.value, "plan_id": self.plan_id}


class SelectiveRebuildInProgressError(CatalogError):
    """Another rebuild is already running for the same replacement root
    -- distinct from CatalogRebuildInProgressError (the catalog-wide
    filesystem-reconciliation rebuild from v2.4). Guards against
    accidentally duplicating expensive selective-rebuild work, not
    against arbitrary concurrent catalog writes (which v2.4 already
    handles)."""

    def __init__(self, *, old_type: str, old_id: str) -> None:
        self.old_type = old_type
        self.old_id = old_id
        super().__init__(f"A selective rebuild rooted at {old_type}/{old_id} is already in progress")

    def to_dict(self) -> dict:
        return {"code": CatalogErrorCode.SELECTIVE_REBUILD_IN_PROGRESS.value, "old_type": self.old_type, "old_id": self.old_id}
