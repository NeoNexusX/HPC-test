import type { BatchConfig, EnvConfig, InstantConfig, EnvUpdate } from './config'

export interface ConfigurationPayload {
  instant: InstantConfig
  batch: BatchConfig
  config_path: string
  batch_path: string
  profiles: string[]
  profile_resources: Record<string, InstantConfig['resources']>
  batch_saved: boolean
}

export interface ConfigFilesPayload {
  profiles: string[]
  profile_resources: Record<string, InstantConfig['resources']>
  batch: BatchConfig
  batch_saved: boolean
}

/** A run started from this UI; only active ones and those finished within the last hour are listed. */
export interface RunSummary {
  id: string
  mode: 'instant' | 'batch'
  status: string
  created_at: string
  job_id?: string | null
  region?: string | null
  dataset?: string | null
  image?: string | null
  progress?: number | null
  total?: number | null
  latest_line?: string | null
  cloud_status?: string | null
  finished_at?: string | null
}

export interface CloudResource {
  Cores?: number
  Memory?: number
  InstanceTypes?: string[]
  Disks?: Array<{ Size?: number; Type?: string }>
}

export interface RunDetail extends RunSummary {
  lines: string[]
  summary?: Record<string, unknown> | null
  cloud?: {
    status: string
    status_reason?: string | null
    resource?: CloudResource | null
  } | null
  children?: BatchChild[]
}

export interface BatchChild {
  run_id: string
  config_file?: string
  instance_type?: string
  submitted_instance_types?: string[] | string | null
  repeat_index?: number
  job_id?: string | null
  status?: string
  exit_code?: number | null
  dataset?: string | null
  results?: string | null
  oss_prefix?: string | null
}

export interface StatusPayload {
  runs: RunSummary[]
  active_count: number
}

/** One archived attempt on OSS: a folder holding manifest.json. */
export interface OssRecord {
  id: string
  bucket: string
  prefix: string
  group: string
  run_id: string
  attempt: string
  finished_at: string
  job_id?: string | null
  dataset?: string | null
  status: 'succeeded' | 'failed'
  umap_pass?: boolean | null
  artifacts_uploaded?: boolean | null
  oss_prefix: string
  file_count: number
  size: number
}

export interface RecordResult {
  cpu_model?: string | null
  cpus?: string | number | null
  peak_rss_mib?: number | null
  stages_seconds?: Record<string, number>
  timings?: {
    list_seconds?: number | null
    massflow_import_seconds?: number | null
    download_seconds?: number | null
    import_download_overlap_seconds?: number | null
    list_to_umap_seconds?: number | null
    pipeline_seconds?: number | null
  }
  total_seconds?: number | null
  labels?: Record<string, string | number> | null
  umap?: { pixels?: number; features?: number; fit_samples?: number; matrix_mib?: number } | null
  dataset?: { objects?: number; bytes?: number; downloaded_bytes?: number | null } | null
  image_git_sha?: string | null
  hostname?: string | null
}

export interface RecordDetail extends OssRecord {
  files: Array<{ name: string; size: number }>
  result: RecordResult | null
  log: string[]
}

export interface RecordsPayload {
  records: OssRecord[]
  groups: Array<{ name: string; count: number }>
  archives: string[]
  error?: string | null
  synced_at?: string | null
}

export interface PreviewPayload {
  mode: 'instant' | 'batch'
  valid: boolean
  preview: Record<string, unknown> | Record<string, unknown>[]
}

export interface StartPayload {
  id: string
  mode: 'instant' | 'batch'
  status: string
  created_at: string
}

export type { BatchConfig, EnvConfig, EnvUpdate, InstantConfig }
