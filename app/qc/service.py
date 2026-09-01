"""QC business logic:
TRANSFORMED ARTIFACT -> DATASET-LEVEL METRIC COLLECTION -> PROFILE-DRIVEN
CHECK EVALUATION -> QC REPORT + MANIFEST.

Steps 1-7 establish "this data exists, is valid, canonically represented,
temporally aligned, free of rows that shouldn't be there, and turned into
feature windows." Step 8 asks a different question: "is this transformed
dataset, as a WHOLE, healthy enough for ML use?" It never repeats Step 3's
per-value plausibility judgment, Step 6's per-row keep/drop judgment, or
Step 7's per-window feature computation — it only reports on the dataset
that already exists.

This module never opens the transformed artifact for writing and never
touches its manifest — it only reads it, and writes exclusively to its
own separate QC report store.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger
from app.qc.checks.drift import BaselineNotFoundError, evaluate_drift, load_baseline
from app.qc.checks.group_distribution import evaluate_group_imbalance
from app.qc.metrics import DatasetMetricsCollector
from app.qc.models import (
    QC_ENGINE_VERSION,
    DatasetSummary,
    DriftSummary,
    FeatureDistributionSummary,
    ModalityCoverageSummary,
    ProfileRef,
    QCConfig,
    QCIssue,
    QCManifest,
    QCReport,
    QCRequest,
    QCResponse,
    QCStatus,
    QCSummary,
    Severity,
    UpstreamTransformationLineage,
    WindowSizeSummary,
)
from app.qc.registry import QCProfileRegistry
from app.qc.serialization import canonical_json, compute_report_sha256
from app.storage.atomic import write_manifest_file
from app.storage.qc_store import QCReportStore
from app.storage.transformed_store import TransformedArtifactStore
from app.utils.hashing import sha256_of_path
from app.utils.ids import generate_qc_id

logger = get_logger("app.qc")

_SUPPORTED_EXTENSIONS = (".jsonl",)  # JSONL transformed input only for the Step 8 MVP
_ARTIFACT_FILENAME = "transformed.jsonl"  # Step 7 always commits under this fixed name


class QCError(Exception):
    """Base class for QC-service failures mapped to HTTP by the API layer."""


class TransformationNotFoundError(QCError):
    pass


class TransformedArtifactChecksumMismatchError(QCError):
    pass


class UnsupportedQCFileTypeError(QCError):
    pass


class QCService:
    def __init__(
        self,
        *,
        transformed_store: TransformedArtifactStore,
        profile_registry: QCProfileRegistry,
        qc_store: QCReportStore,
        settings: Settings,
    ) -> None:
        self._transformed_store = transformed_store
        self._profile_registry = profile_registry
        self._qc_store = qc_store
        self._settings = settings

    def run_qc(self, *, transformation_id: str, request: QCRequest) -> QCResponse:
        manifest = self._transformed_store.find_manifest_by_transformation_id(transformation_id)
        if manifest is None:
            raise TransformationNotFoundError(
                f"No transformation run found with transformation_id='{transformation_id}'"
            )
        if manifest.get("transformation_id") != transformation_id:
            raise TransformationNotFoundError(
                f"Transformation manifest for '{transformation_id}' is inconsistent with its own ID"
            )
        # Step 7 only ever commits a manifest for a successfully completed
        # run (it has no "rejected" concept, unlike Step 6's cleaning
        # manifest) — a committed, self-consistent manifest IS the proof
        # that this transformation's status is acceptable.

        cleaning_id = manifest["cleaning_id"]

        # Raises QCProfileNotFoundError directly (already the right
        # name/semantics for the API layer to catch).
        profile = self._profile_registry.get(request.profile_name, request.profile_version)

        extension = Path(_ARTIFACT_FILENAME).suffix
        if extension not in _SUPPORTED_EXTENSIONS:
            raise UnsupportedQCFileTypeError(
                f"QC is not supported for transformed artifact type '{extension}'"
            )

        artifact_path = Path(
            self._transformed_store.artifact_path(
                cleaning_id=cleaning_id, transformation_id=transformation_id, filename=_ARTIFACT_FILENAME
            )
        )
        if not artifact_path.exists():
            raise TransformationNotFoundError(
                f"Transformed artifact file is missing on disk for transformation_id='{transformation_id}'"
            )

        computed_sha256 = sha256_of_path(artifact_path)
        if computed_sha256 != manifest["transformed_sha256"]:
            raise TransformedArtifactChecksumMismatchError(
                f"Transformed artifact for transformation_id='{transformation_id}' has been modified "
                f"since transformation: expected sha256={manifest['transformed_sha256']}, "
                f"computed={computed_sha256}"
            )

        # Raises InvalidQCConfigurationError directly.
        profile.validate_config(request.config)

        qc_id = generate_qc_id()
        logger.info(
            "QC_STARTED qc_id=%s transformation_id=%s profile_name=%s profile_version=%s",
            qc_id,
            transformation_id,
            request.profile_name,
            request.profile_version,
        )

        staging_dir = self._qc_store.staging_dir(transformation_id=transformation_id, qc_id=qc_id)
        config_hash = profile.config_hash(request.config)

        try:
            metrics = self._collect_metrics(artifact_path)
            checks = profile.build_checks(request.config)
            issues: list[QCIssue] = []
            for check in checks:
                issues.extend(check.evaluate(metrics, request.config))

            session_distribution = self._session_distribution(manifest, metrics.sample_count)
            issues.extend(evaluate_group_imbalance(session_distribution, request.config))

            features_summary = self._build_feature_summaries(metrics)

            drift_summary: DriftSummary | None = None
            if request.config.baseline_qc_id is not None:
                baseline_manifest, baseline_report = load_baseline(self._qc_store, request.config.baseline_qc_id)
                current_for_drift = {
                    path: {"mean": fd.mean, "std": fd.std} for path, fd in features_summary.items()
                }
                drift_summary, drift_issues = evaluate_drift(
                    current_features=current_for_drift,
                    baseline_manifest=baseline_manifest,
                    baseline_report=baseline_report,
                    baseline_qc_id=request.config.baseline_qc_id,
                    current_profile=ProfileRef(name=request.profile_name, version=request.profile_version),
                    drift_config=request.config.drift,
                )
                issues.extend(drift_issues)

            # Counts reflect the TRUE total (every issue found), even though
            # the detailed `issues` list below is bounded — truncation only
            # ever trims stored detail, never the reported counts.
            status = self._determine_status(issues)
            warning_count = sum(1 for i in issues if i.severity == Severity.WARNING)
            error_count = sum(1 for i in issues if i.severity == Severity.ERROR)
            issue_count = len(issues)

            issues, issues_truncated = self._bound_issues(issues)

            summary = QCSummary(
                samples_checked=metrics.sample_count,
                issue_count=issue_count,
                warning_count=warning_count,
                error_count=error_count,
            )

            dataset_summary = DatasetSummary(
                sample_count=metrics.sample_count,
                earliest_timestamp=metrics.earliest_timestamp,
                latest_timestamp=metrics.latest_timestamp,
                duration_seconds=metrics.duration_seconds,
            )

            modality_summary = {}
            for name in metrics.known_modalities:
                accumulator = metrics.modality[name]
                coverage_stats = accumulator.coverage_stats()
                modality_summary[name] = ModalityCoverageSummary(
                    samples_present=accumulator.present_count,
                    coverage_ratio=(
                        accumulator.present_count / metrics.sample_count if metrics.sample_count else 0.0
                    ),
                    mean_window_coverage=coverage_stats["mean"],
                    median_window_coverage=coverage_stats["median"],
                    minimum_window_coverage=coverage_stats["min"],
                    maximum_window_coverage=coverage_stats["max"],
                )

            window_size_summary = WindowSizeSummary(
                row_count_min=int(metrics.window_row_counts.min) if metrics.window_row_counts.count else None,
                row_count_max=int(metrics.window_row_counts.max) if metrics.window_row_counts.count else None,
                row_count_mean=metrics.window_row_counts.mean if metrics.window_row_counts.count else None,
                row_count_std=metrics.window_row_counts.std if metrics.window_row_counts.count else None,
            )

            report = QCReport(
                qc_id=qc_id,
                transformation_id=transformation_id,
                status=status,
                summary=summary,
                dataset=dataset_summary,
                modality_coverage=modality_summary,
                features=features_summary,
                window_size=window_size_summary,
                session_distribution=session_distribution,
                drift=drift_summary,
                issues=issues,
                issues_truncated=issues_truncated,
            )
            report_bytes = canonical_json(report.model_dump(mode="json")).encode("utf-8")
            (staging_dir / "report.json").write_bytes(report_bytes)
            report_sha256 = compute_report_sha256(report_bytes)

            report_uri = f"file://{self._qc_store.report_path(transformation_id=transformation_id, qc_id=qc_id)}"

            upstream = UpstreamTransformationLineage(
                transformation_id=transformation_id,
                cleaning_id=cleaning_id,
                synchronization_id=manifest["upstream"]["synchronization_id"],
                transformation_config_hash=manifest["transformation_config_hash"],
                session_ids=manifest["upstream"].get("session_ids", []),
            )
            profile_ref = ProfileRef(name=request.profile_name, version=request.profile_version)
            manifest_model = QCManifest(
                qc_id=qc_id,
                transformation_id=transformation_id,
                upstream=upstream,
                source_transformed_sha256=manifest["transformed_sha256"],
                profile=profile_ref,
                qc_config_hash=config_hash,
                qc_engine_version=QC_ENGINE_VERSION,
                status=status,
                samples_checked=metrics.sample_count,
                warning_count=warning_count,
                error_count=error_count,
                report_sha256=report_sha256,
                report_uri=report_uri,
                created_at=datetime.now(timezone.utc),
            )
            write_manifest_file(staging_dir, "manifest.json", manifest_model.model_dump_json(indent=2))

            self._qc_store.commit(transformation_id=transformation_id, qc_id=qc_id, staging_dir=staging_dir)
        except Exception:
            self._qc_store.discard(staging_dir)
            logger.error(
                "QC_FAILED qc_id=%s transformation_id=%s profile_name=%s profile_version=%s",
                qc_id,
                transformation_id,
                request.profile_name,
                request.profile_version,
            )
            raise

        logger.info(
            "QC_COMPLETED qc_id=%s transformation_id=%s profile_name=%s profile_version=%s "
            "samples_checked=%d warning_count=%d error_count=%d status=%s",
            qc_id,
            transformation_id,
            request.profile_name,
            request.profile_version,
            metrics.sample_count,
            warning_count,
            error_count,
            status.value,
        )

        return QCResponse(
            qc_id=qc_id,
            transformation_id=transformation_id,
            status=status,
            profile=profile_ref,
            summary=summary,
            report_uri=report_uri,
        )

    def _collect_metrics(self, artifact_path: Path):
        collector = DatasetMetricsCollector(max_values_per_feature=self._settings.MAX_QC_VALUES_PER_FEATURE)
        with artifact_path.open("r", encoding="utf-8") as source:
            index = 0
            for line in source:
                stripped = line.strip()
                if not stripped:
                    continue
                sample = json.loads(stripped)
                collector.observe_sample(index, sample)
                index += 1
        return collector.metrics

    def _build_feature_summaries(self, metrics) -> dict[str, FeatureDistributionSummary]:
        summaries = {}
        for path in sorted(metrics.feature_accumulators.keys()):
            accumulator = metrics.feature_accumulators[path]
            percentiles = accumulator.percentile_buffer.percentiles()
            has_data = accumulator.present_count > 0
            summaries[path] = FeatureDistributionSummary(
                count=accumulator.present_count,
                missing_count=accumulator.missing_count,
                null_count=accumulator.null_count,
                non_finite_count=accumulator.non_finite_count,
                mean=accumulator.welford.mean if has_data else None,
                std=accumulator.welford.std if has_data else None,
                min=accumulator.welford.min,
                max=accumulator.welford.max,
                median=percentiles["p50"],
                p01=percentiles["p01"],
                p05=percentiles["p05"],
                p25=percentiles["p25"],
                p50=percentiles["p50"],
                p75=percentiles["p75"],
                p95=percentiles["p95"],
                p99=percentiles["p99"],
                percentiles_truncated=accumulator.percentile_buffer.truncated,
            )
        return summaries

    def _session_distribution(self, manifest: dict, sample_count: int) -> dict[str, int] | None:
        session_ids = manifest.get("upstream", {}).get("session_ids", [])
        if len(session_ids) != 1:
            # Ambiguous/unavailable per-sample attribution — never fabricate
            # a multi-session breakdown from dataset-wide lineage alone.
            return None
        return {session_ids[0]: sample_count}

    def _bound_issues(self, issues: list[QCIssue]) -> tuple[list[QCIssue], bool]:
        limit = self._settings.MAX_QC_ISSUE_DETAILS
        if len(issues) <= limit:
            return issues, False
        return issues[:limit], True

    def _determine_status(self, issues: list[QCIssue]) -> QCStatus:
        if any(issue.severity == Severity.ERROR for issue in issues):
            return QCStatus.FAILED
        if any(issue.severity == Severity.WARNING for issue in issues):
            return QCStatus.PASSED_WITH_WARNINGS
        return QCStatus.PASSED
