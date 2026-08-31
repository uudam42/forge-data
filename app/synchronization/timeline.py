"""Reference timeline generation.

Mode A (reference stream, the MVP default) needs no dedicated generator
here — the chosen stream's own corrected (epoch_us, record) sequence *is*
the timeline, consumed directly and fully streamed (see service.py).

Mode B (fixed_rate) generates a synthetic uniform timeline lazily, over an
explicit, documented interval policy: the intersection of every
participating stream's own (corrected) time range. Never extrapolates
beyond any stream's observed bounds.
"""

from __future__ import annotations

from typing import Iterator


class InvalidSyncConfigurationError(Exception):
    pass


def fixed_rate_period_us(frequency_hz: float, *, max_frequency_hz: float) -> int:
    """Validates and converts a requested frequency into an integer period
    in microseconds.

    Frequencies must be strictly positive and not exceed max_frequency_hz —
    a configured ceiling that exists specifically to prevent an
    accidentally enormous generated timeline (e.g. a typo'd 100_000 Hz
    request producing millions of rows).
    """
    if frequency_hz <= 0:
        raise InvalidSyncConfigurationError(f"frequency_hz must be positive, got {frequency_hz}")
    if frequency_hz > max_frequency_hz:
        raise InvalidSyncConfigurationError(
            f"frequency_hz={frequency_hz} exceeds the configured maximum of {max_frequency_hz} Hz"
        )
    period_us = round(1_000_000.0 / frequency_hz)
    if period_us <= 0:
        raise InvalidSyncConfigurationError(
            f"frequency_hz={frequency_hz} is too high to represent at microsecond resolution"
        )
    return period_us


def intersection_interval(stream_ranges: list[tuple[int, int]]) -> tuple[int, int]:
    """Returns (start, end) as the intersection of every (first, last) range.

    If ranges don't overlap, start > end is returned deliberately (rather
    than raised) — fixed_rate_timeline() below treats that as "no usable
    interval" and yields nothing, since an empty synchronized result is a
    legitimate outcome to report, not a configuration error.
    """
    start = max(first for first, _ in stream_ranges)
    end = min(last for _, last in stream_ranges)
    return start, end


def fixed_rate_timeline(*, start_epoch_us: int, end_epoch_us: int, period_us: int) -> Iterator[int]:
    """Lazily yields start_epoch_us, start_epoch_us + period_us, ... up to
    and including end_epoch_us. Yields nothing if start > end.
    """
    t = start_epoch_us
    while t <= end_epoch_us:
        yield t
        t += period_us
