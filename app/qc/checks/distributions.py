"""Non-finite value reporting and configured feature-range checks.

Step 7 already prohibits NaN/Infinity in transformed output, so
NON_FINITE_FEATURE_VALUE is a defensive check — it should normally never
fire. One issue is emitted per affected feature path (an aggregate count),
not per occurrence, keeping issue volume bounded by feature count rather
than sample count.

Range checks operate on Step 7's DERIVED features (e.g.
features.imu.statistics.accel_x_mean), not raw sensor values — that
distinction matters because Step 3 already owns raw-value plausibility."""

from __future__ import annotations

from app.qc.checks.base import QCCheck
from app.qc.metrics import DatasetMetrics
from app.qc.models import QCConfig, QCErrorCode, QCIssue, Severity


class DistributionCheck(QCCheck):
    def evaluate(self, metrics: DatasetMetrics, config: QCConfig) -> list[QCIssue]:
        issues: list[QCIssue] = []
        issues.extend(self._non_finite_issues(metrics))
        issues.extend(self._range_issues(metrics, config))
        return issues

    def _non_finite_issues(self, metrics: DatasetMetrics) -> list[QCIssue]:
        issues = []
        for path, accumulator in metrics.feature_accumulators.items():
            if accumulator.non_finite_count > 0:
                issues.append(
                    QCIssue(
                        code=QCErrorCode.NON_FINITE_FEATURE_VALUE.value,
                        severity=Severity.ERROR,
                        path=path,
                        observed=accumulator.non_finite_count,
                        threshold=0,
                        message=(
                            f"Feature '{path}' had {accumulator.non_finite_count} non-finite "
                            f"(NaN/Infinity) value(s), excluded from statistics."
                        ),
                    )
                )
        return issues

    def _range_issues(self, metrics: DatasetMetrics, config: QCConfig) -> list[QCIssue]:
        issues = []
        for path, range_config in config.feature_ranges.items():
            accumulator = metrics.feature_accumulators.get(path)
            if accumulator is None or accumulator.present_count == 0:
                continue

            observed_min = accumulator.welford.min
            observed_max = accumulator.welford.max
            below_min = range_config.min is not None and observed_min < range_config.min
            above_max = range_config.max is not None and observed_max > range_config.max
            if not (below_min or above_max):
                continue

            observed = observed_min if below_min else observed_max
            threshold = range_config.min if below_min else range_config.max
            issues.append(
                QCIssue(
                    code=QCErrorCode.FEATURE_RANGE_VIOLATION.value,
                    severity=range_config.severity,
                    path=path,
                    observed=observed,
                    threshold=threshold,
                    message=(
                        f"Feature '{path}' observed range [{observed_min}, {observed_max}] violates "
                        f"the configured range [{range_config.min}, {range_config.max}]."
                    ),
                )
            )
        return issues
