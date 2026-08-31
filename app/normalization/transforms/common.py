"""Small numeric/boolean helpers shared by normalization profiles.

to_float is reused from app.integrity.checks.base rather than duplicated —
it is a generic, non-integrity-specific utility (no IntegrityIssue creation
inside it) that already does exactly what's needed here: bridge CSV's
string cells and JSON's native numbers through one path, without guessing
or silently coercing. Reimplementing the same few lines a third time
(Step 2 validators, Step 3, now Step 4) would be pure duplication.
"""

from __future__ import annotations

import math

from app.integrity.checks.base import to_float

__all__ = ["to_float", "to_bool", "is_finite"]

# Mirrors Step 2's CSV boolean convention exactly: only these literal
# strings (case-insensitive) are accepted — no other truthy/falsy string.
_TRUE_STRINGS = {"true"}
_FALSE_STRINGS = {"false"}


def is_finite(value: float) -> bool:
    return math.isfinite(value)


def to_bool(value: object) -> bool | None:
    """Returns None if `value` is not a recognized boolean representation.

    Native JSON booleans pass through unchanged; CSV strings must be
    exactly "true"/"false" (case-insensitive) — no "1"/"0"/"yes"/"no".
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    return None
