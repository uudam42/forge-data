# Changelog

All notable changes to Forge Data are documented in this file. This is a
release-oriented summary of highlights, not a commit-by-commit log — see
`git log` for full history.

## [2.0.0] — Unreleased

Forge Data v2.0 is a from-the-ground-up reliability, scale, and usability
upgrade over v1.0's pipeline core. It adds crash safety, large-data resource
bounds, a plugin-based sensor architecture, multiprocess-safe concurrency,
governance-aware lineage, durable pipeline run tracking, and a full local
CLI/GUI distribution — while remaining schema- and API-compatible with v1.0
workspaces. See `docs/MIGRATION_V1_TO_V2.md` for upgrade details and
`docs/RELEASE_NOTES_V2.md` for the full feature tour and known limitations.

### Added

- **Crash-safe artifacts & recovery** — every stage publishes artifacts via
  atomic staging + fsync'd directory rename; a partial write can never enter
  finalized storage. A staging recovery scan cleans up interrupted work on
  restart.
- **Large-scale resource bounds** — synchronization, transformation, and QC
  operate in bounded memory regardless of dataset size; an opt-in
  SQLite-backed dedup backend keeps cleaning's memory flat from 50K to 1M+
  rows (vs. linear growth in the default in-memory backend). Disk-space
  preflight checks reject oversized writes before they start.
- **Sensor plugin architecture** — IMU and GPS support now sit behind a
  composable `SensorPlugin` contract; a new 6-axis Force/Torque sensor
  ships as the first "written after the fact" plugin, proving the
  architecture. Every other stage (sync, cleaning, QC, packaging, catalog)
  is sensor-agnostic.
- **Multiprocess-safe catalog** — SQLite WAL mode, per-process connections,
  `BEGIN IMMEDIATE` writes, and exclusive flock-based rebuild locking make
  concurrent artifact/edge/dataset registration and catalog rebuilds safe
  under real multiprocessing and multi-worker `uvicorn` deployment.
- **Governance-aware lineage & selective rebuild** — artifacts and dataset
  versions carry an append-only governance history (active / deprecated /
  invalid). Invalid or deprecated lineage blocks new downstream work;
  impact analysis reports every affected artifact and dataset version;
  selective rebuild reuses unaffected DAG branches instead of recomputing
  everything.
- **Durable pipeline runs & observability** — a `PipelineRun`/`StageRun`
  execution model tracks run-to-artifact provenance, throttled progress,
  and structured errors; cooperative cancellation and heartbeat-based
  crash reconciliation recover cleanly from a killed process.
- **Local CLI & GUI** — an installable `forge` CLI (init, run, doctor,
  sensors, datasets, lineage, verify, rebuild, recovery, serve) and a React/
  TypeScript GUI served directly by FastAPI, packaged into the wheel
  alongside sensor schemas — no source checkout required to run either.
- **Results Explorer** — inspect a completed run's package contents, QC
  summary, split assignments, file metadata, and lineage fingerprint from
  both the CLI and GUI, backed by metadata-only reads (never streaming
  full file contents through the API).

### Compatibility

- The original v1.0 catalog schema (6 tables: `artifacts`, `lineage_edges`,
  `lineage_issues`, `datasets`, `dataset_versions`, `catalog_metadata`) is
  byte-for-byte unchanged; all v2 additions are new, separate tables
  created via `CREATE TABLE IF NOT EXISTS`. A v1.0 workspace's catalog and
  artifacts open under v2.0 with no migration step and no data loss.
- All v1.0 HTTP API routes remain valid and unchanged; `PipelineRun` usage
  is entirely additive/optional.
- See `docs/MIGRATION_V1_TO_V2.md` for the one known caveat (a *relocated*
  workspace directory requires `forge rebuild` rather than an incremental
  scan) and the full upgrade workflow.

## [1.0.0] — Initial release

The original ten-stage pipeline: ingestion, schema validation, data
integrity checks, normalization, multimodal synchronization, cleaning/
filtering, transformation/feature generation, dataset QC, dataset
packaging/export, and a catalog/lineage/versioning layer tying it all
together. IMU and GPS sensor support. 878 tests passing.
