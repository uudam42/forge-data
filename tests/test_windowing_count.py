"""Unit tests for count-based sliding windowing (app.transformation.windowing)."""

from __future__ import annotations

import pytest

from app.transformation.windowing import (
    InvalidWindowConfigurationError,
    NonMonotonicRowOrderError,
    iter_count_windows,
)


def _rows(n: int, *, step_us: int = 1_000_000) -> list[tuple[int, int, dict]]:
    return [(i, i * step_us, {"i": i}) for i in range(n)]


def test_non_overlapping_windows_size_equals_stride() -> None:
    windows = list(iter_count_windows(_rows(20), size=10, stride=10, drop_incomplete=True))
    assert len(windows) == 2
    assert [r[0] for r in windows[0][3]] == list(range(0, 10))
    assert [r[0] for r in windows[1][3]] == list(range(10, 20))


def test_overlapping_windows_size_gt_stride() -> None:
    # size=20, stride=10 -> window0 rows 0-19, window1 rows 10-29, window2 rows 20-39
    windows = list(iter_count_windows(_rows(40), size=20, stride=10, drop_incomplete=True))
    assert len(windows) == 3
    assert [r[0] for r in windows[0][3]] == list(range(0, 20))
    assert [r[0] for r in windows[1][3]] == list(range(10, 30))
    assert [r[0] for r in windows[2][3]] == list(range(20, 40))


def test_window_indices_are_sequential() -> None:
    windows = list(iter_count_windows(_rows(30), size=10, stride=10, drop_incomplete=True))
    assert [w[0] for w in windows] == [0, 1, 2]


def test_gapped_windows_stride_gt_size() -> None:
    # size=5, stride=10 -> window0 rows0-4, then rows5-9 skipped, window1 rows10-14
    windows = list(iter_count_windows(_rows(20), size=5, stride=10, drop_incomplete=True))
    assert len(windows) == 2
    assert [r[0] for r in windows[0][3]] == [0, 1, 2, 3, 4]
    assert [r[0] for r in windows[1][3]] == [10, 11, 12, 13, 14]


def test_drop_incomplete_true_omits_final_partial_window() -> None:
    windows = list(iter_count_windows(_rows(25), size=10, stride=10, drop_incomplete=True))
    assert len(windows) == 2  # rows 20-24 (5 rows) never reach size=10, dropped


def test_drop_incomplete_false_emits_final_partial_window() -> None:
    windows = list(iter_count_windows(_rows(25), size=10, stride=10, drop_incomplete=False))
    assert len(windows) == 3
    assert [r[0] for r in windows[2][3]] == [20, 21, 22, 23, 24]


def test_drop_incomplete_false_with_overlap_emits_final_partial_window() -> None:
    # size=20 stride=10 over 35 rows: window0 0-19, window1 10-29, leftover 20-34 (15 rows)
    windows = list(iter_count_windows(_rows(35), size=20, stride=10, drop_incomplete=False))
    assert len(windows) == 3
    assert [r[0] for r in windows[2][3]] == list(range(20, 35))


def test_dataset_smaller_than_size_drop_incomplete_true_yields_nothing() -> None:
    windows = list(iter_count_windows(_rows(5), size=10, stride=10, drop_incomplete=True))
    assert windows == []


def test_dataset_smaller_than_size_drop_incomplete_false_yields_one_partial_window() -> None:
    windows = list(iter_count_windows(_rows(5), size=10, stride=10, drop_incomplete=False))
    assert len(windows) == 1
    assert [r[0] for r in windows[0][3]] == [0, 1, 2, 3, 4]


def test_empty_input_yields_no_windows_either_way() -> None:
    assert list(iter_count_windows(_rows(0), size=10, stride=10, drop_incomplete=True)) == []
    assert list(iter_count_windows(_rows(0), size=10, stride=10, drop_incomplete=False)) == []


def test_window_start_end_epoch_match_first_last_row() -> None:
    windows = list(iter_count_windows(_rows(10, step_us=500_000), size=10, stride=10, drop_incomplete=True))
    _, start, end, rows = windows[0]
    assert start == rows[0][1]
    assert end == rows[-1][1]


def test_size_le_zero_rejected() -> None:
    with pytest.raises(InvalidWindowConfigurationError):
        list(iter_count_windows(_rows(10), size=0, stride=5, drop_incomplete=True))
    with pytest.raises(InvalidWindowConfigurationError):
        list(iter_count_windows(_rows(10), size=-1, stride=5, drop_incomplete=True))


def test_stride_le_zero_rejected() -> None:
    with pytest.raises(InvalidWindowConfigurationError):
        list(iter_count_windows(_rows(10), size=5, stride=0, drop_incomplete=True))
    with pytest.raises(InvalidWindowConfigurationError):
        list(iter_count_windows(_rows(10), size=5, stride=-3, drop_incomplete=True))


def test_non_monotonic_timestamps_raise() -> None:
    rows = [(0, 1000, {}), (1, 500, {}), (2, 2000, {})]
    with pytest.raises(NonMonotonicRowOrderError):
        list(iter_count_windows(rows, size=2, stride=2, drop_incomplete=True))


def test_memory_bounded_by_window_size_not_dataset_size() -> None:
    """A streaming generator should never materialize the whole dataset at
    once — proven indirectly by lazily consuming a huge row generator and
    confirming only the requested number of windows get realized."""

    def infinite_rows():
        i = 0
        while True:
            yield (i, i * 1_000_000, {"i": i})
            i += 1

    gen = iter_count_windows(infinite_rows(), size=10, stride=10, drop_incomplete=True)
    first_three = [next(gen) for _ in range(3)]
    assert [w[0] for w in first_three] == [0, 1, 2]


def test_row_objects_not_mutated() -> None:
    rows = _rows(10)
    original = [dict(r[2]) for r in rows]
    list(iter_count_windows(rows, size=5, stride=5, drop_incomplete=True))
    assert [r[2] for r in rows] == original
