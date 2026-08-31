"""The transformation profile contract.

A profile is looked up explicitly by (profile_name, profile_version) —
never an implicit "latest", mirroring every other registry in this project
(schemas, normalization profiles, alignment strategies, cleaning
policies). It decides which window modes and feature types are permitted,
builds the feature extractors for a given request config, and owns the
config-hash computation. There is no arbitrary transformation logic
directly in the API route or service — everything config-shaped is
mediated through a profile.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.transformation.features.base import FeatureExtractor
from app.transformation.models import TransformationConfig
from app.transformation.serialization import compute_transformation_config_hash


class InvalidTransformationConfigurationError(Exception):
    pass


class UnsupportedWindowModeError(Exception):
    pass


class TransformationProfile(ABC):
    profile_name: str
    profile_version: str
    transform_version: str
    permitted_window_modes: tuple[str, ...]

    @abstractmethod
    def validate_config(
        self,
        config: TransformationConfig,
        *,
        known_streams: list[str],
        max_window_size: int,
        max_time_window_ms: float,
    ) -> None:
        """Raises InvalidTransformationConfigurationError,
        UnsupportedWindowModeError, or
        app.transformation.features.common.UnknownFeatureError. Called once
        up front, before any row is processed."""
        raise NotImplementedError

    @abstractmethod
    def build_extractors(self, config: TransformationConfig) -> dict[str, FeatureExtractor]:
        """Returns the feature extractors this profile applies for the
        given effective configuration, keyed by stream name."""
        raise NotImplementedError

    def config_hash(self, config: TransformationConfig) -> str:
        """Deterministic hash of the effective transformation configuration
        — profile identity, transform version, and the full config
        (window mode/size/stride/duration/drop_incomplete, feature
        selection, raw inclusion, derived features, statistics, modality
        mask, relative time)."""
        payload = {
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "transform_version": self.transform_version,
            "config": config.model_dump(mode="json"),
        }
        return compute_transformation_config_hash(payload)
