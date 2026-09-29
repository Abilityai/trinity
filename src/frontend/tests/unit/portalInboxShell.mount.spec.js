// @vitest-environment jsdom
/**
 * trinity-enterprise#610 (D9/D10) — the Inbox inside the Workspace SHELL.
 *
 * `Portal.vue` is mounted for real (shallow: every child is a stub, so the
 * stage-branch chain, the landing replace and the `activeAgent` rule are the
 * shell's own, not a copy). What it pins:
 *
 *   - bare `/workspace` lands on `/workspace/inbox` before the stage resolves,
 *     and an explicit target (`?agent=`, `/c/:id`) does not;
 *   - on the Inbox route with a roster ERROR the error copy renders, not the
 *     Inbox; with an empty roster, "No agents here yet" (the ent#253 class: a
 *     branch must never render under another's verdict);
 *   - `PortalConversation` is NOT mounted on the Inbox route;
 *   - selecting an item scopes the rail to that agent WITHOUT writing
 *     `activeAgentName` — so no `ensureMainListed` → no `fetchSessions`, which
 *     would mint a Main for that agent.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

// Before any import: a charting dependency reads `matchMedia` at load time,
// and jsdom has none. Desktop width (not a phone).
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
import { railColumnReservedFor, railVisibleFor } from '@/components/portal/portalRail'

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
      { path: '/operations', component: Blank },
      { path: '/login', component: Blank },
    ],
  })
}

const THREADS = [
  { id: 't1', session_id: 't1', agent_name: 'scout', is_main: true, last_message_at: '2026-09-27T10:00:00Z', unread: 0 },
]

let store
function arm({ agents = [{ name: 'scout' }, { name: 'sage' }], rosterError = null } = {}) {
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.fetchRoster = vi.fn(async () => {
    store.agents = rosterError ? [] : agents
    store.error = rosterError
    store.rosterLoaded = true
  })
  store.fetchAllSessions = vi.fn(async () => { store.sessionsFailed = false; return THREADS })
  store.fetchChatState = vi.fn(async (opts) => (opts ? { state: { 'thread:t1': { unread: 0 } }, previews: {} } : {}))
  store.fetchSessions = vi.fn(async () => [])
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

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

describe('D9 — the landing', () => {
  it('bare /workspace lands on the Inbox and mounts it, not a conversation', async () => {
    arm()
    const { w, router } = await boot('/workspace')
    expect(router.currentRoute.value.path).toBe('/workspace/inbox')
    expect(w.findComponent({ name: 'PortalInbox' }).exists()).toBe(true)
    expect(w.findComponent({ name: 'PortalConversation' }).exists()).toBe(false)
  })

  it('an explicit ?agent= target is not redirected to the Inbox', async () => {
    arm()
    const { router } = await boot('/workspace?agent=scout')
    expect(router.currentRoute.value.path).not.toBe('/workspace/inbox')
  })

  it('a thread URL is not redirected to the Inbox', async () => {
    arm()
    const { router } = await boot('/workspace/c/t1')
    expect(router.currentRoute.value.path).toBe('/workspace/c/t1')
  })

  it('the Inbox read asks chat-state WITH previews, from the same call', async () => {
    arm()
    await boot('/workspace/inbox')
    expect(store.fetchChatState).toHaveBeenCalledWith({ previews: true })
  })
})

describe('D9 — the stage chain on the Inbox route (ent#253)', () => {
  it('a roster error renders the error copy, not the Inbox', async () => {
    arm({ rosterError: 'Something broke' })
    const { w } = await boot('/workspace/inbox')
    expect(w.findComponent({ name: 'PortalInbox' }).exists()).toBe(false)
    expect(w.text()).toContain('Something broke')
  })

  it('an empty roster says "No agents here yet", not the Inbox', async () => {
    arm({ agents: [] })
    const { w } = await boot('/workspace/inbox')
    expect(w.findComponent({ name: 'PortalInbox' }).exists()).toBe(false)
    expect(w.text()).toMatch(/No agents (here|shared with you) yet/)
  })
})

describe('D10 — selection scopes the rail, never activeAgentName', () => {
  it('selecting an item never calls fetchSessions (no ensureMainListed → no Main minted)', async () => {
    arm()
    const { w, router } = await boot('/workspace/inbox')
    store.fetchSessions.mockClear()
    // An ask from `sage`, an agent with NO Main yet — exactly the pair a write
    // to `activeAgentName` would mint one for.
    store.asks = [{ id: 'a1', agent_name: 'sage', status: 'pending', title: 'Ok?', created_at: '2026-09-27T10:00:00Z' }]
    await router.replace({ path: '/workspace/inbox', query: { tab: 'action', item: 'ask:a1' } })
    await flushPromises()
    expect(store.fetchSessions).not.toHaveBeenCalled()
    // The conversation still is not mounted: the Inbox branch precedes it.
    expect(w.findComponent({ name: 'PortalConversation' }).exists()).toBe(false)
    // …and the rail follows the selected agent (the rail stub renders only
    // while there is a column for it).
    expect(w.findComponent({ name: 'PortalRail' }).exists()).toBe(true)
  })

  it('no selection → no rail, and the reserved column is only held while loading', async () => {
    arm()
    const { w } = await boot('/workspace/inbox')
    expect(w.findComponent({ name: 'PortalRail' }).exists()).toBe(false)
    // The column is released through the existing helper: reserved while the
    // stage loads, not once it is ready with nothing selected.
    expect(railColumnReservedFor({ stageState: 'loading' })).toBe(true)
    expect(railColumnReservedFor({ stageState: 'ready' })).toBe(false)
    expect(railVisibleFor({ stageState: 'ready', activeAgent: null })).toBe(false)
  })
})

describe('D13 twin — a failed previews read is not silent', () => {
  const inbox = (w) => w.findComponent({ name: 'PortalInbox' })

  it('a first previews read that fails is a load failure, not an empty Inbox', async () => {
    arm()
    store.fetchChatState = vi.fn(async () => { throw new Error('503') })
    const { w } = await boot('/workspace/inbox')
    expect(inbox(w).props('threadsLoaded')).toBe(false)
    expect(inbox(w).props('threadsFailed')).toBe(true)
  })

  it('a failed refresh after a good read raises the stale flag; the next good read clears it', async () => {
    arm()
    const { w } = await boot('/workspace/inbox')
    expect(inbox(w).props('threadsFailed')).toBe(false)
    const good = store.fetchChatState
    store.fetchChatState = vi.fn(async () => { throw new Error('503') })
    inbox(w).vm.$emit('refresh'); await flushPromises()
    expect(inbox(w).props('threadsLoaded')).toBe(true)
    expect(inbox(w).props('threadsFailed')).toBe(true)
    store.fetchChatState = good
    inbox(w).vm.$emit('refresh'); await flushPromises()
    expect(inbox(w).props('threadsFailed')).toBe(false)
  })
})

describe('§3g S4 — a failed read write is rolled back', () => {
  async function bootUnread(strict) {
    arm()
    store.fetchChatState = vi.fn(async (opts) => (opts
      ? { state: { 'thread:t1': { kind: 'thread', id: 't1', unread: 3 } }, previews: {} }
      : { 'thread:t1': { kind: 'thread', id: 't1', unread: 3 } }))
    store.markChatReadStrict = vi.fn(strict)
    const { w } = await boot('/workspace/inbox?tab=unread')
    const inbox = w.findComponent({ name: 'PortalInbox' })
    const unreadOf = () => (inbox.props('threads').find((t) => t.id === 't1') || {}).unread
    return { w, inbox, unreadOf }
  }

  it('restores the count and resolves false — it never rejects', async () => {
    const { inbox, unreadOf } = await bootUnread(async () => { throw new Error('500') })
    expect(unreadOf()).toBe(3)
    const readFn = inbox.props('markRead')
    let settled
    if (readFn) settled = readFn('thread', 't1')
    else inbox.vm.$emit('mark-read', 'thread', 't1')
    await flushPromises()
    expect(store.markChatReadStrict).toHaveBeenCalledWith('thread', 't1')
    expect(unreadOf()).toBe(3)
    if (settled) await expect(settled).resolves.toBe(false)
  })

  it('a write that lands keeps the zero and resolves true', async () => {
    const { inbox, unreadOf } = await bootUnread(async () => {})
    const readFn = inbox.props('markRead')
    let settled
    if (readFn) settled = readFn('thread', 't1')
    else inbox.vm.$emit('mark-read', 'thread', 't1')
    await flushPromises()
    expect(unreadOf()).toBe(0)
    if (settled) await expect(settled).resolves.toBe(true)
  })
})
