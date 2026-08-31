"""Cleaning metrics: counters + reason counts + capped detail examples.

`dropped_examples` and `redaction_examples` are each capped independently
at `max_detail_entries` (MAX_CLEANING_ISSUE_DETAILS); `reason_counts` keeps
counting every occurrence regardless of the cap — only the detailed
example objects stop accumulating once a list's cap is hit, and
`details_truncated` is set. Detailed reports focus on removed/redacted
rows only — retained, un-redacted rows are never individually recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.cleaning.rules.base import DropReason, RedactionRecord


@dataclass
class CleaningMetricsAccumulator:
    max_detail_entries: int
    input_rows: int = 0
    retained_rows: int = 0
    dropped_rows: int = 0
    redacted_rows: int = 0
    reason_counts: dict[str, int] = field(default_factory=dict)
    dropped_examples: list[dict] = field(default_factory=list)
    redaction_examples: list[dict] = field(default_factory=list)
    details_truncated: bool = False

    def _bump(self, code: str) -> None:
        self.reason_counts[code] = self.reason_counts.get(code, 0) + 1

    def record_kept(self, row_index: int, timestamp: object, redactions: list[RedactionRecord]) -> None:
        self.input_rows += 1
        self.retained_rows += 1
        if not redactions:
            return

        self.redacted_rows += 1
        for redaction in redactions:
            self._bump(redaction.code)

        if len(self.redaction_examples) < self.max_detail_entries:
            self.redaction_examples.append(
                {
                    "row_index": row_index,
                    "timestamp": timestamp,
                    "redactions": [{"code": r.code, "field": r.field} for r in redactions],
                }
            )
        else:
            self.details_truncated = True

    def record_dropped(self, row_index: int, timestamp: object, reasons: list[DropReason]) -> None:
        self.input_rows += 1
        self.dropped_rows += 1
        for reason in reasons:
            self._bump(reason.code)

        if len(self.dropped_examples) < self.max_detail_entries:
            self.dropped_examples.append(
                {
                    "row_index": row_index,
                    "timestamp": timestamp,
                    "reasons": [
                        {
                            "code": r.code,
                            "stream": r.stream,
                            "duplicate_of_row_index": r.duplicate_of_row_index,
                        }
                        for r in reasons
                    ],
                }
            )
        else:
            self.details_truncated = True

    @property
    def retention_ratio(self) -> float:
        return (self.retained_rows / self.input_rows) if self.input_rows else 0.0
