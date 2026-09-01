"""Discovers committed manifests/reports across every stage's storage root
and registers them (plus their direct lineage edges) into the catalog.

Path safety: the scanner ONLY ever walks the nine CONFIGURED storage roots
via its own `Path.glob()` calls — it never dereferences a manifest's own
`storage_uri`/`artifact_uri` field as a filesystem path to open. Those
fields are stored purely as metadata. This is what prevents a crafted or
corrupted manifest from making the scanner read (or, worse, later have
something else write) outside the configured roots.

Direct semantic edges only (never every possible transitive edge) —
see README "Scanner relationships" for the exact rationale:

    ingestion -> validation            (validated_from)
    validation -> integrity            (checked_from)
    integrity -> normalization         (normalized_from)
    normalization(s) -> synchronization (synchronized_from, one edge per stream)
    synchronization -> cleaning        (cleaned_from)
    cleaning -> transformation         (transformed_from)
    transformation -> qc               (qc_of)
    transformation -> package          (packaged_from)
    qc -> package                      (approved_by_qc)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from app.catalog import graph
from app.catalog.errors import LineageCycleDetectedError
from app.catalog.models import STAGE_RANK, ArtifactType
from app.catalog.repository import CatalogRepository
from app.catalog.serialization import canonical_json, compute_manifest_sha256
from app.core.config import Settings


class BrokenLineageError(Exception):
    pass


@dataclass
class ScanOutcome:
    inserted: int = 0
    unchanged: int = 0
    edges_inserted: int = 0
    issues: list[dict] = field(default_factory=list)


def _is_staging_path(path: Path) -> bool:
    """True for anything under a staging convention this codebase uses:
    the sibling-of-final `.tmp-<id>` directories (normalization,
    synchronization, cleaning, transformation, qc, package) and the
    dedicated `.staging/` subtree (ingestion, validation, integrity) — see
    app.storage.atomic. Both are already excluded by every glob() call
    below since Path.glob() skips dot-prefixed components by default; this
    check is defense-in-depth, not the only thing preventing a staging
    entry from being scanned.
    """
    return any(
        part.startswith(".tmp-") or part == ".gitkeep" or part == ".staging" for part in path.parts
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_uri(path: Path) -> str:
    return f"file://{path.resolve()}"


@dataclass(frozen=True)
class ParentRef:
    artifact_type: str
    artifact_id: str
    relationship: str


@dataclass(frozen=True)
class DiscoveredArtifact:
    artifact_type: str
    artifact_id: str
    record: dict
    parents: list[ParentRef]


def _record(
    *,
    artifact_type: str,
    artifact_id: str,
    status: str | None,
    storage_uri: str | None,
    content_sha256: str | None,
    manifest_path: Path,
    manifest_data: dict,
    created_at: str | None,
    session_id: str | None,
) -> dict:
    metadata_json = canonical_json(manifest_data)
    return {
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "pipeline_stage": STAGE_RANK[artifact_type],
        "status": status,
        "storage_uri": storage_uri,
        "content_sha256": content_sha256,
        "manifest_uri": _manifest_uri(manifest_path),
        "manifest_sha256": compute_manifest_sha256(manifest_path.read_text(encoding="utf-8")),
        "created_at": created_at,
        "session_id": session_id,
        "metadata_json": metadata_json,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Per-stage discovery — each yields DiscoveredArtifact in stable filesystem order
# ---------------------------------------------------------------------------


def _scan_ingestion(root: Path) -> Iterator[DiscoveredArtifact]:
    for manifest_path in sorted(root.glob("*/*/*/manifest.json")):
        if _is_staging_path(manifest_path):
            continue
        data = _load_json(manifest_path)
        record = _record(
            artifact_type=ArtifactType.INGESTION.value,
            artifact_id=data["ingestion_id"],
            status=None,
            storage_uri=data.get("storage_uri"),
            content_sha256=data.get("sha256"),
            manifest_path=manifest_path,
            manifest_data=data,
            created_at=data.get("ingested_at"),
            session_id=data.get("session_id"),
        )
        yield DiscoveredArtifact(ArtifactType.INGESTION.value, data["ingestion_id"], record, [])


def _scan_validation(root: Path) -> Iterator[DiscoveredArtifact]:
    for manifest_path in sorted(root.glob("*/*/report.json")):
        if _is_staging_path(manifest_path):
            continue
        data = _load_json(manifest_path)
        record = _record(
            artifact_type=ArtifactType.VALIDATION.value,
            artifact_id=data["validation_id"],
            status=data.get("status"),
            storage_uri=None,
            content_sha256=None,
            manifest_path=manifest_path,
            manifest_data=data,
            created_at=data.get("validated_at"),
            session_id=None,
        )
        parents = [ParentRef(ArtifactType.INGESTION.value, data["ingestion_id"], "validated_from")]
        yield DiscoveredArtifact(ArtifactType.VALIDATION.value, data["validation_id"], record, parents)


def _scan_integrity(root: Path) -> Iterator[DiscoveredArtifact]:
    for manifest_path in sorted(root.glob("*/*/report.json")):
        if _is_staging_path(manifest_path):
            continue
        data = _load_json(manifest_path)
        record = _record(
            artifact_type=ArtifactType.INTEGRITY.value,
            artifact_id=data["integrity_id"],
            status=data.get("status"),
            storage_uri=None,
            content_sha256=None,
            manifest_path=manifest_path,
            manifest_data=data,
            created_at=data.get("created_at"),
            session_id=None,
        )
        parents = [ParentRef(ArtifactType.VALIDATION.value, data["validation_id"], "checked_from")]
        yield DiscoveredArtifact(ArtifactType.INTEGRITY.value, data["integrity_id"], record, parents)


def _scan_normalization(root: Path) -> Iterator[DiscoveredArtifact]:
    for manifest_path in sorted(root.glob("*/*/manifest.json")):
        if _is_staging_path(manifest_path):
            continue
        data = _load_json(manifest_path)
        record = _record(
            artifact_type=ArtifactType.NORMALIZATION.value,
            artifact_id=data["normalization_id"],
            status=None,
            storage_uri=data.get("artifact_uri"),
            content_sha256=data.get("normalized_sha256"),
            manifest_path=manifest_path,
            manifest_data=data,
            created_at=data.get("created_at"),
            session_id=None,
        )
        parents = [ParentRef(ArtifactType.INTEGRITY.value, data["integrity_id"], "normalized_from")]
        yield DiscoveredArtifact(ArtifactType.NORMALIZATION.value, data["normalization_id"], record, parents)


def _scan_synchronization(root: Path) -> Iterator[DiscoveredArtifact]:
    for manifest_path in sorted(root.glob("*/manifest.json")):
        if _is_staging_path(manifest_path):
            continue
        data = _load_json(manifest_path)
        streams = data.get("streams", [])
        session_id = streams[0].get("session_id") if streams else None
        record = _record(
            artifact_type=ArtifactType.SYNCHRONIZATION.value,
            artifact_id=data["synchronization_id"],
            status=None,
            storage_uri=data.get("artifact_uri"),
            content_sha256=data.get("synchronized_sha256"),
            manifest_path=manifest_path,
            manifest_data=data,
            created_at=data.get("created_at"),
            session_id=session_id,
        )
        parents = [
            ParentRef(ArtifactType.NORMALIZATION.value, stream["normalization_id"], "synchronized_from")
            for stream in streams
        ]
        yield DiscoveredArtifact(ArtifactType.SYNCHRONIZATION.value, data["synchronization_id"], record, parents)


def _scan_cleaning(root: Path) -> Iterator[DiscoveredArtifact]:
    for manifest_path in sorted(root.glob("*/*/manifest.json")):
        if _is_staging_path(manifest_path):
            continue
        data = _load_json(manifest_path)
        streams = data.get("streams", [])
        session_id = streams[0].get("session_id") if streams else None
        record = _record(
            artifact_type=ArtifactType.CLEANING.value,
            artifact_id=data["cleaning_id"],
            status=data.get("status"),
            storage_uri=data.get("artifact_uri"),
            content_sha256=data.get("cleaned_sha256"),
            manifest_path=manifest_path,
            manifest_data=data,
            created_at=data.get("created_at"),
            session_id=session_id,
        )
        parents = [ParentRef(ArtifactType.SYNCHRONIZATION.value, data["synchronization_id"], "cleaned_from")]
        yield DiscoveredArtifact(ArtifactType.CLEANING.value, data["cleaning_id"], record, parents)


def _scan_transformation(root: Path) -> Iterator[DiscoveredArtifact]:
    for manifest_path in sorted(root.glob("*/*/manifest.json")):
        if _is_staging_path(manifest_path):
            continue
        data = _load_json(manifest_path)
        session_ids = data.get("upstream", {}).get("session_ids", [])
        record = _record(
            artifact_type=ArtifactType.TRANSFORMATION.value,
            # Step 7 has no "rejected" concept — every committed manifest
            # represents a successfully completed run, so there is no
            # status field to record here (never invented).
            artifact_id=data["transformation_id"],
            status=None,
            storage_uri=data.get("artifact_uri"),
            content_sha256=data.get("transformed_sha256"),
            manifest_path=manifest_path,
            manifest_data=data,
            created_at=data.get("created_at"),
            session_id=session_ids[0] if len(session_ids) == 1 else None,
        )
        parents = [ParentRef(ArtifactType.CLEANING.value, data["cleaning_id"], "transformed_from")]
        yield DiscoveredArtifact(ArtifactType.TRANSFORMATION.value, data["transformation_id"], record, parents)


def _scan_qc(root: Path) -> Iterator[DiscoveredArtifact]:
    for manifest_path in sorted(root.glob("*/*/manifest.json")):
        if _is_staging_path(manifest_path):
            continue
        data = _load_json(manifest_path)
        session_ids = data.get("upstream", {}).get("session_ids", [])
        record = _record(
            artifact_type=ArtifactType.QC.value,
            artifact_id=data["qc_id"],
            status=data.get("status"),
            storage_uri=data.get("report_uri"),
            content_sha256=None,
            manifest_path=manifest_path,
            manifest_data=data,
            created_at=data.get("created_at"),
            session_id=session_ids[0] if len(session_ids) == 1 else None,
        )
        parents = [ParentRef(ArtifactType.TRANSFORMATION.value, data["transformation_id"], "qc_of")]
        yield DiscoveredArtifact(ArtifactType.QC.value, data["qc_id"], record, parents)


def _scan_package(root: Path) -> Iterator[DiscoveredArtifact]:
    for manifest_path in sorted(root.glob("*/*/manifest.json")):
        if _is_staging_path(manifest_path):
            continue
        data = _load_json(manifest_path)
        session_ids = data.get("upstream", {}).get("session_ids", [])
        record = _record(
            artifact_type=ArtifactType.PACKAGE.value,
            artifact_id=data["package_id"],
            status=data.get("status"),
            storage_uri=None,
            content_sha256=None,
            manifest_path=manifest_path,
            manifest_data=data,
            created_at=data.get("created_at"),
            session_id=session_ids[0] if len(session_ids) == 1 else None,
        )
        parents = [
            ParentRef(ArtifactType.TRANSFORMATION.value, data["transformation_id"], "packaged_from"),
            ParentRef(ArtifactType.QC.value, data["qc_id"], "approved_by_qc"),
        ]
        yield DiscoveredArtifact(ArtifactType.PACKAGE.value, data["package_id"], record, parents)


_SCANNERS: list[tuple[str, Callable[[Path], Iterator[DiscoveredArtifact]]]] = [
    (ArtifactType.INGESTION.value, _scan_ingestion),
    (ArtifactType.VALIDATION.value, _scan_validation),
    (ArtifactType.INTEGRITY.value, _scan_integrity),
    (ArtifactType.NORMALIZATION.value, _scan_normalization),
    (ArtifactType.SYNCHRONIZATION.value, _scan_synchronization),
    (ArtifactType.CLEANING.value, _scan_cleaning),
    (ArtifactType.TRANSFORMATION.value, _scan_transformation),
    (ArtifactType.QC.value, _scan_qc),
    (ArtifactType.PACKAGE.value, _scan_package),
]


class CatalogScanner:
    def __init__(self, settings: Settings) -> None:
        self._roots: dict[str, Path] = {
            ArtifactType.INGESTION.value: Path(settings.RAW_STORAGE_ROOT),
            ArtifactType.VALIDATION.value: Path(settings.VALIDATION_STORAGE_ROOT),
            ArtifactType.INTEGRITY.value: Path(settings.INTEGRITY_STORAGE_ROOT),
            ArtifactType.NORMALIZATION.value: Path(settings.NORMALIZED_STORAGE_ROOT),
            ArtifactType.SYNCHRONIZATION.value: Path(settings.SYNCHRONIZED_STORAGE_ROOT),
            ArtifactType.CLEANING.value: Path(settings.CLEANED_STORAGE_ROOT),
            ArtifactType.TRANSFORMATION.value: Path(settings.TRANSFORMED_STORAGE_ROOT),
            ArtifactType.QC.value: Path(settings.QC_STORAGE_ROOT),
            ArtifactType.PACKAGE.value: Path(settings.PACKAGE_STORAGE_ROOT),
        }

    def scan(self, repo: CatalogRepository, *, strict: bool) -> ScanOutcome:
        """Registers every discovered artifact and its direct lineage
        edges. Must be called inside an already-open `repo.transaction()`
        — this function itself does not open one, so the caller controls
        whether a whole scan/rebuild commits or rolls back as one unit.
        """
        outcome = ScanOutcome()

        for artifact_type, scan_fn in _SCANNERS:
            root = self._roots[artifact_type]
            if not root.exists():
                continue
            for discovered in scan_fn(root):
                result = repo.upsert_artifact(discovered.record)
                if result == "inserted":
                    outcome.inserted += 1
                else:
                    outcome.unchanged += 1

                for parent in discovered.parents:
                    if repo.get_artifact(parent.artifact_type, parent.artifact_id) is None:
                        detail = (
                            f"{discovered.artifact_type}/{discovered.artifact_id} references "
                            f"{parent.artifact_type}/{parent.artifact_id} ({parent.relationship}), "
                            f"which is not registered"
                        )
                        if strict:
                            raise BrokenLineageError(detail)
                        repo.record_issue(
                            artifact_type=discovered.artifact_type,
                            artifact_id=discovered.artifact_id,
                            issue_code="MISSING_LINEAGE_PARENT",
                            detail=detail,
                            detected_at=datetime.now(timezone.utc).isoformat(),
                        )
                        outcome.issues.append(
                            {
                                "artifact_type": discovered.artifact_type,
                                "artifact_id": discovered.artifact_id,
                                "issue_code": "MISSING_LINEAGE_PARENT",
                                "detail": detail,
                            }
                        )
                        continue

                    if graph.would_create_cycle(
                        repo,
                        parent_type=parent.artifact_type,
                        parent_id=parent.artifact_id,
                        child_type=discovered.artifact_type,
                        child_id=discovered.artifact_id,
                    ):
                        raise LineageCycleDetectedError(
                            f"Adding edge {parent.artifact_type}/{parent.artifact_id} -> "
                            f"{discovered.artifact_type}/{discovered.artifact_id} would create a cycle"
                        )

                    if repo.insert_edge(
                        parent_type=parent.artifact_type,
                        parent_id=parent.artifact_id,
                        child_type=discovered.artifact_type,
                        child_id=discovered.artifact_id,
                        relationship=parent.relationship,
                    ):
                        outcome.edges_inserted += 1

        return outcome
