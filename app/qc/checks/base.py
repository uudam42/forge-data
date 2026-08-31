"""QC check contract: a pure function of (DatasetMetrics, QCConfig) ->
list[QCIssue].

Checks never mutate DatasetMetrics, never touch storage, and never decide
the overall QCStatus — that aggregation happens once, in the service, from
the union of every check's issues. This keeps metric collection (see
app.qc.metrics) fully decoupled from policy/threshold evaluation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.qc.metrics import DatasetMetrics
from app.qc.models import QCConfig, QCIssue


class QCCheck(ABC):
    @abstractmethod
    def evaluate(self, metrics: DatasetMetrics, config: QCConfig) -> list[QCIssue]:
        raise NotImplementedError
