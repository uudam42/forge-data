"""Storage for validation reports — deliberately separate from RawStorage.

Validation reports are a distinct artifact type from immutable raw data:
they are derived output, not source-of-truth customer data, and Step 2 must
never be able to touch anything under RawStorage's tree. Keeping this as its
own small class (not a RawStorage implementation) makes that boundary
structural rather than a convention someone can accidentally violate.

Only one backend exists today, so this is a concrete class rather than an
ABC + implementation pair — add that split if/when a second backend
(S3, GCS, ...) is actually needed, mirroring RawStorage/LocalRawStorage.
"""

from __future__ import annotations

import json
from pathlib import Path

_REPORT_FILENAME = "report.json"


def _is_safe_path_component(value: str) -> bool:
    """Reject anything that could escape the ingestion_id directory tree.

    ingestion_id ultimately traces back to an API path parameter — it must
    never be allowed to contain a path separator or traverse directories
    when used to build a glob/lookup path.
    """
    return bool(value) and "/" not in value and "\\" not in value and value not in (".", "..")


class ValidationReportStore:
    """Base contract other backends should follow if one is added later."""

    def write_report(self, *, ingestion_id: str, validation_id: str, report: dict) -> str:
        raise NotImplementedError

    def find_reports(self, ingestion_id: str) -> list[dict]:
        """Return every persisted validation report for this ingestion_id.

        Read-only lookup, isolated here so callers (e.g. IntegrityService)
        never glob the filesystem themselves. MVP note: this is a directory
        scan, not an index — see README for the same limitation already
        documented for RawStorage.find_manifest().
        """
        raise NotImplementedError


class LocalValidationReportStore(ValidationReportStore):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def write_report(self, *, ingestion_id: str, validation_id: str, report: dict) -> str:
        report_dir = self._root / ingestion_id / validation_id
        # exist_ok=False: validation_id is UUID4, so a collision should never
        # happen — fail safe rather than overwrite a prior report if it does.
        report_dir.mkdir(parents=True, exist_ok=False)

        report_path = report_dir / _REPORT_FILENAME
        tmp_path = report_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(report_path)

        return f"file://{report_path.resolve()}"

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
