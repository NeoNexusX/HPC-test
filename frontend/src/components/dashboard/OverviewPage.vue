<script setup lang="ts">
import { computed } from 'vue'
import type {
  BatchConfig, InstantConfig, OssRecord, PreviewPayload, RecordDetail, RecordsPayload, RunSummary,
} from '../../types/dashboard'
import LaunchPanel from './LaunchPanel.vue'
import RecordBrowser from './RecordBrowser.vue'
import SequencePanel from './SequencePanel.vue'

const props = defineProps<{
  config: InstantConfig | null
  batch: BatchConfig | null
  configPath: string
  activeRuns: RunSummary[]
  records: OssRecord[]
  recordGroups: RecordsPayload['groups']
  recordsInfo: Pick<RecordsPayload, 'archives' | 'error' | 'synced_at'> | null
  selectedRecordId: string | null
  selectedRecord: RecordDetail | null
  recordLoading: boolean
  preview: PreviewPayload | null
  busy: boolean
}>()
const emit = defineEmits<{
  preview: [mode: 'instant' | 'batch', dataset?: string]
  start: [mode: 'instant' | 'batch', dataset?: string, waitTimeout?: number]
  selectRecord: [id: string]
  refreshRecords: []
  editSequence: []
  showRuns: []
}>()

const current = computed(() => props.activeRuns[0] ?? null)
const failedCount = computed(() => props.records.filter(record => record.status === 'failed').length)
const imageName = computed(() => props.config?.image?.split('/').at(-1) ?? '尚未配置镜像')
const sourceName = computed(() => props.config?.umap?.source_zarr_path?.split('/').at(-1) ?? '尚未配置数据集')
</script>

<template>
  <div class="overview-page">
    <section class="hero">
      <div class="hero-copy">
        <span class="hero-kicker"><span class="signal-dot" /> MASSFLOW / E-HPC INSTANT</span>
        <h1>作业运行台<span class="title-period">.</span></h1>
        <p>从配置到归档，查看每一项 UMAP 任务的执行轨迹。</p>
        <div class="hero-current">
          <span class="hero-current-label">{{ current ? '当前正在执行' : '当前状态' }}</span>
          <strong>{{ current ? current.id : '队列空闲 · 等待新任务' }}</strong>
          <small v-if="current?.latest_line">{{ current.latest_line }}</small>
          <small v-else>提交后将在这里显示最新的本地运行信息。</small>
        </div>
      </div>
      <div class="flow-diagram" aria-label="运行流程：本地提交到 E-HPC INSTANT，再归档到 OSS">
        <div class="flow-grid" aria-hidden="true" />
        <div class="flow-title">数据路径 / DATA ROUTE</div>
        <div class="flow-line"><span /><span /><span /></div>
        <div class="flow-points">
          <div><i>01</i><strong>本地提交</strong><small>配置 · 凭证</small></div>
          <div><i>02</i><strong>E-HPC</strong><small>计算 · 状态</small></div>
          <div><i>03</i><strong>OSS 归档</strong><small>结果 · 日志</small></div>
        </div>
      </div>
    </section>

    <div class="status-grid">
      <article class="status-card">
        <div class="status-card-top"><span>正在执行</span><span class="metric-indicator live" /></div>
        <strong>{{ activeRuns.length.toString().padStart(2, '0') }}</strong>
        <small>本地提交与等待中的任务</small>
      </article>
      <article class="status-card">
        <div class="status-card-top"><span>OSS 记录</span><span class="metric-indicator success" /></div>
        <strong>{{ records.length.toString().padStart(2, '0') }}</strong>
        <small>{{ recordGroups.length }} 个分组 · 直接读取 OSS 归档</small>
      </article>
      <article class="status-card">
        <div class="status-card-top"><span>需要关注</span><span class="metric-indicator attention" /></div>
        <strong>{{ failedCount.toString().padStart(2, '0') }}</strong>
        <small>OSS 记录中未通过的作业</small>
      </article>
    </div>

    <div class="working-grid">
      <SequencePanel :batch="batch" @edit="emit('editSequence')" />
      <LaunchPanel :busy="busy" :preview="preview" @preview="(...args) => emit('preview', ...args)" @start="(...args) => emit('start', ...args)" />
    </div>

    <div class="context-strip">
      <div><span>当前镜像</span><strong :title="config?.image">{{ imageName }}</strong></div>
      <div><span>默认数据集</span><strong :title="config?.umap?.source_zarr_path">{{ sourceName }}</strong></div>
      <div><span>配置文件</span><strong :title="configPath">{{ configPath }}</strong></div>
    </div>

    <div class="runs-header">
      <span>最近写入 OSS 的结果</span>
      <button type="button" @click="emit('showRuns')">查看全部记录 <span aria-hidden="true">→</span></button>
    </div>
    <RecordBrowser :records="records" :groups="recordGroups" :info="recordsInfo" :selected-id="selectedRecordId" :detail="selectedRecord"
      :loading="recordLoading" compact @select="emit('selectRecord', $event)" @refresh="emit('refreshRecords')" />
  </div>
</template>

<style scoped>
.overview-page { display: grid; gap: 24px; }
.hero { min-height: 292px; display: grid; grid-template-columns: 1.25fr .8fr; gap: 20px; overflow: hidden; border-radius: 24px; padding: 31px 34px; background: #16313e; color: #f7fbfb; position: relative; box-shadow: 0 16px 35px #24475620; }
.hero:before { content: ''; position: absolute; width: 430px; height: 430px; background: radial-gradient(circle, #297d8380, transparent 62%); left: 34%; top: -270px; pointer-events: none; }
.hero-copy { z-index: 1; display: flex; flex-direction: column; align-items: start; }
.hero-kicker { display: inline-flex; align-items: center; gap: 9px; color: #9bd6d1; font: 700 10px var(--font-mono); letter-spacing: .13em; }.signal-dot { width: 7px; height: 7px; border-radius: 50%; background: #72d1be; box-shadow: 0 0 0 5px #7be2d824; }
.hero h1 { font-family: var(--font-display); font-size: clamp(32px, 4vw, 51px); letter-spacing: -.06em; font-weight: 700; line-height: 1.15; margin: 23px 0 9px; }.title-period { color: #ef9b75; }
.hero-copy > p { font-size: 13px; color: #aec6cb; margin: 0; }
.hero-current { margin-top: auto; display: grid; gap: 5px; min-width: 0; padding-top: 30px; }.hero-current-label { font-size: 10px; color: #88adb6; }.hero-current strong { font: 600 14px var(--font-mono); overflow-wrap: anywhere; }.hero-current small { color: #91b0b9; font: 10px/1.5 var(--font-mono); max-width: 490px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.flow-diagram { position: relative; align-self: stretch; display: flex; flex-direction: column; justify-content: flex-end; padding: 24px 22px 17px; border: 1px solid #ffffff24; border-radius: 18px; background: #ffffff0c; overflow: hidden; }
.flow-grid { position: absolute; inset: 0; opacity: .35; background-image: linear-gradient(#93cad213 1px, transparent 1px), linear-gradient(90deg, #93cad213 1px, transparent 1px); background-size: 31px 31px; mask-image: linear-gradient(black, transparent 90%); }
.flow-title { position: relative; margin-bottom: auto; color: #93bec4; font: 700 9px var(--font-mono); letter-spacing: .14em; }
.flow-line { position: relative; height: 1px; margin: 0 24px 17px; background: linear-gradient(90deg, #5bc5bb, #a9d6d4); display: flex; align-items: center; justify-content: space-between; }.flow-line span { width: 11px; height: 11px; border: 2px solid #8cddd1; border-radius: 50%; background: #1f5560; box-shadow: 0 0 0 6px #6ec9c220; }.flow-line span:nth-child(2) { background: #ef9976; border-color: #ef9976; box-shadow: 0 0 0 6px #ef997625; }
.flow-points { position: relative; display: flex; justify-content: space-between; gap: 10px; }.flow-points div { display: grid; gap: 4px; }.flow-points i { font: 700 9px var(--font-mono); font-style: normal; color: #80b1ba; }.flow-points strong { font-size: 10px; font-weight: 600; white-space: nowrap; }.flow-points small { color: #81a8b0; font-size: 9px; white-space: nowrap; }
.status-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; }.status-card { min-width: 0; border: 1px solid var(--border); background: var(--surface); box-shadow: var(--shadow-soft); border-radius: 16px; padding: 17px 19px; }.status-card-top { display: flex; justify-content: space-between; color: var(--muted); font-size: 11px; }.metric-indicator { width: 17px; height: 4px; border-radius: 20px; background: #9fb0b7; }.metric-indicator.live { background: #4fc0b3; }.metric-indicator.success { background: #78a981; }.metric-indicator.attention { background: #df8f6e; }.status-card strong { display: block; font: 600 28px/1 var(--font-display); letter-spacing: -.04em; margin: 12px 0 8px; }.status-card small { font-size: 10px; color: #8aa0a9; }
.working-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: start; }
.context-strip { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); padding: 16px 0; gap: 15px; }.context-strip div { display: grid; gap: 6px; min-width: 0; }.context-strip div:not(:first-child) { padding-left: 17px; border-left: 1px solid var(--border); }.context-strip span { color: var(--muted); font-size: 10px; }.context-strip strong { font: 600 11px var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.runs-header { display: flex; justify-content: space-between; align-items: center; margin: 0 0 -19px; color: var(--muted); font-size: 11px; }.runs-header button { background: none; border: 0; color: var(--accent); font: 700 11px var(--font-body); cursor: pointer; }.runs-header button:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
@media (max-width: 1100px) { .hero { grid-template-columns: 1fr .88fr; } }
@media (max-width: 820px) { .hero { grid-template-columns: 1fr; }.flow-diagram { min-height: 145px; }.working-grid { grid-template-columns: 1fr; } }
@media (max-width: 580px) { .hero { padding: 25px; }.status-grid { gap: 8px; }.status-card { padding: 13px 11px; }.status-card small { display: none; }.status-card strong { font-size: 23px; }.context-strip { grid-template-columns: 1fr; }.context-strip div:not(:first-child) { border-left: 0; padding-left: 0; } }
</style>
