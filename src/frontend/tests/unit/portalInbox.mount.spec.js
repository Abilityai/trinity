// @vitest-environment jsdom
/**
 * trinity-enterprise#610 — the Inbox container, MOUNTED (design contract
 * #2918: a predicate that gates a store write or a keyboard contract is proven
 * by a mount, not a regex).
 *
 *   honest state   the empty copy only after a read SUCCEEDED; `LoadFailed` on a
 *                  first-load failure; the stale banner beside data a refresh
 *                  failed to replace
 *   reading        opening a chat row emits `mark-read` exactly once, and the row
 *                  stays selected, drawn read, in place (principle 5); an
 *                  AUTO-selection on landing is not a read
 *   answering      goes through `store.answerAsk`, and the ended ask stays drawn
 *                  in place until the selection changes (D8)
 *   Mark all read  settles through `markChatReadStrict` and names the failed count
 *   phone          Back returns to the list and focus returns to the row (D12)
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
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
import PortalInbox from '@/components/portal/PortalInbox.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

let phone = false
function stubMatchMedia() {
  window.matchMedia = (q) => ({
    matches: phone, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  })
}

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
  phone = false
  stubMatchMedia()
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
      { path: '/workspace/inbox', component: { template: '<div />' } },
      { path: '/workspace/c/:id', component: { template: '<div />' } },
      { path: '/operations', component: { template: '<div />' } },
    ],
  })
  await router.push('/workspace/inbox')
  await router.isReady()
})
afterEach(() => { wrapper?.unmount(); wrapper = null; document.body.innerHTML = '' })

async function mountInbox(props = {}, { query } = {}) {
  if (query) { await router.replace({ path: '/workspace/inbox', query }); await flushPromises() }
  wrapper = mount(PortalInbox, {
    props: { threads: [], previews: {}, threadsLoaded: true, threadsFailed: false, labels: {}, ...props },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}
const has = (w, id) => w.find(`[data-testid="${id}"]`).exists()

describe('honest state — loading ≠ empty ≠ failed (principle 15)', () => {
  it('Action shows no empty copy before the asks read has a verdict', async () => {
    const w = await mountInbox({}, { query: { tab: 'action' } })
    expect(has(w, 'inbox-list-loading')).toBe(true)
    expect(has(w, 'inbox-empty')).toBe(false)
    store.asksLoaded = true
    await flushPromises()
    expect(has(w, 'inbox-empty')).toBe(true)
  })

  it('Unread shows no empty copy until the thread list has loaded', async () => {
    const w = await mountInbox({ threadsLoaded: false }, { query: { tab: 'unread' } })
    expect(has(w, 'inbox-empty')).toBe(false)
    expect(has(w, 'inbox-list-loading')).toBe(true)
    await w.setProps({ threadsLoaded: true })
    expect(has(w, 'inbox-empty')).toBe(true)
  })

  it('a first-load failure renders LoadFailed, never the empty copy', async () => {
    store.asksFailed = true
    const w = await mountInbox({}, { query: { tab: 'action' } })
    expect(has(w, 'inbox-list-failed')).toBe(true)
    expect(has(w, 'inbox-empty')).toBe(false)
  })

  it('a failed refresh with data on screen keeps the rows and raises the stale banner', async () => {
    store.asks = [ask('q1')]
    store.asksLoaded = true
    store.asksLoadedAt = NOW - 60_000
    store.asksFailed = true
    const w = await mountInbox({}, { query: { tab: 'action' } })
    expect(has(w, 'inbox-stale')).toBe(true)
    expect(has(w, 'inbox-row-ask:q1')).toBe(true)
  })

  it("a platform session's empty Action points at Operations (A9); a client's does not", async () => {
    store.asksLoaded = true
    const w = await mountInbox({ isPlatform: true }, { query: { tab: 'action' } })
    expect(has(w, 'inbox-empty-link')).toBe(true)
    await w.setProps({ isPlatform: false })
    expect(has(w, 'inbox-empty-link')).toBe(false)
  })
})

describe('reading a chat (D11)', () => {
  it('opening a chat row marks it read exactly once, and the row stays selected, drawn read', async () => {
    const threads = [thread('t1', { unread: 2, last_message_at: iso(1) }), thread('t2', { unread: 3, last_message_at: iso(20) })]
    const w = await mountInbox({ threads }, { query: { tab: 'unread' } })
    // Desktop auto-selects the first row — and that is NOT a read.
    expect(router.currentRoute.value.query.item).toBe('thread:t1')
    expect(w.emitted('mark-read')).toBeUndefined()

    await w.find('[data-testid="inbox-row-thread:t2"]').trigger('click')
    await flushPromises()
    expect(w.emitted('mark-read')).toEqual([['thread', 't2']])
    expect(router.currentRoute.value.query.item).toBe('thread:t2')

    // The shell zeroes the count: the row leaves Unread's membership but stays
    // where it was, drawn read, while it is selected.
    await w.setProps({ threads: [threads[0], { ...threads[1], unread: 0 }] })
    const row = w.find('[data-testid="inbox-row-thread:t2"]')
    expect(row.exists()).toBe(true)
    expect(row.attributes('aria-current')).toBe('true')
    expect(has(w, 'inbox-row-read-thread:t2')).toBe(true)
    expect(w.emitted('mark-read')).toHaveLength(1)
  })

  it('the pane reads the chat once, with the bounded history window', async () => {
    const threads = [thread('t1', { unread: 1 })]
    await mountInbox({ threads, previews: { 'thread:t1': { latest: null, first_unread_message_id: 'm9' } } }, { query: { tab: 'unread' } })
    expect(store.fetchHistory).toHaveBeenCalledTimes(1)
    expect(store.fetchHistory).toHaveBeenCalledWith('scout', 't1', { limit: 50 })
  })

  it('on All, the pane keeps what was new after the read refreshes the previews away (principle 5)', async () => {
    const msgs = Array.from({ length: 8 }, (_, i) => ({ id: `m${i + 1}`, role: i % 2 ? 'assistant' : 'user', content: `c${i + 1}` }))
    store.fetchHistory = vi.fn(async () => ({ messages: msgs }))
    store.asksLoaded = true
    const threads = [thread('t1', { unread: 2, last_message_at: iso(1) }), thread('t2', { last_message_at: iso(30) })]
    const previews = { 'thread:t1': { latest: { kind: 'message', id: 'm8', at: iso(1), excerpt: 'c8' }, first_unread_message_id: 'm2' } }
    const w = await mountInbox({ threads, previews }, { query: { tab: 'all' } })
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    const shown = () => w.findAll('[data-testid^="inbox-pane-message-"]').map((x) => x.attributes('data-testid'))
    const before = shown()
    expect(before[0]).toBe('inbox-pane-message-m2')
    await w.find('[data-testid="inbox-pane-open"]').trigger('click')
    const href = w.emitted('open-chat')[0][0]
    expect(href).toContain('m%3Am2')

    // The read lands and the next refresh carries no preview for t1: still on All.
    await w.setProps({ threads: [{ ...threads[0], unread: 0 }, threads[1]], previews: {} })
    await flushPromises()
    expect(shown()).toEqual(before)
    await w.find('[data-testid="inbox-pane-open"]').trigger('click')
    expect(w.emitted('open-chat')[1][0]).toBe(href)

    // Selecting another chat releases the hold.
    await w.find('[data-testid="inbox-row-thread:t2"]').trigger('click')
    await flushPromises()
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    await w.find('[data-testid="inbox-pane-open"]').trigger('click')
    expect(w.emitted('open-chat').at(-1)[0]).toBe('/workspace/c/t1')
  })

  it('a failed deliverables read is LoadFailed in the pane, not "no deliverables"', async () => {
    store.fetchSessionDeliverablesStrict = vi.fn(async () => { throw new Error('500') })
    const w = await mountInbox({ threads: [thread('t1', { unread: 1 })] }, { query: { tab: 'unread' } })
    expect(has(w, 'inbox-pane-failed')).toBe(true)
    expect(has(w, 'inbox-pane-empty')).toBe(false)
  })
})

describe('answering an ask in the pane (D8)', () => {
  it('goes through store.answerAsk, and the ended ask stays in place until the selection changes', async () => {
    store.asks = [ask('q1')]
    store.asksLoaded = true
    store.answerAsk = vi.fn(async (id) => {
      const answered = { ...store.asks.find((a) => a.id === id), status: 'answered', ended_by: 'you', ended_at: new Date().toISOString() }
      store.asks = store.asks.map((a) => (a.id === id ? answered : a))
      return answered
    })
    const w = await mountInbox({}, { query: { tab: 'action' } })
    expect(router.currentRoute.value.query.item).toBe('ask:q1')

    await w.find('[data-testid="inbox-ask-input-q1"]').setValue('yes, go')
    await w.find('[data-testid="inbox-ask-input-q1"]').element.form.dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(store.answerAsk).toHaveBeenCalledWith('q1', expect.anything())

    // No longer pending → no longer Action's membership, but still drawn, ended.
    expect(has(w, 'inbox-row-ask:q1')).toBe(true)
    expect(has(w, 'inbox-row-ended-ask:q1')).toBe(true)

    // The selection moves on → it leaves.
    await router.replace({ path: '/workspace/inbox', query: { tab: 'action' } })
    await flushPromises()
    expect(has(w, 'inbox-row-ask:q1')).toBe(false)
  })
})

describe('a selection restored from the URL (D8)', () => {
  it('a deep-linked ask is held in place once answered, exactly as a clicked one is', async () => {
    store.asks = [ask('a1', { created_at: iso(1) }), ask('a2', { created_at: iso(20) })]
    store.asksLoaded = true
    store.answerAsk = vi.fn(async (id) => {
      const answered = { ...store.asks.find((a) => a.id === id), status: 'answered', ended_by: 'you', ended_at: new Date().toISOString() }
      store.asks = store.asks.map((a) => (a.id === id ? answered : a))
      return answered
    })
    // Reload / deep link: the URL names the item, nobody clicked it.
    const w = await mountInbox({}, { query: { tab: 'action', item: 'ask:a2' } })
    await w.find('[data-testid="inbox-ask-input-a2"]').setValue('yes')
    await w.find('[data-testid="inbox-ask-input-a2"]').element.form.dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(store.answerAsk).toHaveBeenCalledWith('a2', expect.anything())
    expect(has(w, 'inbox-row-ask:a2')).toBe(true)
    expect(has(w, 'inbox-row-ended-ask:a2')).toBe(true)
    // …and it keeps its place: second, where the reader last saw it.
    const keys = w.findAll('[data-inbox-row]').map((r) => r.attributes('data-inbox-row'))
    expect(keys).toEqual(['ask:a1', 'ask:a2'])
  })
})

describe('Mark all read (D11)', () => {
  it('settles through markChatReadStrict and names how many failed', async () => {
    store.markChatReadStrict = vi.fn(async (_k, id) => { if (id === 't2') throw new Error('500') })
    const threads = [thread('t1', { unread: 2 }), thread('t2', { unread: 1, last_message_at: iso(9) })]
    const w = await mountInbox({ threads }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-mark-all-read"]').trigger('click')
    await flushPromises()
    expect(store.markChatReadStrict.mock.calls.map((c) => c[1]).sort()).toEqual(['t1', 't2'])
    expect(w.find('[data-testid="inbox-mark-all-error"]').text()).toContain('1 of 2 chats')
    expect(w.emitted('refresh')).toHaveLength(1)
  })
})

describe('phone (D12)', () => {
  it('nothing is auto-selected; Back returns to the list and focus to the row', async () => {
    phone = true
    stubMatchMedia()
    const threads = [thread('t1', { unread: 2 })]
    const w = await mountInbox({ threads }, { query: { tab: 'unread' } })
    expect(router.currentRoute.value.query.item).toBeUndefined()

    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    expect(document.activeElement?.getAttribute('data-testid')).toBe('inbox-pane-heading')

    await w.find('[data-testid="inbox-pane-back"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.item).toBeUndefined()
    expect(document.activeElement?.getAttribute('data-inbox-row')).toBe('thread:t1')
  })

  // jsdom ignores Tailwind's `hidden`, so "focus landed" is not enough: a real
  // browser drops focus() on a display:none element. Record, at the moment of
  // each focus() call, whether the target sat under a `hidden` list column.
  it('Back focuses the row only once the list column is shown again', async () => {
    phone = true
    stubMatchMedia()
    const calls = []
    const orig = HTMLElement.prototype.focus
    const spy = vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(function (...a) {
      calls.push({ row: this.getAttribute('data-inbox-row'), hidden: !!this.closest('.hidden') })
      return orig.apply(this, a)
    })
    try {
      const w = await mountInbox({ threads: [thread('t1', { unread: 2 })] }, { query: { tab: 'unread' } })
      await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
      await flushPromises()
      calls.length = 0
      await w.find('[data-testid="inbox-pane-back"]').trigger('click')
      await flushPromises()
      const rowCalls = calls.filter((c) => c.row === 'thread:t1')
      expect(rowCalls.length).toBeGreaterThan(0)
      expect(rowCalls.every((c) => !c.hidden)).toBe(true)
    } finally { spy.mockRestore() }
  })

  it("Back after reading the chat off Unread focuses the row now in its place, else the list", async () => {
    phone = true
    stubMatchMedia()
    const two = [thread('t1', { unread: 2 }), thread('t2', { unread: 1, last_message_at: iso(9) })]
    const w = await mountInbox({ threads: two }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    // The shell's read lands: t1 has nothing new any more.
    await w.setProps({ threads: [thread('t1', { unread: 0 }), two[1]] })
    await w.find('[data-testid="inbox-pane-back"]').trigger('click')
    await flushPromises()
    expect(document.activeElement?.getAttribute('data-inbox-row')).toBe('thread:t2')

    await w.find('[data-testid="inbox-row-thread:t2"]').trigger('click')
    await flushPromises()
    await w.setProps({ threads: [thread('t1', { unread: 0 }), thread('t2', { unread: 0, last_message_at: iso(9) })] })
    await w.find('[data-testid="inbox-pane-back"]').trigger('click')
    await flushPromises()
    expect(document.activeElement?.getAttribute('data-testid')).toBe('inbox-list-column')
  })

  it('Esc goes back too', async () => {
    phone = true
    stubMatchMedia()
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })] }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    await w.find('[data-testid="inbox-pane-heading"]').trigger('keydown', { key: 'Escape' })
    await flushPromises()
    expect(router.currentRoute.value.query.item).toBeUndefined()
  })
})
