"""Feature completeness check.

completeness_ratio = numeric_present_count / total_samples (documented
definition — see README). missing_ratio = 1 - completeness_ratio folds in
structurally-absent paths, explicit nulls, and non-finite values alike,
since none of those are usable numeric observations."""

from __future__ import annotations

from app.qc.checks.base import QCCheck
from app.qc.metrics import DatasetMetrics
from app.qc.models import QCConfig, QCErrorCode, QCIssue


class FeatureCompletenessCheck(QCCheck):
    def evaluate(self, metrics: DatasetMetrics, config: QCConfig) -> list[QCIssue]:
        fc = config.feature_completeness
        if fc is None or metrics.sample_count == 0:
            return []

        issues: list[QCIssue] = []
        total = metrics.sample_count
        for path, accumulator in metrics.feature_accumulators.items():
            override = fc.per_feature.get(path)
            threshold = fc.maximum_missing_ratio
            severity = fc.severity
            if override is not None:
                if override.maximum_missing_ratio is not None:
                    threshold = override.maximum_missing_ratio
                if override.severity is not None:
                    severity = override.severity
            if threshold is None:
                continue

            missing_ratio = 1.0 - (accumulator.present_count / total)
            if missing_ratio > threshold:
                issues.append(
                    QCIssue(
                        code=QCErrorCode.LOW_FEATURE_COMPLETENESS.value,
                        severity=severity,
                        path=path,
                        observed=missing_ratio,
                        threshold=threshold,
                        message=(
                            f"Feature '{path}' is missing/null/non-finite in {missing_ratio:.4f} of "
                            f"samples, exceeding the configured maximum of {threshold}."
                        ),
                    )
                )
        return issues
