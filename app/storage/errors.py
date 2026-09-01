"""Structured error codes and exceptions for the crash-safe storage layer
(v2.1 — Crash Safety & Atomic Artifacts).

These are storage-primitive errors, distinct from any single pipeline
stage's own domain errors — every store built on top of
`app.storage.atomic` raises from this module for staging/commit failures.
"""

from __future__ import annotations

from enum import Enum


class StorageErrorCode(str, Enum):
    STAGING_CREATE_FAILED = "STAGING_CREATE_FAILED"
    ARTIFACT_COMMIT_FAILED = "ARTIFACT_COMMIT_FAILED"
    ARTIFACT_DESTINATION_EXISTS = "ARTIFACT_DESTINATION_EXISTS"
    ARTIFACT_CHECKSUM_MISMATCH = "ARTIFACT_CHECKSUM_MISMATCH"
    STAGING_METADATA_INVALID = "STAGING_METADATA_INVALID"
    STALE_STAGING_FOUND = "STALE_STAGING_FOUND"
    ARTIFACT_NOT_FINALIZED = "ARTIFACT_NOT_FINALIZED"


class StorageLayerError(Exception):
    """Base class for crash-safety-layer failures."""

    code: StorageErrorCode

    def __init__(self, message: str) -> None:
        super().__init__(message)


class StagingCreateFailedError(StorageLayerError):
    code = StorageErrorCode.STAGING_CREATE_FAILED


class ArtifactCommitFailedError(StorageLayerError):
    code = StorageErrorCode.ARTIFACT_COMMIT_FAILED


class ArtifactDestinationExistsError(StorageLayerError):
    code = StorageErrorCode.ARTIFACT_DESTINATION_EXISTS


class ArtifactChecksumMismatchError(StorageLayerError):
    code = StorageErrorCode.ARTIFACT_CHECKSUM_MISMATCH


class StagingMetadataInvalidError(StorageLayerError):
    code = StorageErrorCode.STAGING_METADATA_INVALID


class StaleStagingFoundError(StorageLayerError):
    code = StorageErrorCode.STALE_STAGING_FOUND


class ArtifactNotFinalizedError(StorageLayerError):
    code = StorageErrorCode.ARTIFACT_NOT_FINALIZED
