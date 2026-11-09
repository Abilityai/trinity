// @vitest-environment jsdom
/**
 * ent#1201 — the Workspace stage while a member's agent is being set up.
 *
 * The first roster read after accepting can be empty: the agent is still being
 * seated or started. The roster then says `agent_setup_pending`, and the stage
 * shows one line — "Your assistant is being set up" — where the conversation
 * will mount, instead of "No agents shared with you yet". The shell re-reads the
 * roster quietly every 3 s until the flag clears, for at most 3 minutes, after
 * which it says it is taking longer. A flip of a member's progress step
 * re-reads the roster once.
 *
 * `Portal.vue` mounted for real (shallow — the harness of
 * `portalAgentLandingStage.mount.spec.js`).
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
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, userEmail: 'me@example.com', logout: vi.fn() }),
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
import { usePortalProgressStore } from '@/stores/portalProgress'
import Portal from '@/views/Portal.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const Blank = { template: '<div />' }
let store
let mounted = []

function arm({ pending = true } = {}) {
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.fetchRoster = vi.fn(async () => {
    store.agents = []
    store.agentSetupPending = pending
    store.error = null
    store.rosterLoaded = true
  })
  store.refreshRoster = vi.fn(async () => {})
  store.fetchAllSessions = vi.fn(async () => { store.sessionsFailed = false; return [] })
  store.fetchChatState = vi.fn(async (opts) => (opts ? { state: {}, previews: {} } : {}))
  store.fetchSessions = vi.fn(async () => [])
  store.fetchAsks = vi.fn(async () => [])
  store.seedAgentRecency = vi.fn()
  store.ensureBriefing = vi.fn()
  store.loadAgentSuggestions = vi.fn()
  store.markChatRead = vi.fn(async () => {})
}

async function boot(opts) {
  arm(opts)
  const router = createRouter({
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
  await router.push('/workspace')
  await router.isReady()
  const w = shallowMount(Portal, { global: { plugins: [router] } })
  mounted.push(w)
  await flushPromises(); await flushPromises()
  return w
}
const stageLine = (w) => w.find('[data-testid="portal-setup-pending"]')

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  vi.useFakeTimers({ shouldAdvanceTime: true })
})
afterEach(() => {
  for (const w of mounted) w.unmount()
  mounted = []
  vi.useRealTimers()
})

describe('ent#1201 the set-up landing', () => {
  it('says the assistant is being set up instead of "none shared with you"', async () => {
    const w = await boot()
    expect(stageLine(w).exists()).toBe(true)
    expect(stageLine(w).text()).toBe('Your assistant is being set up')
    expect(w.text()).not.toContain('No agents shared with you yet')
    // The sidebar gets the same bit.
    expect(w.findAllComponents({ name: 'PortalSidebar' })[0].props('setupPending')).toBe(true)
  })

  it('without the flag the empty stage is unchanged', async () => {
    const w = await boot({ pending: false })
    expect(stageLine(w).exists()).toBe(false)
    expect(w.text()).toContain('No agents shared with you yet')
  })

  it('re-reads the roster quietly every 3 s and stops when the flag clears', async () => {
    await boot()
    expect(store.refreshRoster).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(3000)
    expect(store.refreshRoster).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(3000)
    expect(store.refreshRoster).toHaveBeenCalledTimes(2)
    store.agentSetupPending = false
    await flushPromises()
    await vi.advanceTimersByTimeAsync(9000)
    expect(store.refreshRoster).toHaveBeenCalledTimes(2)
    // The full fetch ran once, at boot — never again for the poll.
    expect(store.fetchRoster).toHaveBeenCalledTimes(1)
  })

  it('stops after three minutes and says it is taking longer', async () => {
    const w = await boot()
    await vi.advanceTimersByTimeAsync(3 * 60 * 1000 + 3000)
    const calls = store.refreshRoster.mock.calls.length
    expect(calls).toBeGreaterThan(0)
    expect(calls).toBeLessThanOrEqual(60)
    expect(stageLine(w).text()).toBe("This is taking longer than usual — we're looking into it.")
    await vi.advanceTimersByTimeAsync(30000)
    expect(store.refreshRoster.mock.calls.length).toBe(calls)
  })

  it("a member's progress step flipping re-reads the roster once", async () => {
    await boot({ pending: false })
    const progress = usePortalProgressStore()
    progress.byAgent['bob-assistant'] = { loaded: true, steps: [{ key: 'assistant_ready', state: 'in_progress' }] }
    await flushPromises()
    const before = store.refreshRoster.mock.calls.length
    progress.byAgent['bob-assistant'] = { loaded: true, steps: [{ key: 'assistant_ready', state: 'done' }] }
    await flushPromises()
    expect(store.refreshRoster.mock.calls.length).toBe(before + 1)
  })
})
