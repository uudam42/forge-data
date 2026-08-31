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
