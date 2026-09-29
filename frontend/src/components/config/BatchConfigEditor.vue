<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue'
import type { BatchConfig, BatchRun, InstantResources } from '../../types/config'

const props = defineProps<{
  model: BatchConfig | null
  profiles: string[]
  profileResources: Record<string, InstantResources>
  saved: boolean
  busy: boolean
}>()
const emit = defineEmits<{ save: [batch: BatchConfig] }>()

const draft = ref<BatchConfig | null>(null)
const typesText = ref<string[]>([])
const rowIds = ref<number[]>([])
const nextId = shallowRef(0)
const rawMode = shallowRef(false)
const rawText = shallowRef('')
const error = shallowRef('')

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function loadDraft(value: BatchConfig | null) {
  draft.value = value ? clone(value) : null
  typesText.value = value?.runs.map(run => run.instance_types?.join('\n') ?? '') ?? []
  rowIds.value = value?.runs.map(() => nextId.value++) ?? []
  rawText.value = value ? JSON.stringify(value, null, 2) : ''
  rawMode.value = false
  error.value = ''
}

const plannedJobs = computed(() => draft.value?.runs.reduce((total, run, index) => {
  const typeCount = typesText.value[index]?.split(/[\n,]/).filter(value => value.trim()).length || 1
  return total + typeCount * (Number(run.repeat) || 1)
}, 0) ?? 0)

function isDirty(base: BatchConfig | null | undefined): boolean {
  if (!draft.value || !base) return false
  if (rawMode.value) return rawText.value !== JSON.stringify(base, null, 2)
  return JSON.stringify(draft.value) !== JSON.stringify(base)
    || typesText.value.some((value, index) => value !== (base.runs[index]?.instance_types?.join('\n') ?? ''))
}

const changed = computed(() => isDirty(props.model))

// The model is re-read from disk every few seconds; only an untouched draft follows it.
watch(() => props.model, (value, previous) => { if (!isDirty(previous)) loadDraft(value) }, { immediate: true })

function addRun() {
  if (!draft.value) return
  draft.value.runs.push({ config: props.profiles[0] || 'config/instant.local.json', repeat: 1 })
  typesText.value.push('')
  rowIds.value.push(nextId.value++)
}

function duplicateRun(index: number) {
  if (!draft.value) return
  draft.value.runs.splice(index + 1, 0, clone(draft.value.runs[index]))
  typesText.value.splice(index + 1, 0, typesText.value[index])
  rowIds.value.splice(index + 1, 0, nextId.value++)
}

function removeRun(index: number) {
  if (!draft.value || draft.value.runs.length < 2) return
  draft.value.runs.splice(index, 1)
  typesText.value.splice(index, 1)
  rowIds.value.splice(index, 1)
}

function moveRun(index: number, direction: -1 | 1) {
  if (!draft.value) return
  const target = index + direction
  if (target < 0 || target >= draft.value.runs.length) return
  ;[draft.value.runs[index], draft.value.runs[target]] = [draft.value.runs[target], draft.value.runs[index]]
  ;[typesText.value[index], typesText.value[target]] = [typesText.value[target], typesText.value[index]]
  ;[rowIds.value[index], rowIds.value[target]] = [rowIds.value[target], rowIds.value[index]]
}

function updateRepeat(index: number, event: Event) {
  if (!draft.value) return
  draft.value.runs[index].repeat = Number((event.target as HTMLInputElement).value)
}

type ResourceKey = 'cores' | 'memory_gib' | 'system_disk_gib'

function updateResource(index: number, key: ResourceKey, event: Event) {
  if (!draft.value) return
  const run = draft.value.runs[index]
  const value = (event.target as HTMLInputElement).value
  if (!value) {
    if (run.resources) {
      delete run.resources[key]
      if (!Object.keys(run.resources).length) delete run.resources
    }
    return
  }
  run.resources ??= {}
  run.resources[key] = Number(value)
}

function inherited(run: BatchRun): InstantResources | undefined {
  return props.profileResources[run.config]
}

function effective(run: BatchRun, key: ResourceKey): number | undefined {
  return run.resources?.[key] ?? inherited(run)?.[key]
}

// Enterprise c/g/r families: large = 2 vCPU, xlarge = 4, Nxlarge = 4N, with 2 / 4 / 8 GiB per vCPU.
// 13xlarge sizes break the pattern, and other families are not checked.
function knownSpec(type: string): [number, number] | null {
  const match = /^ecs\.([cgr])\d+[a-z]*\.(large|(\d*)xlarge)$/.exec(type)
  if (!match || match[3] === '13') return null
  const cores = match[2] === 'large' ? 2 : 4 * Number(match[3] || 1)
  return [cores, cores * { c: 2, g: 4, r: 8 }[match[1] as 'c' | 'g' | 'r']]
}

function resourceWarning(run: BatchRun, index: number): string {
  const types = (typesText.value[index] ?? '').split(/[\n,]/).map(value => value.trim()).filter(Boolean)
  const mismatched = types.flatMap(type => {
    const expected = knownSpec(type)
    return expected && (effective(run, 'cores') !== expected[0] || effective(run, 'memory_gib') !== expected[1])
      ? [`${type} 为 ${expected[0]} vCPU / ${expected[1]} GiB`] : []
  })
  if (!mismatched.length) return ''
  return `${mismatched.join('；')}，当前记为 ${effective(run, 'cores') ?? '—'} vCPU / ${effective(run, 'memory_gib') ?? '—'} GiB`
}

function syncOptionalFields(): boolean {
  if (!draft.value) return false
  if (!draft.value.runs.length) {
    error.value = '批量序列至少需要一个条目。'
    return false
  }
  if (!/^[A-Za-z0-9_-]{1,48}$/.test(draft.value.batch_name ?? 'batch')) {
    error.value = '批次名称只能使用字母、数字、下划线和连字符，最多 48 个字符。'
    return false
  }
  for (const [index, run] of draft.value.runs.entries()) {
    if (!run.config.trim()) {
      error.value = `第 ${index + 1} 条需要配置文件路径。`
      return false
    }
    if (!inherited(run)) {
      error.value = `第 ${index + 1} 条引用的配置文件不存在或不可用：${run.config}`
      return false
    }
    if (!Number.isInteger(run.repeat ?? 1) || (run.repeat ?? 1) < 1 || (run.repeat ?? 1) > 100) {
      error.value = `第 ${index + 1} 条的重复次数必须在 1–100 之间。`
      return false
    }
    const types = (typesText.value[index] ?? '').split(/[\n,]/).map(value => value.trim()).filter(Boolean)
    if (types.length) run.instance_types = types
    else delete run.instance_types
    for (const [key, value] of Object.entries(run.resources ?? {})) {
      if (!Number.isInteger(value) || (value as number) < 1) {
        error.value = `第 ${index + 1} 条的 ${key} 必须是正整数。`
        return false
      }
    }
    if (!run.dataset?.trim()) delete run.dataset
    else run.dataset = run.dataset.trim()
  }
  return true
}

function readRaw(): BatchConfig | null {
  try {
    const parsed: unknown = JSON.parse(rawText.value)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed) || !('runs' in parsed)
      || !Array.isArray(parsed.runs)) throw new Error('缺少 runs')
    if (parsed.runs.some(run => !run || typeof run !== 'object' || Array.isArray(run)
      || !('config' in run) || typeof run.config !== 'string'
      || ('instance_types' in run && !Array.isArray(run.instance_types)))) {
      throw new Error('runs 条目结构不正确')
    }
    return parsed as BatchConfig
  } catch {
    error.value = '完整 JSON 无法解析，或 runs 条目结构不正确。'
    return null
  }
}

function toggleRawMode() {
  error.value = ''
  if (rawMode.value) {
    const parsed = readRaw()
    if (!parsed) return
    draft.value = parsed
    typesText.value = parsed.runs.map(run => run.instance_types?.join('\n') ?? '')
    rowIds.value = parsed.runs.map(() => nextId.value++)
    rawMode.value = false
    return
  }
  if (!syncOptionalFields()) return
  rawText.value = JSON.stringify(draft.value, null, 2)
  rawMode.value = true
}

function save() {
  error.value = ''
  if (!draft.value) return
  if (rawMode.value) {
    const parsed = readRaw()
    if (parsed) emit('save', parsed)
    return
  }
  if (syncOptionalFields()) emit('save', clone(draft.value))
}
</script>

<template>
  <div class="cfg-editor">
    <div class="cfg-heading-row">
      <div>
        <p class="cfg-kicker">BATCH SEQUENCE</p>
        <h3 class="cfg-heading">批量运行序列</h3>
        <p class="cfg-subtitle">列表从上到下依次执行。每条默认继承所选配置文件的 CPU、内存和系统盘，也可以单独覆盖。</p>
      </div>
      <button type="button" class="cfg-ghost-button" :disabled="!draft || busy" @click="toggleRawMode">
        {{ rawMode ? '返回序列' : '编辑完整 JSON' }}
      </button>
    </div>

    <div v-if="!draft" class="cfg-empty">未读取到批量配置。请先确认批量配置文件存在。</div>
    <form v-else class="cfg-form" @submit.prevent="save">
      <div v-if="!saved" class="cfg-note"><strong>新序列</strong><span>当前显示按现有配置生成的起始条目，保存后写入 config/batch.local.json。</span></div>
      <template v-if="!rawMode">
        <div class="cfg-grid cfg-batch-name">
          <label class="cfg-field"><span>批次名称</span><input v-model.trim="draft.batch_name" required maxlength="48" placeholder="umap-benchmark" spellcheck="false" /></label>
          <div class="cfg-batch-summary"><strong>{{ plannedJobs }}</strong><span>次计划运行</span><small>未指定规格的条目可能按其配置文件扩展为更多作业</small></div>
        </div>

        <datalist id="config-profile-paths"><option v-for="path in profiles" :key="path" :value="path"></option></datalist>
        <div class="cfg-run-list">
          <article v-for="(run, index) in draft.runs" :key="rowIds[index]" class="cfg-run">
            <div class="cfg-run__top">
              <div class="cfg-run__index"><span>RUN</span><strong>{{ String(index + 1).padStart(2, '0') }}</strong></div>
              <div class="cfg-run__title">序列条目 {{ index + 1 }}</div>
              <div class="cfg-run__tools">
                <button type="button" title="上移" :aria-label="`将第 ${index + 1} 条上移`" :disabled="index === 0 || busy" @click="moveRun(index, -1)">↑</button>
                <button type="button" title="下移" :aria-label="`将第 ${index + 1} 条下移`" :disabled="index === draft.runs.length - 1 || busy" @click="moveRun(index, 1)">↓</button>
                <button type="button" title="复制" :aria-label="`复制第 ${index + 1} 条`" :disabled="busy" @click="duplicateRun(index)">复制</button>
                <button type="button" title="删除" class="cfg-run__delete" :aria-label="`删除第 ${index + 1} 条`" :disabled="draft.runs.length === 1 || busy" @click="removeRun(index)">删除</button>
              </div>
            </div>
            <div class="cfg-grid cfg-run__fields">
              <label class="cfg-field cfg-field--full"><span>配置文件路径</span><input v-model.trim="run.config" list="config-profile-paths" required spellcheck="false" placeholder="config/instant.local.json" /></label>
              <p v-if="!inherited(run)" class="cfg-missing-profile cfg-field--full">配置文件不存在或不可用，请选择现有文件；此前删除的文件不会自动恢复。</p>
              <label class="cfg-field"><span>实例规格 <b>可选，多行填写</b></span><textarea v-model="typesText[index]" rows="2" placeholder="ecs.c9a.xlarge&#10;ecs.g7.large" spellcheck="false"></textarea></label>
              <div class="cfg-run__minor">
                <label class="cfg-field"><span>每种规格重复次数</span><input :value="run.repeat ?? 1" type="number" min="1" max="100" step="1" required @input="updateRepeat(index, $event)" /></label>
                <label class="cfg-field"><span>覆盖数据集 <b>可选</b></span><input v-model.trim="run.dataset" placeholder="path/to/dataset.zarr" spellcheck="false" /></label>
              </div>
              <div class="cfg-field--full cfg-resource-panel">
                <div class="cfg-resource-panel__heading"><strong>作业资源</strong><span>提交值：{{ effective(run, 'cores') ?? '—' }} vCPU / {{ effective(run, 'memory_gib') ?? '—' }} GiB 内存 / {{ effective(run, 'system_disk_gib') ?? '—' }} GiB 系统盘</span></div>
                <div class="cfg-resource-panel__grid">
                  <label class="cfg-field"><span>CPU 核数 <b>留空继承</b></span><input type="number" min="1" step="1" :value="run.resources?.cores ?? ''" :placeholder="String(inherited(run)?.cores ?? '—')" @input="updateResource(index, 'cores', $event)" /></label>
                  <label class="cfg-field"><span>内存 GiB <b>留空继承</b></span><input type="number" min="1" step="1" :value="run.resources?.memory_gib ?? ''" :placeholder="String(inherited(run)?.memory_gib ?? '—')" @input="updateResource(index, 'memory_gib', $event)" /></label>
                  <label class="cfg-field"><span>系统盘 GiB <b>留空继承</b></span><input type="number" min="1" step="1" :value="run.resources?.system_disk_gib ?? ''" :placeholder="String(inherited(run)?.system_disk_gib ?? '—')" @input="updateResource(index, 'system_disk_gib', $event)" /></label>
                </div>
                <p>填了实例规格时，机器的 CPU 和内存由规格决定，这里的值写进结果标签，售罄回退为自动选型时也按它选机器；没填规格时按这里的 CPU 和内存自动选型。系统盘总是按这里的值申请。INSTANT 接口没有 GPU 字段，要用 GPU 只能填写 GPU 实例规格。</p>
                <p v-if="resourceWarning(run, index)" class="cfg-resource-warning">{{ resourceWarning(run, index) }}。结果标签会与实际机器不符，建议覆盖为一致；不同规格需要不同值时拆成不同条目。</p>
              </div>
            </div>
          </article>
        </div>
        <button type="button" class="cfg-add-button" :disabled="busy" @click="addRun"><span>＋</span> 添加序列条目</button>
      </template>

      <div v-else class="cfg-raw">
        <div class="cfg-raw__heading"><strong>完整 JSON</strong><span>支持修改任意现有字段；切回序列时会解析并应用。</span></div>
        <textarea v-model="rawText" aria-label="完整批量配置 JSON" spellcheck="false"></textarea>
      </div>

      <p v-if="error" class="cfg-error" role="alert">{{ error }}</p>
      <div class="cfg-actions">
        <span class="cfg-actions__state">{{ changed ? '有未保存的更改' : saved ? '当前内容与已保存配置一致' : '起始条目尚未保存' }}</span>
        <button type="button" class="cfg-ghost-button" :disabled="busy || !changed" @click="loadDraft(model)">撤销更改</button>
        <button type="submit" class="cfg-primary-button" :disabled="busy">{{ busy ? '保存中…' : '保存批量序列' }}</button>
      </div>
    </form>
  </div>
</template>
