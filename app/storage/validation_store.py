"""Storage for validation reports — deliberately separate from RawStorage.

Validation reports are a distinct artifact type from immutable raw data:
they are derived output, not source-of-truth customer data, and Step 2 must
never be able to touch anything under RawStorage's tree. Keeping this as its
own small class (not a RawStorage implementation) makes that boundary
structural rather than a convention someone can accidentally violate.

Crash safety (v2.1): report.json is written into
`{root}/.staging/{validation_id}/` first, then published at
`{root}/{ingestion_id}/{validation_id}/` via one atomic rename — a crash
mid-write leaves nothing at the final location, never a directory with a
missing or truncated report.json. See
app.storage.atomic.commit_staging_dir.

Only one backend exists today, so this is a concrete class rather than an
ABC + implementation pair — add that split if/when a second backend
(S3, GCS, ...) is actually needed, mirroring RawStorage/LocalRawStorage.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.storage.atomic import commit_staging_dir, create_staging_dir, discard_staging_dir, write_manifest_file
from app.storage.errors import ArtifactDestinationExistsError

_REPORT_FILENAME = "report.json"
_STAGING_DIR_NAME = ".staging"


def _is_safe_path_component(value: str) -> bool:
    """Reject anything that could escape the ingestion_id directory tree.

    ingestion_id ultimately traces back to an API path parameter — it must
    never be allowed to contain a path separator or traverse directories
    when used to build a glob/lookup path.
    """
    return bool(value) and "/" not in value and "\\" not in value and value not in (".", "..")


class ValidationReportAlreadyExistsError(Exception):
    pass


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
    def __init__(self, root: Path, *, fsync_enabled: bool = True) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._fsync_enabled = fsync_enabled

    def write_report(self, *, ingestion_id: str, validation_id: str, report: dict) -> str:
        final_dir = self._root / ingestion_id / validation_id
        if final_dir.exists():
            raise ValidationReportAlreadyExistsError(f"Validation report already exists: {final_dir}")

        # exist_ok=False (enforced inside create_staging_dir): validation_id
        # is UUID4, so a collision should never happen — fail safe rather
        # than overwrite a prior report if it does.
        staging_dir = self._root / _STAGING_DIR_NAME / validation_id
        create_staging_dir(
            staging_dir,
            operation_id=validation_id,
            artifact_id=validation_id,
            stage="validation",
            final_destination=final_dir,
        )
        try:
            report_bytes = json.dumps(report, indent=2, sort_keys=True).encode("utf-8")
            write_manifest_file(staging_dir, _REPORT_FILENAME, report_bytes)
            commit_staging_dir(staging_dir, final_dir, fsync_enabled=self._fsync_enabled)
        except ArtifactDestinationExistsError as exc:
            discard_staging_dir(staging_dir)
            raise ValidationReportAlreadyExistsError(f"Validation report already exists: {final_dir}") from exc
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
