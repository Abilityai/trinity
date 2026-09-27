import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// Unit tests cover pure modules (utils/) by default — node environment, no DOM.
// A spec that must MOUNT a component (the click-reaches-the-store class the
// #2829 evidence bar asks for) opts into jsdom per file with a leading
// `// @vitest-environment jsdom` comment; the Vue plugin below is what lets
// such a file import an SFC. First consumer: portalThemeSwitch.spec.js
// (trinity-enterprise#625). Playwright e2e specs live in e2e/ and are
// explicitly out of scope here.
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  test: {
    environment: 'node',
    include: ['tests/unit/**/*.spec.js'],
  },
})
