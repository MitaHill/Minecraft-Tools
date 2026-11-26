<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { useAdminList } from '../admin'
import { account } from '../session'

type Node = {
  id: string
  name: string
  public_host: string
  server_addr: string | null
  server_port: number | null
  client_channel: 'hidden' | 'dev' | 'stable' | 'both'
  frp_token_configured: boolean
  status: string
  agent_version: string | null
  port_start: number
  port_end: number
  deployed_ports: [number, number][] | null
  pending_port_start: number | null
  pending_port_end: number | null
  range_error: string | null
}
const { data, error, loading, refresh, request } = useAdminList<{ nodes: Node[] }>('/nodes', 2500)
const canEdit = computed(() => account.value?.role === 'superadmin')
const dialog = ref<HTMLDialogElement>()
const editing = ref<Node>()
const frpToken = ref('')
const clearFrpToken = ref(false)
const saveError = ref('')
const message = ref('')
const busy = ref(false)

const range = (start: number, end: number) => start === end ? String(start) : `${start}–${end}`
const channels = { hidden: '未发布', dev: 'dev', stable: 'stable', both: 'dev 和 stable' }

async function edit(row: Node) {
  editing.value = { ...row }
  frpToken.value = ''
  clearFrpToken.value = false
  saveError.value = ''
  await nextTick()
  dialog.value?.showModal()
}

async function save() {
  const row = editing.value
  if (!row || busy.value) return
  busy.value = true
  saveError.value = ''
  message.value = ''
  try {
    const { name, public_host, port_start, port_end, server_addr, server_port, client_channel } = row
    const tokenUpdate = clearFrpToken.value ? { frp_token: null }
      : frpToken.value ? { frp_token: frpToken.value } : {}
    const result = await request<{ pending: boolean }>(`/nodes/${encodeURIComponent(row.id)}`, 'PATCH', {
      name, public_host, port_start, port_end, client_channel,
      server_addr: server_addr?.trim() || null, server_port: server_port || null, ...tokenUpdate,
    })
    message.value = result.pending ? '已提交端口范围，等待节点确认。' : '节点信息已保存。'
    dialog.value?.close()
    await refresh()
  } catch (issue) {
    saveError.value = (issue as Error).message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <header class="page-heading"><h1>节点</h1></header>
  <p v-if="error" class="action-error" role="alert">{{ error }}</p>
  <p v-if="message" role="status">{{ message }}</p>
  <section v-if="!data?.nodes.length" class="empty-panel">{{ loading && !data ? '正在读取节点…' : '暂无节点。' }}</section>
  <div v-else class="resource-table">
    <table aria-label="节点">
      <thead><tr>
        <th scope="col">节点</th><th scope="col">公网地址</th><th scope="col">状态</th><th scope="col">Agent 版本</th>
        <th scope="col">部署范围</th><th scope="col">生效范围</th><th v-if="canEdit" scope="col">操作</th>
      </tr></thead>
      <tbody><tr v-for="node in data.nodes" :key="node.id">
        <td><strong>{{ node.name }}</strong><small class="cell-note">{{ node.id }} · {{ channels[node.client_channel] }}</small></td>
        <td>{{ node.public_host }}</td><td>{{ node.status === 'online' ? '在线' : '离线' }}</td><td>{{ node.agent_version || '—' }}</td>
        <td>{{ node.deployed_ports?.map(([start, end]) => range(start, end)).join('、') || '尚未同步' }}</td>
        <td>{{ range(node.port_start, node.port_end) }}
          <small v-if="node.pending_port_start !== null && node.pending_port_end !== null" class="cell-note">
            待确认 {{ range(node.pending_port_start, node.pending_port_end) }}
          </small>
          <small v-if="node.range_error" class="cell-note action-error">{{ node.range_error }}</small>
        </td>
        <td v-if="canEdit"><button class="console-button" type="button" :disabled="node.pending_port_start !== null" @click="edit(node)">编辑</button></td>
      </tr></tbody>
    </table>
  </div>
  <dialog ref="dialog" class="tunnel-dialog" aria-labelledby="node-title" @cancel="busy && $event.preventDefault()">
    <form v-if="editing" class="admin-form" @submit.prevent="save">
      <h2 id="node-title">编辑节点 {{ editing.id }}</h2>
      <label>节点名称<input v-model="editing.name" required maxlength="80" :disabled="busy"></label>
      <label>公网主机名或 IP<input v-model="editing.public_host" required maxlength="253" :disabled="busy || editing.status !== 'online'"></label>
      <label>客户端发布通道
        <select v-model="editing.client_channel" :disabled="busy">
          <option v-for="(label, channel) in channels" :key="channel" :value="channel">{{ label }}</option>
        </select>
      </label>
      <div class="port-fields">
        <label>frpc 连接主机<input v-model="editing.server_addr" maxlength="253" placeholder="默认使用公网主机" :disabled="busy"></label>
        <label>frpc 连接端口<input v-model.number="editing.server_port" type="number" min="1" max="65535" :required="editing.client_channel !== 'hidden'" :disabled="busy"></label>
      </div>
      <label>frpc 登录令牌<input v-model="frpToken" type="password" maxlength="1024" autocomplete="new-password" :placeholder="editing.frp_token_configured ? '留空保留现有令牌' : '可选'" :disabled="busy || clearFrpToken"></label>
      <label v-if="editing.frp_token_configured"><input v-model="clearFrpToken" type="checkbox" :disabled="busy">清除 frpc 登录令牌</label>
      <div class="port-fields">
        <label>起始端口<input v-model.number="editing.port_start" type="number" required min="1024" max="65535" :disabled="busy || editing.status !== 'online' || !editing.deployed_ports"></label>
        <label>结束端口<input v-model.number="editing.port_end" type="number" required min="1024" max="65535" :disabled="busy || editing.status !== 'online' || !editing.deployed_ports"></label>
      </div>
      <p v-if="editing.status !== 'online'">节点上线后可编辑公网地址和端口范围。</p>
      <p v-else-if="!editing.deployed_ports">Agent 同步部署范围后可编辑端口范围。</p>
      <p v-if="saveError" class="action-error" role="alert">{{ saveError }}</p>
      <div class="resource-actions">
        <button class="console-button" type="button" :disabled="busy" @click="dialog?.close()">取消</button>
        <button class="console-button" type="submit" :disabled="busy">保存</button>
      </div>
    </form>
  </dialog>
</template>
