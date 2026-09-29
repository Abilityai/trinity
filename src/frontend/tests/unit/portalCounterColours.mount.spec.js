// @vitest-environment jsdom
/**
 * trinity-enterprise#610 §3g A3b — one colour per fact, everywhere a count is
 * drawn. The UX review found the SAME "needs you" count in three colours
 * (success-green tab badge, urgent-700 pinned row, urgent-500 agent pill at
 * 2.80:1) and "new" in 700 on one row and 600 on the next.
 *
 *   needs you  white on status-urgent-700   (5.18:1)
 *   new        white on action-primary-700  (7.90:1)
 *
 * Counters are solid; a per-row fact ("3 new" on an Inbox row) is the tinted
 * BaseBadge `primary`. Mounted: the rendered class list is the fact, and each
 * counter must carry exactly one ground colour (#2662).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(async () => ({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(async () => ({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useClientPortalStore } from '@/stores/clientPortal'
import PortalSidebar from '@/components/portal/PortalSidebar.vue'
import PortalChatRow from '@/components/portal/PortalChatRow.vue'
import PortalAgentDetails from '@/components/portal/PortalAgentDetails.vue'
import PortalInbox from '@/components/portal/PortalInbox.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
window.matchMedia = window.matchMedia || ((q) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }))

const NEEDS = 'bg-status-urgent-700'
const NEW = 'bg-action-primary-700'
const grounds = (el) => el.classes().filter((k) => /^(dark:)?bg-/.test(k))

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
  await router.push('/workspace/inbox')
  await router.isReady()
})

const THREADS = [{ id: 't1', session_id: 't1', agent_name: 'scout', title: 'One', last_message_at: new Date().toISOString(), unread: 3 }]
const ASKS = [{ id: 'a1', agent_name: 'scout', status: 'pending', title: 'Ok?', created_at: new Date().toISOString() }]

describe('the sidebar agent row', () => {
  it('its ask pill is urgent-700 and its unread pill primary-700, one ground each', async () => {
    const store = useClientPortalStore()
    store.asks = ASKS
    const w = mount(PortalSidebar, { props: { roster: [{ name: 'scout' }], threads: THREADS }, global: { plugins: [router] } })
    await flushPromises()
    const ask = w.find('[data-testid="agent-ask-count"]')
    const unread = w.find('[data-testid="agent-unread-count"]')
    expect(ask.exists()).toBe(true)
    expect(unread.exists()).toBe(true)
    expect(grounds(ask)).toEqual([NEEDS])
    expect(grounds(unread)).toEqual([NEW])
  })
})

describe('the chat rows', () => {
  it("a sidebar chat row's unread pill is primary-700", () => {
    const w = mount(PortalChatRow, { props: { thread: THREADS[0] }, global: { plugins: [router] } })
    const pill = w.findAll('span').find((s) => s.text() === '3' && s.classes().some((k) => k.startsWith('bg-')))
    expect(pill).toBeTruthy()
    expect(grounds(pill)).toEqual([NEW])
  })

  it("the agent details' chat list pill is primary-700", async () => {
    const w = shallowMount(PortalAgentDetails, {
      props: { agentName: 'scout', agent: { name: 'scout' }, threads: THREADS },
      global: { plugins: [router] },
    })
    await flushPromises()
    const pill = w.findAll('span').find((s) => s.text() === '3' && s.classes().some((k) => k.startsWith('bg-')))
    expect(pill).toBeTruthy()
    expect(grounds(pill)).toEqual([NEW])
  })
})

describe('the Inbox', () => {
  it('its tab counters are solid: Action urgent-700, Unread primary-700', async () => {
    const store = useClientPortalStore()
    store.asks = ASKS
    store.asksLoaded = true
    store.fetchHistory = vi.fn(async () => ({ messages: [] }))
    store.fetchSessionDeliverablesStrict = vi.fn(async () => [])
    const w = mount(PortalInbox, { props: { threads: THREADS, threadsLoaded: true }, global: { plugins: [router] } })
    await flushPromises()
    const tabBadge = (label) => {
      const btn = w.findAll('nav button').find((b) => b.text().startsWith(label) && !b.attributes('data-measure-tab') && b.attributes('tabindex') !== '-1')
      return btn.findAll('span').find((s) => /^\d+$/.test(s.text()))
    }
    expect(grounds(tabBadge('Action'))).toEqual([NEEDS])
    expect(grounds(tabBadge('Unread'))).toEqual([NEW])
  })

  it('a row\'s "N new" is the tinted primary badge — a fact, not a counter', async () => {
    const store = useClientPortalStore()
    store.asksLoaded = true
    store.fetchHistory = vi.fn(async () => ({ messages: [] }))
    store.fetchSessionDeliverablesStrict = vi.fn(async () => [])
    await router.replace({ path: '/workspace/inbox', query: { tab: 'unread' } })
    const w = mount(PortalInbox, { props: { threads: THREADS, threadsLoaded: true }, global: { plugins: [router] } })
    await flushPromises()
    const badge = w.find('[data-testid="inbox-row-new-thread:t1"]')
    expect(badge.classes()).toContain('bg-action-primary-100')
    expect(badge.classes()).toContain('dark:bg-action-primary-500/16')
  })
})
