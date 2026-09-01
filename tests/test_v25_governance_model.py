"""Unit-level tests for the v2.5 governance state machine: absent-means-
active, transitions, reason requirement, append-only history, and
catalog-rebuild preservation. Uses direct repo/service construction
(the tests/test_catalog_service.py pattern) rather than the full HTTP
pipeline, since none of this needs a real pipeline artifact."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.catalog.errors import (
    GovernanceReasonRequiredError,
    GovernanceTargetNotFoundError,
    InvalidGovernanceTransitionError,
)
from app.catalog.repository import CatalogRepository
from app.catalog.scanner import CatalogScanner
from app.catalog.service import CatalogService
from app.catalog.verifier import ArtifactVerifier
from app.core.config import Settings
from app.storage.catalog_store import get_connection


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        RAW_STORAGE_ROOT=tmp_path / "raw",
        VALIDATION_STORAGE_ROOT=tmp_path / "validation",
        INTEGRITY_STORAGE_ROOT=tmp_path / "integrity",
        NORMALIZED_STORAGE_ROOT=tmp_path / "normalized",
        SYNCHRONIZED_STORAGE_ROOT=tmp_path / "synchronized",
        CLEANED_STORAGE_ROOT=tmp_path / "cleaned",
        TRANSFORMED_STORAGE_ROOT=tmp_path / "transformed",
        QC_STORAGE_ROOT=tmp_path / "qc",
        PACKAGE_STORAGE_ROOT=tmp_path / "packages",
        CATALOG_DB_PATH=tmp_path / "catalog" / "catalog.db",
    )


def _service(settings: Settings) -> CatalogService:
    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = CatalogRepository(conn)
    return CatalogService(repo=repo, scanner=CatalogScanner(settings), verifier=ArtifactVerifier(settings), settings=settings)


def _seed_artifact(service: CatalogService, artifact_type: str, artifact_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with service._repo.transaction():  # noqa: SLF001 -- test-only direct seed, no HTTP pipeline needed
        service._repo.upsert_artifact(
            {
                "artifact_type": artifact_type, "artifact_id": artifact_id, "pipeline_stage": 4, "status": "completed",
                "storage_uri": f"data/{artifact_type}/{artifact_id}", "content_sha256": "a" * 64,
                "manifest_uri": f"data/{artifact_type}/{artifact_id}.manifest.json", "manifest_sha256": "b" * 64,
                "created_at": now, "session_id": None, "metadata_json": "{}", "registered_at": now,
            }
        )


def test_absent_governance_means_active(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    _seed_artifact(service, "normalization", "norm_1")
    gov = service.get_artifact_governance("normalization", "norm_1")
    assert gov.state == "active"
    assert gov.reason is None


def test_mark_deprecated(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    _seed_artifact(service, "normalization", "norm_1")
    result = service.set_artifact_governance("normalization", "norm_1", new_state="deprecated", reason="superseded by a better profile")
    assert result.state == "deprecated"
    assert result.reason == "superseded by a better profile"
    assert service.get_artifact_governance("normalization", "norm_1").state == "deprecated"


def test_mark_invalid(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    _seed_artifact(service, "normalization", "norm_1")
    result = service.set_artifact_governance("normalization", "norm_1", new_state="invalid", reason="wrong gyro conversion")
    assert result.state == "invalid"


def test_reactivate_after_invalid(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    _seed_artifact(service, "normalization", "norm_1")
    service.set_artifact_governance("normalization", "norm_1", new_state="invalid", reason="calibration bug suspected")
    result = service.set_artifact_governance("normalization", "norm_1", new_state="active", reason="investigation cleared artifact")
    assert result.state == "active"
    # Absence of a row means active -- no lingering current-state row.
    assert service._repo.get_artifact_governance("normalization", "norm_1") is None  # noqa: SLF001


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_reason_required_for_every_transition(tmp_path: Path, reason) -> None:
    service = _service(_settings(tmp_path))
    _seed_artifact(service, "normalization", "norm_1")
    with pytest.raises(GovernanceReasonRequiredError):
        service.set_artifact_governance("normalization", "norm_1", new_state="invalid", reason=reason)


def test_governance_target_must_exist_in_catalog(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    with pytest.raises(GovernanceTargetNotFoundError):
        service.set_artifact_governance("normalization", "norm_never_scanned", new_state="invalid", reason="x")


def test_transition_history_is_append_only_and_survives_reactivation(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    _seed_artifact(service, "normalization", "norm_1")
    service.set_artifact_governance("normalization", "norm_1", new_state="invalid", reason="calibration bug suspected")
    service.set_artifact_governance("normalization", "norm_1", new_state="active", reason="investigation cleared artifact")
    service.set_artifact_governance("normalization", "norm_1", new_state="deprecated", reason="superseded by norm_2")

    history = service.get_artifact_governance_history("normalization", "norm_1")
    assert [e.new_state for e in history.events] == ["invalid", "active", "deprecated"]
    assert [e.previous_state for e in history.events] == ["active", "invalid", "active"]
    # The invalidation event's reason is never erased by the later reactivation.
    assert history.events[0].reason == "calibration bug suspected"
    assert history.current.state == "deprecated"


def test_invalid_transition_rejected(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    _seed_artifact(service, "normalization", "norm_1")
    with pytest.raises(InvalidGovernanceTransitionError):
        service.set_artifact_governance("normalization", "norm_1", new_state="bogus_state", reason="x")


def test_same_state_transition_updates_reason_and_is_recorded(tmp_path: Path) -> None:
    """invalid -> invalid is allowed on purpose: it lets a caller update
    the reason without first reactivating, and still lands as a new
    event (Design Requirement 31's ALLOWED_TRANSITIONS)."""
    service = _service(_settings(tmp_path))
    _seed_artifact(service, "normalization", "norm_1")
    service.set_artifact_governance("normalization", "norm_1", new_state="invalid", reason="first reason")
    service.set_artifact_governance("normalization", "norm_1", new_state="invalid", reason="more detail after investigation")
    current = service.get_artifact_governance("normalization", "norm_1")
    assert current.reason == "more detail after investigation"
    history = service.get_artifact_governance_history("normalization", "norm_1")
    assert len(history.events) == 2


def test_catalog_rebuild_preserves_artifact_governance(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = _service(settings)
    _seed_artifact(service, "normalization", "norm_1")
    service.set_artifact_governance("normalization", "norm_1", new_state="invalid", reason="preserved across rebuild")

    # rebuild() clears and re-scans the artifact index from an EMPTY
    # filesystem (nothing under settings.*_STORAGE_ROOT) -- so norm_1
    # disappears from `artifacts`, but its governance row/history must
    # NOT be deleted (Design Requirement 25).
    service.rebuild()

    assert service._repo.get_artifact("normalization", "norm_1") is None  # noqa: SLF001
    gov_row = service._repo.get_artifact_governance("normalization", "norm_1")  # noqa: SLF001
    assert gov_row is not None
    assert gov_row["state"] == "invalid"
    events = service._repo.list_artifact_governance_events("normalization", "norm_1")  # noqa: SLF001
    assert len(events) == 1


def test_broken_governance_reference_surfaced_in_health(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = _service(settings)
    _seed_artifact(service, "normalization", "norm_1")
    service.set_artifact_governance("normalization", "norm_1", new_state="invalid", reason="orphaned by rebuild")
    service.rebuild()  # norm_1 vanishes from `artifacts`; governance row remains

    health = service.health()
    assert health.status == "degraded"
    assert any(i.code == "BROKEN_GOVERNANCE_REFERENCE" for i in health.issues)


def test_marking_active_artifact_invalid_is_not_itself_a_health_issue(tmp_path: Path) -> None:
    """An invalid artifact is an intentional, healthy governance state --
    never itself a catalog health problem (Design Requirement 27)."""
    settings = _settings(tmp_path)
    service = _service(settings)
    _seed_artifact(service, "normalization", "norm_1")
    service.set_artifact_governance("normalization", "norm_1", new_state="invalid", reason="known bad")

    health = service.health()
    assert health.status == "healthy"
    assert not any(i.code == "BROKEN_GOVERNANCE_REFERENCE" for i in health.issues)
