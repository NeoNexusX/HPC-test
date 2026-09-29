import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { api } from '../api'
import type {
  BatchConfig,
  EnvConfig,
  EnvUpdate,
  InstantConfig,
  OssRecord,
  PreviewPayload,
  RecordDetail,
  RecordsPayload,
  RunDetail,
  RunSummary,
  StatusPayload,
} from '../types/dashboard'

const DEFAULT_PROFILE = 'config/instant.local.json'

export function useDashboard() {
  const config = ref<InstantConfig | null>(null)
  const batch = ref<BatchConfig | null>(null)
  const environment = ref<EnvConfig | null>(null)
  const configPath = shallowRef('config/instant.local.json')
  const profiles = ref<string[]>([])
  const profileResources = ref<Record<string, InstantConfig['resources']>>({})
  const batchSaved = shallowRef(false)
  const runs = ref<RunSummary[]>([])
  const records = ref<OssRecord[]>([])
  const recordGroups = ref<RecordsPayload['groups']>([])
  const recordsInfo = ref<Pick<RecordsPayload, 'archives' | 'error' | 'synced_at'> | null>(null)
  const selectedRecordId = shallowRef<string | null>(null)
  const selectedRecord = ref<RecordDetail | null>(null)
  const recordLoading = shallowRef(false)
  const selectedRunId = shallowRef<string | null>(null)
  const selectedRun = ref<RunDetail | null>(null)
  const preview = ref<PreviewPayload | null>(null)
  const loading = shallowRef(true)
  const busy = shallowRef(false)
  const syncing = shallowRef(false)
  const error = shallowRef('')
  const notice = shallowRef('')
  const lastSynced = shallowRef<Date | null>(null)

  const activeRuns = computed(() => runs.value.filter((run) => isActive(run.status)))
  const recentRuns = computed(() => runs.value.slice(0, 8))
  let lastDetailFetch = 0

  function isActive(status: string) {
    return ['running', 'pending', 'queued', 'submitting', 'starting', 'initing', 'retrying', 'restarting']
      .includes(status.toLowerCase())
  }

  function applyStatus(response: StatusPayload) {
    runs.value = response.runs
    lastSynced.value = new Date()
  }

  function applyRecords(payload: RecordsPayload) {
    if (!same(payload.records, records.value)) records.value = payload.records
    recordGroups.value = payload.groups
    recordsInfo.value = { archives: payload.archives, error: payload.error, synced_at: payload.synced_at }
    if (!selectedRecordId.value && payload.records.length) void selectRecord(payload.records[0].id)
  }

  // Picks up config files added, edited or deleted outside the UI without a page reload.
  async function syncConfigFiles() {
    try {
      const data = await api.configFiles()
      if (!same(data.profiles, profiles.value)) profiles.value = data.profiles
      if (!same(data.profile_resources, profileResources.value)) profileResources.value = data.profile_resources
      if (!same(data.batch, batch.value)) batch.value = data.batch
      batchSaved.value = data.batch_saved
      const missing = configPath.value
      if (!busy.value && !data.profiles.includes(missing)) {
        await selectProfile(DEFAULT_PROFILE)
        if (configPath.value === DEFAULT_PROFILE) notice.value = `${missing} 已不在磁盘上，已切换到 ${DEFAULT_PROFILE}`
      }
    } catch {
      // Connection problems are already reported by the status request.
    }
  }

  async function refresh(force = false) {
    if (syncing.value) return
    syncing.value = true
    try {
      const wasActive = activeRuns.value.map(run => run.id)
      const response = await api.status()
      applyStatus(response)
      // A run that just ended has uploaded its manifest, so list OSS again instead of waiting for the cache.
      const ended = wasActive.some(id => !activeRuns.value.some(run => run.id === id))
      try {
        applyRecords(await api.records(force || ended))
      } catch (cause) {
        error.value = message(cause)
      }
      await syncConfigFiles()
      if (!selectedRunId.value && response.runs.length) selectedRunId.value = response.runs[0].id
      const selectedSummary = response.runs.find((run) => run.id === selectedRunId.value)
      const shouldRefreshDetail = selectedRunId.value && (
        !selectedRun.value || isActive(selectedSummary?.status ?? '') || Date.now() - lastDetailFetch > 30_000)
      if (shouldRefreshDetail && selectedRunId.value) {
        try {
          selectedRun.value = await api.run(selectedRunId.value)
          lastDetailFetch = Date.now()
        } catch (cause) {
          selectedRun.value = null
          error.value = message(cause)
        }
      }
    } catch (cause) {
      error.value = message(cause)
    } finally {
      syncing.value = false
    }
  }

  async function load() {
    loading.value = true
    const results = await Promise.allSettled([api.configuration(), api.environment(), api.status(), api.records()])
    const [configurationResult, environmentResult, statusResult, recordsResult] = results
    const failures: string[] = []
    if (configurationResult.status === 'fulfilled') {
      const data = configurationResult.value
      config.value = data.instant
      batch.value = data.batch
      configPath.value = data.config_path
      profiles.value = data.profiles
      profileResources.value = data.profile_resources
      batchSaved.value = data.batch_saved
    } else failures.push(message(configurationResult.reason))
    if (environmentResult.status === 'fulfilled') environment.value = environmentResult.value
    else failures.push(message(environmentResult.reason))
    if (statusResult.status === 'fulfilled') {
      applyStatus(statusResult.value)
      if (runs.value.length) await selectRun(runs.value[0].id)
    } else failures.push(message(statusResult.reason))
    if (recordsResult.status === 'fulfilled') applyRecords(recordsResult.value)
    else failures.push(message(recordsResult.reason))
    error.value = [...new Set(failures)].join('；')
    loading.value = false
  }

  async function selectRun(id: string) {
    selectedRunId.value = id
    try {
      selectedRun.value = await api.run(id)
      lastDetailFetch = Date.now()
      error.value = ''
    } catch (cause) {
      selectedRun.value = null
      error.value = message(cause)
    }
  }

  async function selectRecord(id: string) {
    selectedRecordId.value = id
    if (selectedRecord.value?.id === id) return
    recordLoading.value = true
    try {
      const detail = await api.record(id)
      if (selectedRecordId.value === id) selectedRecord.value = detail
    } catch (cause) {
      error.value = message(cause)
    } finally {
      recordLoading.value = false
    }
  }

  async function selectProfile(path: string) {
    busy.value = true
    try {
      const data = await api.configuration(path)
      config.value = data.instant
      batch.value = data.batch
      configPath.value = data.config_path
      profiles.value = data.profiles
      profileResources.value = data.profile_resources
      batchSaved.value = data.batch_saved
      notice.value = `已载入 ${path}`
      error.value = ''
    } catch (cause) {
      error.value = message(cause)
    } finally {
      busy.value = false
    }
  }

  async function saveConfig(nextConfig: InstantConfig) {
    await persist({ instant: nextConfig, config_path: configPath.value }, '作业配置已保存')
  }

  async function saveBatch(nextBatch: BatchConfig) {
    await persist({ batch: nextBatch, config_path: configPath.value }, '运行序列已保存')
  }

  async function saveAsProfile(path: string) {
    if (!config.value) return
    await persist({ instant: config.value, config_path: path }, `已创建 ${path}`)
  }

  async function persist(update: { instant?: InstantConfig; batch?: BatchConfig; config_path: string }, success: string) {
    busy.value = true
    try {
      const data = await api.saveConfiguration(update)
      config.value = data.instant
      batch.value = data.batch
      configPath.value = data.config_path
      profiles.value = data.profiles
      profileResources.value = data.profile_resources
      batchSaved.value = data.batch_saved
      notice.value = success
      error.value = ''
      preview.value = null
    } catch (cause) {
      error.value = message(cause)
    } finally {
      busy.value = false
    }
  }

  async function saveEnvironment(update: EnvUpdate) {
    busy.value = true
    try {
      environment.value = await api.saveEnvironment(update)
      notice.value = '环境变量已保存'
      error.value = ''
    } catch (cause) {
      error.value = message(cause)
    } finally {
      busy.value = false
    }
  }

  async function runPreview(mode: 'instant' | 'batch', dataset?: string) {
    busy.value = true
    preview.value = null
    try {
      preview.value = await api.preview(mode, dataset, configPath.value)
      notice.value = '预览校验通过'
      error.value = ''
    } catch (cause) {
      error.value = message(cause)
    } finally {
      busy.value = false
    }
  }

  async function startRun(mode: 'instant' | 'batch', dataset?: string, waitTimeout = 1800) {
    busy.value = true
    try {
      const started = await api.start(mode, dataset, waitTimeout, configPath.value)
      notice.value = mode === 'batch' ? '批量序列已开始执行' : '单次作业已开始提交'
      error.value = ''
      preview.value = null
      selectedRunId.value = started.id
      await refresh()
      return started.id
    } catch (cause) {
      error.value = message(cause)
      return null
    } finally {
      busy.value = false
    }
  }

  let timer: ReturnType<typeof setInterval> | undefined
  onMounted(() => {
    void load()
    timer = setInterval(() => { void refresh() }, 5000)
  })
  onUnmounted(() => { if (timer) clearInterval(timer) })

  return {
    config, batch, environment, configPath, profiles, profileResources, batchSaved,
    runs, recentRuns, activeRuns, selectedRunId, selectedRun,
    records, recordGroups, recordsInfo, selectedRecordId, selectedRecord, recordLoading, selectRecord,
    preview, loading, busy, syncing, error, notice, lastSynced,
    refresh, selectRun, selectProfile, saveConfig, saveBatch, saveAsProfile, saveEnvironment,
    runPreview, startRun,
  }
}

function same(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right)
}

function message(cause: unknown): string {
  return cause instanceof Error ? cause.message : '操作失败，请查看本地服务日志。'
}
