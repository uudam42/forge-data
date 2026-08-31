"""Explicit field redaction via dot-separated paths.

Redaction paths are declared verbatim by the request config — nothing is
inferred, guessed, or detected via NLP/AI PII heuristics. Only paths
explicitly listed in `privacy.redact_fields` are ever touched.

An absent path in a given row is ignored for that row (not an error) — a
field that simply wasn't present (e.g. an optional stream that didn't
match at this timestamp) isn't a redaction failure. A structurally
invalid path (empty, or with an empty segment) is caught earlier, at
config-validation time, as INVALID_REDACTION_PATH (see CleaningService) —
by the time a rule runs, every path is at least well-formed.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.cleaning.rules.base import CleaningRule, RedactionRecord, RuleContext, RuleOutcome
from app.cleaning.rules.common import path_exists

FIELD_REDACTED = "FIELD_REDACTED"


@dataclass(frozen=True)
class PrivacyRedactionRule(CleaningRule):
    fields: tuple[str, ...]
    code: str = FIELD_REDACTED

    def evaluate(self, row: dict, *, context: RuleContext) -> RuleOutcome:
        redactions = [
            RedactionRecord(code=self.code, field=path) for path in self.fields if path_exists(row, path)
        ]
        return RuleOutcome(redactions=redactions)
