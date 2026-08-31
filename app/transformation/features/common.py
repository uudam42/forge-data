"""Shared helpers for feature extractors: numeric-value validation and the
UnknownFeatureError raised by config validation.

Non-finite (NaN/Infinity) values are never silently propagated: upstream
integrity checking should already have rejected or flagged invalid values,
so encountering one here means something unexpected happened and the whole
transformation run must fail loudly rather than emit invalid output.
"""

from __future__ import annotations

import math


class UnknownFeatureError(Exception):
    pass


class InvalidNumericValueError(Exception):
    pass


def require_finite(value: float, *, field: str, row_index: int) -> float:
    if not math.isfinite(value):
        raise InvalidNumericValueError(
            f"Row {row_index}: field '{field}' has non-finite value {value!r}"
        )
    return value
