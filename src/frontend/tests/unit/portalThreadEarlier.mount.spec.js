// @vitest-environment jsdom
/**
 * trinity-enterprise#594 — "Show earlier" in a long Workspace thread.
 *
 * The thread read is a window (#2694); anything older was unreachable. The
 * history read now reports `hasEarlier` + a `nextBefore` cursor, and the
 * conversation offers "Show earlier" above the first row: a click reads the
 * next older window and prepends it without moving the row the reader is on.
 * A same-thread reload (a call ended) keeps the pages already loaded; a thread
 * switch drops them. Mounted (#2918): the real PortalConversation (shallow,
 * BaseButton real so the click is a real click), store stubbed at its seam.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(() => Promise.resolve({ data: {} })),
    put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

const voiceState = {}
vi.mock('@/composables/useVoiceSession', async (importOriginal) => {
  const actual = await importOriginal()
  const { ref } = await import('vue')
  return {
    ...actual,
    useVoiceSession: (...args) => {
      voiceState.active = ref(false)
      return { ...actual.useVoiceSession(...args), isActive: voiceState.active }
    },
  }
})

import PortalConversation from '@/components/portal/PortalConversation.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import { useClientPortalStore, portalHttp } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const at = (n) => `2026-10-26T10:${String(n).padStart(2, '0')}:00Z`
const msg = (id, n, over = {}) => ({ id, role: n % 2 ? 'assistant' : 'user', content: `text ${id}`, created_at: at(n), ...over })

// A thread of three windows: [o1 o2] [m1 m2] [n1 n2] (oldest → newest).
const PAGES = {
  newest: { messages: [msg('n1', 20), msg('n2', 21)], hasEarlier: true, nextBefore: 'cur-m' },
  'cur-m': { messages: [msg('m1', 10), msg('m2', 11)], hasEarlier: true, nextBefore: 'cur-o' },
  'cur-o': { messages: [msg('o1', 0), msg('o2', 1)], hasEarlier: false, nextBefore: null },
}

function history(page, sessionId = 's1') {
  return { sessionId, truncated: false, inFlightExecutionId: null, inFlightWaitBudgetSeconds: null,
           lastTurnOutcome: null, ...page }
}

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

function pagedFetch(pages = PAGES, sessionId = 's1') {
  return vi.fn(async (_agent, _sid, opts = {}) => history(pages[opts.before || 'newest'], sessionId))
}

async function open({ fetch = pagedFetch(), sessionId = 's1' } = {}) {
  store.fetchHistory = fetch
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, sessionId },
    attachTo: document.body,
    global: { stubs: { BaseButton } },
  })
  await flushPromises()
  return wrapper
}

const shown = (w) => w.vm.messages.map((m) => m.id)
const earlierButton = (w) => w.find('[data-testid="portal-history-earlier"]')

async function clickEarlier(w) {
  await earlierButton(w).trigger('click')
  await flushPromises()
}

describe('trinity-enterprise#594 — Show earlier', () => {
  it('a thread with nothing earlier shows no control and no notice', async () => {
    const w = await open({ fetch: pagedFetch({ newest: { messages: [msg('n1', 20)], hasEarlier: false, nextBefore: null } }) })
    expect(earlierButton(w).exists()).toBe(false)
    expect(w.find('[data-testid="portal-history-start"]').exists()).toBe(false)
  })

  it('a click prepends the older window and keeps every newer row', async () => {
    const w = await open()
    expect(earlierButton(w).text()).toContain('Show earlier')
    await clickEarlier(w)
    expect(store.fetchHistory).toHaveBeenLastCalledWith('scout', 's1', { before: 'cur-m' })
    expect(shown(w)).toEqual(['m1', 'm2', 'n1', 'n2'])
    await clickEarlier(w)
    expect(store.fetchHistory).toHaveBeenLastCalledWith('scout', 's1', { before: 'cur-o' })
    expect(shown(w)).toEqual(['o1', 'o2', 'm1', 'm2', 'n1', 'n2'])
  })

  it('at the start of the chat the control becomes "Start of this chat"', async () => {
    const w = await open()
    await clickEarlier(w)
    await clickEarlier(w)
    expect(earlierButton(w).exists()).toBe(false)
    expect(w.find('[data-testid="portal-history-start"]').text()).toBe('Start of this chat')
  })

  it('keeps the reader on the row they were reading (distance from the bottom)', async () => {
    const w = await open()
    const el = earlierButton(w).element.closest('.overflow-y-auto')
    let top = 0
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 100 })
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 100 * w.vm.messages.length })
    Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => top, set: (v) => { top = v } })
    top = 0                                          // the reader scrolled to the top
    el.dispatchEvent(new Event('scroll'))
    // 200 tall, 100 visible, at the top: 100px of transcript below the viewport.
    await clickEarlier(w)
    // 400 tall now; the same 100px below → scrollTop 200, not 0.
    expect(top).toBe(200)
  })

  it('a failed page says so and the same control retries', async () => {
    let fail = true
    const base = pagedFetch()
    const w = await open({ fetch: vi.fn(async (a, s, opts = {}) => {
      if (opts.before && fail) throw new Error('network')
      return base(a, s, opts)
    }) })
    await clickEarlier(w)
    expect(w.find('[data-testid="portal-history-earlier-failed"]').text()).toContain("Couldn't load earlier messages")
    expect(shown(w)).toEqual(['n1', 'n2'])
    fail = false
    await clickEarlier(w)
    expect(w.find('[data-testid="portal-history-earlier-failed"]').exists()).toBe(false)
    expect(shown(w)).toEqual(['m1', 'm2', 'n1', 'n2'])
  })

  it('is disabled while a turn is sending', async () => {
    const w = await open()
    w.vm.sending = true
    await flushPromises()
    expect(earlierButton(w).attributes('disabled')).toBeDefined()
    await clickEarlier(w)
    expect(store.fetchHistory).toHaveBeenCalledTimes(1)   // only the mount's own load
  })

  it('a reload of the same thread after a call keeps the earlier pages', async () => {
    const w = await open()
    await clickEarlier(w)
    expect(shown(w)).toEqual(['m1', 'm2', 'n1', 'n2'])
    // The call wrote rows: the newest window now ends with them.
    const after = { ...PAGES, newest: { messages: [msg('n1', 20), msg('n2', 21), msg('v1', 22)], hasEarlier: true, nextBefore: 'cur-m' } }
    store.fetchHistory = pagedFetch(after)
    voiceState.active.value = true
    await flushPromises()
    voiceState.active.value = false
    await flushPromises()
    expect(store.fetchHistory).toHaveBeenCalledWith('scout', 's1')
    expect(shown(w)).toEqual(['m1', 'm2', 'n1', 'n2', 'v1'])
    // ...and the cursor still points below the earliest page on screen.
    await clickEarlier(w)
    expect(store.fetchHistory).toHaveBeenLastCalledWith('scout', 's1', { before: 'cur-o' })
    expect(shown(w)).toEqual(['o1', 'o2', 'm1', 'm2', 'n1', 'n2', 'v1'])
  })

  it('a thread switch drops the pages, and a slow page from the old thread is ignored', async () => {
    let release
    const w = await open({ fetch: vi.fn(async (_a, sid, opts = {}) => {
      if (sid === 's1' && opts.before) return new Promise((r) => { release = () => r(history(PAGES['cur-m'])) })
      if (sid === 's2') return history({ messages: [msg('x1', 30)], hasEarlier: false, nextBefore: null }, 's2')
      return history(PAGES.newest)
    }) })
    await earlierButton(w).trigger('click')
    await w.setProps({ sessionId: 's2' })
    await flushPromises()
    release()
    await flushPromises()
    expect(shown(w)).toEqual(['x1'])
    expect(earlierButton(w).exists()).toBe(false)
    expect(w.find('[data-testid="portal-history-start"]').exists()).toBe(false)
  })

  it('a page that lands while a turn is in flight does not misplace that turn\'s failure', async () => {
    let releasePage
    const w = await open({ fetch: vi.fn(async (_a, _sid, opts = {}) => {
      if (opts.before) return new Promise((r) => { releasePage = () => r(history(PAGES['cur-m'])) })
      return history(PAGES.newest)
    }) })
    await earlierButton(w).trigger('click')            // the page is on its way…
    let refuse
    store.startPortalChat = vi.fn(() => new Promise((_, reject) => {
      refuse = () => reject({ response: { status: 503, data: { detail: 'The agent is offline.' } } })
    }))
    w.vm.input = 'are you there?'
    const sent = w.vm.send()                            // …and a turn starts before it lands
    await flushPromises()
    releasePage()
    await flushPromises()
    expect(shown(w).slice(0, 2)).toEqual(['m1', 'm2'])  // the page went above the turn
    refuse()
    await sent
    await flushPromises()
    const mine = w.vm.messages.find((m) => m.content === 'are you there?')
    expect(mine.failed).toBe(true)
  })
})

describe('trinity-enterprise#594 — the store\'s history read carries the cursor', () => {
  // Moved here from two source-text pins in portalVoiceMode.spec.js (#2694).
  it('reports older rows and the cursor to them, and sends `before` back', async () => {
    const get = vi.spyOn(portalHttp, 'get').mockResolvedValue({ data: {
      session_id: 's1', messages: [], truncated: true, has_earlier: true, next_before: 'T|id1' } })
    try {
      const out = await store.fetchHistory('scout', 's1')
      expect(out.hasEarlier).toBe(true)
      expect(out.nextBefore).toBe('T|id1')
      expect(get.mock.calls[0][1].params).toEqual({ session_id: 's1' })
      await store.fetchHistory('scout', 's1', { before: 'T|id1' })
      expect(get.mock.calls[1][1].params).toEqual({ session_id: 's1', before: 'T|id1' })
      // An older backend (a cut with no cursor) offers nothing to page to.
      get.mockResolvedValueOnce({ data: { session_id: 's1', messages: [], truncated: true } })
      expect((await store.fetchHistory('scout', 's1')).hasEarlier).toBe(false)
    } finally { get.mockRestore() }
  })

  it('threads the reply poll\'s `limit` through as a query param (#2694)', async () => {
    const get = vi.spyOn(portalHttp, 'get').mockResolvedValue({ data: { messages: [] } })
    try {
      await store.fetchHistory('scout', 's1', { limit: 8 })
      expect(get.mock.calls[0][1].params).toEqual({ session_id: 's1', limit: 8 })
    } finally { get.mockRestore() }
  })
})
