// /health lives outside the /api/v1 prefix, so it bypasses the shared client base URL.
import type { HealthResponse } from './types';
import { ApiError } from './client';

export async function getHealth(): Promise<HealthResponse> {
  const res = await fetch('/health');
  if (!res.ok) {
    throw new ApiError(res.status, `HTTP ${res.status}`);
  }
  return (await res.json()) as HealthResponse;
}
