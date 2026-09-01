"""The v2.5 flagship end-to-end scenario: a bad IMU normalization is
discovered, marked invalid, its downstream impact is analyzed, new work
through the bad branch is blocked, a corrected normalization is created,
a selective rebuild plan is built and executed reusing the untouched GPS
branch, and a corrected dataset version is registered -- all while the
original branch and dataset version remain fully intact and inspectable.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.v25_helpers import (
    CLEANING_CONFIG,
    PACKAGING_CONFIG,
    QC_CONFIG,
    TRANSFORMATION_CONFIG,
    build_full_pipeline,
    create_dataset_and_version,
)


def test_flagship_bad_normalization_selective_rebuild(client: TestClient) -> None:
    session_id = "sess_flagship_v25"
    pipeline = build_full_pipeline(client, session_id)
    imu_norm_id = pipeline["imu"]["normalization"]["normalization_id"]
    gps_norm_id = pipeline["gps"]["normalization"]["normalization_id"]
    old_sync_id = pipeline["sync"]["synchronization_id"]
    old_cleaning_id = pipeline["cleaned"]["cleaning_id"]
    old_xform_id = pipeline["xform"]["transformation_id"]
    old_qc_id = pipeline["qc"]["qc_id"]
    old_package_id = pipeline["pkg"]["package_id"]

    scan_resp = client.post("/api/v1/catalog/scan")
    assert scan_resp.status_code == 200, scan_resp.text

    version = create_dataset_and_version(client, dataset_name="flagship_fleet", version="1.0.0", package_id=old_package_id)
    assert version["effective_status"] == "healthy"

    # --- 1. Discover the bad IMU normalization, mark it invalid -----
    invalidate_resp = client.post(
        f"/api/v1/catalog/artifacts/normalization/{imu_norm_id}/invalidate",
        json={"reason": "normalization profile applied wrong gyro conversion"},
    )
    assert invalidate_resp.status_code == 200, invalidate_resp.text
    assert invalidate_resp.json()["state"] == "invalid"

    # --- 2. Enriched impact finds every downstream descendant --------
    impact_resp = client.get(f"/api/v1/lineage/normalization/{imu_norm_id}/impact/enriched")
    assert impact_resp.status_code == 200, impact_resp.text
    impact = impact_resp.json()
    assert impact["source_governance_state"] == "invalid"
    assert impact["affected_artifacts"]["synchronization"] == 1
    assert impact["affected_artifacts"]["cleaning"] == 1
    assert impact["affected_artifacts"]["transformation"] == 1
    assert impact["affected_artifacts"]["qc"] == 1
    assert impact["affected_artifacts"]["package"] == 1
    assert impact["affected_packages"] == [old_package_id]
    assert len(impact["affected_dataset_versions"]) == 1
    affected_version = impact["affected_dataset_versions"][0]
    assert affected_version["dataset_name"] == "flagship_fleet"
    assert affected_version["version"] == "1.0.0"
    assert affected_version["effective_status"] == "affected"

    # --- 3. New downstream work through the bad branch is blocked ----
    # Direct: a brand new synchronization using the invalid normalization directly.
    blocked_direct = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [{"name": "imu", "normalization_id": imu_norm_id}, {"name": "gps", "normalization_id": gps_norm_id}],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 400},
        },
    )
    assert blocked_direct.status_code == 409, blocked_direct.text
    assert blocked_direct.json()["detail"]["code"] == "ARTIFACT_INVALID"

    # Transitive: a new cleaning run against the OLD sync, which is itself
    # active but has the now-invalid IMU normalization as an ancestor.
    blocked_transitive = client.post(
        f"/api/v1/cleaning/{old_sync_id}",
        json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": CLEANING_CONFIG},
    )
    assert blocked_transitive.status_code == 409, blocked_transitive.text
    assert blocked_transitive.json()["detail"]["code"] == "UPSTREAM_ARTIFACT_INVALID"

    # Registering ANY new version against the (ancestor-invalid) old package
    # is blocked too, even under a fresh version number (Design Requirement 33).
    blocked_registration = client.post(
        "/api/v1/datasets/flagship_fleet/versions", json={"version": "1.0.1", "package_id": old_package_id}
    )
    assert blocked_registration.status_code == 409, blocked_registration.text
    assert blocked_registration.json()["detail"]["code"] == "UPSTREAM_ARTIFACT_INVALID"

    # --- 4. Create a corrected IMU normalization (same ingestion) -----
    imu_ingestion_id = pipeline["imu"]["ingestion"]["ingestion_id"]
    corrected_norm_resp = client.post(
        f"/api/v1/normalization/{imu_ingestion_id}",
        json={
            "schema_name": "imu", "schema_version": "1.0.0", "profile_name": "imu_canonical", "profile_version": "1.0.0",
            "source_units": {"acceleration": "m/s^2", "angular_velocity": "rad/s"},
        },
    )
    assert corrected_norm_resp.status_code == 200, corrected_norm_resp.text
    new_imu_norm_id = corrected_norm_resp.json()["normalization_id"]
    assert new_imu_norm_id != imu_norm_id

    scan_resp2 = client.post("/api/v1/catalog/scan")
    assert scan_resp2.status_code == 200, scan_resp2.text

    # --- 5. Build the rebuild plan ------------------------------------
    plan_resp = client.post(
        "/api/v1/rebuild/plan",
        json={"replace": {"old_type": "normalization", "old_id": imu_norm_id, "new_type": "normalization", "new_id": new_imu_norm_id}},
    )
    assert plan_resp.status_code == 200, plan_resp.text
    plan = plan_resp.json()
    stages_in_plan = [s["stage_artifact_type"] for s in plan["steps"]]
    assert stages_in_plan == ["synchronization", "cleaning", "transformation", "qc", "package"]

    sync_step = plan["steps"][0]
    assert sync_step["manual_configuration_required"] is False
    parent_types = {p["artifact_type"] for p in sync_step["parents"]}
    assert parent_types == {"normalization"}
    imu_parent = next(p for p in sync_step["parents"] if p["original_id"] == imu_norm_id)
    gps_parent = next(p for p in sync_step["parents"] if p["original_id"] == gps_norm_id)
    assert imu_parent["replaced"] is True
    assert imu_parent["effective_id"] is None  # not known until execution
    assert gps_parent["replaced"] is False  # unaffected sibling branch reused unchanged
    assert gps_parent["effective_id"] == gps_norm_id

    for step in plan["steps"][1:]:
        assert step["manual_configuration_required"] is True, step

    # --- 6. Execute the plan -------------------------------------------
    execute_resp = client.post(
        "/api/v1/rebuild/execute",
        json={
            "plan_id": plan["plan_id"],
            "configs": {
                f"cleaning/{old_cleaning_id}": CLEANING_CONFIG,
                f"transformation/{old_xform_id}": TRANSFORMATION_CONFIG,
                f"qc/{old_qc_id}": QC_CONFIG,
                f"package/{old_package_id}": PACKAGING_CONFIG,
            },
        },
    )
    assert execute_resp.status_code == 200, execute_resp.text
    execution = execute_resp.json()
    assert [r["status"] for r in execution["results"]] == ["rebuilt"] * 5
    new_ids = {r["stage_artifact_type"]: r["new_artifact_id"] for r in execution["results"]}
    assert new_ids["synchronization"] != old_sync_id
    assert new_ids["cleaning"] != old_cleaning_id
    assert new_ids["transformation"] != old_xform_id
    assert new_ids["qc"] != old_qc_id
    assert new_ids["package"] != old_package_id
    new_package_id = new_ids["package"]

    superseded_pairs = {(s["old_id"], s["new_id"]) for s in execution["superseded"]}
    assert (old_sync_id, new_ids["synchronization"]) in superseded_pairs
    assert (old_package_id, new_package_id) in superseded_pairs

    # The executor writes real filesystem artifacts via the existing
    # stage services (Design Requirement 18) but does not itself register
    # them into the catalog -- catalog population stays scan-driven,
    # exactly like every other stage in this system (see docs/DETAILED_GUIDE.md
    # v2.5 "Downstream gating" limitation). A scan is what makes them
    # queryable/governable.
    scan_resp3 = client.post("/api/v1/catalog/scan")
    assert scan_resp3.status_code == 200, scan_resp3.text

    # --- 7. Old lineage is completely unchanged -------------------------
    old_sync_after = client.get(f"/api/v1/catalog/artifacts/synchronization/{old_sync_id}").json()
    assert old_sync_after["artifact"]["content_sha256"] == pipeline["sync"]["synchronized_sha256"]
    old_pkg_governance = client.get(f"/api/v1/catalog/artifacts/synchronization/{old_sync_id}/governance").json()
    assert old_pkg_governance["state"] == "deprecated"
    assert old_pkg_governance["superseded_by_id"] == new_ids["synchronization"]

    # --- 8. Old dataset version remains immutable and now "affected" ---
    old_version_after = client.get("/api/v1/datasets/flagship_fleet/versions/1.0.0").json()
    assert old_version_after["package_id"] == old_package_id  # NEVER repointed
    assert old_version_after["effective_status"] == "affected"

    # --- 9. Register the corrected dataset version ----------------------
    new_version_resp = client.post("/api/v1/datasets/flagship_fleet/versions", json={"version": "1.1.0", "package_id": new_package_id})
    assert new_version_resp.status_code == 201, new_version_resp.text
    new_version = new_version_resp.json()
    assert new_version["package_id"] == new_package_id
    assert new_version["effective_status"] == "healthy"

    # --- 10. Recursive verification of the new lineage -------------------
    verify_resp = client.post(f"/api/v1/catalog/verify/package/{new_package_id}?recursive=true")
    assert verify_resp.status_code == 200, verify_resp.text
    verification = verify_resp.json()
    assert verification["status"] == "verified"
    assert verification["failed_nodes"] == 0
    assert verification["missing_nodes"] == 0

    # --- 11. Old and new lineage both remain independently inspectable ---
    old_lineage = client.get(f"/api/v1/lineage/package/{old_package_id}", params={"direction": "upstream"}).json()
    new_lineage = client.get(f"/api/v1/lineage/package/{new_package_id}", params={"direction": "upstream"}).json()
    old_ids = {n["artifact_id"] for n in old_lineage["nodes"]}
    new_ids_set = {n["artifact_id"] for n in new_lineage["nodes"]}
    assert imu_norm_id in old_ids and new_imu_norm_id not in old_ids
    assert new_imu_norm_id in new_ids_set and imu_norm_id not in new_ids_set
    assert gps_norm_id in old_ids and gps_norm_id in new_ids_set  # the untouched branch appears in BOTH
