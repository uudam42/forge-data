"""Exact duplicate-row detection.

Two rows are duplicates only if their canonical (timestamp + streams)
content is identical — the `alignment` diagnostic block (matched/method/
delta_ms) is deliberately excluded from the identity hash: two rows
describing the same observation are still duplicates regardless of how
they happened to be aligned. Timestamp IS part of the identity — two rows
with identical sensor values but different timestamps are never
duplicates (see canonical_row_key()).

Streaming-friendly by construction: state is a dict of
{content_hash: first_row_index}, never full row content, so memory is
O(number of unique rows) — see README for the documented limitation
(no disk-backed dedupe in this MVP).

Never uses Python's built-in hash() — it is not stable across processes
and would make cleaned output non-reproducible.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.cleaning.rules.base import CleaningRule, DropReason, RuleContext, RuleOutcome
from app.cleaning.rules.common import canonical_json

DUPLICATE_ROW = "DUPLICATE_ROW"


def canonical_row_key(row: dict) -> str:
    payload = {"timestamp": row.get("timestamp"), "streams": row.get("streams")}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass
class DuplicateRowRule(CleaningRule):
    code: str = DUPLICATE_ROW
    _seen: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def evaluate(self, row: dict, *, context: RuleContext) -> RuleOutcome:
        key = canonical_row_key(row)
        first_index = self._seen.get(key)
        if first_index is None:
            self._seen[key] = context.row_index
            return RuleOutcome()
        return RuleOutcome(
            drop_reasons=[DropReason(code=self.code, duplicate_of_row_index=first_index)]
        )
