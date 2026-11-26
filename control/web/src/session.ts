import { shallowRef } from 'vue'

type Account = {
  username: string
  role: 'superadmin' | 'operator' | 'viewer'
}

export const account = shallowRef<Account | null>(null)

export async function checkSession() {
  const response = await fetch('/api/admin/session', { cache: 'no-store' })
  if (response.status !== 401 && !response.ok) throw new Error("无法连接控制端。")
  account.value = response.ok ? (await response.json() as Account) : null
  return account.value
}

export async function signIn(username: string, password: string) {
  const response = await fetch('/api/admin/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (response.status === 401) throw new Error('用户名或密码不正确。')
  if (response.status === 429) throw new Error('登录次数过多，请稍后再试。')
  if (!response.ok) throw new Error('暂时无法登录。')
  account.value = await response.json() as Account
}

export async function signOut() {
  const response = await fetch('/api/admin/session', { method: 'DELETE' })
  if (!response.ok) throw new Error('暂时无法退出，请重试。')
  account.value = null
}
