"""`forge serve` (Design Requirement 17/62) -- starts the existing FastAPI
app (which also serves the built GUI, if present) via uvicorn.

Hands the resolved workspace to the server process via environment
variables (the same fields `Settings` -- a pydantic-settings BaseSettings
-- already reads from the environment) rather than inventing a second
settings-injection path. This only works correctly if `app.main` (and
therefore `app.core.config.get_settings()`) has not been imported/called
yet in this process, which is why the import happens lazily inside
`serve()`, after the environment variables are set -- see
`app.cli.workspace.env_for_workspace`.
"""

from __future__ import annotations

import os
import webbrowser
from pathlib import Path
from typing import Optional

import typer

from app.cli.output import console
from app.cli.workspace import env_for_workspace, resolve_workspace

def serve(
    workspace: Optional[Path] = typer.Option(None, "--workspace"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address. 0.0.0.0 makes this reachable over the network -- no authentication is implemented."),
    port: int = typer.Option(8000, "--port"),
    open_browser: bool = typer.Option(False, "--open-browser"),
) -> None:
    """Starts the Forge Data API + local GUI for this workspace."""
    ws = resolve_workspace(workspace)
    os.environ.update(env_for_workspace(ws))

    if host not in ("127.0.0.1", "localhost"):
        console.print(f"[bold yellow]Warning:[/bold yellow] binding to {host} makes this server reachable over the network. No authentication is implemented -- only do this on a trusted network.")

    import uvicorn

    from app.main import app as fastapi_app

    console.print(f"[bold]Forge Data[/bold] serving workspace {ws}")
    console.print(f"  API   http://{host}:{port}/api/v1")
    console.print(f"  GUI   http://{host}:{port}/")

    if open_browser:
        webbrowser.open(f"http://{host}:{port}/")

    uvicorn.run(fastapi_app, host=host, port=port)
