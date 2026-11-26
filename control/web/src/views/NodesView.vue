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
  allowed_ports: string
  pending_allowed_ports: string | null
  deployed_ports: [number, number][] | null
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
const deleteDialog = ref<HTMLDialogElement>()
const deleting = ref<Node>()
const deleteConfirmed = ref(false)
const deleteError = ref('')
const deleteBusy = ref(false)

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
    const { name, public_host, allowed_ports, server_addr, server_port, client_channel } = row
    const tokenUpdate = clearFrpToken.value ? { frp_token: null }
      : frpToken.value ? { frp_token: frpToken.value } : {}
    const result = await request<{ pending: boolean }>(`/nodes/${encodeURIComponent(row.id)}`, 'PATCH', {
      name, public_host, allowed_ports, client_channel,
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

async function confirmDelete(row: Node) {
  deleting.value = row
  deleteConfirmed.value = false
  deleteError.value = ''
  message.value = ''
  await nextTick()
  deleteDialog.value?.showModal()
}

async function remove() {
  const row = deleting.value
  if (!row || deleteBusy.value || !deleteConfirmed.value) return
  deleteBusy.value = true
  deleteError.value = ''
  try {
    await request(`/nodes/${encodeURIComponent(row.id)}`, 'DELETE')
    message.value = '节点已删除。'
    deleteDialog.value?.close()
    await refresh()
  } catch (issue) {
    deleteError.value = (issue as Error).message
  } finally {
    deleteBusy.value = false
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
        <td>{{ node.allowed_ports }}
          <small v-if="node.pending_allowed_ports" class="cell-note">待确认 {{ node.pending_allowed_ports }}</small>
          <small v-if="node.range_error" class="cell-note action-error">{{ node.range_error }}</small>
        </td>
        <td v-if="canEdit">
          <button class="console-button" type="button" :disabled="node.pending_allowed_ports !== null" @click="edit(node)">编辑</button>
          <button class="console-button danger-button" type="button" @click="confirmDelete(node)">删除</button>
        </td>
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
      <label>允许端口
        <input v-model="editing.allowed_ports" required maxlength="1024" placeholder="30000-30010,31000"
          :disabled="busy || editing.status !== 'online' || !editing.deployed_ports">
      </label>
      <p class="cell-note">逗号分隔，段内用横杠；端口须落在部署范围内。</p>
      <p v-if="editing.status !== 'online'">节点上线后可编辑公网地址和允许端口。</p>
      <p v-else-if="!editing.deployed_ports">Agent 同步部署范围后可编辑允许端口。</p>
      <p v-if="saveError" class="action-error" role="alert">{{ saveError }}</p>
      <div class="resource-actions">
        <button class="console-button" type="button" :disabled="busy" @click="dialog?.close()">取消</button>
        <button class="console-button" type="submit" :disabled="busy">保存</button>
      </div>
    </form>
  </dialog>
  <dialog ref="deleteDialog" class="tunnel-dialog" aria-labelledby="delete-title" @cancel="deleteBusy && $event.preventDefault()">
    <form v-if="deleting" class="admin-form" @submit.prevent="remove">
      <h2 id="delete-title">删除节点 {{ deleting.id }}</h2>
      <p>节点的历史映射、封禁和扫描记录会一并删除，无法恢复。</p>
      <label><input v-model="deleteConfirmed" type="checkbox" :disabled="deleteBusy">确认删除该节点及其历史记录</label>
      <p v-if="deleteError" class="action-error" role="alert">{{ deleteError }}</p>
      <div class="resource-actions">
        <button class="console-button" type="button" :disabled="deleteBusy" @click="deleteDialog?.close()">取消</button>
        <button class="console-button danger-button" type="submit" :disabled="deleteBusy || !deleteConfirmed">确认删除</button>
      </div>
    </form>
  </dialog>
</template>
