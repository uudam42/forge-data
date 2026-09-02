import { apiGet } from './client';
import type { LineageGraphResponse } from './types';

export interface LineageParams {
  direction?: 'both' | 'upstream' | 'downstream';
  maxDepth?: number;
}

export function getLineage(
  artifactType: string,
  artifactId: string,
  params: LineageParams = {},
): Promise<LineageGraphResponse> {
  const search = new URLSearchParams();
  if (params.direction) search.set('direction', params.direction);
  if (params.maxDepth !== undefined) search.set('max_depth', String(params.maxDepth));
  const qs = search.toString();
  return apiGet<LineageGraphResponse>(
    `/lineage/${encodeURIComponent(artifactType)}/${encodeURIComponent(artifactId)}${qs ? `?${qs}` : ''}`,
  );
}
