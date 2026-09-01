"""Transformation business logic:
CLEANED ARTIFACT -> PROFILE-DRIVEN WINDOWING + FEATURE GENERATION -> TRANSFORMED ARTIFACT + REPORT.

Steps 1-6 establish "this data exists, is valid, canonically represented,
temporally aligned, and free of rows/fields that shouldn't be there." Step
7 asks a different question: "how do we group these rows into ML-oriented
samples, and what deterministic handcrafted features describe each one?"
It is not dataset QC (Step 8 — a distributional judgment about the whole
dataset), not labeling, and not train/val/test splitting.

This module never opens the cleaned artifact for writing and never touches
its manifest — it only reads both, and writes exclusively to its own
separate transformed-artifact store.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.cleaning.models import CleaningStatus, PolicyRef
from app.core.config import Settings
from app.core.logging import get_logger
from app.storage.atomic import write_manifest_file
from app.storage.cleaned_store import CleanedArtifactStore
from app.storage.transformed_store import TransformedArtifactStore
from app.synchronization.readers import InvalidTimestampError, format_epoch_us, parse_canonical_timestamp_us
from app.transformation.feature_engine import FeatureEngine
from app.transformation.features.common import InvalidNumericValueError, UnknownFeatureError
from app.transformation.metrics import TransformationMetricsAccumulator
from app.transformation.models import (
    ModalityCoverageStat,
    ProfileRef,
    TransformationManifest,
    TransformationReport,
    TransformationRequest,
    TransformationResponse,
    TransformationStatus,
    TransformationSummary,
    UpstreamCleaningLineage,
)
from app.transformation.profiles.base import InvalidTransformationConfigurationError, UnsupportedWindowModeError
from app.transformation.registry import TransformationProfileRegistry
from app.transformation.serialization import canonical_json, compute_sample_id
from app.transformation.windowing import (
    InvalidWindowConfigurationError,
    NonMonotonicRowOrderError,
    iter_count_windows,
    iter_time_windows,
)
from app.utils.hashing import ChunkedSha256, sha256_of_path
from app.utils.ids import generate_transformation_id

logger = get_logger("app.transformation")

_SUPPORTED_EXTENSIONS = (".jsonl",)  # JSONL cleaned input only for the Step 7 MVP
_ARTIFACT_FILENAME = "cleaned.jsonl"  # cleaning always commits under this fixed name (see app.cleaning.service)


class TransformationError(Exception):
    """Base class for transformation-service failures mapped to HTTP by the API layer."""


class CleaningNotFoundError(TransformationError):
    pass


class CleaningNotAcceptedError(TransformationError):
    pass


class CleanedArtifactChecksumMismatchError(TransformationError):
    pass


class UnsupportedTransformationFileTypeError(TransformationError):
    pass


class InvalidTimestampTransformationError(TransformationError):
    pass


# Re-exported so callers (the API route) only need to import from this module.
InvalidTransformationConfiguration = InvalidTransformationConfigurationError
UnsupportedWindowMode = UnsupportedWindowModeError
UnknownFeature = UnknownFeatureError
InvalidNumericValue = InvalidNumericValueError


class TransformationService:
    def __init__(
        self,
        *,
        cleaned_store: CleanedArtifactStore,
        profile_registry: TransformationProfileRegistry,
        transformed_store: TransformedArtifactStore,
        settings: Settings,
    ) -> None:
        self._cleaned_store = cleaned_store
        self._profile_registry = profile_registry
        self._transformed_store = transformed_store
        self._settings = settings

    def transform(self, *, cleaning_id: str, request: TransformationRequest) -> TransformationResponse:
        manifest = self._cleaned_store.find_manifest_by_cleaning_id(cleaning_id)
        if manifest is None:
            raise CleaningNotFoundError(f"No cleaning run found with cleaning_id='{cleaning_id}'")
        if manifest.get("cleaning_id") != cleaning_id:
            # Unreachable via LocalCleanedArtifactStore's glob lookup, but a
            # manifest is user-legible JSON on disk — verify rather than
            # trust blindly (lineage gate).
            raise CleaningNotFoundError(f"Cleaning manifest for '{cleaning_id}' is inconsistent with its own ID")

        if manifest["status"] != CleaningStatus.COMPLETED.value:
            raise CleaningNotAcceptedError(
                f"Cleaning run '{cleaning_id}' has status='{manifest['status']}', not 'completed'"
            )

        synchronization_id = manifest["synchronization_id"]

        # Raises TransformationProfileNotFoundError directly (already the
        # right name/semantics for the API layer to catch).
        profile = self._profile_registry.get(request.profile_name, request.profile_version)

        extension = Path(_ARTIFACT_FILENAME).suffix
        if extension not in _SUPPORTED_EXTENSIONS:
            raise UnsupportedTransformationFileTypeError(
                f"Transformation is not supported for cleaned artifact type '{extension}'"
            )

        artifact_path = Path(
            self._cleaned_store.artifact_path(
                synchronization_id=synchronization_id, cleaning_id=cleaning_id, filename=_ARTIFACT_FILENAME
            )
        )
        if not artifact_path.exists():
            raise CleaningNotFoundError(f"Cleaned artifact file is missing on disk for cleaning_id='{cleaning_id}'")

        computed_sha256 = sha256_of_path(artifact_path)
        if computed_sha256 != manifest["cleaned_sha256"]:
            raise CleanedArtifactChecksumMismatchError(
                f"Cleaned artifact for cleaning_id='{cleaning_id}' has been modified since cleaning: "
                f"expected sha256={manifest['cleaned_sha256']}, computed={computed_sha256}"
            )

        known_streams = sorted(s["name"] for s in manifest["streams"])

        # Raises InvalidTransformationConfigurationError /
        # UnsupportedWindowModeError / UnknownFeatureError directly.
        profile.validate_config(
            request.config,
            known_streams=known_streams,
            max_window_size=self._settings.MAX_WINDOW_SIZE,
            max_time_window_ms=self._settings.MAX_TIME_WINDOW_MS,
        )

        extractors = profile.build_extractors(request.config)
        # stream_configs() (v2.3), not getattr(): a plugin's stream name
        # may only exist via FeaturesConfig's extra="allow" capture (no
        # declared attribute), and extra values arrive as raw dicts, not
        # validated StreamFeatureConfig instances -- stream_configs()
        # handles both the named (imu/gps) and generic-extra cases
        # uniformly. See app.transformation.models.FeaturesConfig.
        feature_configs = request.config.features.stream_configs()
        config_hash = profile.config_hash(request.config)

        transformation_id = generate_transformation_id()
        logger.info(
            "TRANSFORMATION_STARTED transformation_id=%s cleaning_id=%s profile_name=%s profile_version=%s "
            "window_mode=%s",
            transformation_id,
            cleaning_id,
            request.profile_name,
            request.profile_version,
            request.config.window.mode,
        )

        staging_dir = self._transformed_store.staging_dir(
            cleaning_id=cleaning_id, transformation_id=transformation_id
        )

        engine = FeatureEngine(
            extractors=extractors,
            feature_configs=feature_configs,
            known_streams=known_streams,
            include_modality_mask=request.config.features.include_modality_mask,
            include_relative_time=request.config.features.include_relative_time,
        )
        metrics = TransformationMetricsAccumulator()

        try:
            input_rows, transformed_sha256, transformed_size_bytes = self._process_rows(
                artifact_path=artifact_path,
                engine=engine,
                metrics=metrics,
                staging_dir=staging_dir,
                window_config=request.config.window,
                cleaned_sha256=manifest["cleaned_sha256"],
                config_hash=config_hash,
            )

            summary = TransformationSummary(
                input_rows=input_rows,
                samples_written=metrics.samples_written,
                window_mode=request.config.window.mode,
                average_rows_per_window=metrics.average_rows_per_window,
            )

            coverage_raw = metrics.modality_coverage(known_streams)
            report = TransformationReport(
                transformation_id=transformation_id,
                summary=summary,
                modality_coverage={name: ModalityCoverageStat(**stats) for name, stats in coverage_raw.items()},
                feature_counts=metrics.feature_counts,
            )
            (staging_dir / "report.json").write_text(
                canonical_json(report.model_dump(mode="json")), encoding="utf-8"
            )

            session_ids = sorted({s["session_id"] for s in manifest["streams"]})
            normalization_ids = sorted({s["normalization_id"] for s in manifest["streams"]})
            upstream = UpstreamCleaningLineage(
                cleaning_id=cleaning_id,
                synchronization_id=synchronization_id,
                cleaning_policy=PolicyRef(**manifest["policy"]),
                cleaning_config_hash=manifest["cleaning_config_hash"],
                session_ids=session_ids,
                normalization_ids=normalization_ids,
            )

            transformed_artifact_path = self._transformed_store.artifact_path(
                cleaning_id=cleaning_id, transformation_id=transformation_id, filename="transformed.jsonl"
            )
            transformed_report_path = self._transformed_store.report_path(
                cleaning_id=cleaning_id, transformation_id=transformation_id
            )
            artifact_uri = f"file://{transformed_artifact_path}"
            report_uri = f"file://{transformed_report_path}"

            profile_ref = ProfileRef(name=request.profile_name, version=request.profile_version)
            manifest_model = TransformationManifest(
                transformation_id=transformation_id,
                cleaning_id=cleaning_id,
                upstream=upstream,
                source_cleaned_sha256=manifest["cleaned_sha256"],
                profile=profile_ref,
                transform_version=profile.transform_version,
                transformation_config_hash=config_hash,
                input_rows=input_rows,
                samples_written=metrics.samples_written,
                transformed_sha256=transformed_sha256,
                transformed_size_bytes=transformed_size_bytes,
                artifact_uri=artifact_uri,
                report_uri=report_uri,
                created_at=datetime.now(timezone.utc),
            )
            write_manifest_file(staging_dir, "manifest.json", manifest_model.model_dump_json(indent=2))

            self._transformed_store.commit(
                cleaning_id=cleaning_id, transformation_id=transformation_id, staging_dir=staging_dir
            )
        except Exception:
            self._transformed_store.discard(staging_dir)
            logger.error(
                "TRANSFORMATION_FAILED transformation_id=%s cleaning_id=%s profile_name=%s profile_version=%s",
                transformation_id,
                cleaning_id,
                request.profile_name,
                request.profile_version,
            )
            raise

        logger.info(
            "TRANSFORMATION_COMPLETED transformation_id=%s cleaning_id=%s profile_name=%s profile_version=%s "
            "window_mode=%s input_rows=%d samples_written=%d status=completed",
            transformation_id,
            cleaning_id,
            request.profile_name,
            request.profile_version,
            request.config.window.mode,
            input_rows,
            metrics.samples_written,
        )

        return TransformationResponse(
            transformation_id=transformation_id,
            cleaning_id=cleaning_id,
            status=TransformationStatus.COMPLETED,
            profile=profile_ref,
            summary=summary,
            artifact_uri=artifact_uri,
            report_uri=report_uri,
            transformed_sha256=transformed_sha256,
        )

    def _process_rows(
        self,
        *,
        artifact_path: Path,
        engine: FeatureEngine,
        metrics: TransformationMetricsAccumulator,
        staging_dir: Path,
        window_config,
        cleaned_sha256: str,
        config_hash: str,
    ) -> tuple[int, str, int]:
        transformed_path = staging_dir / "transformed.jsonl"
        digest = ChunkedSha256()
        size_bytes = 0
        row_counter = {"value": 0}

        def row_stream():
            with artifact_path.open("r", encoding="utf-8") as source:
                row_index = 0
                for line in source:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    row = json.loads(stripped)
                    try:
                        epoch_us = parse_canonical_timestamp_us(row["timestamp"])
                    except InvalidTimestampError as exc:
                        raise InvalidTimestampTransformationError(str(exc)) from exc
                    yield row_index, epoch_us, row
                    row_index += 1
                    row_counter["value"] += 1

        if window_config.mode == "count":
            windows = iter_count_windows(
                row_stream(),
                size=window_config.size,
                stride=window_config.stride,
                drop_incomplete=window_config.drop_incomplete,
            )
        else:
            windows = iter_time_windows(
                row_stream(),
                duration_us=int(window_config.duration_ms * 1000),
                stride_us=int(window_config.stride_ms * 1000),
                drop_incomplete=window_config.drop_incomplete,
            )

        with transformed_path.open("wb") as out_file:
            for window_index, start_epoch_us, end_epoch_us, window_rows in windows:
                result = engine.process_window(
                    window_index=window_index,
                    start_epoch_us=start_epoch_us,
                    end_epoch_us=end_epoch_us,
                    window_rows=window_rows,
                )
                metrics.record_window(result)

                sample_id = compute_sample_id(
                    cleaned_sha256=cleaned_sha256,
                    config_hash=config_hash,
                    window_index=window_index,
                    start_epoch_us=start_epoch_us,
                    end_epoch_us=end_epoch_us,
                )

                sample: dict = {
                    "sample_id": sample_id,
                    "window": {
                        "index": window_index,
                        "start_timestamp": format_epoch_us(start_epoch_us),
                        "end_timestamp": format_epoch_us(end_epoch_us),
                        "row_count": result.row_count,
                    },
                    "features": result.features,
                    "modality_coverage": result.modality_coverage,
                    "metadata": {
                        "source_row_start": result.source_row_start,
                        "source_row_end": result.source_row_end,
                    },
                }
                if engine.include_modality_mask:
                    sample["modality_mask"] = result.modality_mask
                if result.relative_time_ms is not None:
                    sample["metadata"]["relative_time_ms"] = result.relative_time_ms

                output_line = (canonical_json(sample) + "\n").encode("utf-8")
                digest.update(output_line)
                size_bytes += len(output_line)
                out_file.write(output_line)

        return row_counter["value"], digest.hexdigest(), size_bytes
