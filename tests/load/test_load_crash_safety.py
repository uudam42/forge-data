"""v2.2 Design Requirement 21: large-scale streaming combined with
injected failure must still leave no partial finalized artifact, must
leave upstream artifacts unchanged, and any v2.2 temp state (here: the
sqlite dedup index) must not leak. Opt-in only -- run with `pytest -m load`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.storage.atomic import fault_injector


class _InjectedCrash(Exception):
    pass


@pytest.fixture(autouse=True)
def _clear_fault_injector():
    fault_injector.clear()
    yield
    fault_injector.clear()


_BASE_TIME = datetime(2026, 8, 30, 18, 0, 0, tzinfo=timezone.utc)


def _large_imu_csv(num_rows: int) -> str:
    lines = ["timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z"]
    for i in range(num_rows):
        ts = (_BASE_TIME + timedelta(milliseconds=i * 10)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        lines.append(f"{ts},0.{i % 10},0.2,9.8,0.01,0.02,0.03")
    return "\n".join(lines) + "\n"


def _companion_gps_csv(num_rows: int) -> str:
    # 1/10th the IMU rate, spanning the same time range -- just enough
    # to make this a genuine (>=2 stream) multimodal synchronization run.
    lines = ["timestamp,latitude,longitude,altitude,speed"]
    for i in range(max(1, num_rows // 10)):
        ts = (_BASE_TIME + timedelta(milliseconds=i * 100)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        lines.append(f"{ts},34.0{i % 90:02d}00,-118.2{i % 90:02d}00,100.0,9.{i % 9}")
    return "\n".join(lines) + "\n"


@pytest.mark.load
def test_large_cleaning_run_with_sqlite_dedup_crashes_safely(
    client: TestClient, test_settings: Settings, cleaned_root: Path, synchronized_root: Path
) -> None:
    num_rows = 50_000
    content = _large_imu_csv(num_rows)
    session_id = "sess_load_crash"

    # This run's CSV is a few MB -- override the test suite's normally
    # tiny 1 MiB upload ceiling for just this large-scale test.
    large_upload_settings = test_settings.model_copy(update={"MAX_UPLOAD_SIZE_MB": 50})
    app.dependency_overrides[get_settings] = lambda: large_upload_settings
    client = TestClient(app)

    ingestion = client.post(
        "/api/v1/ingestion/upload",
        files={"file": ("imu.csv", content.encode(), None)},
        data={"customer_id": "load_crash", "session_id": session_id},
    ).json()
    ingestion_id = ingestion["ingestion_id"]
    original_sha256 = ingestion["sha256"]

    for path in (f"/api/v1/validation/{ingestion_id}", f"/api/v1/integrity/{ingestion_id}"):
        r = client.post(path, json={"schema_name": "imu", "schema_version": "1.0.0"})
        assert r.status_code == 200, r.text

    norm = client.post(
        f"/api/v1/normalization/{ingestion_id}",
        json={
            "schema_name": "imu", "schema_version": "1.0.0",
            "profile_name": "imu_canonical", "profile_version": "1.0.0",
            "source_units": {"acceleration": "m/s^2", "angular_velocity": "rad/s"},
        },
    ).json()

    gps_ingestion = client.post(
        "/api/v1/ingestion/upload",
        files={"file": ("gps.csv", _companion_gps_csv(num_rows).encode(), None)},
        data={"customer_id": "load_crash", "session_id": session_id},
    ).json()
    for path in (f"/api/v1/validation/{gps_ingestion['ingestion_id']}", f"/api/v1/integrity/{gps_ingestion['ingestion_id']}"):
        r = client.post(path, json={"schema_name": "gps", "schema_version": "1.0.0"})
        assert r.status_code == 200, r.text
    gps_norm = client.post(
        f"/api/v1/normalization/{gps_ingestion['ingestion_id']}",
        json={
            "schema_name": "gps", "schema_version": "1.0.0",
            "profile_name": "gps_canonical", "profile_version": "1.0.0",
            "source_units": {"altitude": "m", "speed": "m/s"},
        },
    ).json()

    sync = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": norm["normalization_id"]},
                {"name": "gps", "normalization_id": gps_norm["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 500},
        },
    ).json()
    assert sync["rows_written"] == num_rows

    # Crash after the sqlite dedup index has processed real work (data
    # written, manifest about to be written) -- proves the large-scale
    # temp index gets torn down correctly even mid-failure.
    fault_injector.install("BEFORE_RENAME", lambda: (_ for _ in ()).throw(_InjectedCrash()))

    with pytest.raises(_InjectedCrash):
        client.post(
            f"/api/v1/cleaning/{sync['synchronization_id']}",
            json={
                "policy_name": "default_multimodal", "policy_version": "1.0.0",
                "config": {"required_streams": ["imu"], "duplicate_policy": {"enabled": True, "backend": "sqlite"}},
            },
        )

    # 1. No finalized partial cleaning artifact.
    sync_cleaned_dir = cleaned_root / sync["synchronization_id"]
    committed = [d for d in sync_cleaned_dir.iterdir() if d.is_dir() and not d.name.startswith(".tmp-")] if sync_cleaned_dir.exists() else []
    assert committed == []

    # 2. No leaked dedup temp state anywhere under the cleaned root.
    assert list(cleaned_root.rglob(".dedup_index.sqlite3*")) == []

    # 3. Upstream (synchronized artifact + raw ingestion) unchanged.
    sync_manifest_matches = list(synchronized_root.glob(f"{sync['synchronization_id']}/manifest.json"))
    assert len(sync_manifest_matches) == 1

    import hashlib

    raw_matches = list(cleaned_root.parent.glob(f"raw/*/*/{ingestion_id}/original/*"))
    assert raw_matches, "expected the raw ingestion artifact to still exist"
    actual = hashlib.sha256(raw_matches[0].read_bytes()).hexdigest()
    assert actual == original_sha256

    # 4. Safely rerunnable from the beginning.
    fault_injector.clear()
    retry = client.post(
        f"/api/v1/cleaning/{sync['synchronization_id']}",
        json={
            "policy_name": "default_multimodal", "policy_version": "1.0.0",
            "config": {"required_streams": ["imu"], "duplicate_policy": {"enabled": True, "backend": "sqlite"}},
        },
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["summary"]["input_rows"] == num_rows
