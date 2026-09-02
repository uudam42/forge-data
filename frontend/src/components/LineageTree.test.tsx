import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { LineageTree } from './LineageTree';
import type { LineageGraphResponse } from '../api/types';

describe('LineageTree', () => {
  it('shows an empty state when there are no nodes', () => {
    const graph: LineageGraphResponse = {
      root: { artifact_type: 'package', artifact_id: 'pkg-1' },
      direction: 'both',
      nodes: [],
      edges: [],
    };
    render(<LineageTree graph={graph} />);
    expect(screen.getByText(/no lineage information/i)).toBeInTheDocument();
  });

  it('builds a parent/child tree from a flat edge list', () => {
    const graph: LineageGraphResponse = {
      root: { artifact_type: 'package', artifact_id: 'pkg-1' },
      direction: 'both',
      nodes: [
        { artifact_type: 'package', artifact_id: 'pkg-1', pipeline_stage: 9 },
        { artifact_type: 'qc', artifact_id: 'qc-1', pipeline_stage: 8 },
      ],
      edges: [
        {
          parent: { artifact_type: 'qc', artifact_id: 'qc-1' },
          child: { artifact_type: 'package', artifact_id: 'pkg-1' },
          relationship: 'produces',
        },
      ],
    };
    render(<LineageTree graph={graph} />);
    expect(screen.getByText('pkg-1')).toBeInTheDocument();
    expect(screen.getByText('qc-1')).toBeInTheDocument();
  });

  it('shows ancestors as tree children even when root is a DAG sink (e.g. a package, which is always a child edge, never a parent edge)', () => {
    const graph: LineageGraphResponse = {
      root: { artifact_type: 'package', artifact_id: 'pkg-1' },
      direction: 'both',
      nodes: [
        { artifact_type: 'package', artifact_id: 'pkg-1', pipeline_stage: 9 },
        { artifact_type: 'transformation', artifact_id: 'xform-1', pipeline_stage: 7 },
        { artifact_type: 'ingestion', artifact_id: 'ing-1', pipeline_stage: 1 },
      ],
      edges: [
        {
          parent: { artifact_type: 'transformation', artifact_id: 'xform-1' },
          child: { artifact_type: 'package', artifact_id: 'pkg-1' },
          relationship: 'produces',
        },
        {
          parent: { artifact_type: 'ingestion', artifact_id: 'ing-1' },
          child: { artifact_type: 'transformation', artifact_id: 'xform-1' },
          relationship: 'produces',
        },
      ],
    };
    render(<LineageTree graph={graph} />);
    expect(screen.getByText('pkg-1')).toBeInTheDocument();
    expect(screen.getByText('xform-1')).toBeInTheDocument();
    expect(screen.getByText('ing-1')).toBeInTheDocument();
  });
});
