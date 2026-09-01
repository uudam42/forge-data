"""v2.4 — a higher-volume stress pass mixing artifact registration, edge
registration, and dataset-version races across many real processes and
rounds, ending with a full database-consistency check. Slower than the
rest of the concurrency suite by design; still bounded and deterministic
enough to run in CI (~tens of seconds, not minutes)."""

from __future__ import annotations

import pytest

from app.storage.catalog_store import get_connection

from .helpers import create_dataset_worker, register_artifact_worker, register_version_worker, run_workers

pytestmark = pytest.mark.concurrency

_ROUNDS = 8
_PROCS_PER_ROUND = 4


def test_many_rounds_of_mixed_concurrent_writers_leave_a_consistent_catalog(tmp_path):
    db_path = str(tmp_path / "catalog" / "catalog.db")
    run_workers(create_dataset_worker, [(db_path, "stress_dataset")])

    for round_index in range(_ROUNDS):
        artifact_results = run_workers(
            register_artifact_worker,
            [
                (db_path, "ingestion", f"ing_round{round_index}_{i}", f"{round_index}_{i}" * 8)
                for i in range(_PROCS_PER_ROUND)
            ],
        )
        assert all(status == "ok" for status, _ in artifact_results), artifact_results

        version_results = run_workers(
            register_version_worker,
            [(db_path, "stress_dataset", f"1.{round_index}.0", "pkg_stress") for _ in range(_PROCS_PER_ROUND)],
        )
        statuses = [r[0] for r in version_results]
        assert statuses == ["ok"] * _PROCS_PER_ROUND, version_results
        outcomes = [r[1] for r in version_results]
        assert outcomes.count("created") == 1, outcomes

    conn = get_connection(db_path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == _ROUNDS * _PROCS_PER_ROUND
    assert conn.execute("SELECT COUNT(*) FROM dataset_versions").fetchone()[0] == _ROUNDS
    conn.close()
