"""Unit tests for coverage-based cleaning rules: required streams, minimum
present streams, and the optional all-optional-missing rule.
"""

from __future__ import annotations

from app.cleaning.rules.base import RuleContext
from app.cleaning.rules.coverage import AllOptionalMissingRule, MinPresentStreamsRule, RequiredStreamsRule

_CTX = RuleContext(row_index=1)


def _row(**streams) -> dict:
    return {"timestamp": "2026-08-30T18:00:00Z", "streams": streams}


def test_required_stream_present_keeps_row() -> None:
    rule = RequiredStreamsRule(required_streams=("imu",))
    outcome = rule.evaluate(_row(imu={"accel_x": 0.1}, gps=None), context=_CTX)
    assert outcome.should_drop is False


def test_missing_required_stream_drops_row() -> None:
    rule = RequiredStreamsRule(required_streams=("imu",))
    outcome = rule.evaluate(_row(imu=None, gps={"latitude": 1.0}), context=_CTX)
    assert outcome.should_drop is True
    assert outcome.drop_reasons[0].code == "MISSING_REQUIRED_STREAM"
    assert outcome.drop_reasons[0].stream == "imu"


def test_optional_missing_stream_does_not_drop_by_default() -> None:
    # gps is not in required_streams -> its absence must not trigger a drop.
    rule = RequiredStreamsRule(required_streams=("imu",))
    outcome = rule.evaluate(_row(imu={"accel_x": 0.1}, gps=None), context=_CTX)
    assert outcome.should_drop is False


def test_multiple_missing_required_streams_all_reported() -> None:
    rule = RequiredStreamsRule(required_streams=("imu", "gps"))
    outcome = rule.evaluate(_row(imu=None, gps=None), context=_CTX)
    assert {r.stream for r in outcome.drop_reasons} == {"imu", "gps"}


def test_min_present_streams_keeps_row_at_threshold() -> None:
    rule = MinPresentStreamsRule(min_present_streams=2, known_streams=("imu", "gps", "camera"))
    outcome = rule.evaluate(_row(imu={"a": 1}, gps={"b": 2}, camera=None), context=_CTX)
    assert outcome.should_drop is False


def test_min_present_streams_drops_row_below_threshold() -> None:
    rule = MinPresentStreamsRule(min_present_streams=2, known_streams=("imu", "gps", "camera"))
    outcome = rule.evaluate(_row(imu={"a": 1}, gps=None, camera=None), context=_CTX)
    assert outcome.should_drop is True
    assert outcome.drop_reasons[0].code == "INSUFFICIENT_MODALITY_COVERAGE"


def test_drop_if_all_optional_streams_missing_works() -> None:
    rule = AllOptionalMissingRule(required_streams=("imu",), optional_streams=("gps", "camera"))
    outcome = rule.evaluate(_row(imu={"a": 1}, gps=None, camera=None), context=_CTX)
    assert outcome.should_drop is True
    assert outcome.drop_reasons[0].code == "ALL_OPTIONAL_STREAMS_MISSING"


def test_all_optional_missing_rule_keeps_row_when_one_optional_present() -> None:
    rule = AllOptionalMissingRule(required_streams=("imu",), optional_streams=("gps", "camera"))
    outcome = rule.evaluate(_row(imu={"a": 1}, gps={"b": 2}, camera=None), context=_CTX)
    assert outcome.should_drop is False


def test_all_optional_missing_rule_defers_to_required_check_when_required_missing() -> None:
    # If IMU itself is missing, AllOptionalMissingRule must not also fire —
    # that's RequiredStreamsRule's job (evaluated first by the policy).
    rule = AllOptionalMissingRule(required_streams=("imu",), optional_streams=("gps",))
    outcome = rule.evaluate(_row(imu=None, gps=None), context=_CTX)
    assert outcome.should_drop is False


def test_all_optional_missing_rule_is_off_by_default_via_policy() -> None:
    from app.cleaning.models import CleaningConfig
    from app.cleaning.policies.default import DEFAULT_MULTIMODAL_V1

    config = CleaningConfig(required_streams=["imu"])  # drop_if_all_optional_streams_missing defaults to False
    rules = DEFAULT_MULTIMODAL_V1.build_rules(config, known_streams=["imu", "gps"])
    assert not any(isinstance(r, AllOptionalMissingRule) for r in rules)
