<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { BatchChild, CloudResource, RunDetail, RunSummary } from '../../types/dashboard'

const props = defineProps<{
  runs: RunSummary[]
  selectedId: string | null
  detail: RunDetail | null
  compact?: boolean
}>()
const emit = defineEmits<{ select: [id: string] }>()

const pageSize = 10
const page = ref(1)
const pageCount = computed(() => Math.max(1, Math.ceil(props.runs.length / pageSize)))
const visibleRuns = computed(() => props.compact ? props.runs.slice(0, 5)
  : props.runs.slice((page.value - 1) * pageSize, page.value * pageSize))
watch(() => props.selectedId, id => {
  if (id && !props.compact) {
    const index = props.runs.findIndex(run => run.id === id)
    if (index >= 0) page.value = Math.floor(index / pageSize) + 1
  }
}, { immediate: true })
watch(pageCount, count => { if (page.value > count) page.value = count })
const selectedDetail = computed(() => props.detail?.id === props.selectedId ? props.detail : null)
const selected = computed(() => selectedDetail.value ?? props.runs.find((run) => run.id === props.selectedId) ?? null)
const recentLines = computed(() => selectedDetail.value?.lines?.slice(-30) ?? [])
const children = computed(() => selectedDetail.value?.children ?? [])
const singleResult = computed(() => {
  const value = selectedDetail.value?.summary?.results
  return typeof value === 'string' ? value : null
})

function stateLabel(run: RunSummary | null): string {
  if (!run) return '待选择'
  const key = run.status.toLowerCase()
  if (['running', 'submitting', 'starting'].includes(key)) return '运行中'
  if (['pending', 'queued'].includes(key)) return '排队中'
  if (key === 'initing') return '启动中'
  if (['retrying', 'restarting'].includes(key)) return '重试中'
  if (key === 'suspended') return '已暂停'
  if (['success', 'succeeded', 'succeed', 'completed'].includes(key)) return '已完成'
  if (['failed', 'error', 'exception', 'expired'].includes(key)) return '失败'
  if (key === 'deleted') return '已删除'
  if (key === 'notfound') return '云端已删除'
  if (['timeout', 'timed_out'].includes(key)) return '等待超时'
  if (key === 'recorded') return '历史记录'
  return run.status
}

function stateTone(status: string): string {
  const key = status.toLowerCase()
  if (['running', 'submitting', 'starting', 'pending', 'queued', 'initing', 'retrying', 'restarting'].includes(key)) return 'live'
  if (['success', 'succeeded', 'succeed', 'completed'].includes(key)) return 'success'
  if (['failed', 'error', 'exception', 'expired'].includes(key)) return 'danger'
  return 'quiet'
}

function isFailureStatus(status: string): boolean {
  return ['failed', 'error', 'exception', 'expired'].includes(status.toLowerCase())
}

// INSTANT keeps only InstanceTypes and the system disk when a type is pinned; Cores/Memory otherwise.
function resourceLabel(resource: CloudResource | null | undefined): string {
  if (!resource) return ''
  const machine = resource.InstanceTypes?.length ? resource.InstanceTypes.join(' / ')
    : resource.Cores != null ? `${resource.Cores} vCPU / ${resource.Memory ?? '—'} GiB，自动选型` : ''
  const disk = resource.Disks?.find(item => item.Type === 'System')?.Size
  return [machine, disk != null ? `系统盘 ${disk} GiB` : ''].filter(Boolean).join(' · ')
}

function fellBack(child: BatchChild): boolean {
  return child.submitted_instance_types === 'any' && !!child.instance_type && child.instance_type !== 'any'
}

function childStatus(status: string | undefined, exitCode: number | null | undefined): string {
  if (status && status !== 'running') return stateLabel({ status } as RunSummary)
  if (exitCode === 0) return '已完成'
  if (exitCode === 4) return '等待超时'
  if (exitCode != null) return '失败'
  return '运行中'
}

function childTone(status: string | undefined, exitCode: number | null | undefined): string {
  if (exitCode === 0 || ['success', 'succeed', 'succeeded', 'completed'].includes(status?.toLowerCase() ?? '')) return 'success'
  if ((exitCode != null && exitCode !== 4) || isFailureStatus(status ?? '')) return 'danger'
  return 'live'
}

function formatDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

function truncate(value: string, length = 26): string {
  return value.length > length ? `${value.slice(0, length)}…` : value
}
</script>

<template>
  <section class="monitor">
    <div class="monitor-heading">
      <div>
        <div class="section-kicker">LOCAL LAUNCHES</div>
        <h2 class="section-title">本机提交</h2>
      </div>
      <span class="monitor-count">进行中与 1 小时内结束的 {{ runs.length }} 项 · 结果见下方 OSS 记录</span>
    </div>

    <div v-if="runs.length" class="monitor-body" :class="{ 'monitor-body-compact': compact }">
      <div class="run-list-wrap">
      <div class="run-list" role="listbox" aria-label="本机提交">
        <button
          v-for="run in visibleRuns"
          :key="run.id"
          class="run-row"
          :class="{ selected: run.id === selectedId }"
          type="button"
          role="option"
          :aria-selected="run.id === selectedId"
          @click="emit('select', run.id)"
        >
          <span class="run-state-dot" :class="stateTone(run.status)" />
          <span class="run-row-main">
            <strong class="run-id" :title="run.id">{{ truncate(run.id, 24) }}</strong>
            <small>{{ run.mode === 'batch' ? '批量序列' : '单次作业' }} · {{ formatDate(run.created_at) }}</small>
          </span>
          <span class="run-row-status" :class="stateTone(run.status)">{{ stateLabel(run) }}</span>
        </button>
      </div>
      <div v-if="!compact && pageCount > 1" class="run-pagination">
        <button type="button" :disabled="page === 1" @click="page--">上一页</button>
        <span>{{ page }} / {{ pageCount }} · 每页 {{ pageSize }} 条</span>
        <button type="button" :disabled="page === pageCount" @click="page++">下一页</button>
      </div>
      </div>

      <div class="run-detail">
        <template v-if="selected">
          <div class="detail-topline">
            <span class="detail-eyebrow">{{ selected.mode === 'batch' ? 'BATCH RUN' : 'INSTANT JOB' }}</span>
            <span class="detail-state" :class="stateTone(selected.status)">
              <span class="run-state-dot" :class="stateTone(selected.status)" />{{ stateLabel(selected) }}
            </span>
          </div>
          <h3 class="detail-id">{{ selected.id }}</h3>
          <div class="detail-meta">
            <div><span>提交时间</span><strong>{{ formatDate(selected.created_at) }}</strong></div>
            <div v-if="selected.job_id"><span>云端 Job ID</span><strong>{{ selected.job_id }}</strong></div>
            <div v-if="selectedDetail?.cloud?.status"><span>云端状态</span><strong>{{ selectedDetail.cloud.status }}</strong></div>
            <div v-if="selected.finished_at"><span>结束时间</span><strong>{{ formatDate(selected.finished_at) }}</strong></div>
            <div v-if="resourceLabel(selectedDetail?.cloud?.resource)"><span>云端资源</span><strong>{{ resourceLabel(selectedDetail?.cloud?.resource) }}</strong></div>
            <div v-if="selected.progress != null && selected.total"><span>序列进度</span><strong>{{ selected.progress }} / {{ selected.total }}</strong></div>
            <div v-if="selected.dataset"><span>数据集</span><strong :title="selected.dataset">{{ truncate(selected.dataset, 42) }}</strong></div>
            <div v-if="singleResult"><span>结果位置</span><strong :title="singleResult">{{ singleResult }}</strong></div>
          </div>
          <p v-if="selectedDetail?.cloud?.status_reason" class="reason" :class="{ 'reason-neutral': !isFailureStatus(selectedDetail.cloud.status) }">{{ selectedDetail.cloud.status_reason }}</p>
          <div v-if="selected.mode === 'batch' && selected.progress != null && selected.total" class="progress-track" role="progressbar" :aria-valuenow="selected.progress" :aria-valuemax="selected.total" aria-valuemin="0">
            <span :style="{ width: `${Math.min(100, 100 * selected.progress / selected.total)}%` }" />
          </div>
          <div v-if="selected.mode === 'batch'" class="children-block">
            <div class="children-heading"><span>序列作业</span><small>{{ children.length }} / {{ selected.total ?? '—' }}</small></div>
            <p class="children-hint">批量结果按子作业分别保存；选择下方结果地址可复制。</p>
            <div v-if="children.length" class="children-list">
              <div v-for="(child, index) in children" :key="`${child.run_id}-${index}`" class="child-row">
                <span class="child-index">{{ String(index + 1).padStart(2, '0') }}</span>
                <div class="child-main"><strong :title="child.run_id">{{ child.run_id }}</strong><small>{{ child.instance_type || '自动选择' }}<template v-if="fellBack(child)">（售罄，已回退自动选型）</template> · 第 {{ child.repeat_index ?? 1 }} 次<span v-if="child.job_id"> · {{ child.job_id }}</span></small><small v-if="child.results" class="child-result" :title="child.results">结果：{{ child.results }}</small></div>
                <span class="child-status" :class="childTone(child.status, child.exit_code)">{{ childStatus(child.status, child.exit_code) }}</span>
              </div>
            </div>
            <p v-else class="children-empty">等待第一项作业开始。</p>
          </div>
          <div class="log-heading"><span>本地执行输出</span><small>最近 {{ recentLines.length }} 行</small></div>
          <div class="log-viewer" aria-live="polite">
            <div v-if="recentLines.length" class="log-lines">
              <p v-for="(line, index) in recentLines" :key="`${index}-${line}`">{{ line }}</p>
            </div>
            <p v-else class="log-empty">提交后，这里会显示本地脚本的执行输出。</p>
          </div>
          <p v-if="selected.status.toLowerCase().includes('timeout')" class="timeout-note">本地等待已超时；云端作业可能仍在运行，请以云端状态为准。</p>
        </template>
        <p v-else class="select-empty">选择左侧记录查看运行详情。</p>
      </div>
    </div>

    <div v-else class="monitor-empty">
      <div class="empty-orbit"><span /></div>
      <h3>最近没有从本页提交的作业</h3>
      <p>提交后，进度和本地输出会出现在这里；完成的结果在 OSS 记录里查看。</p>
    </div>
  </section>
</template>

<style scoped>
.monitor { min-width: 0; }
.monitor-heading { display: flex; align-items: end; justify-content: space-between; margin-bottom: 18px; gap: 12px; }
.section-kicker { font: 700 10px/1.4 var(--font-mono); letter-spacing: .17em; color: var(--accent); }
.section-title { font-size: 23px; letter-spacing: -.035em; margin: 4px 0 0; }
.monitor-count { color: var(--muted); font-size: 12px; white-space: nowrap; }
.monitor-body { display: grid; grid-template-columns: minmax(270px, .82fr) minmax(360px, 1.2fr); border: 1px solid var(--border); border-radius: 20px; background: var(--surface); min-height: 420px; overflow: hidden; box-shadow: var(--shadow-soft); }
.monitor-body-compact { min-height: 366px; }
.run-list-wrap { display: flex; flex-direction: column; min-width: 0; border-right: 1px solid var(--border); }
.run-list { min-width: 0; max-height: 570px; overflow-y: auto; }
.run-pagination { display: flex; align-items: center; justify-content: space-between; gap: 6px; margin-top: auto; padding: 12px; border-top: 1px solid var(--border); color: var(--muted); font-size: 10px; }
.run-pagination button { border: 1px solid #cbdfe0; border-radius: 7px; background: #f5faf9; color: #277d80; padding: 6px 9px; cursor: pointer; font-family: inherit; font-size: 10px; font-weight: 700; }
.run-pagination button:disabled { opacity: .45; cursor: default; }
.run-row { border: 0; border-bottom: 1px solid var(--border); width: 100%; min-height: 75px; display: flex; align-items: center; text-align: left; gap: 12px; background: transparent; padding: 14px 17px; cursor: pointer; color: var(--ink); transition: background .15s, box-shadow .15s; }
.run-row:hover, .run-row.selected { background: #edf8f7; }
.run-row.selected { box-shadow: inset 3px 0 0 var(--accent); }
.run-row:focus-visible { outline: 2px solid var(--accent); outline-offset: -3px; }
.run-state-dot { width: 8px; height: 8px; border-radius: 50%; background: #a5b7bc; flex: 0 0 auto; }
.run-state-dot.live { background: #29afa9; box-shadow: 0 0 0 4px #d8f2ed; }
.run-state-dot.success { background: #6ca879; }
.run-state-dot.danger { background: #d97463; }
.run-row-main { min-width: 0; flex: 1; display: grid; gap: 4px; }
.run-id { font: 600 12px/1.3 var(--font-mono); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.run-row-main small { font-size: 11px; color: var(--muted); }
.run-row-status { font-size: 11px; white-space: nowrap; color: var(--muted); }
.run-row-status.live { color: #148b86; }
.run-row-status.success { color: #4f895d; }
.run-row-status.danger { color: #bd6252; }
.run-detail { padding: 22px 24px 24px; min-width: 0; display: flex; flex-direction: column; }
.detail-topline { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.detail-eyebrow { font: 700 10px/1.4 var(--font-mono); letter-spacing: .15em; color: var(--muted); }
.detail-state { display: inline-flex; align-items: center; gap: 9px; font-size: 11px; color: var(--muted); }
.detail-state.live { color: #148b86; }.detail-state.success { color: #4f895d; }.detail-state.danger { color: #bd6252; }
.detail-id { font: 600 16px/1.4 var(--font-mono); overflow-wrap: anywhere; margin: 12px 0 21px; color: var(--ink); }
.detail-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 15px 18px; padding-bottom: 20px; }
.detail-meta div { min-width: 0; display: grid; gap: 5px; }
.detail-meta span { color: var(--muted); font-size: 10px; }
.detail-meta strong { font-size: 11px; font-weight: 600; overflow-wrap: anywhere; }
.reason { padding: 10px 12px; border-radius: 8px; background: #fff0e9; color: #a44732; font-size: 11px; line-height: 1.5; }
.reason-neutral { background: #edf5f5; color: #527880; }
.progress-track { width: 100%; height: 5px; background: #e6f0f0; border-radius: 20px; overflow: hidden; margin-bottom: 12px; }
.progress-track span { display: block; height: 100%; background: var(--accent); border-radius: inherit; transition: width .3s; }
.children-block { margin-bottom: 16px; border-top: 1px solid var(--border); padding-top: 15px; }.children-heading { display: flex; justify-content: space-between; font-size: 11px; font-weight: 700; margin-bottom: 9px; }.children-heading small { color: var(--muted); font: 10px var(--font-mono); }.children-hint { margin: -2px 0 10px; color: var(--muted); font-size: 10px; line-height: 1.5; }.children-list { display: grid; gap: 6px; max-height: 170px; overflow-y: auto; }.child-row { display: flex; align-items: center; gap: 10px; border: 1px solid #e5eded; background: #fafcfc; border-radius: 8px; padding: 8px; }.child-index { color: #6b9c9f; font: 700 10px var(--font-mono); }.child-main { display: grid; gap: 3px; min-width: 0; flex: 1; }.child-main strong { font: 600 10px var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.child-main small { color: var(--muted); font-size: 9px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.child-main .child-result { white-space: normal; overflow-wrap: anywhere; user-select: text; }.child-status { font-size: 10px; color: #238887; white-space: nowrap; }.child-status.success { color: #518e60; }.child-status.danger { color: #bf6753; }.children-empty { color: var(--muted); font-size: 10px; margin: 0; }
.log-heading { display: flex; align-items: center; justify-content: space-between; margin-top: auto; border-top: 1px solid var(--border); padding: 17px 0 10px; font-size: 11px; font-weight: 700; }
.log-heading small { color: var(--muted); font-weight: 400; }
.log-viewer { border-radius: 10px; background: #152c37; color: #c0d7dc; font: 10px/1.65 var(--font-mono); height: 160px; overflow: auto; padding: 12px 14px; }
.log-lines p { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.log-empty { margin: 0; color: #829ea7; }
.timeout-note { font-size: 11px; color: #a26927; margin: 12px 0 0; }
.select-empty { margin: auto; color: var(--muted); font-size: 12px; }
.monitor-empty { display: grid; justify-items: center; padding: 45px 24px; border: 1px dashed #cbdce0; border-radius: 18px; background: #f8fbfb; text-align: center; }
.monitor-empty h3 { margin: 12px 0 2px; font-size: 17px; }.monitor-empty p { margin: 0; max-width: 300px; line-height: 1.7; font-size: 12px; color: var(--muted); }
.empty-orbit { width: 55px; height: 55px; border: 1px solid #b6dadb; border-radius: 50%; display: grid; place-items: center; background: #eef8f7; }
.empty-orbit span { width: 15px; height: 15px; border-radius: 50%; background: #77c7c2; }
@media (max-width: 900px) { .monitor-body { grid-template-columns: 1fr; }.run-list-wrap { border-right: 0; border-bottom: 1px solid var(--border); }.run-list { max-height: 245px; }.run-detail { min-height: 350px; } }
@media (max-width: 520px) { .detail-meta { grid-template-columns: 1fr; }.run-detail { padding: 20px 17px; } }
</style>
