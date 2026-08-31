"""Validation business logic: RAW IMMUTABLE DATA -> SCHEMA VALIDATION -> VALIDATION REPORT.

This module never opens the raw file for writing and never touches
manifest.json — it only reads. The API route stays thin; all orchestration
(resolve ingestion, retrieve schema, select validator, build + persist the
report) lives here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import Settings
from app.core.logging import get_logger
from app.storage.base import RawStorage
from app.storage.validation_store import ValidationReportStore
from app.utils.filenames import extension_of
from app.utils.ids import generate_validation_id
from app.validation.models import (
    SchemaRef,
    ValidationErrorCode,
    ValidationIssue,
    ValidationReport,
    ValidationResponse,
    ValidationStatus,
    ValidationSummary,
)
from app.validation.registry import ValidatorRegistry
from app.validation.schemas.base import SchemaDefinition
from app.validation.schemas.registry import SchemaNotFoundError as SchemaRegistryNotFoundError
from app.validation.schemas.registry import SchemaRegistry
from app.validation.validators.base import ErrorAccumulator

logger = get_logger("app.validation")

# Maps a schema's metadata_requirements key to the ingestion manifest field
# it is checked against. Unlisted keys are checked against a manifest field
# of the same name.
_METADATA_KEY_TO_MANIFEST_FIELD = {"sensor_type": "source_type"}


class ValidationError(Exception):
    """Base class for validation-service failures (mapped to HTTP errors by the API layer)."""


class IngestionNotFoundError(ValidationError):
    pass


class SchemaNotFoundError(ValidationError):
    pass


class UnsupportedValidationFileTypeError(ValidationError):
    pass


class ValidationService:
    def __init__(
        self,
        *,
        storage: RawStorage,
        schema_registry: SchemaRegistry,
        validator_registry: ValidatorRegistry,
        report_store: ValidationReportStore,
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._schema_registry = schema_registry
        self._validator_registry = validator_registry
        self._report_store = report_store
        self._settings = settings

    def validate(
        self, *, ingestion_id: str, schema_name: str, schema_version: str
    ) -> ValidationResponse:
        manifest = self._storage.find_manifest(ingestion_id)
        if manifest is None:
            raise IngestionNotFoundError(f"No ingestion found with ingestion_id='{ingestion_id}'")

        try:
            schema = self._schema_registry.get(schema_name=schema_name, schema_version=schema_version)
        except SchemaRegistryNotFoundError as exc:
            raise SchemaNotFoundError(str(exc)) from exc

        extension = extension_of(manifest["original_filename"])
        if not self._validator_registry.supports(extension):
            raise UnsupportedValidationFileTypeError(
                f"Validation is not supported for file type '{extension}'"
            )
        validator = self._validator_registry.get(extension)

        validation_id = generate_validation_id()
        logger.info(
            "VALIDATION_STARTED validation_id=%s ingestion_id=%s schema_name=%s schema_version=%s",
            validation_id,
            ingestion_id,
            schema_name,
            schema_version,
        )

        accumulator = ErrorAccumulator(max_errors=self._settings.MAX_VALIDATION_ERRORS)
        self._check_metadata_requirements(schema, manifest, accumulator)

        try:
            with self._storage.open_raw(
                customer_id=manifest["customer_id"],
                session_id=manifest["session_id"],
                ingestion_id=manifest["ingestion_id"],
                filename=manifest["original_filename"],
            ) as stream:
                record_counts = validator.validate(stream, schema, accumulator)
        except Exception:
            logger.error(
                "VALIDATION_FAILED validation_id=%s ingestion_id=%s schema_name=%s schema_version=%s "
                "reason=validator_exception",
                validation_id,
                ingestion_id,
                schema_name,
                schema_version,
            )
            raise

        status = ValidationStatus.FAILED if accumulator.error_count > 0 else ValidationStatus.PASSED

        summary = ValidationSummary(
            records_checked=record_counts.records_checked,
            valid_records=record_counts.valid_records,
            invalid_records=record_counts.invalid_records,
            error_count=accumulator.error_count,
            warning_count=accumulator.warning_count,
        )

        schema_ref = SchemaRef(name=schema.schema_name, version=schema.schema_version)

        report = ValidationReport(
            validation_id=validation_id,
            ingestion_id=ingestion_id,
            validated_at=datetime.now(timezone.utc),
            schema=schema_ref,
            raw_sha256=manifest["sha256"],
            status=status,
            summary=summary,
            errors=accumulator.errors,
            warnings=accumulator.warnings,
            errors_truncated=accumulator.errors_truncated,
        )

        report_uri = self._report_store.write_report(
            ingestion_id=ingestion_id,
            validation_id=validation_id,
            report=report.model_dump(mode="json"),
        )

        log = logger.info if status == ValidationStatus.PASSED else logger.warning
        log(
            "VALIDATION_COMPLETED validation_id=%s ingestion_id=%s schema_name=%s schema_version=%s status=%s",
            validation_id,
            ingestion_id,
            schema_name,
            schema_version,
            status.value,
        )

        return ValidationResponse(
            validation_id=validation_id,
            ingestion_id=ingestion_id,
            schema=schema_ref,
            status=status,
            summary=summary,
            report_uri=report_uri,
        )

    def _check_metadata_requirements(
        self, schema: SchemaDefinition, manifest: dict, accumulator: ErrorAccumulator
    ) -> None:
        """Checks schema.metadata_requirements against the ingestion manifest.

        A requirement is only enforced when the corresponding manifest field
        was actually populated at ingestion time (e.g. source_type). Absent
        metadata is not itself an error here — Step 1 doesn't require
        source_type, and Step 2 must not retroactively demand it. This only
        catches an explicit mismatch (e.g. validating GPS-tagged data against
        the IMU schema).
        """
        for key, expected_value in schema.metadata_requirements.items():
            manifest_field = _METADATA_KEY_TO_MANIFEST_FIELD.get(key, key)
            actual_value = manifest.get(manifest_field)
            if actual_value is not None and actual_value != expected_value:
                accumulator.add_error(
                    ValidationIssue(
                        record=None,
                        field=None,
                        code=ValidationErrorCode.METADATA_REQUIREMENT_NOT_MET,
                        message=(
                            f"Expected metadata '{key}'='{expected_value}' but ingestion "
                            f"has '{manifest_field}'='{actual_value}'"
                        ),
                    )
                )
