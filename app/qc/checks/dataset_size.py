"""Dataset size checks: EMPTY_DATASET (always, regardless of config) and
DATASET_TOO_SMALL (only when config.minimum_samples is set)."""

from __future__ import annotations

from app.qc.checks.base import QCCheck
from app.qc.metrics import DatasetMetrics
from app.qc.models import QCConfig, QCErrorCode, QCIssue, Severity


class DatasetSizeCheck(QCCheck):
    def evaluate(self, metrics: DatasetMetrics, config: QCConfig) -> list[QCIssue]:
        if metrics.sample_count == 0:
            return [
                QCIssue(
                    code=QCErrorCode.EMPTY_DATASET.value,
                    severity=Severity.ERROR,
                    observed=0,
                    threshold=None,
                    message="The transformed dataset contains zero samples.",
                )
            ]

        issues: list[QCIssue] = []
        if config.minimum_samples is not None and metrics.sample_count < config.minimum_samples:
            issues.append(
                QCIssue(
                    code=QCErrorCode.DATASET_TOO_SMALL.value,
                    severity=config.dataset_size_severity,
                    observed=metrics.sample_count,
                    threshold=config.minimum_samples,
                    message=(
                        f"Dataset has {metrics.sample_count} samples, below the configured "
                        f"minimum of {config.minimum_samples}."
                    ),
                )
            )
        return issues
