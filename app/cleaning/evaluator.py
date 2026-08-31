"""RowEvaluator: applies a policy's ordered rules to one synchronized row.

Evaluation order is exactly the order `rules` were given — policies decide
that order (see policies/default.py), never re-sorted here, never
dependent on registry iteration order.

The first rule that decides to drop a row short-circuits: later rules
(including privacy redaction, if it hasn't run yet) are never evaluated
for a dropped row, so a dropped row is never unnecessarily redacted, and
its report entry carries only the one rule's reasons.
"""

from __future__ import annotations

from app.cleaning.rules.base import CleaningRule, DropReason, RedactionRecord, RuleContext
from app.cleaning.rules.common import apply_redactions


class RowEvaluator:
    def __init__(self, rules: list[CleaningRule]) -> None:
        self._rules = rules

    def evaluate(
        self, row_index: int, row: dict
    ) -> tuple[dict | None, list[DropReason], list[RedactionRecord]]:
        context = RuleContext(row_index=row_index)
        redactions: list[RedactionRecord] = []

        for rule in self._rules:
            outcome = rule.evaluate(row, context=context)
            if outcome.should_drop:
                return None, outcome.drop_reasons, []
            redactions.extend(outcome.redactions)

        cleaned_row = apply_redactions(row, [r.field for r in redactions]) if redactions else row
        return cleaned_row, [], redactions
