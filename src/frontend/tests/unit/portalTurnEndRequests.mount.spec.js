// @vitest-environment jsdom
/**
 * #4671 — one turn end fetches each Workspace list at most once.
 *
 * Measured on a hosted instance: a turn end fired 17–21 API calls at once,
 * with sessions, chat-state, rooms and asks fetched twice each. Against a
 * backend with two workers that is a queue, and `asks` alone took 0.45 s.
 *
 * This replays a turn end at the shell's seam (`shallowMount`, the harness of
 * `portalDraftLanding.mount.spec.js`) over the REAL store actions and a
 * transport that counts every request. `PortalConversation` is a stub, so the
 * test plays its part exactly as `deliver()` does it: note the change, refresh
 * the asks, emit `sessions-changed`.
 *
 * The transport is mocked, so it runs no interceptors by itself — and the
 * write interceptors are half of what is under test (a mark-read must make
 * the chat-state read behind it a NEW read). The mock therefore calls the
 * store's own registered handlers around every write, as axios would.
 *
 * Two properties, and the second is the one a naive join breaks:
 *
 *   1. COUNT — each list is requested once, also when the turn end lands on
 *      top of the 20 s tick / a tab-focus refresh.
 *   2. FRESHNESS — the chat-state the shell ends up showing was requested
 *      AFTER the mark-read settled. Joining the tick's earlier read would put
 *      the unread badge back on the chat the person is sitting in.
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
  useAuthStore: () => ({ isAuthenticated: false, authHeader: {}, userEmail: 'me@example.com', logout: vi.fn() }),
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

import { useClientPortalStore, portalHttp, joinedGet } from '@/stores/clientPortal'
import { resetInFlight, joinFresh } from '@/utils/inflight'
import Portal from '@/views/Portal.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

// The store's own interceptors, captured before any `mockClear`.
const onRequest = portalHttp.interceptors.request.use.mock.calls[0][0]
const [onResponse] = portalHttp.interceptors.response.use.mock.calls[0]

const BASE = '/api/enterprise/client-portal'
const SID = 'sess-a'
const Blank = { template: '<div />' }
const sess = (id, agent, when) => ({
  id, session_id: id, agent_name: agent, title: `${agent} — ${id}`,
  created_at: when, last_message_at: when, message_count: 2, unread: 0,
})
const THREADS = [sess(SID, 'scout', '2026-11-20T10:00:00Z'), sess('sess-b', 'scribe', '2026-11-19T10:00:00Z')]

/** Every request, in the order things happened: `GET <path> start|end`. */
let log
const LATENCY_MS = 8
let slow          // path → ms, for a read that must still be out when something else happens
const later = (value, ms = LATENCY_MS) => new Promise((resolve) => setTimeout(() => resolve(value), ms))

const BODIES = {
  [`${BASE}/sessions`]: () => ({ sessions: THREADS }),
  [`${BASE}/chat-state`]: () => ({ chats: [], as_of: '2026-11-20T10:00:05Z' }),
  '/api/rooms': () => ({ rooms: [] }),
  [`${BASE}/asks`]: () => [],
  [`${BASE}/mentions`]: () => [],
}

function wireTransport() {
  portalHttp.get.mockImplementation(async (url) => {
    log.push(`GET ${url} start`)
    const data = await later((BODIES[url] || (() => ({})))(), slow[url] || LATENCY_MS)
    log.push(`GET ${url} end`)
    return { data, headers: {}, config: { method: 'get', url } }
  })
  portalHttp.post.mockImplementation(async (url) => {
    const config = { method: 'post', url, headers: {} }
    onRequest(config)
    log.push(`POST ${url} start`)
    await later()
    log.push(`POST ${url} end`)
    return onResponse({ data: {}, headers: {}, config })
  })
}

const started = (path) => log.filter((l) => l === `GET ${path} start`).length
const counts = () => ({
  sessions: started(`${BASE}/sessions`),
  chatState: started(`${BASE}/chat-state`),
  rooms: started('/api/rooms'),
  asks: started(`${BASE}/asks`),
  mentions: started(`${BASE}/mentions`),
  markRead: log.filter((l) => l.startsWith('POST') && l.endsWith('/read start')).length,
})
const settle = async (rounds = 12) => {
  for (let i = 0; i < rounds; i++) { await new Promise((r) => setTimeout(r, LATENCY_MS)); await flushPromises() }
}

let store
let wrapper

async function boot() {
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.fetchRoster = vi.fn(async () => {
    store.agents = [{ name: 'scout' }, { name: 'scribe' }]
    store.clientEmail = 'me@example.com'
    store.multiAgentChatAvailable = true      // rooms are part of the thread list
    store.error = null
    store.rosterLoaded = true
  })
  store.seedAgentRecency = vi.fn()
  store.ensureBriefing = vi.fn()
  store.loadAgentSuggestions = vi.fn()
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
  await router.push(`/workspace/c/${SID}`)
  await router.isReady()
  wrapper = shallowMount(Portal, { global: { plugins: [router] } })
  await settle()
  const conversation = wrapper.findComponent({ name: 'PortalConversation' })
  expect(conversation.exists()).toBe(true)
  expect(conversation.props('sessionId')).toBe(SID)
  return conversation
}

/** What `PortalConversation.deliver()` does the moment the reply is on screen. */
function turnEnds(conversation, through = '2026-11-20T10:01:00Z') {
  store.noteWorkspaceChange()
  store.fetchAsks().catch(() => {})                      // refreshAsksAfterTurn
  conversation.vm.$emit('sessions-changed', SID, through)
}

beforeEach(() => {
  setActivePinia(createPinia())
  resetInFlight()
  localStorage.clear()
  log = []
  slow = {}
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
  wireTransport()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

describe('#4671 — a turn end fetches each list once', () => {
  it('alone: one sessions, one rooms, one chat-state, one asks, one mark-read', async () => {
    const conversation = await boot()
    log.length = 0

    turnEnds(conversation)
    await settle()

    expect(counts()).toEqual({ sessions: 1, chatState: 1, rooms: 1, asks: 1, mentions: 0, markRead: 1 })
  })

  it('on top of a tab-focus refresh: the lists are still requested once', async () => {
    // The tick body (`onTabVisible` is the 20 s tick's own, run on focus):
    // asks + mentions + threads. Before #4671 this doubled every list.
    const conversation = await boot()
    log.length = 0

    turnEnds(conversation)
    document.dispatchEvent(new Event('visibilitychange'))
    await settle()

    const c = counts()
    expect(c.sessions).toBe(1)
    expect(c.rooms).toBe(1)
    expect(c.asks).toBe(1)
    expect(c.mentions).toBe(1)
    expect(c.markRead).toBe(1)
    // The focus refresh read chat-state BEFORE the mark-read settled; the
    // turn's own refresh is owed a read from after it. Two, and no more.
    expect(c.chatState).toBe(2)
  })

  it('a burst of focus events is one read of each', async () => {
    await boot()
    log.length = 0

    for (let i = 0; i < 4; i++) document.dispatchEvent(new Event('visibilitychange'))
    await settle()

    expect(counts()).toEqual({ sessions: 1, chatState: 1, rooms: 1, asks: 1, mentions: 1, markRead: 0 })
  })
})

describe('#4671 — and never a stale list', () => {
  it('the chat-state read behind a mark-read leaves AFTER the mark-read settled', async () => {
    const conversation = await boot()
    log.length = 0

    // A read is already out when the turn ends (the tick, a moment earlier) —
    // and it is SLOW, so it is still out when the mark-read settles. That is
    // the window a plain in-flight join answers with the pre-read counts.
    slow[`${BASE}/chat-state`] = LATENCY_MS * 6
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    turnEnds(conversation)
    await settle(40)

    const readDone = log.findIndex((l) => l.startsWith('POST') && l.endsWith('/read end'))
    const lastChatState = log.lastIndexOf(`GET ${BASE}/chat-state start`)
    expect(readDone).toBeGreaterThan(-1)
    expect(lastChatState).toBeGreaterThan(readDone)
    // …and the two chat-state reads did not overlap: the older one had
    // answered before the newer left, so it cannot land last.
    const firstEnd = log.indexOf(`GET ${BASE}/chat-state end`)
    expect(firstEnd).toBeGreaterThan(-1)
    expect(lastChatState).toBeGreaterThan(firstEnd)
  })

  it('a turn end does not join a list read that left before the reply', async () => {
    const conversation = await boot()
    log.length = 0

    // The tick's reads leave…
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(started(`${BASE}/asks`)).toBe(1)
    // …then the reply lands. Its asks refresh must be a read from after it.
    turnEnds(conversation)
    await settle()

    expect(started(`${BASE}/asks`)).toBe(2)
    expect(started(`${BASE}/sessions`)).toBe(2)
    const firstAsksEnd = log.indexOf(`GET ${BASE}/asks end`)
    expect(log.lastIndexOf(`GET ${BASE}/asks start`)).toBeGreaterThan(firstAsksEnd)
  })

  it('sequential turn ends each get their own reads (a join is not a cache)', async () => {
    const conversation = await boot()
    log.length = 0

    turnEnds(conversation)
    await settle()
    turnEnds(conversation, '2026-11-20T10:02:00Z')
    await settle()

    expect(counts()).toEqual({ sessions: 2, chatState: 2, rooms: 2, asks: 2, mentions: 0, markRead: 2 })
  })
})

/**
 * Is a read that left BEFORE `act` still handed to a caller that asks AFTER it?
 * The real `joinFresh` on a key of its own, so nothing else is counted.
 */
async function readAcross(act) {
  let release
  const before = joinFresh('probe', () => new Promise((resolve) => { release = () => resolve('before') }))
  await Promise.resolve()
  await act()
  const after = joinFresh('probe', async () => 'after')
  const joined = after === before
  release()
  return { joined, value: await after }
}

describe('#4671 — the shell marks a turn end as a change', () => {
  // `onWorkState` is where the shell learns a turn ended (1:1: `sending` fell;
  // room: the `working` list went idle). The rail reads that follow must not
  // join one that left while the turn was running.
  it('work going from live to idle: the read after it is a new one', async () => {
    const conversation = await boot()
    conversation.vm.$emit('work-state', { live: 1 })
    await flushPromises()

    const seen = await readAcross(async () => {
      conversation.vm.$emit('work-state', { live: 0 })
      await flushPromises()
    })
    expect(seen).toEqual({ joined: false, value: 'after' })
  })

  it('a turn merely STARTING, or continuing, is not a change', async () => {
    const conversation = await boot()
    const starting = await readAcross(async () => {
      conversation.vm.$emit('work-state', { live: 1 })
      await flushPromises()
    })
    expect(starting).toEqual({ joined: true, value: 'before' })
    const continuing = await readAcross(async () => {
      conversation.vm.$emit('work-state', { live: 2 })
      await flushPromises()
    })
    expect(continuing).toEqual({ joined: true, value: 'before' })
  })
})

describe('#4671 — a read is joined only by the same credential', () => {
  const as = (token) => ({ headers: { Authorization: `Bearer ${token}` } })

  it('two sessions asking for the same list at once are two requests', async () => {
    const [a, b] = await Promise.all([
      joinedGet(`${BASE}/sessions`, as('alice')),
      joinedGet(`${BASE}/sessions`, as('bob')),
    ])
    expect(started(`${BASE}/sessions`)).toBe(2)
    expect(a).not.toBe(b)
    const sent = portalHttp.get.mock.calls.map(([, config]) => config.headers.Authorization)
    expect(sent.sort()).toEqual(['Bearer alice', 'Bearer bob'])
  })

  it('the same session asking twice at once is one', async () => {
    const [a, b] = await Promise.all([
      joinedGet(`${BASE}/sessions`, as('alice')),
      joinedGet(`${BASE}/sessions`, as('alice')),
    ])
    expect(started(`${BASE}/sessions`)).toBe(1)
    expect(a).toBe(b)
  })

  it('different parameters are different reads', async () => {
    await Promise.all([
      joinedGet(`${BASE}/asks`, { ...as('alice'), params: { include_ended: true } }),
      joinedGet(`${BASE}/asks`, as('alice')),
    ])
    expect(started(`${BASE}/asks`)).toBe(2)
  })
})
