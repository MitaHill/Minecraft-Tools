<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { canOperate, useAdmin } from '../admin'

type Settings = {
  auto_ban_enabled: boolean
  auto_ban_attempts: number
  auto_ban_window_minutes: number
  auto_ban_hours: number
  auto_ban_skip_legit: boolean
}

const request = useAdmin()
const canEdit = computed(() => canOperate.value)
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const message = ref('')
const form = reactive<Settings>({
  auto_ban_enabled: true,
  auto_ban_attempts: 3,
  auto_ban_window_minutes: 10,
  auto_ban_hours: 24,
  auto_ban_skip_legit: true,
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    Object.assign(form, await request<Settings>('/settings'))
  } catch (issue) {
    error.value = (issue as Error).message
  } finally {
    loading.value = false
  }
}

async function save() {
  if (busy.value || !canEdit.value) return
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    Object.assign(form, await request<Settings>('/settings', 'PATCH', { ...form }))
    message.value = '设定已保存，只影响之后的判定。'
  } catch (issue) {
    error.value = (issue as Error).message
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <header class="page-heading">
    <h1>设定</h1>
    <button
      v-if="canEdit"
      class="console-button page-action"
      type="button"
      :disabled="busy || loading"
      @click="save"
    >
      保存
    </button>
  </header>
  <p v-if="error" class="action-error" role="alert">{{ error }}</p>
  <p v-if="message" role="status">{{ message }}</p>
  <p v-if="!canEdit" class="cell-note">当前账号只能查看设定。</p>
  <section v-if="loading" class="empty-panel">正在读取设定…</section>
  <section v-else class="settings-panel" aria-labelledby="security-settings">
    <div>
      <h2 id="security-settings">自动拉黑未授权隧道</h2>
      <p class="cell-note">绕过客户端直接连节点、且没有有效预约的隧道会被封禁。</p>
      <p class="cell-note">以下条件决定何时把来源 IP 加入黑名单。</p>
    </div>
    <label class="checkbox-field">
      <input v-model="form.auto_ban_enabled" type="checkbox" :disabled="busy || !canEdit">
      启用自动拉黑
    </label>
    <div class="settings-grid">
      <label class="settings-field">
        <span class="field-heading">
          <span>触发条数</span>
          <small class="field-hint">1–20 条</small>
        </span>
        <small class="field-note">同一 IP 在统计窗口内累计达到这个条数后触发自动拉黑。</small>
        <input v-model.number="form.auto_ban_attempts" type="number" min="1" max="20" step="1" :disabled="busy || !canEdit">
      </label>
      <label class="settings-field">
        <span class="field-heading">
          <span>统计窗口</span>
          <small class="field-hint">1–1440 分钟</small>
        </span>
        <small class="field-note">从当前时间往前统计的时长，超出窗口的无预约记录不再计入。</small>
        <input v-model.number="form.auto_ban_window_minutes" type="number" min="1" max="1440" step="1" :disabled="busy || !canEdit">
      </label>
      <label class="settings-field">
        <span class="field-heading">
          <span>封禁时长</span>
          <small class="field-hint">1–720 小时（最长 30 天）</small>
        </span>
        <small class="field-note">自动加入黑名单后保持的时长，到期后自动解除。</small>
        <input v-model.number="form.auto_ban_hours" type="number" min="1" max="720" step="1" :disabled="busy || !canEdit">
      </label>
    </div>
    <label class="checkbox-field">
      <input v-model="form.auto_ban_skip_legit" type="checkbox" :disabled="busy || !canEdit">
      该 IP 在窗口内有合法隧道时不自动拉黑
    </label>
    <p class="cell-note">只对公网 IP 生效；修改不会缩短已经生效的封禁。</p>
  </section>
</template>

<style scoped>
.settings-panel {
  display: grid;
  gap: 18px;
  max-width: 720px;
  padding: 20px;
  border: 1px solid var(--line);
  background: var(--panel-bg);
}

.settings-panel h2 { margin: 0; font-size: 16px; }

.settings-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 18px;
}

.settings-field { display: grid; gap: 6px; }

.field-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.field-hint,
.field-note { color: var(--muted); }

.field-note { line-height: 1.45; }

.settings-panel input[type='number'] {
  width: 100%;
  padding: 8px;
  font: inherit;
}

.checkbox-field {
  display: flex;
  align-items: center;
  gap: 8px;
}

.checkbox-field input { width: auto; }

</style>
