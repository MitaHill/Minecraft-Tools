<script setup lang="ts">
import { computed, nextTick, ref, watchEffect, type Component } from 'vue'
import { useRouter } from 'vue-router'
import { account } from '../session'

const props = defineProps<{
  title: string
  icon: Component
  kind: 'nodes' | 'tunnels'
}>()

type Resource = Record<string, number | string | null> & { id: string }
type Scan = { id: number; checked_at: number; result: string; latency_ms: number | null; version: string | null; motd: string | null }
type Action = 'block' | 'unblock' | 'rescan'
const nodeColumns = [
  ['name', '节点'], ['public_host', '公网地址'], ['status', '状态'], ['agent_version', 'Agent 版本'],
]
const tunnelColumns = [
  ['proxy_name', '代理'], ['node_id', '节点'], ['remote_port', '远程端口'], ['status', '状态'], ['block_reason', '封禁原因'],
]
const statusNames: Record<string, string> = {
  pending: '待检查', active: '运行中', block_pending: '封禁中', blocked: '已封禁',
  closed: '已关闭', offline: '离线', online: '在线',
}
const tunnelStatuses = Object.entries(statusNames).filter(([value]) => value !== 'online')
const router = useRouter()
const rows = ref<Resource[]>([])
const status = ref('')
const refresh = ref(0)
const loading = ref(true)
const failed = ref(false)
const scannerEnabled = ref(false)
const canOperate = computed(() => account.value?.role === 'superadmin' || account.value?.role === 'operator')
const acting = ref(false)
const message = ref('')
const alert = ref('')
const dialog = ref<HTMLDialogElement>()
const decision = ref<{ action: 'block' | 'unblock'; row: Resource }>()
const reason = ref('')
const scanTunnel = ref<Resource>()
const scans = ref<Scan[]>([])
const loadingScans = ref(false)
const scansFailed = ref(false)
const actionLabels: Record<Action, string> = { block: '封禁', unblock: '解封', rescan: '重扫' }
const scanLabels: Record<string, string> = { ok: 'Minecraft Java', not_minecraft: '非 Minecraft', unreachable: '不可达', unknown: '无法判定' }
const blockLabels: Record<string, string> = { no_minecraft_status: '未检测到 Minecraft Java 响应' }

function cellValue(key: string, value: Resource[string]) {
  if (value === null || value === undefined) return '—'
  if (key === 'status') return statusNames[String(value)] ?? value
  if (key === 'block_reason') return blockLabels[String(value)] ?? value
  return value
}

watchEffect(async onCleanup => {
  const kind = props.kind
  const filter = kind === 'tunnels' && status.value ? `?status=${encodeURIComponent(status.value)}` : ''
  void refresh.value
  const controller = new AbortController()
  onCleanup(() => controller.abort())
  loading.value = true
  failed.value = false
  try {
    const response = await fetch(`/api/admin/${kind}${filter}`, { signal: controller.signal })
    if (response.status === 401) {
      account.value = null
      await router.replace({ name: 'login', query: { next: kind } })
      return
    }
    if (!response.ok) throw new Error()
    const body = await response.json() as { nodes?: Resource[]; tunnels?: Resource[]; scanner_enabled?: boolean }
    if (!controller.signal.aborted) {
      rows.value = body[kind] ?? []
      scannerEnabled.value = body.scanner_enabled === true
    }
  } catch {
    if (!controller.signal.aborted) failed.value = true
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
})

watchEffect(async onCleanup => {
  const row = scanTunnel.value
  if (props.kind !== 'tunnels' || !row) return
  void refresh.value
  const controller = new AbortController()
  onCleanup(() => controller.abort())
  loadingScans.value = true
  scansFailed.value = false
  try {
    const response = await fetch(`/api/admin/scans?tunnel_id=${encodeURIComponent(row.id)}`, { signal: controller.signal })
    if (!response.ok) throw new Error()
    const body = await response.json() as { scans: Scan[] }
    if (!controller.signal.aborted) scans.value = body.scans
  } catch {
    if (!controller.signal.aborted) scansFailed.value = true
  } finally {
    if (!controller.signal.aborted) loadingScans.value = false
  }
})

async function confirmAction(action: 'block' | 'unblock', row: Resource) {
  decision.value = { action, row }
  reason.value = ''
  await nextTick()
  dialog.value?.showModal()
}

async function submit(action: Action, row: Resource) {
  acting.value = true
  alert.value = ''
  message.value = ''
  try {
    const response = await fetch(`/api/admin/tunnels/${encodeURIComponent(row.id)}/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: action === 'block' ? JSON.stringify({ reason: reason.value.trim() || '管理员封禁' }) : undefined,
    })
    if (response.status === 401) {
      dialog.value?.close()
      account.value = null
      await router.replace({ name: 'login', query: { next: 'tunnels' } })
      return
    }
    if (!response.ok) throw new Error()
    dialog.value?.close()
    message.value = `已提交${actionLabels[action]}。`
    refresh.value++
  } catch {
    dialog.value?.close()
    alert.value = `无法提交${actionLabels[action]}。请刷新隧道状态后重试。`
  } finally {
    acting.value = false
  }
}
</script>

<template>
  <header class="page-heading">
    <h1>{{ title }}</h1>
  </header>
  <div class="resource-actions">
    <label v-if="kind === 'tunnels'" for="status-filter">状态</label>
    <select v-if="kind === 'tunnels'" id="status-filter" v-model="status">
      <option value="">全部</option>
      <option v-for="[value, label] in tunnelStatuses" :key="value" :value="value">
        {{ label }}
      </option>
    </select>
    <button class="console-button" type="button" :disabled="loading" @click="refresh++">刷新</button>
  </div>
  <p v-if="kind === 'tunnels' && message" role="status">{{ message }}</p>
  <p v-if="kind === 'tunnels' && alert" class="action-error" role="alert">{{ alert }}</p>
  <section v-if="loading || failed || rows.length === 0" class="empty-panel" role="status">
    <span class="resource-icon"><component :is="icon" aria-hidden="true" /></span>
    <h2>{{ loading ? '正在加载…' : failed ? `无法读取${title}` : `暂无${title}` }}</h2>
  </section>
  <div v-else class="resource-table">
    <table :aria-label="title">
      <thead>
        <tr>
          <th v-for="[key, label] in kind === 'nodes' ? nodeColumns : tunnelColumns" :key="key" scope="col">
            {{ label }}
          </th>
          <th v-if="kind === 'tunnels'" scope="col">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row.id">
          <td v-for="[key] in kind === 'nodes' ? nodeColumns : tunnelColumns" :key="key">
            {{ cellValue(key, row[key]) }}
          </td>
          <td v-if="kind === 'tunnels'">
            <div class="resource-actions tunnel-row-actions">
              <button class="console-button" type="button" @click="scanTunnel = row">检测记录</button>
              <button v-if="canOperate && ['pending', 'active'].includes(String(row.status))" class="console-button" type="button" :disabled="acting" @click="confirmAction('block', row)">封禁</button>
              <button v-if="canOperate && row.status === 'blocked'" class="console-button" type="button" :disabled="acting" @click="confirmAction('unblock', row)">解封</button>
              <button v-if="canOperate && scannerEnabled && ['pending', 'active', 'offline'].includes(String(row.status))" class="console-button" type="button" :disabled="acting" @click="submit('rescan', row)">重扫</button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
  <section v-if="kind === 'tunnels' && scanTunnel" class="resource-table scan-history" aria-labelledby="scan-title">
    <header class="resource-actions">
      <h2 id="scan-title">{{ scanTunnel.proxy_name }} 的检测记录</h2>
      <button class="console-button" type="button" @click="scanTunnel = undefined">关闭</button>
    </header>
    <p v-if="loadingScans || scansFailed || !scans.length" role="status">
      {{ loadingScans ? '正在读取检测记录…' : scansFailed ? '无法读取检测记录' : '暂无检测记录' }}
    </p>
    <table v-else aria-labelledby="scan-title">
      <thead><tr><th scope="col">时间</th><th scope="col">结果</th><th scope="col">延迟</th><th scope="col">版本</th><th scope="col">MOTD</th></tr></thead>
      <tbody>
        <tr v-for="scan in scans" :key="scan.id">
          <td>{{ new Date(scan.checked_at * 1000).toLocaleString() }}</td>
          <td>{{ scanLabels[scan.result] ?? scan.result }}</td>
          <td>{{ scan.latency_ms === null ? '—' : `${scan.latency_ms} ms` }}</td>
          <td>{{ scan.version ?? '—' }}</td>
          <td>{{ scan.motd || '—' }}</td>
        </tr>
      </tbody>
    </table>
  </section>
  <dialog ref="dialog" class="tunnel-dialog" aria-labelledby="decision-title">
    <form v-if="decision" @submit.prevent="submit(decision.action, decision.row)">
      <h2 id="decision-title">{{ actionLabels[decision.action] }} {{ decision.row.proxy_name }}</h2>
      <p v-if="decision.action === 'block'">现有连接会被切断，关联的大厅房间会下线。</p>
      <label v-if="decision.action === 'block'" class="tunnel-reason">原因<input v-model="reason" maxlength="200" placeholder="管理员封禁"></label>
      <div class="resource-actions">
        <button class="console-button" type="button" :disabled="acting" @click="dialog?.close()">取消</button>
        <button class="console-button" type="submit" :disabled="acting">{{ actionLabels[decision.action] }}</button>
      </div>
    </form>
  </dialog>
</template>
