"""Tests proving Step 10 modifies none of the Step 1-9 artifact
directories — the catalog is report-only and additive, observing and
indexing the pipeline, never mutating it."""

from __future__ import annotations

import json
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
    validation = client.post(f"/api/v1/validation/{ingestion['ingestion_id']}", json={"schema_name": schema_name, "schema_version": "1.0.0"}).json()
    integrity = client.post(f"/api/v1/integrity/{ingestion['ingestion_id']}", json={"schema_name": schema_name, "schema_version": "1.0.0"}).json()
    normalization = client.post(
        f"/api/v1/normalization/{ingestion['ingestion_id']}",
        json={"schema_name": schema_name, "schema_version": "1.0.0", "profile_name": profile_name, "profile_version": "1.0.0", "source_units": source_units},
    ).json()
    return {"ingestion": ingestion, "validation": validation, "integrity": integrity, "normalization": normalization}


def _setup(client: TestClient, session_id: str = "sess_catalog_lineage") -> dict:
    imu = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    sync = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [{"name": "imu", "normalization_id": imu["normalization"]["normalization_id"]}, {"name": "gps", "normalization_id": gps["normalization"]["normalization_id"]}],
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


def _snapshot(paths: list[Path]) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in paths if p.exists()}


def _all_pipeline_files(
    storage_root, validation_root, integrity_root, normalized_root, synchronized_root, cleaned_root, transformed_root, qc_root, package_root
) -> list[Path]:
    files = []
    for root in (storage_root, validation_root, integrity_root, normalized_root, synchronized_root, cleaned_root, transformed_root, qc_root, package_root):
        files.extend(p for p in Path(root).rglob("*") if p.is_file())
    return files


def test_no_upstream_artifact_modified_during_scan(
    client: TestClient, storage_root, validation_root, integrity_root, normalized_root, synchronized_root, cleaned_root, transformed_root, qc_root, package_root
) -> None:
    _setup(client)
    files = _all_pipeline_files(storage_root, validation_root, integrity_root, normalized_root, synchronized_root, cleaned_root, transformed_root, qc_root, package_root)
    before = _snapshot(files)

    client.post("/api/v1/catalog/scan")

    after = _snapshot(files)
    assert before == after


def test_no_upstream_artifact_modified_during_rebuild(
    client: TestClient, storage_root, validation_root, integrity_root, normalized_root, synchronized_root, cleaned_root, transformed_root, qc_root, package_root
) -> None:
    _setup(client)
    files = _all_pipeline_files(storage_root, validation_root, integrity_root, normalized_root, synchronized_root, cleaned_root, transformed_root, qc_root, package_root)
    before = _snapshot(files)

    client.post("/api/v1/catalog/rebuild")

    after = _snapshot(files)
    assert before == after


def test_no_upstream_artifact_modified_during_verification(
    client: TestClient, storage_root, validation_root, integrity_root, normalized_root, synchronized_root, cleaned_root, transformed_root, qc_root, package_root
) -> None:
    setup = _setup(client)
    client.post("/api/v1/catalog/scan")
    files = _all_pipeline_files(storage_root, validation_root, integrity_root, normalized_root, synchronized_root, cleaned_root, transformed_root, qc_root, package_root)
    before = _snapshot(files)

    client.post(f"/api/v1/catalog/verify/package/{setup['pkg']['package_id']}", params={"recursive": "true"})

    after = _snapshot(files)
    assert before == after


def test_catalog_db_contains_metadata_only_no_raw_payloads(client: TestClient, catalog_db_path: Path) -> None:
    """The SQLite catalog stores metadata/indexes only — never raw sample
    payloads or feature arrays."""
    setup = _setup(client)
    client.post("/api/v1/catalog/scan")

    import sqlite3

    conn = sqlite3.connect(str(catalog_db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT metadata_json FROM artifacts WHERE artifact_type = 'transformation'").fetchall()
    assert len(rows) == 1
    metadata = json.loads(rows[0]["metadata_json"])
    # The transformation manifest is small metadata (checksums, config
    # hashes, counts) — never the actual transformed.jsonl sample content.
    assert "artifact_uri" in metadata
    assert "features" not in metadata  # a real sample's feature payload never appears here
    assert len(json.dumps(metadata)) < 5000  # a manifest is tiny; a dumped dataset would not be
