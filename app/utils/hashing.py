"""Chunked SHA-256 hashing utilities.

Files are hashed in fixed-size chunks so we never load an entire upload into
memory, which matters once large sensor/robotics payloads (bag files, zipped
multimodal sessions) are in play.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def sha256_of_path(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file already written to disk."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ChunkedSha256:
    """Incrementally hash bytes as they are streamed to storage.

    Lets the storage layer hash and write in a single pass over the upload
    stream instead of hashing before or after a separate write pass.
    """

    def __init__(self) -> None:
        self._digest = hashlib.sha256()

    def update(self, chunk: bytes) -> None:
        self._digest.update(chunk)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()
