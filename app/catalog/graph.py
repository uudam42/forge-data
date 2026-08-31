"""DAG traversal, cycle detection, and deterministic ordering over the
lineage edge table. Pure read-side logic — never mutates the repository.
"""

from __future__ import annotations

from app.catalog.models import STAGE_RANK
from app.catalog.repository import CatalogRepository


def _sort_key(artifact_type: str, artifact_id: str) -> tuple[int, str, str]:
    return (STAGE_RANK.get(artifact_type, 99), artifact_type, artifact_id)


def would_create_cycle(
    repo: CatalogRepository, *, parent_type: str, parent_id: str, child_type: str, child_id: str
) -> bool:
    """True if adding edge parent->child would create a cycle — i.e. if
    `parent` is already reachable by following existing child-edges
    forward starting from `child` (child is already an ancestor-to-be of
    parent, so closing parent->child would loop back)."""
    if (parent_type, parent_id) == (child_type, child_id):
        return True

    visited: set[tuple[str, str]] = set()
    stack = [(child_type, child_id)]
    while stack:
        current = stack.pop()
        if current == (parent_type, parent_id):
            return True
        if current in visited:
            continue
        visited.add(current)
        for edge in repo.get_children(*current):
            stack.append((edge["child_artifact_type"], edge["child_artifact_id"]))
    return False


def traverse(
    repo: CatalogRepository,
    *,
    root_type: str,
    root_id: str,
    direction: str = "both",
    max_depth: int | None = None,
) -> tuple[list[dict], list[tuple[str, str, str, str, str]]]:
    """Returns (nodes, edges) — nodes always includes the root (if it
    exists in the repository), deterministically ordered by
    (pipeline_stage, artifact_type, artifact_id); edges ordered the same
    way by their parent then child. `edges` entries are
    (parent_type, parent_id, child_type, child_id, relationship) tuples.
    """
    node_keys: dict[tuple[str, str], dict | None] = {(root_type, root_id): repo.get_artifact(root_type, root_id)}
    edges: set[tuple[str, str, str, str, str]] = set()

    def add_node(atype: str, aid: str) -> None:
        key = (atype, aid)
        if key not in node_keys:
            node_keys[key] = repo.get_artifact(atype, aid)

    def bfs(get_related, upstream: bool) -> None:
        frontier: list[tuple[str, str, int]] = [(root_type, root_id, 0)]
        seen = {(root_type, root_id)}
        while frontier:
            atype, aid, depth = frontier.pop(0)
            if max_depth is not None and depth >= max_depth:
                continue
            for edge in get_related(atype, aid):
                edges.add(
                    (
                        edge["parent_artifact_type"],
                        edge["parent_artifact_id"],
                        edge["child_artifact_type"],
                        edge["child_artifact_id"],
                        edge["relationship"],
                    )
                )
                other = (
                    (edge["parent_artifact_type"], edge["parent_artifact_id"])
                    if upstream
                    else (edge["child_artifact_type"], edge["child_artifact_id"])
                )
                add_node(*other)
                if other not in seen:
                    seen.add(other)
                    frontier.append((other[0], other[1], depth + 1))

    if direction in ("upstream", "both"):
        bfs(repo.get_parents, upstream=True)
    if direction in ("downstream", "both"):
        bfs(repo.get_children, upstream=False)

    nodes = sorted(
        (v for v in node_keys.values() if v is not None),
        key=lambda n: _sort_key(n["artifact_type"], n["artifact_id"]),
    )
    edge_list = sorted(
        edges,
        key=lambda e: (_sort_key(e[0], e[1]), _sort_key(e[2], e[3]), e[4]),
    )
    return nodes, edge_list


def impact_analysis(repo: CatalogRepository, *, artifact_type: str, artifact_id: str) -> dict[str, int]:
    """Downstream artifact counts by stage, plus the count of registered
    dataset versions that transitively depend on this artifact via any
    downstream package."""
    nodes, _ = traverse(repo, root_type=artifact_type, root_id=artifact_id, direction="downstream")
    counts: dict[str, int] = {}
    package_ids: list[str] = []
    for node in nodes:
        if node["artifact_type"] == artifact_type and node["artifact_id"] == artifact_id:
            continue
        counts[node["artifact_type"]] = counts.get(node["artifact_type"], 0) + 1
        if node["artifact_type"] == "package":
            package_ids.append(node["artifact_id"])

    versions = repo.list_dataset_versions_for_packages(package_ids)
    counts["dataset_versions"] = len(versions)
    return counts
