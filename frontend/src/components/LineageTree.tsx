import type { LineageArtifactRef, LineageGraphResponse } from '../api/types';

function keyOf(ref: LineageArtifactRef): string {
  return `${ref.artifact_type}/${ref.artifact_id}`;
}

interface TreeNode {
  ref: LineageArtifactRef;
  pipeline_stage?: number;
  status?: string | null;
  children: TreeNode[];
}

function buildTree(graph: LineageGraphResponse): TreeNode {
  // Edges are always stored in true causal (parent-produces-child)
  // direction, regardless of which artifact the graph was queried from.
  // The root can be either end of that DAG (e.g. a package is a sink --
  // it is always a *child*, never a parent), so this walks the
  // adjacency undirected from the root -- exactly like the CLI's
  // `forge lineage` tree (app.cli.lineage_cmd) -- rather than only
  // following parent->child edges, which would render an empty tree
  // whenever root is a downstream artifact.
  const nodeInfo = new Map(graph.nodes.map((n) => [keyOf(n), n]));
  const neighbors = new Map<string, LineageArtifactRef[]>();
  for (const edge of graph.edges) {
    const pKey = keyOf(edge.parent);
    const cKey = keyOf(edge.child);
    if (!neighbors.has(pKey)) neighbors.set(pKey, []);
    if (!neighbors.has(cKey)) neighbors.set(cKey, []);
    neighbors.get(pKey)!.push(edge.child);
    neighbors.get(cKey)!.push(edge.parent);
  }

  const visited = new Set<string>([keyOf(graph.root)]);
  function build(ref: LineageArtifactRef): TreeNode {
    const key = keyOf(ref);
    const info = nodeInfo.get(key);
    const adjacent = neighbors.get(key) ?? [];
    const unvisited = adjacent.filter((n) => !visited.has(keyOf(n)));
    for (const n of unvisited) visited.add(keyOf(n));
    return {
      ref,
      pipeline_stage: info?.pipeline_stage,
      status: info?.status,
      children: unvisited.map((c) => build(c)),
    };
  }

  return build(graph.root);
}

function TreeNodeView({ node }: { node: TreeNode }) {
  return (
    <li className="lineage-node">
      <span style={{ fontFamily: 'var(--font-mono)' }}>{node.ref.artifact_type}</span>
      {' · '}
      <span>{node.ref.artifact_id}</span>
      {node.pipeline_stage != null && <span className="muted"> (stage {node.pipeline_stage})</span>}
      {node.status && <span className="muted"> [{node.status}]</span>}
      {node.children.length > 0 && (
        <ul>
          {node.children.map((child) => (
            <TreeNodeView key={keyOf(child.ref)} node={child} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function LineageTree({ graph }: { graph: LineageGraphResponse }) {
  if (graph.nodes.length === 0) {
    return <p className="muted">No lineage information available for this artifact.</p>;
  }
  const tree = buildTree(graph);
  return (
    <div className="lineage-tree">
      <ul>
        <TreeNodeView node={tree} />
      </ul>
    </div>
  );
}
