// Thin fetch wrapper: all HTTP calls in the app go through here so error
// handling (esp. the backend's `{ detail: string | object }` error shape)
// is handled in exactly one place.

import type { ApiErrorDetail } from './types';

export class ApiError extends Error {
  status: number;
  detail: ApiErrorDetail;

  constructor(status: number, detail: ApiErrorDetail) {
    super(typeof detail === 'string' ? detail : 'Request failed');
    this.status = status;
    this.detail = detail;
    this.name = 'ApiError';
  }
}

const BASE = '/api/v1';

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail: ApiErrorDetail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && typeof body === 'object' && 'detail' in body) {
        detail = body.detail as ApiErrorDetail;
      }
    } catch {
      // no JSON body -- keep the generic detail
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  return handleResponse<T>(res);
}

export async function apiPostJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return handleResponse<T>(res);
}

export async function apiPostForm<T>(path: string, formData: FormData): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    body: formData,
  });
  return handleResponse<T>(res);
}

export async function apiPostEmpty<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: 'POST' });
  return handleResponse<T>(res);
}

/** Render an ApiError.detail as readable text, never `[object Object]`. */
export function formatApiErrorDetail(detail: ApiErrorDetail): string {
  if (typeof detail === 'string') return detail;
  try {
    return Object.entries(detail)
      .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
      .join('\n');
  } catch {
    return 'Unknown error';
  }
}
