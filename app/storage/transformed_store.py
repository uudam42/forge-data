"""Storage for transformed artifacts — deliberately separate from every
other store, mirroring the same staging -> atomic commit strategy used
throughout this project.

Transformed artifacts nest under their source cleaning_id (one cleaned
artifact can be transformed multiple times — different profiles, different
window configs — each getting its own transformation_id):

    data/transformed/<cleaning_id>/<transformation_id>/transformed.jsonl
    data/transformed/<cleaning_id>/<transformation_id>/report.json
    data/transformed/<cleaning_id>/<transformation_id>/manifest.json

Only one backend exists today, so this is a concrete class rather than an
ABC + implementation pair, mirroring the same choice made throughout this
project's storage layer.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.storage.atomic import commit_staging_dir, create_staging_dir, discard_staging_dir
from app.storage.errors import ArtifactDestinationExistsError

_MANIFEST_FILENAME = "manifest.json"
_REPORT_FILENAME = "report.json"


def _is_safe_path_component(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in (".", "..")


class TransformedArtifactAlreadyExistsError(Exception):
    pass


class TransformedArtifactStore:
    """Base contract other backends should follow if one is added later."""

    def staging_dir(self, *, cleaning_id: str, transformation_id: str) -> Path:
        raise NotImplementedError

    def commit(self, *, cleaning_id: str, transformation_id: str, staging_dir: Path) -> str:
        raise NotImplementedError

    def discard(self, staging_dir: Path) -> None:
        raise NotImplementedError

    def exists(self, *, cleaning_id: str, transformation_id: str) -> bool:
        raise NotImplementedError

    def artifact_path(self, *, cleaning_id: str, transformation_id: str, filename: str) -> str:
        raise NotImplementedError

    def manifest_path(self, *, cleaning_id: str, transformation_id: str) -> str:
        raise NotImplementedError

    def report_path(self, *, cleaning_id: str, transformation_id: str) -> str:
        raise NotImplementedError

    def find_manifest(self, *, cleaning_id: str, transformation_id: str) -> dict | None:
        raise NotImplementedError

    def find_manifest_by_transformation_id(self, transformation_id: str) -> dict | None:
        raise NotImplementedError


class LocalTransformedArtifactStore(TransformedArtifactStore):
    def __init__(self, root: Path, *, fsync_enabled: bool = True) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._fsync_enabled = fsync_enabled

    def _cleaning_dir(self, cleaning_id: str) -> Path:
        return self._root / cleaning_id

    def staging_dir(self, *, cleaning_id: str, transformation_id: str) -> Path:
        staging = self._cleaning_dir(cleaning_id) / f".tmp-{transformation_id}"
        final_dir = self._cleaning_dir(cleaning_id) / transformation_id
        # exist_ok=False (enforced inside create_staging_dir):
        # transformation_id is UUID4, so a collision should never happen —
        # fail safe rather than write into a stale directory.
        create_staging_dir(
            staging,
            operation_id=transformation_id,
            artifact_id=transformation_id,
            stage="transformation",
            final_destination=final_dir,
        )
        return staging

    def commit(self, *, cleaning_id: str, transformation_id: str, staging_dir: Path) -> str:
        final_dir = self._cleaning_dir(cleaning_id) / transformation_id
        try:
            commit_staging_dir(staging_dir, final_dir, fsync_enabled=self._fsync_enabled)
        except ArtifactDestinationExistsError as exc:
            raise TransformedArtifactAlreadyExistsError(f"Transformation run already exists: {final_dir}") from exc
        return f"file://{final_dir.resolve()}"

    def discard(self, staging_dir: Path) -> None:
        discard_staging_dir(staging_dir)

    def exists(self, *, cleaning_id: str, transformation_id: str) -> bool:
        return (self._cleaning_dir(cleaning_id) / transformation_id).exists()

    def artifact_path(self, *, cleaning_id: str, transformation_id: str, filename: str) -> str:
        return str((self._cleaning_dir(cleaning_id) / transformation_id / filename).resolve())

    def manifest_path(self, *, cleaning_id: str, transformation_id: str) -> str:
        return str(
            (self._cleaning_dir(cleaning_id) / transformation_id / _MANIFEST_FILENAME).resolve()
        )

    def report_path(self, *, cleaning_id: str, transformation_id: str) -> str:
        return str((self._cleaning_dir(cleaning_id) / transformation_id / _REPORT_FILENAME).resolve())

    def find_manifest(self, *, cleaning_id: str, transformation_id: str) -> dict | None:
        if not _is_safe_path_component(cleaning_id) or not _is_safe_path_component(transformation_id):
            return None
        path = self._cleaning_dir(cleaning_id) / transformation_id / _MANIFEST_FILENAME
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def find_manifest_by_transformation_id(self, transformation_id: str) -> dict | None:
        """Locate a transformation manifest given only its transformation_id,
        for callers (e.g. Step 8 QC) that don't have the cleaning_id on
        hand. Read-only directory scan — mirrors the bare-ID lookup pattern
        used by other stores (e.g. cleaned_store.find_manifest_by_cleaning_id)."""
        if not _is_safe_path_component(transformation_id):
            return None
        matches = sorted(self._root.glob(f"*/{transformation_id}/{_MANIFEST_FILENAME}"))
        if not matches:
            return None
        return json.loads(matches[0].read_text(encoding="utf-8"))
