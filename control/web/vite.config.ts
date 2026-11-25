import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const { CONTROL_API_TARGET = 'http://127.0.0.1:8765' } = loadEnv(
    mode,
    process.cwd(),
    'CONTROL_API_TARGET',
  )

  return {
    base: '/admin/',
    plugins: [vue()],
    server: {
      host: '127.0.0.1',
      proxy: {
        '/health': CONTROL_API_TARGET,
        '/api/admin': CONTROL_API_TARGET,
      },
    },
  }
})
