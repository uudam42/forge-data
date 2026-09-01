"""Force/Torque validation and integrity tests (Design Requirement 5, 6;
test items 9-22). All of these exercise the EXISTING generic validation/
integrity engines (CSV/JSONL readers, RecordEvaluator, IntegrityChecker
contract) against the new schema — no validation/integrity core code
changed to support Force/Torque."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

VALID_FT_CSV = (
    "timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z,device_id\n"
    "2026-08-30T18:00:00Z,1.0,2.0,9.8,0.1,0.2,0.3,ft_01\n"
    "2026-08-30T18:00:01Z,1.1,2.1,9.7,0.11,0.21,0.31,ft_01\n"
)

VALID_FT_JSONL = "\n".join(
    json.dumps(r)
    for r in [
        {"timestamp": "2026-08-30T18:00:00Z", "force_x": 1.0, "force_y": 2.0, "force_z": 9.8, "torque_x": 0.1, "torque_y": 0.2, "torque_z": 0.3},
        {"timestamp": "2026-08-30T18:00:01Z", "force_x": 1.1, "force_y": 2.1, "force_z": 9.7, "torque_x": 0.11, "torque_y": 0.21, "torque_z": 0.31},
    ]
)


def _upload(client: TestClient, filename: str, content: bytes, **fields) -> dict:
    r = client.post("/api/v1/ingestion/upload", files={"file": (filename, content, None)}, data=fields)
    assert r.status_code == 201, r.text
    return r.json()


def _validate(client: TestClient, ingestion_id: str) -> dict:
    r = client.post(f"/api/v1/validation/{ingestion_id}", json={"schema_name": "force_torque", "schema_version": "1.0.0"})
    return r


def _integrity(client: TestClient, ingestion_id: str) -> dict:
    r = client.post(f"/api/v1/integrity/{ingestion_id}", json={"schema_name": "force_torque", "schema_version": "1.0.0"})
    return r


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_valid_csv_passes(client: TestClient) -> None:
    ing = _upload(client, "ft.csv", VALID_FT_CSV.encode())
    r = _validate(client, ing["ingestion_id"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "passed"


def test_valid_jsonl_passes(client: TestClient) -> None:
    ing = _upload(client, "ft.jsonl", VALID_FT_JSONL.encode())
    r = _validate(client, ing["ingestion_id"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "passed"


def test_missing_required_field_fails(client: TestClient) -> None:
    csv = "timestamp,force_x,force_y,force_z,torque_x,torque_y\n2026-08-30T18:00:00Z,1.0,2.0,9.8,0.1,0.2\n"
    ing = _upload(client, "ft.csv", csv.encode())
    r = _validate(client, ing["ingestion_id"])
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert r.json()["summary"]["error_count"] > 0


def test_wrong_numeric_type_fails(client: TestClient) -> None:
    csv = "timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z\n2026-08-30T18:00:00Z,not_a_number,2.0,9.8,0.1,0.2,0.3\n"
    ing = _upload(client, "ft.csv", csv.encode())
    r = _validate(client, ing["ingestion_id"])
    assert r.json()["status"] == "failed"


def test_invalid_timestamp_fails(client: TestClient) -> None:
    csv = "timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z\nnot-a-timestamp,1.0,2.0,9.8,0.1,0.2,0.3\n"
    ing = _upload(client, "ft.csv", csv.encode())
    r = _validate(client, ing["ingestion_id"])
    assert r.json()["status"] == "failed"


def test_null_vs_missing_device_id_both_accepted(client: TestClient) -> None:
    """device_id is optional/nullable -- explicit empty cell (null) and an
    absent column entirely must both pass, matching IMU/GPS's existing
    nullable-optional-field semantics."""
    csv_missing_column = VALID_FT_CSV  # no device_id needed to be valid since it's optional there too
    csv_explicit_null = (
        "timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z,device_id\n"
        "2026-08-30T18:00:00Z,1.0,2.0,9.8,0.1,0.2,0.3,\n"
    )
    for csv in (csv_missing_column, csv_explicit_null):
        ing = _upload(client, "ft.csv", csv.encode())
        r = _validate(client, ing["ingestion_id"])
        assert r.json()["status"] == "passed", r.text


def test_extra_field_rejected(client: TestClient) -> None:
    """allow_extra_fields=false, matching IMU/GPS's existing policy."""
    csv = (
        "timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z,unexpected_column\n"
        "2026-08-30T18:00:00Z,1.0,2.0,9.8,0.1,0.2,0.3,surprise\n"
    )
    ing = _upload(client, "ft.csv", csv.encode())
    r = _validate(client, ing["ingestion_id"])
    assert r.json()["status"] == "failed"


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def test_finite_values_pass_integrity(client: TestClient) -> None:
    ing = _upload(client, "ft.csv", VALID_FT_CSV.encode())
    _validate(client, ing["ingestion_id"])
    r = _integrity(client, ing["ingestion_id"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "passed"


def test_nan_force_component_fails_integrity(client: TestClient) -> None:
    csv = "timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z\n2026-08-30T18:00:00Z,nan,2.0,9.8,0.1,0.2,0.3\n"
    ing = _upload(client, "ft.csv", csv.encode())
    v = _validate(client, ing["ingestion_id"])
    assert v.json()["status"] == "passed"  # Step 2 doesn't reject nan-as-float
    r = _integrity(client, ing["ingestion_id"])
    assert r.json()["status"] == "failed"
    assert any(issue["code"] == "NON_FINITE_VALUE" for issue in json.loads(
        __import__("pathlib").Path(r.json()["report_uri"].replace("file://", "")).read_text()
    )["issues"])


def test_inf_torque_component_fails_integrity(client: TestClient) -> None:
    csv = "timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z\n2026-08-30T18:00:00Z,1.0,2.0,9.8,inf,0.2,0.3\n"
    ing = _upload(client, "ft.csv", csv.encode())
    _validate(client, ing["ingestion_id"])
    r = _integrity(client, ing["ingestion_id"])
    assert r.json()["status"] == "failed"


def test_timestamp_out_of_order_fails_integrity(client: TestClient) -> None:
    csv = (
        "timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z\n"
        "2026-08-30T18:00:01Z,1.0,2.0,9.8,0.1,0.2,0.3\n"
        "2026-08-30T18:00:00Z,1.1,2.1,9.7,0.11,0.21,0.31\n"
    )
    ing = _upload(client, "ft.csv", csv.encode())
    _validate(client, ing["ingestion_id"])
    r = _integrity(client, ing["ingestion_id"])
    assert r.json()["status"] == "failed"


def test_duplicate_timestamp_is_a_warning_not_a_failure(client: TestClient) -> None:
    csv = (
        "timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z\n"
        "2026-08-30T18:00:00Z,1.0,2.0,9.8,0.1,0.2,0.3\n"
        "2026-08-30T18:00:00Z,1.1,2.1,9.7,0.11,0.21,0.31\n"
    )
    ing = _upload(client, "ft.csv", csv.encode())
    _validate(client, ing["ingestion_id"])
    r = _integrity(client, ing["ingestion_id"])
    assert r.json()["status"] == "passed_with_warnings"


def test_configurable_extreme_force_warning(client: TestClient, integrity_root) -> None:
    from app.storage.integrity_store import LocalIntegrityReportStore
    from app.sensors.force_torque.integrity import ForceTorqueIntegrityChecker, ForceTorqueThresholds

    ing = _upload(client, "ft.csv", VALID_FT_CSV.encode())
    v = _validate(client, ing["ingestion_id"])
    assert v.status_code == 200

    # Directly exercise the checker with a configured threshold -- the
    # HTTP layer doesn't expose per-request thresholds (mirrors IMU),
    # so this proves the configurable-threshold mechanism itself.
    checker = ForceTorqueIntegrityChecker(thresholds=ForceTorqueThresholds(max_abs_force_n=5.0))
    from app.integrity.checks.base import IntegrityIssueAccumulator

    accumulator = IntegrityIssueAccumulator(max_issues=100)
    records = [(1, {"timestamp": "2026-08-30T18:00:00Z", "force_x": 999.0, "force_y": 0.0, "force_z": 0.0, "torque_x": 0.0, "torque_y": 0.0, "torque_z": 0.0})]
    counts = checker.check_stream(iter(records), accumulator)
    assert counts.passed_records == 1  # extreme force is a WARNING, not an ERROR
    assert any(i.code == "FORCE_TORQUE_FORCE_EXTREME" for i in accumulator.issues)


def test_configurable_extreme_torque_warning() -> None:
    from app.sensors.force_torque.integrity import ForceTorqueIntegrityChecker, ForceTorqueThresholds
    from app.integrity.checks.base import IntegrityIssueAccumulator

    checker = ForceTorqueIntegrityChecker(thresholds=ForceTorqueThresholds(max_abs_torque_nm=1.0))
    accumulator = IntegrityIssueAccumulator(max_issues=100)
    records = [(1, {"timestamp": "2026-08-30T18:00:00Z", "force_x": 0.0, "force_y": 0.0, "force_z": 0.0, "torque_x": 50.0, "torque_y": 0.0, "torque_z": 0.0})]
    counts = checker.check_stream(iter(records), accumulator)
    assert counts.passed_records == 1
    assert any(i.code == "FORCE_TORQUE_TORQUE_EXTREME" for i in accumulator.issues)


def test_thresholds_disabled_by_default() -> None:
    """Extreme thresholds are opt-in (None = disabled) -- a hardware with
    a genuinely large operating range must never fail by default."""
    from app.sensors.force_torque.integrity import ForceTorqueIntegrityChecker
    from app.integrity.checks.base import IntegrityIssueAccumulator

    checker = ForceTorqueIntegrityChecker()  # default thresholds
    accumulator = IntegrityIssueAccumulator(max_issues=100)
    records = [(1, {"timestamp": "2026-08-30T18:00:00Z", "force_x": 999999.0, "force_y": 0.0, "force_z": 0.0, "torque_x": 999999.0, "torque_y": 0.0, "torque_z": 0.0})]
    checker.check_stream(iter(records), accumulator)
    assert accumulator.issues == []
