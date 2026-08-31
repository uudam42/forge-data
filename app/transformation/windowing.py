"""Streaming, bounded-memory windowing over an ordered row stream.

Two modes:

- count: a classic overlapping sliding window over row COUNT. Window k
  covers rows [k*stride, k*stride + size). Memory is O(size) via a
  collections.deque, never O(dataset size) — rows are discarded from the
  buffer as soon as no in-flight window can still need them.

- time: windows are defined over the canonical timestamps already present
  in the cleaned rows (no resynchronization/interpolation). Window k covers
  the half-open interval [first_ts + k*stride_ms, first_ts + k*stride_ms +
  duration_ms). A row can belong to multiple overlapping windows at once.
  Memory is bounded by how many rows fall within one window's time span,
  not by a fixed row count and not by dataset size.

Both modes require monotonically non-decreasing input timestamps/row order
(guaranteed by every upstream stage) and raise NonMonotonicRowOrderError
otherwise, rather than silently reordering or producing wrong windows.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Iterator

RowItem = tuple[int, int, dict]  # (row_index, epoch_us, row)
WindowItem = tuple[int, int, int, list[RowItem]]  # (window_index, start_epoch_us, end_epoch_us, rows)


class InvalidWindowConfigurationError(Exception):
    pass


class NonMonotonicRowOrderError(Exception):
    pass


def iter_count_windows(
    rows: Iterable[RowItem], *, size: int, stride: int, drop_incomplete: bool
) -> Iterator[WindowItem]:
    if size <= 0:
        raise InvalidWindowConfigurationError("window.size must be > 0")
    if stride <= 0:
        raise InvalidWindowConfigurationError("window.stride must be > 0")

    buffer: deque[RowItem] = deque()
    skip_remaining = 0
    window_index = 0
    last_epoch_us: int | None = None

    for item in rows:
        row_index, epoch_us, _ = item
        if last_epoch_us is not None and epoch_us < last_epoch_us:
            raise NonMonotonicRowOrderError(
                f"Row {row_index} has timestamp epoch_us={epoch_us} which is earlier than "
                f"the previous row's epoch_us={last_epoch_us}"
            )
        last_epoch_us = epoch_us

        if skip_remaining > 0:
            skip_remaining -= 1
            continue

        buffer.append(item)
        if len(buffer) == size:
            window_rows = list(buffer)
            yield (
                window_index,
                window_rows[0][1],
                window_rows[-1][1],
                window_rows,
            )
            window_index += 1

            pop_count = min(stride, size)
            for _ in range(pop_count):
                buffer.popleft()
            if stride > size:
                skip_remaining = stride - size

    if buffer and not drop_incomplete:
        window_rows = list(buffer)
        yield (window_index, window_rows[0][1], window_rows[-1][1], window_rows)


def _windows_for_timestamp(rel_us: int, duration_us: int, stride_us: int) -> list[int]:
    highest_idx = rel_us // stride_us
    lowest_idx = max(0, (rel_us - duration_us) // stride_us)
    result = []
    for idx in range(int(lowest_idx), int(highest_idx) + 1):
        start = idx * stride_us
        end = start + duration_us
        if start <= rel_us < end:
            result.append(idx)
    return result


def iter_time_windows(
    rows: Iterable[RowItem], *, duration_us: int, stride_us: int, drop_incomplete: bool
) -> Iterator[WindowItem]:
    if duration_us <= 0:
        raise InvalidWindowConfigurationError("window.duration_ms must be > 0")
    if stride_us <= 0:
        raise InvalidWindowConfigurationError("window.stride_ms must be > 0")

    open_windows: dict[int, list[RowItem]] = {}
    first_epoch_us: int | None = None
    last_epoch_us: int | None = None

    for item in rows:
        row_index, epoch_us, _ = item
        if last_epoch_us is not None and epoch_us < last_epoch_us:
            raise NonMonotonicRowOrderError(
                f"Row {row_index} has timestamp epoch_us={epoch_us} which is earlier than "
                f"the previous row's epoch_us={last_epoch_us}"
            )
        last_epoch_us = epoch_us

        if first_epoch_us is None:
            first_epoch_us = epoch_us
        rel_us = epoch_us - first_epoch_us

        for idx in _windows_for_timestamp(rel_us, duration_us, stride_us):
            open_windows.setdefault(idx, []).append(item)

        closeable = [idx for idx in open_windows if idx * stride_us + duration_us <= rel_us]
        for idx in sorted(closeable):
            start = first_epoch_us + idx * stride_us
            end = start + duration_us
            yield idx, start, end, open_windows.pop(idx)

    if not drop_incomplete:
        for idx in sorted(open_windows):
            start = first_epoch_us + idx * stride_us
            end = start + duration_us
            yield idx, start, end, open_windows.pop(idx)
