"""Load tests: large CSV validation stays O(1) + bounded-issue-storage,
never O(dataset). Opt-in only -- run with `pytest -m load`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.validation.schemas.registry import SchemaRegistry
from app.validation.validators.base import ErrorAccumulator
from app.validation.validators.csv_validator import CsvValidator
from app.core.config import _default_schema_dir
from tests.load.memory_utils import format_bytes, measure_peak_rss

_SCHEMA_DIR = _default_schema_dir()
_BASE_TIME = datetime(2026, 8, 30, 0, 0, 0, tzinfo=timezone.utc)


def _generate_imu_csv(path: Path, num_rows: int, *, all_invalid: bool = False) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write("timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n")
        accel_x = "NOT_A_NUMBER" if all_invalid else "0.1"
        for i in range(num_rows):
            ts = (_BASE_TIME + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(f"{ts},{accel_x},0.2,9.8,0.01,0.02,0.03\n")


def _validate_csv_file(file_path_str: str, max_errors: int) -> dict:
    path = Path(file_path_str)
    registry = SchemaRegistry(_SCHEMA_DIR)
    schema = registry.get(schema_name="imu", schema_version="1.0.0")
    accumulator = ErrorAccumulator(max_errors=max_errors)
    with path.open("rb") as f:
        counts = CsvValidator().validate(f, schema, accumulator)
    return {
        "records_checked": counts.records_checked,
        "valid_records": counts.valid_records,
        "invalid_records": counts.invalid_records,
        "error_count": accumulator.error_count,
        "errors_truncated": accumulator.errors_truncated,
        "errors_stored": len(accumulator.errors),
    }


@pytest.mark.load
def test_1m_row_csv_validation_completes(tmp_path: Path) -> None:
    big_file = tmp_path / "imu_1m.csv"
    _generate_imu_csv(big_file, 1_000_000)
    run = measure_peak_rss(_validate_csv_file, str(big_file), 1000, timeout=600)
    assert run.result["records_checked"] == 1_000_000
    assert run.result["valid_records"] == 1_000_000
    print(f"\n1M-row CSV validation: {run.wall_seconds:.1f}s, peak RSS {format_bytes(run.peak_rss_bytes)}")


@pytest.mark.load
def test_bounded_issue_accumulation_with_a_million_invalid_rows(tmp_path: Path) -> None:
    """A million invalid rows must not produce a million in-memory issue
    objects -- error_count stays exact, but stored `errors` respects the
    configured cap and errors_truncated is surfaced honestly."""
    big_file = tmp_path / "imu_1m_invalid.csv"
    _generate_imu_csv(big_file, 1_000_000, all_invalid=True)
    max_errors = 500
    run = measure_peak_rss(_validate_csv_file, str(big_file), max_errors, timeout=600)
    result = run.result
    assert result["records_checked"] == 1_000_000
    assert result["invalid_records"] == 1_000_000
    assert result["error_count"] == 1_000_000  # exact count, always
    assert result["errors_stored"] == max_errors  # never more than the cap
    assert result["errors_truncated"] is True


@pytest.mark.load
def test_validation_peak_memory_growth_is_bounded_across_sizes(tmp_path: Path) -> None:
    sizes = [100_000, 500_000, 1_000_000]
    peaks: dict[int, int] = {}
    for n in sizes:
        f = tmp_path / f"imu_{n}.csv"
        _generate_imu_csv(f, n)
        run = measure_peak_rss(_validate_csv_file, str(f), 1000, timeout=600)
        peaks[n] = run.peak_rss_bytes
        f.unlink()

    print("\npeak RSS by row count:", {n: format_bytes(b) for n, b in peaks.items()})
    # Generous, data-driven regression bound: a 10x row-count increase
    # must not come close to a proportional memory increase for a
    # genuinely O(1)+bounded-issues stage.
    assert peaks[1_000_000] <= peaks[100_000] * 1.5 + 50 * 1024 * 1024
