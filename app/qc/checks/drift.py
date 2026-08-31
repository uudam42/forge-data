"""Baseline drift comparison — a limited, explicit, opt-in mechanism.

A baseline QC report is NEVER auto-selected: the caller must supply an
explicit `baseline_qc_id` (see README "Explicit baseline selection" — this
is important for reproducibility). Comparison uses a simple, fully
deterministic standardized mean difference:

    (current_mean - baseline_mean) / baseline_std

No hypothesis testing, no learned drift detector. When baseline_std == 0
the standardized measure is undefined; rather than divide by zero or
produce a non-finite score, an unequal mean against a zero-variance
baseline is reported with `standardized_mean_difference=None` and
`reason="baseline_std_zero_mean_shifted"`, and is treated as drifted
whenever drift checking is enabled (any shift away from a perfectly
constant baseline is a real, if unstandardized, change).

Missing features are never fabricated: a feature absent from the baseline
(or from the current run) is reported as `compared=False` with a reason,
never silently skipped without a trace.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.qc.models import DriftConfig, DriftFeatureResult, DriftSummary, ProfileRef, QCErrorCode, QCIssue, Severity
from app.storage.qc_store import QCReportStore


class BaselineNotFoundError(Exception):
    pass


def load_baseline(qc_store: QCReportStore, baseline_qc_id: str) -> tuple[dict, dict]:
    """Returns (baseline_manifest, baseline_report) dicts."""
    manifest = qc_store.find_manifest_by_qc_id(baseline_qc_id)
    if manifest is None:
        raise BaselineNotFoundError(f"No QC run found with qc_id='{baseline_qc_id}'")
    report_path = Path(manifest["report_uri"].replace("file://", ""))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return manifest, report


def _standardized_mean_difference(
    current_mean: float, baseline_mean: float, baseline_std: float
) -> tuple[float | None, str | None]:
    if baseline_std == 0:
        if current_mean == baseline_mean:
            return 0.0, None
        return None, "baseline_std_zero_mean_shifted"
    return (current_mean - baseline_mean) / baseline_std, None


def evaluate_drift(
    *,
    current_features: dict[str, dict],
    baseline_manifest: dict,
    baseline_report: dict,
    baseline_qc_id: str,
    current_profile: ProfileRef,
    drift_config: DriftConfig | None,
) -> tuple[DriftSummary, list[QCIssue]]:
    issues: list[QCIssue] = []

    baseline_profile = baseline_manifest.get("profile", {})
    compatible = (
        baseline_profile.get("name") == current_profile.name
        and baseline_profile.get("version") == current_profile.version
    )
    incompatibility_reason = None
    if not compatible:
        incompatibility_reason = (
            f"Baseline profile {baseline_profile} does not match current profile "
            f"{{'name': {current_profile.name!r}, 'version': {current_profile.version!r}}}."
        )
        issues.append(
            QCIssue(
                code=QCErrorCode.BASELINE_INCOMPATIBLE.value,
                severity=Severity.WARNING,
                message=incompatibility_reason,
            )
        )

    baseline_features = baseline_report.get("features", {})
    results: dict[str, DriftFeatureResult] = {}

    for path, current_stats in current_features.items():
        current_mean = current_stats.get("mean")
        if current_mean is None:
            results[path] = DriftFeatureResult(compared=False, reason="missing_in_current")
            continue

        baseline_stats = baseline_features.get(path)
        if baseline_stats is None or baseline_stats.get("mean") is None:
            results[path] = DriftFeatureResult(compared=False, reason="missing_in_baseline")
            continue

        baseline_mean = baseline_stats["mean"]
        baseline_std = baseline_stats.get("std") or 0.0
        current_std = current_stats.get("std")
        smd, reason = _standardized_mean_difference(current_mean, baseline_mean, baseline_std)

        results[path] = DriftFeatureResult(
            compared=True,
            baseline_mean=baseline_mean,
            baseline_std=baseline_std,
            current_mean=current_mean,
            current_std=current_std,
            standardized_mean_difference=smd,
            reason=reason,
        )

        if drift_config is not None and drift_config.enabled:
            drifted = (smd is not None and abs(smd) > drift_config.max_abs_standardized_mean_difference) or (
                reason == "baseline_std_zero_mean_shifted"
            )
            if drifted:
                issues.append(
                    QCIssue(
                        code=QCErrorCode.FEATURE_DISTRIBUTION_DRIFT.value,
                        severity=drift_config.severity,
                        path=path,
                        observed=smd,
                        threshold=drift_config.max_abs_standardized_mean_difference,
                        message=f"Feature '{path}' drifted from baseline (standardized mean difference={smd}).",
                    )
                )

    summary = DriftSummary(
        baseline_qc_id=baseline_qc_id,
        compatible=compatible,
        features=results,
        incompatibility_reason=incompatibility_reason,
    )
    return summary, issues
