import type {
  BatchConfig,
  ConfigurationPayload,
  EnvConfig,
  EnvUpdate,
  InstantConfig,
  PreviewPayload,
  ConfigFilesPayload,
  RecordDetail,
  RecordsPayload,
  RunDetail,
  StartPayload,
  StatusPayload,
} from './types/dashboard'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: {
        ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
        ...init?.headers,
      },
    })
  } catch {
    throw new Error('连接不到本地服务。请确认监控服务已在 127.0.0.1:8765 启动。')
  }

  const payload: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const body = payload as { detail?: unknown; error?: unknown } | null
    const detail = body?.detail ?? body?.error
    let message: string
    if (typeof detail === 'string') message = detail
    else if (Array.isArray(detail)) message = detail.map((entry) => {
      if (typeof entry === 'object' && entry !== null && 'msg' in entry) return String(entry.msg)
      return JSON.stringify(entry)
    }).join('；')
    else message = `请求失败 (${response.status})`
    throw new Error(message)
  }
  return payload as T
}

export const api = {
  configuration: (path?: string) => request<ConfigurationPayload>(
    `/api/config${path ? `?path=${encodeURIComponent(path)}` : ''}`,
  ),
  saveConfiguration: (update: { instant?: InstantConfig; batch?: BatchConfig; config_path: string }) =>
    request<ConfigurationPayload>('/api/config', {
      method: 'PUT',
      body: JSON.stringify(update),
    }),
  environment: () => request<EnvConfig>('/api/env'),
  saveEnvironment: (changes: EnvUpdate) => request<EnvConfig>('/api/env', {
    method: 'PUT',
    body: JSON.stringify(changes),
  }),
  configFiles: () => request<ConfigFilesPayload>('/api/config/files'),
  status: () => request<StatusPayload>('/api/status'),
  records: (refresh = false) => request<RecordsPayload>(`/api/records${refresh ? '?refresh=true' : ''}`),
  record: (id: string) => request<RecordDetail>(`/api/records/detail?id=${encodeURIComponent(id)}`),
  run: (id: string) => request<RunDetail>(`/api/runs/${encodeURIComponent(id)}`),
  preview: (mode: 'instant' | 'batch', dataset?: string, configPath?: string) => request<PreviewPayload>('/api/preview', {
    method: 'POST',
    body: JSON.stringify({ mode, ...(dataset ? { dataset } : {}), ...(configPath ? { config_path: configPath } : {}) }),
  }),
  start: (mode: 'instant' | 'batch', dataset?: string, waitTimeout = 1800, configPath?: string) =>
    request<StartPayload>('/api/runs', {
      method: 'POST',
      body: JSON.stringify({ mode, ...(dataset ? { dataset } : {}), ...(configPath ? { config_path: configPath } : {}), wait_timeout: waitTimeout }),
    }),
}

/** Plain links: the browser streams these straight from the local server. */
export const recordLinks = {
  file: (id: string, name: string, download = false) =>
    `/api/records/file?id=${encodeURIComponent(id)}&name=${encodeURIComponent(name)}${download ? '&download=true' : ''}`,
  zip: (scope: { id?: string; group?: string }) => {
    const query = scope.id !== undefined ? `?id=${encodeURIComponent(scope.id)}`
      : scope.group !== undefined ? `?group=${encodeURIComponent(scope.group)}` : ''
    return `/api/records/download${query}`
  },
}
