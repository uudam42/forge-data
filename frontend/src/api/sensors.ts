import { apiGet } from './client';
import type { SensorPluginSummary } from './types';

export function listSensors(): Promise<SensorPluginSummary[]> {
  return apiGet<SensorPluginSummary[]>('/sensors');
}
