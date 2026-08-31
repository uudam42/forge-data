"""Linear interpolation alignment.

Only ever operates between two bracketing samples (prev.epoch_us <= target
<= pending.epoch_us) — never extrapolates. If either side is missing (the
target falls outside the stream's observed range), the whole stream is
unmatched at that target: no value is invented.

Field-level policy (documented, not guessed):
  - numeric fields (float/integer)  -> linear interpolation
  - everything else (string, boolean, device_id, ...) -> nearest of the two
    bracketing samples, still subject to the configured tolerance
  - an exact timestamp match on either bracket short-circuits interpolation
    entirely and returns that observation's fields verbatim

Not valid for discrete streams (see AlignmentStrategy.supports_discrete_streams)
— interpolating between two frame references or category values is
meaningless, so requesting "linear" for a non-"tabular" schema is rejected
before this class is ever invoked (see registry.py).
"""

from __future__ import annotations

from app.synchronization.strategies.base import (
    AlignmentContext,
    AlignmentOutcome,
    AlignmentStrategy,
    StreamCursor,
)
from app.validation.schemas.base import FieldType

_METHOD = "linear"
_NUMERIC_TYPES = (FieldType.FLOAT, FieldType.INTEGER)


class LinearInterpolationStrategy(AlignmentStrategy):
    supports_discrete_streams = False

    def align(self, cursor: StreamCursor, context: AlignmentContext) -> tuple[dict | None, AlignmentOutcome]:
        prev, pending = cursor.prev, cursor.pending
        target = context.target_epoch_us

        if prev is not None and prev.epoch_us == target:
            return prev.record, AlignmentOutcome(matched=True, method=_METHOD, delta_ms=0.0, is_exact=True)
        if pending is not None and pending.epoch_us == target:
            return pending.record, AlignmentOutcome(matched=True, method=_METHOD, delta_ms=0.0, is_exact=True)

        if prev is None or pending is None:
            return None, AlignmentOutcome(matched=False, method=_METHOD, reason="NO_EXTRAPOLATION")

        t0, t1 = prev.epoch_us, pending.epoch_us
        # StreamCursor guarantees prev.epoch_us < target < pending.epoch_us
        # here (the two exact-match cases above are already handled, and
        # advance_to() never leaves `pending` behind the target).
        fraction = (target - t0) / (t1 - t0)

        interpolated: dict[str, object] = {}
        for field_name, field_def in context.schema.fields.items():
            if field_name == context.timestamp_field:
                continue
            x0 = prev.record.get(field_name)
            x1 = pending.record.get(field_name)

            if field_def.type in _NUMERIC_TYPES:
                interpolated[field_name] = None if (x0 is None or x1 is None) else x0 + (x1 - x0) * fraction
            else:
                delta0, delta1 = abs(target - t0), abs(target - t1)
                nearest_value, nearest_delta_us = (x0, delta0) if delta0 <= delta1 else (x1, delta1)
                interpolated[field_name] = (
                    nearest_value if nearest_delta_us <= context.tolerance_us else None
                )

        delta_ms = min(abs(target - t0), abs(target - t1)) / 1000.0
        return interpolated, AlignmentOutcome(matched=True, method=_METHOD, delta_ms=delta_ms)
