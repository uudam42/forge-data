"""User-facing "final results" models for a completed PipelineRun (v2.7,
Design Requirement 32/33).

These are read-only summaries assembled by `app.runs.results` from data
that already exists in the catalog and on disk (the package manifest, the
QC report, the artifact index) -- nothing here is a new source of truth,
nothing is written, and no per-sample data is ever included. Deliberately
bounded: a package's split files can be gigabytes, so this model carries
file *metadata* (name, size, role) and a local filesystem path, never file
contents (Design Requirement 74/75).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SplitSummary(BaseModel):
    train: int = 0
    validation: int = 0
    test: int = 0


class FileSummary(BaseModel):
    name: str
    relative_path: str
    size_bytes: int
    role: str  # "split" | "export" | "manifest" | "report" | "split_index"


class PackageSummary(BaseModel):
    package_id: str
    status: str
    formats: list[str] = Field(default_factory=list)
    sample_count: int
    local_path: str
    created_at: str


class QCIssueSummary(BaseModel):
    code: str
    severity: str
    message: str
    path: str | None = None


class QCResultSummary(BaseModel):
    qc_id: str
    status: str
    warning_count: int
    error_count: int
    modality_coverage: dict[str, float] = Field(default_factory=dict)
    issues: list[QCIssueSummary] = Field(default_factory=list)
    issues_truncated: bool = False
    report_path: str | None = None


class DatasetRegistrationSummary(BaseModel):
    dataset_name: str
    version: str
    effective_status: str


class RunResultsResponse(BaseModel):
    run_id: str
    run_status: str
    package: PackageSummary | None = None
    qc: QCResultSummary | None = None
    splits: SplitSummary | None = None
    files: list[FileSummary] = Field(default_factory=list)
    lineage_fingerprint: str | None = None
    dataset_registrations: list[DatasetRegistrationSummary] = Field(default_factory=list)
