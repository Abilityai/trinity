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

  it('the total line is reserved in every state, so the list does not move when data lands', async () => {
    // The list states a total above its rows; before this the line appeared
    // only with the rows, and the list jumped ~36px down (layout stability).
    const w = await mountInbox({}, { query: { tab: 'action' } })
    expect(has(w, 'inbox-list-loading')).toBe(true)
    expect(has(w, 'inbox-list-total-reserve')).toBe(true)
    store.asksLoaded = true
    await flushPromises()
    expect(has(w, 'inbox-empty')).toBe(true)
    expect(has(w, 'inbox-list-total-reserve')).toBe(true)
    store.asks = [ask('a1')]
    await flushPromises()
    expect(has(w, 'inbox-list-total')).toBe(true)
    expect(has(w, 'inbox-list-total-reserve')).toBe(false)
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

  it('A11: All waits on the chats only — a failed asks read is a banner, never LoadFailed (§3g S3)', async () => {
    store.asksFailed = true // first asks read failed; no ask data at all
    const w = await mountInbox({ threads: [thread('t1', { last_message_at: iso(3) })] }, { query: { tab: 'all' } })
    expect(has(w, 'inbox-list-failed')).toBe(false)
    expect(has(w, 'inbox-row-thread:t1')).toBe(true)
    const banner = w.find('[data-testid="inbox-asks-stale"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain("Couldn't load your asks — the chats below are current.")
  })

  it('A11: All renders its chats before the asks read has a verdict, and merges the asks in when they land', async () => {
    const w = await mountInbox({ threads: [thread('t1', { last_message_at: iso(3) })] }, { query: { tab: 'all' } })
    expect(has(w, 'inbox-list-loading')).toBe(false)
    expect(has(w, 'inbox-row-thread:t1')).toBe(true)
    store.asks = [ask('a1', { created_at: iso(1) })]
    store.asksLoaded = true
    await flushPromises()
    expect(w.findAll('[data-inbox-row]').map((r) => r.attributes('data-inbox-row'))).toEqual(['ask:a1', 'thread:t1'])
  })

  it('A11: a failed asks REFRESH on All says the asks are stale, beside the rows', async () => {
    store.asks = [ask('a1')]
    store.asksLoaded = true
    store.asksLoadedAt = NOW - 60_000
    store.asksFailed = true
    const w = await mountInbox({ threads: [thread('t1')] }, { query: { tab: 'all' } })
    expect(w.find('[data-testid="inbox-asks-stale"]').text()).toContain("Couldn't refresh your asks")
    expect(has(w, 'inbox-row-ask:a1')).toBe(true)
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
    const markRead = vi.fn(async () => true)
    const w = await mountInbox({ threads, markRead }, { query: { tab: 'unread' } })
    // Desktop previews the first row — not in the URL, and NOT a read (§3g S5).
    expect(router.currentRoute.value.query.item).toBeUndefined()
    expect(w.find('[data-testid="inbox-row-thread:t1"]').attributes('aria-current')).toBe('true')
    expect(markRead).not.toHaveBeenCalled()

    await w.find('[data-testid="inbox-row-thread:t2"]').trigger('click')
    await flushPromises()
    expect(markRead.mock.calls).toEqual([['thread', 't2']])
    expect(router.currentRoute.value.query.item).toBe('thread:t2')

    // The shell zeroes the count: the row leaves Unread's membership but stays
    // where it was, drawn read, while it is selected.
    await w.setProps({ threads: [threads[0], { ...threads[1], unread: 0 }] })
    const row = w.find('[data-testid="inbox-row-thread:t2"]')
    expect(row.exists()).toBe(true)
    expect(row.attributes('aria-current')).toBe('true')
    expect(has(w, 'inbox-row-read-thread:t2')).toBe(true)
    expect(markRead).toHaveBeenCalledTimes(1)
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
    // m2..m8 are new; the pane caps at the tail (§3g S2), and names the rest.
    expect(before[0]).toBe('inbox-pane-message-m4')
    expect(w.find('[data-testid="inbox-pane-earlier"]').text()).toContain('1 earlier arrival')
    await w.find('[data-testid="inbox-pane-open"]').trigger('click')
    const href = w.emitted('open-chat')[0][0]
    expect(href).toContain('m%3Am2')

    // The read lands and the next refresh carries no preview for t1: still on All.
    await w.setProps({ threads: [{ ...threads[0], unread: 0 }, threads[1]], previews: {} })
    await flushPromises()
    expect(shown()).toEqual(before)
    await w.find('[data-testid="inbox-pane-open"]').trigger('click')
    expect(w.emitted('open-chat')[1][0]).toBe(href)

    // §3g T1: what was new is kept for the TAB VISIT, not only while selected —
    // reselecting t1 after another chat still anchors where it was new…
    await w.find('[data-testid="inbox-row-thread:t2"]').trigger('click')
    await flushPromises()
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    await w.find('[data-testid="inbox-pane-open"]').trigger('click')
    expect(w.emitted('open-chat').at(-1)[0]).toBe(href)
    // …and leaving the tab forgets it: back on All, t1 opens at the bottom.
    await router.replace({ path: '/workspace/inbox', query: { tab: 'unread' } })
    await flushPromises()
    await router.replace({ path: '/workspace/inbox', query: { tab: 'all', item: 'thread:t1' } })
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

  it('a deliverable renders its PAYLOAD, never the report row it came in', async () => {
    // fetchAgentReport returns the whole report ({id, title, payload, row_meta});
    // handing that to ReportRenderer made ReportSummary dump Id / Agent name /
    // Report type / Title (found on a live stack). PortalDeliverables unwraps it.
    store.fetchSessionDeliverablesStrict = vi.fn(async () => [
      { id: 'r1', report_type: 'summary', title: 'Invoice', display_hint: null, created_at: iso(2) },
    ])
    const payload = { summary: 'Totals are in.' }
    store.fetchAgentReport = vi.fn(async () => ({ id: 'r1', agent_name: 'scout', report_type: 'summary', title: 'Invoice', payload, row_meta: null }))
    const w = await mountInbox({ threads: [thread('t1', { unread: 1 })] }, { query: { tab: 'unread' } })
    const renderer = w.findComponent({ name: 'ReportRenderer' })
    expect(renderer.exists()).toBe(true)
    expect(renderer.props('payload')).toEqual(payload)
  })
})

describe('All says what it holds (§3g D-4)', () => {
  it('lists an old chat, and its footer says how ended asks leave', async () => {
    store.asksLoaded = true
    const w = await mountInbox({ threads: [thread('old', { last_message_at: iso(90 * 24 * 60) })] }, { query: { tab: 'all' } })
    expect(has(w, 'inbox-row-thread:old')).toBe(true)
    expect(w.find('[data-testid="inbox-list-footer"]').text()).toContain('Answered, expired and cancelled asks drop off after 7 days.')
    expect(w.find('[data-testid="inbox-list-footer"]').text()).not.toContain('Rooms')
  })

  it('says rooms are not here yet only to a viewer who has rooms', async () => {
    store.asksLoaded = true
    const threads = [thread('t1'), { id: 'r1', is_room: true, title: 'Room', last_message_at: iso(2) }]
    const w = await mountInbox({ threads }, { query: { tab: 'all' } })
    expect(w.find('[data-testid="inbox-list-footer"]').text()).toContain("Rooms aren't in the Inbox yet. Open them from the sidebar.")
  })

  it('says when the asks read stopped at its 200-row cap', async () => {
    store.asks = Array.from({ length: 200 }, (_, i) => ask(`a${i}`, { created_at: iso(i + 1) }))
    store.asksLoaded = true
    const w = await mountInbox({ threads: [] }, { query: { tab: 'all' } })
    expect(w.find('[data-testid="inbox-list-footer"]').text()).toContain('Showing your 200 most recent asks.')
  })

  it('an empty All offers a new chat', async () => {
    store.asksLoaded = true
    const w = await mountInbox({ threads: [] }, { query: { tab: 'all' } })
    expect(w.find('[data-testid="inbox-empty"]').text()).toContain('No chats or asks yet')
    expect(w.find('[data-testid="inbox-empty"]').text()).toContain('Start a chat with one of your agents. Its replies and asks land here.')
    expect(w.find('[data-testid="inbox-empty-link"]').text()).toBe('New chat')
  })
})

describe('long lists page: 50, then Show more (§3g SM / C4)', () => {
  const many = (n, over = {}) => Array.from({ length: n }, (_, i) => thread(`t${i}`, { unread: 1, last_message_at: iso(i + 1), ...over }))
  const rowCount = (w) => w.findAll('[data-inbox-row]').length

  it('120 rows show 50; Show more adds 50, moves focus to the first new row, and survives a poll', async () => {
    store.asksLoaded = true
    const threads = many(120)
    const w = await mountInbox({ threads }, { query: { tab: 'all' } })
    expect(rowCount(w)).toBe(50)
    const foot = w.find('[data-testid="inbox-list-page"]')
    expect(foot.text()).toContain('Showing 50 of 120')
    await w.find('[data-testid="inbox-show-more"]').trigger('click')
    await flushPromises()
    expect(rowCount(w)).toBe(100)
    expect(document.activeElement?.getAttribute('data-inbox-row')).toBe('thread:t50')
    // A poll (a fresh thread list) never shrinks the window back.
    await w.setProps({ threads: threads.map((t) => ({ ...t })) })
    await flushPromises()
    expect(rowCount(w)).toBe(100)
  })

  it('a ?item= at row 72 is shown, selected, and scrolled to (round 3)', async () => {
    const scrolled = []
    const orig = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function () { scrolled.push(this.getAttribute('data-inbox-row')) }
    try {
      const w = await mountInbox({ threads: many(120) }, { query: { tab: 'unread', item: 'thread:t71' } })
      expect(rowCount(w)).toBe(72)
      expect(w.find('[data-testid="inbox-row-thread:t71"]').attributes('aria-current')).toBe('true')
      // It sat ~6,000px below the visible list before: the reader saw a pane
      // with no idea which row it was.
      expect(scrolled).toContain('thread:t71')
    } finally {
      Element.prototype.scrollIntoView = orig
    }
  })

  it('the window a deep link widened stays wide when the reader picks another row (round 3)', async () => {
    const w = await mountInbox({ threads: many(120) }, { query: { tab: 'unread', item: 'thread:t71' } })
    expect(rowCount(w)).toBe(72)
    await w.find('[data-testid="inbox-row-thread:t10"]').trigger('click')
    await flushPromises()
    expect(rowCount(w)).toBe(72)
  })

  it('leaving the tab and coming back starts at 50 again', async () => {
    store.asksLoaded = true
    const w = await mountInbox({ threads: many(120) }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-show-more"]').trigger('click')
    await flushPromises()
    expect(rowCount(w)).toBe(100)
    await router.replace({ path: '/workspace/inbox', query: { tab: 'all' } })
    await flushPromises()
    await router.replace({ path: '/workspace/inbox', query: { tab: 'unread' } })
    await flushPromises()
    expect(rowCount(w)).toBe(50)
  })

  it('Unread with 80 rows pages too, and the paging line is gone once all are shown', async () => {
    const w = await mountInbox({ threads: many(80) }, { query: { tab: 'unread' } })
    expect(rowCount(w)).toBe(50)
    await w.find('[data-testid="inbox-show-more"]').trigger('click')
    await flushPromises()
    expect(rowCount(w)).toBe(80)
    expect(has(w, 'inbox-list-page')).toBe(false)
  })
})

describe('counts say what they count (§3g A8 / D-1 / B6)', () => {
  it('a tab counter caps at 99+, and the tab is named with the full number', async () => {
    const threads = Array.from({ length: 2 }, (_, i) => thread(`t${i}`, { unread: i ? 57 : 100 }))
    const w = await mountInbox({ threads }, { query: { tab: 'unread' } })
    const tab = w.findAll('[role="tab"]').find((t) => t.text().startsWith('Unread'))
    expect(tab.text()).toContain('99+')
    expect(tab.attributes('aria-label')).toBe("Unread, 157 new you haven't read")
    expect(w.find('[role="tablist"]').attributes('aria-label')).toBe('Inbox')
  })

  it('the list head names its unit', async () => {
    const threads = [thread('t1', { unread: 2 }), thread('t2', { unread: 3, last_message_at: iso(9) })]
    const w = await mountInbox({ threads }, { query: { tab: 'unread' } })
    expect(w.find('[data-testid="inbox-list-total"]').text()).toBe('2 chats · 5 new')
  })
})

describe('an ask row says what differs (§3g A10)', () => {
  const inMin = (m) => new Date(Date.now() + m * 60_000).toISOString()
  const rowOf = (w, id) => w.find(`[data-testid="inbox-row-ask:${id}"]`)
  const badges = (row) => row.findAll('[data-testid^="inbox-row-badge-"]').map((b) => b.text())

  it('no "Waiting on you"; high/critical say so, medium/low say nothing; expiry within a day is said', async () => {
    store.asks = [
      ask('c', { priority: 'critical', created_at: iso(1) }),
      // 18.5: minutes are floored (round 3), and NOW is taken at module load.
      ask('h', { priority: 'high', created_at: iso(2), expires_at: inMin(18.5) }),
      ask('m', { priority: 'medium', created_at: iso(3), expires_at: inMin(5 * 60 + 5) }),
      ask('l', { priority: 'low', created_at: iso(4) }),
    ]
    store.asksLoaded = true
    const w = await mountInbox({}, { query: { tab: 'action' } })
    expect(w.text()).not.toContain('Waiting on you')
    expect(badges(rowOf(w, 'c'))).toEqual(['Critical'])
    expect(badges(rowOf(w, 'h'))).toEqual(['High', 'Expires in 18m'])
    expect(badges(rowOf(w, 'm'))).toEqual(['Expires in 5h'])
    expect(badges(rowOf(w, 'l'))).toEqual([])
    // Under an hour is a warning; 1–24h is neutral gray, with the absolute time on hover.
    expect(rowOf(w, 'h').find('[data-testid="inbox-row-badge-expiry"]').classes()).toContain('bg-status-warning-100')
    const soon = rowOf(w, 'm').find('[data-testid="inbox-row-badge-expiry"]')
    expect(soon.classes()).toContain('bg-gray-100')
    expect(soon.attributes('title')).toMatch(/\(/) // "… (Zone)"
  })

  it('kind icons are distinct shapes in gray, with the kind spoken; an ended ask is not orange', async () => {
    store.asks = [
      ask('ap', { kind: 'approval', options: ['yes', 'no'], created_at: iso(1) }),
      ask('qu', { kind: 'question', created_at: iso(2) }),
      ask('al', { kind: 'notification', created_at: iso(3) }),
    ]
    store.asksLoaded = true
    const w = await mountInbox({}, { query: { tab: 'action' } })
    const icon = (id) => rowOf(w, id).find('svg')
    const paths = ['ap', 'qu', 'al'].map((id) => icon(id).find('path').attributes('d'))
    expect(new Set(paths).size).toBe(3)
    for (const id of ['ap', 'qu', 'al']) {
      expect(icon(id).classes()).toContain('text-gray-500')
      expect(icon(id).classes()).toContain('dark:text-gray-400')
      expect(icon(id).classes().some((k) => k.includes('urgent'))).toBe(false)
    }
    expect(rowOf(w, 'ap').find('.sr-only').text()).toBe('Needs approval')
  })

  it('the expiry clock ticks every 30 s only while such a row exists', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    try {
      store.asks = [ask('x', { created_at: iso(1), expires_at: inMin(61) })]
      store.asksLoaded = true
      const w = await mountInbox({}, { query: { tab: 'action' } })
      expect(badges(rowOf(w, 'x'))).toEqual(['Expires in 1h'])
      vi.advanceTimersByTime(2 * 60_000)
      await flushPromises()
      expect(badges(rowOf(w, 'x'))).toEqual(['Expires in 59m'])
      // Nothing left to move → the clock stops.
      store.asks = [ask('x', { created_at: iso(1), expires_at: null })]
      await flushPromises()
      expect(vi.getTimerCount()).toBe(0)
    } finally { vi.useRealTimers() }
  })
})

describe('rows keep their place for one tab visit (§3g S1)', () => {
  it('A1: a clicked Unread row survives its read landing before the route does', async () => {
    // t2 is 40 days old: no 30-day All window can rescue it as a fallback.
    const threads = [thread('t1', { unread: 2, last_message_at: iso(1) }), thread('t2', { unread: 3, last_message_at: iso(40 * 24 * 60) })]
    const w = await mountInbox({ threads }, { query: { tab: 'unread' } })
    const row = w.find('[data-testid="inbox-row-thread:t2"]')
    row.element.focus()
    row.trigger('click') // not awaited: the read lands first
    await w.setProps({ threads: [threads[0], { ...threads[1], unread: 0 }] })
    await flushPromises()
    const again = w.find('[data-testid="inbox-row-thread:t2"]')
    expect(again.exists()).toBe(true)
    expect(again.attributes('aria-current')).toBe('true')
    expect(has(w, 'inbox-pane-none')).toBe(false)
    expect(has(w, 'inbox-pane')).toBe(true)
    expect(document.activeElement?.getAttribute('data-inbox-row')).toBe('thread:t2')
  })

  it('A7: answering an ask on All does not move it', async () => {
    store.asks = [ask('a1', { created_at: iso(1) }), ask('a2', { created_at: iso(30) })]
    store.asksLoaded = true
    store.answerAsk = vi.fn(async (id) => {
      const answered = { ...store.asks.find((a) => a.id === id), status: 'answered', ended_by: 'you', ended_at: new Date().toISOString() }
      store.asks = store.asks.map((a) => (a.id === id ? answered : a))
      return answered
    })
    const threads = [thread('t1', { last_message_at: iso(10) })]
    const w = await mountInbox({ threads }, { query: { tab: 'all', item: 'ask:a2' } })
    const order = () => w.findAll('[data-inbox-row]').map((r) => r.attributes('data-inbox-row'))
    expect(order()).toEqual(['ask:a1', 'thread:t1', 'ask:a2'])
    await w.find('[data-testid="inbox-ask-input-a2"]').setValue('yes')
    await w.find('[data-testid="inbox-ask-input-a2"]').element.form.dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(store.answerAsk).toHaveBeenCalled()
    expect(order()).toEqual(['ask:a1', 'thread:t1', 'ask:a2'])
  })

  it('A12: a poll that reads a row elsewhere keeps the order and ghosts it until the tab is left', async () => {
    const threads = [
      thread('t1', { unread: 1, last_message_at: iso(1) }),
      thread('t2', { unread: 2, last_message_at: iso(5) }),
      thread('t3', { unread: 1, last_message_at: iso(9) }),
    ]
    const w = await mountInbox({ threads }, { query: { tab: 'unread', item: 'thread:t1' } })
    const order = () => w.findAll('[data-inbox-row]').map((r) => r.attributes('data-inbox-row'))
    expect(order()).toEqual(['thread:t1', 'thread:t2', 'thread:t3'])

    // Another device reads t2; the poll brings it back read.
    await w.setProps({ threads: [threads[0], { ...threads[1], unread: 0 }, threads[2]] })
    expect(order()).toEqual(['thread:t1', 'thread:t2', 'thread:t3'])
    expect(has(w, 'inbox-row-read-thread:t2')).toBe(true)

    // A newer arrival on t3 does not re-sort it.
    await w.setProps({ threads: [threads[0], { ...threads[1], unread: 0 }, { ...threads[2], unread: 4, last_message_at: iso(0) }] })
    expect(order()).toEqual(['thread:t1', 'thread:t2', 'thread:t3'])

    // The last live rows go read: the head says so rather than counting ghosts.
    await w.setProps({ threads: threads.map((t) => ({ ...t, unread: 0 })) })
    expect(order()).toEqual(['thread:t1', 'thread:t2', 'thread:t3'])
    expect(w.find('[data-testid="inbox-list-total"]').text()).toBe('All caught up')

    // Leave the tab and come back: the ghosts are gone.
    await router.replace({ path: '/workspace/inbox', query: { tab: 'all' } })
    await flushPromises()
    await router.replace({ path: '/workspace/inbox', query: { tab: 'unread' } })
    await flushPromises()
    expect(order()).toEqual([])
  })
})

describe('a chat is read only after the pane has rendered it (§3g S5, D-3)', () => {
  // A deferred promise, so a read can be held open mid-test.
  const deferred = () => { let resolve, reject; const p = new Promise((r, j) => { resolve = r; reject = j }); return { p, resolve, reject } }

  it('an explicit open marks read once history, deliverables and every payload have settled', async () => {
    const hist = deferred()
    const pay = deferred()
    store.fetchHistory = vi.fn(() => hist.p)
    store.fetchSessionDeliverablesStrict = vi.fn(async () => [{ id: 'r1', report_type: 'summary', title: 'R', created_at: iso(1) }])
    store.fetchAgentReport = vi.fn(() => pay.p)
    const markRead = vi.fn(async () => true)
    const threads = [thread('t1', { unread: 2, last_message_at: iso(1) }), thread('t2', { unread: 1, last_message_at: iso(9) })]
    const w = await mountInbox({ threads, markRead }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-row-thread:t2"]').trigger('click')
    await flushPromises()
    expect(markRead).not.toHaveBeenCalled() // history still in flight
    hist.resolve({ messages: [{ id: 'm1', role: 'assistant', content: 'hi' }] })
    await flushPromises()
    expect(markRead).not.toHaveBeenCalled() // the deliverable's payload still in flight
    pay.resolve({ payload: { summary: 'ok' } })
    await flushPromises()
    expect(markRead).toHaveBeenCalledTimes(1)
    expect(markRead).toHaveBeenCalledWith('thread', 't2')
  })

  it('a payload read that fails leaves the chat unread, with the error in the pane and Mark read to hand', async () => {
    store.fetchSessionDeliverablesStrict = vi.fn(async () => [{ id: 'r1', report_type: 'summary', title: 'R', created_at: iso(1) }])
    store.fetchAgentReport = vi.fn(async () => { throw new Error('500') })
    const markRead = vi.fn(async () => true)
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })], markRead }, { query: { tab: 'unread', item: 'thread:t1' } })
    expect(markRead).not.toHaveBeenCalled()
    expect(w.text()).toContain("Couldn't load this deliverable")
    const btn = w.find('[data-testid="inbox-pane-mark-read"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    await flushPromises()
    expect(markRead).toHaveBeenCalledWith('thread', 't1')
  })

  it('the desktop auto-selection is a preview: no ?item=, no read, and the shell is told', async () => {
    const markRead = vi.fn(async () => true)
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })], markRead }, { query: { tab: 'unread' } })
    expect(router.currentRoute.value.query.item).toBeUndefined()
    expect(w.find('[data-testid="inbox-row-thread:t1"]').attributes('aria-current')).toBe('true')
    expect(has(w, 'inbox-pane')).toBe(true)
    expect(markRead).not.toHaveBeenCalled()
    expect(w.emitted('update:preview')?.at(-1)).toEqual(['thread:t1'])
  })

  it('a deep-linked ?item= is an open: read after render', async () => {
    const markRead = vi.fn(async () => true)
    await mountInbox({ threads: [thread('t1', { unread: 2 })], markRead }, { query: { tab: 'unread', item: 'thread:t1' } })
    expect(markRead).toHaveBeenCalledTimes(1)
    expect(markRead).toHaveBeenCalledWith('thread', 't1')
  })

  it('clicking the PREVIEWED chat reloads it first: arrivals since the preview are shown before the read (round 3)', async () => {
    // The preview rendered t1 (renderedKey = t1); a poll then brought arrivals
    // the pane never drew. The click must not read on the stale verdict.
    const markRead = vi.fn(async () => true)
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })], markRead }, { query: { tab: 'unread' } })
    expect(store.fetchHistory).toHaveBeenCalledTimes(1) // the preview's read
    const hist = deferred()
    store.fetchHistory = vi.fn(() => hist.p)
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    expect(store.fetchHistory).toHaveBeenCalledTimes(1) // a fresh read for the open
    expect(markRead).not.toHaveBeenCalled() // ...and no read until it lands
    hist.resolve({ messages: [{ id: 'm9', role: 'assistant', content: 'new' }] })
    await flushPromises()
    expect(markRead).toHaveBeenCalledTimes(1)
  })

  it('reopening a chat read earlier waits for the new load, and a failed reload leaves it unread (round 3)', async () => {
    const markRead = vi.fn(async () => true)
    const threads = [thread('t1', { unread: 2 }), thread('t2', { unread: 1, last_message_at: iso(9) })]
    store.asksLoaded = true
    store.asks = [ask('a1')]
    const w = await mountInbox({ threads, markRead }, { query: { tab: 'all' } })
    await w.find('[data-testid="inbox-row-thread:t2"]').trigger('click')
    await flushPromises()
    expect(markRead).toHaveBeenCalledTimes(1)
    // An ask renders no chat, so nothing replaces t2's verdict...
    await w.find('[data-testid="inbox-row-ask:a1"]').trigger('click')
    await flushPromises()
    // ...and t2 reopens with a load that FAILS: it must stay unread.
    store.fetchHistory = vi.fn(async () => { throw new Error('500') })
    await w.find('[data-testid="inbox-row-thread:t2"]').trigger('click')
    await flushPromises()
    expect(markRead).toHaveBeenCalledTimes(1)
  })

  it('a read write that fails says so in the pane', async () => {
    const markRead = vi.fn(async () => false)
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })], markRead }, { query: { tab: 'unread', item: 'thread:t1' } })
    expect(markRead).toHaveBeenCalledTimes(1)
    expect(has(w, 'inbox-pane-read-error')).toBe(true)
  })
})

describe('answering an ask in the pane (D8)', () => {
  it('goes through store.answerAsk, and the ended ask stays in place until the tab is left (§3g T1)', async () => {
    store.asks = [ask('q1')]
    store.asksLoaded = true
    store.answerAsk = vi.fn(async (id) => {
      const answered = { ...store.asks.find((a) => a.id === id), status: 'answered', ended_by: 'you', ended_at: new Date().toISOString() }
      store.asks = store.asks.map((a) => (a.id === id ? answered : a))
      return answered
    })
    const w = await mountInbox({}, { query: { tab: 'action' } })
    // The desktop preview (§3g S5): selected, not in the URL.
    expect(router.currentRoute.value.query.item).toBeUndefined()
    expect(w.find('[data-testid="inbox-row-ask:q1"]').attributes('aria-current')).toBe('true')

    await w.find('[data-testid="inbox-ask-input-q1"]').setValue('yes, go')
    await w.find('[data-testid="inbox-ask-input-q1"]').element.form.dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(store.answerAsk).toHaveBeenCalledWith('q1', expect.anything())

    // No longer pending → no longer Action's membership, but still drawn, ended.
    expect(has(w, 'inbox-row-ask:q1')).toBe(true)
    expect(has(w, 'inbox-row-ended-ask:q1')).toBe(true)

    // The selection moving on no longer drops it (T1: a ghost lives for the
    // tab visit)…
    await router.replace({ path: '/workspace/inbox', query: { tab: 'action' } })
    await flushPromises()
    expect(has(w, 'inbox-row-ended-ask:q1')).toBe(true)
    // …leaving the tab does.
    await router.replace({ path: '/workspace/inbox', query: { tab: 'all' } })
    await flushPromises()
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

describe('Mark all read (D11, §3g A9)', () => {
  // The shell's markRead zeroes the chat before its write resolves; mimic that
  // so the props follow the reads the way they do in Portal.vue.
  function shellLike(initial, fail = () => false) {
    let current = initial
    const fn = vi.fn(async (_k, id) => {
      if (fail(id)) return false
      current = current.map((t) => (t.id === id ? { ...t, unread: 0 } : t))
      await wrapper.setProps({ threads: current })
      return true
    })
    return fn
  }
  const confirmEl = () => document.querySelector('[data-testid="confirm-dialog"]')

  it('is on Unread and All only, and says how many chats it reads', async () => {
    store.asks = [ask('a1')]
    store.asksLoaded = true
    const threads = [thread('t1', { unread: 2 }), thread('t2', { unread: 3, last_message_at: iso(9) })]
    const w = await mountInbox({ threads }, { query: { tab: 'action' } })
    expect(has(w, 'inbox-mark-all-read')).toBe(false)
    await router.replace({ path: '/workspace/inbox', query: { tab: 'unread' } })
    await flushPromises()
    expect(w.find('[data-testid="inbox-mark-all-read"]').text()).toBe('Mark 2 chats read')
  })

  it('more than one chat asks first, with the consequence, Cancel focused, and a non-danger confirm', async () => {
    const threads = [thread('t1', { unread: 2 }), thread('t2', { unread: 3, last_message_at: iso(9) })]
    const markRead = shellLike(threads)
    const w = await mountInbox({ threads, markRead }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-mark-all-read"]').trigger('click')
    await flushPromises()
    expect(markRead).not.toHaveBeenCalled()
    const dlg = confirmEl()
    expect(dlg).toBeTruthy()
    expect(dlg.querySelector('[data-testid="confirm-dialog-title"]').textContent.trim()).toBe('Mark 2 chats read?')
    expect(dlg.querySelector('[data-testid="confirm-dialog-message"]').textContent.trim())
      .toBe("5 new messages across 2 chats will be marked read. You can't undo this.")
    const confirm = dlg.querySelector('[data-testid="confirm-dialog-confirm"]')
    expect(confirm.textContent.trim()).toBe('Mark 2 chats read')
    expect(confirm.className).not.toContain('bg-status-danger')
    expect(document.activeElement?.getAttribute('data-testid')).toBe('confirm-dialog-cancel')

    confirm.click()
    await flushPromises()
    expect(markRead.mock.calls.map((c) => c[1]).sort()).toEqual(['t1', 't2'])
    expect(w.find('[data-testid="inbox-toast"]').text()).toBe('Marked 2 chats read')
    // A completed bulk read starts a new visit: no ghosts, the empty copy.
    expect(w.find('[data-testid="inbox-empty"]').text()).toContain("You're all caught up")
  })

  it('the confirm is drawn as information, not a warning — it is not destructive (round 3)', async () => {
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 }), thread('t2', { unread: 1 })] }, { query: { tab: 'unread' } })
    expect(w.findComponent({ name: 'ConfirmDialog' }).props('variant')).toBe('info')
  })

  it('Cancel reads nothing', async () => {
    const threads = [thread('t1', { unread: 2 }), thread('t2', { unread: 3, last_message_at: iso(9) })]
    const markRead = shellLike(threads)
    const w = await mountInbox({ threads, markRead }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-mark-all-read"]').trigger('click')
    await flushPromises()
    document.querySelector('[data-testid="confirm-dialog-cancel"]').click()
    await flushPromises()
    expect(markRead).not.toHaveBeenCalled()
    expect(confirmEl()).toBeNull()
  })

  it('one chat writes directly', async () => {
    const threads = [thread('t1', { unread: 2 })]
    const markRead = shellLike(threads)
    const w = await mountInbox({ threads, markRead }, { query: { tab: 'unread' } })
    expect(w.find('[data-testid="inbox-mark-all-read"]').text()).toBe('Mark 1 chat read')
    await w.find('[data-testid="inbox-mark-all-read"]').trigger('click')
    await flushPromises()
    expect(confirmEl()).toBeNull()
    expect(markRead).toHaveBeenCalledWith('thread', 't1')
  })

  it('a partial failure names the count, keeps those chats, and is no success', async () => {
    const threads = [thread('t1', { unread: 2 }), thread('t2', { unread: 1, last_message_at: iso(9) })]
    const markRead = shellLike(threads, (id) => id === 't2')
    const w = await mountInbox({ threads, markRead }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-mark-all-read"]').trigger('click')
    await flushPromises()
    document.querySelector('[data-testid="confirm-dialog-confirm"]').click()
    await flushPromises()
    expect(w.find('[data-testid="inbox-mark-all-error"]').text()).toContain('1 of 2 chats')
    expect(has(w, 'inbox-toast')).toBe(false)
    expect(has(w, 'inbox-row-thread:t2')).toBe(true)
    expect(w.emitted('refresh')).toHaveLength(1)
  })
})

describe('the pane reads like the chat (§3g A13)', () => {
  it('three messages from the agent are one header and three bubbles, in the chat\'s own bubble', async () => {
    const t0 = Date.now() - 10 * 60_000
    const msgs = [1, 2, 3].map((i) => ({ id: `m${i}`, role: 'assistant', content: `c${i}`, created_at: new Date(t0 + i * 60_000).toISOString() }))
    store.fetchHistory = vi.fn(async () => ({ messages: msgs }))
    const w = await mountInbox({ threads: [thread('t1', { unread: 3 })], previews: { 'thread:t1': { latest: null, first_unread_message_id: 'm1' } } },
      { query: { tab: 'unread', item: 'thread:t1' } })
    expect(w.findAll('[data-testid="inbox-pane-run-header"]')).toHaveLength(1)
    expect(w.findAllComponents({ name: 'PortalAgentBubble' })).toHaveLength(3)
    expect(w.findAllComponents({ name: 'PortalAvatar' })).toHaveLength(1)
    const header = w.find('[data-testid="inbox-pane-run-header"]')
    expect(header.classes()).toContain('text-[12.5px]')
    expect(header.classes()).not.toContain('uppercase')
    expect(header.find('[title]').exists()).toBe(true) // absolute time on hover
    // The body is capped at the conversation's reading width.
    expect(w.find('[data-testid="inbox-pane-body"]').classes()).toContain('max-w-[var(--ws-message-max,64rem)]')
  })
})

describe('the pane header (§3g L5: stacked chrome, split order)', () => {
  const headerIds = (w) => w.find('[data-testid="inbox-pane"] header').findAll('[data-testid]')
    .map((el) => el.attributes('data-testid'))
    .filter((id) => /^inbox-pane-(back|heading|mark-read|reply|open|more)$/.test(id))

  it('stacked: [Back][title] … [Open in chat][More ▾]; the Inbox title and tabs step aside', async () => {
    phone = true
    stubMatchMedia()
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })] }, { query: { tab: 'unread' } })
    expect(w.find('[data-testid="inbox-header"]').classes()).not.toContain('hidden')
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    // Round 3: the title has a line of its own under the actions — beside
    // them it got 73–90px at 375–390 and 0px at 200% zoom, which pushed More
    // off-screen (WCAG 1.4.10).
    // The heading stays FIRST in the reading order and moves by `order`.
    expect(headerIds(w)).toEqual(['inbox-pane-back', 'inbox-pane-heading', 'inbox-pane-open', 'inbox-pane-more'])
    const h = w.find('[data-testid="inbox-pane-heading"]')
    expect(h.classes()).toEqual(expect.arrayContaining(['order-last', 'basis-full']))
    expect(h.attributes('title')).toBe(h.text())
    for (const id of ['inbox-pane-back', 'inbox-pane-open', 'inbox-pane-more']) {
      expect(w.find(`[data-testid="${id}"]`).classes()).toContain('max-sm:min-h-11')
    }
    expect(w.find('[data-testid="inbox-header"]').classes()).toContain('hidden')
    expect(w.find('[data-testid="inbox-tabs"]').classes()).toContain('hidden')
    // More holds the rest.
    const more = w.find('[data-testid="inbox-pane-more"]')
    expect(more.attributes('aria-expanded')).toBe('false')
    await more.trigger('click')
    expect(more.attributes('aria-expanded')).toBe('true')
    const menu = w.find('[data-testid="inbox-pane-more-menu"]')
    expect(menu.find('[data-testid="inbox-pane-reply"]').exists()).toBe(true)
    expect(menu.find('[data-testid="inbox-pane-mark-read"]').exists()).toBe(true)
    expect(menu.find('[data-testid="inbox-pane-reply"]').classes()).toContain('max-sm:min-h-11')
    await menu.trigger('keydown', { key: 'Escape' })
    expect(w.find('[data-testid="inbox-pane-more-menu"]').exists()).toBe(false)
    expect(document.activeElement).toBe(more.element)
  })

  it('Esc with More open and focus on its button closes the menu, not the pane (round 3)', async () => {
    phone = true
    stubMatchMedia()
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })] }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    const more = w.find('[data-testid="inbox-pane-more"]')
    more.element.focus()
    await more.trigger('click')
    await more.trigger('keydown', { key: 'Escape' })
    await flushPromises()
    expect(w.find('[data-testid="inbox-pane-more-menu"]').exists()).toBe(false)
    expect(router.currentRoute.value.query.item).toBe('thread:t1')
    expect(has(w, 'inbox-pane')).toBe(true)
    expect(document.activeElement).toBe(more.element)
    // With the menu shut, Esc is Back again.
    await more.trigger('keydown', { key: 'Escape' })
    await flushPromises()
    expect(router.currentRoute.value.query.item).toBeUndefined()
  })

  it("split: the pane's Mark read hands focus to the pane heading as it leaves (round 3)", async () => {
    store.fetchSessionDeliverablesStrict = vi.fn(async () => [{ id: 'r1', report_type: 'summary', title: 'R', created_at: iso(1) }])
    store.fetchAgentReport = vi.fn(async () => { throw new Error('500') }) // keeps it unread, so the button shows
    const markRead = vi.fn(async () => true)
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })], markRead }, { query: { tab: 'unread', item: 'thread:t1' } })
    const btn = w.find('[data-testid="inbox-pane-mark-read"]')
    btn.element.focus()
    await btn.trigger('click')
    await w.setProps({ threads: [thread('t1', { unread: 0 })] })
    await flushPromises()
    expect(document.activeElement?.getAttribute('data-testid')).toBe('inbox-pane-heading')
  })

  it('split: the header reserves the action row, so an action landing late does not move the body (round 3)', async () => {
    store.asksLoaded = true
    store.asks = [ask('a1')]
    const w = await mountInbox({}, { query: { tab: 'action', item: 'ask:a1' } })
    expect(w.find('[data-testid="inbox-pane"] header').classes()).toContain('min-h-[3.25rem]')
  })

  it('split: [title] … [Mark read][Reply][Open in chat] — volatile actions leftmost, the title truncates', async () => {
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })] }, { query: { tab: 'unread', item: 'thread:t1' } })
    expect(headerIds(w)).toEqual(['inbox-pane-heading', 'inbox-pane-mark-read', 'inbox-pane-reply', 'inbox-pane-open'])
    const h = w.find('[data-testid="inbox-pane-heading"]')
    expect(h.classes()).toEqual(expect.arrayContaining(['flex-1', 'truncate', 'min-w-0']))
    expect(w.find('[data-testid="inbox-pane"] header').classes()).not.toContain('flex-wrap')
    expect(w.find('[data-testid="inbox-header"]').classes()).not.toContain('hidden')
  })
})

describe('Open canvas (§3g C10, T6)', () => {
  const headerIds = (w) => w.find('[data-testid="inbox-pane"] header').findAll('[data-testid]')
    .map((el) => el.attributes('data-testid'))
    .filter((id) => /^inbox-pane-(heading|open-canvas|mark-read|reply|open|more)$/.test(id))

  it('split: leftmost of the actions on a chat, and on an ask; it asks the shell for the canvas', async () => {
    store.asks = [ask('q1')]
    store.asksLoaded = true
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })], canvasCount: 2 }, { query: { tab: 'unread', item: 'thread:t1' } })
    expect(headerIds(w)).toEqual(['inbox-pane-heading', 'inbox-pane-open-canvas', 'inbox-pane-mark-read', 'inbox-pane-reply', 'inbox-pane-open'])
    await w.find('[data-testid="inbox-pane-open-canvas"]').trigger('click')
    expect(w.emitted('open-canvas')).toHaveLength(1)
    await router.replace({ path: '/workspace/inbox', query: { tab: 'action', item: 'ask:q1' } })
    await flushPromises()
    expect(has(w, 'inbox-pane-open-canvas')).toBe(true)
  })

  it('absent with no canvas', async () => {
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })], canvasCount: 0 }, { query: { tab: 'unread', item: 'thread:t1' } })
    expect(has(w, 'inbox-pane-open-canvas')).toBe(false)
  })

  it('stacked: it lives in More', async () => {
    phone = true
    stubMatchMedia()
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })], canvasCount: 1 }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    expect(headerIds(w)).not.toContain('inbox-pane-open-canvas')
    await w.find('[data-testid="inbox-pane-more"]').trigger('click')
    expect(w.find('[data-testid="inbox-pane-more-menu"] [data-testid="inbox-pane-open-canvas"]').exists()).toBe(true)
  })
})

describe('a phone reaches the menu from the Inbox (§3g A5)', () => {
  it('the subtitle steps aside on a phone, so the header is one line (round 3)', async () => {
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })] }, { query: { tab: 'unread' } })
    expect(w.find('[data-testid="inbox-subtitle"]').classes()).toContain('max-sm:hidden')
  })

  it('a 44px, phone-only Menu button asks the shell for the drawer', async () => {
    const w = await mountInbox({ threads: [] }, { query: { tab: 'all' } })
    const btn = w.find('[data-testid="inbox-menu"]')
    expect(btn.exists()).toBe(true)
    expect(btn.attributes('aria-label')).toBe('Menu')
    expect(btn.classes()).toEqual(expect.arrayContaining(['sm:hidden', 'h-11', 'w-11']))
    await btn.trigger('click')
    expect(w.emitted('open-menu')).toHaveLength(1)
  })
})

describe('an ask attached to a chat is answerable in the pane (#3055 / §3g L0)', () => {
  // Ingestion attaches every addressed ask to Main, so every real ask has a
  // chat_id — and the card's thread link once took the controls away. The
  // pane keeps the link (the only way to the ask's chat until E1 lands) AND
  // the controls.
  it('a question with a chat shows its answer box and the link back', async () => {
    store.asksLoaded = true
    store.asks = [ask('q1', { chat_id: 'main-1' })]
    const w = await mountInbox({}, { query: { tab: 'action', item: 'ask:q1' } })
    expect(has(w, 'inbox-ask-input-q1')).toBe(true)
    expect(has(w, 'inbox-ask-open-thread-q1')).toBe(true)
  })
  it('an approval with a chat shows its options and Send', async () => {
    store.asksLoaded = true
    store.asks = [ask('ap1', { kind: 'approval', options: ['Yes', 'No'], chat_id: 'main-1' })]
    const w = await mountInbox({}, { query: { tab: 'action', item: 'ask:ap1' } })
    expect(w.findAll('[data-testid="inbox-ask-option-ap1"]').length).toBe(2)
    expect(has(w, 'inbox-ask-send-ap1')).toBe(true)
  })
})

describe('round-3 list fixes', () => {
  it('an empty tab is one column: no "Pick something on the left" beside nothing to pick', async () => {
    store.asksLoaded = true
    const w = await mountInbox({ threads: [] }, { query: { tab: 'all' } })
    expect(has(w, 'inbox-empty')).toBe(true)
    expect(has(w, 'inbox-pane-none')).toBe(false)
    expect(w.find('[data-testid="inbox-list-column"]').classes()).toContain('w-full')
  })

  it("a row's agent name keeps its width; the chat title is what truncates", async () => {
    const w = await mountInbox({ threads: [thread('t1', { unread: 2, title: 'A very long title '.repeat(8) })] }, { query: { tab: 'unread' } })
    const agent = w.find('[data-testid="inbox-row-agent-thread:t1"]')
    expect(agent.classes()).toEqual(expect.arrayContaining(['shrink-0', 'max-w-[60%]', 'truncate']))
  })

  it("the head's capped count is available in full", async () => {
    const threads = [thread('t1', { unread: 80 }), thread('t2', { unread: 95 })]
    const w = await mountInbox({ threads }, { query: { tab: 'unread' } })
    const head = w.find('[data-testid="inbox-list-total"]')
    expect(head.text()).toContain('99+ new')
    expect(head.attributes('title')).toBe('2 chats · 175 new')
    expect(head.find('.sr-only').text()).toBe('2 chats · 175 new')
  })

  it('an ask crossing into the day gets its badge although no row was ticking', async () => {
    vi.useFakeTimers({ now: NOW, toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    try {
      store.asksLoaded = true
      store.asks = [ask('a1', { expires_at: new Date(NOW + 25 * 3600_000).toISOString() })]
      const w = await mountInbox({}, { query: { tab: 'action' } })
      expect(has(w, 'inbox-row-badge-expiry')).toBe(false)
      await vi.advanceTimersByTimeAsync(61 * 60_000)
      expect(w.find('[data-testid="inbox-row-badge-expiry"]').text()).toBe('Expires in 23h')
    } finally {
      vi.useRealTimers()
    }
  })

  it('stacked, the list keeps a reading width', async () => {
    phone = true
    stubMatchMedia()
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })] }, { query: { tab: 'unread' } })
    expect(w.find('[data-testid="inbox-list-column"]').classes()).toEqual(expect.arrayContaining(['w-full', 'max-w-3xl']))
  })
})

describe('the layout follows the CONTAINER width (§3g A4)', () => {
  // A ResizeObserver that reports a fixed content width the moment it observes.
  function fakeRO(width) {
    return class {
      constructor(cb) { this.cb = cb }
      observe(el) { this.cb([{ target: el, contentRect: { width } }]) }
      unobserve() {}
      disconnect() {}
    }
  }
  let saved
  beforeEach(() => { saved = globalThis.ResizeObserver })
  afterEach(() => { globalThis.ResizeObserver = saved })

  it('at 700px on a desktop viewport it stacks: the list takes the width and nothing is previewed', async () => {
    globalThis.ResizeObserver = fakeRO(700)
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })] }, { query: { tab: 'unread' } })
    expect(w.find('[data-testid="inbox"]').attributes('data-layout')).toBe('stacked')
    expect(has(w, 'inbox-pane')).toBe(false) // no auto-select when stacked
    expect(w.find('[data-testid="inbox-list-column"]').classes()).toContain('w-full')
    // Opening a row shows the pane in the list's place, with Back.
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="inbox-list-column"]').classes()).toContain('hidden')
    expect(has(w, 'inbox-pane-back')).toBe(true)
  })

  it('at 1280px it splits, with the wide list', async () => {
    globalThis.ResizeObserver = fakeRO(1280)
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })] }, { query: { tab: 'unread' } })
    expect(w.find('[data-testid="inbox"]').attributes('data-layout')).toBe('split')
    expect(w.find('[data-testid="inbox-list-column"]').classes()).toContain('w-96')
    expect(has(w, 'inbox-pane')).toBe(true)
  })

  it('a flip keeps the opened item and moves focus to where it now is', async () => {
    let fire
    globalThis.ResizeObserver = class {
      constructor(cb) { this.cb = cb }
      observe(el) { fire = (width) => this.cb([{ target: el, contentRect: { width } }]); fire(1280) }
      unobserve() {}
      disconnect() {}
    }
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 }), thread('t2', { last_message_at: iso(9) })] },
      { query: { tab: 'all', item: 'thread:t1' } })
    w.find('[data-testid="inbox-row-thread:t1"]').element.focus()
    fire(700) // split → stacked: the pane replaces the list; focus its heading
    await flushPromises()
    expect(w.find('[data-testid="inbox"]').attributes('data-layout')).toBe('stacked')
    expect(document.activeElement?.getAttribute('data-testid')).toBe('inbox-pane-heading')
    fire(1280) // stacked → split: the selection stays; focus its row
    await flushPromises()
    expect(router.currentRoute.value.query.item).toBe('thread:t1')
    expect(document.activeElement?.getAttribute('data-inbox-row')).toBe('thread:t1')
  })

  it('the rail about to arrive is counted, so a preview never flips the layout', async () => {
    globalThis.ResizeObserver = fakeRO(1000)
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })], railAllowance: 384 }, { query: { tab: 'unread' } })
    expect(w.find('[data-testid="inbox"]').attributes('data-layout')).toBe('stacked')
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

  it('Back after reading the chat off Unread focuses that row, kept in place as read (§3g S1)', async () => {
    phone = true
    stubMatchMedia()
    const two = [thread('t1', { unread: 2 }), thread('t2', { unread: 1, last_message_at: iso(9) })]
    const w = await mountInbox({ threads: two }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    // The shell's read lands: t1 has nothing new any more, and stays as a ghost.
    await w.setProps({ threads: [thread('t1', { unread: 0 }), two[1]] })
    await w.find('[data-testid="inbox-pane-back"]').trigger('click')
    await flushPromises()
    expect(document.activeElement?.getAttribute('data-inbox-row')).toBe('thread:t1')
    expect(has(w, 'inbox-row-read-thread:t1')).toBe(true)
  })

  it('an open chat deleted elsewhere goes Back: the row now in its place, else the list (§3g S1)', async () => {
    phone = true
    stubMatchMedia()
    const two = [thread('t1', { unread: 2 }), thread('t2', { unread: 1, last_message_at: iso(9) })]
    const w = await mountInbox({ threads: two }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    await w.setProps({ threads: [two[1]] }) // t1 deleted elsewhere: nothing to draw
    await flushPromises()
    expect(router.currentRoute.value.query.item).toBeUndefined()
    expect(document.activeElement?.getAttribute('data-inbox-row')).toBe('thread:t2')

    await w.find('[data-testid="inbox-row-thread:t2"]').trigger('click')
    await flushPromises()
    await w.setProps({ threads: [] })
    await flushPromises()
    expect(document.activeElement?.getAttribute('data-testid')).toBe('inbox-list-column')
    expect(w.find('[data-testid="inbox-list-column"]').classes()).not.toContain('hidden')
  })

  it('§3g A6: the hardware Back after a phone open stays in the Inbox, focus on the row', async () => {
    phone = true
    stubMatchMedia()
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 }), thread('t2', { unread: 1, last_message_at: iso(9) })] }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-row-thread:t2"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.item).toBe('thread:t2')
    router.back() // the browser's / the OS's Back, not the pane's button
    await flushPromises()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/workspace/inbox')
    expect(router.currentRoute.value.query.item).toBeUndefined()
    expect(router.currentRoute.value.query.tab).toBe('unread')
    expect(document.activeElement?.getAttribute('data-inbox-row')).toBe('thread:t2')
  })

  it("§3g A6: the pane's Back after a phone open POPS our entry, so Forward reopens it (round 3)", async () => {
    // A replace here would leave a duplicate history entry: the hardware Back
    // would then reopen the pane. Popping is what makes Forward land on it.
    phone = true
    stubMatchMedia()
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })] }, { query: { tab: 'unread' } })
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    await w.find('[data-testid="inbox-pane-back"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.item).toBeUndefined()
    router.forward()
    await flushPromises()
    expect(router.currentRoute.value.query.item).toBe('thread:t1')
  })

  it('§3g A6: split, an open REPLACES — Back does not step through every row read', async () => {
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 }), thread('t2', { unread: 1, last_message_at: iso(9) })] }, { query: { tab: 'unread' } })
    // A guard for the split branch (it held before A6 too): two opens, one Back,
    // and Back never lands on the first row opened.
    await w.find('[data-testid="inbox-row-thread:t2"]').trigger('click')
    await flushPromises()
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    router.back()
    await flushPromises()
    expect(router.currentRoute.value.query.item).not.toBe('thread:t2')
    expect(router.currentRoute.value.path).toBe('/workspace/inbox')
  })

  it("the pane's Back after a DEEP-LINKED item replaces — it never walks out of the Inbox", async () => {
    phone = true
    stubMatchMedia()
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })] }, { query: { tab: 'unread', item: 'thread:t1' } })
    await w.find('[data-testid="inbox-pane-back"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/workspace/inbox')
    expect(router.currentRoute.value.query.item).toBeUndefined()
  })

  it('§3g F6: the pane\'s Back and Mark all read are 44px tall on a phone', async () => {
    phone = true
    stubMatchMedia()
    const w = await mountInbox({ threads: [thread('t1', { unread: 2 })] }, { query: { tab: 'unread' } })
    expect(w.find('[data-testid="inbox-mark-all-read"]').classes()).toContain('max-sm:min-h-11')
    await w.find('[data-testid="inbox-row-thread:t1"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="inbox-pane-back"]').classes()).toContain('max-sm:min-h-11')
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
