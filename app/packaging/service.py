"""Packaging business logic:
TRANSFORMED ARTIFACT + ACCEPTED QC REPORT -> GROUP-AWARE, LEAKAGE-SAFE
SPLIT ASSIGNMENT -> TRAIN/VALIDATION/TEST PACKAGE + SPLIT INDEX + REPORT.

Steps 1-8 establish "this data exists, is valid, canonically represented,
temporally aligned, transformed into feature windows, and judged healthy
enough for ML use." Step 9 asks a different question: "how do we turn
this into a reproducible, leakage-safe, versioned package a model can
actually be trained on?" It reorganizes existing ML samples into split
files — it never changes their semantic content, generates new features,
imputes values, normalizes data, or re-runs QC.

This module never opens the transformed artifact or QC report for
writing and never touches their manifests — it only reads them, and
writes exclusively to its own separate package store.

Streaming/memory: a two-pass design over transformed.jsonl. Pass 1
extracts only lightweight per-sample identity (sample_id, source row
range) to compute group assignment — never full feature payloads,
keeping this phase's memory at O(number_of_samples) of small records, not
O(total feature payload size). Pass 2 re-reads the source once more and
writes each already-assigned sample directly into its split file, one
sample in memory at a time.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger
from app.packaging.exporters.base import ExportDependencyMissingError
from app.packaging.exporters.jsonl import JSONLExporter
from app.packaging.grouping import (
    MissingGroupMetadataError,
    SampleRecord,
    assign_session_groups,
    assign_source_overlap_groups,
)
from app.packaging.leakage import LeakageInvariantViolation, SampleCountMismatch, run_leakage_checks
from app.packaging.metrics import compute_split_stats
from app.packaging.models import (
    PACKAGE_ENGINE_VERSION,
    LeakageChecks,
    PackageManifest,
    PackageStatus,
    PackagingRejectionReason,
    PackagingReport,
    PackagingRequest,
    PackagingResponse,
    PackagingSummary,
    ProfileRef,
    SourceQCSummary,
    SplitManifestEntry,
    UpstreamPackagingLineage,
    SPLIT_NAMES,
)
from app.packaging.registry import PackagingProfileRegistry
from app.packaging.serialization import canonical_json, compute_file_sha256
from app.packaging.splitter import UnsupportedSplitStrategyError, assign_splits
from app.qc.models import QCStatus
from app.storage.atomic import write_manifest_file
from app.storage.package_store import DatasetPackageStore
from app.storage.qc_store import QCReportStore
from app.storage.transformed_store import TransformedArtifactStore
from app.utils.hashing import ChunkedSha256, sha256_of_path
from app.utils.ids import generate_package_id

logger = get_logger("app.packaging")

_SUPPORTED_EXTENSIONS = (".jsonl",)  # JSONL transformed input only for the Step 9 MVP
_ARTIFACT_FILENAME = "transformed.jsonl"  # Step 7 always commits under this fixed name
_ACCEPTED_QC_STATUSES = (QCStatus.PASSED, QCStatus.PASSED_WITH_WARNINGS)


class PackagingError(Exception):
    """Base class for packaging-service failures mapped to HTTP by the API layer."""


class TransformationNotFoundError(PackagingError):
    pass


class TransformedArtifactChecksumMismatchError(PackagingError):
    pass


class QCNotFoundError(PackagingError):
    pass


class QCTransformationMismatchError(PackagingError):
    pass


class QCReportChecksumMismatchError(PackagingError):
    pass


class QCNotAcceptedError(PackagingError):
    pass


class UnsupportedPackagingFileTypeError(PackagingError):
    pass


class MissingSampleIdError(PackagingError):
    pass


class DuplicateSampleIdError(PackagingError):
    pass


class PackagingService:
    def __init__(
        self,
        *,
        transformed_store: TransformedArtifactStore,
        qc_store: QCReportStore,
        profile_registry: PackagingProfileRegistry,
        package_store: DatasetPackageStore,
        settings: Settings,
    ) -> None:
        self._transformed_store = transformed_store
        self._qc_store = qc_store
        self._profile_registry = profile_registry
        self._package_store = package_store
        self._settings = settings
        self._jsonl_exporter = JSONLExporter()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def package(self, *, transformation_id: str, request: PackagingRequest) -> PackagingResponse:
        transformation_manifest = self._locate_transformation(transformation_id)
        cleaning_id = transformation_manifest["cleaning_id"]

        artifact_path = self._locate_transformed_artifact(transformation_manifest, cleaning_id, transformation_id)
        computed_transformed_sha256 = sha256_of_path(artifact_path)
        if computed_transformed_sha256 != transformation_manifest["transformed_sha256"]:
            raise TransformedArtifactChecksumMismatchError(
                f"Transformed artifact for transformation_id='{transformation_id}' has been modified "
                f"since transformation: expected sha256={transformation_manifest['transformed_sha256']}, "
                f"computed={computed_transformed_sha256}"
            )

        qc_manifest = self._locate_qc(transformation_id, request.qc_id)
        if qc_manifest["transformation_id"] != transformation_id:
            raise QCTransformationMismatchError(
                f"QC run '{request.qc_id}' belongs to transformation_id="
                f"'{qc_manifest['transformation_id']}', not '{transformation_id}'"
            )
        if qc_manifest["source_transformed_sha256"] != computed_transformed_sha256:
            raise TransformedArtifactChecksumMismatchError(
                f"QC run '{request.qc_id}' was computed against a different transformed artifact "
                f"(expected sha256={qc_manifest['source_transformed_sha256']}, "
                f"computed={computed_transformed_sha256})"
            )
        self._verify_qc_report_checksum(qc_manifest["transformation_id"], request.qc_id, qc_manifest)
        if qc_manifest["status"] not in (s.value for s in _ACCEPTED_QC_STATUSES):
            raise QCNotAcceptedError(
                f"QC run '{request.qc_id}' has status='{qc_manifest['status']}', not accepted "
                f"({', '.join(s.value for s in _ACCEPTED_QC_STATUSES)})"
            )

        # Raises PackagingProfileNotFoundError directly.
        profile = self._profile_registry.get(request.profile_name, request.profile_version)
        # Raises InvalidSplitRatiosError / UnsupportedSplitStrategyError /
        # UnsupportedGroupingModeError / UnsupportedExportFormatError directly.
        profile.validate_config(request.config)

        package_id = generate_package_id()
        split = request.config.split
        grouping_mode = request.config.grouping.mode
        logger.info(
            "PACKAGING_STARTED package_id=%s transformation_id=%s qc_id=%s profile_name=%s "
            "profile_version=%s strategy=%s grouping_mode=%s",
            package_id,
            transformation_id,
            request.qc_id,
            request.profile_name,
            request.profile_version,
            split.strategy,
            grouping_mode,
        )

        staging_dir = self._package_store.staging_dir(transformation_id=transformation_id, package_id=package_id)
        config_hash = profile.config_hash(request.config)

        try:
            result = self._run_packaging(
                artifact_path=artifact_path,
                staging_dir=staging_dir,
                request=request,
                profile=profile,
                transformed_sha256=computed_transformed_sha256,
                session_ids=transformation_manifest["upstream"].get("session_ids", []),
                transformation_id=transformation_id,
                package_id=package_id,
            )

            source_qc = SourceQCSummary(
                status=QCStatus(qc_manifest["status"]),
                warning_count=qc_manifest["warning_count"],
                error_count=qc_manifest["error_count"],
            )

            report = PackagingReport(
                package_id=package_id,
                transformation_id=transformation_id,
                qc_id=request.qc_id,
                status=result.status,
                summary=result.summary,
                requested_split_ratios=result.requested_ratios,
                actual=result.split_stats,
                leakage_checks=result.leakage_checks,
                source_qc=source_qc,
                rejection_reasons=result.rejection_reasons,
            )
            report_bytes = canonical_json(report.model_dump(mode="json")).encode("utf-8")
            (staging_dir / "report.json").write_bytes(report_bytes)
            report_sha256 = compute_file_sha256(report_bytes)

            report_uri = f"file://{self._package_store.report_path(transformation_id=transformation_id, package_id=package_id)}"

            upstream = UpstreamPackagingLineage(
                cleaning_id=cleaning_id,
                synchronization_id=transformation_manifest["upstream"]["synchronization_id"],
                transformation_config_hash=transformation_manifest["transformation_config_hash"],
                qc_config_hash=qc_manifest["qc_config_hash"],
                session_ids=transformation_manifest["upstream"].get("session_ids", []),
                normalization_ids=transformation_manifest["upstream"].get("normalization_ids", []),
            )
            profile_ref = ProfileRef(name=request.profile_name, version=request.profile_version)

            manifest_model = PackageManifest(
                package_id=package_id,
                transformation_id=transformation_id,
                qc_id=request.qc_id,
                upstream=upstream,
                source_transformed_sha256=computed_transformed_sha256,
                source_qc_report_sha256=qc_manifest["report_sha256"],
                source_qc_status=QCStatus(qc_manifest["status"]),
                profile=profile_ref,
                packaging_config_hash=config_hash,
                package_engine_version=PACKAGE_ENGINE_VERSION,
                status=result.status,
                split_strategy=split.strategy,
                grouping_mode=grouping_mode,
                seed=split.seed,
                requested_ratios=result.requested_ratios,
                source_samples=result.summary.source_samples,
                splits=result.split_manifest_entries,
                split_index_sha256=result.split_index_sha256,
                split_index_size_bytes=result.split_index_size_bytes,
                split_index_uri=(
                    f"file://{self._package_store.artifact_path(transformation_id=transformation_id, package_id=package_id, filename='split_index.jsonl')}"
                ),
                exports=result.export_manifest_entries or None,
                report_sha256=report_sha256,
                report_uri=report_uri,
                created_at=datetime.now(timezone.utc),
                dataset_name=request.dataset_name,
                dataset_version=request.dataset_version,
                description=request.description,
                rejection_reasons=result.rejection_reasons,
            )
            write_manifest_file(staging_dir, "manifest.json", manifest_model.model_dump_json(indent=2))

            self._package_store.commit(transformation_id=transformation_id, package_id=package_id, staging_dir=staging_dir)
        except Exception:
            self._package_store.discard(staging_dir)
            logger.error(
                "PACKAGING_FAILED package_id=%s transformation_id=%s qc_id=%s profile_name=%s profile_version=%s",
                package_id,
                transformation_id,
                request.qc_id,
                request.profile_name,
                request.profile_version,
            )
            raise

        log_event = "PACKAGING_COMPLETED" if result.status == PackageStatus.COMPLETED else "PACKAGING_REJECTED"
        log = logger.info if result.status == PackageStatus.COMPLETED else logger.warning
        log(
            "%s package_id=%s transformation_id=%s qc_id=%s profile_name=%s profile_version=%s "
            "strategy=%s grouping_mode=%s source_samples=%d group_count=%d "
            "train=%d validation=%d test=%d status=%s",
            log_event,
            package_id,
            transformation_id,
            request.qc_id,
            request.profile_name,
            request.profile_version,
            split.strategy,
            grouping_mode,
            result.summary.source_samples,
            result.summary.group_count,
            result.split_stats["train"].samples,
            result.split_stats["validation"].samples,
            result.split_stats["test"].samples,
            result.status.value,
        )

        return PackagingResponse(
            package_id=package_id,
            transformation_id=transformation_id,
            qc_id=request.qc_id,
            status=result.status,
            profile=profile_ref,
            summary=result.summary,
            report_uri=report_uri,
            rejection_reasons=result.rejection_reasons,
        )

    # ------------------------------------------------------------------
    # Lineage / QC gate helpers
    # ------------------------------------------------------------------

    def _locate_transformation(self, transformation_id: str) -> dict:
        manifest = self._transformed_store.find_manifest_by_transformation_id(transformation_id)
        if manifest is None:
            raise TransformationNotFoundError(
                f"No transformation run found with transformation_id='{transformation_id}'"
            )
        if manifest.get("transformation_id") != transformation_id:
            raise TransformationNotFoundError(
                f"Transformation manifest for '{transformation_id}' is inconsistent with its own ID"
            )
        return manifest

    def _locate_transformed_artifact(self, transformation_manifest: dict, cleaning_id: str, transformation_id: str) -> Path:
        extension = Path(_ARTIFACT_FILENAME).suffix
        if extension not in _SUPPORTED_EXTENSIONS:
            raise UnsupportedPackagingFileTypeError(
                f"Packaging is not supported for transformed artifact type '{extension}'"
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
        return artifact_path

    def _locate_qc(self, transformation_id: str, qc_id: str) -> dict:
        # Looked up by bare qc_id (not the compound transformation_id/qc_id
        # key) so a qc_id that genuinely belongs to a DIFFERENT
        # transformation is still found — and can then be reported as the
        # more specific QC_TRANSFORMATION_MISMATCH (409) rather than a
        # generic "not found" (404).
        manifest = self._qc_store.find_manifest_by_qc_id(qc_id)
        if manifest is None:
            raise QCNotFoundError(f"No QC run found with qc_id='{qc_id}'")
        return manifest

    def _verify_qc_report_checksum(self, transformation_id: str, qc_id: str, qc_manifest: dict) -> None:
        report_path = Path(self._qc_store.report_path(transformation_id=transformation_id, qc_id=qc_id))
        if not report_path.exists():
            raise QCNotFoundError(f"QC report file is missing on disk for qc_id='{qc_id}'")
        computed = sha256_of_path(report_path)
        if computed != qc_manifest["report_sha256"]:
            raise QCReportChecksumMismatchError(
                f"QC report for qc_id='{qc_id}' has been modified since it was generated: "
                f"expected sha256={qc_manifest['report_sha256']}, computed={computed}"
            )

    # ------------------------------------------------------------------
    # Core two-pass packaging
    # ------------------------------------------------------------------

    def _run_packaging(
        self, *, artifact_path, staging_dir, request, profile, transformed_sha256, session_ids, transformation_id, package_id
    ):
        split_cfg = request.config.split
        grouping_mode = request.config.grouping.mode

        records = self._read_sample_records(artifact_path)
        source_samples = len(records)

        requested_ratios = {
            "train": split_cfg.train_ratio,
            "validation": split_cfg.validation_ratio,
            "test": split_cfg.test_ratio,
        }

        rejection_reasons: list[str] = []

        if source_samples == 0:
            rejection_reasons.append(PackagingRejectionReason.EMPTY_SOURCE_DATASET.value)
            return self._finalize_empty_result(
                staging_dir=staging_dir,
                requested_ratios=requested_ratios,
                rejection_reasons=rejection_reasons,
                exports=request.config.exports,
                transformation_id=transformation_id,
                package_id=package_id,
            )

        if grouping_mode == "source_overlap":
            grouped = assign_source_overlap_groups(records, transformed_sha256=transformed_sha256)
        elif grouping_mode == "session":
            grouped = assign_session_groups(records, transformed_sha256=transformed_sha256, session_ids=session_ids)
        else:  # pragma: no cover — profile.validate_config already rejects this
            raise MissingGroupMetadataError(f"Unsupported grouping mode '{grouping_mode}'")

        group_ids_in_order: list[str] = []
        seen_groups: set[str] = set()
        group_sample_counts: Counter[str] = Counter()
        for record, group_id in grouped:
            group_sample_counts[group_id] += 1
            if group_id not in seen_groups:
                seen_groups.add(group_id)
                group_ids_in_order.append(group_id)

        group_to_split = assign_splits(
            split_cfg.strategy,
            group_ids_in_order=group_ids_in_order,
            group_sample_counts=dict(group_sample_counts),
            train_ratio=split_cfg.train_ratio,
            validation_ratio=split_cfg.validation_ratio,
            test_ratio=split_cfg.test_ratio,
            seed=split_cfg.seed,
            profile_name=profile.profile_name,
            profile_version=profile.profile_version,
        )

        index_to_assignment: dict[int, tuple[str, str, str]] = {}
        split_sample_counts: Counter[str] = Counter()
        for record, group_id in grouped:
            split_name = group_to_split[group_id]
            index_to_assignment[record.index] = (record.sample_id, group_id, split_name)
            split_sample_counts[split_name] += 1

        groups_per_split: dict[str, set[str]] = defaultdict(set)
        for group_id, split_name in group_to_split.items():
            groups_per_split[split_name].add(group_id)
        split_group_counts = {name: len(groups_per_split.get(name, ())) for name in SPLIT_NAMES}

        nonzero_splits = [name for name in SPLIT_NAMES if requested_ratios[name] > 0]
        group_count = len(group_ids_in_order)
        if group_count < len(nonzero_splits):
            rejection_reasons.append(PackagingRejectionReason.INSUFFICIENT_GROUPS_FOR_SPLIT.value)
        if any(split_sample_counts.get(name, 0) == 0 for name in nonzero_splits):
            rejection_reasons.append(PackagingRejectionReason.EMPTY_REQUESTED_SPLIT.value)

        overlap_ranges_and_splits = []
        if grouping_mode == "source_overlap":
            overlap_ranges_and_splits = [
                (record.source_row_start, record.source_row_end, group_to_split[group_id])
                for record, group_id in grouped
            ]

        leakage_result = run_leakage_checks(
            assignments=[index_to_assignment[i] for i in sorted(index_to_assignment)],
            source_sample_count=source_samples,
            overlap_ranges_and_splits=overlap_ranges_and_splits,
        )

        status = PackageStatus.REJECTED if rejection_reasons else PackageStatus.COMPLETED

        split_manifest_entries, split_index_sha256, split_index_size = self._write_splits(
            artifact_path=artifact_path,
            staging_dir=staging_dir,
            index_to_assignment=index_to_assignment,
            transformation_id=transformation_id,
            package_id=package_id,
        )

        export_manifest_entries = self._run_optional_exports(
            staging_dir=staging_dir,
            exports=request.config.exports,
            split_manifest_entries=split_manifest_entries,
            transformation_id=transformation_id,
            package_id=package_id,
        )

        split_stats = compute_split_stats(
            split_sample_counts=dict(split_sample_counts),
            split_group_counts=split_group_counts,
            total_samples=source_samples,
        )

        summary = PackagingSummary(
            source_samples=source_samples, packaged_samples=sum(split_sample_counts.values()), group_count=group_count
        )

        return _PackagingResult(
            status=status,
            summary=summary,
            requested_ratios=requested_ratios,
            split_stats=split_stats,
            leakage_checks=LeakageChecks(
                duplicate_sample_ids=leakage_result.duplicate_sample_ids,
                cross_split_groups=leakage_result.cross_split_groups,
                cross_split_overlaps=leakage_result.cross_split_overlaps,
                passed=leakage_result.passed,
            ),
            rejection_reasons=rejection_reasons,
            split_manifest_entries=split_manifest_entries,
            split_index_sha256=split_index_sha256,
            split_index_size_bytes=split_index_size,
            export_manifest_entries=export_manifest_entries,
        )

    def _read_sample_records(self, artifact_path: Path) -> list[SampleRecord]:
        records: list[SampleRecord] = []
        seen_sample_ids: set[str] = set()
        with artifact_path.open("r", encoding="utf-8") as source:
            index = 0
            for line in source:
                stripped = line.strip()
                if not stripped:
                    continue
                sample = json.loads(stripped)
                sample_id = sample.get("sample_id")
                if not sample_id:
                    raise MissingSampleIdError(f"Sample at index {index} is missing 'sample_id'")
                if sample_id in seen_sample_ids:
                    raise DuplicateSampleIdError(f"Duplicate sample_id '{sample_id}' at index {index}")
                seen_sample_ids.add(sample_id)

                metadata = sample.get("metadata") or {}
                records.append(
                    SampleRecord(
                        index=index,
                        sample_id=sample_id,
                        source_row_start=metadata.get("source_row_start"),
                        source_row_end=metadata.get("source_row_end"),
                    )
                )
                index += 1
        return records

    def _write_splits(
        self, *, artifact_path: Path, staging_dir: Path, index_to_assignment: dict, transformation_id: str, package_id: str
    ) -> tuple[dict, str, int]:
        split_paths = {name: staging_dir / f"{name}.jsonl" for name in SPLIT_NAMES}
        split_index_path = staging_dir / "split_index.jsonl"

        digests = {name: ChunkedSha256() for name in SPLIT_NAMES}
        sizes = {name: 0 for name in SPLIT_NAMES}
        split_index_digest = ChunkedSha256()
        split_index_size = 0

        handles = {name: path.open("wb") for name, path in split_paths.items()}
        try:
            with artifact_path.open("r", encoding="utf-8") as source, split_index_path.open("wb") as index_handle:
                index = 0
                for line in source:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    sample = json.loads(stripped)
                    sample_id, group_id, split_name = index_to_assignment[index]

                    line_bytes = self._jsonl_exporter.serialize_sample(sample)
                    handles[split_name].write(line_bytes)
                    digests[split_name].update(line_bytes)
                    sizes[split_name] += len(line_bytes)

                    metadata = sample.get("metadata") or {}
                    index_record = {
                        "sample_id": sample_id,
                        "group_id": group_id,
                        "split": split_name,
                        "source_row_start": metadata.get("source_row_start"),
                        "source_row_end": metadata.get("source_row_end"),
                    }
                    index_line = (canonical_json(index_record) + "\n").encode("utf-8")
                    index_handle.write(index_line)
                    split_index_digest.update(index_line)
                    split_index_size += len(index_line)

                    index += 1
        finally:
            for handle in handles.values():
                handle.close()

        split_manifest_entries = {}
        for name in SPLIT_NAMES:
            final_uri = self._final_artifact_uri(transformation_id, package_id, f"{name}.jsonl")
            split_manifest_entries[name] = SplitManifestEntry(
                samples=sum(1 for i, a in index_to_assignment.items() if a[2] == name),
                sha256=digests[name].hexdigest(),
                size_bytes=sizes[name],
                artifact_uri=final_uri,
            )

        return split_manifest_entries, split_index_digest.hexdigest(), split_index_size

    def _final_artifact_uri(self, transformation_id: str, package_id: str, filename: str) -> str:
        # The staging directory is renamed wholesale on atomic commit, so a
        # URI must be computed from the STORE's final-path logic, never
        # from the current (pre-commit) staging Path — otherwise every
        # persisted artifact_uri would point at a directory that no longer
        # exists the moment commit() succeeds.
        return f"file://{self._package_store.artifact_path(transformation_id=transformation_id, package_id=package_id, filename=filename)}"

    def _run_optional_exports(
        self, *, staging_dir: Path, exports: list[str], split_manifest_entries: dict, transformation_id: str, package_id: str
    ) -> dict:
        result: dict = {}
        if "parquet" not in exports:
            return result

        from app.packaging.exporters.parquet import ParquetExporter

        exporter = ParquetExporter()
        optional_dir = staging_dir / "optional"
        optional_dir.mkdir(parents=True, exist_ok=True)

        parquet_entries = {}
        for name in SPLIT_NAMES:
            jsonl_path = staging_dir / f"{name}.jsonl"
            output_path = optional_dir / f"{name}.parquet"
            sha256, size_bytes = exporter.export(jsonl_path=jsonl_path, output_path=output_path)
            parquet_entries[name] = SplitManifestEntry(
                samples=split_manifest_entries[name].samples,
                sha256=sha256,
                size_bytes=size_bytes,
                artifact_uri=self._final_artifact_uri(transformation_id, package_id, f"optional/{name}.parquet"),
            )
        result["parquet"] = parquet_entries
        return result

    def _finalize_empty_result(self, *, staging_dir, requested_ratios, rejection_reasons, exports, transformation_id, package_id):
        # Recommended reproducible behavior: commit empty-but-well-formed
        # deterministic split files (and split_index) alongside the
        # report/manifest, rather than omitting them, so a rejected
        # package is still a complete, auditable, well-shaped artifact set.
        split_manifest_entries = {}
        for name in SPLIT_NAMES:
            path = staging_dir / f"{name}.jsonl"
            path.write_bytes(b"")
            split_manifest_entries[name] = SplitManifestEntry(
                samples=0,
                sha256=compute_file_sha256(b""),
                size_bytes=0,
                artifact_uri=self._final_artifact_uri(transformation_id, package_id, f"{name}.jsonl"),
            )
        split_index_path = staging_dir / "split_index.jsonl"
        split_index_path.write_bytes(b"")

        export_manifest_entries = self._run_optional_exports(
            staging_dir=staging_dir,
            exports=exports,
            split_manifest_entries=split_manifest_entries,
            transformation_id=transformation_id,
            package_id=package_id,
        )

        split_stats = compute_split_stats(split_sample_counts={}, split_group_counts={}, total_samples=0)
        summary = PackagingSummary(source_samples=0, packaged_samples=0, group_count=0)
        leakage_checks = LeakageChecks(duplicate_sample_ids=0, cross_split_groups=0, cross_split_overlaps=0, passed=True)

        return _PackagingResult(
            status=PackageStatus.REJECTED,
            summary=summary,
            requested_ratios=requested_ratios,
            split_stats=split_stats,
            leakage_checks=leakage_checks,
            rejection_reasons=rejection_reasons,
            split_manifest_entries=split_manifest_entries,
            split_index_sha256=compute_file_sha256(b""),
            split_index_size_bytes=0,
            export_manifest_entries=export_manifest_entries,
        )


class _PackagingResult:
    def __init__(
        self,
        *,
        status,
        summary,
        requested_ratios,
        split_stats,
        leakage_checks,
        rejection_reasons,
        split_manifest_entries,
        split_index_sha256,
        split_index_size_bytes,
        export_manifest_entries,
    ) -> None:
        self.status = status
        self.summary = summary
        self.requested_ratios = requested_ratios
        self.split_stats = split_stats
        self.leakage_checks = leakage_checks
        self.rejection_reasons = rejection_reasons
        self.split_manifest_entries = split_manifest_entries
        self.split_index_sha256 = split_index_sha256
        self.split_index_size_bytes = split_index_size_bytes
        self.export_manifest_entries = export_manifest_entries
