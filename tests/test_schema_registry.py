"""Tests for SchemaRegistry: loading, lookup, and duplicate rejection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.validation.schemas.registry import DuplicateSchemaError, SchemaNotFoundError, SchemaRegistry

REAL_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


def test_registry_loads_builtin_imu_schema() -> None:
    registry = SchemaRegistry(schema_dir=REAL_SCHEMA_DIR)
    schema = registry.get(schema_name="imu", schema_version="1.0.0")

    assert schema.schema_name == "imu"
    assert schema.fields["timestamp"].required is True
    assert schema.fields["accel_x"].type.value == "float"
    assert schema.fields["device_id"].required is False


def test_registry_loads_builtin_gps_schema() -> None:
    registry = SchemaRegistry(schema_dir=REAL_SCHEMA_DIR)
    schema = registry.get(schema_name="gps", schema_version="1.0.0")

    assert schema.schema_name == "gps"
    assert "latitude" in schema.fields
    assert "longitude" in schema.fields


def test_registry_lists_available_schemas() -> None:
    registry = SchemaRegistry(schema_dir=REAL_SCHEMA_DIR)
    names = registry.list_schemas()

    assert ("gps", "1.0.0") in names
    assert ("imu", "1.0.0") in names


def test_unknown_schema_raises_not_found() -> None:
    registry = SchemaRegistry(schema_dir=REAL_SCHEMA_DIR)

    with pytest.raises(SchemaNotFoundError):
        registry.get(schema_name="does_not_exist", schema_version="9.9.9")


def test_known_schema_name_but_unknown_version_raises_not_found() -> None:
    registry = SchemaRegistry(schema_dir=REAL_SCHEMA_DIR)

    with pytest.raises(SchemaNotFoundError):
        registry.get(schema_name="imu", schema_version="9.9.9")


def test_duplicate_schema_definitions_are_rejected(tmp_path: Path) -> None:
    schema = {
        "schema_name": "dup",
        "schema_version": "1.0.0",
        "fields": {"a": {"type": "string", "required": True}},
    }
    (tmp_path / "dup_a.json").write_text(json.dumps(schema))
    (tmp_path / "dup_b.json").write_text(json.dumps(schema))

    with pytest.raises(DuplicateSchemaError):
        SchemaRegistry(schema_dir=tmp_path)


def test_missing_schema_dir_yields_no_schemas(tmp_path: Path) -> None:
    registry = SchemaRegistry(schema_dir=tmp_path / "does_not_exist")
    assert registry.list_schemas() == []
