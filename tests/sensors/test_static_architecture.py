"""Static architecture / extension-cost tests (Design Requirement 19;
test items 72-76). These are intentionally narrow, source-text checks —
not brittle to comments or docstrings changing wording, only to whether
the literal string "force_torque" (or "force-torque") appears in a
generic core module. A module gaining an unrelated comment never breaks
this; a module gaining an `if sensor_type == "force_torque"` branch
would.
"""

from __future__ import annotations

import inspect

from app.sensors.registry import get_default_registry

# Every module this project considers "generic pipeline core" -- i.e.
# code that must work identically no matter which sensor plugins exist.
# Deliberately excludes app/sensors/** (the plugin layer itself) and the
# three coordinator files that legitimately import app.sensors.registry
# to build their internal maps (integrity/registry.py,
# normalization/registry.py, transformation/profiles/multimodal_window.py)
# -- those three are the intended, minimal integration seam, not part of
# "core" in the sense this test defends.
_GENERIC_CORE_MODULES = []


def _collect_core_modules():
    if _GENERIC_CORE_MODULES:
        return _GENERIC_CORE_MODULES

    from app.synchronization import service as sync_service, alignment, timeline, metrics as sync_metrics
    from app.synchronization.strategies import nearest, linear, base as strategy_base
    from app.synchronization.clocks import correction
    from app.cleaning import service as cleaning_service, evaluator, metrics as cleaning_metrics
    from app.cleaning.rules import base as rule_base, coverage, duplicates, privacy, common as rule_common
    from app.transformation import service as xform_service, feature_engine, windowing, metrics as xform_metrics
    from app.transformation.features import base as feature_base, common as feature_common, statistics
    from app.qc import service as qc_service, accumulator, selectors, metrics as qc_metrics
    from app.qc.checks import distributions, drift, feature_completeness, variance, modality_coverage, dataset_size, identifiers, temporal, group_distribution
    from app.packaging import service as packaging_service, grouping, splitter, leakage, metrics as packaging_metrics
    from app.catalog import scanner, service as catalog_service, graph, verifier, models as catalog_models
    from app.storage import atomic, disk_preflight

    _GENERIC_CORE_MODULES.extend(
        [
            sync_service, alignment, timeline, sync_metrics, nearest, linear, strategy_base, correction,
            cleaning_service, evaluator, cleaning_metrics, rule_base, coverage, duplicates, privacy, rule_common,
            xform_service, feature_engine, windowing, xform_metrics, feature_base, feature_common, statistics,
            qc_service, accumulator, selectors, qc_metrics, distributions, drift, feature_completeness,
            variance, modality_coverage, dataset_size, identifiers, temporal, group_distribution,
            packaging_service, grouping, splitter, leakage, packaging_metrics,
            scanner, catalog_service, graph, verifier, catalog_models,
            atomic, disk_preflight,
        ]
    )
    return _GENERIC_CORE_MODULES


def test_all_three_builtin_plugins_are_listed() -> None:
    sensor_types = {p.sensor_type for p in get_default_registry().list_plugins()}
    assert sensor_types == {"imu", "gps", "force_torque"}


def test_generic_synchronization_contains_no_force_torque_branch() -> None:
    from app.synchronization import service, alignment, timeline
    from app.synchronization.strategies import nearest, linear
    from app.synchronization.clocks import correction

    for module in (service, alignment, timeline, nearest, linear, correction):
        source = inspect.getsource(module).lower()
        assert "force_torque" not in source and "force-torque" not in source


def test_generic_cleaning_contains_no_force_torque_branch() -> None:
    from app.cleaning import service, evaluator
    from app.cleaning.rules import base, coverage, duplicates, privacy

    for module in (service, evaluator, base, coverage, duplicates, privacy):
        source = inspect.getsource(module).lower()
        assert "force_torque" not in source and "force-torque" not in source


def test_generic_packaging_contains_no_force_torque_branch() -> None:
    from app.packaging import service, grouping, splitter, leakage

    for module in (service, grouping, splitter, leakage):
        source = inspect.getsource(module).lower()
        assert "force_torque" not in source and "force-torque" not in source


def test_generic_catalog_contains_no_force_torque_branch() -> None:
    from app.catalog import scanner, service, graph, verifier

    for module in (scanner, service, graph, verifier):
        source = inspect.getsource(module).lower()
        assert "force_torque" not in source and "force-torque" not in source


def test_extension_cost_no_force_torque_string_anywhere_in_full_generic_core() -> None:
    """The comprehensive version of the four checks above, run once
    across every generic-core module this project has (see
    _collect_core_modules) -- the single test to point to as proof of
    the v2.3 extension-cost claim."""
    offenders = []
    for module in _collect_core_modules():
        source = inspect.getsource(module).lower()
        if "force_torque" in source or "force-torque" in source:
            offenders.append(module.__name__)
    assert offenders == [], f"force_torque coupling found in generic core modules: {offenders}"
