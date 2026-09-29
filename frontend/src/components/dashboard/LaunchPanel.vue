<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import type { PreviewPayload } from '../../types/dashboard'

const props = defineProps<{ busy: boolean; preview: PreviewPayload | null }>()
const emit = defineEmits<{
  preview: [mode: 'instant' | 'batch', dataset?: string]
  start: [mode: 'instant' | 'batch', dataset?: string, waitTimeout?: number]
}>()

const mode = shallowRef<'instant' | 'batch'>('instant')
const dataset = shallowRef('')
const waitTimeout = shallowRef(1800)
const previewText = computed(() => props.preview && props.preview.mode === mode.value
  ? JSON.stringify(props.preview.preview, null, 2)
  : '')
const previewData = computed(() => props.preview?.mode === mode.value ? props.preview.preview : null)
const previewRunId = computed(() => previewData.value && !Array.isArray(previewData.value)
  ? String(previewData.value.run_id ?? previewData.value.batch_id ?? '') : '')
const previewDataset = computed(() => previewData.value && !Array.isArray(previewData.value)
  ? String(previewData.value.dataset ?? '') : '')
const previewTotal = computed(() => previewData.value && !Array.isArray(previewData.value)
  ? Number(previewData.value.total ?? 0) : 0)
const previewCells = computed(() => {
  if (!previewData.value || Array.isArray(previewData.value)) return []
  const cells = previewData.value.cells
  return Array.isArray(cells) ? cells.slice(0, 12) as Array<Record<string, unknown>> : []
})

function selectedDataset(): string | undefined {
  return mode.value === 'instant' && dataset.value.trim() ? dataset.value.trim() : undefined
}

function start() {
  if (!Number.isInteger(waitTimeout.value) || waitTimeout.value < 1) return
  emit('start', mode.value, selectedDataset(), waitTimeout.value)
}
</script>

<template>
  <section class="launch-panel">
    <div class="panel-head">
      <div>
        <span class="panel-kicker">CONTROL</span>
        <h2>开始运行</h2>
      </div>
      <span class="panel-dash" aria-hidden="true">↗</span>
    </div>

    <div class="mode-tabs" role="group" aria-label="运行方式">
      <button type="button" :class="{ active: mode === 'instant' }" @click="mode = 'instant'">单次作业</button>
      <button type="button" :class="{ active: mode === 'batch' }" @click="mode = 'batch'">批量序列</button>
    </div>

    <div class="launch-fields">
      <label v-if="mode === 'instant'" class="field">
        <span>临时替换数据集 <small>可选</small></span>
        <input v-model="dataset" type="text" spellcheck="false" placeholder="沿用已保存配置中的数据集" />
      </label>
      <p v-else class="batch-context">按已保存的序列，依次执行各配置、实例型号与重复次数。</p>
      <label class="field timeout-field">
        <span>本地等待上限 <small>秒</small></span>
        <input v-model.number="waitTimeout" type="number" min="1" step="1" />
      </label>
    </div>

    <div class="launch-actions">
      <button class="button button-quiet" type="button" :disabled="busy" @click="emit('preview', mode, selectedDataset())">先预览</button>
      <button class="button button-primary" type="button" :disabled="busy || !Number.isInteger(waitTimeout) || waitTimeout < 1" @click="start">
        {{ busy ? '处理中…' : '开始运行' }} <span aria-hidden="true">→</span>
      </button>
    </div>
    <p class="save-hint">运行使用已保存的配置。预览只校验请求，不提交作业。</p>

    <div v-if="previewText" class="preview-result">
      <div class="preview-title"><span class="preview-check">✓</span> 校验通过 <small>{{ mode === 'batch' ? '批量执行计划' : '单次提交请求' }}</small></div>
      <div class="preview-summary">
        <div><span>{{ mode === 'batch' ? '本次批次' : '预览 Run ID' }}</span><strong>{{ previewRunId }}</strong></div>
        <div v-if="previewDataset"><span>数据集</span><strong>{{ previewDataset }}</strong></div>
        <div v-if="mode === 'batch'"><span>计划作业数</span><strong>{{ previewTotal }}</strong></div>
      </div>
      <div v-if="mode === 'batch' && previewCells.length" class="preview-cells">
        <div v-for="(cell, index) in previewCells" :key="String(cell.run_id ?? index)"><span>{{ index + 1 }}</span><strong>{{ cell.instance_type || '自动选择' }}</strong><small>{{ cell.config_file }} · 第 {{ cell.repeat_index }} 次</small></div>
        <p v-if="previewTotal > previewCells.length">其余 {{ previewTotal - previewCells.length }} 项见完整计划。</p>
      </div>
      <details class="preview-raw"><summary>查看完整{{ mode === 'batch' ? '计划' : '请求' }}</summary><pre>{{ previewText }}</pre></details>
    </div>
  </section>
</template>

<style scoped>
.launch-panel { min-width: 0; background: var(--surface); border: 1px solid var(--border); box-shadow: var(--shadow-soft); border-radius: 20px; padding: 24px; }
.panel-head { display: flex; align-items: start; justify-content: space-between; margin-bottom: 19px; }
.panel-kicker { color: var(--accent); font: 700 10px/1.4 var(--font-mono); letter-spacing: .16em; }
.panel-head h2 { font-size: 21px; margin: 5px 0 0; letter-spacing: -.035em; }
.panel-dash { display: grid; place-items: center; width: 31px; height: 31px; border: 1px solid #d4e8e8; border-radius: 9px; color: var(--accent); font-size: 18px; }
.mode-tabs { background: #edf3f4; border-radius: 10px; padding: 4px; display: grid; grid-template-columns: 1fr 1fr; gap: 3px; margin-bottom: 22px; }
.mode-tabs button { border: 0; background: transparent; border-radius: 7px; padding: 9px; font: 600 12px var(--font-body); color: var(--muted); cursor: pointer; }
.mode-tabs button.active { color: var(--ink); background: #fff; box-shadow: 0 2px 8px #16435810; }
.mode-tabs button:focus-visible, .button:focus-visible, .field input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.launch-fields { display: grid; gap: 13px; }
.field { display: grid; gap: 8px; color: var(--ink); font-size: 11px; font-weight: 700; }
.field span { display: flex; gap: 6px; align-items: baseline; }.field small { font-size: 10px; color: var(--muted); font-weight: 400; }
.field input { width: 100%; min-height: 38px; padding: 0 11px; border: 1px solid var(--border); border-radius: 8px; color: var(--ink); background: #fbfdfd; font: 11px var(--font-body); }
.field input::placeholder { color: #98a9af; }
.batch-context { padding: 10px 12px; border: 1px solid #d3e9e7; background: #f2faf8; border-radius: 8px; font-size: 11px; line-height: 1.6; color: #397477; margin: 0; }
.timeout-field { max-width: 174px; }
.launch-actions { display: flex; gap: 9px; margin-top: 20px; }
.button { min-height: 39px; padding: 0 15px; border-radius: 9px; border: 1px solid transparent; cursor: pointer; font: 700 11px var(--font-body); transition: transform .15s, background .15s; }
.button:hover:not(:disabled) { transform: translateY(-1px); }.button:disabled { opacity: .55; cursor: not-allowed; }
.button-quiet { border-color: var(--border); background: white; color: var(--ink); }.button-quiet:hover { background: #f2f7f7; }
.button-primary { flex: 1; display: flex; align-items: center; justify-content: space-between; gap: 10px; background: var(--ink); color: white; }
.button-primary:hover { background: #255263; }.button-primary span { font-size: 17px; font-weight: 400; }
.save-hint { margin: 11px 0 0; font-size: 10px; line-height: 1.5; color: var(--muted); }
.preview-result { margin-top: 19px; border-top: 1px solid var(--border); padding-top: 15px; }
.preview-title { display: flex; align-items: center; gap: 7px; font-size: 11px; font-weight: 700; }.preview-title small { font-size: 10px; color: var(--muted); font-weight: 400; margin-left: auto; }
.preview-check { display: inline-grid; place-items: center; width: 17px; height: 17px; border-radius: 50%; background: #dff5e9; color: #338556; font-size: 10px; }
.preview-summary { display: grid; gap: 8px; margin: 12px 0; }.preview-summary div { display: grid; gap: 3px; }.preview-summary span { color: var(--muted); font-size: 9px; }.preview-summary strong { overflow-wrap: anywhere; font: 600 10px/1.4 var(--font-mono); }.preview-cells { display: grid; gap: 4px; max-height: 170px; overflow-y: auto; margin-bottom: 12px; }.preview-cells div { display: flex; align-items: center; gap: 7px; padding: 5px 7px; border-radius: 5px; background: #f2f8f7; font-size: 10px; }.preview-cells span { color: var(--accent); font: 700 9px var(--font-mono); }.preview-cells strong { font: 600 10px var(--font-mono); }.preview-cells small { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.preview-cells p { margin: 4px 0; color: var(--muted); font-size: 10px; }.preview-raw summary { color: var(--accent); font-size: 10px; font-weight: 700; cursor: pointer; }
.preview-result pre { max-height: 245px; margin: 12px 0 0; padding: 13px; border-radius: 8px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; background: #152c37; color: #cce6e7; font: 10px/1.5 var(--font-mono); }
@media (max-width: 500px) { .launch-panel { padding: 19px; } }
</style>
