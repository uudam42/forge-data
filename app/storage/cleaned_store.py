"""Storage for cleaned artifacts — deliberately separate from every other
store, mirroring the same staging -> atomic commit strategy used
throughout this project.

Cleaned artifacts nest under their source synchronization_id (one
synchronized artifact can be cleaned multiple times — different policies,
different configs — each getting its own cleaning_id):

    data/cleaned/<synchronization_id>/<cleaning_id>/cleaned.jsonl
    data/cleaned/<synchronization_id>/<cleaning_id>/report.json
    data/cleaned/<synchronization_id>/<cleaning_id>/manifest.json

Only one backend exists today, so this is a concrete class rather than an
ABC + implementation pair, mirroring the same choice made throughout this
project's storage layer.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

_MANIFEST_FILENAME = "manifest.json"
_REPORT_FILENAME = "report.json"


def _is_safe_path_component(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in (".", "..")


class CleanedArtifactAlreadyExistsError(Exception):
    pass


class CleanedArtifactStore:
    """Base contract other backends should follow if one is added later."""

    def staging_dir(self, *, synchronization_id: str, cleaning_id: str) -> Path:
        raise NotImplementedError

    def commit(self, *, synchronization_id: str, cleaning_id: str, staging_dir: Path) -> str:
        raise NotImplementedError

    def discard(self, staging_dir: Path) -> None:
        raise NotImplementedError

    def exists(self, *, synchronization_id: str, cleaning_id: str) -> bool:
        raise NotImplementedError

    def artifact_path(self, *, synchronization_id: str, cleaning_id: str, filename: str) -> str:
        raise NotImplementedError

    def manifest_path(self, *, synchronization_id: str, cleaning_id: str) -> str:
        raise NotImplementedError

    def report_path(self, *, synchronization_id: str, cleaning_id: str) -> str:
        raise NotImplementedError

    def find_manifest(self, *, synchronization_id: str, cleaning_id: str) -> dict | None:
        raise NotImplementedError

    def find_manifest_by_cleaning_id(self, cleaning_id: str) -> dict | None:
        raise NotImplementedError


class LocalCleanedArtifactStore(CleanedArtifactStore):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _sync_dir(self, synchronization_id: str) -> Path:
        return self._root / synchronization_id

    def staging_dir(self, *, synchronization_id: str, cleaning_id: str) -> Path:
        staging = self._sync_dir(synchronization_id) / f".tmp-{cleaning_id}"
        # exist_ok=False: cleaning_id is UUID4, so a collision should never
        # happen — fail safe rather than write into a stale directory.
        staging.mkdir(parents=True, exist_ok=False)
        return staging

    def commit(self, *, synchronization_id: str, cleaning_id: str, staging_dir: Path) -> str:
        final_dir = self._sync_dir(synchronization_id) / cleaning_id
        if final_dir.exists():
            raise CleanedArtifactAlreadyExistsError(f"Cleaning run already exists: {final_dir}")

        # Path.rename() is atomic when source and destination share a
        # filesystem, which they always do here (both under the same
        # per-synchronization directory).
        staging_dir.rename(final_dir)
        return f"file://{final_dir.resolve()}"

    def discard(self, staging_dir: Path) -> None:
        shutil.rmtree(staging_dir, ignore_errors=True)

    def exists(self, *, synchronization_id: str, cleaning_id: str) -> bool:
        return (self._sync_dir(synchronization_id) / cleaning_id).exists()

    def artifact_path(self, *, synchronization_id: str, cleaning_id: str, filename: str) -> str:
        return str((self._sync_dir(synchronization_id) / cleaning_id / filename).resolve())

    def manifest_path(self, *, synchronization_id: str, cleaning_id: str) -> str:
        return str(
            (self._sync_dir(synchronization_id) / cleaning_id / _MANIFEST_FILENAME).resolve()
        )

    def report_path(self, *, synchronization_id: str, cleaning_id: str) -> str:
        return str((self._sync_dir(synchronization_id) / cleaning_id / _REPORT_FILENAME).resolve())

    def find_manifest(self, *, synchronization_id: str, cleaning_id: str) -> dict | None:
        if not _is_safe_path_component(synchronization_id) or not _is_safe_path_component(cleaning_id):
            return None
        path = self._sync_dir(synchronization_id) / cleaning_id / _MANIFEST_FILENAME
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def find_manifest_by_cleaning_id(self, cleaning_id: str) -> dict | None:
        """Locate a cleaning manifest given only its cleaning_id, for callers
        (e.g. Step 7 transformation) that don't have the synchronization_id
        on hand. Read-only directory scan — mirrors the bare-ID lookup
        pattern used by other stores (e.g. normalized_store.find_manifest)."""
        if not _is_safe_path_component(cleaning_id):
            return None
        matches = sorted(self._root.glob(f"*/{cleaning_id}/{_MANIFEST_FILENAME}"))
        if not matches:
            return None
        return json.loads(matches[0].read_text(encoding="utf-8"))
