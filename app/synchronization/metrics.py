"""Per-stream synchronization metrics.

Accumulated in O(1) memory regardless of output row count — a running
sum/count/max, never a stored list of every delta — per the instruction not
to calculate expensive metrics unnecessarily.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.synchronization.models import StreamMetrics


@dataclass
class StreamMetricsAccumulator:
    source_records: int
    matched_rows: int = 0
    unmatched_rows: int = 0
    exact_match_count: int = 0
    interpolated_count: int = 0
    _delta_sum_ms: float = field(default=0.0, init=False, repr=False)
    _delta_max_ms: float = field(default=0.0, init=False, repr=False)
    _used_linear: bool = field(default=False, init=False, repr=False)

    def record(self, alignment_result: dict) -> None:
        method = alignment_result.get("method")
        if method == "linear":
            self._used_linear = True

        if not alignment_result.get("matched"):
            self.unmatched_rows += 1
            return

        self.matched_rows += 1
        delta_ms = alignment_result.get("delta_ms") or 0.0
        self._delta_sum_ms += delta_ms
        self._delta_max_ms = max(self._delta_max_ms, delta_ms)

        if method == "linear":
            if delta_ms == 0.0:
                self.exact_match_count += 1
            else:
                self.interpolated_count += 1

    def finalize(self, output_rows: int) -> StreamMetrics:
        coverage_ratio = (self.matched_rows / output_rows) if output_rows else 0.0
        mean_abs_delta_ms = (self._delta_sum_ms / self.matched_rows) if self.matched_rows else None
        max_abs_delta_ms = self._delta_max_ms if self.matched_rows else None
        return StreamMetrics(
            source_records=self.source_records,
            matched_rows=self.matched_rows,
            unmatched_rows=self.unmatched_rows,
            coverage_ratio=coverage_ratio,
            mean_abs_delta_ms=mean_abs_delta_ms,
            max_abs_delta_ms=max_abs_delta_ms,
            exact_match_count=self.exact_match_count if self._used_linear else None,
            interpolated_count=self.interpolated_count if self._used_linear else None,
        )
