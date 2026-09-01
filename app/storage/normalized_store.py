"""Storage for normalized artifacts — deliberately separate from RawStorage,
the validation-report store, and the integrity-report store.

Unlike those two report stores, a normalization run produces a genuinely
new DATA artifact, not just a report about existing data, so committing it
needs an atomic write strategy: content is written into a hidden staging
directory first, then atomically renamed into its final, immutable location
only once fully written. A partially-written run must never be discoverable
under its final normalization_id — that's what staging_dir()/commit()/
discard() enforce.

Only one backend exists today, so this is a concrete class rather than an
ABC + implementation pair, mirroring the same choice already made for
ValidationReportStore and IntegrityReportStore.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import BinaryIO

from app.storage.atomic import commit_staging_dir, create_staging_dir, discard_staging_dir
from app.storage.errors import ArtifactDestinationExistsError

_MANIFEST_FILENAME = "manifest.json"


def _is_safe_path_component(value: str) -> bool:
    """Reject anything that could escape the ingestion_id/normalization_id
    directory tree — normalization_id ultimately traces back to an API
    request body (untrusted) when used for a bare lookup (find_manifest).
    """
    return bool(value) and "/" not in value and "\\" not in value and value not in (".", "..")


class NormalizedArtifactAlreadyExistsError(Exception):
    pass


class NormalizedArtifactStore:
    """Base contract other backends should follow if one is added later."""

    def staging_dir(self, *, ingestion_id: str, normalization_id: str) -> Path:
        raise NotImplementedError

    def commit(self, *, ingestion_id: str, normalization_id: str, staging_dir: Path) -> str:
        raise NotImplementedError

    def discard(self, staging_dir: Path) -> None:
        raise NotImplementedError

    def exists(self, *, ingestion_id: str, normalization_id: str) -> bool:
        raise NotImplementedError

    def artifact_path(self, *, ingestion_id: str, normalization_id: str, filename: str) -> str:
        raise NotImplementedError

    def manifest_path(self, *, ingestion_id: str, normalization_id: str) -> str:
        raise NotImplementedError

    def find_manifest(self, normalization_id: str) -> dict | None:
        """Locate a normalization manifest by normalization_id alone.

        Read-only lookup for consumers (e.g. Step 5) that only have a
        normalization_id, not the ingestion_id it was produced under.
        Returns None if no committed run with this ID exists. MVP note:
        this is a directory scan, not an index — same documented
        limitation as RawStorage.find_manifest() and
        ValidationReportStore.find_reports().
        """
        raise NotImplementedError

    def open_artifact(self, *, ingestion_id: str, normalization_id: str, filename: str) -> BinaryIO:
        """Open a committed normalized artifact for reading. Caller must close it.

        Read-only by contract, mirroring RawStorage.open_raw().
        """
        raise NotImplementedError


class LocalNormalizedArtifactStore(NormalizedArtifactStore):
    def __init__(self, root: Path, *, fsync_enabled: bool = True) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._fsync_enabled = fsync_enabled

    def _ingestion_dir(self, ingestion_id: str) -> Path:
        return self._root / ingestion_id

    def staging_dir(self, *, ingestion_id: str, normalization_id: str) -> Path:
        staging = self._ingestion_dir(ingestion_id) / f".tmp-{normalization_id}"
        final_dir = self._ingestion_dir(ingestion_id) / normalization_id
        # exist_ok=False (enforced inside create_staging_dir): normalization_id
        # is UUID4, so a collision should never happen — fail safe rather
        # than write into a stale directory.
        create_staging_dir(
            staging,
            operation_id=normalization_id,
            artifact_id=normalization_id,
            stage="normalization",
            final_destination=final_dir,
        )
        return staging

    def commit(self, *, ingestion_id: str, normalization_id: str, staging_dir: Path) -> str:
        final_dir = self._ingestion_dir(ingestion_id) / normalization_id
        # commit_staging_dir fsyncs staged files, then does the same
        # same-filesystem Path.rename() this store always used — atomic,
        # so a completed run appears all-at-once, never file-by-file.
        try:
            commit_staging_dir(staging_dir, final_dir, fsync_enabled=self._fsync_enabled)
        except ArtifactDestinationExistsError as exc:
            raise NormalizedArtifactAlreadyExistsError(f"Normalization run already exists: {final_dir}") from exc
        return f"file://{final_dir.resolve()}"

    def discard(self, staging_dir: Path) -> None:
        discard_staging_dir(staging_dir)

    def exists(self, *, ingestion_id: str, normalization_id: str) -> bool:
        return (self._ingestion_dir(ingestion_id) / normalization_id).exists()

    def artifact_path(self, *, ingestion_id: str, normalization_id: str, filename: str) -> str:
        return str((self._ingestion_dir(ingestion_id) / normalization_id / filename).resolve())

    def manifest_path(self, *, ingestion_id: str, normalization_id: str) -> str:
        return str(
            (self._ingestion_dir(ingestion_id) / normalization_id / _MANIFEST_FILENAME).resolve()
        )

    def find_manifest(self, normalization_id: str) -> dict | None:
        if not _is_safe_path_component(normalization_id):
            return None

        matches = sorted(self._root.glob(f"*/{normalization_id}/{_MANIFEST_FILENAME}"))
        if not matches:
            return None
        return json.loads(matches[0].read_text(encoding="utf-8"))

    def open_artifact(self, *, ingestion_id: str, normalization_id: str, filename: str) -> BinaryIO:
        path = Path(
            self.artifact_path(ingestion_id=ingestion_id, normalization_id=normalization_id, filename=filename)
        )
        return path.open("rb")
