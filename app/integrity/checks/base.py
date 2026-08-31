"""Shared integrity-check primitives: parsing helpers, issue accumulation,
and the IntegrityChecker interface.

Checkers consume a stream of already-parsed (record_number, record) pairs
regardless of source format. CSV cells arrive as strings and JSON/JSONL
values are native, but to_float() / parse_timestamp() normalize both, so a
checker never needs to know or care which file format it's reading — that
concern lives entirely in app.integrity.records.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Iterator

from app.integrity.models import IntegrityIssue, IntegritySeverity


def to_float(value: object) -> float | None:
    """Best-effort numeric parse. Returns None for missing/empty/non-numeric.

    Integrity checks only ever run against data that already passed Step 2
    schema validation, so type-correctness for declared numeric fields is
    already guaranteed — this exists only to bridge CSV's string
    representation and JSON's native numbers through one code path, not to
    re-validate types Step 2 already checked.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp string; None if absent or unparsable.

    Step 2 already confirmed timezone-aware ISO-8601 format for any record
    that reaches integrity checking, so a parse failure here is treated as
    "nothing to compare" rather than raised as an error.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class IntegrityIssueAccumulator:
    """Collects integrity issues with a cap, mirroring Step 2's ErrorAccumulator.

    Unlike Step 2 (separate errors/warnings lists), Step 3 persists one
    unified `issues` list with a per-issue severity, so both severities
    share the same truncation budget: once `max_issues` detailed issues have
    been stored, further issues still increment error_count/warning_count
    (so counts stay accurate) but are no longer appended to `issues`.
    """

    max_issues: int
    issues: list[IntegrityIssue] = field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    issues_truncated: bool = False

    def add(self, issue: IntegrityIssue) -> None:
        if issue.severity is IntegritySeverity.ERROR:
            self.error_count += 1
        else:
            self.warning_count += 1

        if len(self.issues) < self.max_issues:
            self.issues.append(issue)
        else:
            self.issues_truncated = True

    def add_all(self, issues: Iterable[IntegrityIssue]) -> None:
        for issue in issues:
            self.add(issue)


@dataclass
class IntegrityRecordCounts:
    total_records: int = 0
    checked_records: int = 0
    passed_records: int = 0
    failed_records: int = 0


class IntegrityChecker(ABC):
    """A schema-specific set of semantic/plausibility checks.

    Consumes a stream of (record_number, record) pairs — already immune to
    source format — and reports issues via the shared accumulator. A record
    is "failed" if it produced at least one ERROR-severity issue; warnings
    alone do not fail a record (they can still make the overall report
    `passed_with_warnings`, decided by IntegrityService).
    """

    @abstractmethod
    def check_stream(
        self, records: Iterator[tuple[int, dict]], accumulator: IntegrityIssueAccumulator
    ) -> IntegrityRecordCounts:
        raise NotImplementedError
