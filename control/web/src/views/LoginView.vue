<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { navigation } from '../router'
import { signIn } from '../session'

const route = useRoute()
const router = useRouter()
const username = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)

async function submit() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await signIn(username.value, password.value)
    const destination = route.query.next
    await router.replace(typeof destination === 'string' && navigation.some(item => item.name === destination)
      ? { name: destination }
      : { name: 'overview' })
  } catch (reason) {
    error.value = reason instanceof TypeError ? '无法连接控制端。' : (reason as Error).message
  } finally {
    password.value = ''
    busy.value = false
  }
}
</script>

<template>
  <header class="page-heading"><h1>管理台登录</h1></header>
  <form class="login-form" @submit.prevent="submit">
    <label for="username">用户名</label>
    <input id="username" v-model="username" autocomplete="username" maxlength="80" required>
    <label for="password">密码</label>
    <input id="password" v-model="password" type="password" autocomplete="current-password" maxlength="512" required>
    <p v-if="error" class="action-error" role="alert">{{ error }}</p>
    <button class="console-button" type="submit" :disabled="busy">{{ busy ? '正在登录…' : '登录' }}</button>
  </form>
</template>
