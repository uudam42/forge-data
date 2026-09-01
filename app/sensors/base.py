"""The sensor plugin contract (v2.3).

A SensorPlugin bundles exactly the sensor-specific objects the pipeline's
existing, already-generic subsystems need in order to support a new
sensor type — nothing more. It is composition, not a "god object": every
field is an instance of an abstraction that already existed before v2.3
(IntegrityChecker, NormalizationProfile, FeatureExtractor) or a plain,
declarative metadata value. Generic pipeline stages (synchronization,
cleaning, QC, packaging, catalog) never import this module — they were
already sensor-agnostic before v2.3 and stay that way; only the three
subsystems whose *registries* previously hardcoded a per-sensor map
(integrity, normalization, transformation-feature-extraction) now build
that map from a SensorPluginRegistry instead. See
docs/DETAILED_GUIDE.md#sensor-plugin-architecture-v23.

`sensor_type` is the one key shared across every subsystem this project
already used consistently before v2.3 (schema_name, integrity registry
key, stream name, feature-extractor stream_name) — the plugin doesn't
invent a new identity, it names the one that already existed informally.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.integrity.checks.base import IntegrityChecker
from app.normalization.profiles.base import NormalizationProfile
from app.transformation.features.base import FeatureExtractor


class SensorPluginError(Exception):
    """Base class for sensor-plugin-layer failures."""


class DuplicateSensorPluginError(SensorPluginError):
    pass


class SensorPluginNotFoundError(SensorPluginError):
    pass


class InvalidSensorPluginError(SensorPluginError):
    """Raised at registration time for a structurally inconsistent plugin
    (e.g. a component whose own declared key disagrees with the plugin's
    sensor_type) -- fail at startup, never silently at request time."""


@dataclass(frozen=True)
class SensorPlugin:
    # Identity. sensor_type doubles as: schema_name, the integrity
    # registry key, the synchronization stream name, and the
    # transformation feature-extractor stream_name -- one identity, not
    # five independently-maintained ones.
    sensor_type: str
    plugin_version: str
    display_name: str

    # Which (schema_name, schema_version) this plugin's schema JSON
    # declares -- schema_name is always == sensor_type; schema_version is
    # tracked separately because a schema can gain a new version without
    # the plugin implementation itself changing (see Design Requirement
    # 17 / "Plugin version lineage" in the docs).
    schema_version: str

    integrity_checker: IntegrityChecker
    normalization_profile: NormalizationProfile
    feature_extractor: FeatureExtractor | None = None

    # Declarative metadata surfaced by GET /api/v1/sensors -- never used
    # by any generic pipeline stage to branch on sensor_type, only to
    # answer "what does this plugin claim about itself."
    timestamp_field: str = "timestamp"
    numeric_fields: tuple[str, ...] = field(default_factory=tuple)
    required_fields: tuple[str, ...] = field(default_factory=tuple)
    canonical_units: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.normalization_profile.schema_name != self.sensor_type:
            raise InvalidSensorPluginError(
                f"Plugin '{self.sensor_type}': normalization_profile.schema_name="
                f"{self.normalization_profile.schema_name!r} must equal sensor_type"
            )
        if self.normalization_profile.schema_version != self.schema_version:
            raise InvalidSensorPluginError(
                f"Plugin '{self.sensor_type}': normalization_profile.schema_version="
                f"{self.normalization_profile.schema_version!r} must equal "
                f"schema_version={self.schema_version!r}"
            )
        if self.feature_extractor is not None and self.feature_extractor.stream_name != self.sensor_type:
            raise InvalidSensorPluginError(
                f"Plugin '{self.sensor_type}': feature_extractor.stream_name="
                f"{self.feature_extractor.stream_name!r} must equal sensor_type"
            )
