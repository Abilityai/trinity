/**
 * trinity-enterprise#475 — the rail's feed store and its one owner.
 *
 * Store fetchers are testable here without a mount harness — the
 * `portalReportsStore.spec.js` shape (Pinia in node, the client-portal store
 * mocked at the module boundary). What is proven:
 *
 *   1. the door decides what is FETCHED: a feed set without `files` never
 *      calls the documents route, a client with no `loops` never feeds the
 *      loops store;
 *   2. one agent's failure keeps the others' rows and says the list may be
 *      short; every agent failing on first load is `failed`, never `empty`;
 *   3. a stale response (a chat switch mid-flight) is dropped;
 *   4. uploads are read only on request, and an upload re-reads only its own
 *      agent's inbox — and not at all if the chat moved on meanwhile;
 *   5. push events for a participant refresh (debounced); events for anyone
 *      else, or with no agent, are ignored;
 *   6. the owner composable feeds nothing behind a door, nothing while the rail
 *      is hidden, and does not blank a room's first (empty) beat.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { computed, nextTick, ref } from 'vue'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
  globalThis.window = globalThis.window || { location: { pathname: '/workspace' } }
})

const portal = vi.hoisted(() => ({
  fetchAgentCanvases: vi.fn(),
  fetchDocuments: vi.fn(),
  fetchUploads: vi.fn(),
  uploadDocument: vi.fn(),
  isPlatformSession: true,
}))
vi.mock('@/stores/clientPortal', () => ({ useClientPortalStore: () => portal }))

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))
vi.mock('../../src/api', () => ({ default: api }))

import { usePortalRailFeedsStore } from '@/stores/portalRailFeeds'
import { usePortalLoopsStore } from '@/stores/portalLoops'
import { usePortalRailFeeds } from '@/composables/usePortalRailFeeds'
import { RAIL_TABS, visibleTabs } from '@/components/portal/portalRail'

const flush = () => new Promise((r) => setTimeout(r, 0))

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useRealTimers()
  for (const fn of Object.values(portal)) if (typeof fn?.mockReset === 'function') fn.mockReset()
  api.get.mockReset(); api.post.mockReset()
  portal.fetchAgentCanvases.mockImplementation(async (a) => [{ canvas_id: `${a}-c`, updated_at: '2026-09-06T10:00:00Z' }])
  portal.fetchDocuments.mockImplementation(async (a) => [{ id: `${a}-d`, created_at: '2026-09-06T10:00:00Z' }])
  portal.fetchUploads.mockImplementation(async () => [{ filename: 'brief.pdf' }])
  api.get.mockImplementation(async () => ({ data: [] }))
})

describe('ent#475 — the feed store fetches what the door allows', () => {
  it('asks only for the feeds the shell set, for every participant, and never uploads unasked', async () => {
    const s = usePortalRailFeedsStore()
    s.setParticipants(['scout', 'sage'])
    s.setFeeds({ canvas: true, files: false })
    await s.refresh()
    expect(portal.fetchAgentCanvases.mock.calls.map((c) => c[0])).toEqual(['scout', 'sage'])
    expect(portal.fetchDocuments).not.toHaveBeenCalled()
    expect(portal.fetchUploads).not.toHaveBeenCalled()
    expect(s.hasLoaded).toBe(true)
    expect(s.canvases.scout).toHaveLength(1)
    expect(s.canvasCount).toBe(2)
  })

  it('does nothing with no participants or no feeds', async () => {
    const s = usePortalRailFeedsStore()
    s.setFeeds({ canvas: true, files: true })
    await s.refresh()
    s.setParticipants(['scout'])
    s.setFeeds({ canvas: false, files: false })
    await s.refresh()
    expect(portal.fetchAgentCanvases).not.toHaveBeenCalled()
    expect(portal.fetchDocuments).not.toHaveBeenCalled()
    expect(s.hasLoaded).toBe(false)
  })

  it('keeps what loaded when one agent fails, and says the list may be short', async () => {
    portal.fetchDocuments.mockImplementation(async (a) => { if (a === 'sage') throw new Error('503'); return [{ id: 'd', created_at: '2026-09-06T10:00:00Z' }] })
    const s = usePortalRailFeedsStore()
    s.setParticipants(['scout', 'sage'])
    s.setFeeds({ canvas: false, files: true })
    await s.refresh()
    expect(s.hasLoaded).toBe(true)
    expect(s.documents.scout).toHaveLength(1)
    expect(s.documents.sage).toBeUndefined()
    expect(s.error).toMatch(/may be incomplete/)
  })

  it('is failed — never empty — when every read fails on first load', async () => {
    portal.fetchAgentCanvases.mockRejectedValue(new Error('down'))
    const s = usePortalRailFeedsStore()
    s.setParticipants(['scout'])
    s.setFeeds({ canvas: true, files: false })
    await s.refresh()
    expect(s.hasLoaded).toBe(false)
    expect(s.error).toBeTruthy()
  })

  it('drops a response that arrives after the chat switched', async () => {
    let release
    portal.fetchAgentCanvases.mockImplementation(() => new Promise((r) => { release = () => r([{ canvas_id: 'old', updated_at: '2026-09-06T10:00:00Z' }]) }))
    const s = usePortalRailFeedsStore()
    s.setParticipants(['scout'])
    s.setFeeds({ canvas: true, files: false })
    const p = s.refresh()
    s.setParticipants(['sage'])      // the switch
    release(); await p
    expect(s.canvases.scout).toBeUndefined()
    expect(s.hasLoaded).toBe(false)
  })

  it('reads uploads on request, and an upload re-reads only its own agent — unless the chat moved on', async () => {
    const s = usePortalRailFeedsStore()
    s.setParticipants(['scout', 'sage'])
    s.setFeeds({ canvas: false, files: true })
    await s.refresh({ uploads: true })
    expect(portal.fetchUploads.mock.calls.map((c) => c[0])).toEqual(['scout', 'sage'])
    expect(s.uploadsLoaded).toEqual({ scout: true, sage: true })
    portal.fetchUploads.mockClear()
    portal.uploadDocument.mockResolvedValue({ filename: 'x.txt' })
    await s.upload('scout', { name: 'x.txt' })
    expect(portal.fetchUploads.mock.calls.map((c) => c[0])).toEqual(['scout'])
    portal.fetchUploads.mockClear()
    portal.uploadDocument.mockImplementation(async () => { s.setParticipants(['other']); return { filename: 'y' } })
    await s.upload('sage', { name: 'y' })
    expect(portal.fetchUploads).not.toHaveBeenCalled()
  })

  it('refreshes (debounced) on a participant\'s loop or terminal activity, and ignores the rest', async () => {
    vi.useFakeTimers()
    const s = usePortalRailFeedsStore()
    s.setParticipants(['scout'])
    s.setFeeds({ canvas: true, files: false })
    s.handleWebSocketEvent({ type: 'loop_run_completed' })                               // no agent
    s.handleWebSocketEvent({ type: 'loop_run_completed', agent_name: 'stranger' })       // not ours
    s.handleWebSocketEvent({ type: 'agent_activity', agent_name: 'scout', activity_state: 'started' })  // not terminal
    vi.advanceTimersByTime(5000)
    expect(portal.fetchAgentCanvases).not.toHaveBeenCalled()
    for (let i = 0; i < 20; i++) s.handleWebSocketEvent({ type: 'loop_run_completed', agent_name: 'scout' })
    s.handleWebSocketEvent({ type: 'agent_activity', agent_name: 'scout', activity_state: 'completed' })
    vi.advanceTimersByTime(2100)
    expect(portal.fetchAgentCanvases).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
  })

  it('clear() empties everything and cancels a pending push refresh', async () => {
    vi.useFakeTimers()
    const s = usePortalRailFeedsStore()
    s.setParticipants(['scout'])
    s.setFeeds({ canvas: true, files: false })
    s.handleWebSocketEvent({ type: 'loop_completed', agent_name: 'scout' })
    s.clear()
    vi.advanceTimersByTime(5000)
    expect(portal.fetchAgentCanvases).not.toHaveBeenCalled()
    expect(s.participants).toEqual([])
    expect(s.feeds).toEqual({ canvas: false, files: false })
    vi.useRealTimers()
  })
})

describe('ent#475 — the owner feeds the stores off the door gate', () => {
  function mount({ platform = true, participants = ['scout'], visible = true } = {}) {
    const vis = ref(visible)
    const parts = ref(participants)
    const session = ref(platform)
    const tabs = computed(() => visibleTabs(RAIL_TABS, { isPlatform: session.value, participants: parts.value }))
    const activeTab = ref('work')
    const open = ref(false)
    const sheetOpen = ref(false)
    const rail = usePortalRailFeeds({ visible: vis, tabs, participants: parts, activeTab, open, sheetOpen, storage: () => globalThis.localStorage })
    return { rail, vis, parts, session, activeTab, open, sheetOpen }
  }

  it('a platform session feeds loops, canvases and documents; a client never feeds loops', async () => {
    const { rail } = mount()
    await flush()
    const loops = usePortalLoopsStore()
    expect(loops.participants).toEqual(['scout'])
    expect(api.get).toHaveBeenCalledWith('/api/agents/scout/loops', expect.anything())
    expect(portal.fetchAgentCanvases).toHaveBeenCalledWith('scout')
    expect(portal.fetchDocuments).toHaveBeenCalledWith('scout')
    expect(portal.fetchUploads).not.toHaveBeenCalled()
    expect(rail.signals.value.canvas.updated).toBe(true)     // never seen, non-empty

    setActivePinia(createPinia()); api.get.mockClear(); portal.fetchAgentCanvases.mockClear()
    mount({ platform: false })
    await flush()
    expect(api.get).not.toHaveBeenCalled()
    expect(usePortalLoopsStore().participants).toEqual([])
    expect(portal.fetchAgentCanvases).toHaveBeenCalledWith('scout')
  })

  it('feeds nothing while the rail is hidden, and clears when it hides', async () => {
    const { vis } = mount({ visible: false })
    await flush()
    expect(api.get).not.toHaveBeenCalled()
    expect(portal.fetchAgentCanvases).not.toHaveBeenCalled()
    vis.value = true
    await nextTick(); await flush()
    expect(portal.fetchAgentCanvases).toHaveBeenCalledTimes(1)
    vis.value = false
    await nextTick(); await flush()
    expect(usePortalRailFeedsStore().participants).toEqual([])
    expect(usePortalLoopsStore().participants).toEqual([])
  })

  it('a room\'s first empty beat neither fetches nor clears; a late auth confirmation re-fires', async () => {
    const { parts, session } = mount({ platform: false, participants: [] })
    await flush()
    expect(portal.fetchAgentCanvases).not.toHaveBeenCalled()
    parts.value = ['scout', 'sage']
    await nextTick(); await flush()
    expect(portal.fetchAgentCanvases.mock.calls.map((c) => c[0])).toEqual(['scout', 'sage'])
    expect(api.get).not.toHaveBeenCalled()
    session.value = true                       // `isPlatformSession` settled late
    await nextTick(); await flush()
    expect(usePortalLoopsStore().participants).toEqual(['scout', 'sage'])
    expect(api.get).toHaveBeenCalled()
  })

  it('a 1:1 → 1:1 switch re-scopes both stores to the new agent, with the old data gone', async () => {
    const { parts } = mount()
    await flush()
    expect(usePortalRailFeedsStore().canvases.scout).toHaveLength(1)
    parts.value = ['sage']
    await nextTick(); await flush()
    const feeds = usePortalRailFeedsStore()
    expect(feeds.participants).toEqual(['sage'])
    expect(feeds.canvases.scout).toBeUndefined()
    expect(feeds.canvases.sage).toHaveLength(1)
    expect(usePortalLoopsStore().participants).toEqual(['sage'])
    expect(api.get).toHaveBeenLastCalledWith('/api/agents/sage/loops', expect.anything())
  })

  it('opening the Files tab reads the inbox and marks the feed seen; the dot clears', async () => {
    const { rail, activeTab, open } = mount()
    await flush()
    expect(rail.signals.value.files.updated).toBe(true)
    activeTab.value = 'files'; open.value = true
    await nextTick(); await flush(); await nextTick()
    expect(portal.fetchUploads).toHaveBeenCalledWith('scout')
    expect(rail.seen.value.files.scout).toBe('2026-09-06T10:00:00Z')
    expect(rail.signals.value.files.updated).toBe(false)
    expect(JSON.parse(globalThis.localStorage.getItem('trinity-workspace-rail-seen')).files.scout).toBe('2026-09-06T10:00:00Z')
    // A newer file lights it again — and only after the tab was left.
    portal.fetchDocuments.mockImplementation(async () => [{ id: 'd2', created_at: '2026-09-06T11:00:00Z' }])
    open.value = false
    await nextTick()
    rail.refresh()
    await flush(); await nextTick()
    expect(rail.signals.value.files.updated).toBe(true)
  })
})
