// @vitest-environment jsdom
/**
 * #4160 — a room's content in the Inbox reading pane stays INSIDE the pane.
 *
 * Every agent bubble renders through `PortalMarkdown`, which carries an
 * `sr-only` live region — and `sr-only` is `position: absolute`. With no
 * positioned ancestor between it and the root, its containing block is the
 * initial containing block: the pane's `overflow-y-auto` box does not clip it,
 * and its static position (deep in a long room) stretches the DOCUMENT, so the
 * wheel scrolls the whole Workspace once the pane reaches its end. The list
 * column met the same class at sign-off and fixed it with `relative` on its
 * scroll box (PortalInboxList.vue); the reading pane's scroll box had none.
 *
 * jsdom does no layout, so this pins the STRUCTURE that keeps the content
 * contained: the room card renders inside the pane's scroll container, that
 * container is a positioned box (every absolutely positioned descendant
 * resolves against it or something inside it), and the min-h-0 / overflow
 * chain from the Inbox root down to the scroll box is intact — for a room and
 * for a thread alike (AC: the room behaves like the thread).
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
window.matchMedia = (q) => ({
  matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
})

const NOW = Date.now()
const iso = (minsAgo) => new Date(NOW - minsAgo * 60_000).toISOString()
const thread = (id, over = {}) => ({
  id, session_id: id, agent_name: 'ada', is_main: false, title: `Chat ${id}`,
  last_message_at: iso(30), unread: 0, ...over,
})
const room = (id, over = {}) => ({
  id, is_room: true, member: true, name: `Room ${id}`, title: `Room ${id}`,
  agent_names: ['ada', 'bo'], last_message_at: iso(5), unread: 0, ...over,
})
const roomDetail = (id, messages) => ({
  id, name: `Room ${id}`, status: 'open', participants: [], messages, own_tags: {}, working: [], message_count: messages.length,
})
const msg = (id, seq, over = {}) => ({
  id, seq, kind: 'message', sender_kind: 'agent', sender_identity: 'ada', content: `say ${id}`,
  created_at: iso(10 - seq), ...over,
})
// A room with one very long message (the room limit is 100,000 characters).
const LONG = Array.from({ length: 40 }, (_, i) => `Paragraph ${i}. ${'word '.repeat(400)}`).join('\n\n')

const POSITIONED = ['relative', 'absolute', 'fixed', 'sticky']
const isPositioned = (el) => POSITIONED.some((c) => el.classList?.contains(c))

let store, router, wrapper
beforeEach(async () => {
  localStorage.clear()
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.asksAvailable = true
  store.asksLoaded = true
  store.multiAgentChatAvailable = true
  store.fetchSessionDeliverablesStrict = vi.fn(async () => [])
  store.fetchAgentReport = vi.fn(async () => ({}))
  store.fetchAsks = vi.fn(async () => store.asks)
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/workspace/inbox', component: { template: '<div />' } },
      { path: '/workspace/c/:id', component: { template: '<div />' } },
      { path: '/workspace/r/:roomId', component: { template: '<div />' } },
    ],
  })
  await router.push('/workspace/inbox')
  await router.isReady()
})
afterEach(() => { wrapper?.unmount(); wrapper = null; document.body.innerHTML = '' })

async function mountInbox(props, query) {
  await router.replace({ path: '/workspace/inbox', query })
  await flushPromises()
  wrapper = mount(PortalInbox, {
    props: { previews: {}, threadsLoaded: true, threadsFailed: false, labels: { ada: 'Ada', bo: 'Bo' }, ...props },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

// The containment contract, read off the mounted DOM.
function expectContained(w, contentTestid) {
  // The pane's scroll box is the parent of its reading-width body.
  const body = w.find('[data-testid="inbox-pane-body"]')
  expect(body.exists()).toBe(true)
  const box = body.element.parentElement

  // The scroll box is a flex child that may shrink below its content, and
  // scrolls on its own.
  for (const c of ['flex-1', 'min-h-0', 'overflow-y-auto']) expect(box.classList.contains(c)).toBe(true)

  // The content renders INSIDE it.
  const content = w.find(`[data-testid="${contentTestid}"]`)
  expect(content.exists()).toBe(true)
  expect(box.contains(content.element)).toBe(true)

  // It is the containing block of everything absolutely positioned inside it
  // (the markdown's sr-only live region): no such element resolves against an
  // ancestor OUTSIDE the scroll box, where it would stretch the page.
  expect(isPositioned(box)).toBe(true)
  const escapees = [...box.querySelectorAll('.sr-only, .absolute')].filter((el) => {
    let p = el.parentElement
    while (p && p !== box && !isPositioned(p)) p = p.parentElement
    return p !== box && !(p && box.contains(p))
  })
  expect(escapees).toEqual([])

  // The min-h-0 chain from the Inbox root down to the scroll box is unbroken,
  // so the box gets a bounded height to scroll within.
  const pane = w.find('[data-testid="inbox-pane"]').element
  expect(pane.contains(box)).toBe(true)
  expect(pane.classList.contains('min-h-0')).toBe(true)
  const column = pane.parentElement
  expect(column.classList.contains('min-h-0')).toBe(true)
  expect(w.find('[data-testid="inbox"]').element.classList.contains('min-h-0')).toBe(true)
}

describe('#4160: a room in the reading pane stays inside the pane', () => {
  for (const tab of ['unread', 'all']) {
    it(`a room with one very long message (${tab})`, async () => {
      store.fetchRoom = vi.fn(async (id) => roomDetail(id, [msg('m1', 1, { content: LONG })]))
      const threads = [room('r1', { unread: 1 })]
      const previews = { 'room:r1': { latest: { kind: 'message', id: 'm1', at: iso(6), excerpt: 'Paragraph 0.' }, first_unread_message_id: 'm1' } }
      const w = await mountInbox({ threads, previews }, { tab, item: 'room:r1' })
      expect(w.find('[data-testid="inbox-pane-message-m1"]').exists()).toBe(true)
      expectContained(w, 'inbox-pane-room')
    })
  }

  it('a room with many short messages', async () => {
    const many = Array.from({ length: 40 }, (_, i) => msg(`m${i + 1}`, i + 1, { sender_identity: i % 2 ? 'bo' : 'ada' }))
    store.fetchRoom = vi.fn(async (id) => roomDetail(id, many))
    const threads = [room('r1', { unread: 40 })]
    const previews = { 'room:r1': { latest: { kind: 'message', id: 'm40', at: iso(6), excerpt: 'say m40' }, first_unread_message_id: 'm1' } }
    const w = await mountInbox({ threads, previews }, { tab: 'unread', item: 'room:r1' })
    expect(w.find('[data-testid="inbox-pane-message-m40"]').exists()).toBe(true)
    expectContained(w, 'inbox-pane-room')
  })

  it('a thread in the same pane holds to the same contract', async () => {
    store.fetchHistory = vi.fn(async () => ({ messages: [
      { id: 'h1', role: 'assistant', content: LONG, timestamp: iso(6) },
    ] }))
    const threads = [thread('t1', { unread: 1 })]
    const w = await mountInbox({ threads }, { tab: 'unread', item: 'thread:t1' })
    expect(w.find('[data-testid="inbox-pane-message-h1"]').exists()).toBe(true)
    expectContained(w, 'inbox-pane-body')
  })
})
