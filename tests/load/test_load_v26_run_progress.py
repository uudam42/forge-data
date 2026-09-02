"""Load test: a large-scale run-aware pipeline execution stays bounded in
progress-related SQLite write volume regardless of row count (v2.6,
Design Requirement 15 / the "write amplification" success criterion).
Opt-in only -- run with `pytest -m load`.

Progress in this system is recorded at STAGE BOUNDARIES (start_stage/
complete_stage), not per-record (see docs/DETAILED_GUIDE.md's v2.6
section, "Cancellation and progress granularity") -- so the number of
progress-related UPDATEs issued for a run is bounded by 2x its stage
count, completely independent of how many rows/records any individual
stage processes. This test proves that scaling property directly by
counting real UPDATE calls against a 200,000-row two-stream run.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, _default_schema_dir, get_settings
from app.main import app
from app.runs.repository import RunRepository
from tests.v26_helpers import DEFAULT_CONFIG

pytestmark = pytest.mark.load

ROW_COUNT = 200_000
_BASE_TIME = datetime(2026, 8, 30, 0, 0, 0, tzinfo=timezone.utc)


def _generate_imu_csv(path: Path, rows: int) -> None:
    with path.open("w") as f:
        f.write("timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n")
        for i in range(rows):
            ts = (_BASE_TIME + timedelta(milliseconds=10 * i)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            f.write(f"{ts},0.{i % 10},0.2,9.8,0.01,0.02,0.03\n")


def _generate_gps_csv(path: Path, rows: int) -> None:
    with path.open("w") as f:
        f.write("timestamp,latitude,longitude,altitude,speed\n")
        for i in range(0, rows, 3):
            ts = (_BASE_TIME + timedelta(milliseconds=10 * i)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            f.write(f"{ts},34.0{i % 90:02d},-118.2{i % 90:02d},100.0,9.{i % 9}\n")


def test_progress_db_write_count_is_bounded_independent_of_row_count(tmp_path: Path) -> None:
    imu_path = tmp_path / "imu_large.csv"
    gps_path = tmp_path / "gps_large.csv"
    _generate_imu_csv(imu_path, ROW_COUNT)
    _generate_gps_csv(gps_path, ROW_COUNT)

    settings = Settings(
        RAW_STORAGE_ROOT=tmp_path / "raw", MAX_UPLOAD_SIZE_MB=100, SCHEMA_DIR=_default_schema_dir(),
        VALIDATION_STORAGE_ROOT=tmp_path / "validation", INTEGRITY_STORAGE_ROOT=tmp_path / "integrity",
        NORMALIZED_STORAGE_ROOT=tmp_path / "normalized", SYNCHRONIZED_STORAGE_ROOT=tmp_path / "synchronized",
        CLEANED_STORAGE_ROOT=tmp_path / "cleaned", TRANSFORMED_STORAGE_ROOT=tmp_path / "transformed",
        QC_STORAGE_ROOT=tmp_path / "qc", PACKAGE_STORAGE_ROOT=tmp_path / "packages",
        CATALOG_DB_PATH=tmp_path / "catalog" / "catalog.db",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        client = TestClient(app)

        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["session_id"] = "sess_load_v26"
        config["streams"] = [
            {"sensor_type": "imu", "source_units": {"acceleration": "m/s^2", "angular_velocity": "rad/s"}},
            {"sensor_type": "gps", "source_units": {"altitude": "m", "speed": "m/s"}},
        ]
        # A 200,000-row count-window transformation with stride=size would
        # produce a huge number of windows; widen it so this stays a
        # progress/write-amplification test, not an unrelated transformation
        # stress test (already covered by tests/load/test_load_transformation_windowing.py).
        config["transformation"]["config"]["window"] = {"mode": "count", "size": 5000, "stride": 5000, "drop_incomplete": True}

        real_update_stage_run = RunRepository.update_stage_run
        call_count = {"n": 0}

        def _counting_update_stage_run(self, *args, **kwargs):
            call_count["n"] += 1
            return real_update_stage_run(self, *args, **kwargs)

        with patch.object(RunRepository, "update_stage_run", _counting_update_stage_run):
            with imu_path.open("rb") as imu_f, gps_path.open("rb") as gps_f:
                resp = client.post(
                    "/api/v1/runs",
                    data={"config": json.dumps(config)},
                    files=[("files", ("imu.csv", imu_f, "text/csv")), ("files", ("gps.csv", gps_f, "text/csv"))],
                )
        assert resp.status_code == 202, resp.text
        run_id = resp.json()["run_id"]

        final = client.get(f"/api/v1/runs/{run_id}").json()
        assert final["status"] == "completed", final
        assert final["stages_total"] == 13

        validation_stage = next(s for s in final["stage_runs"] if s["stage"] == "validation:imu")
        assert validation_stage["records_processed"] == ROW_COUNT
        assert validation_stage["records_total"] == ROW_COUNT

        # Bounded by stage count, NOT by row count: at most ~3 UPDATEs per
        # stage (start, records_total backfill, complete) x 13 stages -- a
        # generous multiple of that, so this asserts genuine scale-
        # independence rather than pinning an exact number.
        assert call_count["n"] < 100, f"{call_count['n']} progress UPDATEs for a {ROW_COUNT}-row run -- expected O(stage count), not O(rows)"
        print(f"\n[write-amplification] {ROW_COUNT} records processed via {call_count['n']} progress DB UPDATEs across {final['stages_total']} stages")
    finally:
        app.dependency_overrides.clear()
