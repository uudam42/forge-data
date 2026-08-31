"""Synchronization business logic:
N NORMALIZED ARTIFACTS -> LINEAGE VERIFICATION -> TIMELINE ALIGNMENT -> SYNCHRONIZED ARTIFACT.

Step 5 answers "which observations from different sensors correspond to
the same point in time?" It operates exclusively on already-normalized
artifacts (never raw data), and never mutates any upstream artifact. It is
NOT cleaning: a missing match becomes a null value in the output row, never
a dropped row and never an invented/interpolated-beyond-bounds value.

This module never opens a raw file, a normalization artifact, or any
report for writing — it only reads them, and writes exclusively to its own
separate synchronized-artifact store.

Known architectural limitation (documented, not fixed here): Step 3's IMU
extreme-value thresholds run against raw values before Step 4's unit
conversion, so a raw "180 deg/s" can trigger a warning as if it were
already "180 rad/s". Step 5 consumes Step 4's canonical (already-converted)
values and must not reinterpret units itself — this module contains no
unit-conversion logic of any kind.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from app.core.config import Settings
from app.core.logging import get_logger
from app.storage.base import RawStorage
from app.storage.normalized_store import NormalizedArtifactStore
from app.storage.synchronization_store import SynchronizationArtifactStore
from app.synchronization import alignment, timeline
from app.synchronization import readers as sync_readers
from app.synchronization.alignment import StreamRuntime
from app.synchronization.clocks.correction import IDENTITY_CORRECTION, apply_stream_correction
from app.synchronization.models import (
    ClockCorrectionConfig,
    StreamLineage,
    StreamMetrics,
    SynchronizationManifest,
    SynchronizationRequest,
    SynchronizationResponse,
    SynchronizationStatus,
)
from app.synchronization.metrics import StreamMetricsAccumulator
from app.synchronization.registry import AlignmentStrategyRegistry, UnsupportedAlignmentMethodError
from app.synchronization.strategies.base import StreamCursor
from app.synchronization.timeline import InvalidSyncConfigurationError
from app.utils.filenames import extension_of
from app.utils.hashing import ChunkedSha256, sha256_of_path
from app.utils.ids import generate_synchronization_id
from app.validation.schemas.base import SchemaDefinition
from app.validation.schemas.registry import SchemaNotFoundError as SchemaRegistryNotFoundError
from app.validation.schemas.registry import SchemaRegistry

logger = get_logger("app.synchronization")

_ARTIFACT_FILENAME = "synchronized.jsonl"
_SUPPORTED_EXTENSIONS = (".csv", ".json", ".jsonl")


class SynchronizationError(Exception):
    """Base class for synchronization-service failures mapped to HTTP by the API layer."""


class NormalizationNotFoundError(SynchronizationError):
    pass


class SchemaNotFoundError(SynchronizationError):
    pass


class NormalizedArtifactChecksumMismatchError(SynchronizationError):
    pass


class SessionMismatchError(SynchronizationError):
    pass


class DuplicateStreamNameError(SynchronizationError):
    pass


class ReferenceStreamNotFoundError(SynchronizationError):
    pass


class UnsupportedSyncFileTypeError(SynchronizationError):
    pass


class ClockCorrectionError(SynchronizationError):
    pass


class SynchronizationConversionError(SynchronizationError):
    pass


@dataclass
class _LoadedStream:
    name: str
    normalization_id: str
    ingestion_id: str
    session_id: str
    validation_id: str
    integrity_id: str
    source_raw_sha256: str
    schema_name: str
    schema_version: str
    schema: SchemaDefinition
    normalized_sha256: str
    artifact_local_path: Path
    extension: str
    profile_name: str
    profile_version: str
    normalization_config_hash: str
    transform_version: str
    records_written: int


class SynchronizationService:
    def __init__(
        self,
        *,
        raw_storage: RawStorage,
        normalized_store: NormalizedArtifactStore,
        schema_registry: SchemaRegistry,
        strategy_registry: AlignmentStrategyRegistry,
        artifact_store: SynchronizationArtifactStore,
        settings: Settings,
    ) -> None:
        self._raw_storage = raw_storage
        self._normalized_store = normalized_store
        self._schema_registry = schema_registry
        self._strategy_registry = strategy_registry
        self._artifact_store = artifact_store
        self._settings = settings

    def synchronize(self, request: SynchronizationRequest) -> SynchronizationResponse:
        self._validate_request_shape(request)

        loaded_streams: dict[str, _LoadedStream] = {}
        for stream_req in request.streams:
            loaded_streams[stream_req.name] = self._load_stream(
                name=stream_req.name, normalization_id=stream_req.normalization_id
            )
        self._verify_session_compatibility(loaded_streams)

        tolerance_ms = (
            request.alignment.max_time_delta_ms
            if request.alignment.max_time_delta_ms is not None
            else self._settings.DEFAULT_SYNC_TOLERANCE_MS
        )
        tolerance_us = round(tolerance_ms * 1000)

        effective_methods = {
            name: (request.alignment.streams[name].method if name in request.alignment.streams else request.alignment.default_method)
            for name in loaded_streams
        }
        strategies = {
            name: self._strategy_registry.get(method, schema=loaded_streams[name].schema)
            for name, method in effective_methods.items()
        }

        corrections: dict[str, ClockCorrectionConfig] = {}
        for name in loaded_streams:
            cfg = request.clock_corrections.get(name, IDENTITY_CORRECTION)
            scale = 1.0 + cfg.drift_ppm / 1_000_000.0
            if scale <= 0:
                raise ClockCorrectionError(
                    f"drift_ppm={cfg.drift_ppm} for stream '{name}' would reverse time order (scale={scale})"
                )
            corrections[name] = cfg

        synchronization_id = generate_synchronization_id()
        stream_names = sorted(loaded_streams)
        logger.info(
            "SYNCHRONIZATION_STARTED synchronization_id=%s session_id=%s streams=%s "
            "normalization_ids=%s reference_mode=%s",
            synchronization_id,
            next(iter(loaded_streams.values())).session_id,
            stream_names,
            [loaded_streams[n].normalization_id for n in stream_names],
            request.reference.mode,
        )

        staging_dir = self._artifact_store.staging_dir(synchronization_id=synchronization_id)
        metrics_accumulators = {
            name: StreamMetricsAccumulator(source_records=loaded_streams[name].records_written)
            for name in loaded_streams
        }

        try:
            if request.reference.mode == "fixed_rate":
                targets, streams_runtime, open_files = self._build_fixed_rate_inputs(
                    request=request,
                    loaded_streams=loaded_streams,
                    strategies=strategies,
                    corrections=corrections,
                    tolerance_us=tolerance_us,
                )
            else:
                targets, streams_runtime, open_files = self._build_reference_stream_inputs(
                    request=request,
                    loaded_streams=loaded_streams,
                    strategies=strategies,
                    corrections=corrections,
                    tolerance_us=tolerance_us,
                )

            try:
                rows_written, synchronized_sha256, synchronized_size_bytes = self._write_rows(
                    targets=targets,
                    streams=streams_runtime,
                    staging_dir=staging_dir,
                    metrics_accumulators=metrics_accumulators,
                )
            finally:
                for f in open_files:
                    f.close()

            config_hash = self._config_hash(
                request, tolerance_ms=tolerance_ms, effective_methods=effective_methods
            )

            stream_lineage = [
                StreamLineage(
                    name=name,
                    normalization_id=loaded.normalization_id,
                    normalized_sha256=loaded.normalized_sha256,
                    ingestion_id=loaded.ingestion_id,
                    session_id=loaded.session_id,
                    validation_id=loaded.validation_id,
                    integrity_id=loaded.integrity_id,
                    source_raw_sha256=loaded.source_raw_sha256,
                    schema={"name": loaded.schema_name, "version": loaded.schema_version},
                    normalization_profile_name=loaded.profile_name,
                    normalization_profile_version=loaded.profile_version,
                    normalization_config_hash=loaded.normalization_config_hash,
                    normalization_transform_version=loaded.transform_version,
                )
                for name, loaded in loaded_streams.items()
            ]
            metrics = {
                name: metrics_accumulators[name].finalize(rows_written) for name in loaded_streams
            }

            manifest_model = SynchronizationManifest(
                synchronization_id=synchronization_id,
                created_at=datetime.now(timezone.utc),
                streams=stream_lineage,
                reference=request.reference,
                alignment_config=request.alignment,
                clock_corrections=request.clock_corrections,
                synchronization_config_hash=config_hash,
                rows_written=rows_written,
                metrics=metrics,
                synchronized_sha256=synchronized_sha256,
                synchronized_size_bytes=synchronized_size_bytes,
                artifact_uri=f"file://{self._artifact_store.artifact_path(synchronization_id=synchronization_id, filename=_ARTIFACT_FILENAME)}",
                artifact_filename=_ARTIFACT_FILENAME,
            )
            (staging_dir / "manifest.json").write_text(
                manifest_model.model_dump_json(indent=2), encoding="utf-8"
            )

            self._artifact_store.commit(synchronization_id=synchronization_id, staging_dir=staging_dir)
        except Exception:
            self._artifact_store.discard(staging_dir)
            logger.error(
                "SYNCHRONIZATION_FAILED synchronization_id=%s streams=%s",
                synchronization_id,
                stream_names,
            )
            raise

        logger.info(
            "SYNCHRONIZATION_COMPLETED synchronization_id=%s streams=%s rows_written=%d "
            "coverage=%s status=%s",
            synchronization_id,
            stream_names,
            rows_written,
            {name: metrics[name].coverage_ratio for name in stream_names},
            SynchronizationStatus.COMPLETED.value,
        )

        return SynchronizationResponse(
            synchronization_id=synchronization_id,
            status=SynchronizationStatus.COMPLETED,
            reference=request.reference,
            streams=request.streams,
            rows_written=rows_written,
            coverage={name: metrics[name].coverage_ratio for name in stream_names},
            artifact_uri=manifest_model.artifact_uri,
            synchronized_sha256=synchronized_sha256,
        )

    # -- validation -----------------------------------------------------

    def _validate_request_shape(self, request: SynchronizationRequest) -> None:
        if len(request.streams) < 2:
            raise InvalidSyncConfigurationError(
                f"At least 2 streams are required for multimodal synchronization, got {len(request.streams)}"
            )

        names = [s.name for s in request.streams]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise DuplicateStreamNameError(f"Duplicate stream name(s): {duplicates}")

        if request.reference.mode not in ("stream", "fixed_rate"):
            raise InvalidSyncConfigurationError(f"Unknown reference.mode '{request.reference.mode}'")

        if request.reference.mode == "stream":
            if not request.reference.stream:
                raise InvalidSyncConfigurationError("reference.stream is required when reference.mode='stream'")
            if request.reference.stream not in names:
                raise ReferenceStreamNotFoundError(
                    f"reference.stream '{request.reference.stream}' is not among the declared streams {names}"
                )
        else:
            if request.reference.frequency_hz is None:
                raise InvalidSyncConfigurationError(
                    "reference.frequency_hz is required when reference.mode='fixed_rate'"
                )

        if request.alignment.max_time_delta_ms is not None and request.alignment.max_time_delta_ms < 0:
            raise InvalidSyncConfigurationError("alignment.max_time_delta_ms must not be negative")

    def _verify_session_compatibility(self, loaded_streams: dict[str, _LoadedStream]) -> None:
        session_ids = {ls.session_id for ls in loaded_streams.values() if ls.session_id}
        if len(session_ids) > 1:
            details = ", ".join(f"{ls.name}={ls.session_id}" for ls in loaded_streams.values())
            raise SessionMismatchError(f"Streams belong to different sessions and cannot be synchronized by default: {details}")

    # -- loading ----------------------------------------------------------

    def _load_stream(self, *, name: str, normalization_id: str) -> _LoadedStream:
        manifest = self._normalized_store.find_manifest(normalization_id)
        if manifest is None:
            raise NormalizationNotFoundError(
                f"No normalization run found with normalization_id='{normalization_id}' for stream '{name}'"
            )

        ingestion_id = manifest["ingestion_id"]
        artifact_filename = manifest["artifact_filename"]
        extension = extension_of(artifact_filename)
        if extension not in _SUPPORTED_EXTENSIONS:
            raise UnsupportedSyncFileTypeError(
                f"Normalized artifact '{artifact_filename}' for stream '{name}' has unsupported "
                f"extension '{extension}'"
            )

        artifact_local_path = Path(
            self._normalized_store.artifact_path(
                ingestion_id=ingestion_id, normalization_id=normalization_id, filename=artifact_filename
            )
        )
        if not artifact_local_path.exists():
            raise NormalizationNotFoundError(
                f"Normalized artifact file is missing on disk for normalization_id='{normalization_id}' "
                f"(stream '{name}')"
            )

        computed_sha256 = sha256_of_path(artifact_local_path)
        if computed_sha256 != manifest["normalized_sha256"]:
            raise NormalizedArtifactChecksumMismatchError(
                f"Normalized artifact for stream '{name}' (normalization_id='{normalization_id}') has "
                f"been modified since normalization: expected sha256={manifest['normalized_sha256']}, "
                f"computed={computed_sha256}"
            )

        ingestion_manifest = self._raw_storage.find_manifest(ingestion_id)
        if ingestion_manifest is None:
            raise NormalizationNotFoundError(
                f"Ingestion '{ingestion_id}' referenced by normalization_id='{normalization_id}' "
                "no longer exists"
            )

        schema_name = manifest["schema"]["name"]
        schema_version = manifest["schema"]["version"]
        try:
            schema = self._schema_registry.get(schema_name=schema_name, schema_version=schema_version)
        except SchemaRegistryNotFoundError as exc:
            raise SchemaNotFoundError(str(exc)) from exc

        return _LoadedStream(
            name=name,
            normalization_id=normalization_id,
            ingestion_id=ingestion_id,
            session_id=ingestion_manifest["session_id"],
            validation_id=manifest["validation_id"],
            integrity_id=manifest["integrity_id"],
            source_raw_sha256=manifest["source_raw_sha256"],
            schema_name=schema_name,
            schema_version=schema_version,
            schema=schema,
            normalized_sha256=manifest["normalized_sha256"],
            artifact_local_path=artifact_local_path,
            extension=extension,
            profile_name=manifest["normalization_profile"]["name"],
            profile_version=manifest["normalization_profile"]["version"],
            normalization_config_hash=manifest["normalization_config_hash"],
            transform_version=manifest["transform_version"],
            records_written=manifest["records_written"],
        )

    # -- timeline construction --------------------------------------------

    def _build_reference_stream_inputs(
        self, *, request, loaded_streams, strategies, corrections, tolerance_us
    ) -> tuple[Iterator[tuple[int, dict | None]], list[StreamRuntime], list[BinaryIO]]:
        reference_name = request.reference.stream
        open_files: list[BinaryIO] = []
        generators: dict[str, Iterator[tuple[int, int, dict]]] = {}

        for name, loaded in loaded_streams.items():
            f = loaded.artifact_local_path.open("rb")
            open_files.append(f)
            typed = sync_readers.iter_typed_records(f, loaded.extension, loaded.schema)
            generators[name] = apply_stream_correction(typed, corrections[name])

        targets = ((epoch_us, record) for _, epoch_us, record in generators[reference_name])

        streams_runtime: list[StreamRuntime] = []
        for name, loaded in loaded_streams.items():
            if name == reference_name:
                streams_runtime.append(
                    StreamRuntime(
                        name=name,
                        schema=loaded.schema,
                        strategy=strategies[name],
                        cursor=None,
                        tolerance_us=tolerance_us,
                        is_reference=True,
                    )
                )
            else:
                streams_runtime.append(
                    StreamRuntime(
                        name=name,
                        schema=loaded.schema,
                        strategy=strategies[name],
                        cursor=StreamCursor(generators[name]),
                        tolerance_us=tolerance_us,
                        is_reference=False,
                    )
                )

        return targets, streams_runtime, open_files

    def _build_fixed_rate_inputs(
        self, *, request, loaded_streams, strategies, corrections, tolerance_us
    ) -> tuple[Iterator[tuple[int, dict | None]], list[StreamRuntime], list[BinaryIO]]:
        period_us = timeline.fixed_rate_period_us(
            request.reference.frequency_hz, max_frequency_hz=self._settings.MAX_SYNC_FREQUENCY_HZ
        )

        # First pass: each stream's own corrected time range. A second,
        # independent read follows for the actual alignment cursors — this
        # mode inherently needs the full range before any target can be
        # generated, unlike reference-stream mode (see module docstring).
        ranges: list[tuple[int, int]] = []
        for name, loaded in loaded_streams.items():
            with loaded.artifact_local_path.open("rb") as f:
                typed = sync_readers.iter_typed_records(f, loaded.extension, loaded.schema)
                corrected = apply_stream_correction(typed, corrections[name])
                first_epoch_us = None
                last_epoch_us = None
                for _, epoch_us, _ in corrected:
                    if first_epoch_us is None:
                        first_epoch_us = epoch_us
                    last_epoch_us = epoch_us
            if first_epoch_us is None:
                raise SynchronizationConversionError(
                    f"Stream '{name}' has no records; fixed_rate synchronization requires at least "
                    "one sample per stream"
                )
            ranges.append((first_epoch_us, last_epoch_us))

        start_epoch_us, end_epoch_us = timeline.intersection_interval(ranges)
        targets = (
            (t, None)
            for t in timeline.fixed_rate_timeline(
                start_epoch_us=start_epoch_us, end_epoch_us=end_epoch_us, period_us=period_us
            )
        )

        open_files: list[BinaryIO] = []
        streams_runtime: list[StreamRuntime] = []
        for name, loaded in loaded_streams.items():
            f = loaded.artifact_local_path.open("rb")
            open_files.append(f)
            typed = sync_readers.iter_typed_records(f, loaded.extension, loaded.schema)
            corrected = apply_stream_correction(typed, corrections[name])
            streams_runtime.append(
                StreamRuntime(
                    name=name,
                    schema=loaded.schema,
                    strategy=strategies[name],
                    cursor=StreamCursor(corrected),
                    tolerance_us=tolerance_us,
                    is_reference=False,
                )
            )

        return targets, streams_runtime, open_files

    # -- output writing -----------------------------------------------------

    def _write_rows(
        self,
        *,
        targets: Iterator[tuple[int, dict | None]],
        streams: list[StreamRuntime],
        staging_dir: Path,
        metrics_accumulators: dict[str, StreamMetricsAccumulator],
    ) -> tuple[int, str, int]:
        artifact_path = staging_dir / _ARTIFACT_FILENAME
        rows_written = 0
        digest = ChunkedSha256()
        size_bytes = 0

        with artifact_path.open("wb") as out_file:
            for target_epoch_us, streams_payload, alignment_payload in alignment.iter_rows(
                targets=targets, streams=streams
            ):
                for name, result in alignment_payload.items():
                    metrics_accumulators[name].record(result)

                row = {
                    "timestamp": sync_readers.format_epoch_us(target_epoch_us),
                    "streams": streams_payload,
                    "alignment": alignment_payload,
                }
                line = (json.dumps(row) + "\n").encode("utf-8")
                digest.update(line)
                size_bytes += len(line)
                out_file.write(line)
                rows_written += 1

        return rows_written, digest.hexdigest(), size_bytes

    # -- config hash ----------------------------------------------------

    def _config_hash(self, request: SynchronizationRequest, *, tolerance_ms: float, effective_methods: dict[str, str]) -> str:
        payload = {
            "reference": {
                "mode": request.reference.mode,
                "stream": request.reference.stream,
                "frequency_hz": request.reference.frequency_hz,
            },
            "alignment": {
                "default_method": request.alignment.default_method,
                "max_time_delta_ms": tolerance_ms,
                "effective_methods": effective_methods,
            },
            "clock_corrections": {
                name: {"offset_ms": cfg.offset_ms, "drift_ppm": cfg.drift_ppm}
                for name, cfg in request.clock_corrections.items()
            },
            "streams": sorted(effective_methods),
            "tie_breaking_policy": "prefer_earlier_observation",
            "timestamp_policy": "utc_iso8601_z_preserve_subsecond_v1",
            "transform_version": "1.0.0",
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
