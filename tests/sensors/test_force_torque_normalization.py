"""Force/Torque normalization tests (Design Requirements 7, 8; test
items 23-33). All conversions and alias handling run through the
EXISTING generic RecordNormalizer/UnitDimension engine -- no new
normalization code was added, only two new UnitDimension constants
(FORCE, TORQUE)."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import _default_schema_dir

from fastapi.testclient import TestClient

FT_CSV = (
    "timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z\n"
    "2026-08-30T18:00:00.500Z,1.0,2.0,3.0,0.1,0.2,0.3\n"
)


def _upload(client: TestClient, filename: str, content: bytes, **fields) -> dict:
    r = client.post("/api/v1/ingestion/upload", files={"file": (filename, content, None)}, data=fields)
    assert r.status_code == 201, r.text
    return r.json()


def _run_to_normalized(client: TestClient, csv: str, source_units: dict) -> dict:
    ing = _upload(client, "ft.csv", csv.encode())
    for path in (f"/api/v1/validation/{ing['ingestion_id']}", f"/api/v1/integrity/{ing['ingestion_id']}"):
        r = client.post(path, json={"schema_name": "force_torque", "schema_version": "1.0.0"})
        assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/normalization/{ing['ingestion_id']}",
        json={
            "schema_name": "force_torque", "schema_version": "1.0.0",
            "profile_name": "force_torque_canonical", "profile_version": "1.0.0",
            "source_units": source_units,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _read_normalized_rows(normalized_root: Path, response: dict) -> list[dict]:
    path = Path(response["artifact_uri"].replace("file://", ""))
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    import csv as csv_module

    reader = csv_module.DictReader(lines)
    return list(reader)


def test_n_identity(client: TestClient, normalized_root: Path) -> None:
    response = _run_to_normalized(client, FT_CSV, {"force": "N", "torque": "N*m"})
    rows = _read_normalized_rows(normalized_root, response)
    assert float(rows[0]["force_x"]) == 1.0
    assert float(rows[0]["torque_x"]) == 0.1


def test_kn_to_n(client: TestClient, normalized_root: Path) -> None:
    response = _run_to_normalized(client, FT_CSV, {"force": "kN", "torque": "N*m"})
    rows = _read_normalized_rows(normalized_root, response)
    assert float(rows[0]["force_x"]) == 1000.0
    assert float(rows[0]["force_y"]) == 2000.0


def test_lbf_to_n(client: TestClient, normalized_root: Path) -> None:
    response = _run_to_normalized(client, FT_CSV, {"force": "lbf", "torque": "N*m"})
    rows = _read_normalized_rows(normalized_root, response)
    assert abs(float(rows[0]["force_x"]) - 4.4482216152605) < 1e-9


def test_newton_meter_identity(client: TestClient, normalized_root: Path) -> None:
    response = _run_to_normalized(client, FT_CSV, {"force": "N", "torque": "N·m"})
    rows = _read_normalized_rows(normalized_root, response)
    assert float(rows[0]["torque_x"]) == 0.1


def test_millinewton_meter_to_newton_meter(client: TestClient, normalized_root: Path) -> None:
    response = _run_to_normalized(client, FT_CSV, {"force": "N", "torque": "mN*m"})
    rows = _read_normalized_rows(normalized_root, response)
    assert abs(float(rows[0]["torque_x"]) - 0.0001) < 1e-12


def test_lbf_ft_to_newton_meter(client: TestClient, normalized_root: Path) -> None:
    response = _run_to_normalized(client, FT_CSV, {"force": "N", "torque": "lbf*ft"})
    rows = _read_normalized_rows(normalized_root, response)
    expected = 0.1 * 4.4482216152605 * 0.3048
    assert abs(float(rows[0]["torque_x"]) - expected) < 1e-9


def test_timestamp_normalized_to_utc_z(client: TestClient, normalized_root: Path) -> None:
    response = _run_to_normalized(client, FT_CSV, {"force": "N", "torque": "N*m"})
    rows = _read_normalized_rows(normalized_root, response)
    assert rows[0]["timestamp"] == "2026-08-30T18:00:00.500000Z"


def test_alias_mapping_fx_fy_fz_tx_ty_tz() -> None:
    """Aliases are a normalization-record concept, exercised at the
    RecordNormalizer level directly -- exactly how the existing IMU alias
    tests work (see tests/test_normalization_imu.py), since a raw
    uploaded file must already use canonical field names to pass Step 2
    schema validation in the first place; aliases resolve differently
    *named* raw fields once you're already operating on parsed records
    (e.g. a source whose exporter used non-canonical names)."""
    from app.sensors.force_torque.normalization import FORCE_TORQUE_CANONICAL_V1
    from app.normalization.profiles.base import RecordNormalizer
    from app.validation.schemas.registry import SchemaRegistry

    schema = SchemaRegistry(_default_schema_dir()).get(
        schema_name="force_torque", schema_version="1.0.0"
    )
    normalizer = RecordNormalizer(schema=schema, profile=FORCE_TORQUE_CANONICAL_V1, source_units={"force": "N", "torque": "N*m"})
    raw_record = {"timestamp": "2026-08-30T18:00:00Z", "fx": "1.0", "fy": "2.0", "fz": "3.0", "tx": "0.1", "ty": "0.2", "tz": "0.3"}
    result = normalizer.normalize_record(1, raw_record)
    assert result["force_x"] == 1.0
    assert result["torque_z"] == 0.3


def test_alias_collision_fails_loudly() -> None:
    """Both an alias AND its canonical name present in the same raw
    record is an unresolvable ambiguity -- must fail loudly, never
    silently pick one (mirrors IMU's existing
    test_ambiguous_alias_mapping_fails)."""
    from app.normalization.profiles.base import AmbiguousFieldMappingError, RecordNormalizer
    from app.sensors.force_torque.normalization import FORCE_TORQUE_CANONICAL_V1
    from app.validation.schemas.registry import SchemaRegistry
    import pytest

    schema = SchemaRegistry(_default_schema_dir()).get(
        schema_name="force_torque", schema_version="1.0.0"
    )
    normalizer = RecordNormalizer(schema=schema, profile=FORCE_TORQUE_CANONICAL_V1, source_units={"force": "N", "torque": "N*m"})
    raw_record = {
        "timestamp": "2026-08-30T18:00:00Z",
        "force_x": "1.0", "fx": "1.0",  # both map to force_x
        "force_y": "2.0", "force_z": "3.0", "torque_x": "0.1", "torque_y": "0.2", "torque_z": "0.3",
    }
    with pytest.raises(AmbiguousFieldMappingError):
        normalizer.normalize_record(1, raw_record)


def test_deterministic_normalization(client: TestClient, normalized_root: Path) -> None:
    response_a = _run_to_normalized(client, FT_CSV, {"force": "kN", "torque": "lbf*ft"})
    rows_a = _read_normalized_rows(normalized_root, response_a)

    response_b = _run_to_normalized(client, FT_CSV, {"force": "kN", "torque": "lbf*ft"})
    rows_b = _read_normalized_rows(normalized_root, response_b)

    # Content is identical across two independent runs even though
    # ingestion_id/normalization_id differ.
    assert rows_a[0]["force_x"] == rows_b[0]["force_x"]
    assert rows_a[0]["torque_x"] == rows_b[0]["torque_x"]
    assert response_a["normalized_sha256"] == response_b["normalized_sha256"]


def test_explicit_source_units_required(client: TestClient) -> None:
    ing = _upload(client, "ft.csv", FT_CSV.encode())
    for path in (f"/api/v1/validation/{ing['ingestion_id']}", f"/api/v1/integrity/{ing['ingestion_id']}"):
        r = client.post(path, json={"schema_name": "force_torque", "schema_version": "1.0.0"})
        assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/normalization/{ing['ingestion_id']}",
        json={
            "schema_name": "force_torque", "schema_version": "1.0.0",
            "profile_name": "force_torque_canonical", "profile_version": "1.0.0",
            "source_units": {},  # force/torque units never inferred
        },
    )
    assert r.status_code >= 400
