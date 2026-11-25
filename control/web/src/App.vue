<script setup lang="ts">
import { Box } from '@element-plus/icons-vue'
import { ref } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { navigation } from './router'
import { account, signOut } from './session'

const route = useRoute()
const router = useRouter()
const logoutError = ref('')

async function logout() {
  logoutError.value = ''
  try {
    await signOut()
    await router.replace({ name: 'login' })
  } catch {
    logoutError.value = '暂时无法退出，请重试。'
  }
}
</script>

<template>
  <a class="skip-link" href="#main-content">跳到内容</a>
  <div class="console">
    <aside class="sidebar">
      <RouterLink class="brand" :to="{ name: 'overview' }">
        <span class="brand-mark"><Box aria-hidden="true" /></span>
        <span class="brand-text">
          <strong>Minecraft Tools</strong>
          <span>Control Server</span>
        </span>
      </RouterLink>

      <nav v-if="account" class="navigation" aria-label="管理台">
        <RouterLink v-for="item in navigation" :key="item.name" :to="{ name: item.name }">
          <component :is="item.icon" aria-hidden="true" />
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>

    <div class="console-content">
      <header class="topbar">
        <nav class="breadcrumb" aria-label="当前位置">
          <RouterLink v-if="route.name !== 'overview'" :to="{ name: 'overview' }">
            控制台
          </RouterLink>
          <span v-else>控制台</span>
          <span class="breadcrumb-separator" aria-hidden="true">/</span>
          <span aria-current="page">{{ route.meta.title }}</span>
        </nav>
        <div v-if="account" class="session-actions">
          <span>{{ account.username }}</span>
          <button class="console-button" type="button" @click="logout">退出</button>
        </div>
      </header>

      <main id="main-content" class="workspace" tabindex="-1">
        <p v-if="logoutError" class="action-error" role="alert">{{ logoutError }}</p>
        <RouterView />
      </main>
    </div>
  </div>
</template>
