// @vitest-environment jsdom
/**
 * trinity-enterprise#610 (D13) — the sidebar's pinned Inbox row, MOUNTED.
 *
 * Its two counts are the SAME numbers the rest of the Workspace shows —
 * `needs` = pending asks (`askCount`), `came` = `totalUnread` over the threads
 * the sidebar renders — so the row and the agent rows never disagree (AC 1).
 * The ask badge keeps its `sidebar-ask-count` address (the existing specs and
 * e2e use it); the unread badge gains `sidebar-unread-count`; the row links to
 * the Inbox, and so does the brand mark.
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

import { useClientPortalStore } from '@/stores/clientPortal'
import PortalSidebar from '@/components/portal/PortalSidebar.vue'
import { inboxCounts } from '@/components/portal/portalInbox'
import { unreadByAgent } from '@/components/portal/portalUtils'
import { WORKSPACE_INBOX } from '@/components/portal/portalUtils'

const THREADS = [
  { id: 't1', agent_name: 'scout', last_message_at: '2026-09-27T10:00:00Z', unread: 2 },
  { id: 't2', agent_name: 'sage', last_message_at: '2026-09-27T09:00:00Z', unread: 3, archived_at: '2026-09-27T09:30:00Z' },
  { id: 't3', agent_name: 'sage', last_message_at: '2026-09-26T09:00:00Z', unread: 0 },
]
const ASKS = [
  { id: 'a1', agent_name: 'scout', status: 'pending' },
  { id: 'a2', agent_name: 'sage', status: 'pending' },
  { id: 'a3', agent_name: 'sage', status: 'answered' },
]

let router
beforeEach(async () => {
  setActivePinia(createPinia())
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/workspace', component: { template: '<div />' } },
      { path: '/workspace/inbox', component: { template: '<div />' } },
    ],
  })
  await router.push('/workspace')
  await router.isReady()
})

async function mountSidebar() {
  const store = useClientPortalStore()
  store.asks = ASKS
  const w = mount(PortalSidebar, {
    props: { roster: [{ name: 'scout' }, { name: 'sage' }], threads: THREADS },
    global: { plugins: [router] },
  })
  await flushPromises()
  return { w, store }
}

describe('the pinned Inbox row', () => {
  it('shows the counts inboxCounts computes over the same threads and asks', async () => {
    const { w, store } = await mountSidebar()
    const expected = inboxCounts(THREADS, store.openAsks)
    expect(expected).toEqual({ needs: 2, came: 5 })
    const row = w.find('[data-testid="sidebar-inbox"]')
    expect(row.find('[data-testid="sidebar-ask-count"]').text()).toBe(String(expected.needs))
    expect(row.find('[data-testid="sidebar-unread-count"]').text()).toBe(String(expected.came))
    // …and they agree with the agent rows' own per-agent sums (AC 1).
    const perAgent = Object.values(unreadByAgent(THREADS)).reduce((a, b) => a + b, 0)
    expect(perAgent).toBe(expected.came)
  })

  it('links to the Inbox, and the brand mark does too', async () => {
    const { w } = await mountSidebar()
    expect(w.find('[data-testid="sidebar-inbox"]').attributes('href')).toBe(WORKSPACE_INBOX)
    expect(w.findAll(`a[href="${WORKSPACE_INBOX}"]`).length).toBeGreaterThanOrEqual(2)
  })

  it('is marked current on the Inbox route only', async () => {
    const { w } = await mountSidebar()
    expect(w.find('[data-testid="sidebar-inbox"]').attributes('aria-current')).toBeUndefined()
    await router.push('/workspace/inbox')
    await flushPromises()
    expect(w.find('[data-testid="sidebar-inbox"]').attributes('aria-current')).toBe('page')
  })

  it('renders no badge at zero, and white ink only on a 700 ground (#2201)', async () => {
    setActivePinia(createPinia())
    const store = useClientPortalStore()
    store.asks = []
    const w = mount(PortalSidebar, {
      props: { roster: [{ name: 'scout' }], threads: [{ id: 't1', agent_name: 'scout', unread: 0, last_message_at: 'x' }] },
      global: { plugins: [router] },
    })
    await flushPromises()
    expect(w.find('[data-testid="sidebar-ask-count"]').exists()).toBe(false)
    expect(w.find('[data-testid="sidebar-unread-count"]').exists()).toBe(false)
    const { w: w2 } = await mountSidebar()
    for (const id of ['sidebar-ask-count', 'sidebar-unread-count']) {
      const cls = w2.find(`[data-testid="${id}"]`).attributes('class')
      expect(cls).toContain('text-white')
      expect(cls).toMatch(/bg-[a-z-]+-700\b/)
    }
  })
})
