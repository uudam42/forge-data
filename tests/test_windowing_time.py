"""Unit tests for time-based windowing (app.transformation.windowing).

Interval semantics are [start, end) except the final window(s) closed only
because the input stream ended — see iter_time_windows' docstring.
"""

from __future__ import annotations

import pytest

from app.transformation.windowing import (
    InvalidWindowConfigurationError,
    NonMonotonicRowOrderError,
    iter_time_windows,
)


def _rows_at(*epoch_us_values: int) -> list[tuple[int, int, dict]]:
    return [(i, us, {"i": i}) for i, us in enumerate(epoch_us_values)]


def _rows_every(n: int, step_us: int, start_us: int = 0) -> list[tuple[int, int, dict]]:
    return [(i, start_us + i * step_us, {"i": i}) for i in range(n)]


def test_non_overlapping_windows_duration_equals_stride() -> None:
    # 1s apart, 0..9s; duration=stride=3000ms -> windows [0,3),[3,6),[6,9)
    rows = _rows_every(10, 1_000_000)
    windows = list(iter_time_windows(rows, duration_us=3_000_000, stride_us=3_000_000, drop_incomplete=True))
    assert [w[0] for w in windows] == [0, 1, 2]
    assert [r[0] for r in windows[0][3]] == [0, 1, 2]
    assert [r[0] for r in windows[1][3]] == [3, 4, 5]
    assert [r[0] for r in windows[2][3]] == [6, 7, 8]


def test_overlapping_windows_duration_gt_stride() -> None:
    # duration=1000ms, stride=500ms, rows every 100ms for 2s (20 rows, t=0..1900ms)
    rows = _rows_every(20, 100_000)
    windows = list(iter_time_windows(rows, duration_us=1_000_000, stride_us=500_000, drop_incomplete=True))
    window0 = next(w for w in windows if w[0] == 0)
    window1 = next(w for w in windows if w[0] == 1)
    # window0 [0,1000) -> t=0..900 (10 rows)
    assert [r[0] for r in window0[3]] == list(range(10))
    # window1 [500,1500) -> t=500..1400 (10 rows)
    assert [r[0] for r in window1[3]] == list(range(5, 15))


def test_half_open_interval_excludes_end_boundary() -> None:
    # duration=1000ms: a row exactly at t=1000 belongs to window1 [1000,2000), not window0 [0,1000)
    rows = _rows_at(0, 500_000, 1_000_000, 1_500_000)
    windows = list(iter_time_windows(rows, duration_us=1_000_000, stride_us=1_000_000, drop_incomplete=False))
    window0 = next(w for w in windows if w[0] == 0)
    window1 = next(w for w in windows if w[0] == 1)
    assert [r[0] for r in window0[3]] == [0, 1]
    assert [r[0] for r in window1[3]] == [2, 3]


def test_gapped_windows_stride_gt_duration() -> None:
    # duration=500ms, stride=1000ms -> window0 [0,500) rows at t=0,100,200,300,400; gap [500,1000) not covered
    rows = _rows_every(20, 100_000)
    windows = list(iter_time_windows(rows, duration_us=500_000, stride_us=1_000_000, drop_incomplete=True))
    window0 = next(w for w in windows if w[0] == 0)
    assert [r[0] for r in window0[3]] == [0, 1, 2, 3, 4]
    window1 = next(w for w in windows if w[0] == 1)
    # window1 starts at rel=1000ms -> t=1000,1100,...,1400
    assert [r[0] for r in window1[3]] == [10, 11, 12, 13, 14]


def test_drop_incomplete_true_omits_trailing_naturally_bounded_window() -> None:
    # duration=1000ms stride=1000ms over t=0..2400ms (3 full seconds of coverage impossible):
    # window0 [0,1000) t=0..900 complete since a row at t=1000 closes it;
    # window1 [1000,2000) t=1000..1900 complete since a row at t=2000 would close it, but stream ends at 2400
    # -> window1 IS closed by row at 2000? Let's use rows every 100ms for 2500ms (26 rows, t=0..2500)
    rows = _rows_every(26, 100_000)
    with_drop = list(iter_time_windows(rows, duration_us=1_000_000, stride_us=1_000_000, drop_incomplete=True))
    without_drop = list(iter_time_windows(rows, duration_us=1_000_000, stride_us=1_000_000, drop_incomplete=False))
    assert len(without_drop) == len(with_drop) + 1
    trailing_idx = without_drop[-1][0]
    assert trailing_idx not in [w[0] for w in with_drop]


def test_drop_incomplete_false_includes_trailing_window() -> None:
    rows = _rows_every(15, 100_000)  # t=0..1400ms
    windows = list(iter_time_windows(rows, duration_us=1_000_000, stride_us=1_000_000, drop_incomplete=False))
    last = windows[-1]
    assert last[3][-1][0] == 14  # includes the very last row


def test_empty_input_yields_no_windows() -> None:
    assert list(iter_time_windows([], duration_us=1000, stride_us=1000, drop_incomplete=True)) == []
    assert list(iter_time_windows([], duration_us=1000, stride_us=1000, drop_incomplete=False)) == []


def test_window_bounds_are_anchored_to_first_row_not_epoch_zero() -> None:
    rows = _rows_every(5, 1_000_000, start_us=10_000_000_000)
    windows = list(iter_time_windows(rows, duration_us=2_000_000, stride_us=2_000_000, drop_incomplete=False))
    assert windows[0][1] == 10_000_000_000


def test_duration_le_zero_rejected() -> None:
    with pytest.raises(InvalidWindowConfigurationError):
        list(iter_time_windows(_rows_every(5, 1000), duration_us=0, stride_us=1000, drop_incomplete=True))
    with pytest.raises(InvalidWindowConfigurationError):
        list(iter_time_windows(_rows_every(5, 1000), duration_us=-1, stride_us=1000, drop_incomplete=True))


def test_stride_le_zero_rejected() -> None:
    with pytest.raises(InvalidWindowConfigurationError):
        list(iter_time_windows(_rows_every(5, 1000), duration_us=1000, stride_us=0, drop_incomplete=True))


def test_non_monotonic_timestamps_raise() -> None:
    rows = [(0, 1000, {}), (1, 500, {}), (2, 2000, {})]
    with pytest.raises(NonMonotonicRowOrderError):
        list(iter_time_windows(rows, duration_us=1000, stride_us=1000, drop_incomplete=True))


def test_single_row_produces_one_window_when_not_dropped() -> None:
    rows = _rows_at(0)
    windows = list(iter_time_windows(rows, duration_us=1000, stride_us=1000, drop_incomplete=False))
    assert len(windows) == 1
    assert [r[0] for r in windows[0][3]] == [0]
