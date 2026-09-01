"""Built-in transformation profile: multimodal_window_v1.

Supports both windowing modes (count, time) and any registered sensor
plugin's stream (v2.3: resolved through app.sensors.registry, not a
hardcoded per-sensor map) — this is the only profile shipped in the
Step 7 MVP; additional profiles are a future extension point, not
something the API route or service ever branch on directly.
"""

from __future__ import annotations

from pydantic import ValidationError

from app.sensors.base import SensorPluginNotFoundError
from app.sensors.registry import SensorPluginRegistry, get_default_registry
from app.transformation.features.base import FeatureExtractor
from app.transformation.models import TransformationConfig
from app.transformation.profiles.base import (
    InvalidTransformationConfigurationError,
    TransformationProfile,
    UnsupportedWindowModeError,
)

_WINDOW_MODES = ("count", "time")


class MultimodalWindowProfile(TransformationProfile):
    profile_name = "multimodal_window_v1"
    profile_version = "1.0.0"
    transform_version = "1.0.0"
    permitted_window_modes = _WINDOW_MODES

    def __init__(self, sensor_registry: SensorPluginRegistry | None = None) -> None:
        self._sensor_registry = sensor_registry or get_default_registry()

    def validate_config(
        self,
        config: TransformationConfig,
        *,
        known_streams: list[str],
        max_window_size: int,
        max_time_window_ms: float,
    ) -> None:
        window = config.window
        if window.mode not in self.permitted_window_modes:
            raise UnsupportedWindowModeError(f"Unsupported window mode '{window.mode}'")

        if window.mode == "count":
            if window.size is None or window.stride is None:
                raise InvalidTransformationConfigurationError(
                    "window.size and window.stride are required for mode='count'"
                )
            if window.size <= 0:
                raise InvalidTransformationConfigurationError("window.size must be > 0")
            if window.stride <= 0:
                raise InvalidTransformationConfigurationError("window.stride must be > 0")
            if window.size > max_window_size:
                raise InvalidTransformationConfigurationError(
                    f"window.size {window.size} exceeds the configured maximum {max_window_size}"
                )
        else:  # time
            if window.duration_ms is None or window.stride_ms is None:
                raise InvalidTransformationConfigurationError(
                    "window.duration_ms and window.stride_ms are required for mode='time'"
                )
            if window.duration_ms <= 0:
                raise InvalidTransformationConfigurationError("window.duration_ms must be > 0")
            if window.stride_ms <= 0:
                raise InvalidTransformationConfigurationError("window.stride_ms must be > 0")
            if window.duration_ms > max_time_window_ms:
                raise InvalidTransformationConfigurationError(
                    f"window.duration_ms {window.duration_ms} exceeds the configured maximum {max_time_window_ms}"
                )

        try:
            requested = config.features.stream_configs()
        except ValidationError as exc:
            raise InvalidTransformationConfigurationError(f"Invalid feature configuration: {exc}") from exc

        for stream_name, stream_config in requested.items():
            if stream_name not in known_streams:
                raise InvalidTransformationConfigurationError(
                    f"Features requested for stream '{stream_name}', which is not part of this "
                    f"cleaning run's lineage (known streams: {sorted(known_streams)})"
                )
            try:
                plugin = self._sensor_registry.get(stream_name)
            except SensorPluginNotFoundError as exc:
                raise InvalidTransformationConfigurationError(
                    f"Features requested for stream '{stream_name}', which has no registered sensor "
                    f"plugin (available: {sorted(p.sensor_type for p in self._sensor_registry.list_plugins())})"
                ) from exc
            if plugin.feature_extractor is None:
                raise InvalidTransformationConfigurationError(
                    f"Sensor plugin '{stream_name}' does not provide a feature extractor"
                )
            plugin.feature_extractor.validate_config(stream_config)

    def build_extractors(self, config: TransformationConfig) -> dict[str, FeatureExtractor]:
        extractors: dict[str, FeatureExtractor] = {}
        for stream_name in config.features.stream_configs():
            plugin = self._sensor_registry.get(stream_name)
            if plugin.feature_extractor is not None:
                extractors[stream_name] = plugin.feature_extractor
        return extractors


MULTIMODAL_WINDOW_V1 = MultimodalWindowProfile()
