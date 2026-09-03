"""Resolves a completed PipelineRun's final, user-facing results (v2.7,
Design Requirement 32).

Read-only by construction (Design Requirement 64): every method here only
ever reads already-published catalog rows and already-committed manifest/
report JSON files. Nothing is written, nothing is mutated, and no stage
logic is re-run or duplicated -- this purely joins:

    run  -->  run_artifacts (stage="package")  -->  artifacts.metadata_json
         -->  PackageManifest  -->  (qc_id)  -->  artifacts.metadata_json
         -->  QC manifest  -->  (report_uri)  -->  report.json on disk

`metadata_json` already holds the full manifest.json content for both
"package" and "qc" artifacts (see `app.catalog.scanner._record` -- it's
`canonical_json(manifest_data)`, the exact JSON committed to disk), so no
package/QC service object needs to be constructed just to read a summary.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.catalog.errors import CatalogScanFailedError
from app.catalog.repository import CatalogRepository
from app.catalog.service import CatalogService
from app.runs.models import PipelineRunResponse
from app.runs.results_models import (
    DatasetRegistrationSummary,
    FileSummary,
    PackageSummary,
    QCIssueSummary,
    QCResultSummary,
    RunResultsResponse,
    SplitSummary,
)


def _strip_file_uri(uri: str) -> Path:
    if uri.startswith("file://"):
        return Path(uri[len("file://") :])
    return Path(uri)


def _load_metadata(repo: CatalogRepository, artifact_type: str, artifact_id: str) -> dict | None:
    row = repo.get_artifact(artifact_type, artifact_id)
    if row is None or not row.get("metadata_json"):
        return None
    return json.loads(row["metadata_json"])


class RunResultsService:
    """Depends only on read paths already used elsewhere in the catalog
    layer -- `CatalogRepository.get_artifact` (v2.1) and
    `CatalogRepository.list_dataset_versions_for_packages` (v1). No new
    storage access pattern is introduced."""

    def __init__(self, *, catalog_repo: CatalogRepository, catalog_service: CatalogService) -> None:
        self._repo = catalog_repo
        self._catalog = catalog_service

    def get_results(self, run: PipelineRunResponse) -> RunResultsResponse:
        package_ref = next((a for a in run.artifacts if a.artifact_type == "package"), None)
        if package_ref is None:
            return RunResultsResponse(run_id=run.run_id, run_status=run.status)

        manifest = _load_metadata(self._repo, "package", package_ref.artifact_id)
        if manifest is None:
            # The catalog's `artifacts` index is populated by an explicit
            # scan/rebuild (v1-v2.5 design: the index is reconstructible
            # from on-disk manifests, never written to live by a stage
            # service) -- a run_artifacts row can legitimately name a
            # package the index hasn't picked up yet, immediately after
            # that run's packaging stage committed. Run the cheap,
            # non-destructive incremental scan once and retry before
            # giving up, so Results doesn't require the user to separately
            # know to call catalog scan (Design Requirement 30). A scan
            # failure (e.g. a relocated workspace tripping the registry's
            # anti-silent-overwrite guard -- see docs/MIGRATION_V1_TO_V2.md)
            # falls through to the same resultless response below rather
            # than raising -- Results is a best-effort, always-200 read
            # path, never a place that should surface a raw 500.
            try:
                self._catalog.scan()
            except CatalogScanFailedError:
                pass
            else:
                manifest = _load_metadata(self._repo, "package", package_ref.artifact_id)
        if manifest is None:
            # Still missing after a scan -- genuinely gone from the index
            # (e.g. manual data/ tampering). Report a resultless run rather
            # than crash; the catalog's own health check already surfaces
            # BROKEN_RUN_ARTIFACT_REFERENCE separately.
            return RunResultsResponse(run_id=run.run_id, run_status=run.status)

        splits = manifest.get("splits", {})
        sample_count = sum(int(entry.get("samples", 0)) for entry in splits.values())
        formats = ["jsonl"] + sorted((manifest.get("exports") or {}).keys())
        package_dir = _strip_file_uri(manifest["report_uri"]).parent if manifest.get("report_uri") else None
        if package_dir is None:
            manifest_row = self._repo.get_artifact("package", package_ref.artifact_id)
            package_dir = _strip_file_uri(manifest_row["manifest_uri"]).parent

        package = PackageSummary(
            package_id=manifest["package_id"],
            status=manifest["status"],
            formats=formats,
            sample_count=sample_count,
            local_path=str(package_dir),
            created_at=manifest.get("created_at", package_ref.created_at),
        )

        split_summary = SplitSummary(
            train=int(splits.get("train", {}).get("samples", 0)),
            validation=int(splits.get("validation", {}).get("samples", 0)),
            test=int(splits.get("test", {}).get("samples", 0)),
        )

        files = self._list_package_files(package_dir, manifest)
        qc = self._resolve_qc(manifest.get("qc_id"))
        lineage_fingerprint = manifest.get("source_transformed_sha256")
        registrations = self._resolve_dataset_registrations(package_ref.artifact_id)

        return RunResultsResponse(
            run_id=run.run_id,
            run_status=run.status,
            package=package,
            qc=qc,
            splits=split_summary,
            files=files,
            lineage_fingerprint=lineage_fingerprint,
            dataset_registrations=registrations,
        )

    def _list_package_files(self, package_dir: Path, manifest: dict) -> list[FileSummary]:
        files: list[FileSummary] = []
        for name, entry in (manifest.get("splits") or {}).items():
            path = _strip_file_uri(entry["artifact_uri"])
            files.append(FileSummary(name=path.name, relative_path=f"{name}.jsonl", size_bytes=entry.get("size_bytes", 0), role="split"))
        for export_format, split_entries in (manifest.get("exports") or {}).items():
            for name, entry in split_entries.items():
                path = _strip_file_uri(entry["artifact_uri"])
                try:
                    rel = str(path.relative_to(package_dir))
                except ValueError:
                    rel = path.name
                files.append(FileSummary(name=path.name, relative_path=rel, size_bytes=entry.get("size_bytes", 0), role="export"))
        for filename, role in (("manifest.json", "manifest"), ("report.json", "report"), ("split_index.jsonl", "split_index")):
            candidate = package_dir / filename
            if candidate.is_file():
                files.append(FileSummary(name=filename, relative_path=filename, size_bytes=candidate.stat().st_size, role=role))
        return files

    def _resolve_qc(self, qc_id: str | None) -> QCResultSummary | None:
        if not qc_id:
            return None
        qc_manifest = _load_metadata(self._repo, "qc", qc_id)
        if qc_manifest is None:
            return None

        report_path: Path | None = None
        report: dict = {}
        report_uri = qc_manifest.get("report_uri")
        if report_uri:
            report_path = _strip_file_uri(report_uri)
            if report_path.is_file():
                report = json.loads(report_path.read_text(encoding="utf-8"))

        summary = report.get("summary", qc_manifest.get("summary", {}))
        modality_coverage = {
            sensor_type: entry.get("coverage_ratio", 0.0) for sensor_type, entry in (report.get("modality_coverage") or {}).items()
        }
        issues = [
            QCIssueSummary(code=i["code"], severity=i["severity"], message=i["message"], path=i.get("path"))
            for i in (report.get("issues") or [])[:50]
        ]
        return QCResultSummary(
            qc_id=qc_id,
            status=qc_manifest.get("status", "unknown"),
            warning_count=summary.get("warning_count", 0),
            error_count=summary.get("issue_count", 0) - summary.get("warning_count", 0) if summary.get("issue_count") is not None else 0,
            modality_coverage=modality_coverage,
            issues=issues,
            issues_truncated=bool(report.get("issues_truncated", False)) or len(report.get("issues") or []) > 50,
            report_path=str(report_path) if report_path else None,
        )

    def _resolve_dataset_registrations(self, package_id: str) -> list[DatasetRegistrationSummary]:
        rows = self._repo.list_dataset_versions_for_packages([package_id])
        result = []
        for row in rows:
            version = self._catalog.get_version(row["dataset_name"], row["version"])
            result.append(
                DatasetRegistrationSummary(
                    dataset_name=row["dataset_name"], version=row["version"], effective_status=version.effective_status
                )
            )
        return result
