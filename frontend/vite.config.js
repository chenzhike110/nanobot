import { resolve } from 'node:path'
import { defineConfig } from 'vite'

const apiTarget = process.env.NANOBOT_WEB_API_TARGET || 'http://127.0.0.1:8765'

export default defineConfig({
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/config': apiTarget,
      '/events': apiTarget,
      '/health': apiTarget,
      '/history': apiTarget,
      '/poll': apiTarget,
      '/message': apiTarget,
      '/upload': apiTarget,
      '/files': apiTarget,
    },
  },
  build: {
    outDir: resolve(__dirname, '../nanobot/webui/dist'),
    emptyOutDir: true,
  },
})
