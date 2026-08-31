"""Aggregate split metrics for the packaging report: requested vs. actual
ratios at both the sample level and the group level, since a small number
of unevenly-sized groups can make sample ratios diverge from group ratios
— see README "Requested vs. actual ratios"."""

from __future__ import annotations

from app.packaging.models import SPLIT_NAMES, SplitStats


def compute_split_stats(
    *, split_sample_counts: dict[str, int], split_group_counts: dict[str, int], total_samples: int
) -> dict[str, SplitStats]:
    stats: dict[str, SplitStats] = {}
    for name in SPLIT_NAMES:
        samples = split_sample_counts.get(name, 0)
        groups = split_group_counts.get(name, 0)
        ratio = samples / total_samples if total_samples else 0.0
        stats[name] = SplitStats(samples=samples, groups=groups, sample_ratio=ratio)
    return stats
