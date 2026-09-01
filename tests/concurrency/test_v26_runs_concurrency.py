"""v2.6 — real multiprocess tests for pipeline-run creation, polling, and
cancellation (Design Requirement 32), plus catalog rebuild alongside real
run history (Design Requirement 57)."""

from __future__ import annotations

import pytest

from app.runs.repository import RunRepository
from app.storage.catalog_store import get_connection

from .helpers import CTX, cancel_run_worker, create_run_worker, mark_running_and_completed_worker, poll_run_worker, run_workers

pytestmark = pytest.mark.concurrency


def test_four_concurrent_run_creations_get_unique_ids(tmp_path):
    data_root = str(tmp_path)
    results = run_workers(create_run_worker, [(data_root,) for _ in range(4)])
    assert [r[0] for r in results] == ["ok"] * 4, results
    run_ids = [r[1] for r in results]
    assert len(set(run_ids)) == 4  # all unique, no collisions

    conn = get_connection(str(tmp_path / "catalog" / "catalog.db"))
    repo = RunRepository(conn)
    assert repo.count_all_runs() == 4


def test_polling_from_a_separate_process_while_another_creates_runs(tmp_path):
    data_root = str(tmp_path)
    create_results = run_workers(create_run_worker, [(data_root,) for _ in range(3)])
    run_ids = [r[1] for r in create_results]

    poll_results = run_workers(poll_run_worker, [(data_root, rid) for rid in run_ids])
    assert poll_results.count(("ok", "queued")) == 3, poll_results


def test_cancel_request_from_a_different_process_than_the_creator(tmp_path):
    data_root = str(tmp_path)
    [create_result] = run_workers(create_run_worker, [(data_root,)])
    run_id = create_result[1]

    [cancel_result] = run_workers(cancel_run_worker, [(data_root, run_id)])
    assert cancel_result == ("ok", "cancel_requested")

    conn = get_connection(str(tmp_path / "catalog" / "catalog.db"))
    repo = RunRepository(conn)
    assert repo.get_run(run_id)["status"] == "cancel_requested"


def test_catalog_rebuild_alongside_real_run_history_preserves_it(tmp_path):
    data_root = str(tmp_path)
    create_results = run_workers(create_run_worker, [(data_root,) for _ in range(3)])
    run_ids = [r[1] for r in create_results]

    # A real separate process drives each run through its full lifecycle
    # (running -> completed, with a stage run and a run_artifact each).
    lifecycle_results = run_workers(mark_running_and_completed_worker, [(data_root, rid) for rid in run_ids])
    assert [r[0] for r in lifecycle_results] == ["ok"] * 3, lifecycle_results

    conn = get_connection(str(tmp_path / "catalog" / "catalog.db"))
    from app.catalog.repository import CatalogRepository
    from app.catalog.scanner import CatalogScanner
    from app.catalog.service import CatalogService
    from app.catalog.verifier import ArtifactVerifier
    from .helpers import settings_for

    settings = settings_for(data_root)
    catalog_repo = CatalogRepository(conn)
    before = catalog_repo.count_run_tables()
    assert before[0] == 3  # 3 runs

    service = CatalogService(repo=catalog_repo, scanner=CatalogScanner(settings), verifier=ArtifactVerifier(settings), settings=settings)
    service.rebuild()

    assert catalog_repo.count_run_tables() == before
    run_repo = RunRepository(conn)
    for run_id in run_ids:
        run = run_repo.get_run(run_id)
        assert run["status"] == "completed"
        assert len(run_repo.list_run_artifacts(run_id)) == 1
