<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import ConfigPanel from './components/config/ConfigPanel.vue'
import OverviewPage from './components/dashboard/OverviewPage.vue'
import RecordBrowser from './components/dashboard/RecordBrowser.vue'
import RunMonitor from './components/dashboard/RunMonitor.vue'
import { useDashboard } from './composables/useDashboard'
import type { InstantConfig, BatchConfig, EnvUpdate } from './types/dashboard'

const dashboard = useDashboard()
const page = shallowRef<'overview' | 'runs' | 'config'>('overview')
const configSection = shallowRef<'instant' | 'batch' | 'env'>('instant')

const pageTitle = computed(() => ({
  overview: '总览', runs: '运行记录', config: '配置中心',
})[page.value])
const syncLabel = computed(() => dashboard.lastSynced.value
  ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(dashboard.lastSynced.value)
  : '尚未同步')

function openConfig(section: 'instant' | 'batch' | 'env') {
  configSection.value = section
  page.value = 'config'
}

async function start(mode: 'instant' | 'batch', dataset?: string, timeout?: number) {
  const id = await dashboard.startRun(mode, dataset, timeout)
  if (id) page.value = 'runs'
}

function saveConfig(value: InstantConfig) { void dashboard.saveConfig(value) }
function saveBatch(value: BatchConfig) { void dashboard.saveBatch(value) }
function saveEnv(value: EnvUpdate) { void dashboard.saveEnvironment(value) }
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true"><span /><span /><span /><span /></div>
        <div><strong>MassFlow</strong><small>INSTANT CONTROL</small></div>
      </div>
      <div class="sidebar-section-label">WORKSPACE</div>
      <nav class="sidebar-nav" aria-label="主导航">
        <button type="button" aria-label="总览" :class="{ active: page === 'overview' }" @click="page = 'overview'">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M3.5 10.5 12 4l8.5 6.5V20h-6v-6h-5v6h-6z" stroke-linecap="round" stroke-linejoin="round" /></svg>
          <span>总览</span>
        </button>
        <button type="button" aria-label="运行记录" :class="{ active: page === 'runs' }" @click="page = 'runs'">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M4 5h16M4 12h16M4 19h16" stroke-linecap="round" /><circle cx="8" cy="5" r="1.5" fill="currentColor" stroke="none" /><circle cx="15" cy="12" r="1.5" fill="currentColor" stroke="none" /><circle cx="10" cy="19" r="1.5" fill="currentColor" stroke="none" /></svg>
          <span>运行记录</span><em v-if="dashboard.activeRuns.value.length">{{ dashboard.activeRuns.value.length }}</em>
        </button>
        <button type="button" aria-label="配置中心" :class="{ active: page === 'config' }" @click="openConfig('instant')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M5 6h14M5 12h14M5 18h14" stroke-linecap="round" /><circle cx="9" cy="6" r="2" fill="var(--sidebar-bg)" /><circle cx="15" cy="12" r="2" fill="var(--sidebar-bg)" /><circle cx="11" cy="18" r="2" fill="var(--sidebar-bg)" /></svg>
          <span>配置中心</span>
        </button>
      </nav>
      <div class="sidebar-section-label section-secondary">QUICK ACCESS</div>
      <nav class="sidebar-nav sidebar-nav-secondary" aria-label="快捷配置">
        <button type="button" @click="openConfig('batch')"><span class="nav-tick">↳</span><span>运行序列</span></button>
        <button type="button" @click="openConfig('env')"><span class="nav-tick">↳</span><span>环境变量</span></button>
      </nav>
      <div class="sidebar-bottom">
        <span class="service-light" />
        <div><strong>本地服务</strong><small>127.0.0.1:8765</small></div>
      </div>
    </aside>

    <main class="main-area">
      <header class="topbar">
        <div class="topbar-left"><span>MASSFLOW</span><span class="topbar-separator">/</span><strong>{{ pageTitle }}</strong></div>
        <div class="topbar-right"><span class="sync-caption">上次同步 {{ syncLabel }}</span><button type="button" class="refresh-button" :disabled="dashboard.syncing.value" aria-label="刷新状态" @click="dashboard.refresh(true)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M20 11a8 8 0 1 1-2.2-5.5M20 4v5.5h-5.5" stroke-linecap="round" stroke-linejoin="round" /></svg></button></div>
      </header>

      <div class="content-wrap">
        <div v-if="dashboard.error.value" class="notice-banner error-banner" role="alert"><span class="notice-symbol">!</span><span>{{ dashboard.error.value }}</span></div>
        <div v-if="dashboard.notice.value" class="notice-banner success-banner" role="status"><span class="notice-symbol">✓</span><span>{{ dashboard.notice.value }}</span><button type="button" aria-label="关闭提示" @click="dashboard.notice.value = ''">×</button></div>

        <div v-if="dashboard.loading.value" class="loading-state"><span class="loading-ring" /><strong>正在读取本地配置与运行记录</strong><small>首次加载通常只需几秒</small></div>

        <OverviewPage
          v-else-if="page === 'overview'"
          :config="dashboard.config.value"
          :batch="dashboard.batch.value"
          :config-path="dashboard.configPath.value"
          :active-runs="dashboard.activeRuns.value"
          :records="dashboard.records.value"
          :record-groups="dashboard.recordGroups.value"
          :records-info="dashboard.recordsInfo.value"
          :selected-record-id="dashboard.selectedRecordId.value"
          :selected-record="dashboard.selectedRecord.value"
          :record-loading="dashboard.recordLoading.value"
          :preview="dashboard.preview.value"
          :busy="dashboard.busy.value"
          @preview="dashboard.runPreview"
          @start="start"
          @select-record="dashboard.selectRecord"
          @refresh-records="dashboard.refresh(true)"
          @edit-sequence="openConfig('batch')"
          @show-runs="page = 'runs'"
        />

        <section v-else-if="page === 'runs'" class="runs-page">
          <div class="page-intro"><div><span class="page-kicker">MONITORING</span><h1>运行记录</h1><p>记录直接读取 OSS 归档，可下载单条、分组或全部结果；从本页提交、仍在运行的作业单独显示在上方。</p></div><button type="button" class="outline-action" :disabled="dashboard.syncing.value" @click="dashboard.refresh(true)">刷新记录 ↻</button></div>
          <RunMonitor v-if="dashboard.runs.value.length" :runs="dashboard.runs.value" :selected-id="dashboard.selectedRunId.value" :detail="dashboard.selectedRun.value" @select="dashboard.selectRun" />
          <RecordBrowser :records="dashboard.records.value" :groups="dashboard.recordGroups.value" :info="dashboard.recordsInfo.value"
            :selected-id="dashboard.selectedRecordId.value" :detail="dashboard.selectedRecord.value" :loading="dashboard.recordLoading.value"
            @select="dashboard.selectRecord" @refresh="dashboard.refresh(true)" />
        </section>

        <section v-else class="config-page">
          <ConfigPanel
            :config="dashboard.config.value"
            :batch="dashboard.batch.value"
            :env="dashboard.environment.value"
            :profiles="dashboard.profiles.value"
            :profile-resources="dashboard.profileResources.value"
            :batch-saved="dashboard.batchSaved.value"
            :config-path="dashboard.configPath.value"
            :active-section="configSection"
            :busy="dashboard.busy.value"
            @select-profile="dashboard.selectProfile"
            @save-as-profile="dashboard.saveAsProfile"
            @section-change="configSection = $event"
            @save-config="saveConfig"
            @save-batch="saveBatch"
            @save-env="saveEnv"
          />
        </section>
      </div>
      <footer class="app-footer"><span>MassFlow / E-HPC INSTANT</span><span>本地运行台 · 状态每 5 秒同步 · OSS 记录每分钟更新</span></footer>
    </main>
  </div>
</template>
