"""Local filesystem implementation of RawStorage.

Layout:

    {root}/{customer_id}/{session_id}/{ingestion_id}/original/{filename}
    {root}/{customer_id}/{session_id}/{ingestion_id}/manifest.json

The ingestion_id directory is created with exist_ok=False, which acts as the
immutability guard: a second attempt to save into the same ingestion_id
fails instead of silently overwriting the first artifact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import BinaryIO

from app.storage.base import ArtifactAlreadyExistsError, RawStorage, SavedArtifact
from app.utils.hashing import ChunkedSha256

_ORIGINAL_DIR = "original"
_MANIFEST_FILENAME = "manifest.json"
_WRITE_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def _is_safe_path_component(value: str) -> bool:
    """Reject anything that could escape the customer/session/ingestion tree.

    ingestion_id arrives as an API path parameter (untrusted), and is used
    to build a glob pattern in find_manifest — it must never be allowed to
    contain a path separator or traverse directories.
    """
    return bool(value) and "/" not in value and "\\" not in value and value not in (".", "..")


class LocalRawStorage(RawStorage):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _ingestion_dir(self, *, customer_id: str, session_id: str, ingestion_id: str) -> Path:
        return self._root / customer_id / session_id / ingestion_id

    def exists(self, *, customer_id: str, session_id: str, ingestion_id: str) -> bool:
        return self._ingestion_dir(
            customer_id=customer_id, session_id=session_id, ingestion_id=ingestion_id
        ).exists()

    def get_path(
        self, *, customer_id: str, session_id: str, ingestion_id: str, filename: str | None = None
    ) -> str:
        directory = self._ingestion_dir(
            customer_id=customer_id, session_id=session_id, ingestion_id=ingestion_id
        )
        if filename is None:
            return str(directory)
        return str(directory / _ORIGINAL_DIR / filename)

    def save(
        self,
        *,
        customer_id: str,
        session_id: str,
        ingestion_id: str,
        filename: str,
        stream: BinaryIO,
    ) -> SavedArtifact:
        ingestion_dir = self._ingestion_dir(
            customer_id=customer_id, session_id=session_id, ingestion_id=ingestion_id
        )

        try:
            # exist_ok=False is the immutability guard: a repeat ingestion_id
            # (should never happen with UUID4, but fail safe if it does)
            # raises instead of silently overwriting prior data.
            ingestion_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise ArtifactAlreadyExistsError(
                f"Ingestion directory already exists: {ingestion_dir}"
            ) from exc

        original_dir = ingestion_dir / _ORIGINAL_DIR
        original_dir.mkdir(parents=True, exist_ok=False)
        destination = original_dir / filename

        digest = ChunkedSha256()
        size_bytes = 0
        try:
            with destination.open("wb") as out:
                while chunk := stream.read(_WRITE_CHUNK_SIZE):
                    digest.update(chunk)
                    size_bytes += len(chunk)
                    out.write(chunk)
        except Exception:
            # Fail safe: don't leave a partially-written "immutable" artifact
            # or an empty ingestion directory behind.
            destination.unlink(missing_ok=True)
            original_dir.rmdir()
            ingestion_dir.rmdir()
            raise

        return SavedArtifact(
            storage_uri=f"file://{destination.resolve()}",
            size_bytes=size_bytes,
            sha256=digest.hexdigest(),
        )

    def write_manifest(
        self,
        *,
        customer_id: str,
        session_id: str,
        ingestion_id: str,
        manifest: dict,
    ) -> str:
        ingestion_dir = self._ingestion_dir(
            customer_id=customer_id, session_id=session_id, ingestion_id=ingestion_id
        )
        manifest_path = ingestion_dir / _MANIFEST_FILENAME

        if manifest_path.exists():
            raise ArtifactAlreadyExistsError(f"Manifest already exists: {manifest_path}")

        # Write to a temp file then atomically rename, so a crash mid-write
        # never leaves a partial/corrupt manifest at the final path.
        tmp_path = manifest_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(manifest_path)

        return f"file://{manifest_path.resolve()}"

    def find_manifest(self, ingestion_id: str) -> dict | None:
        if not _is_safe_path_component(ingestion_id):
            return None

        matches = sorted(self._root.glob(f"*/*/{ingestion_id}/{_MANIFEST_FILENAME}"))
        if not matches:
            return None
        return json.loads(matches[0].read_text(encoding="utf-8"))

    def open_raw(
        self, *, customer_id: str, session_id: str, ingestion_id: str, filename: str
    ) -> BinaryIO:
        path = Path(
            self.get_path(
                customer_id=customer_id,
                session_id=session_id,
                ingestion_id=ingestion_id,
                filename=filename,
            )
        )
        return path.open("rb")
