"""Startup crash reconciliation (v2.6, Design Requirement 34) -- unit
level, using an injected stale timestamp rather than a real sleep
(Design Requirement 59)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.runs.recovery import RunRecoveryService
from app.runs.repository import RunRepository
from app.runs.service import RunService
from app.storage.catalog_store import get_connection
from tests.test_v26_run_state_machine import _minimal_request, _settings


def _repo(tmp_path: Path) -> RunRepository:
    conn = get_connection(_settings(tmp_path).CATALOG_DB_PATH)
    return RunRepository(conn)


def test_stale_running_run_is_marked_process_lost(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    service = RunService(repo=repo, settings=_settings(tmp_path))
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="dead:123:abcd")

    # Simulate time passing with no heartbeat: force last_heartbeat_at
    # far in the past directly (no real sleep).
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    with repo.transaction():
        repo.touch_heartbeat(run_id, last_heartbeat_at=stale_ts)

    reconciled = RunRecoveryService(repo=repo, stale_after_seconds=30).reconcile()
    assert reconciled == 1

    run = service.get_run(run_id)
    assert run.status == "failed"
    assert run.error_code == "RUN_PROCESS_LOST"


def test_fresh_heartbeat_is_left_alone(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    service = RunService(repo=repo, settings=_settings(tmp_path))
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="alive:1:abcd")  # heartbeat is "now"

    reconciled = RunRecoveryService(repo=repo, stale_after_seconds=30).reconcile()
    assert reconciled == 0
    assert service.get_run(run_id).status == "running"


def test_reconcile_is_safe_to_call_repeatedly(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    service = RunService(repo=repo, settings=_settings(tmp_path))
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="dead:1:x")
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    with repo.transaction():
        repo.touch_heartbeat(run_id, last_heartbeat_at=stale_ts)

    recovery = RunRecoveryService(repo=repo, stale_after_seconds=30)
    assert recovery.reconcile() == 1
    assert recovery.reconcile() == 0  # already failed -- not revisited


def test_cancel_requested_run_can_also_be_reconciled(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    service = RunService(repo=repo, settings=_settings(tmp_path))
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="dead:1:x")
    service.request_cancel(run_id)
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    with repo.transaction():
        repo.touch_heartbeat(run_id, last_heartbeat_at=stale_ts)

    reconciled = RunRecoveryService(repo=repo, stale_after_seconds=30).reconcile()
    assert reconciled == 1
    assert service.get_run(run_id).status == "failed"
    assert service.get_run(run_id).error_code == "RUN_PROCESS_LOST"


def test_finished_runs_are_never_touched(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    service = RunService(repo=repo, settings=_settings(tmp_path))
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="x")
    service.mark_completed(run_id)

    reconciled = RunRecoveryService(repo=repo, stale_after_seconds=0).reconcile()
    assert reconciled == 0
    assert service.get_run(run_id).status == "completed"
