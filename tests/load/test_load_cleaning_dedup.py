"""Load test: the sqlite-backed exact-dedup index keeps memory flat where
the default in-memory backend must grow with the number of unique rows.
Opt-in only -- run with `pytest -m load`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.cleaning.rules.base import RuleContext
from app.cleaning.rules.duplicates import DuplicateRowRule
from tests.load.memory_utils import format_bytes, measure_peak_rss


def _run_dedup(backend: str, temp_dir_str: str | None, num_unique_rows: int) -> dict:
    temp_dir = Path(temp_dir_str) if temp_dir_str else None
    rule = DuplicateRowRule(backend=backend, temp_dir=temp_dir)
    try:
        duplicates_found = 0
        for i in range(num_unique_rows):
            row = {"timestamp": f"2026-08-30T18:00:{i:05d}Z", "streams": {"imu": {"accel_x": i}}}
            outcome = rule.evaluate(row, context=RuleContext(row_index=i))
            if outcome.should_drop:
                duplicates_found += 1
        # Re-evaluate the first row -- must now be detected as a duplicate,
        # proving the index actually holds all num_unique_rows entries.
        repeat_outcome = rule.evaluate(
            {"timestamp": "2026-08-30T18:00:00000Z", "streams": {"imu": {"accel_x": 0}}},
            context=RuleContext(row_index=num_unique_rows),
        )
        return {"duplicates_found": duplicates_found, "repeat_detected": repeat_outcome.should_drop}
    finally:
        rule.close()


@pytest.mark.load
def test_sqlite_dedup_backend_memory_stays_flat_where_memory_backend_grows(tmp_path: Path) -> None:
    small_n, large_n = 50_000, 1_000_000

    sqlite_small_dir = tmp_path / "sqlite_small"
    sqlite_large_dir = tmp_path / "sqlite_large"
    sqlite_small_dir.mkdir()
    sqlite_large_dir.mkdir()

    memory_small = measure_peak_rss(_run_dedup, "memory", None, small_n, timeout=600)
    memory_large = measure_peak_rss(_run_dedup, "memory", None, large_n, timeout=600)
    sqlite_small = measure_peak_rss(_run_dedup, "sqlite", str(sqlite_small_dir), small_n, timeout=600)
    sqlite_large = measure_peak_rss(_run_dedup, "sqlite", str(sqlite_large_dir), large_n, timeout=600)

    for run in (memory_small, memory_large, sqlite_small, sqlite_large):
        assert run.result["repeat_detected"] is True

    print(
        f"\ndedup memory backend: {small_n:,} rows = {format_bytes(memory_small.peak_rss_bytes)}, "
        f"{large_n:,} rows = {format_bytes(memory_large.peak_rss_bytes)}"
    )
    print(
        f"dedup sqlite backend: {small_n:,} rows = {format_bytes(sqlite_small.peak_rss_bytes)}, "
        f"{large_n:,} rows = {format_bytes(sqlite_large.peak_rss_bytes)}"
    )

    memory_growth = memory_large.peak_rss_bytes - memory_small.peak_rss_bytes
    sqlite_growth = sqlite_large.peak_rss_bytes - sqlite_small.peak_rss_bytes

    # The in-memory backend is EXPECTED to grow substantially (this proves
    # the documented O(unique_rows) limitation is real, not hypothetical).
    assert memory_growth > 20 * 1024 * 1024
    # The sqlite backend's growth over the same 20x row-count increase
    # must be a small fraction of the in-memory backend's growth.
    assert sqlite_growth < memory_growth * 0.3
