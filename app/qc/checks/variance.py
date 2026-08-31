"""Constant / low-variance scalar feature detection.

A single threshold-based LOW_FEATURE_VARIANCE check, deliberately not
split into separate CONSTANT_FEATURE / NEAR_CONSTANT_FEATURE codes for the
MVP (see README "Deliberate MVP limitations")."""

from __future__ import annotations

from app.qc.checks.base import QCCheck
from app.qc.metrics import DatasetMetrics
from app.qc.models import QCConfig, QCErrorCode, QCIssue


class VarianceCheck(QCCheck):
    def evaluate(self, metrics: DatasetMetrics, config: QCConfig) -> list[QCIssue]:
        vc = config.variance
        if vc is None or not vc.enabled:
            return []

        issues: list[QCIssue] = []
        for path, accumulator in metrics.feature_accumulators.items():
            if accumulator.present_count < 2:
                continue  # variance is meaningless with fewer than 2 observations
            variance = accumulator.welford.variance
            if variance < vc.minimum_variance:
                issues.append(
                    QCIssue(
                        code=QCErrorCode.LOW_FEATURE_VARIANCE.value,
                        severity=vc.severity,
                        path=path,
                        observed=variance,
                        threshold=vc.minimum_variance,
                        message=(
                            f"Feature '{path}' has variance {variance:.6e}, below the configured "
                            f"minimum of {vc.minimum_variance:.6e}."
                        ),
                    )
                )
        return issues
