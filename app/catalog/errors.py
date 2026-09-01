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
