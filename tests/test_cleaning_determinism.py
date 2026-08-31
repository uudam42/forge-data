"""Tests for the cleaning config hash and byte-for-byte determinism of the
cleaned artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.cleaning.models import CleaningConfig, DuplicatePolicyConfig, PrivacyConfig
from app.cleaning.policies.default import DEFAULT_MULTIMODAL_V1
from app.cleaning.rules.common import canonical_json

CLEAN_URL = "/api/v1/cleaning"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,0.1,0.2,9.8\n" for i in range(6)
)
GPS_CSV = "timestamp,latitude,longitude\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,34.020{i},-118.285{i}\n" for i in range(6)
)


# ---------------------------------------------------------------------------
# Config hash — unit level
# ---------------------------------------------------------------------------


def test_config_hash_deterministic() -> None:
    config = CleaningConfig(required_streams=["imu"], min_present_streams=1)
    h1 = DEFAULT_MULTIMODAL_V1.config_hash(config)
    h2 = DEFAULT_MULTIMODAL_V1.config_hash(CleaningConfig(required_streams=["imu"], min_present_streams=1))
    assert h1 == h2
    assert len(h1) == 64


def test_changing_required_streams_changes_config_hash() -> None:
    h1 = DEFAULT_MULTIMODAL_V1.config_hash(CleaningConfig(required_streams=["imu"]))
    h2 = DEFAULT_MULTIMODAL_V1.config_hash(CleaningConfig(required_streams=["imu", "gps"]))
    assert h1 != h2


def test_changing_privacy_fields_changes_config_hash() -> None:
    h1 = DEFAULT_MULTIMODAL_V1.config_hash(
        CleaningConfig(privacy=PrivacyConfig(redact_fields=["streams.gps.latitude"]))
    )
    h2 = DEFAULT_MULTIMODAL_V1.config_hash(
        CleaningConfig(privacy=PrivacyConfig(redact_fields=["streams.gps.latitude", "streams.gps.longitude"]))
    )
    assert h1 != h2


def test_changing_duplicate_policy_changes_config_hash() -> None:
    h1 = DEFAULT_MULTIMODAL_V1.config_hash(CleaningConfig(duplicate_policy=DuplicatePolicyConfig(enabled=True)))
    h2 = DEFAULT_MULTIMODAL_V1.config_hash(CleaningConfig(duplicate_policy=DuplicatePolicyConfig(enabled=False)))
    assert h1 != h2


def test_config_hash_independent_of_dict_key_order() -> None:
    """A pydantic model always serializes fields in declaration order, so
    this mainly proves canonical_json/config_hash aren't accidentally
    sensitive to incidental JSON structure — sort_keys is what guarantees
    determinism, not declaration order."""
    payload_a = {"b": 2, "a": 1}
    payload_b = {"a": 1, "b": 2}
    assert canonical_json(payload_a) == canonical_json(payload_b)


# ---------------------------------------------------------------------------
# End-to-end byte determinism
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


def _synchronized(client: TestClient, session_id: str = "sess_determinism") -> dict:
    imu = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    response = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": imu["normalization_id"]},
                {"name": "gps", "normalization_id": gps["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 500},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_same_input_and_config_creates_byte_identical_cleaned_artifact(
    client: TestClient, cleaned_root: Path
) -> None:
    sync = _synchronized(client)
    request = {
        "policy_name": "default_multimodal",
        "policy_version": "1.0.0",
        "config": {
            "required_streams": ["imu"],
            "duplicate_policy": {"enabled": True},
            "privacy": {"redact_fields": ["streams.gps.latitude"]},
        },
    }

    body1 = client.post(f"{CLEAN_URL}/{sync['synchronization_id']}", json=request).json()
    body2 = client.post(f"{CLEAN_URL}/{sync['synchronization_id']}", json=request).json()

    assert body1["cleaning_id"] != body2["cleaning_id"]
    assert body1["cleaned_sha256"] == body2["cleaned_sha256"]

    bytes1 = (cleaned_root / sync["synchronization_id"] / body1["cleaning_id"] / "cleaned.jsonl").read_bytes()
    bytes2 = (cleaned_root / sync["synchronization_id"] / body2["cleaning_id"] / "cleaned.jsonl").read_bytes()
    assert bytes1 == bytes2


def test_canonical_json_serialization_used_for_output(client: TestClient) -> None:
    sync = _synchronized(client)
    request = {"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {}}
    body = client.post(f"{CLEAN_URL}/{sync['synchronization_id']}", json=request).json()

    artifact_path = Path(body["artifact_uri"].replace("file://", ""))
    first_line = artifact_path.read_text().splitlines()[0]

    # sort_keys + compact separators: no spaces after ":"/"," and top-level
    # keys appear in alphabetical order (alignment, streams, timestamp).
    assert ": " not in first_line
    assert ", " not in first_line
    parsed = json.loads(first_line)
    row_keys = list(json.loads(first_line).keys())
    assert row_keys == sorted(row_keys)
    assert "alignment" in parsed and "streams" in parsed and "timestamp" in parsed
