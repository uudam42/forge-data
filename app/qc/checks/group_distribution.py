"""Session/group imbalance check.

Deliberately NOT a standard QCCheck: session/group identity isn't part of
DatasetMetrics (transformed samples carry no per-sample session field —
only the transformation manifest's upstream.session_ids list does). The
service computes session_distribution from manifest lineage and passes it
here directly.

The current pipeline transforms one cleaning run originating from one
synchronized session — see README "Important single-session limitation."
This check is a no-op whenever fewer than two groups are known, so it
never manufactures a spurious imbalance finding for the (currently
universal) single-session case.
"""

from __future__ import annotations

from app.qc.models import QCConfig, QCErrorCode, QCIssue


def evaluate_group_imbalance(
    session_distribution: dict[str, int] | None, config: QCConfig
) -> list[QCIssue]:
    if not session_distribution or len(session_distribution) < 2:
        return []
    if config.max_group_fraction is None:
        return []

    total = sum(session_distribution.values())
    if total == 0:
        return []

    issues: list[QCIssue] = []
    for name, count in session_distribution.items():
        fraction = count / total
        if fraction > config.max_group_fraction:
            issues.append(
                QCIssue(
                    code=QCErrorCode.GROUP_IMBALANCE.value,
                    severity=config.group_imbalance_severity,
                    path=name,
                    observed=fraction,
                    threshold=config.max_group_fraction,
                    message=(
                        f"Group '{name}' accounts for {fraction:.4f} of samples, exceeding the "
                        f"configured maximum fraction of {config.max_group_fraction}."
                    ),
                )
            )
    return issues
