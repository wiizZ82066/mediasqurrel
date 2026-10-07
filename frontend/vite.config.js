import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const backendOrigin = `http://127.0.0.1:${process.env.MS_PORT || 8642}`

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1', // 明确 IPv4（默认 ::1 会导致 wait-on/Electron 探测失败）
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': backendOrigin,
      '/media': backendOrigin,
      '/ws': { target: backendOrigin.replace('http:', 'ws:'), ws: true },
    },
  },
})
