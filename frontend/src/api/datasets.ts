import { apiGet, apiPostJson } from './client';
import type { DatasetResponse, DatasetVersionResponse } from './types';

export function listDatasets(): Promise<DatasetResponse[]> {
  return apiGet<DatasetResponse[]>('/datasets');
}

export function createDataset(
  datasetName: string,
  description?: string,
  metadata?: Record<string, unknown>,
): Promise<DatasetResponse> {
  return apiPostJson<DatasetResponse>('/datasets', {
    dataset_name: datasetName,
    description,
    metadata,
  });
}

export function listDatasetVersions(datasetName: string): Promise<DatasetVersionResponse[]> {
  return apiGet<DatasetVersionResponse[]>(`/datasets/${encodeURIComponent(datasetName)}/versions`);
}

export function getLatestDatasetVersion(datasetName: string): Promise<DatasetVersionResponse> {
  return apiGet<DatasetVersionResponse>(`/datasets/${encodeURIComponent(datasetName)}/latest`);
}

export function registerDatasetVersion(
  datasetName: string,
  version: string,
  packageId: string,
  description?: string,
  tags?: string[],
): Promise<DatasetVersionResponse> {
  return apiPostJson<DatasetVersionResponse>(`/datasets/${encodeURIComponent(datasetName)}/versions`, {
    version,
    package_id: packageId,
    description,
    tags,
  });
}

/** Two-call flow described in the API contract: create-dataset (idempotent) then register-version. */
export async function registerDatasetVersionFlow(
  datasetName: string,
  version: string,
  packageId: string,
): Promise<DatasetVersionResponse> {
  await createDataset(datasetName);
  return registerDatasetVersion(datasetName, version, packageId);
}
