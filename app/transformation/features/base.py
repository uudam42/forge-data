"""Feature extractor interface and shared per-window vocabulary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.transformation.models import StreamFeatureConfig


@dataclass(frozen=True)
class WindowRow:
    row_index: int
    epoch_us: int
    payload: dict | None  # this stream's own sub-dict from row["streams"][name], or None if absent


@dataclass(frozen=True)
class StreamFeatureResult:
    present: bool  # True if at least one row in the window had non-null payload for this stream
    present_count: int
    missing_count: int
    features: dict | None  # None when nothing was requested/available to report for this stream


class FeatureExtractor(ABC):
    stream_name: str

    @abstractmethod
    def validate_config(self, config: StreamFeatureConfig) -> None:
        """Raise UnknownFeatureError for any unrecognized statistic/derived
        feature name. Called once up front, before any row is processed, so
        misconfiguration fails fast rather than partway through a run."""
        raise NotImplementedError

    @abstractmethod
    def extract(self, rows: list[WindowRow], config: StreamFeatureConfig) -> StreamFeatureResult:
        raise NotImplementedError
