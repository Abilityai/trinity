// @vitest-environment jsdom
/**
 * #4347 — the Inbox leads with what needs the person, says so in the URL, and
 * does not show the same chat list twice.
 *
 *   landing   a bare `/workspace/inbox` opens on Action when it has items,
 *             else Unread, else All — and writes that tab into the URL once,
 *             so a reload is stable. A tab the URL names is never rewritten.
 *   empty     an empty Action tab names a next action for a client too.
 *   sidebar   while the Inbox is open the sidebar's chat groups fold behind
 *             one "Chats" toggle, so All's rows are not on screen twice; the
 *             person's choice to open them is remembered per browser.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, userEmail: 'me@example.com', logout: vi.fn() }),
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
import PortalInbox from '@/components/portal/PortalInbox.vue'
import PortalSidebar from '@/components/portal/PortalSidebar.vue'
import PortalChatRow from '@/components/portal/PortalChatRow.vue'
import { INBOX_SIDEBAR_CHATS_KEY } from '@/components/portal/portalInbox'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const NOW = Date.now()
const iso = (minsAgo) => new Date(NOW - minsAgo * 60_000).toISOString()
const thread = (id, over = {}) => ({
  id, session_id: id, agent_name: 'scout', is_main: false, title: `Chat ${id}`,
  last_message_at: iso(5), unread: 0, ...over,
})
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'question', priority: 'medium', title: `Ask ${id}`, question: `Ask ${id}?`,
  options: null, created_at: iso(10), expires_at: null, status: 'pending', chat_id: null,
  sync: 'confirmed', aging: false, ended_at: null, ended_by: null, ...over,
})

let store, router, wrapper
beforeEach(async () => {
  window.matchMedia = (q) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })
  localStorage.clear()
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.asksAvailable = true
  store.fetchHistory = vi.fn(async () => ({ messages: [] }))
  store.fetchSessionDeliverablesStrict = vi.fn(async () => [])
  store.fetchAgentReport = vi.fn(async () => ({}))
  store.fetchAsks = vi.fn(async () => store.asks)
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/workspace', component: { template: '<div />' } },
      { path: '/workspace/inbox', component: { template: '<div />' } },
      { path: '/workspace/c/:id', component: { template: '<div />' } },
    ],
  })
  await router.push('/workspace/inbox')
  await router.isReady()
})
afterEach(() => { wrapper?.unmount(); wrapper = null; document.body.innerHTML = '' })

async function mountInbox({ threads = [], asks = [], query = null, isPlatform = false } = {}) {
  if (query) { await router.replace({ path: '/workspace/inbox', query }); await flushPromises() }
  store.asks = asks
  store.asksLoaded = true
  wrapper = mount(PortalInbox, {
    props: { threads, previews: {}, threadsLoaded: true, threadsFailed: false, labels: {}, isPlatform },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}
const urlTab = () => router.currentRoute.value.query.tab

describe('#4347 — a bare Inbox URL states the tab it chose', () => {
  it('Action when something waits on the person', async () => {
    await mountInbox({ threads: [thread('t1', { unread: 2 })], asks: [ask('a1')] })
    expect(urlTab()).toBe('action')
  })

  it('Unread when nothing waits but something came back', async () => {
    await mountInbox({ threads: [thread('t1', { unread: 2 })] })
    expect(urlTab()).toBe('unread')
  })

  it('All when there is neither', async () => {
    await mountInbox({ threads: [thread('t1')] })
    expect(urlTab()).toBe('all')
  })

  it('never rewrites a tab the URL names', async () => {
    await mountInbox({ threads: [thread('t1', { unread: 2 })], asks: [ask('a1')], query: { tab: 'all' } })
    expect(urlTab()).toBe('all')
  })
})

describe('#4347 — an empty Action tab names a next action', () => {
  it('a client with nothing back is offered a new chat', async () => {
    await mountInbox({ threads: [thread('t1')], query: { tab: 'action' } })
    const link = wrapper.get('[data-testid="inbox-empty-link"]')
    expect(link.text()).toBe('Start a chat')
  })

  it('a client with replies waiting is pointed at them', async () => {
    await mountInbox({ threads: [thread('t1', { unread: 1 })], query: { tab: 'action' } })
    expect(wrapper.get('[data-testid="inbox-empty-link"]').text()).toBe('See what came back')
  })
})

describe('#4347 — the sidebar folds its chats while the Inbox is open', () => {
  const THREADS = [thread('t1'), thread('t2'), thread('t3')]
  const side = (props) => mount(PortalSidebar, {
    props: { roster: [{ name: 'scout', playbooks: [] }], threads: THREADS, loadingRoster: false, ...props },
    global: { plugins: [router] },
  })
  const rows = (w) => w.findAllComponents(PortalChatRow).length

  it('folds the chat groups behind one toggle on the Inbox', async () => {
    const w = side({ inboxActive: true })
    const toggle = w.get('[data-testid="sidebar-chats-toggle"]')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.text()).toContain('3')
    expect(rows(w)).toBe(0)
    w.unmount()
  })

  it('shows them as before anywhere else, with no toggle', () => {
    const w = side({ inboxActive: false })
    expect(w.find('[data-testid="sidebar-chats-toggle"]').exists()).toBe(false)
    expect(rows(w)).toBe(3)
    w.unmount()
  })

  it('opening them is remembered for this browser', async () => {
    const w = side({ inboxActive: true })
    await w.get('[data-testid="sidebar-chats-toggle"]').trigger('click')
    expect(rows(w)).toBe(3)
    expect(localStorage.getItem(INBOX_SIDEBAR_CHATS_KEY)).toBe('open')
    w.unmount()
    const again = side({ inboxActive: true })
    expect(again.get('[data-testid="sidebar-chats-toggle"]').attributes('aria-expanded')).toBe('true')
    expect(rows(again)).toBe(3)
    again.unmount()
  })
})
