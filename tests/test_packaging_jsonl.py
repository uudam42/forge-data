"""Unit tests for JSONL export and canonical serialization
(app.packaging.exporters.jsonl, app.packaging.serialization)."""

from __future__ import annotations

import json
import math

import pytest

from app.packaging.exporters.jsonl import JSONLExporter
from app.packaging.serialization import canonical_json

EXPORTER = JSONLExporter()


def test_serialize_sample_produces_valid_json_line() -> None:
    sample = {"sample_id": "s0", "features": {"imu": {"statistics": {"x": 1.0}}}}
    line = EXPORTER.serialize_sample(sample)
    assert line.endswith(b"\n")
    parsed = json.loads(line.decode("utf-8"))
    assert parsed == sample


def test_canonical_serialization_sorted_keys_compact() -> None:
    sample = {"b": 2, "a": 1}
    line = EXPORTER.serialize_sample(sample).decode("utf-8")
    assert ": " not in line
    assert ", " not in line
    assert line.index('"a"') < line.index('"b"')


def test_allow_nan_false_enforced() -> None:
    sample = {"value": float("nan")}
    with pytest.raises(ValueError):
        EXPORTER.serialize_sample(sample)


def test_infinity_rejected() -> None:
    sample = {"value": math.inf}
    with pytest.raises(ValueError):
        EXPORTER.serialize_sample(sample)


def test_numerical_values_unchanged() -> None:
    sample = {"a": 0.123456789012345, "b": -3, "c": 0.0}
    line = json.loads(EXPORTER.serialize_sample(sample).decode("utf-8"))
    assert line == sample


def test_timestamps_unchanged() -> None:
    sample = {"window": {"start_timestamp": "2026-08-30T18:00:00Z", "end_timestamp": "2026-08-30T18:00:09Z"}}
    line = json.loads(EXPORTER.serialize_sample(sample).decode("utf-8"))
    assert line == sample


def test_sample_ids_unchanged() -> None:
    sample = {"sample_id": "sample_deadbeefdeadbeefdeadbeefdeadbeef"}
    line = json.loads(EXPORTER.serialize_sample(sample).decode("utf-8"))
    assert line["sample_id"] == sample["sample_id"]


def test_raw_arrays_unchanged() -> None:
    sample = {"features": {"imu": {"raw": {"accel_x": [0.1, 0.2, 0.3]}}}}
    line = json.loads(EXPORTER.serialize_sample(sample).decode("utf-8"))
    assert line["features"]["imu"]["raw"]["accel_x"] == [0.1, 0.2, 0.3]


def test_no_fields_removed_or_added() -> None:
    sample = {
        "sample_id": "s0",
        "window": {"index": 0},
        "features": {"imu": {"statistics": {"x": 1.0}}},
        "modality_mask": {"imu": True},
        "modality_coverage": {"imu": 1.0},
        "metadata": {"source_row_start": 0, "source_row_end": 9},
    }
    line = json.loads(EXPORTER.serialize_sample(sample).decode("utf-8"))
    assert set(line.keys()) == set(sample.keys())
    assert line == sample


def test_nested_array_order_not_reordered() -> None:
    sample = {"features": {"imu": {"raw": {"accel_x": [3.0, 1.0, 2.0]}}}}
    line = json.loads(EXPORTER.serialize_sample(sample).decode("utf-8"))
    assert line["features"]["imu"]["raw"]["accel_x"] == [3.0, 1.0, 2.0]


def test_canonical_json_helper_matches_exporter() -> None:
    sample = {"a": 1}
    assert EXPORTER.serialize_sample(sample) == (canonical_json(sample) + "\n").encode("utf-8")
