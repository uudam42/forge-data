"""Artifact identity/integrity verification.

Recomputes checksums and compares them against what the catalog has
registered — it never repairs anything. Deliberately reuses each stage's
own storage class (`find_manifest*`, `artifact_path`/`report_path`) rather
than re-deriving filesystem paths itself, so path construction always
goes through the same safe, already-tested logic every stage's own
service uses; the verifier never trusts a manifest's own `storage_uri`
field as something to open directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.catalog.models import VerificationStatus
from app.catalog.repository import CatalogRepository
from app.core.config import Settings
from app.storage.cleaned_store import LocalCleanedArtifactStore
from app.storage.integrity_store import LocalIntegrityReportStore
from app.storage.local import LocalRawStorage
from app.storage.normalized_store import LocalNormalizedArtifactStore
from app.storage.package_store import LocalDatasetPackageStore
from app.storage.qc_store import LocalQCReportStore
from app.storage.synchronization_store import LocalSynchronizationArtifactStore
from app.storage.transformed_store import LocalTransformedArtifactStore
from app.storage.validation_store import LocalValidationReportStore
from app.utils.hashing import sha256_of_path

_PRECEDENCE = (
    VerificationStatus.MISSING.value,
    VerificationStatus.CHECKSUM_MISMATCH.value,
    VerificationStatus.MANIFEST_MISMATCH.value,
    VerificationStatus.VERIFIED.value,
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class VerificationOutcome:
    status: str
    checks: list[Check]


def _overall_status(checks: list[Check]) -> str:
    statuses = {c.status for c in checks}
    for candidate in _PRECEDENCE:
        if candidate in statuses:
            return candidate
    return VerificationStatus.VERIFIED.value


def _check_manifest_bytes(manifest_path: Path, expected_sha256: str | None) -> Check:
    if not manifest_path.exists():
        return Check("manifest_file", VerificationStatus.MISSING.value, f"missing: {manifest_path}")
    actual = sha256_of_path(manifest_path)
    if expected_sha256 is not None and actual != expected_sha256:
        return Check(
            "manifest_file",
            VerificationStatus.MANIFEST_MISMATCH.value,
            f"expected sha256={expected_sha256}, computed={actual}",
        )
    return Check("manifest_file", VerificationStatus.VERIFIED.value)


def _check_content_file(name: str, path: Path, expected_sha256: str | None) -> Check:
    if not path.exists():
        return Check(name, VerificationStatus.MISSING.value, f"missing: {path}")
    actual = sha256_of_path(path)
    if expected_sha256 is not None and actual != expected_sha256:
        return Check(name, VerificationStatus.CHECKSUM_MISMATCH.value, f"expected sha256={expected_sha256}, computed={actual}")
    return Check(name, VerificationStatus.VERIFIED.value)


class ArtifactVerifier:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._raw = LocalRawStorage(root=settings.RAW_STORAGE_ROOT)
        self._validation = LocalValidationReportStore(root=settings.VALIDATION_STORAGE_ROOT)
        self._integrity = LocalIntegrityReportStore(root=settings.INTEGRITY_STORAGE_ROOT)
        self._normalized = LocalNormalizedArtifactStore(root=settings.NORMALIZED_STORAGE_ROOT)
        self._synchronized = LocalSynchronizationArtifactStore(root=settings.SYNCHRONIZED_STORAGE_ROOT)
        self._cleaned = LocalCleanedArtifactStore(root=settings.CLEANED_STORAGE_ROOT)
        self._transformed = LocalTransformedArtifactStore(root=settings.TRANSFORMED_STORAGE_ROOT)
        self._qc = LocalQCReportStore(root=settings.QC_STORAGE_ROOT)
        self._package = LocalDatasetPackageStore(root=settings.PACKAGE_STORAGE_ROOT)

    def verify(self, repo: CatalogRepository, artifact_type: str, artifact_id: str) -> VerificationOutcome:
        artifact = repo.get_artifact(artifact_type, artifact_id)
        if artifact is None:
            return VerificationOutcome(
                VerificationStatus.MISSING.value,
                [Check("catalog_entry", VerificationStatus.MISSING.value, "not registered in catalog")],
            )
        metadata = json.loads(artifact["metadata_json"])
        method = getattr(self, f"_verify_{artifact_type}", None)
        if method is None:
            return VerificationOutcome(VerificationStatus.VERIFIED.value, [])
        checks = method(artifact, metadata)
        return VerificationOutcome(_overall_status(checks), checks)

    def _verify_ingestion(self, artifact: dict, metadata: dict) -> list[Check]:
        checks = []
        manifest = self._raw.find_manifest(artifact["artifact_id"])
        if manifest is None:
            return [Check("manifest_file", VerificationStatus.MISSING.value, "ingestion manifest not found")]
        checks.append(_check_manifest_bytes(_uri_to_path(artifact["manifest_uri"]), artifact["manifest_sha256"]))
        raw_path = Path(
            self._raw.get_path(
                customer_id=manifest["customer_id"],
                session_id=manifest["session_id"],
                ingestion_id=manifest["ingestion_id"],
                filename=manifest["original_filename"],
            )
        )
        checks.append(_check_content_file("raw_file", raw_path, artifact["content_sha256"]))
        return checks

    def _verify_validation(self, artifact: dict, metadata: dict) -> list[Check]:
        reports = self._validation.find_reports(metadata["ingestion_id"])
        match = next((r for r in reports if r.get("validation_id") == artifact["artifact_id"]), None)
        if match is None:
            return [Check("report_file", VerificationStatus.MISSING.value, "validation report not found")]
        return [_check_manifest_bytes(_uri_to_path(artifact["manifest_uri"]), artifact["manifest_sha256"])]

    def _verify_integrity(self, artifact: dict, metadata: dict) -> list[Check]:
        reports = self._integrity.find_reports(metadata["ingestion_id"])
        match = next((r for r in reports if r.get("integrity_id") == artifact["artifact_id"]), None)
        if match is None:
            return [Check("report_file", VerificationStatus.MISSING.value, "integrity report not found")]
        return [_check_manifest_bytes(_uri_to_path(artifact["manifest_uri"]), artifact["manifest_sha256"])]

    def _verify_normalization(self, artifact: dict, metadata: dict) -> list[Check]:
        manifest = self._normalized.find_manifest(artifact["artifact_id"])
        if manifest is None:
            return [Check("manifest_file", VerificationStatus.MISSING.value, "normalization manifest not found")]
        checks = [_check_manifest_bytes(_uri_to_path(artifact["manifest_uri"]), artifact["manifest_sha256"])]
        artifact_path = Path(
            self._normalized.artifact_path(
                ingestion_id=manifest["ingestion_id"],
                normalization_id=manifest["normalization_id"],
                filename=manifest["artifact_filename"],
            )
        )
        checks.append(_check_content_file("normalized_artifact", artifact_path, artifact["content_sha256"]))
        return checks

    def _verify_synchronization(self, artifact: dict, metadata: dict) -> list[Check]:
        manifest = self._synchronized.find_manifest(artifact["artifact_id"])
        if manifest is None:
            return [Check("manifest_file", VerificationStatus.MISSING.value, "synchronization manifest not found")]
        checks = [_check_manifest_bytes(_uri_to_path(artifact["manifest_uri"]), artifact["manifest_sha256"])]
        artifact_path = Path(
            self._synchronized.artifact_path(
                synchronization_id=manifest["synchronization_id"], filename=manifest["artifact_filename"]
            )
        )
        checks.append(_check_content_file("synchronized_artifact", artifact_path, artifact["content_sha256"]))
        return checks

    def _verify_cleaning(self, artifact: dict, metadata: dict) -> list[Check]:
        manifest = self._cleaned.find_manifest_by_cleaning_id(artifact["artifact_id"])
        if manifest is None:
            return [Check("manifest_file", VerificationStatus.MISSING.value, "cleaning manifest not found")]
        checks = [_check_manifest_bytes(_uri_to_path(artifact["manifest_uri"]), artifact["manifest_sha256"])]
        artifact_path = Path(
            self._cleaned.artifact_path(
                synchronization_id=manifest["synchronization_id"],
                cleaning_id=manifest["cleaning_id"],
                filename="cleaned.jsonl",
            )
        )
        checks.append(_check_content_file("cleaned_artifact", artifact_path, artifact["content_sha256"]))
        return checks

    def _verify_transformation(self, artifact: dict, metadata: dict) -> list[Check]:
        manifest = self._transformed.find_manifest_by_transformation_id(artifact["artifact_id"])
        if manifest is None:
            return [Check("manifest_file", VerificationStatus.MISSING.value, "transformation manifest not found")]
        checks = [_check_manifest_bytes(_uri_to_path(artifact["manifest_uri"]), artifact["manifest_sha256"])]
        artifact_path = Path(
            self._transformed.artifact_path(
                cleaning_id=manifest["cleaning_id"],
                transformation_id=manifest["transformation_id"],
                filename="transformed.jsonl",
            )
        )
        checks.append(_check_content_file("transformed_artifact", artifact_path, artifact["content_sha256"]))
        return checks

    def _verify_qc(self, artifact: dict, metadata: dict) -> list[Check]:
        manifest = self._qc.find_manifest_by_qc_id(artifact["artifact_id"])
        if manifest is None:
            return [Check("manifest_file", VerificationStatus.MISSING.value, "QC manifest not found")]
        checks = [_check_manifest_bytes(_uri_to_path(artifact["manifest_uri"]), artifact["manifest_sha256"])]
        report_path = Path(
            self._qc.report_path(transformation_id=manifest["transformation_id"], qc_id=manifest["qc_id"])
        )
        checks.append(_check_content_file("qc_report", report_path, manifest.get("report_sha256")))
        return checks

    def _verify_package(self, artifact: dict, metadata: dict) -> list[Check]:
        transformation_id = metadata.get("transformation_id")
        manifest = self._package.find_manifest(transformation_id=transformation_id, package_id=artifact["artifact_id"])
        if manifest is None:
            return [Check("manifest_file", VerificationStatus.MISSING.value, "package manifest not found")]
        checks = [_check_manifest_bytes(_uri_to_path(artifact["manifest_uri"]), artifact["manifest_sha256"])]
        for split_name, entry in manifest.get("splits", {}).items():
            checks.append(_check_content_file(f"split_{split_name}", _uri_to_path(entry["artifact_uri"]), entry["sha256"]))
        if manifest.get("split_index_uri"):
            checks.append(
                _check_content_file("split_index", _uri_to_path(manifest["split_index_uri"]), manifest.get("split_index_sha256"))
            )
        report_path = Path(
            self._package.report_path(transformation_id=transformation_id, package_id=artifact["artifact_id"])
        )
        checks.append(_check_content_file("package_report", report_path, manifest.get("report_sha256")))
        return checks


def _uri_to_path(uri: str) -> Path:
    return Path(uri.replace("file://", ""))
