"""Ingestion business logic: SOURCE -> RECEIVE -> IDENTIFY -> HASH -> STORE RAW -> WRITE MANIFEST.

This module knows nothing about HTTP or the filesystem directly — it depends
only on the RawStorage abstraction, so swapping in S3/GCS/Azure later never
requires touching this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import BinaryIO

from app.core.config import Settings
from app.core.logging import get_logger
from app.ingestion.models import IngestionResponse, Manifest, resolve_customer_id
from app.storage.base import ArtifactAlreadyExistsError, RawStorage
from app.utils.filenames import extension_of, sanitize_filename
from app.utils.ids import generate_ingestion_id, generate_session_id

logger = get_logger("app.ingestion")

_PEEK_SIZE = 1024 * 1024  # 1 MiB — enough to detect an empty upload without buffering the file


class IngestionError(Exception):
    """Base class for ingestion validation failures (mapped to HTTP errors by the API layer)."""


class UnsupportedFileTypeError(IngestionError):
    pass


class EmptyFileError(IngestionError):
    pass


class FileTooLargeError(IngestionError):
    pass


class IngestionConflictError(IngestionError):
    pass


class _SizeLimitedReader:
    """Wraps a binary stream and raises FileTooLargeError once max_bytes is exceeded.

    Enforced during the read loop so an oversized upload is rejected mid-stream
    rather than after being fully buffered or fully written to disk.
    """

    def __init__(self, stream: BinaryIO, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._read_so_far = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        self._read_so_far += len(chunk)
        if self._read_so_far > self._max_bytes:
            raise FileTooLargeError(f"Upload exceeds maximum size of {self._max_bytes} bytes")
        return chunk


class _PrefixedStream:
    """Replays a buffered prefix before continuing to read the underlying stream.

    Lets the service peek at the first chunk (to detect an empty file) without
    losing those bytes or buffering the whole upload in memory.
    """

    def __init__(self, prefix: bytes, stream: BinaryIO) -> None:
        self._prefix = prefix
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        if self._prefix:
            if size < 0 or size >= len(self._prefix):
                chunk, self._prefix = self._prefix, b""
                return chunk
            chunk, self._prefix = self._prefix[:size], self._prefix[size:]
            return chunk
        return self._stream.read(size)


@dataclass
class UploadRequest:
    filename: str | None
    content_type: str | None
    stream: BinaryIO
    customer_id: str | None
    device_id: str | None
    session_id: str | None
    source_type: str | None
    notes: str | None


class IngestionService:
    def __init__(self, storage: RawStorage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings

    def ingest(self, request: UploadRequest) -> IngestionResponse:
        safe_filename = sanitize_filename(request.filename)
        extension = extension_of(safe_filename)

        ingestion_id = generate_ingestion_id()
        session_id = (
            request.session_id.strip()
            if request.session_id and request.session_id.strip()
            else generate_session_id()
        )
        customer_id = resolve_customer_id(request.customer_id)

        logger.info(
            "INGESTION_STARTED ingestion_id=%s session_id=%s customer_id=%s filename=%s",
            ingestion_id,
            session_id,
            customer_id,
            safe_filename,
        )

        if extension not in self._settings.ALLOWED_EXTENSIONS:
            logger.warning(
                "INGESTION_FAILED ingestion_id=%s session_id=%s reason=unsupported_extension extension=%s",
                ingestion_id,
                session_id,
                extension,
            )
            raise UnsupportedFileTypeError(
                f"Unsupported file extension '{extension}'. "
                f"Allowed: {', '.join(self._settings.ALLOWED_EXTENSIONS)}"
            )

        if self._storage.exists(
            customer_id=customer_id, session_id=session_id, ingestion_id=ingestion_id
        ):
            logger.error(
                "INGESTION_FAILED ingestion_id=%s session_id=%s reason=collision",
                ingestion_id,
                session_id,
            )
            raise IngestionConflictError("Ingestion ID collision detected")

        first_chunk = request.stream.read(_PEEK_SIZE)
        if not first_chunk:
            logger.warning(
                "INGESTION_FAILED ingestion_id=%s session_id=%s reason=empty_file",
                ingestion_id,
                session_id,
            )
            raise EmptyFileError("Uploaded file is empty")

        stream_for_storage = _PrefixedStream(first_chunk, request.stream)
        limited_stream = _SizeLimitedReader(stream_for_storage, self._settings.max_upload_size_bytes)

        try:
            saved = self._storage.save(
                customer_id=customer_id,
                session_id=session_id,
                ingestion_id=ingestion_id,
                filename=safe_filename,
                stream=limited_stream,
            )
        except FileTooLargeError:
            logger.warning(
                "INGESTION_FAILED ingestion_id=%s session_id=%s reason=too_large",
                ingestion_id,
                session_id,
            )
            raise
        except ArtifactAlreadyExistsError as exc:
            logger.error(
                "INGESTION_FAILED ingestion_id=%s session_id=%s reason=already_exists",
                ingestion_id,
                session_id,
            )
            raise IngestionConflictError(str(exc)) from exc

        ingested_at = datetime.now(timezone.utc)

        manifest = Manifest(
            ingestion_id=ingestion_id,
            session_id=session_id,
            customer_id=customer_id,
            device_id=request.device_id,
            source_type=request.source_type,
            notes=request.notes,
            original_filename=safe_filename,
            content_type=request.content_type,
            size_bytes=saved.size_bytes,
            sha256=saved.sha256,
            ingested_at=ingested_at,
            storage_uri=saved.storage_uri,
        )

        self._storage.write_manifest(
            customer_id=customer_id,
            session_id=session_id,
            ingestion_id=ingestion_id,
            manifest=manifest.model_dump(mode="json"),
        )

        logger.info(
            "INGESTION_COMPLETED ingestion_id=%s session_id=%s customer_id=%s sha256=%s size_bytes=%d",
            ingestion_id,
            session_id,
            customer_id,
            saved.sha256,
            saved.size_bytes,
        )

        return IngestionResponse(
            ingestion_id=ingestion_id,
            session_id=session_id,
            customer_id=customer_id,
            device_id=request.device_id,
            source_type=request.source_type,
            original_filename=safe_filename,
            content_type=request.content_type,
            size_bytes=saved.size_bytes,
            sha256=saved.sha256,
            storage_uri=saved.storage_uri,
            status="stored",
        )
