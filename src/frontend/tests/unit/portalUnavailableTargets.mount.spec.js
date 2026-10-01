// @vitest-environment jsdom
/**
 * #3140 — a Workspace URL the viewer cannot open must not offer a live composer.
 *
 *   - `/workspace/c/<an id that is not yours>`: the shell used to fall back to
 *     the FIRST roster agent, so the page showed an empty chat of one of your
 *     own agents, with a working composer, under someone else's chat id.
 *   - `/workspace/a/<an agent not shared with you>`: "You don't have access"
 *     beside "Start a conversation below." and a live composer, plus a
 *     "Try again" that cannot help.
 *
 * Both now render a stage that says what happened and offers a way back, and
 * nothing else. `Portal.vue` is mounted for real (shallow — the harness of
 * `portalInboxShell.mount.spec.js`), so the stage chain and the
 * `activeAgent` rule under test are the shell's own.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { shallowMount, mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { defineComponent, h, ref } from 'vue'

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
import { usePortalAgentPage, clearPortalAgentPageCache } from '@/composables/usePortalAgentPage'

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

const THREADS = [
  { id: 't1', session_id: 't1', agent_name: 'scout', is_main: true, last_message_at: '2026-09-27T10:00:00Z', unread: 0 },
]

let store
function arm({ sessionsFail = false } = {}) {
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.fetchRoster = vi.fn(async () => {
    store.agents = [{ name: 'scout' }, { name: 'sage' }]
    store.error = null
    store.rosterLoaded = true
  })
  store.fetchAllSessions = vi.fn(async () => {
    store.sessionsFailed = sessionsFail
    return sessionsFail ? [] : THREADS
  })
  store.fetchChatState = vi.fn(async (opts) => (opts ? { state: { 'thread:t1': { unread: 0 } }, previews: {} } : {}))
  store.fetchSessions = vi.fn(async () => THREADS)
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

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

describe('#3140 — a chat URL that is not yours', () => {
  it('says the chat is not available, with a way back and no conversation', async () => {
    arm()
    const { w, router } = await boot('/workspace/c/someone-elses')
    const stage = w.get('[data-testid="chat-unavailable"]')
    expect(stage.text()).toContain("This chat isn't available")
    expect(conversation(w).exists()).toBe(false)
    expect(w.find('textarea').exists()).toBe(false)

    await w.get('[data-testid="chat-unavailable-back"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).not.toBe('/workspace/c/someone-elses')
    expect(w.find('[data-testid="chat-unavailable"]').exists()).toBe(false)
  })

  it('never reads its history or marks it read', async () => {
    arm()
    await boot('/workspace/c/someone-elses')
    expect(store.markChatRead).not.toHaveBeenCalledWith(expect.anything(), 'someone-elses')
  })

  it('your own chat still opens as a conversation', async () => {
    arm()
    const { w } = await boot('/workspace/c/t1')
    expect(conversation(w).exists()).toBe(true)
    expect(w.find('[data-testid="chat-unavailable"]').exists()).toBe(false)
  })

  it('in-app navigation to an unknown id lands on the same state', async () => {
    arm()
    const { w, router } = await boot('/workspace/c/t1')
    await router.push('/workspace/c/not-mine')
    await flushPromises()
    expect(w.find('[data-testid="chat-unavailable"]').exists()).toBe(true)
    expect(conversation(w).exists()).toBe(false)
  })

  it('when the list failed, the conversation decides — and its 404 lands on the same state', async () => {
    arm({ sessionsFail: true })
    const { w } = await boot('/workspace/c/unknown')
    // No verdict from a failed list: the conversation is allowed to try.
    expect(conversation(w).exists()).toBe(true)
    conversation(w).vm.$emit('thread-missing', 'unknown')
    await flushPromises()
    expect(w.find('[data-testid="chat-unavailable"]').exists()).toBe(true)
    expect(conversation(w).exists()).toBe(false)
  })

  it('a late 404 for a chat the person already left changes nothing', async () => {
    arm({ sessionsFail: true })
    const { w, router } = await boot('/workspace/c/unknown')
    const conv = conversation(w).vm
    await router.push('/workspace/c/t1')
    await flushPromises()
    conv.$emit('thread-missing', 'unknown')
    await flushPromises()
    expect(w.find('[data-testid="chat-unavailable"]').exists()).toBe(false)
  })
})

describe('#3140 — an agent URL that is not shared with you', () => {
  it("says you don't have access, with a way back and no conversation", async () => {
    arm()
    const { w, router } = await boot('/workspace/a/stranger')
    expect(w.text()).toContain("You don't have access to")
    expect(w.text()).toContain('stranger')
    expect(conversation(w).exists()).toBe(false)
    expect(w.find('textarea').exists()).toBe(false)
    expect(w.text()).not.toContain('Try again')

    await w.get('[data-testid="agent-unreachable-back"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).not.toBe('/workspace/a/stranger')
    expect(w.text()).not.toContain("You don't have access to")
  })

  it('an agent on your roster still opens', async () => {
    arm()
    const { w, router } = await boot('/workspace/a/scout')
    expect(w.text()).not.toContain("You don't have access to")
    expect(router.currentRoute.value.path).toBe('/workspace/c/t1')
  })
})

describe('#3140 — a refused agent page offers no retry', () => {
  // The band and the details panel read `denied` from this composable.
  function harness(status) {
    setActivePinia(createPinia())
    const s = useClientPortalStore()
    s.fetchAgentPage = vi.fn(async () => { throw Object.assign(new Error('x'), { response: { status } }) })
    clearPortalAgentPageCache()
    let api
    const C = defineComponent({ setup() { api = usePortalAgentPage(ref('stranger'), ref('7d')); return () => h('div') } })
    mount(C)
    return () => api
  }

  it.each([[403], [404]])('%s → denied, so the surfaces withhold "Try again"', async (status) => {
    const api = harness(status)
    await flushPromises()
    expect(api().denied.value).toBe(true)
    expect(api().error.value).toBe("You don't have access to this agent.")
  })

  it('a 5xx is not denied — retrying can help', async () => {
    const api = harness(503)
    await flushPromises()
    expect(api().denied.value).toBe(false)
  })
})
