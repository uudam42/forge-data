"""Storage for QC artifacts — deliberately separate from every other store,
mirroring the same staging -> atomic commit strategy used throughout this
project.

QC runs nest under their source transformation_id (one transformed
artifact can be QC'd multiple times — different profiles, different
thresholds, different baselines — each getting its own qc_id):

    data/qc/<transformation_id>/<qc_id>/report.json
    data/qc/<transformation_id>/<qc_id>/manifest.json

Step 8 never re-emits a copy of transformed.jsonl — it only ever writes
report.json + manifest.json here.

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


class QCArtifactAlreadyExistsError(Exception):
    pass


class QCReportStore:
    """Base contract other backends should follow if one is added later."""

    def staging_dir(self, *, transformation_id: str, qc_id: str) -> Path:
        raise NotImplementedError

    def commit(self, *, transformation_id: str, qc_id: str, staging_dir: Path) -> str:
        raise NotImplementedError

    def discard(self, staging_dir: Path) -> None:
        raise NotImplementedError

    def exists(self, *, transformation_id: str, qc_id: str) -> bool:
        raise NotImplementedError

    def report_path(self, *, transformation_id: str, qc_id: str) -> str:
        raise NotImplementedError

    def manifest_path(self, *, transformation_id: str, qc_id: str) -> str:
        raise NotImplementedError

    def find_manifest(self, *, transformation_id: str, qc_id: str) -> dict | None:
        raise NotImplementedError

    def find_manifest_by_qc_id(self, qc_id: str) -> dict | None:
        raise NotImplementedError


class LocalQCReportStore(QCReportStore):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _transformation_dir(self, transformation_id: str) -> Path:
        return self._root / transformation_id

    def staging_dir(self, *, transformation_id: str, qc_id: str) -> Path:
        staging = self._transformation_dir(transformation_id) / f".tmp-{qc_id}"
        # exist_ok=False: qc_id is UUID4, so a collision should never
        # happen — fail safe rather than write into a stale directory.
        staging.mkdir(parents=True, exist_ok=False)
        return staging

    def commit(self, *, transformation_id: str, qc_id: str, staging_dir: Path) -> str:
        final_dir = self._transformation_dir(transformation_id) / qc_id
        if final_dir.exists():
            raise QCArtifactAlreadyExistsError(f"QC run already exists: {final_dir}")

        # Path.rename() is atomic when source and destination share a
        # filesystem, which they always do here (both under the same
        # per-transformation directory).
        staging_dir.rename(final_dir)
        return f"file://{final_dir.resolve()}"

    def discard(self, staging_dir: Path) -> None:
        shutil.rmtree(staging_dir, ignore_errors=True)

    def exists(self, *, transformation_id: str, qc_id: str) -> bool:
        return (self._transformation_dir(transformation_id) / qc_id).exists()

    def report_path(self, *, transformation_id: str, qc_id: str) -> str:
        return str((self._transformation_dir(transformation_id) / qc_id / _REPORT_FILENAME).resolve())

    def manifest_path(self, *, transformation_id: str, qc_id: str) -> str:
        return str((self._transformation_dir(transformation_id) / qc_id / _MANIFEST_FILENAME).resolve())

    def find_manifest(self, *, transformation_id: str, qc_id: str) -> dict | None:
        if not _is_safe_path_component(transformation_id) or not _is_safe_path_component(qc_id):
            return None
        path = self._transformation_dir(transformation_id) / qc_id / _MANIFEST_FILENAME
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def find_manifest_by_qc_id(self, qc_id: str) -> dict | None:
        """Locate a QC manifest given only its qc_id — used to resolve an
        explicitly supplied baseline_qc_id, which may belong to a different
        transformation_id than the run currently being QC'd."""
        if not _is_safe_path_component(qc_id):
            return None
        matches = sorted(self._root.glob(f"*/{qc_id}/{_MANIFEST_FILENAME}"))
        if not matches:
            return None
        return json.loads(matches[0].read_text(encoding="utf-8"))
