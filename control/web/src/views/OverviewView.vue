<script setup lang="ts">
import { ArrowRight, Connection, Monitor, Refresh } from '@element-plus/icons-vue'
import { ElButton } from 'element-plus'
import 'element-plus/es/components/button/style/css'
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

type HealthState = 'checking' | 'healthy' | 'unavailable'

const healthLabels: Record<HealthState, string> = {
  checking: '检查中',
  healthy: '运行正常',
  unavailable: '服务不可用',
}

const destinations = [
  { name: 'nodes', title: '节点', icon: Monitor },
  { name: 'tunnels', title: '隧道', icon: Connection },
]

const health = ref<HealthState>('checking')
const checkedAt = ref<Date>()
const timeFormat = new Intl.DateTimeFormat('zh-CN', { timeStyle: 'medium', hour12: false })

async function checkHealth() {
  health.value = 'checking'
  try {
    const response = await fetch('/health', {
      cache: 'no-store',
      signal: AbortSignal.timeout(5000),
    })
    const report = response.ok ? await response.json() : null
    health.value = report?.status === 'ok' ? 'healthy' : 'unavailable'
  } catch {
    health.value = 'unavailable'
  } finally {
    checkedAt.value = new Date()
  }
}

onMounted(checkHealth)
</script>

<template>
  <header class="page-heading">
    <h1>控制台概览</h1>
  </header>

  <section class="health-card" aria-labelledby="health-title">
    <div class="health-header">
      <h2 id="health-title">控制端状态</h2>
      <ElButton class="refresh-button" :icon="Refresh" :disabled="health === 'checking'" @click="checkHealth">
        刷新
      </ElButton>
    </div>
    <div class="health-body">
      <div class="health-status" role="status" :data-state="health">
        <span class="status-dot" aria-hidden="true"></span>
        <strong>{{ healthLabels[health] }}</strong>
      </div>
      <span v-if="checkedAt" class="health-time">
        最近检查 <time :datetime="checkedAt.toISOString()">{{ timeFormat.format(checkedAt) }}</time>
      </span>
    </div>
  </section>

  <section class="resources" aria-labelledby="resources-title">
    <h2 id="resources-title">资源管理</h2>
    <div class="resource-grid">
      <RouterLink
        v-for="item in destinations"
        :key="item.name"
        class="resource-link"
        :to="{ name: item.name }"
      >
        <span class="resource-icon"><component :is="item.icon" aria-hidden="true" /></span>
        <strong>{{ item.title }}</strong>
        <ArrowRight class="resource-arrow" aria-hidden="true" />
      </RouterLink>
    </div>
  </section>
</template>
