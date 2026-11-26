<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { canOperate, useAdminList } from '../admin'

type Room = {
  listing_id: string; room_name: string; game_version: string | null
  player_count: number; max_players: number; description: string | null
  server_addr: string; remote_port: number; host_player: string
}
type Decision = { action: 'remove'; room: Room } | { action: 'toggle'; enabled: boolean }

const { data, error, loading, refresh, request } = useAdminList<{ enabled: boolean; rooms: Room[] }>('/lobby/rooms', 3000)
const dialog = ref<HTMLDialogElement>()
const decision = ref<Decision>()
const busy = ref(false)
const actionError = ref('')
const message = ref('')

function address(room: Room) {
  const host = room.server_addr
  return `${host.includes(':') ? `[${host}]` : host}:${room.remote_port}`
}

async function confirm(value: Decision) {
  decision.value = value
  actionError.value = ''
  await nextTick()
  dialog.value?.showModal()
}

async function submit() {
  const value = decision.value
  if (busy.value || !value) return
  busy.value = true
  actionError.value = ''
  message.value = ''
  try {
    if (value.action === 'remove') {
      await request(`/lobby/rooms/${encodeURIComponent(value.room.listing_id)}`, 'DELETE')
      message.value = '房间已下架。'
    } else {
      await request('/lobby', 'PATCH', { enabled: value.enabled })
      message.value = value.enabled ? '大厅已开放。' : '大厅已关闭。'
    }
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
    <h1>公开联机大厅</h1>
    <span v-if="data" class="connection-state" :data-state="data.enabled ? 'live' : 'retrying'">{{ data.enabled ? '已开放' : '已关闭' }}</span>
    <button v-if="data && canOperate" class="console-button page-action" :class="{ 'danger-button': data.enabled }" type="button" @click="confirm({ action: 'toggle', enabled: !data.enabled })">
      {{ data.enabled ? '关闭大厅' : '重新开放大厅' }}
    </button>
  </header>
  <p v-if="error" class="action-error" role="alert">{{ error }}</p>
  <p v-if="message" role="status">{{ message }}</p>
  <section v-if="!data?.rooms.length" class="empty-panel">{{ loading && !data ? '正在读取房间…' : '当前没有存活的公开房间。' }}</section>
  <ul v-else class="room-grid" aria-label="存活的公开房间">
    <li v-for="room in data.rooms" :key="room.listing_id" class="room-card">
      <div class="room-heading"><h2>{{ room.room_name }}</h2><span>{{ room.player_count }}/{{ room.max_players }} 人</span></div>
      <dl class="details-list">
        <dt>版本</dt><dd>{{ room.game_version || '未知' }}</dd>
        <dt>房主</dt><dd>{{ room.host_player }}</dd>
        <dt>连接地址</dt><dd>{{ address(room) }}</dd>
      </dl>
      <p v-if="room.description">{{ room.description }}</p>
      <button v-if="canOperate" class="console-button danger-button" type="button" @click="confirm({ action: 'remove', room })">下架房间</button>
    </li>
  </ul>
  <dialog ref="dialog" class="tunnel-dialog" aria-labelledby="lobby-title" @cancel="busy && $event.preventDefault()">
    <form v-if="decision" class="admin-form" @submit.prevent="submit">
      <h2 id="lobby-title">
        {{ decision.action === 'remove' ? '确认下架房间' : decision.enabled ? '确认重新开放大厅' : '确认关闭大厅' }}
      </h2>
      <p v-if="decision.action === 'remove'">{{ decision.room.room_name }} 将从公开大厅移除。房主可手动重新发布。</p>
      <p v-else-if="decision.enabled">仍在续期的房间将恢复展示。</p>
      <p v-else>客户端暂时无法在大厅查看公开房间。</p>
      <p v-if="actionError" class="action-error" role="alert">{{ actionError }}</p>
      <div class="resource-actions">
        <button class="console-button" type="button" :disabled="busy" @click="dialog?.close()">取消</button>
        <button class="console-button" type="submit" :class="{ 'danger-button': decision.action === 'remove' || !decision.enabled }" :disabled="busy">确认</button>
      </div>
    </form>
  </dialog>
</template>
