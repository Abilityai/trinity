// @vitest-environment jsdom
/**
 * #3672 — the shell's sidebar latch (`sidebarReady` in Portal.vue).
 *
 * `portalSidebarCalmLoad.mount.spec.js` proves what the sidebar does with a
 * `settled` prop; this proves the shell feeds it the right one. Portal.vue is
 * mounted for real (shallow — the harness of `portalAgentLandingStage`), and
 * the asks read is held open so the verdict arrives when the test says:
 *
 *   - before the verdict, both sidebars (column and drawer) get `settled=false`;
 *   - the asks verdict latches it to true, ONCE;
 *   - a later drop in the verdict's inputs (a refresh) does not re-show the
 *     skeletons — background refresh is invisible;
 *   - signing out resets it, so the next person's first load holds too.
 *
 * Binding `:settled="true"` (or dropping the latch) turns this red.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})

// NOT a platform session: signing out must actually end the session here.
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: false, authHeader: {}, userEmail: null, logout: vi.fn() }),
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

const THREADS = [{ id: 't1', session_id: 't1', agent_name: 'scout', is_main: true, last_message_at: '2026-09-27T10:00:00Z', unread: 0 }]

let store
let releaseAsks
function arm() {
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.fetchRoster = vi.fn(async () => {
    store.agents = [{ name: 'scout' }]
    store.error = null
    store.rosterLoaded = true
  })
  store.fetchAllSessions = vi.fn(async () => { store.sessionsFailed = false; return THREADS })
  store.fetchChatState = vi.fn(async (opts) => (opts ? { state: {}, previews: {} } : {}))
  store.fetchSessions = vi.fn(async () => THREADS)
  store.fetchMentions = vi.fn(async () => [])
  // The asks read stays out until the test releases it — that IS the verdict.
  store.fetchAsks = vi.fn(() => new Promise((resolve) => {
    releaseAsks = () => { store.asks = []; store.asksLoaded = true; store.asksFailed = false; resolve([]) }
  }))
  store.seedAgentRecency = vi.fn()
  store.ensureBriefing = vi.fn()
  store.loadAgentSuggestions = vi.fn()
  store.markChatRead = vi.fn(async () => {})
}

let mounted = []
async function boot(path = '/workspace/c/t1') {
  arm()
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const w = shallowMount(Portal, { global: { plugins: [router] } })
  mounted.push(w)
  await flushPromises(); await flushPromises()
  return w
}

// Every mounted sidebar's `settled` (the column always; the drawer only while open).
const settledOf = (w) => w.findAllComponents({ name: 'PortalSidebar' }).map((s) => s.props('settled'))

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})
afterEach(() => {
  for (const w of mounted) w.unmount()
  mounted = []
})

describe('#3672 — the shell latches the sidebar verdict', () => {
  it('holds skeletons until the asks verdict, then latches once', async () => {
    const w = await boot()
    expect(store.fetchAsks).toHaveBeenCalled()               // bootstrap is done; asks still out
    expect(settledOf(w)).toEqual([false])
    releaseAsks()
    await flushPromises()
    expect(settledOf(w)).toEqual([true])
  })

  it('a later refresh does not re-show the skeletons', async () => {
    const w = await boot()
    releaseAsks()
    await flushPromises()
    expect(settledOf(w)).toEqual([true])
    // The verdict's inputs drop (as when a refresh is out, or the next
    // bootstrap clears `bootstrapResolved`) — the latch holds.
    store.asksLoaded = false
    store.asksFailed = false
    await flushPromises()
    expect(settledOf(w)).toEqual([true])
  })

  it('the drawer sidebar reads the same latch', async () => {
    const w = await boot()
    w.vm.mobileNav = true
    await flushPromises()
    expect(settledOf(w)).toEqual([false, false])
    releaseAsks()
    await flushPromises()
    expect(settledOf(w)).toEqual([true, true])
  })

  it('signing out resets it, so the next session\'s first load holds again', async () => {
    const w = await boot()
    releaseAsks()
    await flushPromises()
    expect(settledOf(w)).toEqual([true])
    store.signOut()                                          // ends the session: the sign-in form shows
    await flushPromises()
    expect(w.findAllComponents({ name: 'PortalSidebar' })).toHaveLength(0)
    // The next client signs in; its asks have no verdict yet.
    store.portalToken = 'tok-2'
    await flushPromises()
    expect(settledOf(w)).toEqual([false])
    store.asksLoaded = true
    await flushPromises()
    expect(settledOf(w)).toEqual([true])
  })
})
