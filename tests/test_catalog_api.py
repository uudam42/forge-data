"""End-to-end tests for the catalog/lineage/dataset HTTP APIs."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,0.{i%10},0.2,9.8,0.01,0.02,0.03\n" for i in range(40)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,34.02{i%90:02d},-118.28{i%90:02d},100.0,9.{i%9}\n" for i in range(0, 40, 3)
)


def _upload(client: TestClient, filename: str, content: str, **fields) -> dict:
    response = client.post("/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=fields)
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
        json={"schema_name": schema_name, "schema_version": "1.0.0", "profile_name": profile_name, "profile_version": "1.0.0", "source_units": source_units},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _full_pipeline(client: TestClient, session_id: str = "sess_catalog_api") -> dict:
    imu = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    sync = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [{"name": "imu", "normalization_id": imu["normalization_id"]}, {"name": "gps", "normalization_id": gps["normalization_id"]}],
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
        json={"profile_name": "multimodal_window_v1", "profile_version": "1.0.0", "config": {"window": {"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True}}},
    ).json()
    qc = client.post(
        f"/api/v1/qc/{xform['transformation_id']}",
        json={"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}},
    ).json()
    pkg = client.post(
        f"/api/v1/packaging/{xform['transformation_id']}",
        json={
            "qc_id": qc["qc_id"], "profile_name": "default_ml_package", "profile_version": "1.0.0",
            "config": {"split": {"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}, "grouping": {"mode": "source_overlap"}, "exports": ["jsonl"]},
        },
    ).json()
    return {"imu": imu, "gps": gps, "sync": sync, "cleaned": cleaned, "xform": xform, "qc": qc, "pkg": pkg}


def test_api_scan_endpoint(client: TestClient) -> None:
    _full_pipeline(client)
    response = client.post("/api/v1/catalog/scan")
    assert response.status_code == 200
    body = response.json()
    assert body["artifacts_registered"] == 13


def test_api_rebuild_endpoint(client: TestClient) -> None:
    _full_pipeline(client)
    response = client.post("/api/v1/catalog/rebuild")
    assert response.status_code == 200
    body = response.json()
    assert body["artifacts_registered"] == 13
    assert body["edges_registered"] == 13


def test_health_endpoint_healthy_for_consistent_dag(client: TestClient) -> None:
    _full_pipeline(client)
    client.post("/api/v1/catalog/rebuild")
    response = client.get("/api/v1/catalog/health")
    body = response.json()
    assert body["status"] == "healthy"
    assert body["artifacts"] == 13
    assert body["cycle_count"] == 0
    assert body["issues"] == []


def test_api_artifact_endpoint(client: TestClient) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    response = client.get(f"/api/v1/catalog/artifacts/package/{setup['pkg']['package_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["artifact"]["artifact_id"] == setup["pkg"]["package_id"]
    assert len(body["parents"]) == 2


def test_unknown_artifact_type_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/catalog/artifacts/bogus_type/some_id")
    assert response.status_code == 400


def test_artifact_not_found_returns_404(client: TestClient) -> None:
    client.post("/api/v1/catalog/scan")
    response = client.get("/api/v1/catalog/artifacts/package/pkg_does_not_exist")
    assert response.status_code == 404


def test_artifact_filtering_by_stage(client: TestClient) -> None:
    _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    response = client.get("/api/v1/catalog/artifacts", params={"stage": "qc"})
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["artifact_type"] == "qc"


def test_artifact_filtering_by_status(client: TestClient) -> None:
    _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    response = client.get("/api/v1/catalog/artifacts", params={"stage": "package", "status": "completed"})
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_api_lineage_endpoint(client: TestClient) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    response = client.get(f"/api/v1/lineage/package/{setup['pkg']['package_id']}", params={"direction": "upstream"})
    assert response.status_code == 200
    body = response.json()
    assert len(body["nodes"]) == 13  # full upstream chain including root


def test_lineage_downstream_from_ingestion(client: TestClient) -> None:
    _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    artifacts = client.get("/api/v1/catalog/artifacts", params={"stage": "ingestion"}).json()
    ingestion_id = artifacts[0]["artifact_id"]
    response = client.get(f"/api/v1/lineage/ingestion/{ingestion_id}", params={"direction": "downstream"})
    assert response.status_code == 200
    types = {n["artifact_type"] for n in response.json()["nodes"]}
    assert "package" in types


def test_lineage_unknown_type_returns_400(client: TestClient) -> None:
    response = client.get("/api/v1/lineage/bogus/some_id")
    assert response.status_code == 400


def test_api_impact_endpoint(client: TestClient) -> None:
    _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    artifacts = client.get("/api/v1/catalog/artifacts", params={"stage": "ingestion"}).json()
    ingestion_id = artifacts[0]["artifact_id"]
    response = client.get(f"/api/v1/lineage/ingestion/{ingestion_id}/impact")
    assert response.status_code == 200
    body = response.json()
    assert body["affected"]["package"] == 1


def test_api_verify_endpoint(client: TestClient) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    response = client.post(f"/api/v1/catalog/verify/package/{setup['pkg']['package_id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "verified"


def test_recursive_verification_traverses_upstream(client: TestClient) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    response = client.post(f"/api/v1/catalog/verify/package/{setup['pkg']['package_id']}", params={"recursive": "true"})
    body = response.json()
    assert body["verified_nodes"] == 13
    assert body["failed_nodes"] == 0
    assert body["missing_nodes"] == 0


def test_checksum_mismatch_detected(client: TestClient, transformed_root: Path) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    matches = list(transformed_root.glob(f"*/{setup['xform']['transformation_id']}/transformed.jsonl"))
    original = matches[0].read_bytes()
    matches[0].write_bytes(original + b"tampered")
    try:
        response = client.post(f"/api/v1/catalog/verify/transformation/{setup['xform']['transformation_id']}")
        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "checksum_mismatch"
    finally:
        matches[0].write_bytes(original)


def test_missing_artifact_file_detected(client: TestClient, transformed_root: Path) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    matches = list(transformed_root.glob(f"*/{setup['xform']['transformation_id']}/transformed.jsonl"))
    original = matches[0].read_bytes()
    matches[0].unlink()
    try:
        response = client.post(f"/api/v1/catalog/verify/transformation/{setup['xform']['transformation_id']}")
        body = response.json()
        assert body["status"] == "missing"
    finally:
        matches[0].write_bytes(original)


def test_api_dataset_create_works(client: TestClient) -> None:
    response = client.post("/api/v1/datasets", json={"dataset_name": "robotics_demo", "description": "d"})
    assert response.status_code == 201
    assert response.json()["dataset_name"] == "robotics_demo"


def test_duplicate_dataset_creation_handled(client: TestClient) -> None:
    client.post("/api/v1/datasets", json={"dataset_name": "robotics_demo"})
    response = client.post("/api/v1/datasets", json={"dataset_name": "robotics_demo"})
    assert response.status_code == 200


def test_invalid_dataset_name_rejected_via_api(client: TestClient) -> None:
    response = client.post("/api/v1/datasets", json={"dataset_name": "../bad"})
    assert response.status_code == 400


def test_api_version_register_works(client: TestClient) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    client.post("/api/v1/datasets", json={"dataset_name": "robotics_demo"})
    response = client.post("/api/v1/datasets/robotics_demo/versions", json={"version": "1.0.0", "package_id": setup["pkg"]["package_id"]})
    assert response.status_code == 201
    assert response.json()["package_id"] == setup["pkg"]["package_id"]


def test_invalid_semver_rejected_via_api(client: TestClient) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    client.post("/api/v1/datasets", json={"dataset_name": "robotics_demo"})
    response = client.post("/api/v1/datasets/robotics_demo/versions", json={"version": "not-a-version", "package_id": setup["pkg"]["package_id"]})
    assert response.status_code == 400


def test_rejected_package_cannot_be_versioned(client: TestClient) -> None:
    # A single-group source_overlap dataset with a 3-way split will be rejected.
    setup = _full_pipeline(client)  # already uses train_ratio=1.0 -> completed; build a rejected one explicitly
    xform = setup["xform"]
    rejected_pkg = client.post(
        f"/api/v1/packaging/{xform['transformation_id']}",
        json={
            "qc_id": setup["qc"]["qc_id"], "profile_name": "default_ml_package", "profile_version": "1.0.0",
            "config": {"split": {"strategy": "group_hash", "train_ratio": 0.98, "validation_ratio": 0.01, "test_ratio": 0.01, "seed": 1}, "grouping": {"mode": "source_overlap"}, "exports": ["jsonl"]},
        },
    ).json()
    assert rejected_pkg["status"] == "rejected"
    client.post("/api/v1/catalog/scan")
    client.post("/api/v1/datasets", json={"dataset_name": "robotics_demo"})
    response = client.post("/api/v1/datasets/robotics_demo/versions", json={"version": "1.0.0", "package_id": rejected_pkg["package_id"]})
    assert response.status_code == 409


def test_api_version_list_works(client: TestClient) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    client.post("/api/v1/datasets", json={"dataset_name": "robotics_demo"})
    client.post("/api/v1/datasets/robotics_demo/versions", json={"version": "1.0.0", "package_id": setup["pkg"]["package_id"]})
    response = client.get("/api/v1/datasets/robotics_demo/versions")
    assert response.status_code == 200
    assert [v["version"] for v in response.json()] == ["1.0.0"]


def test_api_latest_works(client: TestClient) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    client.post("/api/v1/datasets", json={"dataset_name": "robotics_demo"})
    client.post("/api/v1/datasets/robotics_demo/versions", json={"version": "1.0.0", "package_id": setup["pkg"]["package_id"]})
    response = client.get("/api/v1/datasets/robotics_demo/latest")
    assert response.status_code == 200
    assert response.json()["version"] == "1.0.0"


def test_api_reproducibility_endpoint_works(client: TestClient) -> None:
    setup = _full_pipeline(client)
    client.post("/api/v1/catalog/scan")
    client.post("/api/v1/datasets", json={"dataset_name": "robotics_demo"})
    client.post("/api/v1/datasets/robotics_demo/versions", json={"version": "1.0.0", "package_id": setup["pkg"]["package_id"]})
    response = client.get("/api/v1/datasets/robotics_demo/versions/1.0.0/reproducibility")
    assert response.status_code == 200
    body = response.json()
    assert body["package_config_hash"] is not None
    assert len(body["raw_sha256_values"]) == 2
    assert body["lineage_fingerprint"]


def test_dataset_listing_deterministic(client: TestClient) -> None:
    client.post("/api/v1/datasets", json={"dataset_name": "zebra"})
    client.post("/api/v1/datasets", json={"dataset_name": "alpha"})
    response = client.get("/api/v1/datasets")
    names = [d["dataset_name"] for d in response.json()]
    assert names == sorted(names)
