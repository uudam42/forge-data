"""The pipeline executor (v2.6, Design Requirements 6/9/30).

Orchestrates the EXISTING stage services directly -- never through HTTP,
never duplicating stage logic (same principle as v2.5's
SelectiveRebuildExecutor). Cancellation and progress are checked/recorded
at STAGE BOUNDARIES (before each of the N stages a run performs) rather
than inside any stage's own per-record loop -- see
docs/DETAILED_GUIDE.md's v2.6 section, "Cancellation and progress
granularity", for why: the per-record loops live one layer deeper than
the service methods this executor calls (e.g. validation's row loop is
inside a separate Validator implementation, not ValidationService
itself), and reaching into every one of those would mean rewriting
stage-internal algorithms, which is explicitly out of scope. A
cancellation request is therefore observed within, at most, the
duration of whichever single stage call is currently in flight -- never
mid-stage, but also never as slow as waiting for the whole run.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import BinaryIO

from app.catalog import governance
from app.catalog.errors import ArtifactDeprecatedError, ArtifactInvalidError, UpstreamArtifactDeprecatedError, UpstreamArtifactInvalidError
from app.core.config import Settings
from app.runs.cancellation import CancellationToken
from app.runs.error_adapter import normalize_stage_error
from app.runs.errors import RunCancellationRequested, StageFailure
from app.runs.models import PipelineRunRequest
from app.runs.progress import DatabaseProgressReporter
from app.sensors.registry import SensorPluginRegistry, get_default_registry

logger = logging.getLogger("app.runs.executor")


def plan_stages(sensor_types: list[str]) -> list[str]:
    """The ordered list of stage names one PipelineRun performs for a
    given set of streams -- four per-stream stages followed by five
    shared downstream stages. Pulled out of `PipelineRunner.execute` so
    `forge run --dry-run` (v2.7) can show a run's real planned stage list
    without duplicating this ordering by hand."""
    planned: list[str] = []
    for sensor_type in sensor_types:
        planned += [f"ingestion:{sensor_type}", f"validation:{sensor_type}", f"integrity:{sensor_type}", f"normalization:{sensor_type}"]
    planned += ["synchronization", "cleaning", "transformation", "qc", "package"]
    return planned


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StreamFile:
    sensor_type: str
    filename: str
    content_type: str | None
    stream: BinaryIO
    source_units: dict[str, str]


class PipelineExecutionFailed(Exception):
    """Wraps a normalized StageFailure -- raised by _run_stage so the top-
    level execute() can mark remaining stages skipped and the run failed
    without re-deriving the failure summary."""

    def __init__(self, failure: StageFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


class PipelineRunner:
    def __init__(self, *, settings: Settings, repo, catalog_repo, sensor_registry: SensorPluginRegistry | None = None) -> None:
        self._settings = settings
        self._repo = repo  # app.runs.repository.RunRepository
        self._catalog_repo = catalog_repo  # app.catalog.repository.CatalogRepository (governance reads only)
        self._sensor_registry = sensor_registry or get_default_registry()
        self._stage_run_ids: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Stage bookkeeping
    # ------------------------------------------------------------------

    def _create_all_stage_runs(self, run_id: str, planned_stages: list[str]) -> None:
        """All stage rows are created up front, in `pending`, before any
        of them run -- this is what makes `pending` an observable state
        from the moment a run starts (Design Requirement 2) and what
        makes `stages_total` in the API response correct even one stage
        into a long run, rather than growing as the run progresses."""
        with self._repo.transaction(operation="create_stage_runs"):
            for stage in planned_stages:
                stage_run_id = f"stagerun_{uuid.uuid4().hex}"
                self._repo.create_stage_run(stage_run_id=stage_run_id, run_id=run_id, stage=stage, status="pending")
                self._stage_run_ids[stage] = stage_run_id

    def _skip_remaining(self, remaining_stages: list[str]) -> None:
        for stage in remaining_stages:
            DatabaseProgressReporter(self._repo, self._stage_run_ids[stage]).skip_stage()

    def _cancel_remaining(self, remaining_stages: list[str]) -> None:
        for stage in remaining_stages:
            DatabaseProgressReporter(self._repo, self._stage_run_ids[stage]).cancel_stage()

    def _gate(self, *, artifact_type: str, artifact_id: str, allow_deprecated: bool) -> None:
        governance.enforce_upstream_gate(self._catalog_repo, artifact_type=artifact_type, artifact_id=artifact_id, allow_deprecated=allow_deprecated)

    def _record_artifact(self, run_id: str, stage: str, artifact_type: str, artifact_id: str) -> None:
        with self._repo.transaction(operation="record_run_artifact"):
            self._repo.record_run_artifact(run_id=run_id, stage=stage, artifact_type=artifact_type, artifact_id=artifact_id, created_at=_now())

    def _run_stage(self, run_id: str, stage: str, cancellation: CancellationToken, fn):
        """Runs one stage's real work with full bookkeeping: pending ->
        running -> {completed, failed, cancelled}. `fn` returns
        (artifact_type, artifact_id, records_processed, records_total,
        bytes_processed)."""
        with self._repo.transaction(operation="run_heartbeat"):
            self._repo.touch_heartbeat(run_id, last_heartbeat_at=_now())

        stage_run_id = self._stage_run_ids[stage]
        reporter = DatabaseProgressReporter(self._repo, stage_run_id)

        # Checked BEFORE touching the run's or this stage's status at
        # all -- a real, more serious bug caught here in development:
        # unconditionally setting status="running" first (as an earlier
        # version of this method did) OVERWRITES a `cancel_requested`
        # status back to `running` on every single stage boundary,
        # before the check below ever gets a chance to observe it --
        # which means cancellation could NEVER actually take effect
        # under normal operation. Checking first, and only updating
        # status afterward if not cancelled, is what makes the
        # subsequent `update_run_status(status="running", ...)` never
        # clobber a pending cancellation.
        try:
            cancellation.check(force=True)
        except RunCancellationRequested:
            reporter.cancel_stage()  # pending -> cancelled directly; the run's own status is untouched here
            raise

        with self._repo.transaction(operation="update_run_current_stage"):
            self._repo.update_run_status(run_id, status="running", current_stage=stage)
        reporter.start_stage()

        try:
            artifact_type, artifact_id, records_processed, records_total, bytes_processed = fn()
        except RunCancellationRequested:
            reporter.cancel_stage()
            raise
        except (ArtifactInvalidError, UpstreamArtifactInvalidError, ArtifactDeprecatedError, UpstreamArtifactDeprecatedError) as exc:
            failure = StageFailure(stage=stage, code=exc.to_dict().pop("code"), message=str(exc))
            reporter.fail_stage(error_code=failure.code, error_message=failure.message)
            logger.warning("RUN_STAGE_GOVERNANCE_BLOCKED run_id=%s stage=%s code=%s", run_id, stage, failure.code)
            raise PipelineExecutionFailed(failure) from exc
        except Exception as exc:
            failure = normalize_stage_error(stage, exc)
            reporter.fail_stage(error_code=failure.code, error_message=failure.message)
            logger.exception("RUN_STAGE_FAILED run_id=%s stage=%s code=%s", run_id, stage, failure.code)
            raise PipelineExecutionFailed(failure) from exc

        reporter.complete_stage(records_processed=records_processed, bytes_processed=bytes_processed, artifacts_created=1)
        if records_total is not None:
            with self._repo.transaction(operation="stage_records_total"):
                self._repo.update_stage_run(stage_run_id, records_total=records_total)
        self._record_artifact(run_id, stage, artifact_type, artifact_id)
        logger.info("RUN_STAGE_COMPLETED run_id=%s stage=%s artifact_type=%s artifact_id=%s", run_id, stage, artifact_type, artifact_id)
        return artifact_id

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, *, run_id: str, request: PipelineRunRequest, stream_files: list[StreamFile], cancellation: CancellationToken, allow_deprecated: bool = False) -> str:
        """Returns the produced package_id. Raises PipelineExecutionFailed
        (already-recorded structured failure) or RunCancellationRequested
        (already-recorded cancellation) -- the caller (LocalRunExecutor)
        maps either into the run's final status; it never needs to
        inspect stage internals itself."""
        self._current_run_id = run_id
        # All streams in one run share ONE session_id -- ingesting each
        # stream separately without this would give every stream its own
        # random session, and synchronization refuses to align streams
        # from different sessions (a real bug caught here in development:
        # the first version of this executor left session_id unset per
        # stream and every multi-stream run failed at synchronization).
        session_id = request.session_id or f"sess_{uuid.uuid4().hex}"
        planned_stages = plan_stages([f.sensor_type for f in stream_files])
        self._create_all_stage_runs(run_id, planned_stages)

        remaining = list(planned_stages)
        normalization_ids: dict[str, str] = {}

        try:
            for f in stream_files:
                plugin = self._sensor_registry.get(f.sensor_type)

                ingestion_id = self._run_stage(run_id, remaining.pop(0), cancellation, lambda f=f: self._do_ingest(f, session_id, request))
                validation_id = self._run_stage(
                    run_id, remaining.pop(0), cancellation, lambda ingestion_id=ingestion_id, plugin=plugin: self._do_validate(ingestion_id, plugin)
                )
                integrity_id = self._run_stage(
                    run_id, remaining.pop(0), cancellation, lambda ingestion_id=ingestion_id, plugin=plugin: self._do_integrity(ingestion_id, plugin)
                )
                norm_id = self._run_stage(
                    run_id, remaining.pop(0), cancellation,
                    lambda ingestion_id=ingestion_id, plugin=plugin, f=f: self._do_normalize(ingestion_id, plugin, f, allow_deprecated),
                )
                normalization_ids[plugin.sensor_type] = norm_id

            sync_id = self._run_stage(run_id, remaining.pop(0), cancellation, lambda: self._do_synchronize(request, normalization_ids, allow_deprecated))
            cleaning_id = self._run_stage(run_id, remaining.pop(0), cancellation, lambda: self._do_clean(request, sync_id, allow_deprecated))
            xform_id = self._run_stage(run_id, remaining.pop(0), cancellation, lambda: self._do_transform(request, cleaning_id, allow_deprecated))
            qc_id = self._run_stage(run_id, remaining.pop(0), cancellation, lambda: self._do_qc(request, xform_id, allow_deprecated))
            package_id = self._run_stage(run_id, remaining.pop(0), cancellation, lambda: self._do_package(request, xform_id, qc_id, allow_deprecated))
            return package_id
        except PipelineExecutionFailed:
            self._skip_remaining(remaining)
            raise
        except RunCancellationRequested:
            self._cancel_remaining(remaining)
            raise

    # ------------------------------------------------------------------
    # Per-stage work -- each returns (artifact_type, artifact_id,
    # records_processed, records_total, bytes_processed)
    # ------------------------------------------------------------------

    def _do_ingest(self, f: StreamFile, session_id: str, request: PipelineRunRequest):
        from app.ingestion.service import IngestionService, UploadRequest
        from app.storage.local import LocalRawStorage

        service = IngestionService(storage=LocalRawStorage(root=self._settings.RAW_STORAGE_ROOT), settings=self._settings)
        response = service.ingest(
            UploadRequest(
                filename=f.filename, content_type=f.content_type, stream=f.stream,
                customer_id=request.customer_id, device_id=request.device_id, session_id=session_id,
                source_type=f.sensor_type, notes=None,
            )
        )
        return "ingestion", response.ingestion_id, 1, 1, response.size_bytes

    def _do_validate(self, ingestion_id: str, plugin):
        from app.storage.local import LocalRawStorage
        from app.storage.validation_store import LocalValidationReportStore
        from app.validation.registry import ValidatorRegistry
        from app.validation.schemas.registry import SchemaRegistry
        from app.validation.service import ValidationService

        service = ValidationService(
            storage=LocalRawStorage(root=self._settings.RAW_STORAGE_ROOT),
            schema_registry=SchemaRegistry(schema_dir=self._settings.SCHEMA_DIR),
            validator_registry=ValidatorRegistry(),
            report_store=LocalValidationReportStore(root=self._settings.VALIDATION_STORAGE_ROOT),
            settings=self._settings,
        )
        response = service.validate(ingestion_id=ingestion_id, schema_name=plugin.sensor_type, schema_version=plugin.schema_version)
        return "validation", response.validation_id, response.summary.records_checked, response.summary.records_checked, None

    def _do_integrity(self, ingestion_id: str, plugin):
        from app.integrity.registry import IntegrityCheckerRegistry
        from app.integrity.service import IntegrityService
        from app.storage.integrity_store import LocalIntegrityReportStore
        from app.storage.local import LocalRawStorage
        from app.storage.validation_store import LocalValidationReportStore
        from app.validation.schemas.registry import SchemaRegistry

        service = IntegrityService(
            storage=LocalRawStorage(root=self._settings.RAW_STORAGE_ROOT),
            schema_registry=SchemaRegistry(schema_dir=self._settings.SCHEMA_DIR),
            validation_report_store=LocalValidationReportStore(root=self._settings.VALIDATION_STORAGE_ROOT),
            checker_registry=IntegrityCheckerRegistry(),
            report_store=LocalIntegrityReportStore(root=self._settings.INTEGRITY_STORAGE_ROOT),
            settings=self._settings,
        )
        response = service.run(ingestion_id=ingestion_id, schema_name=plugin.sensor_type, schema_version=plugin.schema_version)
        return "integrity", response.integrity_id, response.checked_records, response.total_records, None

    def _do_normalize(self, ingestion_id: str, plugin, f: StreamFile, allow_deprecated: bool):
        self._gate(artifact_type="ingestion", artifact_id=ingestion_id, allow_deprecated=allow_deprecated)

        from app.normalization.registry import NormalizationProfileRegistry
        from app.normalization.service import NormalizationService
        from app.storage.integrity_store import LocalIntegrityReportStore
        from app.storage.local import LocalRawStorage
        from app.storage.normalized_store import LocalNormalizedArtifactStore
        from app.storage.validation_store import LocalValidationReportStore
        from app.validation.schemas.registry import SchemaRegistry

        service = NormalizationService(
            storage=LocalRawStorage(root=self._settings.RAW_STORAGE_ROOT),
            schema_registry=SchemaRegistry(schema_dir=self._settings.SCHEMA_DIR),
            validation_report_store=LocalValidationReportStore(root=self._settings.VALIDATION_STORAGE_ROOT),
            integrity_report_store=LocalIntegrityReportStore(root=self._settings.INTEGRITY_STORAGE_ROOT),
            profile_registry=NormalizationProfileRegistry(),
            artifact_store=LocalNormalizedArtifactStore(root=self._settings.NORMALIZED_STORAGE_ROOT),
            settings=self._settings,
        )
        response = service.normalize(
            ingestion_id=ingestion_id, schema_name=plugin.sensor_type, schema_version=plugin.schema_version,
            profile_name=plugin.normalization_profile.profile_name, profile_version=plugin.normalization_profile.profile_version,
            source_units=f.source_units,
        )
        return "normalization", response.normalization_id, response.records_written, response.records_written, None

    def _do_synchronize(self, request: PipelineRunRequest, normalization_ids: dict[str, str], allow_deprecated: bool):
        for sensor_type, norm_id in normalization_ids.items():
            self._gate(artifact_type="normalization", artifact_id=norm_id, allow_deprecated=allow_deprecated)

        from app.storage.local import LocalRawStorage
        from app.storage.normalized_store import LocalNormalizedArtifactStore
        from app.storage.synchronization_store import LocalSynchronizationArtifactStore
        from app.synchronization.models import StreamRequest, SynchronizationRequest
        from app.synchronization.registry import AlignmentStrategyRegistry
        from app.synchronization.service import SynchronizationService
        from app.validation.schemas.registry import SchemaRegistry

        service = SynchronizationService(
            raw_storage=LocalRawStorage(root=self._settings.RAW_STORAGE_ROOT),
            normalized_store=LocalNormalizedArtifactStore(root=self._settings.NORMALIZED_STORAGE_ROOT),
            schema_registry=SchemaRegistry(schema_dir=self._settings.SCHEMA_DIR),
            strategy_registry=AlignmentStrategyRegistry(),
            artifact_store=LocalSynchronizationArtifactStore(root=self._settings.SYNCHRONIZED_STORAGE_ROOT),
            settings=self._settings,
        )
        sync_cfg = request.synchronization
        streams = [StreamRequest(name=sensor_type, normalization_id=norm_id) for sensor_type, norm_id in normalization_ids.items()]
        sync_request = SynchronizationRequest(streams=streams, reference=sync_cfg.reference, alignment=sync_cfg.alignment, clock_corrections=sync_cfg.clock_corrections)
        response = service.synchronize(sync_request)
        return "synchronization", response.synchronization_id, response.rows_written, response.rows_written, None

    def _do_clean(self, request: PipelineRunRequest, synchronization_id: str, allow_deprecated: bool):
        self._gate(artifact_type="synchronization", artifact_id=synchronization_id, allow_deprecated=allow_deprecated)

        from app.cleaning.registry import CleaningPolicyRegistry
        from app.cleaning.service import CleaningService
        from app.storage.cleaned_store import LocalCleanedArtifactStore
        from app.storage.synchronization_store import LocalSynchronizationArtifactStore

        service = CleaningService(
            settings=self._settings,
            sync_store=LocalSynchronizationArtifactStore(root=self._settings.SYNCHRONIZED_STORAGE_ROOT),
            policy_registry=CleaningPolicyRegistry(),
            cleaned_store=LocalCleanedArtifactStore(root=self._settings.CLEANED_STORAGE_ROOT),
        )
        cfg = request.cleaning
        from app.cleaning.models import CleaningRequest

        response = service.clean(synchronization_id=synchronization_id, request=CleaningRequest(policy_name=cfg.policy_name, policy_version=cfg.policy_version, config=cfg.config))
        return "cleaning", response.cleaning_id, response.summary.retained_rows, response.summary.input_rows, None

    def _do_transform(self, request: PipelineRunRequest, cleaning_id: str, allow_deprecated: bool):
        self._gate(artifact_type="cleaning", artifact_id=cleaning_id, allow_deprecated=allow_deprecated)

        from app.storage.cleaned_store import LocalCleanedArtifactStore
        from app.storage.transformed_store import LocalTransformedArtifactStore
        from app.transformation.models import TransformationRequest
        from app.transformation.registry import TransformationProfileRegistry
        from app.transformation.service import TransformationService

        service = TransformationService(
            settings=self._settings,
            cleaned_store=LocalCleanedArtifactStore(root=self._settings.CLEANED_STORAGE_ROOT),
            profile_registry=TransformationProfileRegistry(),
            transformed_store=LocalTransformedArtifactStore(root=self._settings.TRANSFORMED_STORAGE_ROOT),
        )
        cfg = request.transformation
        response = service.transform(cleaning_id=cleaning_id, request=TransformationRequest(profile_name=cfg.profile_name, profile_version=cfg.profile_version, config=cfg.config))
        return "transformation", response.transformation_id, response.summary.samples_written, response.summary.input_rows, None

    def _do_qc(self, request: PipelineRunRequest, transformation_id: str, allow_deprecated: bool):
        self._gate(artifact_type="transformation", artifact_id=transformation_id, allow_deprecated=allow_deprecated)

        from app.qc.models import QCRequest
        from app.qc.registry import QCProfileRegistry
        from app.qc.service import QCService
        from app.storage.qc_store import LocalQCReportStore
        from app.storage.transformed_store import LocalTransformedArtifactStore

        service = QCService(
            settings=self._settings,
            transformed_store=LocalTransformedArtifactStore(root=self._settings.TRANSFORMED_STORAGE_ROOT),
            profile_registry=QCProfileRegistry(),
            qc_store=LocalQCReportStore(root=self._settings.QC_STORAGE_ROOT),
        )
        cfg = request.qc
        response = service.run_qc(transformation_id=transformation_id, request=QCRequest(profile_name=cfg.profile_name, profile_version=cfg.profile_version, config=cfg.config))
        return "qc", response.qc_id, response.summary.samples_checked, response.summary.samples_checked, None

    def _do_package(self, request: PipelineRunRequest, transformation_id: str, qc_id: str, allow_deprecated: bool):
        self._gate(artifact_type="transformation", artifact_id=transformation_id, allow_deprecated=allow_deprecated)
        self._gate(artifact_type="qc", artifact_id=qc_id, allow_deprecated=allow_deprecated)

        from app.packaging.models import PackagingRequest
        from app.packaging.registry import PackagingProfileRegistry
        from app.packaging.service import PackagingService
        from app.storage.package_store import LocalDatasetPackageStore
        from app.storage.qc_store import LocalQCReportStore
        from app.storage.transformed_store import LocalTransformedArtifactStore

        service = PackagingService(
            settings=self._settings,
            transformed_store=LocalTransformedArtifactStore(root=self._settings.TRANSFORMED_STORAGE_ROOT),
            qc_store=LocalQCReportStore(root=self._settings.QC_STORAGE_ROOT),
            profile_registry=PackagingProfileRegistry(),
            package_store=LocalDatasetPackageStore(root=self._settings.PACKAGE_STORAGE_ROOT),
        )
        cfg = request.packaging
        response = service.package(
            transformation_id=transformation_id,
            request=PackagingRequest(
                qc_id=qc_id, profile_name=cfg.profile_name, profile_version=cfg.profile_version, config=cfg.config,
                dataset_name=cfg.dataset_name, dataset_version=cfg.dataset_version, description=cfg.description,
            ),
        )
        return "package", response.package_id, response.summary.packaged_samples, response.summary.source_samples, None
