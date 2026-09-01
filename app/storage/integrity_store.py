"""Storage for integrity reports — deliberately separate from both RawStorage
and the validation-report store.

Same rationale as ValidationReportStore: integrity reports are derived
output, not source-of-truth data, and must never share a tree with
immutable raw artifacts. Kept as its own store (rather than folded into
ValidationReportStore) because it addresses a different artifact type with
its own lifecycle, even though the mechanics are the same.

Crash safety (v2.1): same staging -> atomic rename strategy as
ValidationReportStore — see app.storage.atomic.commit_staging_dir.

Only one backend exists today, so this is a concrete class rather than an
ABC + implementation pair, mirroring the same choice made for
ValidationReportStore.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.storage.atomic import commit_staging_dir, create_staging_dir, discard_staging_dir, write_manifest_file
from app.storage.errors import ArtifactDestinationExistsError

_REPORT_FILENAME = "report.json"
_STAGING_DIR_NAME = ".staging"


def _is_safe_path_component(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in (".", "..")


class IntegrityReportAlreadyExistsError(Exception):
    pass


class IntegrityReportStore:
    """Base contract other backends should follow if one is added later."""

    def write_report(self, *, ingestion_id: str, integrity_id: str, report: dict) -> str:
        raise NotImplementedError

    def find_reports(self, ingestion_id: str) -> list[dict]:
        raise NotImplementedError


class LocalIntegrityReportStore(IntegrityReportStore):
    def __init__(self, root: Path, *, fsync_enabled: bool = True) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._fsync_enabled = fsync_enabled

    def write_report(self, *, ingestion_id: str, integrity_id: str, report: dict) -> str:
        final_dir = self._root / ingestion_id / integrity_id
        if final_dir.exists():
            raise IntegrityReportAlreadyExistsError(f"Integrity report already exists: {final_dir}")

        # exist_ok=False (enforced inside create_staging_dir): integrity_id
        # is UUID4, so a collision should never happen — fail safe rather
        # than overwrite a prior report if it does.
        staging_dir = self._root / _STAGING_DIR_NAME / integrity_id
        create_staging_dir(
            staging_dir,
            operation_id=integrity_id,
            artifact_id=integrity_id,
            stage="integrity",
            final_destination=final_dir,
        )
        try:
            report_bytes = json.dumps(report, indent=2, sort_keys=True).encode("utf-8")
            write_manifest_file(staging_dir, _REPORT_FILENAME, report_bytes)
            commit_staging_dir(staging_dir, final_dir, fsync_enabled=self._fsync_enabled)
        except ArtifactDestinationExistsError as exc:
            discard_staging_dir(staging_dir)
            raise IntegrityReportAlreadyExistsError(f"Integrity report already exists: {final_dir}") from exc
        except Exception:
            discard_staging_dir(staging_dir)
            raise

        return f"file://{(final_dir / _REPORT_FILENAME).resolve()}"

    def find_reports(self, ingestion_id: str) -> list[dict]:
        if not _is_safe_path_component(ingestion_id):
            return []

        ingestion_dir = self._root / ingestion_id
        if not ingestion_dir.exists():
            return []

        reports = []
        for report_path in sorted(ingestion_dir.glob(f"*/{_REPORT_FILENAME}")):
            reports.append(json.loads(report_path.read_text(encoding="utf-8")))
        return reports
