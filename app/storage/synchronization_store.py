"""Storage for synchronized artifacts — deliberately separate from every
other store (RawStorage, validation/integrity report stores, the
normalized-artifact store).

Unlike normalized artifacts (nested under their source ingestion_id), a
synchronization run has no single owning ingestion — it combines several —
so synchronized artifacts are keyed directly by synchronization_id alone:

    data/synchronized/<synchronization_id>/synchronized.jsonl
    data/synchronized/<synchronization_id>/manifest.json

Same staging -> atomic commit strategy as NormalizedArtifactStore: content
is written into a hidden staging directory first, then atomically renamed
into its final location only once fully written.

Only one backend exists today, so this is a concrete class rather than an
ABC + implementation pair, mirroring the same choice made throughout this
project's storage layer.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import BinaryIO

_MANIFEST_FILENAME = "manifest.json"


def _is_safe_path_component(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in (".", "..")


class SynchronizationArtifactAlreadyExistsError(Exception):
    pass


class SynchronizationArtifactStore:
    """Base contract other backends should follow if one is added later."""

    def staging_dir(self, *, synchronization_id: str) -> Path:
        raise NotImplementedError

    def commit(self, *, synchronization_id: str, staging_dir: Path) -> str:
        raise NotImplementedError

    def discard(self, staging_dir: Path) -> None:
        raise NotImplementedError

    def exists(self, *, synchronization_id: str) -> bool:
        raise NotImplementedError

    def artifact_path(self, *, synchronization_id: str, filename: str) -> str:
        raise NotImplementedError

    def manifest_path(self, *, synchronization_id: str) -> str:
        raise NotImplementedError

    def find_manifest(self, synchronization_id: str) -> dict | None:
        raise NotImplementedError

    def open_artifact(self, *, synchronization_id: str, filename: str) -> BinaryIO:
        raise NotImplementedError


class LocalSynchronizationArtifactStore(SynchronizationArtifactStore):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def staging_dir(self, *, synchronization_id: str) -> Path:
        staging = self._root / f".tmp-{synchronization_id}"
        # exist_ok=False: synchronization_id is UUID4, so a collision should
        # never happen — fail safe rather than write into a stale directory.
        staging.mkdir(parents=True, exist_ok=False)
        return staging

    def commit(self, *, synchronization_id: str, staging_dir: Path) -> str:
        final_dir = self._root / synchronization_id
        if final_dir.exists():
            raise SynchronizationArtifactAlreadyExistsError(
                f"Synchronization run already exists: {final_dir}"
            )

        # Path.rename() is atomic when source and destination share a
        # filesystem, which they always do here (both directly under root).
        staging_dir.rename(final_dir)
        return f"file://{final_dir.resolve()}"

    def discard(self, staging_dir: Path) -> None:
        shutil.rmtree(staging_dir, ignore_errors=True)

    def exists(self, *, synchronization_id: str) -> bool:
        return (self._root / synchronization_id).exists()

    def artifact_path(self, *, synchronization_id: str, filename: str) -> str:
        return str((self._root / synchronization_id / filename).resolve())

    def manifest_path(self, *, synchronization_id: str) -> str:
        return str((self._root / synchronization_id / _MANIFEST_FILENAME).resolve())

    def find_manifest(self, synchronization_id: str) -> dict | None:
        if not _is_safe_path_component(synchronization_id):
            return None
        manifest_path = self._root / synchronization_id / _MANIFEST_FILENAME
        if not manifest_path.exists():
            return None
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def open_artifact(self, *, synchronization_id: str, filename: str) -> BinaryIO:
        path = Path(self.artifact_path(synchronization_id=synchronization_id, filename=filename))
        return path.open("rb")
