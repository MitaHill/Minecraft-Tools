import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { account } from './session'

export const canOperate = computed(() => account.value?.role === 'operator' || account.value?.role === 'superadmin')

export function useAdmin() {
  const route = useRoute()
  const router = useRouter()

  async function request<T>(path: string, method = 'GET', data?: object): Promise<T> {
    const response = await fetch(`/api/admin${path}`, {
      method,
      cache: 'no-store',
      headers: data ? { 'Content-Type': 'application/json' } : undefined,
      body: data ? JSON.stringify(data) : undefined,
    })
    if (response.status === 401) {
      account.value = null
      await router.replace({ name: 'login', query: { next: String(route.name) } })
    }
    if (response.status === 204) return undefined as T
    const body = await response.json()
    if (!response.ok) {
      if (typeof body.detail === 'string') throw new Error(body.detail)
      if (Array.isArray(body.detail)) throw new Error('请检查输入内容。')
      throw new Error(`请求失败（${response.status}）`)
    }
    return body as T
  }

  return request
}

export function useAdminList<T>(path: string, interval = 0) {
  const request = useAdmin()
  const data = shallowRef<T>()
  const error = ref('')
  const loading = ref(false)
  let timer = 0
  let requestNumber = 0

  async function refresh() {
    const current = ++requestNumber
    loading.value = true
    try {
      const result = await request<T>(path)
      if (current !== requestNumber) return
      data.value = result
      error.value = ''
    } catch (issue) {
      if (current === requestNumber) error.value = (issue as Error).message
    } finally {
      if (current === requestNumber) loading.value = false
    }
  }

  onMounted(() => {
    void refresh()
    if (interval) timer = window.setInterval(() => { if (!loading.value) void refresh() }, interval)
  })
  onUnmounted(() => {
    window.clearInterval(timer)
    requestNumber++
  })
  return { data, error, loading, refresh, request }
}

export function dateTime(value: number) {
  return new Date(value * 1000).toLocaleString()
}
