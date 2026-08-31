"""Single-pass dataset metrics collection over a transformed.jsonl stream.

    transformed samples -> metric collectors -> DatasetMetrics -> QC checks

Metric collection is deliberately separate from check evaluation (see
app.qc.checks) — this module only observes and aggregates; it never
decides pass/fail, and it never mutates the sample dicts it reads.

Feature-path "missing" semantics use a single streaming pass with
retroactive backfill: when a scalar feature path is first seen at sample
index `i`, its accumulator is created with `missing_count=i` (every prior
sample, by construction, did not have this path — otherwise it would have
been discovered earlier). This avoids a second file pass while still
correctly implementing "the union of feature paths across the whole
dataset, not just the first sample."
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.qc.accumulator import PercentileBuffer, WelfordAccumulator
from app.qc.selectors import discover_scalar_feature_paths
from app.synchronization.readers import InvalidTimestampError, parse_canonical_timestamp_us


class FeatureAccumulator:
    def __init__(self, *, missing_count: int, max_values: int) -> None:
        self.missing_count = missing_count
        self.null_count = 0
        self.non_finite_count = 0
        self.welford = WelfordAccumulator()
        self.percentile_buffer = PercentileBuffer(max_values)

    @property
    def present_count(self) -> int:
        return self.welford.count

    def observe(self, value: object) -> None:
        if value is None:
            self.null_count += 1
            return
        numeric = float(value)
        if not math.isfinite(numeric):
            self.non_finite_count += 1
            return
        self.welford.update(numeric)
        self.percentile_buffer.add(numeric)

    def observe_missing(self) -> None:
        self.missing_count += 1


@dataclass
class ModalityAccumulator:
    present_count: int = 0
    coverage_values: list[float] = field(default_factory=list)

    def observe(self, present: bool, coverage_ratio: float | None, max_values: int) -> None:
        if present:
            self.present_count += 1
        if coverage_ratio is not None and len(self.coverage_values) < max_values:
            self.coverage_values.append(coverage_ratio)

    def coverage_stats(self) -> dict[str, float | None]:
        if not self.coverage_values:
            return {"mean": None, "median": None, "min": None, "max": None}
        ordered = sorted(self.coverage_values)
        n = len(ordered)
        mean = sum(ordered) / n
        mid = n // 2
        median = ordered[mid] if n % 2 == 1 else (ordered[mid - 1] + ordered[mid]) / 2
        return {"mean": mean, "median": median, "min": ordered[0], "max": ordered[-1]}


@dataclass
class NonMonotonicEvent:
    index: int
    previous_start_us: int
    current_start_us: int


@dataclass
class DuplicateSampleId:
    sample_id: str
    first_index: int
    duplicate_index: int


@dataclass
class DatasetMetrics:
    sample_count: int = 0
    known_modalities: list[str] = field(default_factory=list)
    modality: dict[str, ModalityAccumulator] = field(default_factory=dict)
    feature_accumulators: dict[str, FeatureAccumulator] = field(default_factory=dict)
    sample_ids_seen: dict[str, int] = field(default_factory=dict)
    duplicate_sample_ids: list[DuplicateSampleId] = field(default_factory=list)
    window_row_counts: WelfordAccumulator = field(default_factory=WelfordAccumulator)
    earliest_epoch_us: int | None = None
    latest_epoch_us: int | None = None
    earliest_timestamp: str | None = None
    latest_timestamp: str | None = None
    non_monotonic_events: list[NonMonotonicEvent] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float | None:
        if self.earliest_epoch_us is None or self.latest_epoch_us is None:
            return None
        return (self.latest_epoch_us - self.earliest_epoch_us) / 1_000_000.0


class DatasetMetricsCollector:
    def __init__(self, *, max_values_per_feature: int) -> None:
        self._max_values = max_values_per_feature
        self.metrics = DatasetMetrics()
        self._last_window_start_us: int | None = None

    def observe_sample(self, index: int, sample: dict) -> None:
        m = self.metrics
        m.sample_count += 1

        self._observe_sample_id(index, sample)
        self._observe_window(index, sample)
        self._observe_modality(sample)
        self._observe_features(index, sample)

    def _observe_sample_id(self, index: int, sample: dict) -> None:
        m = self.metrics
        sample_id = sample.get("sample_id")
        if sample_id is None:
            return
        if sample_id in m.sample_ids_seen:
            m.duplicate_sample_ids.append(
                DuplicateSampleId(sample_id=sample_id, first_index=m.sample_ids_seen[sample_id], duplicate_index=index)
            )
        else:
            m.sample_ids_seen[sample_id] = index

    def _observe_window(self, index: int, sample: dict) -> None:
        m = self.metrics
        window = sample.get("window") or {}

        row_count = window.get("row_count")
        if isinstance(row_count, (int, float)) and not isinstance(row_count, bool):
            m.window_row_counts.update(float(row_count))

        start_str = window.get("start_timestamp")
        if start_str:
            start_us = self._safe_parse_timestamp(start_str)
            if start_us is not None:
                if m.earliest_epoch_us is None or start_us < m.earliest_epoch_us:
                    m.earliest_epoch_us = start_us
                    m.earliest_timestamp = start_str
                if self._last_window_start_us is not None and start_us < self._last_window_start_us:
                    m.non_monotonic_events.append(
                        NonMonotonicEvent(
                            index=index, previous_start_us=self._last_window_start_us, current_start_us=start_us
                        )
                    )
                self._last_window_start_us = start_us

        end_str = window.get("end_timestamp")
        if end_str:
            end_us = self._safe_parse_timestamp(end_str)
            if end_us is not None and (m.latest_epoch_us is None or end_us > m.latest_epoch_us):
                m.latest_epoch_us = end_us
                m.latest_timestamp = end_str

    @staticmethod
    def _safe_parse_timestamp(value: str) -> int | None:
        try:
            return parse_canonical_timestamp_us(value)
        except InvalidTimestampError:
            return None

    def _observe_modality(self, sample: dict) -> None:
        m = self.metrics
        modality_mask = sample.get("modality_mask") or {}
        modality_coverage = sample.get("modality_coverage") or {}
        for name in set(modality_mask) | set(modality_coverage):
            if name not in m.modality:
                m.modality[name] = ModalityAccumulator()
                m.known_modalities.append(name)

            coverage = modality_coverage.get(name)
            coverage = coverage if isinstance(coverage, (int, float)) and not isinstance(coverage, bool) else None

            present = modality_mask.get(name) if name in modality_mask else None
            if present is None and coverage is not None:
                # Fall back to coverage-implied presence when no explicit
                # mask was requested — mirrors Step 7's own definition of
                # "present" as "at least one non-null observation."
                present = coverage > 0

            m.modality[name].observe(bool(present), coverage, self._max_values)

    def _observe_features(self, index: int, sample: dict) -> None:
        m = self.metrics
        features = sample.get("features") or {}
        discovered = discover_scalar_feature_paths(features)

        for path, value in discovered.items():
            if path not in m.feature_accumulators:
                m.feature_accumulators[path] = FeatureAccumulator(missing_count=index, max_values=self._max_values)
            m.feature_accumulators[path].observe(value)

        for path, accumulator in m.feature_accumulators.items():
            if path not in discovered:
                accumulator.observe_missing()
