"""Deterministic split assignment: maps each whole GROUP to exactly one
split. A splitter never sees individual samples — only group identities
(and, for `sequential`, each group's sample count) — so it structurally
cannot break a leakage group apart.
"""

from __future__ import annotations

from app.packaging.serialization import group_split_fraction

SUPPORTED_STRATEGIES = ("group_hash", "sequential")


class UnsupportedSplitStrategyError(Exception):
    pass


def assign_group_hash_splits(
    group_ids: list[str],
    *,
    train_ratio: float,
    validation_ratio: float,
    test_ratio: float,
    seed: int,
    profile_name: str,
    profile_version: str,
) -> dict[str, str]:
    """Each group's fraction depends only on its own group_id + seed +
    profile identity — never on any other group or the total group count.
    This is what makes assignments stable as new, unrelated groups are
    added later (see README "Stability under dataset growth"): packaging
    {A,B,C} and later {A,B,C,D} with the same config never reshuffles A/B/C."""
    assignment: dict[str, str] = {}
    for group_id in group_ids:
        fraction = group_split_fraction(
            group_id=group_id, seed=seed, profile_name=profile_name, profile_version=profile_version
        )
        if fraction < train_ratio:
            assignment[group_id] = "train"
        elif fraction < train_ratio + validation_ratio:
            assignment[group_id] = "validation"
        else:
            assignment[group_id] = "test"
    return assignment


def assign_sequential_splits(
    group_order: list[tuple[str, int]],
    *,
    train_ratio: float,
    validation_ratio: float,
    test_ratio: float,
) -> dict[str, str]:
    """Greedy prefix-fill over groups in first-appearance order: keeps
    filling `train` until its sample-count target is reached, then
    `validation`, then `test`. Operates on whole groups, never individual
    samples — a group is never split mid-way through."""
    total_samples = sum(count for _, count in group_order)
    targets = {
        "train": train_ratio * total_samples,
        "validation": validation_ratio * total_samples,
        "test": test_ratio * total_samples,
    }
    order = ["train", "validation", "test"]
    assignment: dict[str, str] = {}
    idx = 0
    used_in_current = 0
    for group_id, count in group_order:
        while idx < len(order) - 1 and targets[order[idx]] > 0 and used_in_current >= targets[order[idx]]:
            idx += 1
            used_in_current = 0
        assignment[group_id] = order[idx]
        used_in_current += count
    return assignment


def assign_splits(
    strategy: str,
    *,
    group_ids_in_order: list[str],
    group_sample_counts: dict[str, int],
    train_ratio: float,
    validation_ratio: float,
    test_ratio: float,
    seed: int,
    profile_name: str,
    profile_version: str,
) -> dict[str, str]:
    if strategy == "group_hash":
        return assign_group_hash_splits(
            group_ids_in_order,
            train_ratio=train_ratio,
            validation_ratio=validation_ratio,
            test_ratio=test_ratio,
            seed=seed,
            profile_name=profile_name,
            profile_version=profile_version,
        )
    if strategy == "sequential":
        ordered = [(gid, group_sample_counts[gid]) for gid in group_ids_in_order]
        return assign_sequential_splits(
            ordered, train_ratio=train_ratio, validation_ratio=validation_ratio, test_ratio=test_ratio
        )
    raise UnsupportedSplitStrategyError(f"Unsupported split strategy '{strategy}'")
