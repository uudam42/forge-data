"""Shared helper for building a full, real, end-to-end pipeline run
(IMU + GPS -> sync -> clean -> transform -> qc -> package) through the
live HTTP API, for v2.5 governance/rebuild tests. Adapted from
tests/test_catalog_lineage.py's `_setup` helper — same config values,
known to work end-to-end."""

from __future__ import annotations

from fastapi.testclient import TestClient

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,0.{i%10},0.2,9.8,0.01,0.02,0.03\n" for i in range(40)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,34.02{i%90:02d},-118.28{i%90:02d},100.0,9.{i%9}\n" for i in range(0, 40, 3)
)

CLEANING_CONFIG = {"required_streams": ["imu"]}
TRANSFORMATION_CONFIG = {"window": {"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True}}
QC_CONFIG = {"minimum_samples": 1}
PACKAGING_CONFIG = {
    "split": {"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1},
    "grouping": {"mode": "source_overlap"},
    "exports": ["jsonl"],
}


def upload(client: TestClient, filename: str, content: str, **fields) -> dict:
    response = client.post("/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=fields)
    assert response.status_code == 201, response.text
    return response.json()


def normalize_stream(client: TestClient, filename, content, schema_name, profile_name, source_units, **fields) -> dict:
    ingestion = upload(client, filename, content, **fields)
    client.post(f"/api/v1/validation/{ingestion['ingestion_id']}", json={"schema_name": schema_name, "schema_version": "1.0.0"})
    client.post(f"/api/v1/integrity/{ingestion['ingestion_id']}", json={"schema_name": schema_name, "schema_version": "1.0.0"})
    normalization_resp = client.post(
        f"/api/v1/normalization/{ingestion['ingestion_id']}",
        json={"schema_name": schema_name, "schema_version": "1.0.0", "profile_name": profile_name, "profile_version": "1.0.0", "source_units": source_units},
    )
    assert normalization_resp.status_code == 200, normalization_resp.text
    return {"ingestion": ingestion, "normalization": normalization_resp.json()}


def build_full_pipeline(client: TestClient, session_id: str) -> dict:
    """Returns a dict with every stage's response, plus the artifact IDs
    needed to build downstream: imu, gps, sync, cleaned, xform, qc, pkg."""
    imu = normalize_stream(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = normalize_stream(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    sync = build_downstream_from_normalizations(
        client, imu_normalization_id=imu["normalization"]["normalization_id"], gps_normalization_id=gps["normalization"]["normalization_id"]
    )
    return {"imu": imu, "gps": gps, **sync}


def build_downstream_from_normalizations(client: TestClient, *, imu_normalization_id: str, gps_normalization_id: str) -> dict:
    """The part of the pipeline downstream of normalization -- factored
    out so a rebuild test can call this again with a REPLACED
    normalization_id and directly compare the two branches."""
    sync_resp = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [{"name": "imu", "normalization_id": imu_normalization_id}, {"name": "gps", "normalization_id": gps_normalization_id}],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 400},
        },
    )
    assert sync_resp.status_code == 200, sync_resp.text
    sync = sync_resp.json()

    cleaned_resp = client.post(
        f"/api/v1/cleaning/{sync['synchronization_id']}",
        json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": CLEANING_CONFIG},
    )
    assert cleaned_resp.status_code == 200, cleaned_resp.text
    cleaned = cleaned_resp.json()

    xform_resp = client.post(
        f"/api/v1/transformation/{cleaned['cleaning_id']}",
        json={"profile_name": "multimodal_window_v1", "profile_version": "1.0.0", "config": TRANSFORMATION_CONFIG},
    )
    assert xform_resp.status_code == 200, xform_resp.text
    xform = xform_resp.json()

    qc_resp = client.post(
        f"/api/v1/qc/{xform['transformation_id']}",
        json={"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": QC_CONFIG},
    )
    assert qc_resp.status_code == 200, qc_resp.text
    qc = qc_resp.json()

    pkg_resp = client.post(
        f"/api/v1/packaging/{xform['transformation_id']}",
        json={"qc_id": qc["qc_id"], "profile_name": "default_ml_package", "profile_version": "1.0.0", "config": PACKAGING_CONFIG},
    )
    assert pkg_resp.status_code == 200, pkg_resp.text
    pkg = pkg_resp.json()

    return {"sync": sync, "cleaned": cleaned, "xform": xform, "qc": qc, "pkg": pkg}


def create_dataset_and_version(client: TestClient, *, dataset_name: str, version: str, package_id: str) -> dict:
    create_resp = client.post("/api/v1/datasets", json={"dataset_name": dataset_name})
    assert create_resp.status_code in (200, 201), create_resp.text
    version_resp = client.post(f"/api/v1/datasets/{dataset_name}/versions", json={"version": version, "package_id": package_id})
    assert version_resp.status_code in (200, 201), version_resp.text
    return version_resp.json()
