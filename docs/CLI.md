# Forge Data CLI reference

The `forge` command is a thin client over the same application/service
layer and HTTP API documented in `docs/DETAILED_GUIDE.md` — see its
"Local CLI and GUI (v2.7)" section for the architecture. This document
is the practical command-by-command reference.

Install: Forge Data is not yet published to PyPI, so `pip install
forge-data` is aspirational for now — install from a locally built
wheel (`pip install dist/*.whl`) or, for development, `pip install
-e ".[dev]"` from a source checkout. Either way you get a real `forge`
console command; `python -m app.cli.main` is never required.

## Workspace resolution

Every command that touches a workspace accepts `--workspace <dir>`.
Without it, resolution falls through:

1. `--workspace <dir>` (explicit)
2. `FORGE_WORKSPACE` environment variable
3. current directory, if it already contains `forge.yaml`
4. otherwise: a clear error telling you to run `forge init` or set one
   of the above — a workspace (and its `data/` tree) is never silently
   created in an arbitrary directory.

## `forge --version` / `forge --help`

Prints the single authoritative version (`app/version.py`) / the
command list.

## `forge init <directory> [--force]`

Creates a new workspace: `forge.yaml` (the marker file — its presence,
not its contents, is what makes a directory a workspace), a `data/`
tree (created lazily by whichever command first needs a given
subdirectory), `pipelines/example.yaml`, and tiny synthetic
`input/imu.csv` + `input/gps.csv` (20 rows each) so the example config
runs instantly.

Idempotent if the target is already a workspace. Fails clearly on a
non-empty, non-workspace directory unless `--force` is given.

```bash
forge init my-workspace
cd my-workspace
forge run pipelines/example.yaml
```

## `forge config validate <file> [--json]`

Validates a pipeline YAML/JSON config without running anything:
syntax, `PipelineRunRequest` structure, each stream's input file
existing on disk, each stream's `sensor_type` being a registered
plugin, and packaging split ratios summing to 1.0. Exit code `0` if
valid, `1` with field-level errors otherwise.

## `forge run <file> [--dry-run] [--json] [--workspace <dir>]`

Runs a pipeline config through the real, in-process v2.6
`RunService`/`LocalRunExecutor` path — the same execution engine
`POST /api/v1/runs` uses, never a duplicate. Prints a live stage-by-
stage progress display (checkmark/spinner/skip/cancel glyphs,
percentage only when genuinely known) unless `--json`, which instead
prints only the final run as JSON once it reaches a terminal state (for
scripting).

`--dry-run` validates the config, resolves the real stage plan, and
prints it — no run record is created, no artifact is written.

Exit codes: `0` completed, `1` config/validation error, `2` the run
itself failed or was cancelled, `3` resource not found.

```bash
forge run pipelines/example.yaml
forge run pipelines/example.yaml --dry-run
forge run pipelines/example.yaml --json | jq .status
```

## `forge run show <run_id> [--json]`

Shows a run's current status, stage list (in real execution order),
progress, and — if failed — its structured error code/message. Exit
code `3` if the run doesn't exist.

## `forge run cancel <run_id> [--json]`

Requests cooperative cancellation. Idempotent: a no-op (still returns
200/the current run) if the run is already in a terminal state.
Cancellation takes effect at the run's next safe stage boundary, not
instantly.

## `forge run events <run_id> [--json]`

Lists the run's append-only lifecycle events (`RUN_CREATED`,
`RUN_STARTED`, `RUN_COMPLETED`, `RUN_FAILED`, `RUN_CANCELLED`, ...) —
not per-progress-update, just meaningful transitions.

## `forge runs [--status <s>] [--run-type <t>] [--limit N] [--json]`

Lists recent runs (default limit 20, max 100).

## `forge sensors [--json]`

Lists every registered sensor plugin (type, schema, normalization
profile, required fields) straight from the real v2.3
`SensorPluginRegistry` — never a hard-coded list. Whatever plugins are
installed/registered is exactly what this shows.

## `forge datasets [--json]`

Lists registered datasets (name, version count, latest version).

## `forge dataset show <name> [--json]`

Lists every version of one dataset: version, status, effective
governance status (`healthy` or `Affected: <reason>`), package ID.

## `forge dataset register <name> --version <v> --package-id <id> [--allow-deprecated] [--json]`

Registers an existing, completed package as a new dataset version.
Version is always explicit — never auto-picked or auto-incremented.
Respects v2.5 governance gates; `--allow-deprecated` permits a
deprecated (but not invalid) ancestor, matching the HTTP API's own
query parameter.

```bash
forge dataset register robotics-grasping --version 1.0.0 --package-id pkg_92ae...
```

## `forge lineage <artifact_type> <artifact_id> [--direction upstream|downstream|both] [--max-depth N] [--json]`

Shows an artifact's lineage as a tree (parents/children walked outward
from the queried artifact in both directions, not just the direction
that would make it a "parent" — see the DETAILED_GUIDE note on why a
naive parent-only walk renders empty when rooted at a package).

```bash
forge lineage package pkg_92ae...
```

## `forge verify <artifact_type> <artifact_id> [--recursive] [--json]`

Verifies an artifact's checksums/references via the existing
`ArtifactVerifier`/`CatalogService.verify` — never a second checksum
implementation. Exit code `1` if verification fails.

## `forge recover scan [--json]`

Runs the v2.1 `RecoveryService` scan: classifies every staging entry as
`ACTIVE`, `STALE`, or `INVALID_STAGING_ENTRY`.

## `forge recover cleanup [--dry-run] [--yes] [--json]`

Removes currently-`STALE` staging entries only — never `ACTIVE` or
`INVALID_STAGING_ENTRY` ones. Requires `--yes` to actually delete
anything; `--dry-run` previews what would be removed without deleting.

## `forge doctor [--strict] [--json]`

The main diagnostic command. Checks: Python version, workspace/data
directory writability, catalog reachability, SQLite journal mode and
foreign-key enforcement, `PRAGMA integrity_check`, `PRAGMA
foreign_key_check`, stale run heartbeats, recoverable staging entries,
free disk space, registered sensor plugins, and whether the built
frontend is present (advisory — an API-only install is fully
supported, never a hard failure). `--strict` exits non-zero if any
*non-advisory* check failed, for scripting/CI use.

```bash
forge doctor
forge doctor --strict   # exit 4 if unhealthy
forge doctor --json | jq '.checks[] | select(.ok == false)'
```

## `forge rebuild [--json]`

Rebuilds the catalog's artifact index and lineage edges from the
artifacts actually present on disk (the same operation
`POST /api/v1/catalog/rebuild` performs over HTTP, callable here without
starting `forge serve`). Datasets, dataset versions, and all governance/
run metadata are user-registered/operational catalog state, not
reconstructible from stage manifests, and are never touched — only the
artifact/lineage index is reconstructed.

This is the fix for `CatalogScanFailedError` (the message every
`.scan()`-then-retry command prints points back to
`docs/MIGRATION_V1_TO_V2.md`, "Relocated workspaces") — an incremental
scan refuses to silently overwrite a registered artifact's
`manifest_uri` when a workspace has moved to a different filesystem
path; a full rebuild re-derives the index from what's on disk instead
and recovers cleanly. Obeys the same v2.4 exclusive rebuild lock as the
HTTP route: if another process already holds it, this fails immediately
with a clean `CATALOG_REBUILD_IN_PROGRESS` message and exit code 1,
never a hang.

```bash
forge rebuild --workspace <path>
```

```
Catalog rebuild completed

Artifacts registered: 13
Edges registered: 13
Datasets preserved: 1
Dataset versions preserved: 1
```

## `forge serve [--host 127.0.0.1] [--port 8000] [--open-browser]`

Starts the FastAPI backend (via `uvicorn`) for the resolved workspace,
serving the API under `/api/v1` and — if the frontend has been built —
the local GUI at `/`. Defaults to `127.0.0.1` (never `0.0.0.0`) since
this is a local-first tool with no authentication; passing an explicit
`--host 0.0.0.0` is allowed but prints a warning that the service
becomes network-reachable.

```bash
forge serve
forge serve --port 8080 --open-browser
```

## Machine-readable output

Most commands accept `--json` for scripting: `forge runs --json`,
`forge run show <id> --json`, `forge doctor --json`, etc. Output in
that mode is pure JSON on stdout — safe to pipe into `jq` or similar.
