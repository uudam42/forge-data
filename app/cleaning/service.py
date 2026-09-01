"""Cleaning business logic:
SYNCHRONIZED ARTIFACT -> POLICY EVALUATION -> CLEANED ARTIFACT + REPORT.

Steps 1-5 establish "this data exists, is structurally/semantically valid,
canonically represented, and temporally aligned." Step 6 asks a different,
final-mile question before downstream feature work: "is this row usable,
and does anything in it need redacting?" It is not integrity checking
(Step 3 — per-value plausibility) and not Dataset QC (Step 8 — a
distributional judgment about the whole dataset) — Step 6 only applies
explicit, deterministic, configured rules to each row, and reports exactly
why any row was dropped or redacted. A row is never dropped silently.

This module never opens the synchronized artifact for writing and never
touches its manifest — it only reads both, and writes exclusively to its
own separate cleaned-artifact store.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.cleaning.evaluator import RowEvaluator
from app.cleaning.metrics import CleaningMetricsAccumulator
from app.cleaning.models import (
    CleaningConfig,
    CleaningManifest,
    CleaningReport,
    CleaningRequest,
    CleaningResponse,
    CleaningStatus,
    CleaningSummary,
    DroppedRowExample,
    PolicyRef,
    RedactionExample,
    UpstreamStreamLineage,
)
from app.cleaning.registry import CleaningPolicyRegistry
from app.cleaning.rules.common import canonical_json, is_valid_field_path
from app.core.config import Settings
from app.core.logging import get_logger
from app.storage.atomic import write_manifest_file
from app.storage.cleaned_store import CleanedArtifactStore
from app.storage.synchronization_store import SynchronizationArtifactStore
from app.utils.filenames import extension_of
from app.utils.hashing import ChunkedSha256, sha256_of_path
from app.utils.ids import generate_cleaning_id

logger = get_logger("app.cleaning")

_SUPPORTED_EXTENSIONS = (".jsonl",)  # JSONL synchronized input only for the Step 6 MVP


class CleaningError(Exception):
    """Base class for cleaning-service failures mapped to HTTP by the API layer."""


class SynchronizationNotFoundError(CleaningError):
    pass


class SynchronizedArtifactChecksumMismatchError(CleaningError):
    pass


class InvalidCleaningConfigurationError(CleaningError):
    pass


class InvalidRedactionPathError(CleaningError):
    pass


class UnsupportedCleaningFileTypeError(CleaningError):
    pass


class CleaningService:
    def __init__(
        self,
        *,
        sync_store: SynchronizationArtifactStore,
        policy_registry: CleaningPolicyRegistry,
        cleaned_store: CleanedArtifactStore,
        settings: Settings,
    ) -> None:
        self._sync_store = sync_store
        self._policy_registry = policy_registry
        self._cleaned_store = cleaned_store
        self._settings = settings

    def clean(self, *, synchronization_id: str, request: CleaningRequest) -> CleaningResponse:
        manifest = self._sync_store.find_manifest(synchronization_id)
        if manifest is None:
            raise SynchronizationNotFoundError(
                f"No synchronization run found with synchronization_id='{synchronization_id}'"
            )
        if manifest.get("synchronization_id") != synchronization_id:
            # Unreachable via LocalSynchronizationArtifactStore's direct
            # lookup, but a manifest is user-legible JSON on disk — verify
            # rather than trust blindly (lineage gate step 6).
            raise SynchronizationNotFoundError(
                f"Synchronization manifest for '{synchronization_id}' is inconsistent with its own ID"
            )

        # Raises CleaningPolicyNotFoundError directly (already the right
        # name/semantics for the API layer to catch).
        policy = self._policy_registry.get(request.policy_name, request.policy_version)

        self._validate_config(request.config)

        artifact_filename = manifest["artifact_filename"]
        extension = extension_of(artifact_filename)
        if extension not in _SUPPORTED_EXTENSIONS:
            raise UnsupportedCleaningFileTypeError(
                f"Cleaning is not supported for synchronized artifact type '{extension}'"
            )

        artifact_path = Path(
            self._sync_store.artifact_path(synchronization_id=synchronization_id, filename=artifact_filename)
        )
        if not artifact_path.exists():
            raise SynchronizationNotFoundError(
                f"Synchronized artifact file is missing on disk for synchronization_id='{synchronization_id}'"
            )

        computed_sha256 = sha256_of_path(artifact_path)
        if computed_sha256 != manifest["synchronized_sha256"]:
            raise SynchronizedArtifactChecksumMismatchError(
                f"Synchronized artifact for synchronization_id='{synchronization_id}' has been "
                f"modified since synchronization: expected sha256={manifest['synchronized_sha256']}, "
                f"computed={computed_sha256}"
            )

        known_streams = sorted(s["name"] for s in manifest["streams"])
        config_hash = policy.config_hash(request.config)

        cleaning_id = generate_cleaning_id()
        logger.info(
            "CLEANING_STARTED cleaning_id=%s synchronization_id=%s policy_name=%s policy_version=%s",
            cleaning_id,
            synchronization_id,
            request.policy_name,
            request.policy_version,
        )

        staging_dir = self._cleaned_store.staging_dir(
            synchronization_id=synchronization_id, cleaning_id=cleaning_id
        )
        # temp_dir=staging_dir: any rule needing disk-backed state (e.g. a
        # sqlite-backend DuplicateRowRule) puts it inside this run's own
        # v2.1 staging directory, so it's cleaned up by discard() on
        # failure and explicitly closed before commit() on success (see
        # the `finally` below) -- it never becomes part of a finalized
        # artifact.
        rules = policy.build_rules(request.config, known_streams=known_streams, temp_dir=staging_dir)
        metrics = CleaningMetricsAccumulator(max_detail_entries=self._settings.MAX_CLEANING_ISSUE_DETAILS)
        evaluator = RowEvaluator(rules)

        try:
            try:
                cleaned_sha256, cleaned_size_bytes = self._process_rows(
                    artifact_path=artifact_path, evaluator=evaluator, metrics=metrics, staging_dir=staging_dir
                )
            finally:
                for rule in rules:
                    closer = getattr(rule, "close", None)
                    if closer is not None:
                        closer()

            status, rejection_reasons = self._determine_status(metrics, request.config)

            summary = CleaningSummary(
                input_rows=metrics.input_rows,
                retained_rows=metrics.retained_rows,
                dropped_rows=metrics.dropped_rows,
                redacted_rows=metrics.redacted_rows,
                retention_ratio=metrics.retention_ratio,
            )
            policy_ref = PolicyRef(name=request.policy_name, version=request.policy_version)

            report = CleaningReport(
                cleaning_id=cleaning_id,
                synchronization_id=synchronization_id,
                status=status,
                summary=summary,
                reason_counts=metrics.reason_counts,
                dropped_examples=[DroppedRowExample(**e) for e in metrics.dropped_examples],
                redaction_examples=[RedactionExample(**e) for e in metrics.redaction_examples],
                details_truncated=metrics.details_truncated,
                rejection_reasons=rejection_reasons,
            )
            (staging_dir / "report.json").write_text(
                canonical_json(report.model_dump(mode="json")), encoding="utf-8"
            )

            stream_lineage = [
                UpstreamStreamLineage(
                    name=s["name"],
                    normalization_id=s["normalization_id"],
                    ingestion_id=s["ingestion_id"],
                    session_id=s["session_id"],
                )
                for s in manifest["streams"]
            ]

            artifact_uri = f"file://{self._cleaned_store.artifact_path(synchronization_id=synchronization_id, cleaning_id=cleaning_id, filename='cleaned.jsonl')}"
            report_uri = f"file://{self._cleaned_store.report_path(synchronization_id=synchronization_id, cleaning_id=cleaning_id)}"

            manifest_model = CleaningManifest(
                cleaning_id=cleaning_id,
                synchronization_id=synchronization_id,
                source_synchronized_sha256=manifest["synchronized_sha256"],
                synchronization_config_hash=manifest["synchronization_config_hash"],
                streams=stream_lineage,
                policy=policy_ref,
                cleaning_config_hash=config_hash,
                transform_version=policy.transform_version,
                status=status,
                input_rows=metrics.input_rows,
                retained_rows=metrics.retained_rows,
                dropped_rows=metrics.dropped_rows,
                redacted_rows=metrics.redacted_rows,
                cleaned_sha256=cleaned_sha256,
                cleaned_size_bytes=cleaned_size_bytes,
                artifact_uri=artifact_uri,
                report_uri=report_uri,
                created_at=datetime.now(timezone.utc),
                rejection_reasons=rejection_reasons,
            )
            write_manifest_file(staging_dir, "manifest.json", manifest_model.model_dump_json(indent=2))

            self._cleaned_store.commit(
                synchronization_id=synchronization_id, cleaning_id=cleaning_id, staging_dir=staging_dir
            )
        except Exception:
            self._cleaned_store.discard(staging_dir)
            logger.error(
                "CLEANING_FAILED cleaning_id=%s synchronization_id=%s policy_name=%s policy_version=%s",
                cleaning_id,
                synchronization_id,
                request.policy_name,
                request.policy_version,
            )
            raise

        log_event = "CLEANING_COMPLETED" if status == CleaningStatus.COMPLETED else "CLEANING_REJECTED"
        log = logger.info if status == CleaningStatus.COMPLETED else logger.warning
        log(
            "%s cleaning_id=%s synchronization_id=%s policy_name=%s policy_version=%s "
            "input_rows=%d retained_rows=%d dropped_rows=%d redacted_rows=%d status=%s",
            log_event,
            cleaning_id,
            synchronization_id,
            request.policy_name,
            request.policy_version,
            metrics.input_rows,
            metrics.retained_rows,
            metrics.dropped_rows,
            metrics.redacted_rows,
            status.value,
        )

        return CleaningResponse(
            cleaning_id=cleaning_id,
            synchronization_id=synchronization_id,
            status=status,
            policy=policy_ref,
            summary=summary,
            artifact_uri=artifact_uri,
            report_uri=report_uri,
            cleaned_sha256=cleaned_sha256,
            rejection_reasons=rejection_reasons,
        )

    def _validate_config(self, config: CleaningConfig) -> None:
        if config.min_present_streams is not None and config.min_present_streams < 0:
            raise InvalidCleaningConfigurationError("min_present_streams must not be negative")
        if config.minimum_retained_rows is not None and config.minimum_retained_rows < 0:
            raise InvalidCleaningConfigurationError("minimum_retained_rows must not be negative")
        for path in config.privacy.redact_fields:
            if not is_valid_field_path(path):
                raise InvalidRedactionPathError(f"'{path}' is not a valid redaction field path")

    def _determine_status(
        self, metrics: CleaningMetricsAccumulator, config: CleaningConfig
    ) -> tuple[CleaningStatus, list[str]]:
        if metrics.input_rows == 0:
            return CleaningStatus.REJECTED, ["EMPTY_SYNCHRONIZED_DATASET"]
        if config.minimum_retained_rows is not None and metrics.retained_rows < config.minimum_retained_rows:
            return CleaningStatus.REJECTED, ["INSUFFICIENT_RETAINED_ROWS"]
        return CleaningStatus.COMPLETED, []

    def _process_rows(
        self,
        *,
        artifact_path: Path,
        evaluator: RowEvaluator,
        metrics: CleaningMetricsAccumulator,
        staging_dir: Path,
    ) -> tuple[str, int]:
        cleaned_path = staging_dir / "cleaned.jsonl"
        digest = ChunkedSha256()
        size_bytes = 0

        with artifact_path.open("r", encoding="utf-8") as source, cleaned_path.open("wb") as out_file:
            row_index = 0
            for line in source:
                stripped = line.strip()
                if not stripped:
                    continue
                row_index += 1

                row = json.loads(stripped)
                timestamp = row.get("timestamp")

                cleaned_row, drop_reasons, redactions = evaluator.evaluate(row_index, row)
                if drop_reasons:
                    metrics.record_dropped(row_index, timestamp, drop_reasons)
                    continue

                metrics.record_kept(row_index, timestamp, redactions)
                output_line = (canonical_json(cleaned_row) + "\n").encode("utf-8")
                digest.update(output_line)
                size_bytes += len(output_line)
                out_file.write(output_line)

        return digest.hexdigest(), size_bytes
