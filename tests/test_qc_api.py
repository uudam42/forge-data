"""End-to-end tests for the QC HTTP API.

Covers the full ingest -> validate -> integrity -> normalize -> synchronize
-> clean -> transform -> qc pipeline, request validation, and the
request-level error cases.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

QC_URL = "/api/v1/qc"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,0.{i},0.2,9.8,0.01,0.02,0.03\n" for i in range(20)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,34.02{i:02d},-118.28{i:02d},100.0,9.{i}\n" for i in range(0, 20, 4)
)


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


def _transformed(client: TestClient, session_id: str = "sess_qc_api", **window_overrides) -> dict:
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
    window = {"mode": "count", "size": 10, "stride": 5, "drop_incomplete": True}
    window.update(window_overrides)
    xform = client.post(
        f"/api/v1/transformation/{cleaned['cleaning_id']}",
        json={
            "profile_name": "multimodal_window_v1",
            "profile_version": "1.0.0",
            "config": {
                "window": window,
                "features": {
                    "imu": {"statistics": ["mean", "std", "min", "max"]},
                    "gps": {"statistics": ["mean"]},
                    "include_modality_mask": True,
                },
            },
        },
    ).json()
    return xform


def _default_qc_request(**config_overrides) -> dict:
    config = {
        "minimum_samples": 1,
        "modality_coverage": {"imu": {"minimum_ratio": 0.95, "severity": "error"}},
        "feature_completeness": {"maximum_missing_ratio": 0.5},
        "variance": {"enabled": True, "minimum_variance": 1e-12},
    }
    config.update(config_overrides)
    return {"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": config}


def test_valid_dataset_qc_passes(client: TestClient) -> None:
    xform = _transformed(client)
    response = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request(minimum_samples=1))
    assert response.status_code == 200, response.text
    body = response.json()
    # gyro fields are constant in this fixture data, so this dataset is
    # expected to surface LOW_FEATURE_VARIANCE warnings.
    assert body["status"] in ("passed", "passed_with_warnings")


def test_warning_only_qc_yields_passed_with_warnings(client: TestClient) -> None:
    xform = _transformed(client)
    response = client.post(
        f"{QC_URL}/{xform['transformation_id']}",
        json=_default_qc_request(variance={"enabled": True, "minimum_variance": 1e-12, "severity": "warning"}),
    )
    body = response.json()
    assert body["status"] == "passed_with_warnings"
    assert body["summary"]["error_count"] == 0
    assert body["summary"]["warning_count"] > 0


def test_error_issue_yields_failed(client: TestClient) -> None:
    xform = _transformed(client)
    response = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request(minimum_samples=1000))
    body = response.json()
    assert body["status"] == "failed"


def test_qc_failure_returns_http_200(client: TestClient) -> None:
    xform = _transformed(client)
    response = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request(minimum_samples=1000))
    assert response.status_code == 200


def test_minimum_sample_threshold_works(client: TestClient) -> None:
    xform = _transformed(client)
    response = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request(minimum_samples=1000))
    body = response.json()
    assert any(i["code"] == "DATASET_TOO_SMALL" for i in body_issues(response, body))


def body_issues(response, body):
    report_path = Path(body["report_uri"].replace("file://", ""))
    return json.loads(report_path.read_text())["issues"]


def test_report_persisted(client: TestClient, qc_root: Path) -> None:
    xform = _transformed(client)
    body = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request()).json()
    report_path = qc_root / xform["transformation_id"] / body["qc_id"] / "report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert report["qc_id"] == body["qc_id"]


def test_manifest_persisted(client: TestClient, qc_root: Path) -> None:
    xform = _transformed(client)
    body = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request()).json()
    manifest_path = qc_root / xform["transformation_id"] / body["qc_id"] / "manifest.json"
    assert manifest_path.exists()


def test_manifest_contains_transformation_id_and_transformed_sha256(client: TestClient, qc_root: Path) -> None:
    xform = _transformed(client)
    body = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request()).json()
    manifest = json.loads((qc_root / xform["transformation_id"] / body["qc_id"] / "manifest.json").read_text())
    assert manifest["transformation_id"] == xform["transformation_id"]
    assert manifest["source_transformed_sha256"] == xform["transformed_sha256"]


def test_manifest_contains_profile_and_version(client: TestClient, qc_root: Path) -> None:
    xform = _transformed(client)
    body = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request()).json()
    manifest = json.loads((qc_root / xform["transformation_id"] / body["qc_id"] / "manifest.json").read_text())
    assert manifest["profile"] == {"name": "default_dataset_qc", "version": "1.0.0"}


def test_report_sha256_correct(client: TestClient, qc_root: Path) -> None:
    xform = _transformed(client)
    body = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request()).json()
    report_dir = qc_root / xform["transformation_id"] / body["qc_id"]
    report_bytes = (report_dir / "report.json").read_bytes()
    manifest = json.loads((report_dir / "manifest.json").read_text())
    assert hashlib.sha256(report_bytes).hexdigest() == manifest["report_sha256"]


def test_empty_transformed_dataset_returns_failed_with_empty_dataset(client: TestClient) -> None:
    xform = _transformed(client)
    artifact_path = Path(xform["artifact_uri"].replace("file://", ""))
    artifact_path.write_text("")
    manifest_path = artifact_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["transformed_sha256"] = hashlib.sha256(b"").hexdigest()
    manifest["transformed_size_bytes"] = 0
    manifest_path.write_text(json.dumps(manifest))

    response = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request(minimum_samples=None))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    issues = body_issues(response, body)
    assert any(i["code"] == "EMPTY_DATASET" for i in issues)


def test_transformation_not_found_returns_404(client: TestClient) -> None:
    response = client.post(f"{QC_URL}/xform_does_not_exist", json=_default_qc_request())
    assert response.status_code == 404


def test_profile_not_found_returns_404(client: TestClient) -> None:
    xform = _transformed(client)
    request = _default_qc_request()
    request["profile_name"] = "does_not_exist"
    response = client.post(f"{QC_URL}/{xform['transformation_id']}", json=request)
    assert response.status_code == 404


def test_baseline_missing_returns_404(client: TestClient) -> None:
    xform = _transformed(client)
    request = _default_qc_request(baseline_qc_id="qc_does_not_exist")
    response = client.post(f"{QC_URL}/{xform['transformation_id']}", json=request)
    assert response.status_code == 404


def test_transformed_checksum_mismatch_returns_409(client: TestClient) -> None:
    xform = _transformed(client)
    artifact_path = Path(xform["artifact_uri"].replace("file://", ""))
    original = artifact_path.read_bytes()
    artifact_path.write_bytes(original + b"tampered")
    try:
        response = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request())
        assert response.status_code == 409
    finally:
        artifact_path.write_bytes(original)


def test_invalid_configuration_returns_400(client: TestClient) -> None:
    xform = _transformed(client)
    response = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request(minimum_samples=-1))
    assert response.status_code == 400


def test_existing_qc_run_cannot_be_overwritten(client: TestClient, qc_root: Path) -> None:
    xform = _transformed(client)
    body1 = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request()).json()
    body2 = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request()).json()
    assert body1["qc_id"] != body2["qc_id"]
    assert (qc_root / xform["transformation_id"] / body1["qc_id"]).exists()
    assert (qc_root / xform["transformation_id"] / body2["qc_id"]).exists()


def test_baseline_drift_detected_end_to_end(client: TestClient) -> None:
    xform = _transformed(client)
    baseline = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request()).json()

    request = _default_qc_request(
        baseline_qc_id=baseline["qc_id"],
        drift={"enabled": True, "max_abs_standardized_mean_difference": 1.0, "severity": "warning"},
    )
    response = client.post(f"{QC_URL}/{xform['transformation_id']}", json=request)
    assert response.status_code == 200, response.text
    body = response.json()
    report = json.loads(Path(body["report_uri"].replace("file://", "")).read_text())
    assert report["drift"]["baseline_qc_id"] == baseline["qc_id"]
    assert report["drift"]["compatible"] is True
    # Identical dataset compared to itself -> zero drift everywhere.
    for result in report["drift"]["features"].values():
        if result["compared"]:
            assert result["standardized_mean_difference"] == 0.0


def test_no_baseline_means_no_drift_section(client: TestClient) -> None:
    xform = _transformed(client)
    body = client.post(f"{QC_URL}/{xform['transformation_id']}", json=_default_qc_request()).json()
    report = json.loads(Path(body["report_uri"].replace("file://", "")).read_text())
    assert report["drift"] is None
