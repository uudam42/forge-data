"""`forge doctor` (Design Requirement 16) -- the main usability/diagnostic
command. Every check here reuses an existing backend capability (catalog
connection + PRAGMAs, RecoveryService, RunRepository, SensorPluginRegistry)
rather than reimplementing any of them."""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer

from app.cli.output import console, print_json
from app.cli.workspace import build_settings_for_workspace, resolve_workspace_or_exit
from app.runs.repository import RunRepository
from app.sensors.registry import get_default_registry
from app.storage.catalog_store import get_connection
from app.storage.recovery import RecoveryService

class _Check:
    def __init__(self, name: str, ok: bool, detail: str) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail


def _run_checks(workspace: Path) -> list[_Check]:
    checks: list[_Check] = []
    settings = build_settings_for_workspace(workspace)

    checks.append(_Check("Python version", sys.version_info >= (3, 12), f"{sys.version.split()[0]}"))
    checks.append(_Check("Workspace writable", _writable(workspace), str(workspace)))
    checks.append(_Check("Data directory writable", _writable(workspace / "data"), str(workspace / "data")))

    try:
        conn = get_connection(settings.CATALOG_DB_PATH, busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS, journal_mode=settings.CATALOG_JOURNAL_MODE)
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        checks.append(_Check("Catalog reachable", True, str(settings.CATALOG_DB_PATH)))
        checks.append(_Check("SQLite journal mode", journal_mode.lower() == "wal", journal_mode))
        checks.append(_Check("Foreign keys enabled", bool(foreign_keys), "on" if foreign_keys else "off"))
        checks.append(_Check("Catalog integrity_check", integrity == "ok", integrity))
        checks.append(_Check("Foreign key check", len(fk_violations) == 0, f"{len(fk_violations)} violation(s)"))

        run_repo = RunRepository(conn, db_path=str(settings.CATALOG_DB_PATH), busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS)
        stale_threshold = datetime.now(timezone.utc) - timedelta(seconds=settings.RUN_STALE_HEARTBEAT_SECONDS)
        stale_runs = run_repo.find_stale_running_runs(older_than=stale_threshold)
        checks.append(_Check("Stale run state", len(stale_runs) == 0, f"{len(stale_runs)} run(s) with a stale heartbeat (reconciled at next `forge serve` startup)"))
        conn.close()
    except Exception as exc:  # noqa: BLE001 -- doctor must report, never crash
        checks.append(_Check("Catalog reachable", False, str(exc)))

    scan = RecoveryService(settings).scan()
    checks.append(_Check("Recoverable staging entries", scan.stale_count == 0, f"{scan.stale_count} stale, {scan.invalid_count} invalid, {scan.active_count} active"))

    free_bytes = shutil.disk_usage(workspace).free
    checks.append(_Check("Free disk space", free_bytes >= settings.MIN_FREE_DISK_BYTES, f"{free_bytes / (1024**3):.1f} GB free"))

    plugins = get_default_registry().list_plugins()
    checks.append(_Check("Sensor plugins registered", len(plugins) > 0, f"{len(plugins)} ({', '.join(p.sensor_type for p in plugins)})"))

    try:
        import importlib.resources

        web_dist = Path(str(importlib.resources.files("app.web"))) / "dist" / "index.html"
        checks.append(_Check("Frontend assets installed", web_dist.is_file(), str(web_dist) if web_dist.is_file() else "not built -- API-only mode (see docs/GUI.md)"))
    except Exception:  # noqa: BLE001
        checks.append(_Check("Frontend assets installed", False, "could not resolve app.web package"))

    return checks


def _writable(path: Path) -> bool:
    import os

    path.mkdir(parents=True, exist_ok=True)
    return os.access(path, os.W_OK)


def doctor(
    workspace: Optional[Path] = typer.Option(None, "--workspace"),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero if any check fails (frontend-not-built is never fatal)"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Diagnoses this workspace: Python, filesystem, catalog integrity,
    disk space, sensor plugins, stale runs, recoverable staging."""
    ws = resolve_workspace_or_exit(workspace)
    checks = _run_checks(ws)

    if as_json:
        print_json({"workspace": str(ws), "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks]})
    else:
        console.print("[bold]Forge Data doctor[/bold]")
        console.print()
        for c in checks:
            icon = "[green]✓[/green]" if c.ok else "[bold red]✗[/bold red]"
            console.print(f"{icon} {c.name}: {c.detail}")
        console.print()
        # Frontend-not-built is advisory, not a health failure -- an
        # API-only install is a legitimate, fully-supported mode.
        hard_failures = [c for c in checks if not c.ok and c.name != "Frontend assets installed"]
        if hard_failures:
            console.print("[bold red]Issues found.[/bold red]")
        else:
            console.print("[bold green]System ready.[/bold green]")

    hard_failures = [c for c in checks if not c.ok and c.name != "Frontend assets installed"]
    if strict and hard_failures:
        raise typer.Exit(code=4)
