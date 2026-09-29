<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue'
import type { EnvConfig, EnvEntry, EnvSource, EnvUpdate } from '../../types/config'

interface EditableEntry extends EnvEntry {
  id: number
  draftValue: string
  changed: boolean
  removed: boolean
  isNew: boolean
}

const props = defineProps<{ model: EnvConfig | null; busy: boolean }>()
const emit = defineEmits<{ save: [changes: EnvUpdate] }>()

const rows = ref<EditableEntry[]>([])
const newKey = shallowRef('')
const newValue = shallowRef('')
const nextId = shallowRef(0)
const error = shallowRef('')
const hasChanges = computed(() => rows.value.some(row => row.changed || row.removed || row.isNew))

function makeRow(entry: EnvEntry, isNew = false): EditableEntry {
  return {
    ...entry,
    id: nextId.value++,
    draftValue: entry.value,
    changed: false,
    removed: false,
    isNew,
  }
}

function loadRows(value: EnvConfig | null) {
  rows.value = value?.entries.map(entry => makeRow(entry)) ?? []
  newKey.value = ''
  newValue.value = ''
  error.value = ''
}

watch(() => props.model, loadRows, { immediate: true })

function sourceLabel(source: EnvSource): string {
  if (source === 'file') return '.env 文件'
  if (source === 'process') return '进程注入'
  return '未设置'
}

function updateValue(row: EditableEntry, event: Event) {
  row.draftValue = (event.target as HTMLInputElement).value
  row.changed = row.isNew || row.draftValue !== row.value
}

function addEntry() {
  error.value = ''
  const key = newKey.value.trim()
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
    error.value = '变量名只能使用字母、数字和下划线，且不能以数字开头。'
    return
  }
  if (rows.value.some(row => row.key === key)) {
    error.value = `${key} 已在列表中。`
    return
  }
  if (!newValue.value) {
    error.value = '新增变量请填写值。'
    return
  }
  const row = makeRow({ key, value: '', present: false, source: 'unset' }, true)
  row.draftValue = newValue.value
  row.changed = true
  rows.value.push(row)
  newKey.value = ''
  newValue.value = ''
}

function toggleRemove(row: EditableEntry) {
  error.value = ''
  if (row.isNew) {
    rows.value = rows.value.filter(item => item.id !== row.id)
    return
  }
  row.removed = !row.removed
}

function save() {
  error.value = ''
  const changes: EnvUpdate['changes'] = []
  for (const row of rows.value) {
    if (row.removed) {
      changes.push({ key: row.key, value: null })
    } else if (row.isNew || row.changed) {
      changes.push({ key: row.key, value: row.draftValue })
    }
  }
  if (changes.length) emit('save', { changes })
}
</script>

<template>
  <div class="cfg-editor">
    <div class="cfg-heading-row">
      <div>
        <p class="cfg-kicker">LOCAL ENVIRONMENT</p>
        <h3 class="cfg-heading">环境变量</h3>
        <p class="cfg-subtitle">显示当前 .env 或进程变量的实际值。编辑后保存到本机 .env，新作业会使用保存后的值。</p>
      </div>
      <span class="cfg-env-count">{{ rows.length }} 个变量</span>
    </div>

    <div class="cfg-note"><strong>生效顺序</strong><span>从本界面启动的新作业会优先使用 .env 中保存的值；直接在终端运行脚本时，终端已导出的变量优先。删除“进程注入”的条目不会清除当前进程的变量。</span></div>

    <div v-if="!model" class="cfg-empty">未读取到环境变量。请检查本机 .env 文件。</div>
    <template v-else>
      <form class="cfg-env-form" @submit.prevent="save">
        <div v-if="!rows.length" class="cfg-empty">当前没有可编辑的变量。可以在下方添加。</div>
        <div v-else class="cfg-env-list">
          <div v-for="row in rows" :key="row.id" :class="['cfg-env-row', { 'is-removed': row.removed }]">
            <div class="cfg-env-row__identity">
              <div class="cfg-env-row__key">{{ row.key }}</div>
              <div class="cfg-env-row__meta">
                <span :class="['cfg-source', `cfg-source--${row.source}`]">{{ sourceLabel(row.source) }}</span>
                <span v-if="row.isNew" class="cfg-new-tag">新增</span>
              </div>
            </div>
            <div class="cfg-env-row__edit">
              <label class="sr-only" :for="`env-${row.id}`">{{ row.key }} 的值</label>
              <input :id="`env-${row.id}`" type="text"
                :value="row.draftValue" :disabled="row.removed || busy" :placeholder="row.present ? '输入新值' : '未设置 · 输入新值'"
                autocomplete="off" spellcheck="false" @input="updateValue(row, $event)" />
            </div>
            <button type="button" class="cfg-env-row__remove" :disabled="busy" @click="toggleRemove(row)">{{ row.removed ? '撤销删除' : '删除' }}</button>
          </div>
        </div>

        <div class="cfg-env-add">
          <div class="cfg-env-add__heading"><strong>添加变量</strong><span>可添加脚本需要的其他 KEY=VALUE 参数</span></div>
          <div class="cfg-env-add__fields">
            <label class="cfg-field"><span>变量名</span><input v-model.trim="newKey" placeholder="MY_VARIABLE" spellcheck="false" autocomplete="off" /></label>
            <label class="cfg-field"><span>变量值</span><input v-model="newValue" type="text" placeholder="输入变量值" spellcheck="false" autocomplete="off" /></label>
            <button type="button" class="cfg-add-button" :disabled="busy" @click="addEntry">＋ 添加</button>
          </div>
        </div>

        <p v-if="error" class="cfg-error" role="alert">{{ error }}</p>
        <div class="cfg-actions">
          <span class="cfg-actions__state">{{ hasChanges ? '有未保存的更改' : '尚未修改变量' }}</span>
          <button type="button" class="cfg-ghost-button" :disabled="busy || !hasChanges" @click="loadRows(model)">撤销更改</button>
          <button type="submit" class="cfg-primary-button" :disabled="busy || !hasChanges">{{ busy ? '保存中…' : '保存环境变量' }}</button>
        </div>
      </form>
    </template>
  </div>
</template>
