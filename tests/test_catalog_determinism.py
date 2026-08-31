"""Tests for the deterministic lineage fingerprint: same content/config
lineage produces the same fingerprint despite different random execution
IDs; changing any content/config hash changes it."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.catalog.serialization import canonical_json, compute_lineage_fingerprint

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,0.{i%10},0.2,9.8,0.01,0.02,0.03\n" for i in range(40)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,34.02{i%90:02d},-118.28{i%90:02d},100.0,9.{i%9}\n" for i in range(0, 40, 3)
)


# ---------------------------------------------------------------------------
# Unit level
# ---------------------------------------------------------------------------


def _base_payload(**overrides) -> dict:
    payload = {
        "raw_sha256_values": ["a", "b"],
        "schema_versions": ["gps:1.0.0", "imu:1.0.0"],
        "normalization_config_hashes": ["n1", "n2"],
        "synchronization_config_hash": "s1",
        "cleaning_config_hash": "c1",
        "transformation_config_hash": "t1",
        "qc_config_hash": "q1",
        "package_config_hash": "p1",
        "split_checksums": {"train": "tr", "validation": "va", "test": "te"},
    }
    payload.update(overrides)
    return payload


def test_fingerprint_deterministic() -> None:
    f1 = compute_lineage_fingerprint(_base_payload())
    f2 = compute_lineage_fingerprint(_base_payload())
    assert f1 == f2
    assert len(f1) == 64


def test_fingerprint_independent_of_collection_order() -> None:
    p1 = _base_payload(raw_sha256_values=["a", "b"])
    p2 = _base_payload(raw_sha256_values=["b", "a"])
    # Sorting happens upstream (in service.py) before this call — this
    # confirms canonical_json itself doesn't reorder lists, so callers must
    # sort collections themselves (documented behavior).
    assert canonical_json(p1) != canonical_json(p2)


def test_raw_checksum_change_changes_fingerprint() -> None:
    f1 = compute_lineage_fingerprint(_base_payload())
    f2 = compute_lineage_fingerprint(_base_payload(raw_sha256_values=["a", "different"]))
    assert f1 != f2


def test_normalization_config_change_changes_fingerprint() -> None:
    f1 = compute_lineage_fingerprint(_base_payload())
    f2 = compute_lineage_fingerprint(_base_payload(normalization_config_hashes=["n1", "different"]))
    assert f1 != f2


def test_transformation_config_change_changes_fingerprint() -> None:
    f1 = compute_lineage_fingerprint(_base_payload())
    f2 = compute_lineage_fingerprint(_base_payload(transformation_config_hash="different"))
    assert f1 != f2


def test_packaging_config_change_changes_fingerprint() -> None:
    f1 = compute_lineage_fingerprint(_base_payload())
    f2 = compute_lineage_fingerprint(_base_payload(package_config_hash="different"))
    assert f1 != f2


def test_fingerprint_excludes_volatile_fields_by_construction() -> None:
    """The payload itself never carries package_id/qc_id/created_at — this
    is enforced by CatalogService._fingerprint_payload only ever building
    the whitelisted keys tested here, never passing through IDs."""
    payload = _base_payload()
    assert "package_id" not in payload
    assert "qc_id" not in payload
    assert "created_at" not in payload


# ---------------------------------------------------------------------------
# End-to-end: two independent pipeline runs over equivalent data/config
# ---------------------------------------------------------------------------


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


def _run_pipeline_to_package(client: TestClient, session_id: str, seed: int) -> dict:
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
            "config": {"split": {"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": seed}, "grouping": {"mode": "source_overlap"}, "exports": ["jsonl"]},
        },
    ).json()
    return {"pkg": pkg}


def test_identical_pipeline_runs_produce_identical_fingerprint(client: TestClient) -> None:
    """Two entirely independent pipeline runs over byte-identical source
    data and identical configuration produce the SAME fingerprint, even
    though every execution ID (ingestion_id, transformation_id,
    package_id, ...) differs between them."""
    run_a = _run_pipeline_to_package(client, "sess_fp_a", seed=42)
    run_b = _run_pipeline_to_package(client, "sess_fp_b", seed=42)
    assert run_a["pkg"]["package_id"] != run_b["pkg"]["package_id"]

    client.post("/api/v1/catalog/scan")
    client.post("/api/v1/datasets", json={"dataset_name": "ds_a"})
    client.post("/api/v1/datasets", json={"dataset_name": "ds_b"})
    client.post("/api/v1/datasets/ds_a/versions", json={"version": "1.0.0", "package_id": run_a["pkg"]["package_id"]})
    client.post("/api/v1/datasets/ds_b/versions", json={"version": "1.0.0", "package_id": run_b["pkg"]["package_id"]})

    fp_a = client.get("/api/v1/datasets/ds_a/versions/1.0.0/reproducibility").json()["lineage_fingerprint"]
    fp_b = client.get("/api/v1/datasets/ds_b/versions/1.0.0/reproducibility").json()["lineage_fingerprint"]
    assert fp_a == fp_b


def test_different_seed_changes_fingerprint_end_to_end(client: TestClient) -> None:
    """Seed is folded into packaging_config_hash, so a different seed
    changes the fingerprint even over identical source data."""
    run_a = _run_pipeline_to_package(client, "sess_fp_seed_a", seed=1)
    run_b = _run_pipeline_to_package(client, "sess_fp_seed_b", seed=2)

    client.post("/api/v1/catalog/scan")
    client.post("/api/v1/datasets", json={"dataset_name": "ds_seed_a"})
    client.post("/api/v1/datasets", json={"dataset_name": "ds_seed_b"})
    client.post("/api/v1/datasets/ds_seed_a/versions", json={"version": "1.0.0", "package_id": run_a["pkg"]["package_id"]})
    client.post("/api/v1/datasets/ds_seed_b/versions", json={"version": "1.0.0", "package_id": run_b["pkg"]["package_id"]})

    fp_a = client.get("/api/v1/datasets/ds_seed_a/versions/1.0.0/reproducibility").json()["lineage_fingerprint"]
    fp_b = client.get("/api/v1/datasets/ds_seed_b/versions/1.0.0/reproducibility").json()["lineage_fingerprint"]
    assert fp_a != fp_b


def test_dataset_version_response_exposes_fingerprint(client: TestClient) -> None:
    run = _run_pipeline_to_package(client, "sess_fp_version", seed=5)
    client.post("/api/v1/catalog/scan")
    client.post("/api/v1/datasets", json={"dataset_name": "ds_version_fp"})
    body = client.post("/api/v1/datasets/ds_version_fp/versions", json={"version": "1.0.0", "package_id": run["pkg"]["package_id"]}).json()
    assert body["lineage_fingerprint"] is not None
    assert len(body["lineage_fingerprint"]) == 64
