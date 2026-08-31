"""Storage abstraction for immutable raw ingestion artifacts.

The ingestion service depends only on this interface. Concrete backends
(local filesystem now; S3/GCS/Azure Blob later) implement it without the
ingestion API or business logic ever changing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import BinaryIO


class StorageError(Exception):
    """Base class for storage-layer failures."""


class ArtifactAlreadyExistsError(StorageError):
    """Raised when a write would overwrite an existing immutable artifact."""


@dataclass(frozen=True)
class SavedArtifact:
    """Result of persisting a raw upload."""

    storage_uri: str
    size_bytes: int
    sha256: str


class RawStorage(ABC):
    """Abstraction over where and how raw ingested files are persisted.

    Implementations must guarantee immutability: once an artifact is saved
    at a given (customer_id, session_id, ingestion_id) location, a second
    save to the same location must fail rather than overwrite.
    """

    @abstractmethod
    def save(
        self,
        *,
        customer_id: str,
        session_id: str,
        ingestion_id: str,
        filename: str,
        stream: BinaryIO,
    ) -> SavedArtifact:
        """Persist the upload stream, hashing it as it is written.

        Raises ArtifactAlreadyExistsError if the destination already exists.
        """
        raise NotImplementedError

    @abstractmethod
    def exists(self, *, customer_id: str, session_id: str, ingestion_id: str) -> bool:
        """Return True if an ingestion directory already exists at this path."""
        raise NotImplementedError

    @abstractmethod
    def get_path(
        self, *, customer_id: str, session_id: str, ingestion_id: str, filename: str | None = None
    ) -> str:
        """Return a backend-specific locator (path or URI) for an artifact."""
        raise NotImplementedError

    @abstractmethod
    def write_manifest(
        self,
        *,
        customer_id: str,
        session_id: str,
        ingestion_id: str,
        manifest: dict,
    ) -> str:
        """Write the manifest for an ingestion event. Returns its storage_uri."""
        raise NotImplementedError

    @abstractmethod
    def find_manifest(self, ingestion_id: str) -> dict | None:
        """Locate an ingestion's manifest by ingestion_id alone.

        Returns None if no ingestion with this ID exists. Read-only — never
        used to derive a path for writing, so it cannot be used to mutate
        raw data.

        MVP note: callers only have ingestion_id (e.g. from a URL path
        parameter), not the customer_id/session_id needed to address the
        artifact directly, so backends must search rather than address
        directly. A production system would maintain a lookup index (e.g. a
        database) instead of a filesystem scan.
        """
        raise NotImplementedError

    @abstractmethod
    def open_raw(
        self, *, customer_id: str, session_id: str, ingestion_id: str, filename: str
    ) -> BinaryIO:
        """Open the immutable raw artifact for reading. Caller must close it.

        Read-only by contract — implementations must never open in a mode
        that could modify the underlying artifact.
        """
        raise NotImplementedError
