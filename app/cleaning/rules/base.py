"""Shared cleaning-rule vocabulary: RuleContext, DropReason, RedactionRecord,
RuleOutcome, and the CleaningRule interface.

Kept intentionally simple — no generic rule language, no expression
evaluation, no dynamic code execution (no eval()). A rule is a plain
Python class with one `evaluate` method; policies decide which rules run
and in what order (see policies/default.py).

These are lightweight dataclasses, not Pydantic models — every row
produces a transient decision, and only a capped subset ever needs to
become a persisted (Pydantic) report object (see metrics.py).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuleContext:
    row_index: int


@dataclass(frozen=True)
class DropReason:
    code: str
    stream: str | None = None
    duplicate_of_row_index: int | None = None


@dataclass(frozen=True)
class RedactionRecord:
    code: str
    field: str


@dataclass
class RuleOutcome:
    drop_reasons: list[DropReason] = field(default_factory=list)
    redactions: list[RedactionRecord] = field(default_factory=list)

    @property
    def should_drop(self) -> bool:
        return bool(self.drop_reasons)


class CleaningRule(ABC):
    code: str

    @abstractmethod
    def evaluate(self, row: dict, *, context: RuleContext) -> RuleOutcome:
        raise NotImplementedError
