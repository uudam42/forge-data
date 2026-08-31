"""The packaging profile contract.

A profile is looked up explicitly by (profile_name, profile_version) —
never an implicit "latest" — mirroring every other registry in this
project. It decides which split strategies, grouping modes, and export
formats are permitted; there is no arbitrary Python execution or
user-supplied expression evaluation of any kind. Policy logic (supported
strategies/modes/formats, validation rules) lives here, never scattered
across PackagingService.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.packaging.models import PackagingConfig, PACKAGE_ENGINE_VERSION
from app.packaging.serialization import compute_packaging_config_hash


class InvalidPackagingConfigurationError(Exception):
    pass


class InvalidSplitRatiosError(Exception):
    pass


class UnsupportedSplitStrategyError(Exception):
    pass


class UnsupportedGroupingModeError(Exception):
    pass


class UnsupportedExportFormatError(Exception):
    pass


_RATIO_TOLERANCE = 1e-6


class PackagingProfile(ABC):
    profile_name: str
    profile_version: str
    package_engine_version: str = PACKAGE_ENGINE_VERSION
    supported_split_strategies: tuple[str, ...]
    supported_grouping_modes: tuple[str, ...]
    supported_export_formats: tuple[str, ...]

    def validate_config(self, config: PackagingConfig) -> None:
        """Raises InvalidSplitRatiosError, UnsupportedSplitStrategyError,
        UnsupportedGroupingModeError, UnsupportedExportFormatError, or
        InvalidPackagingConfigurationError. Called once up front, before
        any sample is read."""
        split = config.split
        if split.strategy not in self.supported_split_strategies:
            raise UnsupportedSplitStrategyError(f"Unsupported split strategy '{split.strategy}'")
        if config.grouping.mode not in self.supported_grouping_modes:
            raise UnsupportedGroupingModeError(f"Unsupported grouping mode '{config.grouping.mode}'")
        for fmt in config.exports:
            if fmt not in self.supported_export_formats:
                raise UnsupportedExportFormatError(f"Unsupported export format '{fmt}'")

        for name, value in (
            ("train_ratio", split.train_ratio),
            ("validation_ratio", split.validation_ratio),
            ("test_ratio", split.test_ratio),
        ):
            if value < 0:
                raise InvalidSplitRatiosError(f"{name} must not be negative")
        if split.train_ratio <= 0:
            raise InvalidSplitRatiosError("train_ratio must be > 0")

        total = split.train_ratio + split.validation_ratio + split.test_ratio
        if abs(total - 1.0) > _RATIO_TOLERANCE:
            raise InvalidSplitRatiosError(
                f"train_ratio + validation_ratio + test_ratio must sum to 1.0 "
                f"(within {_RATIO_TOLERANCE}); got {total}"
            )

    def config_hash(self, config: PackagingConfig) -> str:
        """Deterministic hash of the effective packaging configuration —
        profile identity, engine version, and the full behavior-changing
        config (split strategy/ratios/seed, grouping mode, export
        formats). Deliberately EXCLUDES dataset_name/dataset_version/
        description — those are inert metadata that never affects split
        assignment or file bytes, so two packages differing only in that
        metadata are, correctly, "the same effective config."""
        payload = {
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "package_engine_version": self.package_engine_version,
            "config": config.model_dump(mode="json"),
        }
        return compute_packaging_config_hash(payload)
