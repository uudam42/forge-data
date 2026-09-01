"""Local filesystem implementation of RawStorage.

Layout:

    {root}/{customer_id}/{session_id}/{ingestion_id}/original/{filename}
    {root}/{customer_id}/{session_id}/{ingestion_id}/manifest.json

Crash safety (v2.1): the raw bytes are streamed into
`{root}/.staging/{ingestion_id}/` first, hashed as they're written, and
only made visible at their final `{customer_id}/{session_id}/{ingestion_id}`
location via one atomic directory rename once fully written — a crash
mid-upload leaves nothing at the final location at all, not even an empty
directory. `write_manifest()` remains a second, separate commit onto the
now-existing final directory (see docs/DETAILED_GUIDE.md,
"finalized vs. run state": `save()` alone already produces a complete,
immutable, independently checksummed raw artifact; the manifest is
metadata *about* that artifact, and `find_manifest()` — the only way any
other stage discovers an ingestion — returns None until it exists, so a
crash between the two never lets a manifest-less ingestion be mistaken
for a valid one downstream).

Collisions (a repeat ingestion_id, which should never happen with UUID4)
fail with ArtifactAlreadyExistsError instead of silently overwriting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import BinaryIO

from app.storage.atomic import (
    commit_staging_dir,
    create_staging_dir,
    discard_staging_dir,
    fsync_dir,
    fsync_file,
)
from app.storage.base import ArtifactAlreadyExistsError, RawStorage, SavedArtifact
from app.storage.errors import ArtifactDestinationExistsError
from app.utils.hashing import ChunkedSha256

_ORIGINAL_DIR = "original"
_MANIFEST_FILENAME = "manifest.json"
_STAGING_DIR_NAME = ".staging"
_WRITE_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def _is_safe_path_component(value: str) -> bool:
    """Reject anything that could escape the customer/session/ingestion tree.

    ingestion_id arrives as an API path parameter (untrusted), and is used
    to build a glob pattern in find_manifest — it must never be allowed to
    contain a path separator or traverse directories.
    """
    return bool(value) and "/" not in value and "\\" not in value and value not in (".", "..")


class LocalRawStorage(RawStorage):
    def __init__(self, root: Path, *, fsync_enabled: bool = True) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._fsync_enabled = fsync_enabled

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
        if ingestion_dir.exists():
            # Fail fast, before consuming any of the upload stream — a
            # repeat ingestion_id should never happen with UUID4, but fail
            # safe rather than overwrite if it does.
            raise ArtifactAlreadyExistsError(f"Ingestion directory already exists: {ingestion_dir}")

        staging_dir = self._root / _STAGING_DIR_NAME / ingestion_id
        try:
            create_staging_dir(
                staging_dir,
                operation_id=ingestion_id,
                artifact_id=ingestion_id,
                stage="ingestion",
                final_destination=ingestion_dir,
            )
        except FileExistsError as exc:
            raise ArtifactAlreadyExistsError(f"Ingestion directory already exists: {ingestion_dir}") from exc

        original_dir = staging_dir / _ORIGINAL_DIR
        original_dir.mkdir(parents=True, exist_ok=True)
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
            # Fail safe: nothing partially written is ever left visible at
            # the final location -- the whole staging tree is discarded.
            discard_staging_dir(staging_dir)
            raise

        try:
            commit_staging_dir(staging_dir, ingestion_dir, fsync_enabled=self._fsync_enabled)
        except ArtifactDestinationExistsError as exc:
            discard_staging_dir(staging_dir)
            raise ArtifactAlreadyExistsError(f"Ingestion directory already exists: {ingestion_dir}") from exc

        return SavedArtifact(
            storage_uri=f"file://{(ingestion_dir / _ORIGINAL_DIR / filename).resolve()}",
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
        # never leaves a partial/corrupt manifest at the final path. This
        # is a single-file commit (unlike save()'s directory-staging
        # above) because it is always exactly one file replacing nothing.
        tmp_path = manifest_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        if self._fsync_enabled:
            fsync_file(tmp_path)
        tmp_path.replace(manifest_path)
        if self._fsync_enabled:
            fsync_file(manifest_path)
            fsync_dir(ingestion_dir)

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
