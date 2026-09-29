<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue'
import { recordLinks } from '../../api'
import type { OssRecord, RecordDetail, RecordsPayload } from '../../types/dashboard'

const props = defineProps<{
  records: OssRecord[]
  groups: RecordsPayload['groups']
  info: Pick<RecordsPayload, 'archives' | 'error' | 'synced_at'> | null
  selectedId: string | null
  detail: RecordDetail | null
  loading?: boolean
  compact?: boolean
}>()
const emit = defineEmits<{ select: [id: string]; refresh: [] }>()

const ALL = '*'
const group = shallowRef(ALL)
const query = shallowRef('')
const pageSize = 10
const page = ref(1)

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return props.records.filter(record => (group.value === ALL || record.group === group.value)
    && (!needle || [record.run_id, record.job_id ?? '', record.group, record.dataset ?? '']
      .some(value => value.toLowerCase().includes(needle))))
})
const pageCount = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)))
const visible = computed(() => props.compact ? props.records.slice(0, 5)
  : filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const failedCount = computed(() => props.records.filter(record => record.status === 'failed').length)
watch([group, query], () => { page.value = 1 })
watch(pageCount, count => { if (page.value > count) page.value = count })

const selectedDetail = computed(() => props.detail?.id === props.selectedId ? props.detail : null)
const selected = computed(() => selectedDetail.value ?? props.records.find(record => record.id === props.selectedId) ?? null)
const topFiles = computed(() => selectedDetail.value?.files.filter(file => !file.name.startsWith('zarr-delta/')) ?? [])
const zarrFiles = computed(() => selectedDetail.value?.files.filter(file => file.name.startsWith('zarr-delta/')) ?? [])
const hasImage = computed(() => topFiles.value.some(file => file.name === 'umap_image.jpg'))
const logLines = computed(() => selectedDetail.value?.log.slice(-40) ?? [])
const result = computed(() => selectedDetail.value?.result ?? null)
const groupZip = computed(() => group.value === ALL ? recordLinks.zip({}) : recordLinks.zip({ group: group.value }))

function groupLabel(name: string): string {
  return name || '单独运行'
}

function formatDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`
}

function formatSeconds(seconds: number): string {
  return seconds < 60 ? `${seconds.toFixed(1)} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`
}

function basename(path: string): string {
  return path.replace(/\/$/, '').split('/').at(-1) ?? path
}

function truncate(value: string, length = 26): string {
  return value.length > length ? `${value.slice(0, length)}…` : value
}
</script>

<template>
  <section class="records">
    <div class="records-heading">
      <div>
        <div class="section-kicker">OSS ARCHIVE</div>
        <h2 class="section-title">运行记录</h2>
      </div>
      <span class="records-count">{{ compact ? `最近 ${Math.min(5, records.length)} 条 / ` : '' }}OSS 共 {{ records.length }} 条<template v-if="failedCount"> · 失败 {{ failedCount }}</template></span>
    </div>
    <p v-if="info?.error" class="records-note">{{ info.synced_at ? `刷新 OSS 列表失败，显示的是 ${formatDate(info.synced_at)} 获取的列表` : '读取 OSS 记录失败' }}（{{ info.error }}）</p>

    <div v-if="!compact" class="records-toolbar">
      <label class="records-filter"><span>分组</span>
        <select v-model="group">
          <option :value="ALL">全部分组（{{ records.length }}）</option>
          <option v-for="item in groups" :key="item.name" :value="item.name">{{ groupLabel(item.name) }}（{{ item.count }}）</option>
        </select>
      </label>
      <label class="records-filter records-search"><span>搜索</span>
        <input v-model="query" type="search" placeholder="run id / job id / 数据集" spellcheck="false" />
      </label>
      <a class="records-zip" :href="groupZip" download>{{ group === ALL ? '下载全部记录' : `下载「${groupLabel(group)}」` }}（zip）</a>
      <button type="button" class="records-refresh" @click="emit('refresh')">重新读取 OSS ↻</button>
    </div>
    <p v-if="!compact" class="records-hint">来源：{{ info?.archives.join('、') || '—' }}。每条记录是一个含 manifest.json 的结果文件夹，与提交它的机器无关；打包下载含 zarr-delta，较大的分组需要十几秒。</p>

    <div v-if="records.length" class="records-body" :class="{ 'records-body-compact': compact }">
      <div class="records-list-wrap">
        <div class="records-list" role="listbox" aria-label="OSS 运行记录">
          <button v-for="record in visible" :key="record.id" type="button" role="option" class="records-row"
            :class="{ selected: record.id === selectedId }" :aria-selected="record.id === selectedId" @click="emit('select', record.id)">
            <span class="records-dot" :class="record.status" />
            <span class="records-row-main">
              <strong :title="record.run_id">{{ truncate(record.run_id, 26) }}</strong>
              <small>{{ groupLabel(record.group) }} · {{ formatDate(record.finished_at) }}</small>
            </span>
            <span class="records-row-status" :class="record.status">{{ record.status === 'succeeded' ? '成功' : '失败' }}</span>
          </button>
          <p v-if="!visible.length" class="records-empty-filter">没有匹配的记录。</p>
        </div>
        <div v-if="!compact && pageCount > 1" class="records-pagination">
          <button type="button" :disabled="page === 1" @click="page--">上一页</button>
          <span>{{ page }} / {{ pageCount }} · 匹配 {{ filtered.length }} 条</span>
          <button type="button" :disabled="page === pageCount" @click="page++">下一页</button>
        </div>
      </div>

      <div class="records-detail">
        <template v-if="selected">
          <div class="detail-topline">
            <span class="detail-eyebrow">OSS RECORD</span>
            <span class="detail-state" :class="selected.status"><span class="records-dot" :class="selected.status" />{{ selected.status === 'succeeded' ? '成功' : '失败' }}</span>
          </div>
          <h3 class="detail-id">{{ selected.run_id }}</h3>
          <div class="detail-meta">
            <div><span>完成时间</span><strong>{{ formatDate(selected.finished_at) }}</strong></div>
            <div><span>分组</span><strong>{{ groupLabel(selected.group) }}</strong></div>
            <div v-if="selected.job_id"><span>Job ID</span><strong>{{ selected.job_id }}</strong></div>
            <div><span>Attempt</span><strong>{{ selected.attempt }}</strong></div>
            <div v-if="selected.dataset"><span>数据集</span><strong :title="selected.dataset">{{ basename(selected.dataset) }}</strong></div>
            <div v-if="result?.cpu_model"><span>CPU</span><strong>{{ result.cpu_model }}<template v-if="result.cpus"> · {{ result.cpus }} vCPU</template></strong></div>
            <div v-if="result?.total_seconds != null"><span>流水线耗时</span><strong>{{ formatSeconds(result.total_seconds) }}</strong></div>
            <div v-if="result?.timings?.list_seconds != null"><span>OSS 列举</span><strong>{{ formatSeconds(result.timings.list_seconds) }}</strong></div>
            <div v-if="result?.timings?.massflow_import_seconds != null"><span>MassFlow 导入</span><strong>{{ formatSeconds(result.timings.massflow_import_seconds) }}</strong></div>
            <div v-if="result?.timings?.download_seconds != null"><span>实际下载</span><strong>{{ formatSeconds(result.timings.download_seconds) }}<template v-if="result.dataset?.downloaded_bytes != null"> · {{ formatBytes(result.dataset.downloaded_bytes) }}</template></strong></div>
            <div v-if="result?.timings?.import_download_overlap_seconds != null"><span>导入与下载重叠</span><strong>{{ formatSeconds(result.timings.import_download_overlap_seconds) }}</strong></div>
            <div v-if="result?.peak_rss_mib != null"><span>峰值内存</span><strong>{{ (result.peak_rss_mib / 1024).toFixed(2) }} GiB</strong></div>
            <div v-if="result?.umap?.pixels"><span>UMAP 规模</span><strong>{{ result.umap.pixels }} 像素 × {{ result.umap.features }} 特征 · 拟合 {{ result.umap.fit_samples }}</strong></div>
            <div v-if="result?.labels"><span>标签</span><strong>{{ Object.entries(result.labels).map(([key, value]) => `${key}=${value}`).join(' · ') }}</strong></div>
          </div>
          <p v-if="result?.stages_seconds && Object.keys(result.stages_seconds).length" class="detail-stages">
            <span v-for="(seconds, stage) in result.stages_seconds" :key="stage">{{ stage }} <b>{{ seconds.toFixed(1) }}s</b></span>
          </p>
          <p class="detail-location" :title="selected.oss_prefix">{{ selected.oss_prefix }}</p>

          <p v-if="loading && !selectedDetail" class="detail-loading">正在从 OSS 读取 result.json 和 run.log…</p>
          <template v-if="selectedDetail">
            <img v-if="hasImage" class="detail-image" :src="recordLinks.file(selected.id, 'umap_image.jpg')" alt="UMAP 结果图" loading="lazy" />
            <div class="detail-files">
              <div class="detail-files-heading"><span>文件</span><small>{{ selected.file_count }} 个 · {{ formatBytes(selected.size) }}</small></div>
              <a v-for="file in topFiles" :key="file.name" :href="recordLinks.file(selected.id, file.name, true)" download>
                <span>{{ file.name }}</span><small>{{ formatBytes(file.size) }}</small>
              </a>
              <p v-if="zarrFiles.length" class="detail-zarr">zarr-delta/ 共 {{ zarrFiles.length }} 个对象 · {{ formatBytes(zarrFiles.reduce((sum, file) => sum + file.size, 0)) }}（包含在 zip 中）</p>
            </div>
            <div class="detail-actions">
              <a class="records-zip" :href="recordLinks.zip({ id: selected.id })" download>下载本条（zip）</a>
              <a v-if="selected.group" class="records-zip records-zip-quiet" :href="recordLinks.zip({ group: selected.group })" download>下载本组 {{ selected.group }}（zip）</a>
            </div>
          </template>

          <div class="log-heading"><span>run.log（容器内输出）</span><small>最近 {{ logLines.length }} 行</small></div>
          <div class="log-viewer">
            <div v-if="logLines.length" class="log-lines"><p v-for="(line, index) in logLines" :key="`${index}-${line}`">{{ line }}</p></div>
            <p v-else class="log-empty">{{ selectedDetail ? '这条记录没有 run.log。' : '选择后从 OSS 读取。' }}</p>
          </div>
        </template>
        <p v-else class="select-empty">选择左侧记录查看详情。</p>
      </div>
    </div>

    <div v-else class="records-empty">
      <h3>{{ info?.error ? 'OSS 记录暂不可读' : 'OSS 上还没有运行记录' }}</h3>
      <p>作业完成并上传 manifest.json 后，记录会出现在这里。</p>
    </div>
  </section>
</template>

<style scoped>
.records { min-width: 0; }
.records-heading { display: flex; align-items: end; justify-content: space-between; margin-bottom: 14px; gap: 12px; }
.section-kicker { font: 700 10px/1.4 var(--font-mono); letter-spacing: .17em; color: var(--accent); }
.section-title { font-size: 23px; letter-spacing: -.035em; margin: 4px 0 0; }
.records-count { color: var(--muted); font-size: 12px; white-space: nowrap; }
.records-note { margin: 0 0 12px; padding: 9px 12px; border-radius: 8px; background: #fff4e5; color: #9a5b1c; font-size: 11px; line-height: 1.5; }
.records-toolbar { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; margin-bottom: 8px; }
.records-filter { display: grid; gap: 5px; font-size: 10px; font-weight: 700; color: var(--muted); }
.records-filter select, .records-filter input { min-height: 34px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); color: var(--ink); padding: 0 10px; font: 12px var(--font-body, inherit); }
.records-search { flex: 1 1 200px; }
.records-zip, .records-refresh { display: inline-flex; align-items: center; min-height: 34px; border-radius: 8px; padding: 0 12px; font-size: 11px; font-weight: 700; text-decoration: none; cursor: pointer; white-space: nowrap; }
.records-zip { background: var(--ink); color: #fff; border: 1px solid var(--ink); }
.records-zip-quiet { background: #f5faf9; color: #277d80; border-color: #cbdfe0; }
.records-refresh { border: 1px solid #cbdfe0; background: #f5faf9; color: #277d80; font-family: inherit; }
.records-hint { margin: 0 0 14px; color: var(--muted); font-size: 10px; line-height: 1.6; overflow-wrap: anywhere; }
.records-body { display: grid; grid-template-columns: minmax(270px, .82fr) minmax(360px, 1.2fr); border: 1px solid var(--border); border-radius: 20px; background: var(--surface); min-height: 420px; overflow: hidden; box-shadow: var(--shadow-soft); }
.records-body-compact { min-height: 366px; }
.records-list-wrap { display: flex; flex-direction: column; min-width: 0; border-right: 1px solid var(--border); }
.records-list { min-width: 0; max-height: 760px; overflow-y: auto; }
.records-row { border: 0; border-bottom: 1px solid var(--border); width: 100%; min-height: 66px; display: flex; align-items: center; text-align: left; gap: 12px; background: transparent; padding: 12px 17px; cursor: pointer; color: var(--ink); }
.records-row:hover, .records-row.selected { background: #edf8f7; }
.records-row.selected { box-shadow: inset 3px 0 0 var(--accent); }
.records-row:focus-visible { outline: 2px solid var(--accent); outline-offset: -3px; }
.records-dot { width: 8px; height: 8px; border-radius: 50%; background: #6ca879; flex: 0 0 auto; }
.records-dot.failed { background: #d97463; }
.records-row-main { min-width: 0; flex: 1; display: grid; gap: 4px; }
.records-row-main strong { font: 600 12px/1.3 var(--font-mono); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.records-row-main small { font-size: 11px; color: var(--muted); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.records-row-status { font-size: 11px; white-space: nowrap; color: #4f895d; }
.records-row-status.failed { color: #bd6252; }
.records-empty-filter { margin: 18px; color: var(--muted); font-size: 12px; }
.records-pagination { display: flex; align-items: center; justify-content: space-between; gap: 6px; margin-top: auto; padding: 12px; border-top: 1px solid var(--border); color: var(--muted); font-size: 10px; }
.records-pagination button { border: 1px solid #cbdfe0; border-radius: 7px; background: #f5faf9; color: #277d80; padding: 6px 9px; cursor: pointer; font-family: inherit; font-size: 10px; font-weight: 700; }
.records-pagination button:disabled { opacity: .45; cursor: default; }
.records-detail { padding: 22px 24px 24px; min-width: 0; display: flex; flex-direction: column; }
.detail-topline { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.detail-eyebrow { font: 700 10px/1.4 var(--font-mono); letter-spacing: .15em; color: var(--muted); }
.detail-state { display: inline-flex; align-items: center; gap: 9px; font-size: 11px; color: #4f895d; }
.detail-state.failed { color: #bd6252; }
.detail-id { font: 600 16px/1.4 var(--font-mono); overflow-wrap: anywhere; margin: 12px 0 18px; color: var(--ink); }
.detail-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px 18px; padding-bottom: 14px; }
.detail-meta div { min-width: 0; display: grid; gap: 5px; }
.detail-meta span { color: var(--muted); font-size: 10px; }
.detail-meta strong { font-size: 11px; font-weight: 600; overflow-wrap: anywhere; }
.detail-stages { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 10px; }
.detail-stages span { padding: 4px 8px; border-radius: 6px; background: #edf5f5; color: #527880; font: 10px var(--font-mono); }
.detail-stages b { color: var(--ink); font-weight: 700; }
.detail-location { margin: 0 0 14px; color: var(--muted); font: 10px/1.5 var(--font-mono); overflow-wrap: anywhere; user-select: all; }
.detail-loading { color: var(--muted); font-size: 11px; }
.detail-image { display: block; max-width: 100%; max-height: 260px; margin: 0 0 14px; border: 1px solid var(--border); border-radius: 10px; object-fit: contain; background: #fff; }
.detail-files { display: grid; gap: 4px; margin-bottom: 12px; }
.detail-files-heading { display: flex; justify-content: space-between; font-size: 11px; font-weight: 700; margin-bottom: 4px; }
.detail-files-heading small { color: var(--muted); font-weight: 400; }
.detail-files a { display: flex; justify-content: space-between; gap: 12px; padding: 6px 8px; border: 1px solid #e5eded; border-radius: 7px; background: #fafcfc; color: #277d80; font: 11px var(--font-mono); text-decoration: none; }
.detail-files a:hover { background: #edf8f7; }
.detail-files a small { color: var(--muted); white-space: nowrap; }
.detail-zarr { margin: 2px 0 0; color: var(--muted); font-size: 10px; }
.detail-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 4px; }
.log-heading { display: flex; align-items: center; justify-content: space-between; margin-top: auto; border-top: 1px solid var(--border); padding: 17px 0 10px; font-size: 11px; font-weight: 700; }
.log-heading small { color: var(--muted); font-weight: 400; }
.log-viewer { border-radius: 10px; background: #152c37; color: #c0d7dc; font: 10px/1.65 var(--font-mono); height: 180px; overflow: auto; padding: 12px 14px; }
.log-lines p { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.log-empty { margin: 0; color: #829ea7; }
.select-empty { margin: auto; color: var(--muted); font-size: 12px; }
.records-empty { display: grid; justify-items: center; padding: 45px 24px; border: 1px dashed #cbdce0; border-radius: 18px; background: #f8fbfb; text-align: center; }
.records-empty h3 { margin: 0 0 4px; font-size: 17px; }
.records-empty p { margin: 0; max-width: 320px; line-height: 1.7; font-size: 12px; color: var(--muted); }
@media (max-width: 900px) { .records-body { grid-template-columns: 1fr; }.records-list-wrap { border-right: 0; border-bottom: 1px solid var(--border); }.records-list { max-height: 300px; } }
@media (max-width: 520px) { .detail-meta { grid-template-columns: 1fr; }.records-detail { padding: 20px 17px; } }
</style>
