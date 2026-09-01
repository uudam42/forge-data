"""Load test: count-window transformation memory tracks window size, not
total row count. Opt-in only -- run with `pytest -m load`.
"""

from __future__ import annotations

import pytest

from app.transformation.windowing import iter_count_windows
from tests.load.memory_utils import format_bytes, measure_peak_rss


def _row_stream(num_rows: int):
    for i in range(num_rows):
        yield (i, i * 1000, {"accel_x": 0.1, "accel_y": 0.2, "accel_z": 9.8, "gyro_x": 0.01, "gyro_y": 0.02, "gyro_z": 0.03})


def _run_windowing(num_rows: int, window_size: int, stride: int) -> dict:
    window_count = 0
    for _ in iter_count_windows(_row_stream(num_rows), size=window_size, stride=stride, drop_incomplete=True):
        window_count += 1
    return {"window_count": window_count}


@pytest.mark.load
def test_large_overlapping_window_transformation_completes(tmp_path) -> None:
    run = measure_peak_rss(_run_windowing, 1_000_000, 50, 25, timeout=600)  # stride < size: overlapping
    expected_windows = (1_000_000 - 50) // 25 + 1
    assert run.result["window_count"] == expected_windows
    print(f"\n1M-row overlapping-window transformation: {run.wall_seconds:.1f}s, peak RSS {format_bytes(run.peak_rss_bytes)}")


@pytest.mark.load
@pytest.mark.parametrize("stride_relation", ["stride_lt_size", "stride_eq_size", "stride_gt_size"])
def test_windowing_memory_tracks_window_size_not_total_rows(stride_relation: str) -> None:
    window_size = 20
    stride = {"stride_lt_size": 10, "stride_eq_size": 20, "stride_gt_size": 40}[stride_relation]

    small_run = measure_peak_rss(_run_windowing, 100_000, window_size, stride, timeout=300)
    large_run = measure_peak_rss(_run_windowing, 1_000_000, window_size, stride, timeout=300)

    print(
        f"\nwindowing ({stride_relation}) 100k rows: {format_bytes(small_run.peak_rss_bytes)}, "
        f"1M rows: {format_bytes(large_run.peak_rss_bytes)}"
    )
    # A 10x row-count increase, same window_size/stride, must not come
    # close to a proportional memory increase -- generous threshold.
    assert large_run.peak_rss_bytes <= small_run.peak_rss_bytes * 1.5 + 30 * 1024 * 1024


@pytest.mark.load
def test_windowing_memory_scales_with_window_size(tmp_path) -> None:
    small_window_run = measure_peak_rss(_run_windowing, 500_000, 10, 10, timeout=300)
    large_window_run = measure_peak_rss(_run_windowing, 500_000, 5000, 5000, timeout=300)

    print(
        f"\nwindowing size=10: {format_bytes(small_window_run.peak_rss_bytes)}, "
        f"size=5000: {format_bytes(large_window_run.peak_rss_bytes)}"
    )
    # Larger window size IS expected to cost somewhat more (O(window_size)
    # is the documented model) -- this just sanity-checks that relationship
    # is not wildly disproportionate to the 500x window-size increase.
    assert large_window_run.peak_rss_bytes <= small_window_run.peak_rss_bytes * 3 + 30 * 1024 * 1024
