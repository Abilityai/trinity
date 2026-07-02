import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

// Use localhost for local dev, backend for Docker
const backendHost = process.env.DOCKER_ENV ? 'backend' : 'localhost'

// Issue #549: dev-mode security headers. Production nginx adds these via
// security-headers.conf; without this plugin, `npm run dev` serves HTML
// without any security headers and devs miss CSP-violation regressions
// until prod. Mirrors security-headers.conf as closely as Vite's
// JS-runtime-emitted asset model allows. HSTS is intentionally NOT set
// here — dev runs over HTTP and HSTS would force the dev box's hostname
// to HTTPS in the browser HSTS cache, breaking any subsequent HTTP work
// on the same port.
const devSecurityHeaders = {
  'X-Frame-Options': 'SAMEORIGIN',
  'X-Content-Type-Options': 'nosniff',
  'X-XSS-Protection': '0',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'Permissions-Policy': 'camera=(), microphone=(self), geolocation=(), payment=()',
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Resource-Policy': 'same-origin',
  // Vite injects HMR client + inline module preloads; 'unsafe-inline' on
  // script-src is needed in dev only. Production CSP in security-headers.conf
  // is stricter (no script 'unsafe-inline').
  //
  // blob: note (#1400): connect-src, media-src, object-src and frame-src MUST
  // include blob: so the Files-tab preview/download can fetch(URL.createObjectURL
  // (blob)) and render blob: URLs via <img>/<video>/<audio> and PDF <embed>. The
  // PDF <embed> needs BOTH object-src (element) and frame-src (Chrome renders the
  // embedded PDF in an internal viewer frame). Keep in sync with the mirrored
  // production CSP in security-headers.conf.
  'Content-Security-Policy':
    "default-src 'self'; " +
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; " +
    "style-src 'self' 'unsafe-inline'; " +
    "img-src 'self' data: blob:; " +
    "media-src 'self' blob:; " +
    "font-src 'self'; " +
    "connect-src 'self' blob: ws: wss: https://us-central1-mcp-server-project-455215.cloudfunctions.net https://intake.abilityai.dev; " +
    "object-src 'self' blob:; " +
    "frame-src 'self' blob:; " +
    "frame-ancestors 'self'; " +
    "base-uri 'self'; " +
    "form-action 'self'",
}

const securityHeadersPlugin = {
  name: 'trinity-security-headers',
  configureServer(server) {
    server.middlewares.use((req, res, next) => {
      for (const [name, value] of Object.entries(devSecurityHeaders)) {
        res.setHeader(name, value)
      }
      next()
    })
  },
}

export default defineConfig({
  plugins: [vue(), securityHeadersPlugin],
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
