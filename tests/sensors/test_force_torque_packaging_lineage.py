"""Force/Torque packaging, lineage, and reliability tests (Design
Requirements 15-17, 27; test items 56-68). Packaging and the catalog
never branch on sensor modality; lineage/reproducibility already capture
schema/profile identity generically -- these tests prove both."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.catalog.repository import CatalogRepository
from app.catalog.scanner import CatalogScanner
from app.storage.atomic import fault_injector
from app.storage.catalog_store import get_connection
from tests.sensors.pipeline_helpers import full_pipeline_to_package


class _InjectedCrash(Exception):
    pass


def _clear_injector():
    fault_injector.clear()


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def test_three_modality_package_deterministic_and_leakage_safe(client: TestClient, package_root: Path) -> None:
    result = full_pipeline_to_package(client, "sess_ft_pkg", seed=7)
    pkg = result["pkg"]
    assert pkg["status"] == "completed"
    assert pkg["summary"]["source_samples"] == pkg["summary"]["packaged_samples"]

    report = json.loads(Path(pkg["report_uri"].replace("file://", "")).read_text())
    assert report["leakage_checks"]["cross_split_groups"] == 0


def test_deterministic_splits_across_two_runs(client: TestClient) -> None:
    result_a = full_pipeline_to_package(client, "sess_ft_pkg_det_a", seed=99)
    result_b = full_pipeline_to_package(client, "sess_ft_pkg_det_b", seed=99)
    report_a = json.loads(Path(result_a["pkg"]["report_uri"].replace("file://", "")).read_text())
    report_b = json.loads(Path(result_b["pkg"]["report_uri"].replace("file://", "")).read_text())
    assert report_a["actual"] == report_b["actual"]


def test_package_output_framework_neutral_jsonl(client: TestClient, package_root: Path) -> None:
    result = full_pipeline_to_package(client, "sess_ft_pkg_neutral")
    pkg_dir = package_root / result["xform"]["transformation_id"] / result["pkg"]["package_id"]
    for name in ("train.jsonl", "validation.jsonl", "test.jsonl", "split_index.jsonl", "manifest.json", "report.json"):
        assert (pkg_dir / name).exists()


def test_no_force_torque_specific_branch_in_packaging_core() -> None:
    from app.packaging import service, grouping, splitter, leakage, metrics

    for module in (service, grouping, splitter, leakage, metrics):
        source = inspect.getsource(module)
        assert "force_torque" not in source.lower()


# ---------------------------------------------------------------------------
# Lineage / catalog
# ---------------------------------------------------------------------------


def test_plugin_identity_recorded_in_normalization_manifest(client: TestClient) -> None:
    result = full_pipeline_to_package(client, "sess_ft_lineage_identity")
    ft_norm = result["normalized"]["force_torque"]
    assert ft_norm["schema"] == {"name": "force_torque", "version": "1.0.0"}
    assert ft_norm["profile"] == {"name": "force_torque_canonical", "version": "1.0.0"}


def test_recursive_verification_covers_force_torque_lineage(client: TestClient, test_settings) -> None:
    result = full_pipeline_to_package(client, "sess_ft_lineage_verify")
    pkg_id = result["pkg"]["package_id"]

    rebuild = client.post("/api/v1/catalog/rebuild")
    assert rebuild.status_code == 200, rebuild.text

    verify = client.post(f"/api/v1/catalog/verify/package/{pkg_id}?recursive=true")
    assert verify.status_code == 200, verify.text
    body = verify.json()
    assert body["status"] == "verified"
    assert body["failed_nodes"] == 0
    assert body["missing_nodes"] == 0

    # The force_torque ingestion is reachable in the verified upstream set.
    ft_ingestion_id = result["normalized"]["force_torque"]["ingestion_id"]
    node_ids = {(n["artifact_type"], n["artifact_id"]) for n in body["nodes"]}
    assert ("ingestion", ft_ingestion_id) in node_ids


def test_reproducibility_fingerprint_stable_for_force_torque_pipeline(client: TestClient) -> None:
    client.post("/api/v1/datasets", json={"dataset_name": "ft_repro_test"})
    result_a = full_pipeline_to_package(client, "sess_ft_repro_a", seed=5)
    result_b = full_pipeline_to_package(client, "sess_ft_repro_b", seed=5)

    client.post("/api/v1/catalog/rebuild")
    client.post("/api/v1/datasets", json={"dataset_name": "ft_repro_a"})
    client.post("/api/v1/datasets", json={"dataset_name": "ft_repro_b"})
    v_a = client.post("/api/v1/datasets/ft_repro_a/versions", json={"version": "1.0.0", "package_id": result_a["pkg"]["package_id"]})
    v_b = client.post("/api/v1/datasets/ft_repro_b/versions", json={"version": "1.0.0", "package_id": result_b["pkg"]["package_id"]})
    assert v_a.status_code in (200, 201), v_a.text
    assert v_b.status_code in (200, 201), v_b.text
    assert v_a.json()["lineage_fingerprint"] == v_b.json()["lineage_fingerprint"]


def test_no_force_torque_specific_branch_in_catalog_core() -> None:
    from app.catalog import scanner, service, graph, verifier, models

    for module in (scanner, service, graph, verifier, models):
        source = inspect.getsource(module)
        assert "force_torque" not in source.lower()


# ---------------------------------------------------------------------------
# Reliability (v2.1 crash safety at the Force/Torque plugin)
# ---------------------------------------------------------------------------


def test_atomic_normalization_crash_leaves_no_partial_artifact(client: TestClient, normalized_root: Path, storage_root: Path) -> None:
    from tests.sensors.pipeline_helpers import FT_CSV, upload

    ing = upload(client, "ft.csv", FT_CSV, session_id="sess_ft_crash_norm")
    for path in (f"/api/v1/validation/{ing['ingestion_id']}", f"/api/v1/integrity/{ing['ingestion_id']}"):
        r = client.post(path, json={"schema_name": "force_torque", "schema_version": "1.0.0"})
        assert r.status_code == 200

    fault_injector.install("BEFORE_RENAME", lambda: (_ for _ in ()).throw(_InjectedCrash()))
    try:
        import pytest

        with pytest.raises(_InjectedCrash):
            client.post(
                f"/api/v1/normalization/{ing['ingestion_id']}",
                json={
                    "schema_name": "force_torque", "schema_version": "1.0.0",
                    "profile_name": "force_torque_canonical", "profile_version": "1.0.0",
                    "source_units": {"force": "N", "torque": "N*m"},
                },
            )
    finally:
        _clear_injector()

    ingestion_norm_dir = normalized_root / ing["ingestion_id"]
    committed = [d for d in ingestion_norm_dir.iterdir() if d.is_dir() and not d.name.startswith(".tmp-")] if ingestion_norm_dir.exists() else []
    assert committed == []

    # Upstream raw ingestion unchanged.
    raw_matches = list(storage_root.glob(f"*/*/{ing['ingestion_id']}/manifest.json"))
    assert len(raw_matches) == 1

    # Safely rerunnable.
    retry = client.post(
        f"/api/v1/normalization/{ing['ingestion_id']}",
        json={
            "schema_name": "force_torque", "schema_version": "1.0.0",
            "profile_name": "force_torque_canonical", "profile_version": "1.0.0",
            "source_units": {"force": "N", "torque": "N*m"},
        },
    )
    assert retry.status_code == 200, retry.text


def test_atomic_transformation_crash_leaves_no_partial_artifact(client: TestClient, transformed_root: Path) -> None:
    from tests.sensors.pipeline_helpers import clean, synchronize, three_sensor_normalized

    normalized = three_sensor_normalized(client, "sess_ft_crash_xform")
    sync = synchronize(client, normalized)
    cleaned = clean(client, sync["synchronization_id"])

    fault_injector.install("BEFORE_RENAME", lambda: (_ for _ in ()).throw(_InjectedCrash()))
    try:
        import pytest

        with pytest.raises(_InjectedCrash):
            client.post(
                f"/api/v1/transformation/{cleaned['cleaning_id']}",
                json={
                    "profile_name": "multimodal_window_v1", "profile_version": "1.0.0",
                    "config": {
                        "window": {"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True},
                        "features": {"force_torque": {"statistics": ["mean"], "derived": ["force_magnitude"]}},
                    },
                },
            )
    finally:
        _clear_injector()

    cleaning_xform_dir = transformed_root / cleaned["cleaning_id"]
    committed = [d for d in cleaning_xform_dir.iterdir() if d.is_dir() and not d.name.startswith(".tmp-")] if cleaning_xform_dir.exists() else []
    assert committed == []
