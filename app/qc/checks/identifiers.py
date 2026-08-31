"""Sample identifier checks: DUPLICATE_SAMPLE_ID.

Step 7 generates deterministic sample IDs; Step 8 only verifies their
uniqueness across the dataset — it never deduplicates or rewrites them."""

from __future__ import annotations

from app.qc.checks.base import QCCheck
from app.qc.metrics import DatasetMetrics
from app.qc.models import QCConfig, QCErrorCode, QCIssue, Severity


class DuplicateSampleIdCheck(QCCheck):
    def evaluate(self, metrics: DatasetMetrics, config: QCConfig) -> list[QCIssue]:
        return [
            QCIssue(
                code=QCErrorCode.DUPLICATE_SAMPLE_ID.value,
                severity=Severity.ERROR,
                path=dup.sample_id,
                observed=dup.duplicate_index,
                threshold=dup.first_index,
                message=(
                    f"sample_id '{dup.sample_id}' at sample index {dup.duplicate_index} duplicates "
                    f"the one first seen at index {dup.first_index}."
                ),
            )
            for dup in metrics.duplicate_sample_ids
        ]
