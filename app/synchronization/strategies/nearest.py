"""Nearest-neighbor alignment.

Given a target timestamp, choose the bracketing sample (prev or pending)
with the smallest absolute time difference. Ties are broken deterministically
in favor of the earlier observation — since `prev.epoch_us <= pending.epoch_us`
always holds for an ordered stream, a tie means `prev` wins.

Valid for discrete (non-interpolatable) streams too — this is the only
strategy a future camera/frame-reference stream would ever use.
"""

from __future__ import annotations

from app.synchronization.strategies.base import (
    AlignmentContext,
    AlignmentOutcome,
    AlignmentStrategy,
    StreamCursor,
)

_METHOD = "nearest"


class NearestAlignmentStrategy(AlignmentStrategy):
    supports_discrete_streams = True

    def align(self, cursor: StreamCursor, context: AlignmentContext) -> tuple[dict | None, AlignmentOutcome]:
        candidates = [c for c in (cursor.prev, cursor.pending) if c is not None]
        if not candidates:
            return None, AlignmentOutcome(matched=False, method=_METHOD, reason="NO_DATA")

        target = context.target_epoch_us
        # Prefer the earlier sample (cursor.prev) on an exact tie: since
        # candidates are visited in (prev, pending) order and prev's epoch
        # is always <= pending's, using a stable min() over that order
        # already implements the "earlier wins" tie-break.
        best = min(candidates, key=lambda c: abs(c.epoch_us - target))
        delta_us = abs(best.epoch_us - target)
        delta_ms = delta_us / 1000.0

        if delta_us > context.tolerance_us:
            return None, AlignmentOutcome(matched=False, method=_METHOD, reason="OUTSIDE_TOLERANCE")

        return best.record, AlignmentOutcome(
            matched=True, method=_METHOD, delta_ms=delta_ms, is_exact=(delta_us == 0)
        )
