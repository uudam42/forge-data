"""Local run capacity enforcement (v2.6, Design Requirement 39).

Tested at the service level -- bypassing the executor entirely -- since
`create_run` alone occupies a capacity slot (a queued run is presumed to
start executing immediately; see RunRepository.count_active_runs) and
we want to observe that in isolation, deterministically, without racing
FastAPI TestClient's synchronous background-task execution.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.runs.errors import RunCapacityExceededError
from app.runs.repository import RunRepository
from app.runs.service import RunService
from app.storage.catalog_store import get_connection
from tests.test_v26_run_state_machine import _minimal_request


def _service(tmp_path: Path, max_runs: int) -> RunService:
    settings = Settings(
        RAW_STORAGE_ROOT=tmp_path / "raw", VALIDATION_STORAGE_ROOT=tmp_path / "validation",
        INTEGRITY_STORAGE_ROOT=tmp_path / "integrity", NORMALIZED_STORAGE_ROOT=tmp_path / "normalized",
        SYNCHRONIZED_STORAGE_ROOT=tmp_path / "synchronized", CLEANED_STORAGE_ROOT=tmp_path / "cleaned",
        TRANSFORMED_STORAGE_ROOT=tmp_path / "transformed", QC_STORAGE_ROOT=tmp_path / "qc",
        PACKAGE_STORAGE_ROOT=tmp_path / "packages", CATALOG_DB_PATH=tmp_path / "catalog" / "catalog.db",
        MAX_LOCAL_PIPELINE_RUNS=max_runs,
    )
    conn = get_connection(settings.CATALOG_DB_PATH)
    return RunService(repo=RunRepository(conn), settings=settings)


def test_capacity_enforced_at_creation(tmp_path: Path) -> None:
    service = _service(tmp_path, max_runs=2)
    service.create_run(run_type="pipeline", request=_minimal_request())
    service.create_run(run_type="pipeline", request=_minimal_request())
    with pytest.raises(RunCapacityExceededError) as excinfo:
        service.create_run(run_type="pipeline", request=_minimal_request())
    assert excinfo.value.limit == 2
    assert excinfo.value.current == 2
    payload = excinfo.value.to_dict()
    assert payload["code"] == "LOCAL_RUN_CAPACITY_EXCEEDED"


def test_capacity_released_after_completion(tmp_path: Path) -> None:
    service = _service(tmp_path, max_runs=1)
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    with pytest.raises(RunCapacityExceededError):
        service.create_run(run_type="pipeline", request=_minimal_request())

    service.mark_running(run_id, executor_id="x")
    service.mark_completed(run_id)

    # The slot is free again.
    second = service.create_run(run_type="pipeline", request=_minimal_request())
    assert second != run_id


def test_capacity_released_after_failure(tmp_path: Path) -> None:
    service = _service(tmp_path, max_runs=1)
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="x")
    service.mark_failed(run_id, error_code="X", error_message="boom")

    second = service.create_run(run_type="pipeline", request=_minimal_request())
    assert second != run_id


def test_capacity_released_after_cancellation(tmp_path: Path) -> None:
    service = _service(tmp_path, max_runs=1)
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="x")
    service.request_cancel(run_id)
    with pytest.raises(RunCapacityExceededError):
        # cancel_requested still counts -- the slot isn't free until the
        # cancellation is actually observed and finalized.
        service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_cancelled(run_id)
    third = service.create_run(run_type="pipeline", request=_minimal_request())
    assert third != run_id
