// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A2 — the 2026-09-30 ruling, item 3 (the rework's
 * item 4): an agent row in the sidebar carries a "needs you" mark beside its
 * unread mark, counted from the SAME feed the pinned Inbox row counts —
 * pending asks addressed to you, and nothing else.
 *
 * The pill itself predates the ruling (#2424, coloured by §3g A3b). What this
 * pins is the ruling's contract on it:
 *   - one feed: the rows' marks sum to the Inbox row's number, for any mix of
 *     chat-turn and background asks (both need you; only their homes differ);
 *   - pending only: an ask that ended leaves both at once;
 *   - it sits beside the unread mark (ask mark, then unread mark, same row);
 *   - it names itself on hover the way the Inbox row's does ("N asks are
 *     waiting on your answer"), so the two marks read as one fact.
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
import PortalSidebar from '@/components/portal/PortalSidebar.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
window.matchMedia = window.matchMedia || ((q) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }))

const ask = (id, agent, over = {}) => ({
  id, agent_name: agent, kind: 'question', status: 'pending', title: id, created_at: '2026-09-30T10:00:00Z',
  chat_id: 'main', raised_in_turn: false, ...over,
})
const THREADS = [
  { id: 't1', session_id: 't1', agent_name: 'scout', title: 'One', last_message_at: '2026-09-30T10:00:00Z', unread: 2 },
]

let router
let store
beforeEach(async () => {
  setActivePinia(createPinia())
  store = useClientPortalStore()
  router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:p(.*)*', component: { template: '<div />' } }],
  })
  await router.push('/workspace')
  await router.isReady()
})

async function sidebar() {
  const w = mount(PortalSidebar, {
    props: { roster: [{ name: 'scout' }, { name: 'bard' }, { name: 'quiet' }], threads: THREADS },
    global: { plugins: [router] },
  })
  await flushPromises()
  return w
}
const rowOf = (w, name) => w.findAll('button').find((b) => b.attributes('title')?.includes(name) && b.find('[data-testid="agent-unread-count"], [data-testid="agent-ask-count"]').exists())
const markOf = (w, name) => rowOf(w, name)?.find('[data-testid="agent-ask-count"]')

describe('the needs-you mark on agent rows', () => {
  it('sums to the pinned Inbox row, chat-turn and background asks alike; ended asks count nowhere', async () => {
    store.asks = [
      ask('a1', 'scout'),
      ask('a2', 'scout', { raised_in_turn: true, chat_id: 's1' }),
      ask('b1', 'bard', { raised_in_turn: true, chat_id: 's9' }),
      ask('done', 'bard', { status: 'answered' }),
      ask('gone', 'quiet', { status: 'expired' }),
    ]
    const w = await sidebar()
    const inbox = Number(w.find('[data-testid="sidebar-ask-count"]').text())
    const marks = w.findAll('[data-testid="agent-ask-count"]').map((m) => Number(m.text()))
    expect(inbox).toBe(3)
    expect(marks.reduce((x, y) => x + y, 0)).toBe(inbox)
    expect(markOf(w, 'scout').text()).toBe('2')
    expect(markOf(w, 'bard').text()).toBe('1')
    expect(w.findAll('[data-testid="agent-ask-count"]')).toHaveLength(2)
  })

  it('an ask answered leaves the row mark and the Inbox row together', async () => {
    store.asks = [ask('a1', 'bard')]
    const w = await sidebar()
    expect(w.findAll('[data-testid="agent-ask-count"]')).toHaveLength(1)
    store.asks = [ask('a1', 'bard', { status: 'answered' })]
    await flushPromises()
    expect(w.findAll('[data-testid="agent-ask-count"]')).toHaveLength(0)
    expect(w.find('[data-testid="sidebar-ask-count"]').exists()).toBe(false)
  })

  it('sits beside the unread mark, ask first, and names itself like the Inbox row', async () => {
    store.asks = [ask('a1', 'scout'), ask('a2', 'scout')]
    const w = await sidebar()
    const row = rowOf(w, 'scout')
    const pills = row.findAll('[data-testid="agent-ask-count"], [data-testid="agent-unread-count"]')
    expect(pills.map((p) => p.attributes('data-testid'))).toEqual(['agent-ask-count', 'agent-unread-count'])
    expect(pills[0].attributes('title')).toBe('2 asks are waiting on your answer')
    expect(pills[0].attributes('title')).toBe(w.find('[data-testid="sidebar-ask-count"]').attributes('title'))
  })
})
