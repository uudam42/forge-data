"""Storage for dataset package artifacts — deliberately separate from
every other store, mirroring the same staging -> atomic commit strategy
used throughout this project. Never reuses RawStorage or the transformed
artifact store for writes — Step 9 owns its own tree exclusively.

Packages nest under their source transformation_id (one transformed
artifact can be packaged multiple times — different QC runs, different
split configs — each getting its own package_id):

    data/packages/<transformation_id>/<package_id>/train.jsonl
    data/packages/<transformation_id>/<package_id>/validation.jsonl
    data/packages/<transformation_id>/<package_id>/test.jsonl
    data/packages/<transformation_id>/<package_id>/split_index.jsonl
    data/packages/<transformation_id>/<package_id>/report.json
    data/packages/<transformation_id>/<package_id>/manifest.json
    data/packages/<transformation_id>/<package_id>/optional/train.parquet (...)

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


class PackageAlreadyExistsError(Exception):
    pass


class DatasetPackageStore:
    """Base contract other backends should follow if one is added later."""

    def staging_dir(self, *, transformation_id: str, package_id: str) -> Path:
        raise NotImplementedError

    def commit(self, *, transformation_id: str, package_id: str, staging_dir: Path) -> str:
        raise NotImplementedError

    def discard(self, staging_dir: Path) -> None:
        raise NotImplementedError

    def exists(self, *, transformation_id: str, package_id: str) -> bool:
        raise NotImplementedError

    def artifact_path(self, *, transformation_id: str, package_id: str, filename: str) -> str:
        raise NotImplementedError

    def manifest_path(self, *, transformation_id: str, package_id: str) -> str:
        raise NotImplementedError

    def report_path(self, *, transformation_id: str, package_id: str) -> str:
        raise NotImplementedError

    def find_manifest(self, *, transformation_id: str, package_id: str) -> dict | None:
        raise NotImplementedError


class LocalDatasetPackageStore(DatasetPackageStore):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _transformation_dir(self, transformation_id: str) -> Path:
        return self._root / transformation_id

    def staging_dir(self, *, transformation_id: str, package_id: str) -> Path:
        staging = self._transformation_dir(transformation_id) / f".tmp-{package_id}"
        # exist_ok=False: package_id is UUID4, so a collision should never
        # happen — fail safe rather than write into a stale directory.
        staging.mkdir(parents=True, exist_ok=False)
        return staging

    def commit(self, *, transformation_id: str, package_id: str, staging_dir: Path) -> str:
        final_dir = self._transformation_dir(transformation_id) / package_id
        if final_dir.exists():
            raise PackageAlreadyExistsError(f"Package already exists: {final_dir}")

        # Path.rename() is atomic when source and destination share a
        # filesystem, which they always do here (both under the same
        # per-transformation directory).
        staging_dir.rename(final_dir)
        return f"file://{final_dir.resolve()}"

    def discard(self, staging_dir: Path) -> None:
        shutil.rmtree(staging_dir, ignore_errors=True)

    def exists(self, *, transformation_id: str, package_id: str) -> bool:
        return (self._transformation_dir(transformation_id) / package_id).exists()

    def artifact_path(self, *, transformation_id: str, package_id: str, filename: str) -> str:
        return str((self._transformation_dir(transformation_id) / package_id / filename).resolve())

    def manifest_path(self, *, transformation_id: str, package_id: str) -> str:
        return str(
            (self._transformation_dir(transformation_id) / package_id / _MANIFEST_FILENAME).resolve()
        )

    def report_path(self, *, transformation_id: str, package_id: str) -> str:
        return str((self._transformation_dir(transformation_id) / package_id / _REPORT_FILENAME).resolve())

    def find_manifest(self, *, transformation_id: str, package_id: str) -> dict | None:
        if not _is_safe_path_component(transformation_id) or not _is_safe_path_component(package_id):
            return None
        path = self._transformation_dir(transformation_id) / package_id / _MANIFEST_FILENAME
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
