<script setup lang="ts">
import { computed } from 'vue'
import type { BatchConfig } from '../../types/dashboard'

const props = defineProps<{ batch: BatchConfig | null }>()
const emit = defineEmits<{ edit: [] }>()

const runs = computed(() => props.batch?.runs ?? [])
const total = computed(() => runs.value.reduce((count, run) => count +
  Math.max(1, run.instance_types?.length ?? 1) * (run.repeat ?? 1), 0))

function basename(path: string): string { return path.split('/').at(-1) || path }
function typeLabel(types?: string[]): string { return types?.length ? types.join(' · ') : '自动选择实例' }
</script>

<template>
  <section class="sequence-panel">
    <div class="sequence-heading">
      <div>
        <span class="sequence-kicker">EXECUTION ORDER</span>
        <h2>运行序列</h2>
      </div>
      <button class="edit-link" type="button" @click="emit('edit')">编辑序列 <span aria-hidden="true">↗</span></button>
    </div>
    <p class="sequence-desc">每组配置按列表顺序执行，组内依次尝试实例型号和重复次数。</p>
    <div v-if="runs.length" class="sequence-list">
      <div v-for="(run, index) in runs" :key="index" class="sequence-row">
        <span class="sequence-index">{{ String(index + 1).padStart(2, '0') }}</span>
        <span class="sequence-rail" aria-hidden="true" />
        <div class="sequence-content">
          <strong>{{ basename(run.config) }}</strong>
          <span>{{ typeLabel(run.instance_types) }}</span>
        </div>
        <span class="sequence-repeat">×{{ run.repeat ?? 1 }}</span>
      </div>
    </div>
    <div v-else class="sequence-empty">还没有配置运行序列。添加配置组后即可批量运行。</div>
    <div class="sequence-footer">
      <span>至少 <strong>{{ total }}</strong> 项作业</span>
      <span class="sequence-name">{{ batch?.batch_name || '未命名序列' }}</span>
    </div>
  </section>
</template>

<style scoped>
.sequence-panel { padding: 24px; min-width: 0; border: 1px solid var(--border); background: var(--surface); box-shadow: var(--shadow-soft); border-radius: 20px; }
.sequence-heading { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
.sequence-kicker { color: var(--accent); font: 700 10px/1.4 var(--font-mono); letter-spacing: .15em; }.sequence-heading h2 { font-size: 21px; letter-spacing: -.035em; margin: 5px 0 0; }
.edit-link { border: 0; background: transparent; color: var(--accent); font: 700 11px var(--font-body); padding: 4px 0; cursor: pointer; white-space: nowrap; }.edit-link:hover { color: #125f65; }.edit-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }
.sequence-desc { color: var(--muted); font-size: 11px; line-height: 1.6; margin: 11px 0 20px; }
.sequence-list { display: grid; max-height: 228px; overflow-y: auto; }
.sequence-row { position: relative; display: flex; align-items: center; gap: 15px; min-height: 65px; padding: 9px 0; }
.sequence-index { z-index: 1; display: grid; place-items: center; width: 30px; height: 30px; flex: 0 0 auto; border: 1px solid #c8e5e3; border-radius: 9px; background: #ecf9f7; color: #23888a; font: 700 10px var(--font-mono); }
.sequence-rail { position: absolute; top: 38px; bottom: -28px; left: 15px; width: 1px; background: #c8e5e3; }.sequence-row:last-child .sequence-rail { display: none; }
.sequence-content { min-width: 0; flex: 1; display: grid; gap: 5px; }.sequence-content strong { font: 600 11px var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.sequence-content span { color: var(--muted); font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sequence-repeat { color: #5f7c85; background: #f1f5f5; border-radius: 6px; padding: 5px 7px; font: 700 10px var(--font-mono); }
.sequence-empty { border: 1px dashed var(--border); padding: 20px; border-radius: 10px; color: var(--muted); font-size: 11px; line-height: 1.6; }
.sequence-footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; border-top: 1px solid var(--border); padding-top: 16px; margin-top: 14px; color: var(--muted); font-size: 10px; }.sequence-footer strong { color: var(--ink); font: 700 15px var(--font-mono); margin-left: 3px; }.sequence-name { max-width: 50%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 500px) { .sequence-panel { padding: 19px; } }
</style>
