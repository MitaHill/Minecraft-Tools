import { Connection, Grid, Monitor } from '@element-plus/icons-vue'
import { createRouter, createWebHistory } from 'vue-router'
import { checkSession } from './session'
import OverviewView from './views/OverviewView.vue'
import ResourceView from './views/ResourceView.vue'

export const navigation = [
  { name: 'overview', label: '概览', icon: Grid },
  { name: 'nodes', label: '节点', icon: Monitor },
  { name: 'tunnels', label: '隧道', icon: Connection },
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
      component: ResourceView,
      props: { title: '节点', icon: Monitor, kind: 'nodes' },
      meta: { title: '节点', protected: true },
    },
    {
      path: '/tunnels',
      name: 'tunnels',
      component: ResourceView,
      props: { title: '隧道', icon: Connection, kind: 'tunnels' },
      meta: { title: '隧道', protected: true },
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
