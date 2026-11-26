<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { AdminRequestError, canOperate, dateTime, useAdminList } from '../admin'

type Entry = { ip: string; reason: string; actor: string; created_at: number; expires_at: number | null }
type Block = {
  id: number; tunnel_id: string; client_ip: string | null; node_id: string
  proxy_name: string; reason: string; source: string; created_at: number; status: string
}
type PendingNode = { id: string; status: string; blacklist_error: string | null }
const { data, error, loading, refresh, request } = useAdminList<{
  entries: Entry[]; blocks: Block[]; pending_nodes: PendingNode[]
}>('/blacklist', 2500)
const dialog = ref<HTMLDialogElement>()
const addDialog = ref<HTMLDialogElement>()
const draft = ref({ target: '', reason: '', duration: '24h' })
const confirmTarget = ref('')
const confirmAll = ref(false)
const target = ref<{ path: string; method: 'POST' | 'DELETE'; name: string }>()
const busy = ref(false)
const actionError = ref('')
const message = ref('')

function duration(row: Entry) {
  if (row.expires_at === null) return '永久'
  const hours = Math.round((row.expires_at - row.created_at) / 3600)
  return hours > 24 ? `${Math.round(hours / 24)} 天` : `${hours} 小时`
}

function clearConfirmation() {
  confirmTarget.value = ''
  confirmAll.value = false
  actionError.value = ''
}

function showAddDialog() {
  draft.value = { target: '', reason: '', duration: '24h' }
  clearConfirmation()
  addDialog.value?.showModal()
}

async function addRule() {
  if (busy.value || (confirmTarget.value && !confirmAll.value)) return
  busy.value = true
  actionError.value = ''
  message.value = ''
  try {
    await request('/blacklist', 'POST', {
      ...draft.value,
      target: draft.value.target.trim(),
      reason: draft.value.reason.trim(),
      confirm_all: confirmAll.value,
    })
    addDialog.value?.close()
    message.value = '已添加黑名单规则，节点正在同步。'
    await refresh()
  } catch (issue) {
    const detail = issue instanceof AdminRequestError ? issue.detail : undefined
    if (
      detail && typeof detail === 'object' && 'code' in detail && detail.code === 'confirm_all'
      && 'target' in detail && (detail.target === '0.0.0.0/0' || detail.target === '::/0')
    ) {
      draft.value.target = detail.target
      confirmTarget.value = detail.target
      confirmAll.value = false
    } else {
      actionError.value = (issue as Error).message
    }
  } finally {
    busy.value = false
  }
}

async function confirmIp(ip: string) {
  target.value = { path: `/blacklist?target=${encodeURIComponent(ip)}`, method: 'DELETE', name: ip }
  await open()
}

async function confirmTunnel(row: Block) {
  target.value = { path: `/tunnels/${encodeURIComponent(row.tunnel_id)}/unblock`, method: 'POST', name: row.proxy_name }
  await open()
}

async function open() {
  actionError.value = ''
  await nextTick()
  dialog.value?.showModal()
}

async function release() {
  if (!target.value || busy.value) return
  busy.value = true
  actionError.value = ''
  message.value = ''
  try {
    await request(target.value.path, target.value.method)
    message.value = '已提交解除，节点正在同步。'
    dialog.value?.close()
    await refresh()
  } catch (issue) {
    actionError.value = (issue as Error).message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <header class="page-heading">
    <h1>黑名单</h1>
    <button v-if="canOperate" class="console-button page-action" type="button" @click="showAddDialog">添加规则</button>
  </header>
  <p v-if="error" class="action-error" role="alert">{{ error }}</p>
  <p v-if="message" role="status">{{ message }}</p>
  <section v-if="data?.pending_nodes.length" class="sync-notice" aria-label="节点同步状态">
    <strong>等待以下节点同步 IP 黑名单</strong>
    <ul>
      <li v-for="node in data.pending_nodes" :key="node.id">
        {{ node.id }} · {{ node.status === 'online' ? node.blacklist_error || '待确认' : '离线' }}
      </li>
    </ul>
  </section>
  <p v-if="loading && !data" role="status">正在读取黑名单…</p>
  <section v-if="data" aria-labelledby="ip-title" class="admin-section">
    <h2 id="ip-title">IP 黑名单</h2>
    <p v-if="!data.entries.length" class="empty-panel">当前没有生效的 IP 黑名单。</p>
    <div v-else class="resource-table">
      <table aria-labelledby="ip-title">
        <thead><tr><th scope="col">客户端 IP 或网段</th><th scope="col">理由</th><th scope="col">来源</th><th scope="col">时长</th><th scope="col">到期时间</th><th v-if="canOperate" scope="col">操作</th></tr></thead>
        <tbody><tr v-for="row in data.entries" :key="row.ip">
          <td>{{ row.ip }}</td><td>{{ row.reason }}</td><td>{{ row.actor }}</td><td>{{ duration(row) }}</td>
          <td>{{ row.expires_at === null ? '手动解除' : dateTime(row.expires_at) }}</td>
          <td v-if="canOperate"><button class="console-button" type="button" @click="confirmIp(row.ip)">解除拉黑</button></td>
        </tr></tbody>
      </table>
    </div>
  </section>
  <section v-if="data" aria-labelledby="block-title" class="admin-section">
    <h2 id="block-title">隧道封禁</h2>
    <p v-if="!data.blocks.length" class="empty-panel">当前没有生效的隧道封禁。</p>
    <div v-else class="resource-table">
      <table aria-labelledby="block-title">
        <thead><tr><th scope="col">客户端 IP</th><th scope="col">隧道</th><th scope="col">理由</th><th scope="col">来源</th><th scope="col">时长</th><th scope="col">状态</th><th v-if="canOperate" scope="col">操作</th></tr></thead>
        <tbody><tr v-for="row in data.blocks" :key="row.id">
          <td>{{ row.client_ip || '未知' }}</td><td>{{ row.proxy_name }}<small class="cell-note">{{ row.node_id }}</small></td>
          <td>{{ row.source === 'scanner' && row.reason === '测试' ? '未检测到 Minecraft Java 响应' : row.reason }}</td>
          <td>{{ row.source === 'scanner' ? '扫描器自动封禁' : row.source.replace(/^admin:/, '') }}</td>
          <td>手动解除</td><td>{{ row.status === 'block_pending' ? '等待节点执行' : '已封禁' }}</td>
          <td v-if="canOperate"><button class="console-button" type="button" :disabled="row.status === 'block_pending'" @click="confirmTunnel(row)">解除封禁</button></td>
        </tr></tbody>
      </table>
    </div>
  </section>
  <dialog ref="addDialog" class="tunnel-dialog" aria-labelledby="add-rule-title" @cancel="busy && $event.preventDefault()">
    <form class="admin-form" @submit.prevent="addRule">
      <h2 id="add-rule-title">添加黑名单规则</h2>
      <label>IP 或 CIDR
        <input v-model.trim="draft.target" required maxlength="64" placeholder="例如 192.0.2.0/24" :disabled="busy" @input="clearConfirmation">
      </label>
      <label>拉黑时长
        <select v-model="draft.duration" :disabled="busy">
          <option value="1h">1 小时</option><option value="24h">24 小时</option><option value="7d">7 天</option><option value="30d">30 天</option><option value="permanent">永久</option>
        </select>
      </label>
      <label>封禁理由<textarea v-model.trim="draft.reason" required maxlength="200" rows="3" :disabled="busy" /></label>
      <label v-if="confirmTarget" class="sync-notice confirm-network">
        <input v-model="confirmAll" type="checkbox" required :disabled="busy">
        确认拉黑所有 {{ confirmTarget === '0.0.0.0/0' ? 'IPv4' : 'IPv6' }} 地址，并断开命中的现有隧道。
      </label>
      <p v-if="actionError" class="action-error" role="alert">{{ actionError }}</p>
      <div class="resource-actions">
        <button class="console-button" type="button" :disabled="busy" @click="addDialog?.close()">取消</button>
        <button class="console-button danger-button" type="submit" :disabled="busy || (!!confirmTarget && !confirmAll)">
          {{ confirmTarget ? '确认并添加' : '添加规则' }}
        </button>
      </div>
    </form>
  </dialog>
  <dialog ref="dialog" class="tunnel-dialog" aria-labelledby="release-title" @cancel="busy && $event.preventDefault()">
    <form v-if="target" class="admin-form" @submit.prevent="release">
      <h2 id="release-title">确认解除{{ target.method === 'DELETE' ? '拉黑' : '封禁' }}</h2>
      <p>{{ target.name }}</p>
      <p v-if="actionError" class="action-error" role="alert">{{ actionError }}</p>
      <div class="resource-actions">
        <button class="console-button" type="button" :disabled="busy" @click="dialog?.close()">取消</button>
        <button class="console-button" type="submit" :disabled="busy">确认解除</button>
      </div>
    </form>
  </dialog>
</template>


<style scoped>
.admin-form .confirm-network {
  grid-template-columns: auto 1fr;
  align-items: start;
  gap: 8px;
}

.confirm-network input {
  width: auto;
}
</style>
