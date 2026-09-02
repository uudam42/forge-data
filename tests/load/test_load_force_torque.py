"""Load test: Force/Torque respects v2.2's resource contracts at
1,000,000-row scale -- validation, normalization, and transformation all
stream/stay bounded exactly like IMU/GPS. Opt-in only -- run with
`pytest -m load`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.sensors.force_torque.normalization import FORCE_TORQUE_CANONICAL_V1
from app.transformation.windowing import iter_count_windows
from app.validation.schemas.registry import SchemaRegistry
from app.validation.validators.base import ErrorAccumulator
from app.validation.validators.csv_validator import CsvValidator
from app.core.config import _default_schema_dir
from tests.load.memory_utils import format_bytes, measure_peak_rss

_SCHEMA_DIR = _default_schema_dir()
_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _generate_ft_csv(path: Path, num_rows: int) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write("timestamp,force_x,force_y,force_z,torque_x,torque_y,torque_z\n")
        for i in range(num_rows):
            ts = (_BASE_TIME + timedelta(milliseconds=i * 10)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            f.write(f"{ts},1.{i % 10},2.{i % 10},9.8,0.1{i % 10},0.2{i % 10},0.3{i % 10}\n")


def _validate_ft_csv(file_path_str: str) -> dict:
    schema = SchemaRegistry(_SCHEMA_DIR).get(schema_name="force_torque", schema_version="1.0.0")
    accumulator = ErrorAccumulator(max_errors=1000)
    with Path(file_path_str).open("rb") as f:
        counts = CsvValidator().validate(f, schema, accumulator)
    return {"records_checked": counts.records_checked, "valid_records": counts.valid_records}


def _normalize_ft_csv(file_path_str: str) -> dict:
    import csv as csv_module

    from app.normalization.profiles.base import RecordNormalizer

    schema = SchemaRegistry(_SCHEMA_DIR).get(schema_name="force_torque", schema_version="1.0.0")
    normalizer = RecordNormalizer(schema=schema, profile=FORCE_TORQUE_CANONICAL_V1, source_units={"force": "kN", "torque": "lbf*ft"})
    count = 0
    with Path(file_path_str).open("r", encoding="utf-8", newline="") as f:
        for record_number, row in enumerate(csv_module.DictReader(f), start=1):
            normalizer.normalize_record(record_number, row)
            count += 1
    return {"records_normalized": count}


def _row_stream(num_rows: int):
    for i in range(num_rows):
        yield (i, i * 10_000, {"force_x": 1.0, "force_y": 2.0, "force_z": 9.8, "torque_x": 0.1, "torque_y": 0.2, "torque_z": 0.3})


def _transform_force_torque(num_rows: int, window_size: int, stride: int) -> dict:
    window_count = 0
    for _ in iter_count_windows(_row_stream(num_rows), size=window_size, stride=stride, drop_incomplete=True):
        window_count += 1
    return {"window_count": window_count}


@pytest.mark.load
def test_1m_row_force_torque_validation(tmp_path: Path) -> None:
    big_file = tmp_path / "ft_1m.csv"
    _generate_ft_csv(big_file, 1_000_000)
    run = measure_peak_rss(_validate_ft_csv, str(big_file), timeout=600)
    assert run.result["records_checked"] == 1_000_000
    assert run.result["valid_records"] == 1_000_000
    print(f"\n1M-row Force/Torque validation: {run.wall_seconds:.1f}s, peak RSS {format_bytes(run.peak_rss_bytes)}")


@pytest.mark.load
def test_1m_row_force_torque_normalization(tmp_path: Path) -> None:
    big_file = tmp_path / "ft_1m.csv"
    _generate_ft_csv(big_file, 1_000_000)
    run = measure_peak_rss(_normalize_ft_csv, str(big_file), timeout=600)
    assert run.result["records_normalized"] == 1_000_000
    print(f"\n1M-row Force/Torque normalization (kN->N, lbf*ft->N*m): {run.wall_seconds:.1f}s, peak RSS {format_bytes(run.peak_rss_bytes)}")


@pytest.mark.load
def test_1m_row_force_torque_transformation_bounded_memory() -> None:
    small_run = measure_peak_rss(_transform_force_torque, 100_000, 20, 20, timeout=300)
    large_run = measure_peak_rss(_transform_force_torque, 1_000_000, 20, 20, timeout=300)
    print(
        f"\nForce/Torque windowing 100k rows: {format_bytes(small_run.peak_rss_bytes)}, "
        f"1M rows: {format_bytes(large_run.peak_rss_bytes)}"
    )
    assert large_run.peak_rss_bytes <= small_run.peak_rss_bytes * 1.5 + 30 * 1024 * 1024
