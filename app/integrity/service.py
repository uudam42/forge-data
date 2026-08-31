"""Integrity business logic: VALIDATION REPORT -> INTEGRITY CHECKS -> INTEGRITY REPORT.

Step 2 answers "is this record structurally valid?" (right fields, right
types, parseable timestamps). Step 3 answers a different question: "are
these structurally valid values plausible and internally consistent?" —
GPS coordinates within Earth's bounds, non-negative speed, non-decreasing
timestamps, sensor readings within a plausible range. It never mutates,
cleans, or repairs anything; it only reports.

This module never opens the raw file for writing, never touches
manifest.json, and never touches a validation report — it only reads all
three. The API route stays thin; all orchestration (resolve ingestion,
verify Step 2 lineage, select checker, build + persist the report) lives
here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrity import records
from app.integrity.checks.base import IntegrityIssueAccumulator
from app.integrity.models import (
    IntegrityErrorCode,
    IntegrityIssue,
    IntegrityReport,
    IntegrityResponse,
    IntegritySeverity,
    IntegrityStatus,
    SchemaRef,
)
from app.integrity.registry import IntegrityCheckerRegistry
from app.storage.base import RawStorage
from app.storage.integrity_store import IntegrityReportStore
from app.storage.validation_store import ValidationReportStore
from app.utils.filenames import extension_of
from app.utils.ids import generate_integrity_id
from app.validation.schemas.registry import SchemaNotFoundError as SchemaRegistryNotFoundError
from app.validation.schemas.registry import SchemaRegistry

logger = get_logger("app.integrity")


class IntegrityError(Exception):
    """Base class for integrity-service failures (mapped to HTTP errors by the API layer)."""


class IngestionNotFoundError(IntegrityError):
    pass


class SchemaNotFoundError(IntegrityError):
    pass


class NoMatchingValidationReportError(IntegrityError):
    pass


class ValidationNotPassedError(IntegrityError):
    pass


class UnsupportedIntegrityFileTypeError(IntegrityError):
    pass


class UnsupportedIntegrityCheckerError(IntegrityError):
    pass


class IntegrityService:
    def __init__(
        self,
        *,
        storage: RawStorage,
        schema_registry: SchemaRegistry,
        validation_report_store: ValidationReportStore,
        checker_registry: IntegrityCheckerRegistry,
        report_store: IntegrityReportStore,
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._schema_registry = schema_registry
        self._validation_report_store = validation_report_store
        self._checker_registry = checker_registry
        self._report_store = report_store
        self._settings = settings

    def run(self, *, ingestion_id: str, schema_name: str, schema_version: str) -> IntegrityResponse:
        manifest = self._storage.find_manifest(ingestion_id)
        if manifest is None:
            raise IngestionNotFoundError(f"No ingestion found with ingestion_id='{ingestion_id}'")

        try:
            schema = self._schema_registry.get(schema_name=schema_name, schema_version=schema_version)
        except SchemaRegistryNotFoundError as exc:
            raise SchemaNotFoundError(str(exc)) from exc

        validation_report = self._find_matching_validation_report(
            ingestion_id=ingestion_id,
            schema_name=schema_name,
            schema_version=schema_version,
            raw_sha256=manifest["sha256"],
        )
        if validation_report is None:
            raise NoMatchingValidationReportError(
                f"No validation report found for ingestion_id='{ingestion_id}' matching "
                f"schema '{schema_name}' v{schema_version} and the current raw file "
                f"(sha256={manifest['sha256']}). Run schema validation first, or re-validate "
                "if the raw file's checksum has changed since it was last validated."
            )
        if validation_report["status"] != "passed":
            raise ValidationNotPassedError(
                f"Validation report {validation_report['validation_id']} for ingestion_id="
                f"'{ingestion_id}' has status='{validation_report['status']}'; integrity checks "
                "require a passing validation report."
            )

        extension = extension_of(manifest["original_filename"])
        if not records.supports(extension):
            raise UnsupportedIntegrityFileTypeError(
                f"Integrity checking is not supported for file type '{extension}'"
            )
        if not self._checker_registry.supports(schema_name):
            raise UnsupportedIntegrityCheckerError(
                f"No integrity checker registered for schema '{schema_name}'"
            )
        checker = self._checker_registry.get(schema_name)

        integrity_id = generate_integrity_id()
        logger.info(
            "INTEGRITY_STARTED integrity_id=%s ingestion_id=%s validation_id=%s "
            "schema_name=%s schema_version=%s",
            integrity_id,
            ingestion_id,
            validation_report["validation_id"],
            schema_name,
            schema_version,
        )

        accumulator = IntegrityIssueAccumulator(max_issues=self._settings.MAX_INTEGRITY_ISSUES)

        try:
            with self._storage.open_raw(
                customer_id=manifest["customer_id"],
                session_id=manifest["session_id"],
                ingestion_id=manifest["ingestion_id"],
                filename=manifest["original_filename"],
            ) as stream:
                record_stream = records.iter_records(stream, extension)
                counts = checker.check_stream(record_stream, accumulator)
        except Exception:
            logger.error(
                "INTEGRITY_FAILED integrity_id=%s ingestion_id=%s validation_id=%s "
                "schema_name=%s schema_version=%s reason=checker_exception",
                integrity_id,
                ingestion_id,
                validation_report["validation_id"],
                schema_name,
                schema_version,
            )
            raise

        if counts.total_records == 0:
            accumulator.add(
                IntegrityIssue(
                    record_number=None,
                    field=None,
                    code=IntegrityErrorCode.EMPTY_DATASET,
                    severity=IntegritySeverity.ERROR,
                    message="No records found in the raw file",
                )
            )

        if accumulator.error_count > 0:
            status = IntegrityStatus.FAILED
        elif accumulator.warning_count > 0:
            status = IntegrityStatus.PASSED_WITH_WARNINGS
        else:
            status = IntegrityStatus.PASSED

        schema_ref = SchemaRef(name=schema.schema_name, version=schema.schema_version)

        report = IntegrityReport(
            integrity_id=integrity_id,
            ingestion_id=ingestion_id,
            validation_id=validation_report["validation_id"],
            customer_id=manifest["customer_id"],
            device_id=manifest.get("device_id"),
            schema_name=schema.schema_name,
            schema_version=schema.schema_version,
            source_filename=manifest["original_filename"],
            raw_sha256=manifest["sha256"],
            status=status,
            total_records=counts.total_records,
            checked_records=counts.checked_records,
            passed_records=counts.passed_records,
            failed_records=counts.failed_records,
            warning_count=accumulator.warning_count,
            error_count=accumulator.error_count,
            issues=accumulator.issues,
            issues_truncated=accumulator.issues_truncated,
            created_at=datetime.now(timezone.utc),
        )

        report_uri = self._report_store.write_report(
            ingestion_id=ingestion_id,
            integrity_id=integrity_id,
            report=report.model_dump(mode="json"),
        )

        log = logger.info if status == IntegrityStatus.PASSED else logger.warning
        log(
            "INTEGRITY_COMPLETED integrity_id=%s ingestion_id=%s validation_id=%s "
            "schema_name=%s schema_version=%s status=%s total_records=%d error_count=%d "
            "warning_count=%d",
            integrity_id,
            ingestion_id,
            validation_report["validation_id"],
            schema_name,
            schema_version,
            status.value,
            counts.total_records,
            accumulator.error_count,
            accumulator.warning_count,
        )

        return IntegrityResponse(
            integrity_id=integrity_id,
            ingestion_id=ingestion_id,
            validation_id=validation_report["validation_id"],
            schema=schema_ref,
            status=status,
            total_records=counts.total_records,
            checked_records=counts.checked_records,
            passed_records=counts.passed_records,
            failed_records=counts.failed_records,
            warning_count=accumulator.warning_count,
            error_count=accumulator.error_count,
            report_uri=report_uri,
        )

    def _find_matching_validation_report(
        self, *, ingestion_id: str, schema_name: str, schema_version: str, raw_sha256: str
    ) -> dict | None:
        """Finds the most recent validation report matching this exact
        ingestion + schema + raw checksum.

        Filesystem globbing is isolated inside ValidationReportStore
        (find_reports); this method only filters/ranks already-loaded
        report dicts, so it never touches the filesystem directly. Never
        matches a report whose raw_sha256 differs from the current raw
        file's checksum — that would mean validating against stale data.
        """
        candidates = [
            report
            for report in self._validation_report_store.find_reports(ingestion_id)
            if report.get("schema", {}).get("name") == schema_name
            and report.get("schema", {}).get("version") == schema_version
            and report.get("raw_sha256") == raw_sha256
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda report: datetime.fromisoformat(report["validated_at"]))
