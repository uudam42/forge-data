"""End-to-end tests for the packaging HTTP API.

Covers the full ingest -> validate -> integrity -> normalize -> synchronize
-> clean -> transform -> qc -> package pipeline, request validation, and
the request-level error cases.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app

PKG_URL = "/api/v1/packaging"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,0.{i%10},0.2,9.8,0.01,0.02,0.03\n" for i in range(300)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,34.02{i%90:02d},-118.28{i%90:02d},100.0,9.{i%9}\n" for i in range(0, 300, 3)
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


def _qc_ready(client: TestClient, session_id: str, *, window_size=10, window_stride=10, drop_incomplete=True) -> dict:
    """Runs the full pipeline through an accepted QC result using
    NON-overlapping windows by default (stride == size), so source_overlap
    grouping naturally produces one independent group per window — this is
    what lets a 3-way split actually succeed in most API tests below."""
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
    xform = client.post(
        f"/api/v1/transformation/{cleaned['cleaning_id']}",
        json={
            "profile_name": "multimodal_window_v1",
            "profile_version": "1.0.0",
            "config": {
                "window": {"mode": "count", "size": window_size, "stride": window_stride, "drop_incomplete": drop_incomplete},
                "features": {"imu": {"statistics": ["mean", "std"]}, "gps": {"statistics": ["mean"]}},
            },
        },
    ).json()
    qc = client.post(
        f"/api/v1/qc/{xform['transformation_id']}",
        json={"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}},
    ).json()
    return {"transformation_id": xform["transformation_id"], "qc_id": qc["qc_id"], "qc_status": qc["status"]}


def _default_package_request(**overrides) -> dict:
    request = {
        "qc_id": None,
        "profile_name": "default_ml_package",
        "profile_version": "1.0.0",
        "config": {
            "split": {"strategy": "group_hash", "train_ratio": 0.7, "validation_ratio": 0.15, "test_ratio": 0.15, "seed": 42},
            "grouping": {"mode": "source_overlap"},
            "exports": ["jsonl"],
        },
    }
    request.update(overrides)
    return request


def test_valid_packaging_succeeds(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_valid")
    request = _default_package_request(qc_id=ready["qc_id"])
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["summary"]["source_samples"] == body["summary"]["packaged_samples"]


def test_passed_qc_accepted(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_passed")
    assert ready["qc_status"] == "passed"
    request = _default_package_request(qc_id=ready["qc_id"])
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 200


def test_passed_with_warnings_qc_accepted(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_warn")
    request = {
        "profile_name": "default_dataset_qc",
        "profile_version": "1.0.0",
        "config": {"variance": {"enabled": True, "minimum_variance": 1e12}},  # forces warnings
    }
    qc2 = client.post(f"/api/v1/qc/{ready['transformation_id']}", json=request).json()
    assert qc2["status"] == "passed_with_warnings"
    pkg_request = _default_package_request(qc_id=qc2["qc_id"])
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=pkg_request)
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_failed_qc_rejected_with_409(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_failed")
    failing_qc = client.post(
        f"/api/v1/qc/{ready['transformation_id']}",
        json={"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 999999}},
    ).json()
    assert failing_qc["status"] == "failed"
    request = _default_package_request(qc_id=failing_qc["qc_id"])
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 409


def test_qc_transformation_mismatch_rejected(client: TestClient) -> None:
    ready_a = _qc_ready(client, "sess_pkg_mismatch_a")
    ready_b = _qc_ready(client, "sess_pkg_mismatch_b")
    request = _default_package_request(qc_id=ready_a["qc_id"])
    response = client.post(f"{PKG_URL}/{ready_b['transformation_id']}", json=request)
    assert response.status_code == 409


def _find_transformed_artifact(transformed_root: Path, transformation_id: str) -> Path:
    matches = list(transformed_root.glob(f"*/{transformation_id}/transformed.jsonl"))
    assert matches, f"expected to locate the transformed artifact for {transformation_id} on disk"
    return matches[0]


def test_transformed_checksum_mismatch_rejected(client: TestClient, transformed_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_tamper")
    artifact_path = _find_transformed_artifact(transformed_root, ready["transformation_id"])
    original = artifact_path.read_bytes()
    artifact_path.write_bytes(original + b"tampered")
    try:
        request = _default_package_request(qc_id=ready["qc_id"])
        response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
        assert response.status_code == 409
    finally:
        artifact_path.write_bytes(original)


def test_qc_report_checksum_mismatch_rejected(client: TestClient, qc_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_qc_tamper")
    report_path = qc_root / ready["transformation_id"] / ready["qc_id"] / "report.json"
    original = report_path.read_bytes()
    report_path.write_bytes(original + b"tampered")
    try:
        request = _default_package_request(qc_id=ready["qc_id"])
        response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
        assert response.status_code == 409
    finally:
        report_path.write_bytes(original)


def test_valid_ratios_accepted(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_ratios_ok")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["config"]["split"] = {"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 200


def test_ratios_not_summing_to_one_rejected(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_ratios_bad")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["config"]["split"]["train_ratio"] = 0.5
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 400


def test_negative_ratio_rejected(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_neg_ratio")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["config"]["split"]["validation_ratio"] = -0.1
    request["config"]["split"]["train_ratio"] = 1.1
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 400


def test_zero_train_ratio_rejected(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_zero_train")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["config"]["split"] = {"strategy": "group_hash", "train_ratio": 0.0, "validation_ratio": 0.5, "test_ratio": 0.5, "seed": 1}
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 400


def test_zero_validation_ratio_allowed(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_zero_val")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["config"]["split"] = {"strategy": "group_hash", "train_ratio": 0.9, "validation_ratio": 0.0, "test_ratio": 0.1, "seed": 1}
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 200


def test_zero_test_ratio_allowed(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_zero_test")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["config"]["split"] = {"strategy": "group_hash", "train_ratio": 0.9, "validation_ratio": 0.1, "test_ratio": 0.0, "seed": 1}
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 200


def test_same_group_never_crosses_split(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_no_cross", window_size=10, window_stride=5)  # overlapping -> 1 group
    request = _default_package_request(qc_id=ready["qc_id"])
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    body = response.json()
    report = json.loads(Path(body["report_uri"].replace("file://", "")).read_text())
    assert report["leakage_checks"]["cross_split_groups"] == 0
    assert report["leakage_checks"]["passed"] is True


def test_one_group_with_three_nonzero_splits_produces_rejected_status(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_one_group", window_size=10, window_stride=5)  # overlapping -> 1 group
    request = _default_package_request(qc_id=ready["qc_id"])
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert "INSUFFICIENT_GROUPS_FOR_SPLIT" in body["rejection_reasons"]


def test_empty_requested_split_detected(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_empty_split", window_size=10, window_stride=5)
    request = _default_package_request(qc_id=ready["qc_id"])
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    body = response.json()
    assert "EMPTY_REQUESTED_SPLIT" in body["rejection_reasons"]


def test_rejected_packaging_returns_http_200(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_rejected_200", window_size=10, window_stride=5)
    request = _default_package_request(qc_id=ready["qc_id"])
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 200


def test_train_validation_test_jsonl_generated(client: TestClient, package_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_files")
    request = _default_package_request(qc_id=ready["qc_id"])
    body = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()
    pkg_dir = package_root / ready["transformation_id"] / body["package_id"]
    assert (pkg_dir / "train.jsonl").exists()
    assert (pkg_dir / "validation.jsonl").exists()
    assert (pkg_dir / "test.jsonl").exists()


def test_split_index_generated_with_required_fields(client: TestClient, package_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_index")
    request = _default_package_request(qc_id=ready["qc_id"])
    body = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()
    pkg_dir = package_root / ready["transformation_id"] / body["package_id"]
    index_path = pkg_dir / "split_index.jsonl"
    assert index_path.exists()
    lines = [json.loads(line) for line in index_path.read_text().splitlines()]
    assert len(lines) > 0
    for entry in lines:
        assert "sample_id" in entry
        assert "group_id" in entry
        assert "split" in entry


def test_requested_and_actual_ratios_reported(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_ratios_report")
    request = _default_package_request(qc_id=ready["qc_id"])
    body = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()
    report = json.loads(Path(body["report_uri"].replace("file://", "")).read_text())
    assert report["requested_split_ratios"] == {"train": 0.7, "validation": 0.15, "test": 0.15}
    assert "sample_ratio" in report["actual"]["train"]
    assert "groups" in report["actual"]["train"]


def test_empty_source_dataset_handled(client: TestClient, transformed_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_empty_source")
    artifact_path = _find_transformed_artifact(transformed_root, ready["transformation_id"])
    original = artifact_path.read_bytes()
    artifact_path.write_bytes(b"")

    import hashlib as _hashlib

    manifest_path = artifact_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["transformed_sha256"] = _hashlib.sha256(b"").hexdigest()
    manifest["transformed_size_bytes"] = 0
    manifest_path.write_text(json.dumps(manifest))

    # Note: QC's own cached source_transformed_sha256 still points at the
    # ORIGINAL (non-empty) artifact, so the QC-side checksum gate is
    # expected to legitimately reject this as tampered (409) — this test
    # accepts either outcome; what must never happen is a crash or a false
    # "completed" result.
    try:
        request = _default_package_request(qc_id=ready["qc_id"])
        response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
        # Either blocked at the checksum gate (409, since QC's cached
        # checksum no longer matches) or -- if reached -- rejected for
        # EMPTY_SOURCE_DATASET. Both are valid, safe outcomes; what must
        # NEVER happen is a crash (500) or a false "completed" (200/completed).
        assert response.status_code in (200, 409)
        if response.status_code == 200:
            assert response.json()["status"] == "rejected"
    finally:
        artifact_path.write_bytes(original)


def test_unsupported_strategy_returns_400(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_bad_strategy")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["config"]["split"]["strategy"] = "bogus"
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 400


def test_unsupported_grouping_mode_returns_400(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_bad_grouping")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["config"]["grouping"]["mode"] = "bogus"
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 400


def test_unsupported_export_returns_400(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_bad_export")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["config"]["exports"] = ["bogus_format"]
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 400


def test_transformation_not_found_returns_404(client: TestClient) -> None:
    request = _default_package_request(qc_id="qc_does_not_exist")
    response = client.post(f"{PKG_URL}/xform_does_not_exist", json=request)
    assert response.status_code == 404


def test_qc_not_found_returns_404(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_qc_404")
    request = _default_package_request(qc_id="qc_does_not_exist")
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 404


def test_profile_not_found_returns_404(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_profile_404")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["profile_name"] = "does_not_exist"
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 404


def test_dataset_name_and_version_persisted(client: TestClient, package_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_metadata")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["dataset_name"] = "warehouse_robot_imu_gps"
    request["dataset_version"] = "1.2.3"
    request["description"] = "test dataset"
    body = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()
    manifest = json.loads((package_root / ready["transformation_id"] / body["package_id"] / "manifest.json").read_text())
    assert manifest["dataset_name"] == "warehouse_robot_imu_gps"
    assert manifest["dataset_version"] == "1.2.3"
    assert manifest["description"] == "test dataset"


def test_invalid_dataset_version_rejected(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_bad_version")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["dataset_version"] = "not-a-semver"
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 422  # pydantic request validation error


def test_dataset_metadata_does_not_affect_assignments(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_metadata_neutral")
    request_a = _default_package_request(qc_id=ready["qc_id"])
    request_b = _default_package_request(qc_id=ready["qc_id"])
    request_b["dataset_name"] = "totally_different_name"
    body_a = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request_a).json()
    body_b = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request_b).json()
    bytes_a = Path(json.loads(Path(body_a["report_uri"].replace("file://", "")).parent.joinpath("manifest.json").read_text())["splits"]["train"]["artifact_uri"].replace("file://", "")).read_bytes()
    bytes_b = Path(json.loads(Path(body_b["report_uri"].replace("file://", "")).parent.joinpath("manifest.json").read_text())["splits"]["train"]["artifact_uri"].replace("file://", "")).read_bytes()
    assert bytes_a == bytes_b


def test_existing_package_collision_does_not_overwrite(client: TestClient, package_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_collision")
    request = _default_package_request(qc_id=ready["qc_id"])
    body1 = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()
    body2 = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()
    assert body1["package_id"] != body2["package_id"]
    assert (package_root / ready["transformation_id"] / body1["package_id"]).exists()
    assert (package_root / ready["transformation_id"] / body2["package_id"]).exists()


def test_session_grouping_works_when_metadata_available(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_session_mode")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["config"]["grouping"] = {"mode": "session"}
    request["config"]["split"] = {"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["group_count"] == 1  # single session -> exactly one group
    assert body["status"] == "completed"


def test_session_grouping_never_splits_same_session(client: TestClient) -> None:
    ready = _qc_ready(client, "sess_pkg_session_no_split")
    request = _default_package_request(qc_id=ready["qc_id"])
    request["config"]["grouping"] = {"mode": "session"}
    response = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    body = response.json()
    # 3 nonzero splits requested, but session mode -> 1 group -> rejected,
    # never breaks the single session across splits.
    assert body["status"] == "rejected"
    report = json.loads(Path(body["report_uri"].replace("file://", "")).read_text())
    assert report["leakage_checks"]["cross_split_groups"] == 0


def test_disk_preflight_rejects_impossible_request_before_writing(
    client: TestClient, test_settings: Settings, package_root: Path
) -> None:
    """v2.2: an intentionally impossible disk-space requirement (an
    astronomically large DISK_RESERVE_BYTES) must be rejected with 507
    BEFORE any package files are written -- not partway through."""
    ready = _qc_ready(client, "sess_pkg_disk_preflight")
    request = _default_package_request(qc_id=ready["qc_id"])

    impossible_settings = test_settings.model_copy(update={"DISK_RESERVE_BYTES": 10**18})
    app.dependency_overrides[get_settings] = lambda: impossible_settings
    try:
        with TestClient(app) as strict_client:
            response = strict_client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request)
    finally:
        app.dependency_overrides[get_settings] = lambda: test_settings

    assert response.status_code == 507, response.text
    body = response.json()["detail"]
    assert body["code"] == "INSUFFICIENT_DISK_SPACE"
    assert body["stage"] == "packaging"
    assert body["reserve_bytes"] == 10**18

    # Nothing was written for this transformation_id -- rejected before
    # any expensive work began, not partway through.
    transformation_pkg_dir = package_root / ready["transformation_id"]
    existing = [d for d in transformation_pkg_dir.iterdir() if d.is_dir()] if transformation_pkg_dir.exists() else []
    assert existing == []
