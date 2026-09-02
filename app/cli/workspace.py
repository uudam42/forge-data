"""Workspace resolution and workspace-scoped Settings construction
(Design Requirement 69).

A Forge workspace is just a directory containing a `forge.yaml` marker
file plus a `data/` tree -- there is no second storage hierarchy here;
`build_settings_for_workspace` only ever points the *existing*
`app.core.config.Settings` roots at `<workspace>/data/<stage>`, reusing
the exact same fields every stage service already reads.

Resolution precedence (first match wins), never silently creating `data/`
in an arbitrary cwd:
    1. an explicit --workspace path
    2. the FORGE_WORKSPACE environment variable
    3. the current directory, if it already contains forge.yaml
    4. WorkspaceNotFoundError, with guidance
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from app.cli.errors import CliError
from app.core.config import Settings

WORKSPACE_MARKER = "forge.yaml"

# Every Settings field that names a stage storage root, keyed by the
# subdirectory it becomes under <workspace>/data/. Kept as one explicit
# map (rather than reflecting over Settings) so a new stage's root is a
# deliberate one-line addition here, not "whatever Settings happens to
# expose next."
_DATA_SUBDIRS: dict[str, str] = {
    "RAW_STORAGE_ROOT": "raw",
    "VALIDATION_STORAGE_ROOT": "validation",
    "INTEGRITY_STORAGE_ROOT": "integrity",
    "NORMALIZED_STORAGE_ROOT": "normalized",
    "SYNCHRONIZED_STORAGE_ROOT": "synchronized",
    "CLEANED_STORAGE_ROOT": "cleaned",
    "TRANSFORMED_STORAGE_ROOT": "transformed",
    "QC_STORAGE_ROOT": "qc",
    "PACKAGE_STORAGE_ROOT": "packages",
}


class WorkspaceNotFoundError(CliError):
    pass


def resolve_workspace(explicit: Path | None) -> Path:
    if explicit is not None:
        candidate = explicit.resolve()
        if not (candidate / WORKSPACE_MARKER).is_file():
            raise WorkspaceNotFoundError(f"'{candidate}' is not a Forge workspace (no {WORKSPACE_MARKER} found).")
        return candidate

    env_value = os.environ.get("FORGE_WORKSPACE")
    if env_value:
        candidate = Path(env_value).resolve()
        if not (candidate / WORKSPACE_MARKER).is_file():
            raise WorkspaceNotFoundError(f"FORGE_WORKSPACE='{candidate}' is not a Forge workspace (no {WORKSPACE_MARKER} found).")
        return candidate

    cwd = Path.cwd()
    if (cwd / WORKSPACE_MARKER).is_file():
        return cwd

    raise WorkspaceNotFoundError(
        "No Forge workspace found. Run `forge init <dir>` to create one, "
        "pass --workspace <dir>, or set FORGE_WORKSPACE."
    )


def workspace_data_roots(workspace: Path) -> dict[str, Path]:
    data_dir = workspace / "data"
    roots = {field: data_dir / subdir for field, subdir in _DATA_SUBDIRS.items()}
    roots["CATALOG_DB_PATH"] = data_dir / "catalog" / "catalog.db"
    return roots


def resolve_workspace_or_exit(workspace: Path | None) -> Path:
    """Like `resolve_settings_or_exit`, but for a command (e.g. `forge
    doctor`) that needs the raw workspace `Path` itself, not a `Settings`
    object."""
    try:
        return resolve_workspace(workspace)
    except CliError as exc:
        from app.cli.output import print_error

        print_error(str(exc))
        raise typer.Exit(code=exc.exit_code) from exc


def resolve_settings_or_exit(workspace: Path | None) -> Settings:
    """The one call every CLI command should use to go from an optional
    `--workspace` argument to a real, workspace-rooted `Settings` object.
    Catches `WorkspaceNotFoundError` (a `CliError`) itself and prints a
    clean message + exits, rather than letting it surface as a raw
    traceback -- see `app.cli.errors` for why every command needs to do
    this explicitly instead of relying on Typer/Click to do it."""
    try:
        return build_settings_for_workspace(resolve_workspace(workspace))
    except CliError as exc:
        from app.cli.output import print_error

        print_error(str(exc))
        raise typer.Exit(code=exc.exit_code) from exc


def build_settings_for_workspace(workspace: Path, **overrides: object) -> Settings:
    """Constructs a real `Settings` object rooted at this workspace.
    `SCHEMA_DIR` is deliberately left at its packaged-resource default
    (Design Requirement 50) -- schemas are bundled application resources,
    never workspace-editable content."""
    return Settings(**workspace_data_roots(workspace), **overrides)


def env_for_workspace(workspace: Path) -> dict[str, str]:
    """The same roots as `build_settings_for_workspace`, as environment
    variable assignments -- used by `forge serve` to hand the resolved
    workspace to a fresh `Settings()` constructed inside the uvicorn
    server process (see `app.cli.serve`)."""
    return {field: str(path) for field, path in workspace_data_roots(workspace).items()}
