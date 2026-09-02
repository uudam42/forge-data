// Types mirroring the Forge Data backend API contract (app/api/routes/*).
// Keep in sync with the backend Pydantic response models -- this file is the
// single source of truth for shapes used across the frontend.

export interface SensorPluginSummary {
  sensor_type: string;
  plugin_version: string;
  display_name: string;
  schema_name: string;
  schema_version: string;
  normalization_profile: string;
  normalization_profile_version: string;
  timestamp_field: string;
  numeric_fields: string[];
  required_fields: string[];
  canonical_units: Record<string, string>;
  has_feature_extractor: boolean;
}

export type RunStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancel_requested'
  | 'cancelled';

export type StageStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'skipped'
  | 'cancelled';

export interface StageRunResponse {
  stage_run_id: string;
  run_id: string;
  stage: string;
  status: StageStatus;
  started_at?: string | null;
  finished_at?: string | null;
  records_total?: number | null;
  records_processed?: number | null;
  bytes_total?: number | null;
  bytes_processed?: number | null;
  progress_fraction?: number | null;
  artifacts_created: number;
  error_code?: string | null;
  error_message?: string | null;
}

export interface RunArtifactResponse {
  stage: string;
  artifact_type: string;
  artifact_id: string;
  created_at: string;
}

export interface PipelineRunSummary {
  run_id: string;
  run_type: string;
  status: RunStatus;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  current_stage?: string | null;
  stages_total: number;
  stages_completed: number;
  error_code?: string | null;
}

export interface PipelineRunResponse {
  run_id: string;
  run_type: string;
  status: RunStatus;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  current_stage?: string | null;
  config_hash: string;
  retry_of_run_id?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  stages_total: number;
  stages_completed: number;
  stage_runs: StageRunResponse[];
  artifacts: RunArtifactResponse[];
}

export interface RunListResponse {
  runs: PipelineRunSummary[];
  limit: number;
  offset: number;
}

export interface RunEventResponse {
  event_id: number;
  event_type: string;
  detail?: string | null;
  created_at: string;
}

export interface StreamConfig {
  sensor_type: string;
  source_units: Record<string, string>;
}

export interface StageConfig {
  policy_name?: string;
  profile_name?: string;
  policy_version?: string;
  profile_version?: string;
  config?: Record<string, unknown>;
  dataset_name?: string;
  dataset_version?: string;
  description?: string;
}

export interface PipelineRunRequest {
  session_id?: string;
  customer_id?: string;
  device_id?: string;
  streams: StreamConfig[];
  synchronization?: Record<string, unknown>;
  cleaning: StageConfig;
  transformation: StageConfig;
  qc: StageConfig;
  packaging: StageConfig;
}

export interface PackageSummary {
  package_id: string;
  status: string;
  formats: string[];
  sample_count: number;
  local_path: string;
  created_at: string;
}

export interface QcIssue {
  code: string;
  severity: string;
  message: string;
  path?: string | null;
}

export interface QcSummary {
  qc_id: string;
  status: string;
  warning_count: number;
  error_count: number;
  modality_coverage: Record<string, number>;
  issues: QcIssue[];
  issues_truncated: boolean;
  report_path?: string | null;
}

export interface SplitsSummary {
  train: number;
  validation: number;
  test: number;
}

export interface ResultFile {
  name: string;
  relative_path: string;
  size_bytes: number;
  role: 'split' | 'export' | 'manifest' | 'report' | 'split_index';
}

export interface DatasetRegistrationRef {
  dataset_name: string;
  version: string;
  effective_status: string;
}

export interface RunResultsResponse {
  run_id: string;
  run_status: string;
  package: PackageSummary | null;
  qc: QcSummary | null;
  splits: SplitsSummary | null;
  files: ResultFile[];
  lineage_fingerprint: string | null;
  dataset_registrations: DatasetRegistrationRef[];
}

export interface OpenFolderResponse {
  opened: boolean;
  path: string;
}

export interface DatasetResponse {
  dataset_name: string;
  description?: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  version_count: number;
  latest_version?: string | null;
}

export interface DatasetVersionResponse {
  dataset_name: string;
  version: string;
  package_id: string;
  description?: string | null;
  tags: string[];
  status: string;
  created_at: string;
  package_status?: string | null;
  source_qc_status?: string | null;
  lineage_fingerprint?: string | null;
  effective_status: string;
  effective_status_reason?: string | null;
}

export interface LineageArtifactRef {
  artifact_type: string;
  artifact_id: string;
}

export interface LineageNode extends LineageArtifactRef {
  // Numeric pipeline position (1 = ingestion, ... 9 = package), matching
  // the backend's `ArtifactSummary.pipeline_stage: int` -- not a stage
  // name string.
  pipeline_stage: number;
  status?: string | null;
}

export interface LineageEdge {
  parent: LineageArtifactRef;
  child: LineageArtifactRef;
  relationship: string;
}

export interface LineageGraphResponse {
  root: LineageArtifactRef;
  direction: string;
  nodes: LineageNode[];
  edges: LineageEdge[];
}

export interface HealthResponse {
  status: string;
}

export type ApiErrorDetail = string | Record<string, unknown>;
