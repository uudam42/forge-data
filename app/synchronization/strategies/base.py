"""Shared alignment vocabulary and the AlignmentStrategy interface.

StreamCursor does the one piece of work every strategy needs — walking a
stream's ordered iterator forward to bracket a target timestamp — so
NearestAlignmentStrategy and LinearInterpolationStrategy both just inspect
`cursor.prev` / `cursor.pending` rather than each re-implementing iterator
advancement. Because targets are always processed in non-decreasing order
(both reference-stream and fixed-rate timelines are monotonic), advancing
is a single forward pass per stream — O(1) buffered samples, never the
whole stream in memory.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator

from app.validation.schemas.base import SchemaDefinition


@dataclass(frozen=True)
class StreamSample:
    epoch_us: int
    record: dict


class StreamCursor:
    """Wraps a (record_number, epoch_us, record) iterator with one-step
    lookback (`prev`) and lookahead (`pending`), advanced by `advance_to`.
    """

    def __init__(self, iterator: Iterator[tuple[int, int, dict]]) -> None:
        self._iterator = iterator
        self.prev: StreamSample | None = None
        self.pending: StreamSample | None = self._next_sample()

    def _next_sample(self) -> StreamSample | None:
        item = next(self._iterator, None)
        if item is None:
            return None
        _, epoch_us, record = item
        return StreamSample(epoch_us=epoch_us, record=record)

    def advance_to(self, target_epoch_us: int) -> None:
        """Pulls samples forward until `pending` is strictly after the target
        (or the stream is exhausted), keeping `prev` as the last sample at
        or before the target.
        """
        while self.pending is not None and self.pending.epoch_us <= target_epoch_us:
            self.prev = self.pending
            self.pending = self._next_sample()


@dataclass(frozen=True)
class AlignmentContext:
    target_epoch_us: int
    tolerance_us: int
    schema: SchemaDefinition
    timestamp_field: str = "timestamp"


@dataclass(frozen=True)
class AlignmentOutcome:
    matched: bool
    method: str
    delta_ms: float | None = None
    reason: str | None = None
    is_exact: bool = False


class AlignmentStrategy(ABC):
    #: Whether this strategy is valid for a discrete (non-"tabular") stream.
    #: Only nearest-style strategies make sense for discrete data (e.g. a
    #: future camera/frame-reference stream) — interpolating between two
    #: image references is meaningless. See registry.py.
    supports_discrete_streams: bool = True

    @abstractmethod
    def align(self, cursor: StreamCursor, context: AlignmentContext) -> tuple[dict | None, AlignmentOutcome]:
        """Returns (aligned_record_or_None, outcome) for one target timestamp.

        `cursor` has already been advanced to `context.target_epoch_us`
        before this is called.
        """
        raise NotImplementedError
