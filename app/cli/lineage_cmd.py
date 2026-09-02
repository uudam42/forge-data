"""`forge lineage <artifact_type> <artifact_id>` (Design Requirement 13)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.tree import Tree

from app.catalog.errors import ArtifactNotFoundError, InvalidArtifactTypeError
from app.cli.output import console, print_error, print_json
from app.cli.services import build_catalog_service
from app.cli.workspace import resolve_settings_or_exit

def lineage(
    artifact_type: str,
    artifact_id: str,
    direction: str = typer.Option("both", "--direction", help="upstream | downstream | both"),
    max_depth: Optional[int] = typer.Option(None, "--max-depth"),
    workspace: Optional[Path] = typer.Option(None, "--workspace"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Shows an artifact's lineage graph (parents/children)."""
    settings = resolve_settings_or_exit(workspace)
    catalog_service = build_catalog_service(settings)
    try:
        graph = catalog_service.lineage(artifact_type, artifact_id, direction=direction, max_depth=max_depth)
    except ArtifactNotFoundError:
        # See app.runs.results / app.cli.verify_cmd for why: the artifact
        # index is populated by an explicit scan, not written live by
        # each stage -- scan once and retry before reporting "not found".
        catalog_service.scan()
        try:
            graph = catalog_service.lineage(artifact_type, artifact_id, direction=direction, max_depth=max_depth)
        except (InvalidArtifactTypeError, ArtifactNotFoundError) as exc:
            print_error(str(exc))
            raise typer.Exit(code=3) from exc
    except InvalidArtifactTypeError as exc:
        print_error(str(exc))
        raise typer.Exit(code=3) from exc

    if as_json:
        print_json(graph)
        return

    # A generic outward walk from root, regardless of parent/child role --
    # "upstream" makes root a sink (all edges point INTO it), "downstream"
    # makes it a source, "both" mixes freely. Visited-set guards against
    # re-descending into a node reached by more than one path (a
    # multi-parent join, e.g. two streams both feeding synchronization).
    def _key(ref) -> str:
        return f"{ref.artifact_type}/{ref.artifact_id}"

    neighbors: dict[str, list] = {}
    for edge in graph.edges:
        neighbors.setdefault(_key(edge.parent), []).append(edge.child)
        neighbors.setdefault(_key(edge.child), []).append(edge.parent)

    root_key = _key(graph.root)
    tree = Tree(f"[bold]{root_key}[/bold]")
    visited = {root_key}

    def _add(node_key: str, branch) -> None:
        for neighbor in neighbors.get(node_key, []):
            neighbor_key = _key(neighbor)
            if neighbor_key in visited:
                continue
            visited.add(neighbor_key)
            _add(neighbor_key, branch.add(neighbor_key))

    _add(root_key, tree)
    console.print(tree)
