import { apiGet, apiPostEmpty, apiPostForm } from './client';
import type {
  PipelineRunRequest,
  PipelineRunResponse,
  RunEventResponse,
  RunListResponse,
  RunResultsResponse,
  RunStatus,
} from './types';

export interface ListRunsParams {
  status?: RunStatus;
  run_type?: string;
  limit?: number;
  offset?: number;
}

export function listRuns(params: ListRunsParams = {}): Promise<RunListResponse> {
  const search = new URLSearchParams();
  if (params.status) search.set('status', params.status);
  if (params.run_type) search.set('run_type', params.run_type);
  search.set('limit', String(params.limit ?? 20));
  search.set('offset', String(params.offset ?? 0));
  return apiGet<RunListResponse>(`/runs?${search.toString()}`);
}

export function getRun(runId: string): Promise<PipelineRunResponse> {
  return apiGet<PipelineRunResponse>(`/runs/${encodeURIComponent(runId)}`);
}

export function getRunEvents(runId: string): Promise<RunEventResponse[]> {
  return apiGet<RunEventResponse[]>(`/runs/${encodeURIComponent(runId)}/events`);
}

export function getRunResults(runId: string): Promise<RunResultsResponse> {
  return apiGet<RunResultsResponse>(`/runs/${encodeURIComponent(runId)}/results`);
}

export function cancelRun(runId: string): Promise<PipelineRunResponse> {
  return apiPostEmpty<PipelineRunResponse>(`/runs/${encodeURIComponent(runId)}/cancel`);
}

export function createRun(config: PipelineRunRequest, files: File[]): Promise<PipelineRunResponse> {
  const formData = new FormData();
  formData.append('config', JSON.stringify(config));
  for (const file of files) {
    formData.append('files', file);
  }
  return apiPostForm<PipelineRunResponse>('/runs', formData);
}
