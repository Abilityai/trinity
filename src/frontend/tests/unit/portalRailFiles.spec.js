/**
 * trinity#2582 — an upload reaches the Files tab AT ONCE, from every surface.
 *
 * The defect: the composer uploaded straight to the agent's per-client inbox
 * and never told the rail's feed store, so "Files you sent" was stale until the
 * tab was opened or a turn ended.
 *
 * The fix is at the ONE store funnel `clientPortal.js::uploadDocument` — three
 * callers, so notifying there catches the conversation, the room and the rail
 * without touching `PortalConversation.vue`, the file the whole Workspace
 * delivery sequence is serialized to protect.
 *
 * **The tests that matter here are the ones that fail against the OBVIOUS
 * design.** A scalar `lastUpload` + a watcher that joins the in-flight read
 * looks correct and re-breaks the very defect this issue exists to fix, twice:
 *
 *   1. a multi-file drop uploads SEQUENTIALLY without awaiting the feed re-read,
 *      so the second file's note joins a listing snapshotted before it landed;
 *   2. a room's drop is one file into three DIFFERENT agents, and Vue coalesces
 *      mutations in one flush window into a single watcher call carrying only
 *      the last value.
 *
 * Plus a third that only a shared token catches: a `refresh({uploads:true})`
 * issued BEFORE the upload but resolving after it rebuilds its map from a
 * snapshot taken after its own awaits, silently clobbering the fresh listing.
 *
 * ## The fixture
 *
 * The client-portal mock is `reactive(...)`, not a plain `vi.hoisted({})`
 * object. Without that, a watcher on `portal.pendingUploadNotes` never fires
 * and the headline assertion passes for the wrong reason — the store's own
 * direct call would carry it while the drain under test did nothing.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { computed, effectScope, nextTick, reactive, ref } from 'vue'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { join } from 'path'
import { stripComments } from './helpers/stripComments'

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

const portalRaw = vi.hoisted(() => ({
  fetchAgentCanvases: vi.fn(),
  fetchDocuments: vi.fn(),
  fetchUploads: vi.fn(),
  uploadDocument: vi.fn(),
  isPlatformSession: true,
  agents: [],
  // The store's real shape (#2582): a pending-agent map the rail owner drains.
  pendingUploadNotes: {},
  noteUploadPending(name) {
    this.pendingUploadNotes = {
      ...this.pendingUploadNotes,
      [name]: (this.pendingUploadNotes[name] || 0) + 1,
    }
  },
  clearUploadPending(names) {
    const drop = new Set(names || [])
    const next = {}
    for (const [n, seq] of Object.entries(this.pendingUploadNotes)) {
      if (!drop.has(n)) next[n] = seq
    }
    this.pendingUploadNotes = next
  },
}))

// `reactive()` caches by target, so the factory can be called repeatedly and
// every consumer still gets the SAME proxy — which is what makes a watcher on
// it fire at all.
vi.mock('@/stores/clientPortal', async () => {
  const { reactive: r } = await import('vue')
  return { useClientPortalStore: () => r(portalRaw) }
})

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))
vi.mock('../../src/api', () => ({ default: api }))

import { usePortalRailFeedsStore } from '@/stores/portalRailFeeds'
import { usePortalRailFeeds } from '@/composables/usePortalRailFeeds'
import { filesSignalItems, RAIL_TABS, visibleTabs } from '@/components/portal/portalRail'

const SRC = fileURLToPath(new URL('../../src', import.meta.url))
const code = (rel) => stripComments(readFileSync(join(SRC, rel), 'utf8'))

const flush = () => new Promise((r) => setTimeout(r, 0))
const portal = reactive(portalRaw)

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useRealTimers()
  globalThis.localStorage.clear()
  for (const fn of Object.values(portalRaw)) if (typeof fn?.mockReset === 'function') fn.mockReset()
  api.get.mockReset(); api.post.mockReset()
  api.get.mockImplementation(async () => ({ data: [] }))
  portal.pendingUploadNotes = {}
  portal.agents = []
  portalRaw.fetchAgentCanvases.mockImplementation(async () => [])
  portalRaw.fetchDocuments.mockImplementation(async () => [])
  portalRaw.fetchUploads.mockImplementation(async () => [])
  portalRaw.uploadDocument.mockResolvedValue({ filename: 'x.txt' })
})

// The composable is mounted inside an `effectScope` and stopped after each
// test. Without that, its watcher on the SHARED `portal.pendingUploadNotes`
// outlives the test that created it — there is no component to unmount it —
// and the OLDEST surviving watcher drains the queue against a Pinia store from
// a previous test, leaving every later assertion measuring the wrong instance.
// (Diagnosed, not guessed: it presented as `feeds.uploads.scout` undefined and
// a fan-out that noted one agent instead of three.)
let scope = null

function mount({ participants = ['scout'], visible = true, tab = 'work', open = false } = {}) {
  const vis = ref(visible)
  const parts = ref(participants)
  const tabs = computed(() => visibleTabs(RAIL_TABS, { isPlatform: true, participants: parts.value }))
  const activeTab = ref(tab)
  const isOpen = ref(open)
  const sheetOpen = ref(false)
  scope = effectScope()
  const rail = scope.run(() => usePortalRailFeeds({
    visible: vis, tabs, participants: parts, activeTab, open: isOpen, sheetOpen,
    storage: () => globalThis.localStorage,
  }))
  return { rail, vis, parts, activeTab, isOpen, sheetOpen }
}

afterEach(() => {
  if (scope) { scope.stop(); scope = null }
})

// ---------------------------------------------------------------------------
// filesSignalItems — one projection, two readers
// ---------------------------------------------------------------------------

describe('#2582 — the Files signal covers the viewer\'s own uploads', () => {
  it('projects an upload onto the created_at key the dot already reads', () => {
    const out = filesSignalItems(
      { scout: [{ id: 'd1', created_at: '2026-09-07T09:00:00Z' }] },
      { scout: [{ filename: 'u.txt', uploaded_at: '2026-09-07T11:00:00Z' }] },
    )
    expect(out.scout).toHaveLength(2)
    expect(out.scout[1].created_at).toBe('2026-09-07T11:00:00Z')
    expect(out.scout[1].filename).toBe('u.txt')
  })

  it('does not merge the collections themselves — the two-list UI depends on that', () => {
    const documents = { scout: [{ id: 'd1', created_at: 'x' }] }
    const uploads = { scout: [{ filename: 'u.txt', uploaded_at: 'y' }] }
    filesSignalItems(documents, uploads)
    expect(documents.scout).toHaveLength(1)
    expect(uploads.scout).toHaveLength(1)
    expect(uploads.scout[0].created_at).toBeUndefined()
  })

  it('covers an agent present in only one collection, and tolerates junk', () => {
    expect(filesSignalItems({ a: [{ id: '1', created_at: 'x' }] }, {}).a).toHaveLength(1)
    expect(filesSignalItems({}, { b: [{ filename: 'u', uploaded_at: 'x' }] }).b).toHaveLength(1)
    expect(filesSignalItems(null, null)).toEqual({})
    expect(filesSignalItems({ a: null }, { a: 'nope' }).a).toEqual([])
  })

  it('an upload with no timestamp never signals — a null stamp is not "new"', () => {
    const out = filesSignalItems({}, { scout: [{ filename: 'u.txt', uploaded_at: null }] })
    expect(out.scout[0].created_at).toBe(null)
  })
})

// ---------------------------------------------------------------------------
// The headline: an upload lights the dot with Files closed
// ---------------------------------------------------------------------------

describe('#2582 — an upload reaches the rail before any agent reply', () => {
  it('lights the Files dot with the tab CLOSED, after one targeted read', async () => {
    portalRaw.fetchUploads.mockResolvedValue([{ filename: 'brief.pdf', uploaded_at: '2026-09-07T11:00:00Z' }])
    const { rail } = mount({ tab: 'work', open: false })
    await flush()
    expect(rail.signals.value.files.updated).toBe(false)

    // What the composer does: the store funnel, nothing rail-specific.
    portal.noteUploadPending('scout')
    await nextTick()
    await flush()

    expect(portalRaw.fetchUploads.mock.calls.map((c) => c[0])).toEqual(['scout'])
    expect(rail.feeds.uploads.scout).toHaveLength(1)
    expect(rail.signals.value.files.updated).toBe(true)
    expect(rail.signals.value.files.agents).toEqual(['scout'])
  })

  it('opening the Files tab clears the dot the upload lit', async () => {
    portalRaw.fetchUploads.mockResolvedValue([{ filename: 'brief.pdf', uploaded_at: '2026-09-07T11:00:00Z' }])
    const h = mount({ tab: 'work', open: false })
    await flush()
    portal.noteUploadPending('scout')
    await nextTick(); await flush()
    expect(h.rail.signals.value.files.updated).toBe(true)

    h.activeTab.value = 'files'
    h.isOpen.value = true
    await nextTick(); await flush(); await nextTick()

    // markSeen must read the SAME projection: marking only `documents` seen
    // would leave the upload's dot lit forever.
    expect(h.rail.signals.value.files.updated).toBe(false)
  })

  it('a bump for a non-participant does nothing', async () => {
    const { rail } = mount({ participants: ['scout'] })
    await flush()
    portalRaw.fetchUploads.mockClear()
    portal.noteUploadPending('stranger')
    await nextTick(); await flush()
    expect(portalRaw.fetchUploads).not.toHaveBeenCalled()
    expect(rail.signals.value.files.updated).toBe(false)
  })

  it('a bump while the rail is hidden is dropped, not queued forever', async () => {
    const h = mount({ visible: false })
    await flush()
    portal.noteUploadPending('scout')
    await nextTick(); await flush()
    expect(portalRaw.fetchUploads).not.toHaveBeenCalled()
    expect(portal.pendingUploadNotes).toEqual({})
  })
})

// ---------------------------------------------------------------------------
// The two shapes a scalar gets wrong
// ---------------------------------------------------------------------------

describe('#2582 — the regressions a scalar signal would reintroduce', () => {
  it('a two-file batch ends with a listing containing BOTH files', async () => {
    // The trailing-coalesce case. `usePortalFileDrop` uploads sequentially and
    // does not await the feed re-read, so file 2's note lands while file 1's
    // ~200-800ms container_exec_run is still in flight. A consumer that JOINS
    // that read gets a listing snapshotted before file 2 existed — which is
    // defect 1, verbatim, for every multi-file drop.
    const inbox = []
    let resolveFirst
    let call = 0
    portalRaw.fetchUploads.mockImplementation(() => {
      call += 1
      const snapshot = inbox.map((f) => ({ filename: f, uploaded_at: '2026-09-07T11:00:00Z' }))
      if (call === 1) return new Promise((r) => { resolveFirst = () => r(snapshot) })
      return Promise.resolve(snapshot)
    })

    const { rail } = mount()
    await flush()

    inbox.push('one.txt')
    portal.noteUploadPending('scout')
    await nextTick()                      // read #1 starts, sees ['one.txt']

    inbox.push('two.txt')
    portal.noteUploadPending('scout')
    await nextTick()                      // queued, must NOT join read #1

    resolveFirst()
    await flush(); await flush()

    expect(call).toBeGreaterThanOrEqual(2)
    expect(rail.feeds.uploads.scout.map((u) => u.filename)).toEqual(['one.txt', 'two.txt'])
  })

  it('a room fan-out over three agents notes all three', async () => {
    // `PortalRoom.vue` is `for (const name of names) await uploadDocument(name, file)`
    // — one file into three DIFFERENT agents. Vue coalesces mutations landing
    // in one flush window into a single watcher call carrying only the LAST
    // value, so a scalar silently drops the first two.
    portalRaw.fetchUploads.mockResolvedValue([{ filename: 'brief.pdf', uploaded_at: '2026-09-07T11:00:00Z' }])
    const { rail } = mount({ participants: ['scout', 'sage', 'atlas'] })
    await flush()
    portalRaw.fetchUploads.mockClear()

    portal.noteUploadPending('scout')
    portal.noteUploadPending('sage')
    portal.noteUploadPending('atlas')
    await nextTick(); await flush()

    expect(portalRaw.fetchUploads.mock.calls.map((c) => c[0]).sort())
      .toEqual(['atlas', 'sage', 'scout'])
    expect(rail.signals.value.files.agents.sort()).toEqual(['atlas', 'sage', 'scout'])
  })

  it('a refresh resolving AFTER the note does not clobber the fresh listing', async () => {
    // `refresh()` rebuilds `nextUploads` from a snapshot taken after its own
    // awaits, so without a SHARED fetch token the stale map wins silently.
    let releaseRefresh
    const fresh = [{ filename: 'brief.pdf', uploaded_at: '2026-09-07T11:00:00Z' }]
    let n = 0
    portalRaw.fetchUploads.mockImplementation(() => {
      n += 1
      if (n === 1) return new Promise((r) => { releaseRefresh = () => r([]) })   // pre-upload
      return Promise.resolve(fresh)
    })

    const store = usePortalRailFeedsStore()
    store.setParticipants(['scout'])
    store.setFeeds({ canvas: false, files: true })

    const pending = store.refresh({ uploads: true })   // issued BEFORE the upload
    await store.noteUpload('scout')                    // lands first, with the truth
    releaseRefresh()
    await pending
    await flush()

    expect(store.uploads.scout.map((u) => u.filename)).toEqual(['brief.pdf'])
  })

  it('a chat switch mid-read discards the response', async () => {
    let release
    portalRaw.fetchUploads.mockImplementation(() =>
      new Promise((r) => { release = () => r([{ filename: 'stale.txt', uploaded_at: 'x' }]) }))

    const store = usePortalRailFeedsStore()
    store.setParticipants(['scout'])
    store.setFeeds({ canvas: false, files: true })
    const p = store.noteUpload('scout')
    store.setParticipants(['other'])
    release()
    await p
    expect(store.uploads).toEqual({})
  })

  it('upload() delegates to noteUpload rather than carrying a second read', async () => {
    portalRaw.fetchUploads.mockResolvedValue([{ filename: 'x.txt', uploaded_at: 'x' }])
    const store = usePortalRailFeedsStore()
    store.setParticipants(['scout'])
    store.setFeeds({ canvas: false, files: true })
    await store.upload('scout', { name: 'x.txt' })
    expect(portalRaw.fetchUploads.mock.calls.map((c) => c[0])).toEqual(['scout'])
  })
})

// ---------------------------------------------------------------------------
// Source guards — the touches this PR must NOT have made
// ---------------------------------------------------------------------------

describe('#2582 — the conversation and the room are untouched', () => {
  const CONVERSATION = code('components/portal/PortalConversation.vue')
  const ROOM = code('components/portal/PortalRoom.vue')
  const RAIL_FILES = code('components/portal/PortalRailFiles.vue')
  const CLIENT_PORTAL = code('stores/clientPortal.js')

  it('the composer still uploads through the store funnel and knows nothing of the rail', () => {
    // The whole point of fixing this at `uploadDocument`: this file is what the
    // delivery sequence is serialized to protect.
    expect(CONVERSATION).toMatch(/usePortalFileDrop\(\(file\) => store\.uploadDocument\(props\.agent\.name, file\)\)/)
    expect(CONVERSATION).not.toMatch(/portalRailFeeds/)
    expect(CONVERSATION).not.toMatch(/noteUpload/)
  })

  it('the room\'s fan-out is unchanged and equally ignorant of the rail', () => {
    expect(ROOM).toMatch(/for \(const name of names\) await store\.uploadDocument\(name, file\)/)
    expect(ROOM).not.toMatch(/portalRailFeeds/)
  })

  it('the funnel is where the note is raised, and it is a SET', () => {
    const fn = CLIENT_PORTAL.slice(CLIENT_PORTAL.indexOf('async uploadDocument'))
    expect(fn.slice(0, 600)).toMatch(/this\.noteUploadPending\(agentName\)/)
    expect(CLIENT_PORTAL).toMatch(/pendingUploadNotes:\s*\{\}/)
    expect(CLIENT_PORTAL).toMatch(/clearUploadPending\(agentNames\)/)
  })

  it('the tab body still carries the ent#524 drop contract', () => {
    expect(RAIL_FILES).toMatch(/from '@\/composables\/usePortalFileDrop'/)
    expect(RAIL_FILES).toMatch(/<input[^>]*type="file"[^>]*multiple/)
    expect(RAIL_FILES).not.toMatch(/files\?\.\[0\]/)
  })

  it('the dead single-file upload() helper is gone', () => {
    // Zero callers since ent#524 routed both entry points through uploadBatch.
    expect(RAIL_FILES).not.toMatch(/async function upload\(file\)/)
  })

  it('the loading treatment guards still hold on the rebuilt body', () => {
    expect(RAIL_FILES).toContain(`<PortalSkeleton v-if="view.state === 'loading'" variant="rail" />`)
    expect(RAIL_FILES).toContain(`v-else-if="view.state === 'failed'"`)
    expect(RAIL_FILES).toContain('<LoadFailed')
    expect(RAIL_FILES).toContain('<InlineError v-if="view.stale"')
    expect(RAIL_FILES).not.toMatch(/import\s+ScanlineReveal/)
  })
})
