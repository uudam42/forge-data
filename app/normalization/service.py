"""Normalization business logic:
VALIDATION REPORT + INTEGRITY REPORT -> NORMALIZATION -> CANONICAL NORMALIZED ARTIFACT.

Step 2 asks "is this record structurally valid?" Step 3 asks "are these
values plausible and internally consistent?" Step 4 asks a third, different
question: "is the data represented consistently?" — canonical units,
canonical field names, canonical timestamp representation. It is NOT
cleaning: it never removes outliers, repairs missing values, interpolates,
deduplicates, resamples, or synchronizes across files. A record that cannot
be deterministically normalized fails the *entire* run rather than being
silently skipped — Step 4 never commits a partial dataset (see
NormalizedArtifactStore's staging/commit design).

This module never opens the raw file for writing, never touches
manifest.json, and never touches a validation or integrity report — it
only reads all three, and writes exclusively to its own separate
normalized-artifact store.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from app.core.config import Settings
from app.core.logging import get_logger
from app.normalization import records as normalization_records
from app.normalization.models import (
    NormalizationManifest,
    NormalizationResponse,
    NormalizationStatus,
    ProfileRef,
)
from app.normalization.profiles.base import (
    AmbiguousFieldMappingError,
    MissingUnitMetadataError,
    NormalizationConversionError,
    RecordNormalizer,
    UnsupportedSourceUnitError,
)
from app.normalization.registry import NormalizationProfileNotFoundError, NormalizationProfileRegistry
from app.storage.base import RawStorage
from app.storage.integrity_store import IntegrityReportStore
from app.storage.normalized_store import NormalizedArtifactStore
from app.storage.validation_store import ValidationReportStore
from app.utils.filenames import extension_of
from app.utils.hashing import ChunkedSha256
from app.utils.ids import generate_normalization_id
from app.validation.models import SchemaRef
from app.validation.schemas.registry import SchemaNotFoundError as SchemaRegistryNotFoundError
from app.validation.schemas.registry import SchemaRegistry

logger = get_logger("app.normalization")

_ACCEPTED_INTEGRITY_STATUSES = {"passed", "passed_with_warnings"}


class NormalizationError(Exception):
    """Base class for normalization-service failures mapped to HTTP by the API layer."""


class IngestionNotFoundError(NormalizationError):
    pass


class SchemaNotFoundError(NormalizationError):
    pass


class NoMatchingValidationReportError(NormalizationError):
    pass


class ValidationNotPassedError(NormalizationError):
    pass


class NoMatchingIntegrityReportError(NormalizationError):
    pass


class IntegrityLineageMismatchError(NormalizationError):
    pass


class IntegrityNotPassedError(NormalizationError):
    pass


class UnsupportedNormalizationFileTypeError(NormalizationError):
    pass


class InvalidNormalizationInputError(NormalizationError):
    pass


class RawChecksumMismatchError(Exception):
    """Deliberately NOT a NormalizationError.

    This signals that the bytes on disk no longer match their own manifest
    — a storage-layer invariant violation, not a normal request-level
    condition — so it should surface as a 500, not be caught and mapped to
    a 4xx by the API layer alongside genuine lineage/config problems.
    """


class _HashingReader:
    """Wraps a binary stream, computing SHA-256 of every byte read through it.

    Exposes enough of the BinaryIO protocol (readable/writable/seekable) for
    io.TextIOWrapper to accept it as a raw buffer — the source record
    readers wrap this the same way they'd wrap a plain file object.
    """

    closed = False

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._digest = ChunkedSha256()

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        self._digest.update(chunk)
        return chunk

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def flush(self) -> None:
        pass

    def close(self) -> None:
        # Deliberately does NOT close the underlying stream — see _HashingWriter.close().
        pass

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


class _HashingWriter:
    """Wraps a binary stream, computing SHA-256 and size of every byte written through it.

    Exposes enough of the BinaryIO protocol (writable/readable/seekable/
    flush) for io.TextIOWrapper to accept it as a raw buffer — the CSV
    writer wraps this in a TextIOWrapper the same way it would wrap a plain
    file object.
    """

    closed = False

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._digest = ChunkedSha256()
        self.size_bytes = 0

    def write(self, data: bytes) -> int:
        self._digest.update(data)
        self.size_bytes += len(data)
        return self._stream.write(data)

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        # Deliberately does NOT close the underlying stream — the caller
        # (NormalizationService) owns its lifecycle via its own `with`
        # block. TextIOWrapper.detach() may still probe this attribute.
        pass

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


class NormalizationService:
    def __init__(
        self,
        *,
        storage: RawStorage,
        schema_registry: SchemaRegistry,
        validation_report_store: ValidationReportStore,
        integrity_report_store: IntegrityReportStore,
        profile_registry: NormalizationProfileRegistry,
        artifact_store: NormalizedArtifactStore,
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._schema_registry = schema_registry
        self._validation_report_store = validation_report_store
        self._integrity_report_store = integrity_report_store
        self._profile_registry = profile_registry
        self._artifact_store = artifact_store
        self._settings = settings

    def normalize(
        self,
        *,
        ingestion_id: str,
        schema_name: str,
        schema_version: str,
        profile_name: str,
        profile_version: str,
        source_units: dict[str, str],
    ) -> NormalizationResponse:
        if not schema_name.strip() or not profile_name.strip():
            raise InvalidNormalizationInputError(
                "schema_name and profile_name must be non-empty"
            )
        for dimension_name, unit in source_units.items():
            if not isinstance(unit, str) or not unit.strip():
                raise InvalidNormalizationInputError(
                    f"source_units['{dimension_name}'] must be a non-empty string"
                )

        manifest = self._storage.find_manifest(ingestion_id)
        if manifest is None:
            raise IngestionNotFoundError(f"No ingestion found with ingestion_id='{ingestion_id}'")

        try:
            schema = self._schema_registry.get(schema_name=schema_name, schema_version=schema_version)
        except SchemaRegistryNotFoundError as exc:
            raise SchemaNotFoundError(str(exc)) from exc

        # Raises NormalizationProfileNotFoundError directly (already the
        # right name/semantics for the API layer to catch) — no need to
        # re-wrap it the way SchemaNotFoundError wraps the registry's error.
        profile = self._profile_registry.get(
            schema_name=schema_name,
            schema_version=schema_version,
            profile_name=profile_name,
            profile_version=profile_version,
        )

        raw_sha256 = manifest["sha256"]

        validation_report = self._find_matching_validation_report(
            ingestion_id=ingestion_id,
            schema_name=schema_name,
            schema_version=schema_version,
            raw_sha256=raw_sha256,
        )
        if validation_report is None:
            raise NoMatchingValidationReportError(
                f"No validation report found for ingestion_id='{ingestion_id}' matching schema "
                f"'{schema_name}' v{schema_version} and the current raw file (sha256={raw_sha256})"
            )
        if validation_report["status"] != "passed":
            raise ValidationNotPassedError(
                f"Validation report {validation_report['validation_id']} has status="
                f"'{validation_report['status']}'; normalization requires a passing validation report"
            )

        integrity_report = self._find_matching_integrity_report(
            ingestion_id=ingestion_id,
            schema_name=schema_name,
            schema_version=schema_version,
            raw_sha256=raw_sha256,
        )
        if integrity_report is None:
            raise NoMatchingIntegrityReportError(
                f"No integrity report found for ingestion_id='{ingestion_id}' matching schema "
                f"'{schema_name}' v{schema_version} and the current raw file (sha256={raw_sha256})"
            )
        if integrity_report["validation_id"] != validation_report["validation_id"]:
            raise IntegrityLineageMismatchError(
                f"Integrity report {integrity_report['integrity_id']} references "
                f"validation_id='{integrity_report['validation_id']}' but the accepted validation "
                f"report is '{validation_report['validation_id']}'"
            )
        if integrity_report["status"] not in _ACCEPTED_INTEGRITY_STATUSES:
            raise IntegrityNotPassedError(
                f"Integrity report {integrity_report['integrity_id']} has status="
                f"'{integrity_report['status']}'; normalization requires integrity status to be "
                f"one of {sorted(_ACCEPTED_INTEGRITY_STATUSES)}"
            )

        extension = extension_of(manifest["original_filename"])
        if not normalization_records.supports(extension):
            raise UnsupportedNormalizationFileTypeError(
                f"Normalization is not supported for file type '{extension}'"
            )

        # Fails fast — before touching staging storage or reading any
        # records — if the profile needs a source unit that wasn't supplied
        # or is unsupported.
        normalizer = RecordNormalizer(schema=schema, profile=profile, source_units=source_units)

        normalization_id = generate_normalization_id()
        logger.info(
            "NORMALIZATION_STARTED normalization_id=%s ingestion_id=%s validation_id=%s "
            "integrity_id=%s schema_name=%s schema_version=%s profile_name=%s profile_version=%s",
            normalization_id,
            ingestion_id,
            validation_report["validation_id"],
            integrity_report["integrity_id"],
            schema_name,
            schema_version,
            profile_name,
            profile_version,
        )

        artifact_filename = f"normalized{extension}"
        final_artifact_path = self._artifact_store.artifact_path(
            ingestion_id=ingestion_id, normalization_id=normalization_id, filename=artifact_filename
        )
        artifact_uri = f"file://{final_artifact_path}"

        staging_dir = self._artifact_store.staging_dir(
            ingestion_id=ingestion_id, normalization_id=normalization_id
        )

        try:
            records_written, normalized_sha256, normalized_size_bytes = self._write_staged_artifact(
                manifest=manifest,
                extension=extension,
                normalizer=normalizer,
                staging_dir=staging_dir,
                artifact_filename=artifact_filename,
            )

            config_hash = profile.config_hash(source_units)
            schema_ref = SchemaRef(name=schema.schema_name, version=schema.schema_version)
            profile_ref = ProfileRef(name=profile.profile_name, version=profile.profile_version)

            manifest_model = NormalizationManifest(
                normalization_id=normalization_id,
                ingestion_id=ingestion_id,
                validation_id=validation_report["validation_id"],
                integrity_id=integrity_report["integrity_id"],
                customer_id=manifest["customer_id"],
                device_id=manifest.get("device_id"),
                schema=schema_ref,
                source_raw_sha256=raw_sha256,
                normalization_profile=profile_ref,
                source_units=source_units,
                normalization_config_hash=config_hash,
                transform_version=profile.transform_version,
                normalized_sha256=normalized_sha256,
                normalized_size_bytes=normalized_size_bytes,
                records_written=records_written,
                source_filename=manifest["original_filename"],
                artifact_filename=artifact_filename,
                created_at=datetime.now(timezone.utc),
                artifact_uri=artifact_uri,
            )
            (staging_dir / "manifest.json").write_text(
                manifest_model.model_dump_json(indent=2), encoding="utf-8"
            )

            self._artifact_store.commit(
                ingestion_id=ingestion_id, normalization_id=normalization_id, staging_dir=staging_dir
            )
        except Exception:
            self._artifact_store.discard(staging_dir)
            logger.error(
                "NORMALIZATION_FAILED normalization_id=%s ingestion_id=%s validation_id=%s "
                "integrity_id=%s schema_name=%s schema_version=%s profile_name=%s profile_version=%s",
                normalization_id,
                ingestion_id,
                validation_report["validation_id"],
                integrity_report["integrity_id"],
                schema_name,
                schema_version,
                profile_name,
                profile_version,
            )
            raise

        logger.info(
            "NORMALIZATION_COMPLETED normalization_id=%s ingestion_id=%s validation_id=%s "
            "integrity_id=%s schema_name=%s schema_version=%s profile_name=%s profile_version=%s "
            "records_written=%d status=%s",
            normalization_id,
            ingestion_id,
            validation_report["validation_id"],
            integrity_report["integrity_id"],
            schema_name,
            schema_version,
            profile_name,
            profile_version,
            records_written,
            NormalizationStatus.COMPLETED.value,
        )

        return NormalizationResponse(
            normalization_id=normalization_id,
            ingestion_id=ingestion_id,
            validation_id=validation_report["validation_id"],
            integrity_id=integrity_report["integrity_id"],
            schema=schema_ref,
            profile=profile_ref,
            status=NormalizationStatus.COMPLETED,
            records_written=records_written,
            artifact_uri=artifact_uri,
            normalized_sha256=normalized_sha256,
        )

    def _write_staged_artifact(
        self,
        *,
        manifest: dict,
        extension: str,
        normalizer: RecordNormalizer,
        staging_dir: Path,
        artifact_filename: str,
    ) -> tuple[int, str, int]:
        artifact_path = staging_dir / artifact_filename

        with self._storage.open_raw(
            customer_id=manifest["customer_id"],
            session_id=manifest["session_id"],
            ingestion_id=manifest["ingestion_id"],
            filename=manifest["original_filename"],
        ) as raw_stream:
            hashing_reader = _HashingReader(raw_stream)
            source_records = normalization_records.iter_records(hashing_reader, extension)
            normalized_records = (
                normalizer.normalize_record(index, record) for index, record in source_records
            )

            with artifact_path.open("wb") as out_file:
                hashing_writer = _HashingWriter(out_file)
                records_written = normalization_records.write_records(
                    hashing_writer,
                    extension,
                    normalized_records,
                    fieldnames=normalizer.canonical_fields,
                )

            computed_raw_sha256 = hashing_reader.hexdigest()

        if computed_raw_sha256 != manifest["sha256"]:
            raise RawChecksumMismatchError(
                f"Computed raw checksum {computed_raw_sha256} does not match manifest checksum "
                f"{manifest['sha256']} for ingestion_id='{manifest['ingestion_id']}'"
            )

        return records_written, hashing_writer.hexdigest(), hashing_writer.size_bytes

    def _find_matching_validation_report(
        self, *, ingestion_id: str, schema_name: str, schema_version: str, raw_sha256: str
    ) -> dict | None:
        """Finds the most recent validation report matching this exact
        ingestion + schema + raw checksum.

        Filesystem globbing is isolated inside ValidationReportStore
        (find_reports); this method only filters/ranks already-loaded
        report dicts. Mirrors IntegrityService's equivalent method — kept
        as its own small copy here rather than extracted into Steps 1-3,
        per instructions not to modify already-complete stages.
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

    def _find_matching_integrity_report(
        self, *, ingestion_id: str, schema_name: str, schema_version: str, raw_sha256: str
    ) -> dict | None:
        """Finds the most recent integrity report matching this exact
        ingestion + schema + raw checksum. Filesystem globbing stays inside
        IntegrityReportStore (find_reports)."""
        candidates = [
            report
            for report in self._integrity_report_store.find_reports(ingestion_id)
            if report.get("schema_name") == schema_name
            and report.get("schema_version") == schema_version
            and report.get("raw_sha256") == raw_sha256
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda report: datetime.fromisoformat(report["created_at"]))
