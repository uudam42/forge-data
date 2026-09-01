"""Unit-level tests for the v2.6 run state machine, config hashing, and
RunService's basic CRUD -- direct repository/service construction (the
established tests/test_catalog_service.py pattern), no HTTP needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.runs.errors import InvalidRunTransitionError, RunNotFoundError
from app.runs.models import PipelineCleaningConfig, PipelineQCConfig, PipelineRunRequest, PipelineStreamConfig, PipelineTransformationConfig
from app.runs.repository import RunRepository
from app.runs.service import RunService, compute_config_hash
from app.storage.catalog_store import get_connection


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        RAW_STORAGE_ROOT=tmp_path / "raw", VALIDATION_STORAGE_ROOT=tmp_path / "validation",
        INTEGRITY_STORAGE_ROOT=tmp_path / "integrity", NORMALIZED_STORAGE_ROOT=tmp_path / "normalized",
        SYNCHRONIZED_STORAGE_ROOT=tmp_path / "synchronized", CLEANED_STORAGE_ROOT=tmp_path / "cleaned",
        TRANSFORMED_STORAGE_ROOT=tmp_path / "transformed", QC_STORAGE_ROOT=tmp_path / "qc",
        PACKAGE_STORAGE_ROOT=tmp_path / "packages", CATALOG_DB_PATH=tmp_path / "catalog" / "catalog.db",
        **overrides,
    )


def _service(tmp_path: Path, **overrides) -> RunService:
    settings = _settings(tmp_path, **overrides)
    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = RunRepository(conn)
    return RunService(repo=repo, settings=settings)


def _minimal_request() -> PipelineRunRequest:
    from app.packaging.models import PackagingConfig

    return PipelineRunRequest(
        streams=[PipelineStreamConfig(sensor_type="imu")],
        cleaning=PipelineCleaningConfig(policy_name="default_multimodal"),
        transformation=PipelineTransformationConfig(profile_name="multimodal_window_v1"),
        qc=PipelineQCConfig(profile_name="default_dataset_qc"),
        packaging={"profile_name": "default_ml_package", "config": PackagingConfig(split={"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}, grouping={"mode": "source_overlap"})},
    )


def test_create_run_starts_queued(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    assert run_id.startswith("run_")
    run = service.get_run(run_id)
    assert run.status == "queued"
    assert run.created_at is not None
    assert run.started_at is None
    assert run.stages_total == 0  # stage rows aren't created until the executor starts


def test_full_happy_path_transitions(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="host:1:abcd")
    assert service.get_run(run_id).status == "running"
    service.mark_completed(run_id)
    run = service.get_run(run_id)
    assert run.status == "completed"
    assert run.finished_at is not None


def test_running_to_failed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="x")
    service.mark_failed(run_id, error_code="SOME_ERROR", error_message="boom")
    run = service.get_run(run_id)
    assert run.status == "failed"
    assert run.error_code == "SOME_ERROR"
    assert run.error_message == "boom"


def test_cancel_requested_then_cancelled(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="x")
    cancelled = service.request_cancel(run_id)
    assert cancelled.status == "cancel_requested"
    service.mark_cancelled(run_id)
    assert service.get_run(run_id).status == "cancelled"


def test_cancel_requested_can_still_complete_race(tmp_path: Path) -> None:
    """Design Requirement 40: work finishing despite a pending
    cancellation is a legal outcome, never forced back to cancelled."""
    service = _service(tmp_path)
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="x")
    service.request_cancel(run_id)
    service.mark_completed(run_id)
    assert service.get_run(run_id).status == "completed"


def test_cancel_requested_can_still_fail_race(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="x")
    service.request_cancel(run_id)
    service.mark_failed(run_id, error_code="X", error_message="y")
    assert service.get_run(run_id).status == "failed"


def test_cancel_on_already_finished_run_is_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="x")
    service.mark_completed(run_id)
    result = service.request_cancel(run_id)  # no error, no state change
    assert result.status == "completed"


@pytest.mark.parametrize("bad_target", ["running", "cancelled"])
def test_invalid_transitions_rejected(tmp_path: Path, bad_target: str) -> None:
    service = _service(tmp_path)
    run_id = service.create_run(run_type="pipeline", request=_minimal_request())
    service.mark_running(run_id, executor_id="x")
    service.mark_completed(run_id)
    with pytest.raises(InvalidRunTransitionError):
        if bad_target == "running":
            service.mark_running(run_id, executor_id="x")  # completed -> running is illegal
        else:
            service.mark_cancelled(run_id)  # completed -> cancelled is illegal


def test_run_not_found(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(RunNotFoundError):
        service.get_run("run_does_not_exist")


def test_config_hash_is_deterministic_and_content_based(tmp_path: Path) -> None:
    r1 = _minimal_request()
    r2 = _minimal_request()
    assert compute_config_hash(r1) == compute_config_hash(r2)

    r3 = _minimal_request()
    r3.streams[0].sensor_type = "gps"
    assert compute_config_hash(r3) != compute_config_hash(r1)


def test_two_runs_of_identical_config_get_different_run_ids(tmp_path: Path) -> None:
    service = _service(tmp_path)
    id1 = service.create_run(run_type="pipeline", request=_minimal_request())
    id2 = service.create_run(run_type="pipeline", request=_minimal_request())
    assert id1 != id2
    assert service.get_run(id1).config_hash == service.get_run(id2).config_hash
