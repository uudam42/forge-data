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
import shutil
from pathlib import Path
from typing import BinaryIO

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
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _ingestion_dir(self, ingestion_id: str) -> Path:
        return self._root / ingestion_id

    def staging_dir(self, *, ingestion_id: str, normalization_id: str) -> Path:
        staging = self._ingestion_dir(ingestion_id) / f".tmp-{normalization_id}"
        # exist_ok=False: normalization_id is UUID4, so a collision should
        # never happen — fail safe rather than write into a stale directory.
        staging.mkdir(parents=True, exist_ok=False)
        return staging

    def commit(self, *, ingestion_id: str, normalization_id: str, staging_dir: Path) -> str:
        final_dir = self._ingestion_dir(ingestion_id) / normalization_id
        if final_dir.exists():
            raise NormalizedArtifactAlreadyExistsError(f"Normalization run already exists: {final_dir}")

        # Path.rename() is atomic when source and destination share a
        # filesystem, which they always do here (both under the same
        # per-ingestion directory) — this is what makes a completed run
        # appear all-at-once rather than becoming visible file-by-file.
        staging_dir.rename(final_dir)
        return f"file://{final_dir.resolve()}"

    def discard(self, staging_dir: Path) -> None:
        shutil.rmtree(staging_dir, ignore_errors=True)

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
