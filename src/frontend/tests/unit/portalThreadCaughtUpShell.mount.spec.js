// @vitest-environment jsdom
/**
 * #3439 — the shell's half: what happens when the open conversation says it
 * caught up, and what happens when the tab comes back.
 *
 * `PortalConversation` emits `thread-caught-up` with the thread id once it has
 * appended rows the agent wrote on its own. The shell marks that thread read —
 * ONLY if it is the one on screen — and does nothing else: not the list
 * refresh, not the #2579 title-settle cycle that `sessions-changed` drives.
 * And coming back to a hidden tab runs the 20 s tick body at once.
 *
 * Mounted (#2918): the real Portal shell (shallow), store stubbed at the store
 * seam — the harness of `portalAgentLandingStage.mount.spec.js`. These pin the
 * production wiring (the `@thread-caught-up` binding, the `visibilitychange`
 * listener): delete either line in `Portal.vue` and a test here goes red.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { defineComponent, h } from 'vue'

// jsdom has no `matchMedia`; the landing's focus rule asks it. Every query
// answers `false` here — which device the shell is on is not what these pin.
vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, userEmail: 'me@example.com', logout: vi.fn() }),
}))

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

import { useClientPortalStore } from '@/stores/clientPortal'
import Portal from '@/views/Portal.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const Blank = { template: '<div />' }
function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/workspace', component: Blank },
      { path: '/workspace/inbox', component: Blank },
      { path: '/workspace/c/:sessionId', component: Blank },
      { path: '/workspace/r/:roomId', component: Blank },
      { path: '/workspace/a/:agentName', component: Blank },
      { path: '/login', component: Blank },
    ],
  })
}

const OPEN = { id: 't1', session_id: 't1', agent_name: 'scout', is_main: true, message_count: 2, last_message_at: '2026-10-26T10:00:00Z', unread: 0 }
const OTHER = { id: 'g1', session_id: 'g1', agent_name: 'sage', is_main: true, message_count: 4, last_message_at: '2026-10-26T09:00:00Z', unread: 1 }
const THREADS = [OPEN, OTHER]

const ConversationStub = defineComponent({
  name: 'PortalConversation',
  props: ['agent', 'sessionId', 'newChat', 'threads', 'focusOnMount', 'prefill', 'tabOrder'],
  setup(_, { expose }) {
    expose({ focusComposer: vi.fn(), startVoiceCall: vi.fn(), endVoiceCall: vi.fn() })
    return () => h('div')
  },
})

let store
let mounted = []
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.fetchRoster = vi.fn(async () => {
    store.agents = [{ name: 'scout' }, { name: 'sage' }]
    store.error = null
    store.rosterLoaded = true
  })
  store.fetchAllSessions = vi.fn(async () => { store.sessionsFailed = false; return THREADS })
  store.fetchChatState = vi.fn(async (opts) => (opts ? { state: {}, previews: {} } : {}))
  store.fetchSessions = vi.fn(async () => THREADS)
  store.fetchAsks = vi.fn(async () => [])
  store.fetchMentions = vi.fn(async () => [])
  store.seedAgentRecency = vi.fn()
  store.ensureBriefing = vi.fn()
  store.loadAgentSuggestions = vi.fn()
  store.markChatRead = vi.fn(async () => {})
  store.markChatReadStrict = vi.fn(async () => {})
})
afterEach(() => { mounted.forEach((w) => w.unmount()); mounted = [] })

async function boot(path) {
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const w = shallowMount(Portal, {
    global: { plugins: [router], stubs: { PortalConversation: ConversationStub } },
  })
  mounted.push(w)
  await flushPromises(); await flushPromises()
  return w
}

const conversation = (w) => w.findComponent({ name: 'PortalConversation' })

describe('#3439 — the shell on `thread-caught-up`', () => {
  it('marks the open thread read, once, and re-reads nothing else', async () => {
    const w = await boot('/workspace/c/t1')
    store.markChatReadStrict.mockClear()
    const lists = store.fetchAllSessions.mock.calls.length
    conversation(w).vm.$emit('thread-caught-up', 't1')
    await flushPromises()
    expect(store.markChatReadStrict).toHaveBeenCalledTimes(1)
    expect(store.markChatReadStrict).toHaveBeenCalledWith('thread', 't1')
    expect(store.fetchAllSessions).toHaveBeenCalledTimes(lists)   // not a turn: no list refresh, no settle cycle
  })

  it('leaves a thread the person is NOT looking at unread', async () => {
    const w = await boot('/workspace/c/t1')
    store.markChatReadStrict.mockClear()
    conversation(w).vm.$emit('thread-caught-up', 'g1')
    await flushPromises()
    expect(store.markChatReadStrict).not.toHaveBeenCalled()
  })
})

describe('#3439 — coming back to the tab', () => {
  it('runs the tick body at once instead of waiting for the interval', async () => {
    await boot('/workspace/c/t1')
    const lists = store.fetchAllSessions.mock.calls.length
    const asks = store.fetchAsks.mock.calls.length
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(store.fetchAllSessions).toHaveBeenCalledTimes(lists + 1)
    expect(store.fetchAsks).toHaveBeenCalledTimes(asks + 1)
  })

  it('does nothing when the tab went hidden', async () => {
    await boot('/workspace/c/t1')
    const lists = store.fetchAllSessions.mock.calls.length
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(store.fetchAllSessions).toHaveBeenCalledTimes(lists)
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
  })

  it('stops listening once the shell is gone', async () => {
    const w = await boot('/workspace/c/t1')
    w.unmount(); mounted = []
    const lists = store.fetchAllSessions.mock.calls.length
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(store.fetchAllSessions).toHaveBeenCalledTimes(lists)
  })
})
