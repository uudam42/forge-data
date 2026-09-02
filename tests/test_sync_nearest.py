"""Unit tests for NearestAlignmentStrategy and StreamCursor."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.synchronization.strategies.base import AlignmentContext, StreamCursor
from app.synchronization.strategies.nearest import NearestAlignmentStrategy
from app.validation.schemas.registry import SchemaRegistry
from app.core.config import _default_schema_dir

SCHEMA_DIR = _default_schema_dir()


@pytest.fixture
def imu_schema():
    registry = SchemaRegistry(schema_dir=SCHEMA_DIR)
    return registry.get(schema_name="imu", schema_version="1.0.0")


def _cursor(samples: list[tuple[int, dict]]) -> StreamCursor:
    return StreamCursor(iter((i, epoch_us, record) for i, (epoch_us, record) in enumerate(samples, start=1)))


def _context(target_us: int, tolerance_us: int, schema) -> AlignmentContext:
    return AlignmentContext(target_epoch_us=target_us, tolerance_us=tolerance_us, schema=schema)


def test_nearest_chooses_closest_sample(imu_schema) -> None:
    # target 10.000s; samples 9.980s and 10.040s -> delta 20ms vs 40ms -> choose 9.980s
    cursor = _cursor([(9_980_000, {"v": "a"}), (10_040_000, {"v": "b"})])
    strategy = NearestAlignmentStrategy()
    context = _context(10_000_000, tolerance_us=1_000_000, schema=imu_schema)

    cursor.advance_to(context.target_epoch_us)
    record, outcome = strategy.align(cursor, context)

    assert record == {"v": "a"}
    assert outcome.matched is True
    assert outcome.delta_ms == pytest.approx(20.0)


def test_nearest_deterministic_tie_chooses_earlier_sample(imu_schema) -> None:
    # target exactly between two samples -> tie -> prefer earlier
    cursor = _cursor([(9_000_000, {"v": "earlier"}), (11_000_000, {"v": "later"})])
    strategy = NearestAlignmentStrategy()
    context = _context(10_000_000, tolerance_us=5_000_000, schema=imu_schema)

    cursor.advance_to(context.target_epoch_us)
    record, outcome = strategy.align(cursor, context)

    assert record == {"v": "earlier"}
    assert outcome.delta_ms == pytest.approx(1000.0)


def test_nearest_rejects_sample_outside_tolerance(imu_schema) -> None:
    cursor = _cursor([(0, {"v": "a"})])
    strategy = NearestAlignmentStrategy()
    context = _context(10_000_000, tolerance_us=100_000, schema=imu_schema)  # 100ms tolerance, 10s away

    cursor.advance_to(context.target_epoch_us)
    record, outcome = strategy.align(cursor, context)

    assert record is None
    assert outcome.matched is False
    assert outcome.reason == "OUTSIDE_TOLERANCE"


def test_nearest_exact_timestamp_match(imu_schema) -> None:
    cursor = _cursor([(5_000_000, {"v": "a"}), (10_000_000, {"v": "b"}), (15_000_000, {"v": "c"})])
    strategy = NearestAlignmentStrategy()
    context = _context(10_000_000, tolerance_us=1000, schema=imu_schema)

    cursor.advance_to(context.target_epoch_us)
    record, outcome = strategy.align(cursor, context)

    assert record == {"v": "b"}
    assert outcome.delta_ms == 0.0
    assert outcome.is_exact is True


def test_nearest_at_tolerance_boundary_matches_inclusive(imu_schema) -> None:
    cursor = _cursor([(0, {"v": "a"})])
    strategy = NearestAlignmentStrategy()
    context = _context(100_000, tolerance_us=100_000, schema=imu_schema)  # exactly at tolerance

    cursor.advance_to(context.target_epoch_us)
    record, outcome = strategy.align(cursor, context)

    assert record == {"v": "a"}
    assert outcome.matched is True


def test_nearest_with_no_samples_at_all_is_unmatched(imu_schema) -> None:
    cursor = StreamCursor(iter([]))
    strategy = NearestAlignmentStrategy()
    context = _context(0, tolerance_us=1_000_000, schema=imu_schema)

    cursor.advance_to(context.target_epoch_us)
    record, outcome = strategy.align(cursor, context)

    assert record is None
    assert outcome.matched is False
    assert outcome.reason == "NO_DATA"


def test_cursor_advances_forward_across_successive_targets(imu_schema) -> None:
    """Confirms the two-pointer walk never needs to look backward — a
    sequence of increasing targets correctly tracks prev/pending forward."""
    cursor = _cursor([(0, {"v": 0}), (1_000_000, {"v": 1}), (2_000_000, {"v": 2}), (3_000_000, {"v": 3})])
    strategy = NearestAlignmentStrategy()

    results = []
    for target in (0, 1_000_000, 2_000_000, 3_000_000):
        context = _context(target, tolerance_us=100, schema=imu_schema)
        cursor.advance_to(target)
        record, outcome = strategy.align(cursor, context)
        results.append(record["v"])

    assert results == [0, 1, 2, 3]
