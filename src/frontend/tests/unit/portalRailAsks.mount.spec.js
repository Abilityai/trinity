// @vitest-environment jsdom
/**
 * trinity-enterprise#836 — the rail's Asks tab and the calmer "needs you"
 * mark, mounted (#2918: a predicate behind a link or a store read is proven on
 * the rendered DOM, never by a regex over the source).
 *
 *   - the body lists the agent's open asks in the Inbox's order, each row a
 *     link to the Inbox on Action, filtered to the agent, with that ask
 *     selected; a room groups by participant with absence visible;
 *   - the sidebar's agent-row mark is an unfilled ring — no ground, urgent ink
 *     — and keeps its count, its hover words and its spoken name;
 *   - the rail's open strip draws the Asks tab with the SAME mark and the SAME
 *     number the sidebar row shows, named in words for a screen reader.
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
import PortalRailAsks from '@/components/portal/PortalRailAsks.vue'
import PortalSidebar from '@/components/portal/PortalSidebar.vue'
import PortalRail from '@/components/portal/PortalRail.vue'
import { RAIL_TABS, visibleTabs } from '@/components/portal/portalRail'
import { railAskItems, asksSignalFrom } from '@/components/portal/portalRailAsks'
import { asksByAgent } from '@/components/portal/portalUtils'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
window.matchMedia = window.matchMedia || ((q) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }))

const ask = (id, agent, over = {}) => ({
  id, agent_name: agent, kind: 'question', status: 'pending', title: `Ask ${id}`,
  created_at: '2026-10-07T10:00:00Z', chat_id: 'main', raised_in_turn: false, ...over,
})
const ASKS = [
  ask('a1', 'scout', { created_at: '2026-10-07T09:00:00Z' }),
  ask('a2', 'scout', { kind: 'approval', priority: 'high' }),
  ask('b1', 'bard', { kind: 'alert' }),
  ask('done', 'scout', { status: 'answered' }),
]
const THREADS = [
  { id: 't1', session_id: 't1', agent_name: 'scout', title: 'One', last_message_at: '2026-10-07T10:00:00Z', unread: 2 },
]

let router
let store
beforeEach(async () => {
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.asks = ASKS
  store.agents = [{ name: 'scout', display_label: 'Scout' }, { name: 'bard' }, { name: 'quiet' }]
  router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:p(.*)*', component: { template: '<div />' } }],
  })
  await router.push('/workspace/c/s1')
  await router.isReady()
})

const hrefOf = (link) => decodeURIComponent(link.attributes('href') || '')

describe('the Asks tab body', () => {
  it('lists the agent\'s open asks in Action order, each a link into the Inbox at that ask', async () => {
    const w = mount(PortalRailAsks, { props: { participants: ['scout'] }, global: { plugins: [router] } })
    await flushPromises()
    const rows = w.findAll('a[data-testid^="portal-rail-ask-"]')
    const expected = railAskItems(ASKS, ['scout'])
    expect(rows.map((r) => r.attributes('data-testid'))).toEqual(expected.map((it) => `portal-rail-ask-${it.id}`))
    expect(rows).toHaveLength(2)                                   // pending only
    expect(rows[0].text()).toContain(expected[0].title)             // the start of the ask
    for (const [i, row] of rows.entries()) {
      const href = hrefOf(row)
      expect(href).toContain('/workspace/inbox?')
      expect(href).toContain('tab=action')
      expect(href).toContain('from=scout')
      expect(href).toContain(`item=ask:${expected[i].id}`)
      expect(href).not.toContain('agent=')
    }
    // The head is the door every other surface carries: the count, and Open in Inbox.
    expect(w.find('[data-testid="portal-rail-asks-waiting-line"]').text()).toContain('2 asks waiting on you')
    expect(hrefOf(w.find('[data-testid="portal-rail-asks-open-inbox"]'))).toContain('from=scout')
    // Kind by shape, spoken: an approval row says so.
    expect(w.find('[data-testid="portal-rail-ask-a2"]').text()).toMatch(/approval/i)
    // No answer control anywhere: a door, not a second answer surface.
    expect(w.findAll('button, textarea, select')).toHaveLength(0)
    expect(w.findAll('[data-testid^="portal-rail-asks-group-"]')).toHaveLength(0)
  })

  it('emits open on a row (the sheet closes), and follows the feed when an ask ends', async () => {
    const w = mount(PortalRailAsks, { props: { participants: ['scout'] }, global: { plugins: [router] } })
    await flushPromises()
    await w.find('[data-testid="portal-rail-ask-a1"]').trigger('click')
    expect(w.emitted('open')).toHaveLength(1)
    expect(w.emitted('open')[0][0]).toMatchObject({ id: 'a1', agent_name: 'scout' })
    store.asks = ASKS.map((a) => (a.id === 'a1' ? { ...a, status: 'answered' } : a))
    await flushPromises()
    expect(w.findAll('a[data-testid^="portal-rail-ask-"]').map((r) => r.attributes('data-testid'))).toEqual(['portal-rail-ask-a2'])
  })

  it('a room groups by participant, absence visible, nobody else listed', async () => {
    const w = mount(PortalRailAsks, { props: { participants: ['quiet', 'scout', 'bard'] }, global: { plugins: [router] } })
    await flushPromises()
    const groups = w.findAll('[data-testid^="portal-rail-asks-group-"]')
    expect(groups.map((g) => g.attributes('data-testid'))).toEqual([
      'portal-rail-asks-group-quiet', 'portal-rail-asks-group-scout', 'portal-rail-asks-group-bard',
    ])
    expect(groups[0].text()).toContain('nothing waiting')
    expect(groups[1].text()).toContain('Scout')                 // the roster's display label
    expect(groups[1].text()).not.toContain('nothing waiting')
    expect(w.findAll('a[data-testid^="portal-rail-ask-"]')).toHaveLength(3)
    expect(hrefOf(w.find('[data-testid="portal-rail-ask-b1"]'))).toContain('from=bard')
  })
})

async function sidebar() {
  const w = mount(PortalSidebar, {
    props: { roster: [{ name: 'scout' }, { name: 'bard' }], threads: THREADS },
    global: { plugins: [router] },
  })
  await flushPromises()
  return w
}
const RING = ['ring-1', 'ring-inset', 'ring-status-urgent-600', 'dark:ring-status-urgent-400', 'text-status-urgent-700', 'dark:text-status-urgent-400']

describe('the calmer "needs you" mark on the agent row', () => {
  it('is an unfilled ring in urgent ink — no ground — and keeps its count, its hover words and its spoken name', async () => {
    const w = await sidebar()
    const mark = w.find('[data-testid="agent-ask-count"]')
    expect(mark.exists()).toBe(true)
    expect(mark.text()).toBe('2')
    expect(mark.classes().filter((k) => /^(dark:)?bg-/.test(k))).toEqual([])
    expect(mark.classes()).toEqual(expect.arrayContaining(RING))
    expect(mark.classes()).not.toContain('text-white')
    expect(mark.attributes('title')).toBe('2 asks are waiting on your answer')
    expect(mark.attributes('aria-hidden')).toBe('true')
    const row = mark.element.closest('button')
    expect(Array.from(row.querySelectorAll('.sr-only')).map((n) => n.textContent)).toContain('2 asks are waiting on your answer')
    // The unread mark beside it stays a FILLED pill: two obligations, two shapes.
    const unread = w.find('[data-testid="agent-unread-count"]')
    expect(unread.classes()).toContain('bg-action-primary-700')
    // The pinned Inbox row keeps its filled pill — only the agent rows calmed.
    expect(w.find('[data-testid="sidebar-ask-count"]').classes()).toContain('bg-status-urgent-700')
  })
})

describe('the rail\'s open strip', () => {
  it('draws the Asks tab with the same mark and the same number as the agent row, named in words', async () => {
    const counts = asksByAgent(store.openAsks)
    const tabs = visibleTabs(RAIL_TABS, { isPlatform: true, participants: ['scout'], askCounts: counts })
    expect(tabs.map((t) => t.id)).toContain('asks')
    const w = mount(PortalRail, {
      props: { tabs, activeTab: 'asks', open: true, participants: ['scout'], signals: { asks: asksSignalFrom(counts, ['scout']) } },
      global: { plugins: [router] },
    })
    await flushPromises()
    // The visible row's buttons (the mirror row's carry data-measure-tab).
    const tab = w.findAll('nav button:not([data-measure-tab]):not([data-measure-more]):not([data-overflow-trigger])')
      .find((t) => t.text().startsWith('Asks'))
    expect(tab).toBeTruthy()
    expect(tab.attributes('aria-label')).toBe('Asks, 2 asks')
    const badge = tab.findAll('span').find((s) => s.text() === '2')
    expect(badge).toBeTruthy()
    expect(badge.classes()).toEqual(expect.arrayContaining(RING))
    expect(badge.classes().filter((k) => /^(dark:)?bg-/.test(k))).toEqual([])
    // The same number the sidebar row shows — one feed, by construction.
    const side = await sidebar()
    expect(side.find('[data-testid="agent-ask-count"]').text()).toBe(badge.text())
  })

  it('the collapsed strip says it in words on the icon, and ⌥. can reach it', async () => {
    const counts = asksByAgent(store.openAsks)
    const tabs = visibleTabs(RAIL_TABS, { isPlatform: true, participants: ['scout'], askCounts: counts })
    const w = mount(PortalRail, {
      props: { tabs, activeTab: 'work', open: false, participants: ['scout'], signals: { asks: asksSignalFrom(counts, ['scout']) } },
      global: { plugins: [router] },
    })
    await flushPromises()
    const btn = w.find('[data-testid="portal-rail-tab-asks"]')
    expect(btn.exists()).toBe(true)
    expect(btn.attributes('aria-label')).toBe('Open Asks · 2 asks')
    expect(btn.attributes('aria-keyshortcuts')).toBeTruthy()
    await btn.trigger('click')
    expect(w.emitted('update:activeTab')[0]).toEqual(['asks'])
    expect(w.emitted('update:open')[0]).toEqual([true])
  })
})
