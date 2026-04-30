import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

// Use localhost for local dev, backend for Docker
const backendHost = process.env.DOCKER_ENV ? 'backend' : 'localhost'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 80,
    // Allow all hosts - Trinity runs behind a reverse proxy that handles host validation
    allowedHosts: true,
    proxy: {
      // Trailing slash matters: `/api` (no slash) is a prefix that captures
      // the SPA route `/api-keys` and forwards it to the backend → 404. Use
      // `/api/` so only proper API paths are proxied.
      '/api/': {
        target: `http://${backendHost}:8000`,
        changeOrigin: true,
        ws: true,
      },
      '/token': {
        target: `http://${backendHost}:8000`,
        changeOrigin: true,
      },
      '/ws': {
        target: `ws://${backendHost}:8000`,
        ws: true,
      },
    }
  }
})
