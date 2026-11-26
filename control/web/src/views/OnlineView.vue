<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { dateTime, useAdminList } from '../admin'

type OnlineClient = {
  client_ip: string
  first_heartbeat: number
  last_heartbeat: number
  channel: 'dev' | 'stable' | null
  os_version: string | null
}

const { data, error, loading } = useAdminList<{ online: OnlineClient[] }>('/online', 3000)
const now = ref(Date.now())
let ticker = 0

onMounted(() => {
  ticker = window.setInterval(() => { now.value = Date.now() }, 1000)
})
onUnmounted(() => window.clearInterval(ticker))

const channels: Record<string, string> = { dev: '开发版', stable: '稳定版' }

function channelLabel(channel: string | null) {
  return channel ? channels[channel] ?? channel : '未知'
}

function relativeTime(timestamp: number) {
  if (!timestamp) return '—'
  const seconds = Math.max(0, Math.floor(now.value / 1000 - timestamp))
  if (seconds < 5) return '刚刚'
  if (seconds < 60) return `${seconds} 秒前`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

function isoTime(timestamp: number) {
  return new Date(timestamp * 1000).toISOString()
}
</script>

<template>
  <header class="page-heading">
    <h1>在线统计</h1>
    <span v-if="data" class="connection-state" data-state="live">{{ data.online.length }} 台在线</span>
  </header>
  <p v-if="error" class="action-error" role="alert">{{ error }}</p>
  <section v-if="!data?.online.length" class="empty-panel">
    {{ loading && !data ? '正在读取在线客户端…' : '当前没有在线客户端。' }}
  </section>
  <div v-else class="resource-table">
    <table aria-label="在线客户端">
      <thead>
        <tr>
          <th scope="col">序号</th>
          <th scope="col">首次心跳</th>
          <th scope="col">客户端 IP</th>
          <th scope="col">版本通道</th>
          <th scope="col">操作系统版本</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(client, index) in data.online" :key="client.client_ip">
          <td class="online-index">{{ index + 1 }}</td>
          <td>
            <time :datetime="isoTime(client.first_heartbeat)" :title="dateTime(client.first_heartbeat)">
              {{ relativeTime(client.first_heartbeat) }}
            </time>
          </td>
          <td>{{ client.client_ip }}</td>
          <td>{{ channelLabel(client.channel) }}</td>
          <td>{{ client.os_version || '未知' }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.online-index,
.resource-table time {
  font-variant-numeric: tabular-nums;
}

.online-index {
  width: 4.5rem;
  color: var(--muted);
}
</style>
