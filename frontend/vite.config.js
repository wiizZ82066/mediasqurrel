import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1', // 明确 IPv4（默认 ::1 会导致 wait-on/Electron 探测失败）
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8642',
      '/media': 'http://127.0.0.1:8642',
      '/ws': { target: 'ws://127.0.0.1:8642', ws: true },
    },
  },
})
