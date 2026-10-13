// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A — sign-off: the Inbox had no light/dark switch.
 *
 * The chat and the room each render the shell's theme switch through their
 * header's `header-end` slot. The Inbox's own header (new in PR A) had no such
 * slot, so on /workspace/inbox the switch simply was not there. The Inbox now
 * exposes the same seam and the shell fills it the same way.
 *
 * @source-text-pin: the second half is that the SHELL (views/Portal.vue) fills
 * the slot with PortalThemeSwitch; mounting the whole shell to see one child
 * would need the full Workspace stack. The slot itself is proven mounted.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import { useClientPortalStore } from '@/stores/clientPortal'
import PortalInbox from '@/components/portal/PortalInbox.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

let router
beforeEach(async () => {
  window.matchMedia = (q) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })
  setActivePinia(createPinia())
  const store = useClientPortalStore()
  store.asksAvailable = true
  store.fetchAsks = vi.fn(async () => [])
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/workspace/inbox', component: { template: '<div />' } }] })
  await router.push('/workspace/inbox')
  await router.isReady()
})

describe('the Inbox header carries the theme switch', () => {
  it('renders the header-end slot inside its header, on every tab', async () => {
    for (const tab of ['action', 'unread', 'all']) {
      await router.replace({ path: '/workspace/inbox', query: { tab } })
      const w = mount(PortalInbox, {
        props: { threads: [], previews: {}, threadsLoaded: true, threadsFailed: false, labels: {} },
        slots: { 'header-end': '<button data-testid="theme-slot">Theme</button>' },
        global: { plugins: [router] },
      })
      await flushPromises()
      expect(w.find('[data-testid="inbox-header"] [data-testid="theme-slot"]').exists(), tab).toBe(true)
      w.unmount()
    }
  })

  it('the shell fills it with PortalThemeSwitch, as it does for the chat and the room', () => {
    const shell = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src', 'views', 'Portal.vue'), 'utf8')
    const start = shell.indexOf('<PortalInbox')
    const end = shell.indexOf('</PortalInbox>')
    // A self-closing <PortalInbox /> has no slot content at all — and would let
    // the slice run on into the chat's own header-end further down the file.
    expect(start).toBeGreaterThan(-1)
    expect(end).toBeGreaterThan(start)
    const inbox = shell.slice(start, end)
    expect(inbox).toMatch(/<template #header-end>\s*<PortalThemeSwitch \/>\s*<\/template>/)
  })
})
