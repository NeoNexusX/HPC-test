export interface InstantResources {
  cores: number
  memory_gib: number
  system_disk_gib: number
  instance_types?: string[]
  fallback_any_type?: boolean
  [key: string]: unknown
}

export interface UmapSettings {
  oss_bucket: string
  oss_region: string
  oss_endpoint: string
  oss_prefix: string
  source_zarr_path: string
  write_back: boolean
  work_dir: string
  skip_ion_image_chunks: boolean
  download_workers?: number
  full_matrix_mib: number
  sample_matrix_mib: number
  max_fit_samples: number
  benchmark_labels?: Record<string, string | number>
  [key: string]: unknown
}

export interface InstantConfig {
  region: string
  image: string
  private_registry?: boolean
  vswitch_id: string
  security_group_id: string
  enable_external_ip?: boolean
  oss_role_arn: string
  resources: InstantResources
  umap: UmapSettings
  [key: string]: unknown
}

export interface BatchRun {
  config: string
  instance_types?: string[]
  repeat?: number
  dataset?: string
  resources?: Partial<Pick<InstantResources, 'cores' | 'memory_gib' | 'system_disk_gib'>>
  [key: string]: unknown
}

export interface BatchConfig {
  batch_name?: string
  runs: BatchRun[]
  [key: string]: unknown
}

export type EnvSource = 'file' | 'process' | 'unset'

export interface EnvEntry {
  key: string
  value: string
  present: boolean
  source: EnvSource
}

export interface EnvConfig {
  entries: EnvEntry[]
}

export interface EnvUpdate {
  changes: Array<{ key: string; value: string | null }>
}
