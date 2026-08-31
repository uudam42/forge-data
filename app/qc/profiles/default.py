"""Built-in QC profile: default_dataset_qc.

The only profile shipped in the Step 8 MVP — additional profiles are a
future extension point, not something the API route or service ever
branch on directly.
"""

from __future__ import annotations

from app.qc.checks.base import QCCheck
from app.qc.checks.dataset_size import DatasetSizeCheck
from app.qc.checks.distributions import DistributionCheck
from app.qc.checks.feature_completeness import FeatureCompletenessCheck
from app.qc.checks.identifiers import DuplicateSampleIdCheck
from app.qc.checks.modality_coverage import ModalityCoverageCheck
from app.qc.checks.temporal import TemporalOrderCheck
from app.qc.checks.variance import VarianceCheck
from app.qc.models import QCConfig
from app.qc.profiles.base import InvalidQCConfigurationError, QCProfile


class DefaultDatasetQCProfile(QCProfile):
    profile_name = "default_dataset_qc"
    profile_version = "1.0.0"

    def validate_config(self, config: QCConfig) -> None:
        if config.minimum_samples is not None and config.minimum_samples < 0:
            raise InvalidQCConfigurationError("minimum_samples must not be negative")

        for name, threshold in config.modality_coverage.items():
            if not 0.0 <= threshold.minimum_ratio <= 1.0:
                raise InvalidQCConfigurationError(
                    f"modality_coverage['{name}'].minimum_ratio must be within [0, 1]"
                )

        if config.feature_completeness is not None:
            ratio = config.feature_completeness.maximum_missing_ratio
            if ratio is not None and not 0.0 <= ratio <= 1.0:
                raise InvalidQCConfigurationError("feature_completeness.maximum_missing_ratio must be within [0, 1]")
            for path, override in config.feature_completeness.per_feature.items():
                if override.maximum_missing_ratio is not None and not 0.0 <= override.maximum_missing_ratio <= 1.0:
                    raise InvalidQCConfigurationError(
                        f"feature_completeness.per_feature['{path}'].maximum_missing_ratio must be within [0, 1]"
                    )

        if config.variance is not None and config.variance.minimum_variance < 0:
            raise InvalidQCConfigurationError("variance.minimum_variance must not be negative")

        for path, range_config in config.feature_ranges.items():
            if range_config.min is not None and range_config.max is not None and range_config.min > range_config.max:
                raise InvalidQCConfigurationError(f"feature_ranges['{path}']: min must not exceed max")

        if config.max_group_fraction is not None and not 0.0 < config.max_group_fraction <= 1.0:
            raise InvalidQCConfigurationError("max_group_fraction must be within (0, 1]")

        if config.drift is not None and config.drift.enabled:
            if config.drift.max_abs_standardized_mean_difference <= 0:
                raise InvalidQCConfigurationError("drift.max_abs_standardized_mean_difference must be > 0")
            if config.baseline_qc_id is None:
                raise InvalidQCConfigurationError("drift.enabled requires a baseline_qc_id")

    def build_checks(self, config: QCConfig) -> list[QCCheck]:
        return [
            DatasetSizeCheck(),
            ModalityCoverageCheck(),
            FeatureCompletenessCheck(),
            VarianceCheck(),
            DistributionCheck(),
            DuplicateSampleIdCheck(),
            TemporalOrderCheck(),
        ]


DEFAULT_DATASET_QC = DefaultDatasetQCProfile()
