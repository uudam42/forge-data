"""Catalog rebuild must never touch run metadata (v2.6, Design
Requirement 57) -- mirrors the v2.5 governance-preservation test
pattern exactly."""

from __future__ import annotations

from pathlib import Path

from app.catalog.repository import CatalogRepository
from app.catalog.scanner import CatalogScanner
from app.catalog.service import CatalogService
from app.catalog.verifier import ArtifactVerifier
from app.runs.repository import RunRepository
from app.runs.service import RunService
from app.storage.catalog_store import get_connection
from tests.test_v26_run_state_machine import _minimal_request, _settings


def test_catalog_rebuild_preserves_all_run_tables(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = get_connection(settings.CATALOG_DB_PATH)
    run_repo = RunRepository(conn)
    catalog_repo = CatalogRepository(conn)
    run_service = RunService(repo=run_repo, settings=settings)

    run_id = run_service.create_run(run_type="pipeline", request=_minimal_request())
    run_service.mark_running(run_id, executor_id="x")
    with run_repo.transaction():
        run_repo.create_stage_run(stage_run_id="stagerun_1", run_id=run_id, stage="ingestion:imu", status="pending")
        run_repo.update_stage_run("stagerun_1", status="completed", records_processed=10)
        run_repo.record_run_artifact(run_id=run_id, stage="ingestion:imu", artifact_type="ingestion", artifact_id="ing_test_1", created_at="2026-01-01T00:00:00Z")
    run_service.mark_completed(run_id)

    before = catalog_repo.count_run_tables()
    assert before == (1, 1, 1, 3)  # 1 run, 1 stage_run, 1 run_artifact, 3 events (created/started/completed)

    catalog_service = CatalogService(repo=catalog_repo, scanner=CatalogScanner(settings), verifier=ArtifactVerifier(settings), settings=settings)
    catalog_service.rebuild()  # empty filesystem -- clears/rebuilds the (empty) artifact index

    after = catalog_repo.count_run_tables()
    assert after == before

    # And the actual content is untouched, not just the counts.
    run = run_service.get_run(run_id)
    assert run.status == "completed"
    assert len(run.stage_runs) == 1
    assert run.stage_runs[0].records_processed == 10
    assert len(run.artifacts) == 1
    assert run.artifacts[0].artifact_id == "ing_test_1"


def test_broken_run_artifact_reference_surfaced_in_health_not_deleted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = get_connection(settings.CATALOG_DB_PATH)
    run_repo = RunRepository(conn)
    catalog_repo = CatalogRepository(conn)
    run_service = RunService(repo=run_repo, settings=settings)

    run_id = run_service.create_run(run_type="pipeline", request=_minimal_request())
    run_service.mark_running(run_id, executor_id="x")
    with run_repo.transaction():
        run_repo.create_stage_run(stage_run_id="stagerun_1", run_id=run_id, stage="ingestion:imu", status="pending")
        # Points at an artifact that was never actually registered in `artifacts` -- simulates
        # one that later vanished from the index after a rebuild off an empty filesystem.
        run_repo.record_run_artifact(run_id=run_id, stage="ingestion:imu", artifact_type="ingestion", artifact_id="ing_never_indexed", created_at="2026-01-01T00:00:00Z")
    run_service.mark_completed(run_id)

    catalog_service = CatalogService(repo=catalog_repo, scanner=CatalogScanner(settings), verifier=ArtifactVerifier(settings), settings=settings)
    health = catalog_service.health()
    assert health.status == "degraded"
    assert any(i.code == "BROKEN_RUN_ARTIFACT_REFERENCE" for i in health.issues)

    # The run-artifact row itself is still there -- never deleted.
    assert len(run_service.get_run(run_id).artifacts) == 1
