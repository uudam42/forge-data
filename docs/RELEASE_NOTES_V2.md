# Forge Data v2.0 Release Notes

## What's new

Forge Data v2.0 turns the v1.0 pipeline core into a reliability- and
scale-hardened platform, without changing the data model or breaking any
existing v1.0 workspace or API. Highlights:

- **Crash safety** — every artifact is published atomically; a process
  killed mid-write leaves no partial artifact behind, and a recovery scan
  cleans up interrupted staging on restart.
- **Large-data resource bounds** — validated up to 1M rows with bounded
  memory across validation, integrity, normalization, synchronization,
  transformation, and QC; an opt-in SQLite dedup backend keeps cleaning
  flat at any scale.
- **A real sensor plugin architecture** — IMU and GPS moved onto a
  `SensorPlugin` contract, and a 6-axis Force/Torque sensor shipped as
  the first plugin written after the fact, proving new sensors don't
  require touching sync/cleaning/QC/packaging/catalog code.
- **Multiprocess-safe catalog** — concurrent readers and writers, across
  real OS processes and multi-worker `uvicorn`, backed by SQLite WAL mode
  and exclusive rebuild locking.
- **Governance-aware lineage** — mark an artifact or dataset version
  deprecated or invalid, see exactly what downstream work is affected,
  and selectively rebuild only what needs it while reusing unaffected
  branches of the lineage DAG.
- **Durable pipeline runs** — a `PipelineRun`/`StageRun` model with
  progress, cooperative cancellation, and crash reconciliation, usable
  from the CLI, the GUI, or the HTTP API directly.
- **A local CLI and GUI** — `forge init`, `forge run`, `forge doctor`,
  `forge serve`, and a React GUI for monitoring runs and exploring
  results — installable as a wheel, no source checkout required.
- **A Results Explorer** — inspect a completed run's package, QC summary,
  split assignments, and lineage from either the CLI or the GUI.

## Who v2.0 is for

Teams running robotics / physical-AI multimodal sensor data pipelines
(IMU, GPS, Force/Torque, and custom sensors via the plugin system) on a
single machine or a small number of machines sharing a filesystem, who
need reproducible, versioned, auditable ML-ready datasets with real crash
and concurrency guarantees — without operating a distributed job queue or
cloud data platform to get there.

## Architecture improvements over v1.0

- Atomic staging + fsync'd rename replaces direct-write publishing at
  every stage (v2.1).
- Per-stage resource contracts were audited and made explicit; a
  SQLite-backed dedup option and disk-space preflight checks were added
  where the default in-memory approach doesn't scale (v2.2).
- Sensor-specific logic was extracted behind a plugin boundary; every
  other stage became sensor-agnostic (v2.3).
- The catalog moved from implicit single-writer assumptions to WAL mode,
  per-process connections, `BEGIN IMMEDIATE` writes, and flock-based
  exclusive rebuild locking (v2.4).
- Lineage gained a governance layer (active/deprecated/invalid) and
  DAG-aware selective rebuild (v2.5).
- Pipeline execution gained a durable run/stage model with progress,
  cancellation, and heartbeat-based crash reconciliation, independent of
  and additive to direct stage-by-stage API usage (v2.6).
- A CLI and GUI were built and packaged for local, no-checkout
  installation (v2.7).

## CLI usage

Forge Data is not yet published to PyPI — install from a locally built
wheel (`pip install dist/*.whl`) or a source checkout (`pip install
-e ".[dev]"`); the workflow below is identical either way.

```bash
forge init my-workspace
cd my-workspace
forge config validate
forge run pipelines/example.yaml
forge doctor
forge serve   # starts the local GUI + API on 127.0.0.1
```

See `docs/CLI.md` for the full command reference.

## GUI usage

`forge serve` starts a local server (bound to `127.0.0.1` by default) and
serves the GUI at the same address. From there: submit a new run, watch
live progress, inspect a completed run's Results Explorer (package
contents, QC, lineage, dataset registration), and manage dataset
governance — all backed by the same HTTP API the CLI uses.

## Compatibility

v2.0 reads v1.0 workspaces with no destructive migration and no forced
rebuild, as long as the workspace hasn't been moved to a new filesystem
path since it was last scanned. Every v1.0 HTTP API route continues to
work unchanged. Full details, and the one real caveat (relocated
workspaces need `forge rebuild`, not an incremental scan), are in
`docs/MIGRATION_V1_TO_V2.md`.

## Known limitations

Forge Data v2.0 is designed for large **single-machine** (or small,
shared-filesystem) robotics workloads. Specifically, and honestly:

- **No distributed job queue.** A `PipelineRun` executes in-process on
  the machine that received the request. There is no cluster scheduler,
  no distributed worker pool, and no cross-machine job handoff.
- **No cloud storage backend.** Artifacts and the catalog live on local
  (or locally-mounted) disk. There is no S3/GCS/Azure Blob backend.
- **Cancellation is cooperative, not immediate.** A cancellation request
  is honored at the next safe stage boundary, not instantaneously —
  a stage already in flight runs to its next checkpoint before stopping.
- **Three structures are genuinely O(dataset size) in memory, by design
  trade-off, not oversight:**
  - Top-level JSON array input/output (validation, integrity,
    normalization) — a JSON array requires the whole array in memory to
    parse or to close. CSV and JSONL remain fully streaming at every
    stage.
  - The default in-memory cleaning dedup backend — use
    `duplicate_policy.backend: sqlite` for O(1)-memory deduplication at
    any scale, with byte-identical results.
  - The optional Parquet exporter (only reached when a request's
    `exports` includes `"parquet"`) accumulates full columns before
    writing a row group. The mandatory JSONL export is fully streamed.
- **Sensor plugin discovery is static**, via a built-in registry — there
  is no dynamic plugin-loading mechanism (e.g. entry points or a plugin
  marketplace) in this release.
- **Selective rebuild's earliest supported replacement point is a
  normalization artifact**, not a raw ingestion. Concretely: you can fix
  a bad normalization output (wrong unit config, wrong profile) by
  re-normalizing the *same* ingestion and selectively rebuilding
  everything downstream — the common real case, and the one this
  feature was built and tested against. You cannot selectively "swap
  in" a corrected raw ingestion; the pipeline stages upstream of
  normalization (ingestion, validation, integrity) have no rebuild
  executor, and a plan anchored there is now rejected up front with a
  clear error rather than appearing to succeed and then failing at
  execution. A genuinely bad raw file requires a full new pipeline run
  (a new ingestion, validated and processed end to end) rather than a
  selective rebuild.
- **A catalog scan/rebuild holds a write lock for its full duration.**
  Concurrent writers wait up to a configurable busy timeout and then
  receive a structured "catalog busy" error rather than hanging
  indefinitely — but a very large workspace's rebuild is a maintenance
  operation to run occasionally, not a routine request-path write.
- **The GUI does not proxy large file downloads.** Results Explorer
  metadata endpoints report package/file metadata without reading file
  contents; retrieving the actual package bytes is a local filesystem
  operation ("Open Output Folder" / "Copy Path"), not a GUI download.

None of the above are regressions from v1.0 — v1.0 had the same
single-machine, local-storage design; v2.0 makes the boundaries explicit
and adds the reliability/scale/observability layers described above
within that same design.
