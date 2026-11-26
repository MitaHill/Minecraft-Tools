<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { canOperate, dateTime, useAdminList } from '../admin'

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
const target = ref<{ path: string; method: 'POST' | 'DELETE'; name: string }>()
const busy = ref(false)
const actionError = ref('')
const message = ref('')

function duration(row: Entry) {
  if (row.expires_at === null) return '永久'
  const hours = Math.round((row.expires_at - row.created_at) / 3600)
  return hours > 24 ? `${Math.round(hours / 24)} 天` : `${hours} 小时`
}

async function confirmIp(ip: string) {
  target.value = { path: `/blacklist/${encodeURIComponent(ip)}`, method: 'DELETE', name: ip }
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
  <header class="page-heading"><h1>黑名单</h1></header>
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
        <thead><tr><th scope="col">客户端 IP</th><th scope="col">理由</th><th scope="col">来源</th><th scope="col">时长</th><th scope="col">到期时间</th><th v-if="canOperate" scope="col">操作</th></tr></thead>
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
