// @vitest-environment jsdom
/**
 * #4515 — accepting the terms inside the Workspace must reload it.
 *
 * A portal session refused by the terms gate sees `PortalTerms` in place of
 * the roster (`Portal.vue`, `v-else-if="store.termsRefusal && …"`). Accepting
 * is meant to re-bootstrap the shell (`@accepted="bootstrap()"`). It did not:
 * the store's `acceptTerms()` cleared `termsRefusal`, the scheduler flush that
 * ran before `PortalTerms` resumed after its `await` unmounted the screen, and
 * Vue drops an `emit()` from an unmounted instance — so no roster read, no
 * thread read, and the refusal's message left on the stage as "Couldn't load
 * your agents — Accept the current terms to continue."
 *
 * The other mount specs drive `PortalTerms` alone (it emits) or emit
 * `accepted` by hand on a still-mounted screen, so neither sees that order.
 * This one mounts `Portal.vue` with the REAL `PortalTerms`, ticks the box,
 * clicks Accept, and runs the store's real `acceptTerms` / `fetchRoster` /
 * `fetchAllSessions` / `fetchChatState` against one stub adapter that refuses
 * every gated call until the accept is recorded — the server's behaviour.
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
  useAuthStore: () => ({ isAuthenticated: false, authHeader: {}, userEmail: null, logout: vi.fn() }),
}))

import { portalHttp, useClientPortalStore } from '@/stores/clientPortal'
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
      { path: '/terms', component: Blank },
      { path: '/login', component: Blank },
    ],
  })
}

const TERMS = { code: 'terms_required', version: '2026-11', message: 'Accept the current terms to continue.' }
const CURRENT = { required: true, version: '2026-11', title: 'Terms of use', text: 'Be kind.', accepted: false, is_default: true }
const ROSTER = { client_email: 'client@example.com', agents: [{ name: 'scout', display_name: 'Scout' }] }

// Every request, in order, with whether it was sent before or after the accept.
const calls = []
let accepted = false

function ok(config, data) { return { status: 200, data, headers: {}, config, statusText: 'OK' } }
function refused(config) {
  const error = new Error('HTTP 403')
  error.config = config
  error.response = { status: 403, data: { detail: TERMS }, headers: {}, config }
  throw error
}

function serve() {
  portalHttp.defaults.adapter = async (config) => {
    const key = `${(config.method || 'get').toLowerCase()} ${config.url}`
    calls.push({ key, afterAccept: accepted })
    // The terms routes are reachable before acceptance; nothing else is.
    if (key === 'get /api/terms/current') return ok(config, CURRENT)
    if (key === 'post /api/terms/accept') {
      accepted = true
      return ok(config, { accepted: true, created: true, version: CURRENT.version, accepted_at: '2026-11-13T00:00:00Z', landing: null })
    }
    if (!accepted) return refused(config)
    if (key === 'get /api/enterprise/client-portal/my-agents') return ok(config, ROSTER)
    if (key === 'get /api/enterprise/client-portal/sessions') return ok(config, { sessions: [] })
    if (key === 'get /api/enterprise/client-portal/chat-state') return ok(config, { chats: [], as_of: '2026-11-13T00:00:00Z' })
    if (/\/(asks|mentions)$/.test(config.url)) return ok(config, [])
    return ok(config, {})
  }
}

const sentAfterAccept = (key) => calls.filter((c) => c.afterAccept && c.key === key).length

let store
beforeEach(() => {
  localStorage.clear()
  calls.length = 0
  accepted = false
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.portalToken = 'portal-tok'   // a PORTAL session: the terms screen renders in place
  serve()
})
afterEach(() => { delete portalHttp.defaults.adapter })

async function bootRefused() {
  const router = makeRouter()
  await router.push('/workspace')
  await router.isReady()
  const w = shallowMount(Portal, {
    global: {
      plugins: [router],
      // The real screen and the primitives the click goes through.
      stubs: { RouterLink: false, PortalTerms: false, BaseCard: false, BaseButton: false },
    },
  })
  await flushPromises(); await flushPromises()
  return { w, router }
}

async function accept(w) {
  await w.get('[data-testid="portal-terms-agree"]').setValue(true)
  await w.get('[data-testid="portal-terms-accept"]').trigger('click')
  await flushPromises(); await flushPromises(); await flushPromises()
}

describe('#4515 accepting the terms in the Workspace reloads it', () => {
  it('starts on the terms screen: the roster was refused by the gate', async () => {
    const { w } = await bootRefused()
    expect(w.find('[data-testid="portal-terms"]').exists()).toBe(true)
    expect(store.termsRefusal).toBe('terms_required')
    expect(sentAfterAccept('get /api/enterprise/client-portal/my-agents')).toBe(0)
  })

  it('after a successful accept the roster is read again and the agent appears, with no stale refusal', async () => {
    const { w } = await bootRefused()
    await accept(w)

    expect(calls.some((c) => c.key === 'post /api/terms/accept')).toBe(true)
    // The reload happened: the roster was fetched after the accept was recorded.
    expect(sentAfterAccept('get /api/enterprise/client-portal/my-agents')).toBeGreaterThan(0)
    expect(store.agents.map((a) => a.name)).toEqual(['scout'])
    // Nothing of the refusal survives the accept.
    expect(store.termsRefusal).toBe(null)
    expect(store.error).toBe(null)
    expect(w.find('[data-testid="portal-terms"]').exists()).toBe(false)
    const text = w.text()
    expect(text).not.toContain("Couldn't load your agents")
    expect(text).not.toContain(TERMS.message)
    // The sidebar is handed the roster, not an empty list.
    const sidebar = w.findAllComponents({ name: 'PortalSidebar' })[0]
    expect(sidebar.props('roster').map((a) => a.name)).toEqual(['scout'])
  })

  it('after a successful accept the chats are read again, so the Inbox carries no failure from the refused reads', async () => {
    const { w, router } = await bootRefused()
    // The refused first bootstrap lands on the Inbox and its chat reads fail.
    expect(router.currentRoute.value.path).toBe('/workspace/inbox')
    await accept(w)

    expect(sentAfterAccept('get /api/enterprise/client-portal/sessions')).toBeGreaterThan(0)
    expect(sentAfterAccept('get /api/enterprise/client-portal/chat-state')).toBeGreaterThan(0)
    expect(store.sessionsFailed).toBe(false)
    const inbox = w.findComponent({ name: 'PortalInbox' })
    expect(inbox.exists()).toBe(true)
    expect(inbox.props('threadsFailed')).toBe(false)
    expect(inbox.props('threadsLoaded')).toBe(true)
  })
})
