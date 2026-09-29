<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import BatchConfigEditor from './BatchConfigEditor.vue'
import EnvEditor from './EnvEditor.vue'
import InstantConfigEditor from './InstantConfigEditor.vue'
import type { BatchConfig, EnvConfig, EnvUpdate, InstantConfig } from '../../types/config'
import './config-editor.css'

type Section = 'instant' | 'batch' | 'env'

const props = withDefaults(defineProps<{
  config: InstantConfig | null
  batch: BatchConfig | null
  env: EnvConfig | null
  busy?: boolean
  profiles?: string[]
  profileResources?: Record<string, InstantConfig['resources']>
  batchSaved?: boolean
  configPath?: string
  activeSection?: Section
}>(), {
  busy: false,
  profiles: () => [],
  profileResources: () => ({}),
  batchSaved: false,
  configPath: '',
  activeSection: 'instant',
})

const emit = defineEmits<{
  'save-config': [config: InstantConfig]
  'save-batch': [batch: BatchConfig]
  'save-env': [changes: EnvUpdate]
  'select-profile': [path: string]
  'save-as-profile': [path: string]
  'section-change': [section: Section]
}>()

const section = shallowRef<Section>(props.activeSection)
const showingCopy = shallowRef(false)
const newProfilePath = shallowRef('config/instant.copy.local.json')
const copyError = shallowRef('')
watch(() => props.activeSection, value => { section.value = value })
// Only files that exist on disk; the list is re-synced every few seconds, so deleted files drop out.
const availableProfiles = computed(() => props.profiles)

function selectProfile(event: Event) {
  const target = event.target as HTMLSelectElement
  if (target.value && target.value !== props.configPath) emit('select-profile', target.value)
}

function setSection(value: Section) {
  section.value = value
  emit('section-change', value)
}

function copyProfile() {
  copyError.value = ''
  const path = newProfilePath.value.trim()
  if (availableProfiles.value.includes(path)) {
    copyError.value = '这个配置文件已存在，请换一个文件名。'
    return
  }
  if (!/^config\/[A-Za-z0-9._-]+\.json$/.test(path)) {
    copyError.value = '文件需保存在 config/ 下，名称以 .json 结尾。'
    return
  }
  emit('save-as-profile', path)
  showingCopy.value = false
}
</script>

<template>
  <section class="config-panel" aria-label="配置中心">
    <div class="config-panel__masthead">
      <div>
        <p class="config-panel__eyebrow">CONTROL / CONFIGURATION</p>
        <h2 class="config-panel__title">配置中心</h2>
        <p class="config-panel__description">调整作业参数、编排批量序列和本机凭证。保存后用于下一次运行。</p>
      </div>
      <div class="config-panel__masthead-mark" aria-hidden="true">
        <span></span><span></span><span></span>
      </div>
    </div>

    <div class="config-panel__workspace">
      <nav class="config-panel__tabs" aria-label="配置类型">
        <button type="button" :class="['config-panel__tab', { 'is-active': section === 'instant' }]"
          :aria-current="section === 'instant' ? 'page' : undefined" @click="setSection('instant')">
          <span class="config-panel__tab-number">01</span> 单次作业
        </button>
        <button type="button" :class="['config-panel__tab', { 'is-active': section === 'batch' }]"
          :aria-current="section === 'batch' ? 'page' : undefined" @click="setSection('batch')">
          <span class="config-panel__tab-number">02</span> 批量序列
        </button>
        <button type="button" :class="['config-panel__tab', { 'is-active': section === 'env' }]"
          :aria-current="section === 'env' ? 'page' : undefined" @click="setSection('env')">
          <span class="config-panel__tab-number">03</span> 环境变量
        </button>
      </nav>

      <div class="config-panel__body">
        <template v-if="section === 'instant'">
          <div class="config-panel__profile">
            <div>
              <span class="config-panel__label">当前配置文件</span>
              <p class="config-panel__profile-path">{{ configPath || '默认配置' }}</p>
            </div>
            <div class="config-panel__profile-tools">
              <label class="config-panel__profile-select-wrap">
                <span class="sr-only">切换配置文件</span>
                <select class="config-panel__profile-select" :value="configPath" :disabled="busy || !availableProfiles.length"
                  @change="selectProfile">
                  <option v-if="!configPath" value="">选择配置文件</option>
                  <option v-for="path in availableProfiles" :key="path" :value="path">{{ path }}</option>
                </select>
              </label>
              <button class="config-panel__copy-button" type="button" :disabled="busy || !config" @click="showingCopy = !showingCopy">复制为新配置</button>
            </div>
          </div>
          <form v-if="showingCopy" class="config-panel__copy-form" @submit.prevent="copyProfile">
            <label><span>新配置路径</span><input v-model.trim="newProfilePath" spellcheck="false" placeholder="config/instant.copy.local.json" /></label>
            <button type="submit" :disabled="busy">创建副本</button>
            <button type="button" class="config-panel__cancel-button" @click="showingCopy = false">取消</button>
            <p>将当前已保存的单次作业配置复制到新文件。之后可以切换并编辑，供批量序列使用。</p>
            <p v-if="copyError" class="config-panel__copy-error" role="alert">{{ copyError }}</p>
          </form>
          <InstantConfigEditor :model="config" :busy="busy" @save="emit('save-config', $event)" />
        </template>
        <BatchConfigEditor v-else-if="section === 'batch'" :model="batch" :profiles="availableProfiles" :profile-resources="profileResources" :saved="batchSaved" :busy="busy"
          @save="emit('save-batch', $event)" />
        <EnvEditor v-else :model="env" :busy="busy" @save="emit('save-env', $event)" />
      </div>
    </div>
  </section>
</template>

<style scoped>
.config-panel { color: var(--ink, #16313d); display: grid; gap: 18px; }
.config-panel__masthead { position: relative; overflow: hidden; display: flex; justify-content: space-between; gap: 24px; padding: 25px 29px 27px; background: #16313d; color: #fff; border-radius: 18px; }
.config-panel__masthead::after { content: ''; position: absolute; width: 280px; height: 280px; top: -183px; right: 80px; border: 1px solid rgba(116, 206, 204, .3); border-radius: 50%; box-shadow: 0 0 0 38px rgba(116, 206, 204, .045), 0 0 0 79px rgba(116, 206, 204, .04); pointer-events: none; }
.config-panel__eyebrow { margin: 0 0 7px; color: #9de2dc; font: 600 11px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .16em; }
.config-panel__title { margin: 0; font-size: clamp(24px, 2.5vw, 32px); letter-spacing: -.035em; line-height: 1.22; }
.config-panel__description { margin: 9px 0 0; color: #bdd0d5; font-size: 13px; line-height: 1.6; }
.config-panel__masthead-mark { z-index: 1; align-self: end; display: flex; align-items: end; gap: 5px; height: 29px; }
.config-panel__masthead-mark span { display: block; width: 4px; background: #57c0ba; border-radius: 5px; }
.config-panel__masthead-mark span:nth-child(1) { height: 10px; opacity: .55; }
.config-panel__masthead-mark span:nth-child(2) { height: 24px; }
.config-panel__masthead-mark span:nth-child(3) { height: 17px; opacity: .75; }
.config-panel__workspace { min-width: 0; background: var(--surface, #fff); border: 1px solid var(--border, #d9e3e6); border-radius: 18px; overflow: hidden; box-shadow: 0 12px 32px rgba(20, 53, 62, .04); }
.config-panel__tabs { display: flex; overflow-x: auto; scrollbar-width: none; gap: 5px; padding: 8px 22px 0; border-bottom: 1px solid var(--border, #d9e3e6); }.config-panel__tabs::-webkit-scrollbar { display: none; }
.config-panel__tab { position: relative; flex: 0 0 auto; border: 0; border-radius: 10px 10px 0 0; background: transparent; color: #607883; padding: 14px 16px 16px; cursor: pointer; font: inherit; font-size: 13px; font-weight: 650; transition: color .2s, background .2s; }
.config-panel__tab:hover { background: #f3f7f7; color: var(--ink, #16313d); }
.config-panel__tab.is-active { color: var(--ink, #16313d); background: #edf6f5; }
.config-panel__tab.is-active::after { position: absolute; content: ''; height: 3px; left: 13px; right: 13px; bottom: 0; background: var(--accent, #2b8c92); border-radius: 3px 3px 0 0; }
.config-panel__tab-number { margin-right: 8px; opacity: .55; font: 600 11px ui-monospace, SFMono-Regular, Menlo, monospace; }
.config-panel__body { min-width: 0; padding: 26px 28px 30px; }
.config-panel__profile { display: flex; justify-content: space-between; align-items: center; gap: 18px; margin-bottom: 27px; padding: 16px 18px; background: #f1f7f7; border: 1px solid #dcebec; border-radius: 12px; }
.config-panel__label { color: #60808a; font-size: 11px; font-weight: 700; letter-spacing: .05em; }
.config-panel__profile-path { margin: 5px 0 0; color: var(--ink, #16313d); font: 600 13px ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }
.config-panel__profile-select-wrap { display: block; min-width: 230px; }
.config-panel__profile-select { width: 100%; min-width: 0; padding: 10px 35px 10px 12px; border: 1px solid var(--border, #d9e3e6); border-radius: 8px; background: #fff; color: var(--ink, #16313d); font: inherit; font-size: 13px; }
.config-panel__profile-tools { display: flex; align-items: center; gap: 8px; min-width: 0; }.config-panel__copy-button { border: 1px solid #c5dddc; color: #26787b; background: #fff; border-radius: 8px; padding: 10px 11px; white-space: nowrap; cursor: pointer; font-size: 11px; font-weight: 700; }.config-panel__copy-button:hover { background: #eef8f6; }.config-panel__copy-button:disabled { opacity: .5; cursor: not-allowed; }
.config-panel__copy-form { display: flex; flex-wrap: wrap; align-items: end; gap: 9px; margin: -13px 0 25px; padding: 16px; border: 1px solid #d6e7e7; background: #f8fbfb; border-radius: 10px; }.config-panel__copy-form label { display: grid; gap: 5px; flex: 1 1 250px; font-size: 10px; font-weight: 700; }.config-panel__copy-form input { min-height: 36px; border: 1px solid var(--border); border-radius: 7px; padding: 0 10px; font: 11px var(--font-mono); }.config-panel__copy-form button { min-height: 36px; border: 0; border-radius: 7px; padding: 0 12px; background: var(--ink); color: white; font-size: 11px; font-weight: 700; cursor: pointer; }.config-panel__copy-form .config-panel__cancel-button { background: #e9f0f0; color: var(--ink); }.config-panel__copy-form p { margin: 0; flex: 1 0 100%; color: var(--muted); font-size: 10px; line-height: 1.5; }.config-panel__copy-form .config-panel__copy-error { color: #b3523f; }
.config-panel__profile-select:focus-visible, .config-panel__tab:focus-visible, .config-panel__copy-button:focus-visible, .config-panel__copy-form input:focus-visible, .config-panel__copy-form button:focus-visible { outline: 3px solid #8bd3d1; outline-offset: 2px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 640px) { .config-panel__body { padding: 20px 16px 24px; } .config-panel__tabs { padding-left: 7px; } .config-panel__masthead { padding: 22px 20px; } .config-panel__profile { align-items: stretch; flex-direction: column; } .config-panel__profile-tools { align-items: stretch; flex-wrap: wrap; }.config-panel__profile-select-wrap { min-width: 0; flex: 1 1 100%; } }
@media (prefers-reduced-motion: reduce) { .config-panel__tab { transition: none; } }
</style>
