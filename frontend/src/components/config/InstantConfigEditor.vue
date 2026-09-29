<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue'
import type { InstantConfig } from '../../types/config'

const props = defineProps<{ model: InstantConfig | null; busy: boolean }>()
const emit = defineEmits<{ save: [config: InstantConfig] }>()

const draft = ref<InstantConfig | null>(null)
const instanceTypesText = shallowRef('')
const labelsText = shallowRef('')
const rawText = shallowRef('')
const rawMode = shallowRef(false)
const error = shallowRef('')
const downloadWorkers = computed({
  get: () => draft.value?.umap.download_workers ?? 32,
  set: (value: number) => {
    if (draft.value) draft.value.umap.download_workers = value
  },
})
const changed = computed(() => {
  if (!draft.value || !props.model) return false
  if (rawMode.value) return rawText.value !== JSON.stringify(props.model, null, 2)
  return JSON.stringify(draft.value) !== JSON.stringify(props.model)
    || instanceTypesText.value !== (props.model.resources.instance_types ?? []).join('\n')
    || labelsText.value !== (props.model.umap.benchmark_labels ? JSON.stringify(props.model.umap.benchmark_labels, null, 2) : '')
})

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function loadDraft(value: InstantConfig | null) {
  draft.value = value ? clone(value) : null
  instanceTypesText.value = value?.resources.instance_types?.join('\n') ?? ''
  labelsText.value = value?.umap.benchmark_labels ? JSON.stringify(value.umap.benchmark_labels, null, 2) : ''
  rawText.value = value ? JSON.stringify(value, null, 2) : ''
  rawMode.value = false
  error.value = ''
}

watch(() => props.model, loadDraft, { immediate: true })

function syncOptionalFields(): boolean {
  if (!draft.value) return false
  const types = instanceTypesText.value.split(/[\n,]/).map(value => value.trim()).filter(Boolean)
  if (types.length > 5) {
    error.value = '实例规格最多填写 5 个。'
    return false
  }
  if (types.length) draft.value.resources.instance_types = types
  else delete draft.value.resources.instance_types

  if (labelsText.value.trim()) {
    try {
      const parsed: unknown = JSON.parse(labelsText.value)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('映射格式不正确')
      draft.value.umap.benchmark_labels = parsed as Record<string, string | number>
    } catch {
      error.value = '基准标签需要是合法的 JSON 对象。'
      return false
    }
  } else delete draft.value.umap.benchmark_labels
  return true
}

function readRaw(): InstantConfig | null {
  try {
    const parsed: unknown = JSON.parse(rawText.value)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('根节点不是对象')
    if (!('resources' in parsed) || !parsed.resources || typeof parsed.resources !== 'object' || Array.isArray(parsed.resources)
      || !('umap' in parsed) || !parsed.umap || typeof parsed.umap !== 'object' || Array.isArray(parsed.umap)) {
      throw new Error('缺少 resources 或 umap')
    }
    if ('instance_types' in parsed.resources && !Array.isArray(parsed.resources.instance_types)) {
      throw new Error('instance_types 不是数组')
    }
    return parsed as InstantConfig
  } catch {
    error.value = '完整 JSON 无法解析，或 resources / umap 结构不正确。'
    return null
  }
}

function toggleRawMode() {
  error.value = ''
  if (rawMode.value) {
    const parsed = readRaw()
    if (!parsed) return
    draft.value = parsed
    instanceTypesText.value = parsed.resources?.instance_types?.join('\n') ?? ''
    labelsText.value = parsed.umap?.benchmark_labels ? JSON.stringify(parsed.umap.benchmark_labels, null, 2) : ''
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
  if (!syncOptionalFields()) return
  emit('save', clone(draft.value))
}
</script>

<template>
  <div class="cfg-editor">
    <div class="cfg-heading-row">
      <div>
        <p class="cfg-kicker">INSTANT JOB</p>
        <h3 class="cfg-heading">单次作业配置</h3>
        <p class="cfg-subtitle">这里的参数用于提交下一次 INSTANT 作业。运行中的作业不受影响。</p>
      </div>
      <button type="button" class="cfg-ghost-button" :disabled="!draft || busy" @click="toggleRawMode">
        {{ rawMode ? '返回表单' : '编辑完整 JSON' }}
      </button>
    </div>

    <div v-if="!draft" class="cfg-empty">未读取到单次作业配置。请先确认配置文件存在。</div>
    <form v-else class="cfg-form" @submit.prevent="save">
      <template v-if="!rawMode">
        <fieldset class="cfg-section">
          <legend class="cfg-section__title"><span class="cfg-section__dot"></span>云端连接</legend>
          <p class="cfg-section__intro">地域、镜像与网络需位于同一地域。</p>
          <div class="cfg-grid">
            <label class="cfg-field cfg-field--full"><span>容器镜像 <em>必填</em></span>
              <input v-model.trim="draft.image" required placeholder="registry-vpc.example.com/namespace/image:sha-..." spellcheck="false" />
              <small>建议固定 SHA 标签，避免复用 latest 缓存。</small>
            </label>
            <label class="cfg-field"><span>地域</span><input v-model.trim="draft.region" required placeholder="cn-hongkong" spellcheck="false" /></label>
            <label class="cfg-field"><span>OSS 角色 ARN</span><input v-model.trim="draft.oss_role_arn" required placeholder="acs:ram::...:role/..." spellcheck="false" /></label>
            <label class="cfg-field"><span>交换机 ID</span><input v-model.trim="draft.vswitch_id" required placeholder="vsw-..." spellcheck="false" /></label>
            <label class="cfg-field"><span>安全组 ID</span><input v-model.trim="draft.security_group_id" required placeholder="sg-..." spellcheck="false" /></label>
          </div>
          <div class="cfg-options">
            <label class="cfg-check"><input v-model="draft.private_registry" type="checkbox" /><span>私有镜像仓库</span><small>使用环境变量中的 ACR 拉取凭证</small></label>
            <label class="cfg-check"><input v-model="draft.enable_external_ip" type="checkbox" /><span>分配公网 IP</span><small>默认关闭，走 VPC 内网</small></label>
          </div>
        </fieldset>

        <fieldset class="cfg-section">
          <legend class="cfg-section__title"><span class="cfg-section__dot"></span>计算资源</legend>
          <p class="cfg-section__intro">资源规格决定每次作业的机器配置和下载空间。</p>
          <div class="cfg-grid cfg-grid--three">
            <label class="cfg-field"><span>CPU 核数</span><input v-model.number="draft.resources.cores" type="number" min="0.1" step="any" required /></label>
            <label class="cfg-field"><span>内存 <b>GiB</b></span><input v-model.number="draft.resources.memory_gib" type="number" min="0.1" step="any" required /></label>
            <label class="cfg-field"><span>系统盘 <b>GiB</b></span><input v-model.number="draft.resources.system_disk_gib" type="number" min="1" step="1" required /></label>
          </div>
          <div class="cfg-grid">
            <label class="cfg-field cfg-field--full"><span>实例规格优先级 <b>可选，最多 5 个</b></span>
              <textarea v-model="instanceTypesText" rows="2" placeholder="ecs.c9a.xlarge&#10;ecs.g7.large" spellcheck="false"></textarea>
              <small>每行或用逗号填写一个型号；空白表示由 INSTANT 按上面的 CPU 和内存自动选型。填写规格后机器的 CPU 和内存以规格为准。</small>
            </label>
          </div>
          <div class="cfg-options">
            <label class="cfg-check"><input v-model="draft.resources.fallback_any_type" type="checkbox" /><span>售罄时自动回退</span><small>CreateJob 报售罄时去掉规格，按 CPU 和内存再提交一次；阿里云本身不会自动换规格</small></label>
          </div>
        </fieldset>

        <fieldset class="cfg-section">
          <legend class="cfg-section__title"><span class="cfg-section__dot"></span>数据与归档</legend>
          <p class="cfg-section__intro">数据集从 OSS 读取，结果写入归档前缀。</p>
          <div class="cfg-grid">
            <label class="cfg-field"><span>OSS Bucket</span><input v-model.trim="draft.umap.oss_bucket" required spellcheck="false" /></label>
            <label class="cfg-field"><span>OSS 地域</span><input v-model.trim="draft.umap.oss_region" required spellcheck="false" /></label>
            <label class="cfg-field cfg-field--full"><span>OSS Endpoint</span><input v-model.trim="draft.umap.oss_endpoint" required placeholder="https://oss-cn-hongkong-internal.aliyuncs.com" spellcheck="false" /></label>
            <label class="cfg-field"><span>结果归档前缀</span><input v-model.trim="draft.umap.oss_prefix" required spellcheck="false" /></label>
            <label class="cfg-field"><span>源 Zarr 数据集路径</span><input v-model.trim="draft.umap.source_zarr_path" required placeholder="path/to/dataset.zarr" spellcheck="false" /></label>
          </div>
          <div class="cfg-options">
            <label class="cfg-check cfg-check--caution"><input v-model="draft.umap.write_back" type="checkbox" /><span>写回源 Zarr</span><small>开启后会修改源数据集的 analysis/umap</small></label>
          </div>
        </fieldset>

        <fieldset class="cfg-section">
          <legend class="cfg-section__title"><span class="cfg-section__dot"></span>UMAP 执行</legend>
          <p class="cfg-section__intro">调整本地工作目录与内存预算，控制下载和拟合方式。</p>
          <div class="cfg-grid">
            <label class="cfg-field cfg-field--full"><span>容器工作目录</span><input v-model.trim="draft.umap.work_dir" required placeholder="/tmp/umap-work" spellcheck="false" /></label>
            <label class="cfg-field"><span>同时下载数</span><input v-model.number="downloadWorkers" type="number" min="1" max="128" step="1" required /><small>默认 32，连接池会同步扩大。</small></label>
          </div>
          <div class="cfg-grid cfg-grid--three">
            <label class="cfg-field"><span>全量矩阵上限 <b>MiB</b></span><input v-model.number="draft.umap.full_matrix_mib" type="number" min="1" step="1" required /></label>
            <label class="cfg-field"><span>抽样矩阵上限 <b>MiB</b></span><input v-model.number="draft.umap.sample_matrix_mib" type="number" min="1" step="1" required /></label>
            <label class="cfg-field"><span>最多拟合样本数</span><input v-model.number="draft.umap.max_fit_samples" type="number" min="1" step="1" required /></label>
          </div>
          <div class="cfg-options">
            <label class="cfg-check"><input v-model="draft.umap.skip_ion_image_chunks" type="checkbox" /><span>跳过离子图像数据块</span><small>减少重复数据下载</small></label>
          </div>
          <div class="cfg-grid">
            <label class="cfg-field cfg-field--full"><span>基准标签 <b>可选 JSON 对象</b></span>
              <textarea v-model="labelsText" rows="3" placeholder='{"experiment":"trial-a","index":1}' spellcheck="false"></textarea>
              <small>记录到结果中，方便对照不同实验；最多 20 项。</small>
            </label>
          </div>
        </fieldset>
      </template>

      <div v-else class="cfg-raw">
        <div class="cfg-raw__heading"><strong>完整 JSON</strong><span>支持修改任意现有字段；切回表单时会解析并应用。</span></div>
        <textarea v-model="rawText" aria-label="完整单次作业 JSON" spellcheck="false"></textarea>
      </div>

      <p v-if="error" class="cfg-error" role="alert">{{ error }}</p>
      <div class="cfg-actions">
        <span class="cfg-actions__state">{{ changed ? '有未保存的更改' : '当前内容与已保存配置一致' }}</span>
        <button type="button" class="cfg-ghost-button" :disabled="busy || !changed" @click="loadDraft(model)">撤销更改</button>
        <button type="submit" class="cfg-primary-button" :disabled="busy">{{ busy ? '保存中…' : '保存单次配置' }}</button>
      </div>
    </form>
  </div>
</template>
