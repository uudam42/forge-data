"""Orchestrates feature extraction across every known stream for one window:
builds the per-stream feature payloads, the window-level modality mask and
coverage ratios, optional relative-time offsets, and window provenance
metadata. Never decides labels, splits, or dataset-wide judgments — see
app.transformation.service for the surrounding lineage/config-hash logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.transformation.features.base import FeatureExtractor, WindowRow
from app.transformation.models import StreamFeatureConfig
from app.transformation.windowing import RowItem


@dataclass(frozen=True)
class WindowResult:
    window_index: int
    start_epoch_us: int
    end_epoch_us: int
    row_count: int
    source_row_start: int
    source_row_end: int
    features: dict[str, dict | None]
    modality_mask: dict[str, bool]
    modality_coverage: dict[str, float]
    relative_time_ms: list[float] | None
    stream_present_counts: dict[str, int]  # for report.json modality_coverage aggregation


class FeatureEngine:
    def __init__(
        self,
        *,
        extractors: dict[str, FeatureExtractor],
        feature_configs: dict[str, StreamFeatureConfig],
        known_streams: list[str],
        include_modality_mask: bool,
        include_relative_time: bool,
    ) -> None:
        self._extractors = extractors
        self._feature_configs = feature_configs
        self._known_streams = known_streams
        self._include_modality_mask = include_modality_mask
        self._include_relative_time = include_relative_time

    @property
    def include_modality_mask(self) -> bool:
        return self._include_modality_mask

    def process_window(
        self, *, window_index: int, start_epoch_us: int, end_epoch_us: int, window_rows: list[RowItem]
    ) -> WindowResult:
        row_count = len(window_rows)
        source_row_start = window_rows[0][0]
        source_row_end = window_rows[-1][0]

        features: dict[str, dict | None] = {}
        modality_mask: dict[str, bool] = {}
        modality_coverage: dict[str, float] = {}
        stream_present_counts: dict[str, int] = {}

        for stream_name in self._known_streams:
            stream_rows = [
                WindowRow(row_index=ri, epoch_us=eu, payload=(row.get("streams") or {}).get(stream_name))
                for ri, eu, row in window_rows
            ]
            present_count = sum(1 for r in stream_rows if r.payload is not None)
            stream_present_counts[stream_name] = present_count
            modality_mask[stream_name] = present_count > 0
            modality_coverage[stream_name] = present_count / row_count if row_count else 0.0

            extractor = self._extractors.get(stream_name)
            if extractor is not None:
                config = self._feature_configs[stream_name]
                result = extractor.extract(stream_rows, config)
                features[stream_name] = result.features

        relative_time_ms = None
        if self._include_relative_time:
            window_start = window_rows[0][1]
            relative_time_ms = [(eu - window_start) / 1000.0 for _, eu, _ in window_rows]

        return WindowResult(
            window_index=window_index,
            start_epoch_us=start_epoch_us,
            end_epoch_us=end_epoch_us,
            row_count=row_count,
            source_row_start=source_row_start,
            source_row_end=source_row_end,
            features=features,
            modality_mask=modality_mask if self._include_modality_mask else {},
            modality_coverage=modality_coverage,
            relative_time_ms=relative_time_ms,
            stream_present_counts=stream_present_counts,
        )
