"""End-to-end tests for the ingestion HTTP API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

import app.ingestion.service as service_module
from app.version import __version__

UPLOAD_URL = "/api/v1/ingestion/upload"


def _manifest_path(storage_root: Path, customer_id: str, session_id: str, ingestion_id: str) -> Path:
    return storage_root / customer_id / session_id / ingestion_id / "manifest.json"


def _original_path(storage_root: Path, customer_id: str, session_id: str, ingestion_id: str, filename: str) -> Path:
    return storage_root / customer_id / session_id / ingestion_id / "original" / filename


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_csv_upload_succeeds(client: TestClient) -> None:
    content = b"timestamp,accel_x,accel_y\n0.0,0.1,0.2\n"
    response = client.post(
        UPLOAD_URL,
        files={"file": ("imu_data.csv", content, "text/csv")},
        data={"customer_id": "customer_001", "device_id": "imu_01"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "stored"
    assert body["original_filename"] == "imu_data.csv"
    assert body["customer_id"] == "customer_001"
    assert body["device_id"] == "imu_01"
    assert body["size_bytes"] == len(content)
    assert body["sha256"] == hashlib.sha256(content).hexdigest()


def test_json_upload_succeeds(client: TestClient) -> None:
    content = json.dumps({"event": "startup"}).encode()
    response = client.post(UPLOAD_URL, files={"file": ("event.json", content, "application/json")})

    assert response.status_code == 201
    assert response.json()["original_filename"] == "event.json"


def test_unsupported_extension_fails(client: TestClient) -> None:
    response = client.post(UPLOAD_URL, files={"file": ("payload.exe", b"binary", "application/octet-stream")})

    assert response.status_code == 415


def test_empty_file_fails(client: TestClient) -> None:
    response = client.post(UPLOAD_URL, files={"file": ("empty.csv", b"", "text/csv")})

    assert response.status_code == 400


def test_generated_session_id_exists_when_not_provided(client: TestClient) -> None:
    response = client.post(UPLOAD_URL, files={"file": ("a.csv", b"a,b\n1,2\n", "text/csv")})

    assert response.status_code == 201
    assert response.json()["session_id"]


def test_provided_session_id_is_preserved(client: TestClient) -> None:
    response = client.post(
        UPLOAD_URL,
        files={"file": ("a.csv", b"a,b\n1,2\n", "text/csv")},
        data={"session_id": "sess_custom_123"},
    )

    assert response.status_code == 201
    assert response.json()["session_id"] == "sess_custom_123"


def test_sha256_is_correct(client: TestClient) -> None:
    content = b"x" * 10_000
    response = client.post(UPLOAD_URL, files={"file": ("data.jsonl", content, "application/jsonl")})

    assert response.json()["sha256"] == hashlib.sha256(content).hexdigest()


def test_raw_file_exists_after_upload(client: TestClient, storage_root: Path) -> None:
    content = b"a,b\n1,2\n"
    response = client.post(
        UPLOAD_URL,
        files={"file": ("a.csv", content, "text/csv")},
        data={"customer_id": "cust_x", "session_id": "sess_x"},
    )
    body = response.json()

    stored = _original_path(storage_root, "cust_x", "sess_x", body["ingestion_id"], "a.csv")
    assert stored.exists()
    assert stored.read_bytes() == content


def test_manifest_exists_after_upload(client: TestClient, storage_root: Path) -> None:
    response = client.post(
        UPLOAD_URL,
        files={"file": ("a.csv", b"a,b\n1,2\n", "text/csv")},
        data={"customer_id": "cust_y", "session_id": "sess_y"},
    )
    body = response.json()

    manifest_file = _manifest_path(storage_root, "cust_y", "sess_y", body["ingestion_id"])
    assert manifest_file.exists()


def test_manifest_metadata_matches_response(client: TestClient, storage_root: Path) -> None:
    response = client.post(
        UPLOAD_URL,
        files={"file": ("a.csv", b"a,b\n1,2\n", "text/csv")},
        data={"customer_id": "cust_z", "session_id": "sess_z", "device_id": "dev_1", "source_type": "lidar", "notes": "test run"},
    )
    body = response.json()

    manifest_file = _manifest_path(storage_root, "cust_z", "sess_z", body["ingestion_id"])
    manifest = json.loads(manifest_file.read_text())

    assert manifest["ingestion_id"] == body["ingestion_id"]
    assert manifest["session_id"] == body["session_id"]
    assert manifest["customer_id"] == body["customer_id"]
    assert manifest["device_id"] == "dev_1"
    assert manifest["source_type"] == "lidar"
    assert manifest["notes"] == "test run"
    assert manifest["sha256"] == body["sha256"]
    assert manifest["size_bytes"] == body["size_bytes"]
    assert manifest["storage_uri"] == body["storage_uri"]
    assert manifest["pipeline_stage"] == "raw"
    assert manifest["ingested_at"]


def test_uploaded_bytes_are_unchanged(client: TestClient, storage_root: Path) -> None:
    content = bytes(range(256)) * 500
    response = client.post(
        UPLOAD_URL,
        files={"file": ("raw.csv", content, "text/csv")},
        data={"customer_id": "cust_bytes", "session_id": "sess_bytes"},
    )
    body = response.json()

    stored = _original_path(storage_root, "cust_bytes", "sess_bytes", body["ingestion_id"], "raw.csv")
    assert stored.read_bytes() == content


def test_filename_path_traversal_is_neutralized(client: TestClient, storage_root: Path) -> None:
    response = client.post(
        UPLOAD_URL,
        files={"file": ("../../etc/passwd.csv", b"a,b\n1,2\n", "text/csv")},
        data={"customer_id": "cust_trav", "session_id": "sess_trav"},
    )

    assert response.status_code == 201
    body = response.json()

    assert ".." not in body["original_filename"]
    assert "/" not in body["original_filename"]

    # File must be stored strictly inside this ingestion's directory tree.
    ingestion_dir = storage_root / "cust_trav" / "sess_trav" / body["ingestion_id"]
    stored_files = list((ingestion_dir / "original").iterdir())
    assert len(stored_files) == 1
    assert stored_files[0].parent == ingestion_dir / "original"

    # No file was written outside the storage root.
    assert not (storage_root.parent / "etc").exists()


def test_duplicate_ingestion_id_cannot_overwrite_existing_data(
    client: TestClient, storage_root: Path, monkeypatch
) -> None:
    monkeypatch.setattr(service_module, "generate_ingestion_id", lambda: "ing_fixed_collision")

    first = client.post(
        UPLOAD_URL,
        files={"file": ("a.csv", b"first-content\n", "text/csv")},
        data={"customer_id": "cust_dup", "session_id": "sess_dup"},
    )
    assert first.status_code == 201

    second = client.post(
        UPLOAD_URL,
        files={"file": ("a.csv", b"second-content-should-not-land\n", "text/csv")},
        data={"customer_id": "cust_dup", "session_id": "sess_dup"},
    )
    assert second.status_code == 409

    stored = _original_path(storage_root, "cust_dup", "sess_dup", "ing_fixed_collision", "a.csv")
    assert stored.read_bytes() == b"first-content\n"
