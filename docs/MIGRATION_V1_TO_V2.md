# Migrating from v1.0 to v2.0

This document describes exactly what happens when Forge Data v2.0 is pointed
at a workspace created by v1.0, based on testing against a real v1.0
workspace (created with v1.0's own code, running from a `git worktree`
checkout of the v1.0 commit) rather than freshly-generated v2 data. Nothing
in this document is a compatibility *claim* that wasn't exercised directly.

## Summary

**v2.0 reads v1.0 workspaces safely, with no destructive migration and no
required rebuild step**, as long as the workspace's on-disk location has not
changed. If the workspace directory has been *moved* since it was last
scanned, see [Relocated workspaces](#relocated-workspaces-the-one-real-caveat)
below.

## What is unchanged

- **Catalog schema.** The six tables that existed in v1.0
  (`artifacts`, `lineage_edges`, `lineage_issues`, `datasets`,
  `dataset_versions`, `catalog_metadata`) have byte-for-byte identical
  `CREATE TABLE` definitions in v2.0 — confirmed by diffing v1.0's schema
  source against v2.0's. v2.0 adds eight new tables (governance, pipeline
  runs, stage runs, run-to-artifact links, run events) via
  `CREATE TABLE IF NOT EXISTS`, executed on every connection open. No
  column was added, removed, or changed on a v1.0 table.
- **Artifact manifest format.** Manifests written by v1.0 are read
  unmodified by v2.0. No v1.0 manifest field was renamed or removed.
- **Reproducibility fingerprints.** A dataset version's `lineage_fingerprint`
  computed by v2.0 from v1.0-origin data is byte-identical to the value
  v1.0 itself originally computed and stored — confirmed by direct
  comparison against a real registration.
- **HTTP API.** Every v1.0 route continues to work unchanged. `PipelineRun`
  (v2.6) is purely additive — you can run a pipeline via direct stage-by-
  stage API calls (the v1.0 way) or via `POST /api/v1/runs` (the v2.6 way);
  neither is required by the other.
- **Dataset version mappings.** A dataset version registered under v1.0
  remains valid and resolvable under v2.0 with no re-registration step.

## Opening an existing v1.0 workspace

There is no separate "import" or "convert" command — v2.0 opens a v1.0
workspace the same way it opens any workspace: point `--workspace` (CLI) or
`RAW_STORAGE_ROOT`/`CATALOG_DB_PATH` (server config) at the existing data
directory.

If the directory doesn't yet have a `forge.yaml` (v1.0 predates the
workspace/config model introduced for the CLI in v2.7), adopt it
non-destructively with:

```bash
forge init --force <path-to-existing-v1-data-parent-dir>
```

`--force` here only means "write `forge.yaml`, `README.md`, and example
files even if some already exist" — it never deletes or overwrites your
existing `data/` contents. It calls `mkdir(parents=True, exist_ok=True)`
on the data subdirectories, which is a no-op if they already exist, and
never touches `catalog.db` or any artifact directory. This was verified
directly: registering 13 real artifacts and 1 real dataset version under
v1.0, then running `forge init --force`, then re-checking the catalog —
same 13 artifacts, same 1 dataset version, unchanged.

## Recommended upgrade workflow

1. **Back up your workspace directory before upgrading**, as you would
   before any major version upgrade of infrastructure you depend on. Forge
   Data does not include a backup subsystem and will not automatically
   copy a multi-gigabyte `data/` directory for you — use whatever backup
   tooling fits your data size (`rsync`, a filesystem snapshot, a tarball
   of the workspace directory).
2. Install v2.0 (see the README's Quick Start).
3. Point it at your existing workspace: `forge run --workspace <path> ...`,
   or run `forge init --force <path>` first if the workspace predates
   v2.7's `forge.yaml` config file.
4. Run `forge doctor --workspace <path>` to confirm the catalog opens
   cleanly and reports the expected artifact/dataset counts.
5. Continue running pipelines, registering dataset versions, and querying
   lineage exactly as before. No rebuild is required for a workspace that
   has stayed at the same filesystem path.

## Relocated workspaces: the one real caveat

If a v1.0 (or v2.x) workspace directory is **moved to a different
filesystem path** between scans (e.g. migrating to a new machine, or
reorganizing where data lives on disk), an incremental `catalog scan` will
fail with a `CatalogScanFailedError`. This is not new to v2.0 — it is a
deliberate v1.0 behavior (`ArtifactRegistryConflictError`, in
`app/catalog/repository.py`) that refuses to silently overwrite a
registered artifact's `manifest_uri` when a rescan finds it at a different
absolute path than what's stored. This guard exists to prevent silently
conflating two different artifacts that happen to share an ID; it is
correct, intentional behavior, not a bug.

**The fix is a full rebuild, not an incremental scan.** `forge rebuild`
re-derives the entire catalog index from the artifacts actually present on
disk and correctly recovers from a relocation — confirmed directly: after
relocating a real v1.0-origin workspace and forcing a scan failure, `forge
rebuild --workspace <path>` reported:

```
Catalog rebuild completed

Artifacts registered: 13
Edges registered: 13
Datasets preserved: 1
Dataset versions preserved: 1
```

with the dataset registration, version history, and `lineage_fingerprint`
all fully intact afterward (`--json` returns the same fields as
`artifacts_registered`/`edges_registered`/`issues`/`datasets_preserved`/
`dataset_versions_preserved`, for scripting).

```bash
# after moving a workspace directory to a new location:
forge rebuild --workspace <new-path>
```

If you hit `CatalogScanFailedError` from the API, CLI, or GUI after moving
a workspace, the error message points back to this document. `forge
rebuild` calls the same `CatalogService.rebuild()` the HTTP
`POST /api/v1/catalog/rebuild` route does — either recovers a relocated
workspace identically; the CLI form just doesn't require `forge serve` to
be running first.

## What is *not* preserved automatically

- **Nothing is deleted or overwritten automatically**, including on
  `rebuild`. Governance history, run history, and dataset version mappings
  survive a rebuild by design (`datasets_preserved` / `dataset_versions_
  preserved` in the rebuild report reflect this).
- Forge Data will never auto-delete or reset an old catalog on schema
  mismatch. A catalog's schema version is recorded in `catalog_metadata`
  and checked by `forge doctor` / the catalog health endpoint: a mismatch
  surfaces as a `CATALOG_SCHEMA_MISMATCH` health issue (status
  `"degraded"`, not a hard failure), never a silent reset. In practice
  this has not fired across v1.0 through v2.0 — the on-disk schema value
  has been `"1.0.0"` since v1.0 and every v2.x release still writes that
  same value, since schema evolution so far has been purely additive
  (new tables, not new versions of existing ones). It exists as a
  guard for a future breaking schema change, not as an active mechanism
  today.
