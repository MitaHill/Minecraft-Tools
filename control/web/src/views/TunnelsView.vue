<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { canOperate, dateTime, useAdmin } from '../admin'
import { checkSession } from '../session'

type Tunnel = {
  id: string
  run_id: string
  created_at: number
  proxy_name: string
  node_id: string
  remote_port: number
  status: 'pending' | 'active'
  client_ip: string | null
}
type Scan = {
  id: number
  checked_at: number
  result: string
  latency_ms: number | null
  version: string | null
  motd: string | null
}

const router = useRouter()
const request = useAdmin()
const rows = ref<Tunnel[]>([])
const connection = ref<'connecting' | 'live' | 'retrying'>('connecting')
const scannerEnabled = ref(false)
const dialog = ref<HTMLDialogElement>()
const decision = ref<{ action: 'block' | 'blacklist'; row: Tunnel }>()
const reason = ref('')
const duration = ref('24h')
const acting = ref(false)
const error = ref('')
const formError = ref('')
const message = ref('')
const scanTunnel = ref<Tunnel>()
const scans = ref<Scan[]>([])
const scanStatus = ref('')
const scanLabels: Record<string, string> = {
  ok: 'Minecraft Java', not_minecraft: '非 Minecraft', unreachable: '不可达', unknown: '无法判定',
}

let socket: WebSocket | undefined
let retryTimer = 0
let disposed = false
let retryDelay = 1000
let scanRequest = 0

function connect() {
  if (disposed) return
  const url = new URL('/api/admin/tunnels/live', window.location.origin)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  const client = new WebSocket(url)
  socket = client
  client.onmessage = event => {
    if (disposed || client !== socket) return
    const update = JSON.parse(event.data)
    if (!Array.isArray(update.tunnels)) return
    rows.value = update.tunnels
    scannerEnabled.value = update.scanner_enabled === true
    connection.value = 'live'
    retryDelay = 1000
  }
  client.onclose = async () => {
    if (disposed || client !== socket) return
    connection.value = 'retrying'
    rows.value = []
    try {
      if (!await checkSession()) {
        if (!disposed) await router.replace({ name: 'login', query: { next: 'tunnels' } })
        return
      }
    } catch {
      // Retry when the control server becomes reachable again.
    }
    if (!disposed) {
      retryTimer = window.setTimeout(connect, retryDelay)
      retryDelay = Math.min(15000, retryDelay * 2)
    }
  }
}

onMounted(connect)
onUnmounted(() => {
  disposed = true
  window.clearTimeout(retryTimer)
  socket?.close()
})

async function confirmAction(action: 'block' | 'blacklist', row: Tunnel) {
  decision.value = { action, row }
  reason.value = ''
  duration.value = '24h'
  formError.value = ''
  await nextTick()
  dialog.value?.showModal()
}

async function moderate() {
  const item = decision.value
  if (!item || acting.value) return
  if (!reason.value.trim()) {
    formError.value = '请输入封禁理由。'
    return
  }
  acting.value = true
  formError.value = ''
  error.value = ''
  message.value = ''
  try {
    const body = {
      run_id: item.row.run_id,
      created_at: item.row.created_at,
      reason: reason.value.trim(),
      ...(item.action === 'blacklist' ? { duration: duration.value } : {}),
    }
    await request(`/tunnels/${encodeURIComponent(item.row.id)}/${item.action}`, 'POST', body)
    dialog.value?.close()
    message.value = item.action === 'block' ? '已提交封禁，等待节点执行。' : '已拉黑客户端 IP，节点正在同步。'
  } catch (issue) {
    formError.value = (issue as Error).message
  } finally {
    acting.value = false
  }
}

async function showScans(row: Tunnel) {
  scanTunnel.value = row
  scans.value = []
  scanStatus.value = '正在读取检测记录…'
  const current = ++scanRequest
  try {
    const result = await request<{ scans: Scan[] }>(`/scans?tunnel_id=${encodeURIComponent(row.id)}`)
    if (current !== scanRequest) return
    scans.value = result.scans
    scanStatus.value = result.scans.length ? '' : '暂无检测记录。'
  } catch (issue) {
    if (current === scanRequest) scanStatus.value = (issue as Error).message
  }
}

async function rescan(row: Tunnel) {
  if (acting.value) return
  acting.value = true
  error.value = ''
  message.value = ''
  try {
    await request(`/tunnels/${encodeURIComponent(row.id)}/rescan`, 'POST')
    message.value = '已安排重扫。'
  } catch (issue) {
    error.value = (issue as Error).message
  } finally {
    acting.value = false
  }
}
</script>

<template>
  <header class="page-heading">
    <h1>隧道</h1>
    <span class="connection-state" :data-state="connection" role="status">
      {{ connection === 'live' ? '实时连接' : connection === 'retrying' ? '连接中断，正在重连' : '正在连接…' }}
    </span>
  </header>
  <p v-if="error" class="action-error" role="alert">{{ error }}</p>
  <p v-if="message" role="status">{{ message }}</p>
  <section v-if="connection === 'live' && !rows.length" class="empty-panel">当前没有存活的隧道。</section>
  <div v-if="rows.length" class="resource-table">
    <table aria-label="存活的隧道">
      <thead><tr><th scope="col">隧道</th><th scope="col">客户端 IP</th><th scope="col">节点</th><th scope="col">远程端口</th><th scope="col">状态</th><th scope="col">操作</th></tr></thead>
      <tbody>
        <tr v-for="row in rows" :key="row.id">
          <td>{{ row.proxy_name }}</td><td>{{ row.client_ip || '未知' }}</td><td>{{ row.node_id }}</td>
          <td>{{ row.remote_port }}</td><td>{{ row.status === 'active' ? '运行中' : '待检测' }}</td>
          <td>
            <div class="resource-actions tunnel-row-actions">
              <button class="console-button" type="button" @click="showScans(row)">检测记录</button>
              <button v-if="canOperate && scannerEnabled" class="console-button" type="button" :disabled="acting" @click="rescan(row)">重扫</button>
              <button v-if="canOperate" class="console-button danger-button" type="button" :disabled="acting" @click="confirmAction('block', row)">封禁</button>
              <button v-if="canOperate" class="console-button danger-button" type="button" :disabled="acting || !row.client_ip" :title="row.client_ip ? undefined : '暂未取得客户端 IP'" @click="confirmAction('blacklist', row)">拉黑</button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
  <section v-if="scanTunnel" class="resource-table scan-history" aria-labelledby="scan-title">
    <header class="resource-actions">
      <h2 id="scan-title">{{ scanTunnel.proxy_name }} 的检测记录</h2>
      <button class="console-button" type="button" @click="scanTunnel = undefined">关闭</button>
    </header>
    <p v-if="scanStatus" role="status">{{ scanStatus }}</p>
    <table v-else aria-labelledby="scan-title">
      <thead><tr><th scope="col">时间</th><th scope="col">结果</th><th scope="col">延迟</th><th scope="col">版本</th><th scope="col">MOTD</th></tr></thead>
      <tbody>
        <tr v-for="scan in scans" :key="scan.id">
          <td>{{ dateTime(scan.checked_at) }}</td><td>{{ scanLabels[scan.result] ?? scan.result }}</td>
          <td>{{ scan.latency_ms === null ? '—' : `${scan.latency_ms} ms` }}</td>
          <td>{{ scan.version ?? '—' }}</td><td>{{ scan.motd || '—' }}</td>
        </tr>
      </tbody>
    </table>
  </section>
  <dialog ref="dialog" class="tunnel-dialog" aria-labelledby="moderation-title" @cancel="acting && $event.preventDefault()">
    <form v-if="decision" class="admin-form" @submit.prevent="moderate">
      <h2 id="moderation-title">确认{{ decision.action === 'block' ? '封禁隧道' : '拉黑客户端 IP' }}</h2>
      <dl class="details-list">
        <dt>客户端 IP</dt><dd>{{ decision.row.client_ip || '未知' }}</dd>
        <dt>隧道名称</dt><dd>{{ decision.row.proxy_name }}</dd>
      </dl>
      <p>{{ decision.action === 'block' ? '当前映射将被封禁，关联的大厅房间会下架。' : '所有受控节点将拒绝此 IP 建立隧道，现有连接会断开。' }}</p>
      <label v-if="decision.action === 'blacklist'">拉黑时长
        <select v-model="duration" :disabled="acting">
          <option value="1h">1 小时</option><option value="24h">24 小时</option><option value="7d">7 天</option><option value="30d">30 天</option><option value="permanent">永久</option>
        </select>
      </label>
      <label>封禁理由<textarea v-model="reason" required maxlength="200" rows="3" :disabled="acting" /></label>
      <p v-if="formError" class="action-error" role="alert">{{ formError }}</p>
      <div class="resource-actions">
        <button class="console-button" type="button" :disabled="acting" @click="dialog?.close()">取消</button>
        <button class="console-button danger-button" type="submit" :disabled="acting || connection !== 'live'">{{ decision.action === 'block' ? '确认封禁' : '确认拉黑' }}</button>
      </div>
    </form>
  </dialog>
</template>
