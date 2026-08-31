"""Temporal ordering check: NON_MONOTONIC_SAMPLE_TIME.

A structural invariant, not a configurable quality preference — severity
is fixed at "error" rather than exposed as a config knob. Step 8 only
reports a regression; it never reorders samples."""

from __future__ import annotations

from app.qc.checks.base import QCCheck
from app.qc.metrics import DatasetMetrics
from app.qc.models import QCConfig, QCErrorCode, QCIssue, Severity


class TemporalOrderCheck(QCCheck):
    def evaluate(self, metrics: DatasetMetrics, config: QCConfig) -> list[QCIssue]:
        return [
            QCIssue(
                code=QCErrorCode.NON_MONOTONIC_SAMPLE_TIME.value,
                severity=Severity.ERROR,
                path=None,
                observed=event.current_start_us,
                threshold=event.previous_start_us,
                message=(
                    f"Sample at index {event.index} has a window start earlier than the previous "
                    f"sample's window start."
                ),
            )
            for event in metrics.non_monotonic_events
        ]
