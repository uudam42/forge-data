"""Coverage-based cleaning rules: required streams, minimum modality
coverage, and the optional all-optional-missing rule.

A stream's "payload" in a synchronized row counts as present when its
value is not null — Step 5 already sets a stream to null whenever nothing
matched, so presence here is a simple, deterministic null check. This is
NOT a judgment about whether present data is *plausible* (that's Step 3)
or whether the *dataset's* overall coverage distribution is acceptable
(that's Step 8) — Step 6 only applies the explicit thresholds it's given.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.cleaning.rules.base import CleaningRule, DropReason, RuleContext, RuleOutcome

MISSING_REQUIRED_STREAM = "MISSING_REQUIRED_STREAM"
INSUFFICIENT_MODALITY_COVERAGE = "INSUFFICIENT_MODALITY_COVERAGE"
ALL_OPTIONAL_STREAMS_MISSING = "ALL_OPTIONAL_STREAMS_MISSING"


def _is_present(row: dict, stream_name: str) -> bool:
    streams = row.get("streams")
    return isinstance(streams, dict) and streams.get(stream_name) is not None


@dataclass(frozen=True)
class RequiredStreamsRule(CleaningRule):
    required_streams: tuple[str, ...]
    code: str = MISSING_REQUIRED_STREAM

    def evaluate(self, row: dict, *, context: RuleContext) -> RuleOutcome:
        missing = [name for name in self.required_streams if not _is_present(row, name)]
        if not missing:
            return RuleOutcome()
        return RuleOutcome(drop_reasons=[DropReason(code=self.code, stream=name) for name in missing])


@dataclass(frozen=True)
class MinPresentStreamsRule(CleaningRule):
    min_present_streams: int
    known_streams: tuple[str, ...]
    code: str = INSUFFICIENT_MODALITY_COVERAGE

    def evaluate(self, row: dict, *, context: RuleContext) -> RuleOutcome:
        present_count = sum(1 for name in self.known_streams if _is_present(row, name))
        if present_count >= self.min_present_streams:
            return RuleOutcome()
        return RuleOutcome(drop_reasons=[DropReason(code=self.code)])


@dataclass(frozen=True)
class AllOptionalMissingRule(CleaningRule):
    """Drops a row only when every REQUIRED stream is present (a missing
    required stream is RequiredStreamsRule's job, evaluated first) but
    every OPTIONAL stream is absent. Off by default — this is a
    deliberate policy choice (drop_if_all_optional_streams_missing), never
    an automatic inference.
    """

    required_streams: tuple[str, ...]
    optional_streams: tuple[str, ...]
    code: str = ALL_OPTIONAL_STREAMS_MISSING

    def evaluate(self, row: dict, *, context: RuleContext) -> RuleOutcome:
        if not self.optional_streams:
            return RuleOutcome()
        if not all(_is_present(row, name) for name in self.required_streams):
            return RuleOutcome()
        if all(not _is_present(row, name) for name in self.optional_streams):
            return RuleOutcome(drop_reasons=[DropReason(code=self.code)])
        return RuleOutcome()
