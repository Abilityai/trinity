// @vitest-environment jsdom
/**
 * trinity-enterprise#610 §3g B7a — the sidebar's meta ink clears AA.
 *
 * Ten text sites in PortalSidebar were a bare `text-gray-400`: 2.54:1 on white
 * (the contract: "gray-400 is not text"), and no dark half at all. They now
 * share one `META_INK` — gray-500 light (4.83:1), gray-400 dark (6.99:1) — so
 * the next site cannot drift. Mounted: every rendered TEXT element carrying
 * gray-400 must be that pair (icons are exempt — they are decoration).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalSidebar from '@/components/portal/PortalSidebar.vue'

let router
beforeEach(async () => {
  setActivePinia(createPinia())
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div />' } }] })
  await router.push('/workspace')
  await router.isReady()
})

const now = Date.now()
const THREADS = [
  { id: 't1', agent_name: 'scout', title: 'Starred one', starred: true, last_message_at: new Date(now - 60_000).toISOString(), unread: 0 },
  { id: 't2', agent_name: 'scout', title: 'Older', last_message_at: new Date(now - 3 * 86_400_000).toISOString(), unread: 0 },
]

describe('PortalSidebar meta ink (B7a)', () => {
  it('no text element renders a bare light gray-400', async () => {
    const w = mount(PortalSidebar, {
      props: { roster: [{ name: 'scout', display_label: 'Scout' }], threads: THREADS, clientEmail: 'me@example.com' },
      global: { plugins: [router] },
    })
    await flushPromises()
    const offenders = w.findAll('.text-gray-400')
      .filter((el) => el.element.tagName.toLowerCase() !== 'svg' && el.element.tagName.toLowerCase() !== 'button')
      .map((el) => `${el.element.tagName.toLowerCase()} "${el.text().slice(0, 30)}"`)
    expect(offenders).toEqual([])
    const meta = w.findAll('.dark\\:text-gray-400').filter((el) => el.classes().includes('text-gray-500'))
    expect(meta.length).toBeGreaterThanOrEqual(4) // Agents header, the agent's slug, Starred, a date group, Signed in
  })
})
