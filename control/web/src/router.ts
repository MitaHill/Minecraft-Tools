import { Connection, Grid, House, Lock, Monitor, Setting } from '@element-plus/icons-vue'
import { createRouter, createWebHistory } from 'vue-router'
import { checkSession } from './session'
import OverviewView from './views/OverviewView.vue'
import NodesView from './views/NodesView.vue'
import TunnelsView from './views/TunnelsView.vue'

export const navigation = [
  { name: 'overview', label: '概览', icon: Grid },
  { name: 'nodes', label: '节点', icon: Monitor },
  { name: 'tunnels', label: '隧道', icon: Connection },
  { name: 'blacklist', label: '黑名单', icon: Lock },
  { name: 'lobby', label: '公开联机大厅', icon: House },
  { name: 'settings', label: '设定', icon: Setting },
] as const

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', name: 'overview', component: OverviewView, meta: { title: '概览', protected: true } },
    {
      path: '/login', name: 'login', component: () => import('./views/LoginView.vue'),
      meta: { title: '登录' },
    },
    {
      path: '/nodes',
      name: 'nodes',
      component: NodesView,
      meta: { title: '节点', protected: true },
    },
    {
      path: '/tunnels',
      name: 'tunnels',
      component: TunnelsView,
      meta: { title: '隧道', protected: true },
    },
    {
      path: '/blacklist', name: 'blacklist', component: () => import('./views/BlacklistView.vue'),
      meta: { title: '黑名单', protected: true },
    },
    {
      path: '/lobby', name: 'lobby', component: () => import('./views/LobbyView.vue'),
      meta: { title: '公开联机大厅', protected: true },
    },
    {
      path: '/settings', name: 'settings', component: () => import('./views/SettingsView.vue'),
      meta: { title: '设定', protected: true },
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('./views/NotFoundView.vue'),
      meta: { title: '页面未找到' },
    },
  ],
})

router.beforeEach(async to => {
  if (!to.meta.protected) return
  try {
    if (await checkSession()) return
  } catch {
    // The login view can report an unavailable control server when the user retries.
  }
  return { name: 'login', query: { next: String(to.name) } }
})

router.afterEach((to, _from, failure) => {
  if (!failure) {
    document.title = `${to.meta.title} · Minecraft Tools 控制台`
  }
})
