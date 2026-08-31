"""Unit tests for deterministic split assignment (app.packaging.splitter
and app.packaging.serialization.group_split_fraction)."""

from __future__ import annotations

from app.packaging.serialization import group_split_fraction
from app.packaging.splitter import (
    UnsupportedSplitStrategyError,
    assign_group_hash_splits,
    assign_sequential_splits,
    assign_splits,
)

import pytest

PROFILE = {"profile_name": "default_ml_package", "profile_version": "1.0.0"}


def _hash_split(group_ids, seed=42, train=0.7, validation=0.15, test=0.15):
    return assign_group_hash_splits(
        group_ids, train_ratio=train, validation_ratio=validation, test_ratio=test, seed=seed, **PROFILE
    )


def test_group_hash_deterministic() -> None:
    group_ids = [f"grp_{i}" for i in range(50)]
    a1 = _hash_split(group_ids)
    a2 = _hash_split(group_ids)
    assert a1 == a2


def test_same_seed_gives_same_assignments() -> None:
    group_ids = [f"grp_{i}" for i in range(50)]
    a1 = _hash_split(group_ids, seed=1)
    a2 = _hash_split(group_ids, seed=1)
    assert a1 == a2


def test_different_seed_can_change_assignments() -> None:
    group_ids = [f"grp_{i}" for i in range(50)]
    a1 = _hash_split(group_ids, seed=1)
    a2 = _hash_split(group_ids, seed=2)
    assert a1 != a2


def test_existing_groups_stable_when_unrelated_group_added() -> None:
    base_groups = [f"grp_{i}" for i in range(10)]
    before = _hash_split(base_groups)
    after = _hash_split(base_groups + ["grp_new"])
    for group_id in base_groups:
        assert before[group_id] == after[group_id]


def test_fraction_deterministic_and_bounded() -> None:
    for i in range(100):
        fraction = group_split_fraction(group_id=f"grp_{i}", seed=42, **PROFILE)
        assert 0.0 <= fraction < 1.0
        fraction2 = group_split_fraction(group_id=f"grp_{i}", seed=42, **PROFILE)
        assert fraction == fraction2


def test_fraction_changes_with_seed() -> None:
    f1 = group_split_fraction(group_id="grp_x", seed=1, **PROFILE)
    f2 = group_split_fraction(group_id="grp_x", seed=2, **PROFILE)
    assert f1 != f2


def test_zero_validation_ratio_allowed() -> None:
    # No group can ever be assigned "validation" when its fraction can
    # never fall in an empty [train_ratio, train_ratio) interval.
    result = _hash_split([f"grp_{i}" for i in range(20)], train=0.9, validation=0.0, test=0.1)
    for fraction_split in result.values():
        assert fraction_split in ("train", "test")


def test_zero_test_ratio_allowed() -> None:
    result = _hash_split([f"grp_{i}" for i in range(20)], train=0.9, validation=0.1, test=0.0)
    for fraction_split in result.values():
        assert fraction_split in ("train", "validation")


def test_split_stable_across_thousands_of_groups_statistically_close_to_ratio() -> None:
    group_ids = [f"grp_{i}" for i in range(5000)]
    result = _hash_split(group_ids, train=0.7, validation=0.15, test=0.15)
    counts = {"train": 0, "validation": 0, "test": 0}
    for v in result.values():
        counts[v] += 1
    assert abs(counts["train"] / 5000 - 0.7) < 0.05
    assert abs(counts["validation"] / 5000 - 0.15) < 0.05
    assert abs(counts["test"] / 5000 - 0.15) < 0.05


# ---------------------------------------------------------------------------
# Sequential strategy
# ---------------------------------------------------------------------------


def test_sequential_assigns_whole_groups_in_order() -> None:
    group_order = [("grp_0", 10), ("grp_1", 10), ("grp_2", 10), ("grp_3", 10)]
    result = assign_sequential_splits(group_order, train_ratio=0.5, validation_ratio=0.25, test_ratio=0.25)
    # First groups fill train until target (20 samples) reached.
    assert result["grp_0"] == "train"
    assert result["grp_1"] == "train"
    assert result["grp_2"] == "validation"
    assert result["grp_3"] == "test"


def test_sequential_never_splits_within_a_group() -> None:
    # Every group is assigned to exactly one split - trivially true since
    # assign_sequential_splits operates over whole (group_id, count) pairs.
    group_order = [("grp_0", 100)]
    result = assign_sequential_splits(group_order, train_ratio=1.0, validation_ratio=0.0, test_ratio=0.0)
    assert result == {"grp_0": "train"}


# ---------------------------------------------------------------------------
# assign_splits dispatch
# ---------------------------------------------------------------------------


def test_assign_splits_dispatches_group_hash() -> None:
    result = assign_splits(
        "group_hash",
        group_ids_in_order=["grp_0", "grp_1"],
        group_sample_counts={"grp_0": 5, "grp_1": 5},
        train_ratio=1.0,
        validation_ratio=0.0,
        test_ratio=0.0,
        seed=0,
        **PROFILE,
    )
    assert set(result.values()) == {"train"}


def test_assign_splits_unsupported_strategy_raises() -> None:
    with pytest.raises(UnsupportedSplitStrategyError):
        assign_splits(
            "bogus",
            group_ids_in_order=[],
            group_sample_counts={},
            train_ratio=1.0,
            validation_ratio=0.0,
            test_ratio=0.0,
            seed=0,
            **PROFILE,
        )


def test_no_arbitrary_random_behavior() -> None:
    """group_split_fraction must never depend on Python's hash() (randomized
    per-process for str) or on time/RNG — verified by checking the exact
    expected SHA-256-derived value for a known input."""
    import hashlib

    group_id, seed = "grp_test", 42
    payload = f"{group_id}:{seed}:{PROFILE['profile_name']}:{PROFILE['profile_version']}"
    expected_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    expected_fraction = int(expected_digest[:8], 16) / 0x1_0000_0000
    assert group_split_fraction(group_id=group_id, seed=seed, **PROFILE) == expected_fraction
