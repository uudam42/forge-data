"""Storage for integrity reports — deliberately separate from both RawStorage
and the validation-report store.

Same rationale as ValidationReportStore: integrity reports are derived
output, not source-of-truth data, and must never share a tree with
immutable raw artifacts. Kept as its own store (rather than folded into
ValidationReportStore) because it addresses a different artifact type with
its own lifecycle, even though the mechanics are the same.

Only one backend exists today, so this is a concrete class rather than an
ABC + implementation pair, mirroring the same choice made for
ValidationReportStore.
"""

from __future__ import annotations

import json
from pathlib import Path

_REPORT_FILENAME = "report.json"


def _is_safe_path_component(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in (".", "..")


class IntegrityReportStore:
    """Base contract other backends should follow if one is added later."""

    def write_report(self, *, ingestion_id: str, integrity_id: str, report: dict) -> str:
        raise NotImplementedError

    def find_reports(self, ingestion_id: str) -> list[dict]:
        raise NotImplementedError


class LocalIntegrityReportStore(IntegrityReportStore):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def write_report(self, *, ingestion_id: str, integrity_id: str, report: dict) -> str:
        report_dir = self._root / ingestion_id / integrity_id
        # exist_ok=False: integrity_id is UUID4, so a collision should never
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
