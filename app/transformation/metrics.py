"""Accumulates bounded, report-level statistics across all windows of a
transformation run: window/sample counts, per-stream modality coverage, and
a representative feature-key count per stream. Never accumulates per-sample
detail — report.json stays small regardless of dataset size.
"""

from __future__ import annotations

from app.transformation.feature_engine import WindowResult


class TransformationMetricsAccumulator:
    def __init__(self) -> None:
        self.samples_written = 0
        self.total_row_count = 0
        self._windows_present: dict[str, int] = {}
        self._feature_counts: dict[str, int] = {}

    def record_window(self, result: WindowResult) -> None:
        self.samples_written += 1
        self.total_row_count += result.row_count

        for stream_name, present_count in result.stream_present_counts.items():
            if present_count > 0:
                self._windows_present[stream_name] = self._windows_present.get(stream_name, 0) + 1

        for stream_name, payload in result.features.items():
            if stream_name in self._feature_counts or payload is None:
                continue
            if "statistics" in payload:
                self._feature_counts[stream_name] = len(payload["statistics"])
            elif "raw" in payload:
                self._feature_counts[stream_name] = len(payload["raw"])

    @property
    def average_rows_per_window(self) -> float | None:
        if self.samples_written == 0:
            return None
        return self.total_row_count / self.samples_written

    def modality_coverage(self, known_streams: list[str]) -> dict[str, dict]:
        coverage = {}
        for stream_name in known_streams:
            windows_present = self._windows_present.get(stream_name, 0)
            ratio = windows_present / self.samples_written if self.samples_written else 0.0
            coverage[stream_name] = {"windows_present": windows_present, "coverage_ratio": ratio}
        return coverage

    @property
    def feature_counts(self) -> dict[str, int]:
        return dict(self._feature_counts)
