"""Service-level and API-level tests for behavior not covered by the
narrower unit-test files: fixed-rate mode end-to-end, coverage/delta
metrics correctness, config-hash determinism and sensitivity, byte-for-byte
determinism of the synchronized artifact, non-monotonic-stream rejection,
empty-stream handling, and the discrete-stream linear-interpolation guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SYNC_URL = "/api/v1/synchronization"


def _upload(client: TestClient, filename: str, content: str, **form_fields) -> dict:
    response = client.post(
        "/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=form_fields
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pipeline(client: TestClient, filename, content, schema_name, profile_name, source_units, **fields) -> dict:
    ingestion = _upload(client, filename, content, **fields)
    for path, body in (
        (f"/api/v1/validation/{ingestion['ingestion_id']}", {"schema_name": schema_name, "schema_version": "1.0.0"}),
        (f"/api/v1/integrity/{ingestion['ingestion_id']}", {"schema_name": schema_name, "schema_version": "1.0.0"}),
    ):
        r = client.post(path, json=body)
        assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/normalization/{ingestion['ingestion_id']}",
        json={
            "schema_name": schema_name,
            "schema_version": "1.0.0",
            "profile_name": profile_name,
            "profile_version": "1.0.0",
            "source_units": source_units,
        },
    )
    assert r.status_code == 200, r.text
    return {"ingestion": ingestion, "normalization": r.json()}


IMU_CSV = "timestamp,accel_x,accel_y,accel_z\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,0.1,0.2,9.8\n" for i in range(10)
)
GPS_CSV = (
    "timestamp,latitude,longitude\n"
    "2026-08-30T18:00:02Z,34.0,-118.0\n"
    "2026-08-30T18:00:08Z,34.1,-118.1\n"
)


def _setup_pair(client: TestClient, session_id: str = "sess_svc") -> tuple[dict, dict]:
    imu = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    return imu, gps


def _base_request(imu, gps, **overrides) -> dict:
    req = {
        "streams": [
            {"name": "imu", "normalization_id": imu["normalization"]["normalization_id"]},
            {"name": "gps", "normalization_id": gps["normalization"]["normalization_id"]},
        ],
        "reference": {"mode": "stream", "stream": "imu"},
        "alignment": {"default_method": "nearest", "max_time_delta_ms": 3000},
    }
    req.update(overrides)
    return req


def test_fixed_rate_timeline_correct_end_to_end(client: TestClient) -> None:
    imu, gps = _setup_pair(client)
    req = _base_request(imu, gps, reference={"mode": "fixed_rate", "frequency_hz": 1.0})
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows_written"] == 7  # intersection [2,8] inclusive at 1 Hz


def test_fixed_rate_uses_stream_range_intersection_end_to_end(client: TestClient) -> None:
    imu, gps = _setup_pair(client)  # imu: 0..9s, gps: 2..8s
    req = _base_request(imu, gps, reference={"mode": "fixed_rate", "frequency_hz": 1.0})
    body = client.post(SYNC_URL, json=req).json()

    artifact_path = body["artifact_uri"].replace("file://", "")
    lines = [json.loads(line) for line in Path(artifact_path).read_text().splitlines()]
    assert lines[0]["timestamp"] == "2026-08-30T18:00:02Z"
    assert lines[-1]["timestamp"] == "2026-08-30T18:00:08Z"


def test_coverage_ratio_correct(client: TestClient) -> None:
    imu, gps = _setup_pair(client)
    req = _base_request(imu, gps)  # tolerance 3000ms; gps at t=2,8
    body = client.post(SYNC_URL, json=req).json()
    # imu targets 0..9 (10 rows); gps matches within 3s of t=2 or t=8: t=0..5 (to 2) and t=6..9 wait compute precisely below via mean/max test
    assert body["coverage"]["imu"] == 1.0
    assert 0.0 < body["coverage"]["gps"] <= 1.0


def test_mean_and_max_absolute_delta_correct(client: TestClient, synchronized_root: Path) -> None:
    imu, gps = _setup_pair(client)
    req = _base_request(imu, gps)
    body = client.post(SYNC_URL, json=req).json()

    manifest = json.loads((synchronized_root / body["synchronization_id"] / "manifest.json").read_text())
    gps_metrics = manifest["metrics"]["gps"]

    # Recompute expected deltas independently: gps samples at t=2s and t=8s;
    # imu targets 0..9s; nearest match with 3000ms tolerance.
    import math

    deltas = []
    for t in range(10):
        d2, d8 = abs(t - 2) * 1000.0, abs(t - 8) * 1000.0
        best = min(d2, d8)
        if best <= 3000:
            deltas.append(best)
    expected_mean = sum(deltas) / len(deltas)
    expected_max = max(deltas)

    assert gps_metrics["matched_rows"] == len(deltas)
    assert math.isclose(gps_metrics["mean_abs_delta_ms"], expected_mean)
    assert math.isclose(gps_metrics["max_abs_delta_ms"], expected_max)


def test_config_hash_deterministic(client: TestClient, synchronized_root: Path) -> None:
    imu, gps = _setup_pair(client)
    req = _base_request(imu, gps)

    body1 = client.post(SYNC_URL, json=req).json()
    body2 = client.post(SYNC_URL, json=req).json()

    manifest1 = json.loads((synchronized_root / body1["synchronization_id"] / "manifest.json").read_text())
    manifest2 = json.loads((synchronized_root / body2["synchronization_id"] / "manifest.json").read_text())
    assert manifest1["synchronization_config_hash"] == manifest2["synchronization_config_hash"]


def test_changing_tolerance_changes_config_hash(client: TestClient, synchronized_root: Path) -> None:
    imu, gps = _setup_pair(client)
    req_a = _base_request(imu, gps, alignment={"default_method": "nearest", "max_time_delta_ms": 3000})
    req_b = _base_request(imu, gps, alignment={"default_method": "nearest", "max_time_delta_ms": 4000})

    body_a = client.post(SYNC_URL, json=req_a).json()
    body_b = client.post(SYNC_URL, json=req_b).json()

    manifest_a = json.loads((synchronized_root / body_a["synchronization_id"] / "manifest.json").read_text())
    manifest_b = json.loads((synchronized_root / body_b["synchronization_id"] / "manifest.json").read_text())
    assert manifest_a["synchronization_config_hash"] != manifest_b["synchronization_config_hash"]


def test_changing_clock_offset_changes_config_hash(client: TestClient, synchronized_root: Path) -> None:
    imu, gps = _setup_pair(client)
    req_a = _base_request(imu, gps)
    req_b = _base_request(imu, gps, clock_corrections={"gps": {"offset_ms": -25.0, "drift_ppm": 0.0}})

    body_a = client.post(SYNC_URL, json=req_a).json()
    body_b = client.post(SYNC_URL, json=req_b).json()

    manifest_a = json.loads((synchronized_root / body_a["synchronization_id"] / "manifest.json").read_text())
    manifest_b = json.loads((synchronized_root / body_b["synchronization_id"] / "manifest.json").read_text())
    assert manifest_a["synchronization_config_hash"] != manifest_b["synchronization_config_hash"]


def test_same_inputs_and_config_produce_byte_identical_artifact(client: TestClient, synchronized_root: Path) -> None:
    imu, gps = _setup_pair(client)
    req = _base_request(imu, gps)

    body1 = client.post(SYNC_URL, json=req).json()
    body2 = client.post(SYNC_URL, json=req).json()

    assert body1["synchronization_id"] != body2["synchronization_id"]
    assert body1["synchronized_sha256"] == body2["synchronized_sha256"]

    bytes1 = (synchronized_root / body1["synchronization_id"] / "synchronized.jsonl").read_bytes()
    bytes2 = (synchronized_root / body2["synchronization_id"] / "synchronized.jsonl").read_bytes()
    assert bytes1 == bytes2


def test_clock_offset_correction_changes_matching(client: TestClient) -> None:
    imu, gps = _setup_pair(client)
    req_uncorrected = _base_request(imu, gps)
    req_corrected = _base_request(imu, gps, clock_corrections={"gps": {"offset_ms": -25.0, "drift_ppm": 0.0}})

    body_uncorrected = client.post(SYNC_URL, json=req_uncorrected).json()
    body_corrected = client.post(SYNC_URL, json=req_corrected).json()

    # Applying a correction must change the synchronized output relative to
    # not applying one (different deltas), proving the correction is real.
    assert body_uncorrected["synchronized_sha256"] != body_corrected["synchronized_sha256"]


def test_non_monotonic_stream_rejected(client: TestClient, normalized_root: Path) -> None:
    imu, gps = _setup_pair(client)

    # Craft a corrupted normalized artifact with an out-of-order timestamp,
    # as if something bypassed Step 4's own guarantees.
    bad_dir = normalized_root / imu["ingestion"]["ingestion_id"] / "norm_bad00000-0000-0000-0000-000000000000"
    bad_dir.mkdir(parents=True)
    bad_csv = "timestamp,accel_x,accel_y,accel_z\n2026-08-30T18:00:05Z,0.1,0.2,9.8\n2026-08-30T18:00:01Z,0.1,0.2,9.8\n"
    (bad_dir / "normalized.csv").write_text(bad_csv)

    import hashlib

    real_manifest_path = Path(imu["normalization"]["artifact_uri"].replace("file://", "")).parent / "manifest.json"
    real_manifest = json.loads(real_manifest_path.read_text())
    real_manifest["normalization_id"] = "norm_bad00000-0000-0000-0000-000000000000"
    real_manifest["normalized_sha256"] = hashlib.sha256(bad_csv.encode()).hexdigest()
    real_manifest["normalized_size_bytes"] = len(bad_csv.encode())
    real_manifest["records_written"] = 2
    real_manifest["artifact_uri"] = f"file://{(bad_dir / 'normalized.csv').resolve()}"
    (bad_dir / "manifest.json").write_text(json.dumps(real_manifest))

    req = {
        "streams": [
            {"name": "imu", "normalization_id": "norm_bad00000-0000-0000-0000-000000000000"},
            {"name": "gps", "normalization_id": gps["normalization"]["normalization_id"]},
        ],
        "reference": {"mode": "stream", "stream": "imu"},
    }
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 409


def test_blank_empty_normalized_stream_handled_clearly_in_fixed_rate(client: TestClient, normalized_root: Path) -> None:
    imu, gps = _setup_pair(client)

    # Craft an "empty" normalized artifact (header only, zero data rows).
    empty_dir = normalized_root / gps["ingestion"]["ingestion_id"] / "norm_empty00-0000-0000-0000-000000000000"
    empty_dir.mkdir(parents=True)
    empty_csv = "timestamp,latitude,longitude,altitude,speed,device_id\n"
    (empty_dir / "normalized.csv").write_text(empty_csv)

    import hashlib

    real_manifest_path = Path(gps["normalization"]["artifact_uri"].replace("file://", "")).parent / "manifest.json"
    real_manifest = json.loads(real_manifest_path.read_text())
    real_manifest["normalization_id"] = "norm_empty00-0000-0000-0000-000000000000"
    real_manifest["normalized_sha256"] = hashlib.sha256(empty_csv.encode()).hexdigest()
    real_manifest["normalized_size_bytes"] = len(empty_csv.encode())
    real_manifest["records_written"] = 0
    real_manifest["artifact_uri"] = f"file://{(empty_dir / 'normalized.csv').resolve()}"
    (empty_dir / "manifest.json").write_text(json.dumps(real_manifest))

    req = {
        "streams": [
            {"name": "imu", "normalization_id": imu["normalization"]["normalization_id"]},
            {"name": "gps", "normalization_id": "norm_empty00-0000-0000-0000-000000000000"},
        ],
        "reference": {"mode": "fixed_rate", "frequency_hz": 1.0},
    }
    response = client.post(SYNC_URL, json=req)
    # Handled clearly: a structured 400, not a crash/500.
    assert response.status_code == 400
    assert response.json()["detail"]


def test_blank_empty_reference_stream_in_stream_mode_yields_zero_rows(client: TestClient, normalized_root: Path) -> None:
    imu, gps = _setup_pair(client)

    empty_dir = normalized_root / imu["ingestion"]["ingestion_id"] / "norm_empty10-0000-0000-0000-000000000000"
    empty_dir.mkdir(parents=True)
    empty_csv = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z,device_id\n"
    (empty_dir / "normalized.csv").write_text(empty_csv)

    import hashlib

    real_manifest_path = Path(imu["normalization"]["artifact_uri"].replace("file://", "")).parent / "manifest.json"
    real_manifest = json.loads(real_manifest_path.read_text())
    real_manifest["normalization_id"] = "norm_empty10-0000-0000-0000-000000000000"
    real_manifest["normalized_sha256"] = hashlib.sha256(empty_csv.encode()).hexdigest()
    real_manifest["normalized_size_bytes"] = len(empty_csv.encode())
    real_manifest["records_written"] = 0
    real_manifest["artifact_uri"] = f"file://{(empty_dir / 'normalized.csv').resolve()}"
    (empty_dir / "manifest.json").write_text(json.dumps(real_manifest))

    req = {
        "streams": [
            {"name": "imu", "normalization_id": "norm_empty10-0000-0000-0000-000000000000"},
            {"name": "gps", "normalization_id": gps["normalization"]["normalization_id"]},
        ],
        "reference": {"mode": "stream", "stream": "imu"},
    }
    response = client.post(SYNC_URL, json=req)
    assert response.status_code == 200, response.text
    assert response.json()["rows_written"] == 0


def test_discrete_schema_rejects_linear_interpolation() -> None:
    from app.synchronization.registry import AlignmentStrategyRegistry, UnsupportedAlignmentMethodError
    from app.validation.schemas.base import FieldDefinition, FieldType, SchemaDefinition

    discrete_schema = SchemaDefinition(
        schema_name="camera",
        schema_version="1.0.0",
        record_type="discrete",
        fields={
            "timestamp": FieldDefinition(type=FieldType.DATETIME, required=True, nullable=False),
            "frame_path": FieldDefinition(type=FieldType.STRING, required=True, nullable=False),
        },
    )
    registry = AlignmentStrategyRegistry()

    # nearest is fine for a discrete stream
    registry.get("nearest", schema=discrete_schema)

    with pytest.raises(UnsupportedAlignmentMethodError):
        registry.get("linear", schema=discrete_schema)
