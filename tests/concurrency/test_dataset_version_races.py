"""v2.4 — Demo B/C/D: dataset creation and dataset-version registration
races, resolved by DB constraints as final authority."""

from __future__ import annotations

import pytest

from app.storage.catalog_store import get_connection

from .helpers import create_dataset_worker, register_version_worker, run_workers

pytestmark = pytest.mark.concurrency


def test_concurrent_dataset_creation_same_name_is_idempotent(tmp_path):
    """Demo B: N processes racing to create the SAME dataset name --
    exactly one "created", the rest "already_existed", never a raw
    IntegrityError."""
    db_path = str(tmp_path / "catalog" / "catalog.db")
    results = run_workers(create_dataset_worker, [(db_path, "robot_fleet_a") for _ in range(6)])
    statuses = [r[0] for r in results]
    assert statuses == ["ok"] * 6, results
    outcomes = [r[1] for r in results]
    assert outcomes.count("created") == 1, outcomes
    assert outcomes.count("already_existed") == 5, outcomes

    conn = get_connection(db_path)
    assert conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 1
    conn.close()


def test_same_version_same_package_race_is_idempotent(tmp_path):
    """Demo D: N processes racing to register the SAME (dataset, version)
    pointing at the SAME package -- exactly one "created", the rest
    "unchanged" (idempotent re-registration), never an error."""
    db_path = str(tmp_path / "catalog" / "catalog.db")
    assert create_dataset_worker.__module__  # sanity import check
    run_workers(create_dataset_worker, [(db_path, "robot_fleet_b")])

    results = run_workers(
        register_version_worker,
        [(db_path, "robot_fleet_b", "1.0.0", "pkg_same") for _ in range(6)],
    )
    statuses = [r[0] for r in results]
    assert statuses == ["ok"] * 6, results
    outcomes = [r[1] for r in results]
    assert outcomes.count("created") == 1, outcomes
    assert outcomes.count("unchanged") == 5, outcomes

    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT package_id FROM dataset_versions WHERE dataset_name='robot_fleet_b' AND version='1.0.0'"
    ).fetchone()
    conn.close()
    assert row["package_id"] == "pkg_same"


def test_same_version_different_package_race_is_a_conflict(tmp_path):
    """Demo C: N processes racing to register the SAME (dataset, version)
    each pointing at a DIFFERENT package -- exactly one wins ("created"),
    every other process gets a structured DatasetVersionImmutableError,
    and the version is never silently reassigned."""
    db_path = str(tmp_path / "catalog" / "catalog.db")
    run_workers(create_dataset_worker, [(db_path, "robot_fleet_c")])

    results = run_workers(
        register_version_worker,
        [(db_path, "robot_fleet_c", "1.0.0", f"pkg_{i}") for i in range(5)],
    )
    statuses = [r[0] for r in results]
    assert statuses.count("ok") == 1, results
    assert statuses.count("DatasetVersionImmutableError") == 4, results

    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT package_id FROM dataset_versions WHERE dataset_name='robot_fleet_c' AND version='1.0.0'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1  # exactly one row ever exists for this (dataset, version)
