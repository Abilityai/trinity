// @vitest-environment jsdom
/**
 * ent#784 [I1] — a thread-list refresh must not re-mint a fresh chat over a live one.
 *
 * ent#784 made `/workspace/a/:name` LAND on a fresh chat and STAY on that URL.
 * The watcher that resolves that route fires on the route param AND on the
 * thread list arriving (a cold deep link gets the name long before the
 * threads), so once landed, every later list refresh re-fires it with the same
 * name. `landOnAgent`'s own idempotency guard keys on `startingNewChat`, which
 * the first send clears (`onSessionAdopted`) — so a refresh settling in the
 * window between adoption and the route leaving `/a/:name` nulled
 * `pendingSession` and bumped `convGen`, remounting an empty composer over the
 * thread whose first reply was streaming. `ensureMainListed` ends in
 * `refreshThreads()`, and `onSessionAdopted` itself calls it, so that window is
 * reached by the ordinary first send, not by a contrived one.
 *
 * The guard is keyed on "the route did not change on this fire" — deliberately
 * NOT on `pendingSession`, because back/forward from `/workspace/c/:id` to
 * `/workspace/a/:name` legitimately arrives with a session set and must still
 * land fresh (ent#784 T4).
 *
 * `Portal.vue` is mounted for real (shallow — the harness of
 * `portalUnavailableTargets.mount.spec.js`), so the watcher, the guard and the
 * conversation's props are the shell's own.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

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

const T1 = { id: 't1', session_id: 't1', agent_name: 'scout', is_main: true, last_message_at: '2026-09-27T10:00:00Z', unread: 0 }

let store
function arm(threads = [T1]) {
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.fetchRoster = vi.fn(async () => {
    store.agents = [{ name: 'scout' }, { name: 'sage' }]
    store.error = null
    store.rosterLoaded = true
  })
  store.fetchAllSessions = vi.fn(async () => { store.sessionsFailed = false; return threads })
  store.fetchChatState = vi.fn(async (opts) => (opts ? { state: {}, previews: {} } : {}))
  store.fetchSessions = vi.fn(async () => threads)
  store.fetchAsks = vi.fn(async () => [])
  store.seedAgentRecency = vi.fn()
  store.ensureBriefing = vi.fn()
  store.loadAgentSuggestions = vi.fn()
  store.markChatRead = vi.fn(async () => {})
}

async function boot(path) {
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const w = shallowMount(Portal, { global: { plugins: [router] } })
  await flushPromises(); await flushPromises()
  return { w, router }
}

const conversation = (w) => w.findComponent({ name: 'PortalConversation' })
// What the conversation is actually showing: the session it was handed, whether
// it is a fresh chat, and the instance identity (a `convGen` bump changes
// `:key`, so a remount is a NEW component instance).
const showing = (w) => {
  const c = conversation(w)
  return { session: c.props('sessionId'), newChat: c.props('newChat'), agent: c.props('agent')?.name, vm: c.vm }
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

describe('ent#784 — landing on an agent page is idempotent against the thread list', () => {
  it('a list refresh after the first send keeps the adopted chat (I1)', async () => {
    arm()
    const { w, router } = await boot('/workspace/a/scout')
    expect(showing(w).newChat).toBe(true)
    expect(showing(w).session).toBe(null)

    // The first send adopts a real id. `onSessionAdopted` writes the state
    // synchronously and THEN replaces the route; stub the replace so the window
    // this finding is about — adopted, but still on `/workspace/a/scout` —
    // stays open deterministically instead of depending on microtask order.
    vi.spyOn(router, 'replace').mockResolvedValue(undefined)
    conversation(w).vm.$emit('session-adopted', 'new1')
    await flushPromises()
    const adopted = showing(w)
    expect(adopted.session).toBe('new1')
    expect(adopted.newChat).toBe(false)
    expect(router.currentRoute.value.path).toBe('/workspace/a/scout')

    // The list refresh that the adoption itself kicks off lands in that window.
    store.fetchAllSessions = vi.fn(async () => {
      store.sessionsFailed = false
      return [T1, { id: 'new1', session_id: 'new1', agent_name: 'scout', last_message_at: '2026-09-28T10:00:00Z', unread: 0 }]
    })
    await w.vm.refreshThreads()
    await flushPromises()

    const after = showing(w)
    expect(after.session).toBe('new1')
    expect(after.newChat).toBe(false)
    expect(after.vm).toBe(adopted.vm)   // no remount
  })

  it('a list refresh while still composing keeps the one unsent chat', async () => {
    // The pre-existing guard's case, re-asserted behaviourally: nothing adopted
    // yet, so a refresh must not throw away what is being typed either.
    arm()
    const { w } = await boot('/workspace/a/scout')
    const before = showing(w)
    store.fetchAllSessions = vi.fn(async () => {
      store.sessionsFailed = false
      return [T1, { id: 't2', session_id: 't2', agent_name: 'sage', last_message_at: '2026-09-28T10:00:00Z', unread: 0 }]
    })
    await w.vm.refreshThreads()
    await flushPromises()
    expect(showing(w).vm).toBe(before.vm)
    expect(showing(w).newChat).toBe(true)
  })

  it('back to the agent page from a chat of the SAME agent still lands fresh (T4)', async () => {
    arm()
    const { w, router } = await boot('/workspace/a/scout')
    await router.push('/workspace/c/t1')
    await flushPromises()
    expect(showing(w).session).toBe('t1')
    expect(showing(w).newChat).toBe(false)

    // Back. Arrives with `pendingSession` set — which is exactly why the guard
    // must not key on it.
    await router.push('/workspace/a/scout')
    await flushPromises()
    expect(showing(w).newChat).toBe(true)
    expect(showing(w).session).toBe(null)
  })

  it('a cold deep link whose threads arrive after mount still lands', async () => {
    // The watcher's reason for watching the list at all: on a cold load the
    // route param is already in place, so the ONLY fire is the list arriving —
    // with the name unchanged. A guard keyed on the name alone would swallow it.
    arm()
    const { w } = await boot('/workspace/a/sage')
    const seen = showing(w)
    expect(seen.agent).toBe('sage')       // not the `store.agents[0]` fallback
    expect(seen.newChat).toBe(true)
    expect(seen.session).toBe(null)
  })
})
