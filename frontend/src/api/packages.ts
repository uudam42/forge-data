import { apiPostEmpty } from './client';
import type { OpenFolderResponse } from './types';

export function openPackageFolder(packageId: string): Promise<OpenFolderResponse> {
  return apiPostEmpty<OpenFolderResponse>(`/packages/${encodeURIComponent(packageId)}/open-folder`);
}
