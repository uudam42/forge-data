"""Dataset-level modality coverage threshold check.

Never rejects individual samples — only reports the aggregate ratio of
samples in which a modality was present against a configured threshold."""

from __future__ import annotations

from app.qc.checks.base import QCCheck
from app.qc.metrics import DatasetMetrics
from app.qc.models import QCConfig, QCErrorCode, QCIssue


class ModalityCoverageCheck(QCCheck):
    def evaluate(self, metrics: DatasetMetrics, config: QCConfig) -> list[QCIssue]:
        if metrics.sample_count == 0:
            return []

        issues: list[QCIssue] = []
        for name, threshold in config.modality_coverage.items():
            accumulator = metrics.modality.get(name)
            present_count = accumulator.present_count if accumulator else 0
            ratio = present_count / metrics.sample_count
            if ratio < threshold.minimum_ratio:
                issues.append(
                    QCIssue(
                        code=QCErrorCode.LOW_MODALITY_COVERAGE.value,
                        severity=threshold.severity,
                        path=name,
                        observed=ratio,
                        threshold=threshold.minimum_ratio,
                        message=(
                            f"Modality '{name}' coverage is {ratio:.4f}, below the configured "
                            f"minimum of {threshold.minimum_ratio}."
                        ),
                    )
                )
        return issues
