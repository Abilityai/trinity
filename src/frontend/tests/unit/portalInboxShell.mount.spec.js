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

  it("§3g S5: the Inbox's desktop preview scopes the rail without a ?item=", async () => {
    arm()
    const { w, router } = await boot('/workspace/inbox')
    expect(w.findComponent({ name: 'PortalRail' }).exists()).toBe(false)
    w.findComponent({ name: 'PortalInbox' }).vm.$emit('update:preview', 'thread:t1')
    await flushPromises()
    expect(router.currentRoute.value.query.item).toBeUndefined()
    expect(w.findComponent({ name: 'PortalRail' }).exists()).toBe(true)
  })

  it('§3g A4: the rail is counted before it arrives, and until its column has measured a width', async () => {
    arm()
    const { w } = await boot('/workspace/inbox')
    const inbox = () => w.findComponent({ name: 'PortalInbox' })
    expect(w.findComponent({ name: 'PortalRail' }).exists()).toBe(false)
    expect(inbox().props('railAllowance')).toBeGreaterThanOrEqual(48)
    inbox().vm.$emit('update:preview', 'thread:t1')
    await flushPromises()
    expect(w.findComponent({ name: 'PortalRail' }).exists()).toBe(true)
    // Round 3: a column that has not reported a width (it enters from 0) has
    // lost the Inbox nothing yet — the old "0 once it exists" flipped the list
    // mid-animation. The measured case is the next test.
    expect(inbox().props('railAllowance')).toBeGreaterThanOrEqual(48)
  })

  it('§3g A4 (round 3): the allowance is the width the rail column has not grown into yet', async () => {
    // A controllable ResizeObserver: the test says what each element measures.
    const observers = []
    const Real = globalThis.ResizeObserver
    globalThis.ResizeObserver = class { constructor(cb) { this.cb = cb; this.els = []; observers.push(this) } observe(el) { this.els.push(el) } unobserve() {} disconnect() { this.els = [] } }
    try {
      arm()
      const { w } = await boot('/workspace/inbox')
      const inbox = () => w.findComponent({ name: 'PortalInbox' })
      inbox().vm.$emit('update:preview', 'thread:t1')
      await flushPromises()
      const col = w.find('[data-testid="ws-rail-column"]').element
      const ro = observers.find((o) => o.els.includes(col))
      expect(ro).toBeTruthy() // the column is observed although it mounted late
      const report = async (px) => { ro.cb([{ contentRect: { width: px } }]); await flushPromises() }
      // Collapsed rail (the default): a 48px target.
      await report(0)
      expect(inbox().props('railAllowance')).toBe(48) // just entered: nothing lost yet
      await report(20)
      expect(inbox().props('railAllowance')).toBe(28)
      await report(48)
      expect(inbox().props('railAllowance')).toBe(0)
    } finally {
      globalThis.ResizeObserver = Real
    }
  })

  it('§3g A4 (round 3): an OPEN rail with nothing selected keeps its column, with a way to collapse it', async () => {
    // At 1280 an open rail stacks the Inbox; a stacked Inbox previews nothing;
    // with no agent the rail had no tabs — so the rail, its strip and every
    // control to close it vanished, and `open` stayed saved.
    localStorage.setItem('trinity-workspace-rail', JSON.stringify({ open: true, tab: 'work' }))
    arm()
    const { w } = await boot('/workspace/inbox')
    expect(w.findComponent({ name: 'PortalRail' }).exists()).toBe(false)
    const ph = () => w.findComponent({ name: 'PortalRailPlaceholder' })
    expect(ph().exists()).toBe(true)
    expect(w.find('[data-testid="ws-rail-column"]').exists()).toBe(true)
    // At the OPEN width (the one the Inbox counted), not the collapsed strip.
    expect(w.find('[data-testid="ws-rail-column"]').element.closest('[style*="--ws-rail"]')?.getAttribute('style')).toMatch(/--ws-rail:\s*\d{3}px/)
    ph().vm.$emit('collapse')
    await flushPromises()
    expect(ph().exists()).toBe(false)
    expect(JSON.parse(localStorage.getItem('trinity-workspace-rail')).open).toBe(false)
  })

  it('§3g F4 (round 3): the drawer closes on its Inbox row even when the URL does not change', async () => {
    // A phone LANDS on /workspace/inbox, so tapping the drawer's Inbox row
    // there changes no route and the fullPath watcher never fired.
    arm()
    const { w } = await boot('/workspace/inbox')
    const sidebars = () => w.findAllComponents({ name: 'PortalSidebar' })
    const before = sidebars().length
    w.findComponent({ name: 'PortalInbox' }).vm.$emit('open-menu')
    await flushPromises()
    expect(sidebars().length).toBe(before + 1) // the drawer's copy
    sidebars().at(-1).vm.$emit('open-inbox')
    await flushPromises()
    expect(sidebars().length).toBe(before)
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
    // §3g S5: the Inbox receives the shell's markRead as a function prop.
    const settled = inbox.props('markRead')('thread', 't1')
    await flushPromises()
    expect(store.markChatReadStrict).toHaveBeenCalledWith('thread', 't1')
    expect(unreadOf()).toBe(3)
    await expect(settled).resolves.toBe(false)
  })

  it('a write that lands keeps the zero and resolves true', async () => {
    const { inbox, unreadOf } = await bootUnread(async () => {})
    const settled = inbox.props('markRead')('thread', 't1')
    await flushPromises()
    expect(unreadOf()).toBe(0)
    await expect(settled).resolves.toBe(true)
  })
})

describe('§3g A5 / F4 — the phone drawer from the Inbox', () => {
  it("the Inbox's Menu opens the drawer, and any navigation closes it", async () => {
    arm()
    const { w, router } = await boot('/workspace/inbox')
    const sidebars = () => w.findAllComponents({ name: 'PortalSidebar' }).length
    expect(sidebars()).toBe(1)
    w.findComponent({ name: 'PortalInbox' }).vm.$emit('open-menu')
    await flushPromises()
    expect(sidebars()).toBe(2) // the drawer's copy
    await router.push('/workspace/inbox?tab=all')
    await flushPromises()
    expect(sidebars()).toBe(1)
  })
})

describe('§3g C10 — Open canvas from the Inbox', () => {
  it("the Inbox is told the selected agent's canvas count, and Open canvas opens the rail on Canvas", async () => {
    arm()
    const { w } = await boot('/workspace/inbox')
    const { usePortalRailFeedsStore } = await import('@/stores/portalRailFeeds')
    const feeds = usePortalRailFeedsStore()
    const inbox = () => w.findComponent({ name: 'PortalInbox' })
    inbox().vm.$emit('update:preview', 'thread:t1')
    await flushPromises()
    expect(inbox().props('canvasCount')).toBe(0) // the feed has not landed
    // The rail's feed lands for the selected agent (it loads it anyway).
    feeds.canvases = { scout: [{ id: 'c1', updated_at: '2026-09-27T10:00:00Z' }] }
    await flushPromises()
    expect(inbox().props('canvasCount')).toBe(1)
    inbox().vm.$emit('open-canvas')
    await flushPromises()
    expect(w.findComponent({ name: 'PortalRail' }).props('activeTab')).toBe('canvas')
  })
})
