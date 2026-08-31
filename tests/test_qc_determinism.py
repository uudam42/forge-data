"""Tests for the QC config hash and deterministic analytical report
content across repeated runs of the same transformed dataset."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.qc.models import QCConfig
from app.qc.profiles.default import DEFAULT_DATASET_QC
from app.qc.serialization import canonical_json

QC_URL = "/api/v1/qc"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,0.{i},0.2,9.8,0.01,0.02,0.03\n" for i in range(20)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,34.02{i:02d},-118.28{i:02d},100.0,9.{i}\n" for i in range(0, 20, 4)
)


# ---------------------------------------------------------------------------
# Config hash — unit level
# ---------------------------------------------------------------------------


def test_config_hash_deterministic() -> None:
    config = QCConfig.model_validate({"minimum_samples": 10})
    h1 = DEFAULT_DATASET_QC.config_hash(config)
    h2 = DEFAULT_DATASET_QC.config_hash(QCConfig.model_validate({"minimum_samples": 10}))
    assert h1 == h2
    assert len(h1) == 64


def test_changing_threshold_changes_config_hash() -> None:
    h1 = DEFAULT_DATASET_QC.config_hash(QCConfig.model_validate({"minimum_samples": 10}))
    h2 = DEFAULT_DATASET_QC.config_hash(QCConfig.model_validate({"minimum_samples": 20}))
    assert h1 != h2


def test_config_hash_independent_of_dict_key_order() -> None:
    payload_a = {"b": 2, "a": 1}
    payload_b = {"a": 1, "b": 2}
    assert canonical_json(payload_a) == canonical_json(payload_b)


def test_baseline_qc_id_included_in_config_hash() -> None:
    h1 = DEFAULT_DATASET_QC.config_hash(QCConfig.model_validate({}))
    h2 = DEFAULT_DATASET_QC.config_hash(QCConfig.model_validate({"baseline_qc_id": "qc_abc"}))
    assert h1 != h2


# ---------------------------------------------------------------------------
# End-to-end determinism
# ---------------------------------------------------------------------------


def _upload(client: TestClient, filename: str, content: str, **fields) -> dict:
    response = client.post(
        "/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=fields
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
    return r.json()


def _transformed(client: TestClient, session_id: str = "sess_qc_determinism") -> dict:
    imu = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    sync = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": imu["normalization_id"]},
                {"name": "gps", "normalization_id": gps["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 400},
        },
    ).json()
    cleaned = client.post(
        f"/api/v1/cleaning/{sync['synchronization_id']}",
        json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}},
    ).json()
    return client.post(
        f"/api/v1/transformation/{cleaned['cleaning_id']}",
        json={
            "profile_name": "multimodal_window_v1",
            "profile_version": "1.0.0",
            "config": {
                "window": {"mode": "count", "size": 10, "stride": 5, "drop_incomplete": True},
                "features": {"imu": {"statistics": ["mean", "std"]}, "gps": {"statistics": ["mean"]}},
            },
        },
    ).json()


def _strip_volatile(report: dict) -> dict:
    stripped = copy.deepcopy(report)
    stripped.pop("qc_id", None)
    return stripped


def test_same_dataset_and_config_produces_deterministic_analytical_results(client: TestClient) -> None:
    xform = _transformed(client)
    request = {"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}}

    body1 = client.post(f"{QC_URL}/{xform['transformation_id']}", json=request).json()
    body2 = client.post(f"{QC_URL}/{xform['transformation_id']}", json=request).json()

    assert body1["qc_id"] != body2["qc_id"]

    report1 = json.loads(Path(body1["report_uri"].replace("file://", "")).read_text())
    report2 = json.loads(Path(body2["report_uri"].replace("file://", "")).read_text())

    assert _strip_volatile(report1) == _strip_volatile(report2)


def test_manifest_analytical_fields_deterministic_except_ids_and_timestamp(client: TestClient) -> None:
    xform = _transformed(client)
    request = {"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}}

    body1 = client.post(f"{QC_URL}/{xform['transformation_id']}", json=request).json()
    body2 = client.post(f"{QC_URL}/{xform['transformation_id']}", json=request).json()

    manifest1 = json.loads(Path(body1["report_uri"].replace("file://", "")).parent.joinpath("manifest.json").read_text())
    manifest2 = json.loads(Path(body2["report_uri"].replace("file://", "")).parent.joinpath("manifest.json").read_text())

    assert manifest1["qc_config_hash"] == manifest2["qc_config_hash"]
    assert manifest1["samples_checked"] == manifest2["samples_checked"]
    assert manifest1["warning_count"] == manifest2["warning_count"]
    assert manifest1["error_count"] == manifest2["error_count"]
    assert manifest1["status"] == manifest2["status"]


def test_canonical_json_used_for_report_serialization(client: TestClient) -> None:
    xform = _transformed(client)
    request = {"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}}
    body = client.post(f"{QC_URL}/{xform['transformation_id']}", json=request).json()

    report_path = Path(body["report_uri"].replace("file://", ""))
    raw_text = report_path.read_text()
    assert ": " not in raw_text
    assert ", " not in raw_text
    parsed = json.loads(raw_text)
    assert list(parsed.keys()) == sorted(parsed.keys())
