"""Shared CLI error type and exit codes (Design Requirement 67).

0   success
1   config/validation error (bad YAML, unknown sensor, missing file, ...)
2   pipeline/run failure (the run itself reached status=failed)
3   resource-not-found (unknown run/dataset/artifact id)
4   unhealthy (forge doctor found a real problem, with --strict)

Every command that can raise this (directly, or via `resolve_workspace`)
must catch it itself and call `app.cli.output.print_error` before
exiting -- Typer/Click do not print an unhandled exception's message
automatically here (this typer version's dispatch loop matches its own
vendored exception types, not a plain `Exception` subclass), so an
uncaught `CliError` would otherwise surface as a raw traceback instead
of a clean message. See `app.cli.workspace.resolve_workspace_or_exit`
for the single shared helper every command should use instead of
calling `resolve_workspace` directly.
"""

from __future__ import annotations


class CliError(Exception):
    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code
