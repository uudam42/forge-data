"""Shared Rich console + JSON-output helpers for the CLI (Design
Requirement 9/68)."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from pydantic import BaseModel
from rich.console import Console

console = Console()
error_console = Console(stderr=True, style="red")


def _to_plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    return value


def print_json(payload: Any) -> None:
    console.print_json(json.dumps(_to_plain(payload), default=str))


def print_error(message: str) -> None:
    error_console.print(f"[bold red]Error:[/bold red] {message}")


# One glyph per StageStatus / RunStatus value the terminal displays --
# never a fabricated percentage when the underlying state has none
# (Design Requirement 9).
STAGE_GLYPHS: dict[str, str] = {
    "completed": "[green]✓[/green]",
    "running": "[yellow]⠹[/yellow]",
    "pending": "[dim]○[/dim]",
    "failed": "[bold red]✗[/bold red]",
    "cancelled": "[dim]⊘[/dim]",
    "skipped": "[dim]─[/dim]",
}


def stage_glyph(status: str) -> str:
    return STAGE_GLYPHS.get(status, "?")
