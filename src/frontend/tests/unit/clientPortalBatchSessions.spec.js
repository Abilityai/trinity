/**
 * #2198 — the Workspace sidebar's thread list is ONE request, not N+1.
 *
 * The sidebar renders a merged, cross-agent, recency-sorted list, so
 * `fetchAllSessions` asked the per-agent route once per rostered agent. That
 * runs on all SIX `refreshThreads()` call sites — including every thread open
 * and every completed turn — and each call cost 2-3 DB queries server-side.
 *
 * The request count is the easy half. The half that needed tests is that
 * collapsing N calls into one INVERTED a failure mode:
 *
 *   Before, `fetchAllSessions` could not reject. Every per-agent call had its
 *   own `catch { return [] }` — "one down agent never blanks the whole list" —
 *   and `refreshThreads` `Promise.all`s it while only `fetchChatState` was
 *   caught. With one request, a single 500 would blank a populated sidebar AND
 *   abort `bootstrap()` before `resolveAgentQuery()`, breaking Workspace
 *   deep-link landing entirely.
 *
 * So the load-bearing assertions here are the resilience ones, not the count.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

// Harness requirement, not style: the store reads localStorage at state
// construction and installs an axios interceptor at import, and vitest runs
// this in `environment: 'node'`. Same shape as workspaceRoomsGate.spec.js.
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

vi.mock('@/stores/auth', async () => {
  const { ref } = await import('vue')
  const authed = ref(false)
  return {
    useAuthStore: () => ({
      get isAuthenticated() { return authed.value },
      get authHeader() { return authed.value ? { Authorization: 'Bearer jwt' } : {} },
    }),
    __setAuthed: (v) => { authed.value = v },
  }
})

vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import axios from 'axios'
import { stripComments } from './helpers/stripComments'
import { useClientPortalStore } from '@/stores/clientPortal'

const PORTAL = fileURLToPath(new URL('../../src/views/Portal.vue', import.meta.url))
const portalSource = () => stripComments(readFileSync(PORTAL, 'utf8'))

const BATCH_URL = '/api/enterprise/client-portal/sessions'

function signedInStore(agents = ['scout', 'scribe', 'sage']) {
  const store = useClientPortalStore()
  store.portalToken = 'portal-token'
  store.agents = agents.map((name) => ({ name }))
  // Rooms are a separate capability; keep them out of these assertions.
  store.multiAgentChatAvailable = false
  return store
}

const row = (id, agent, at) => ({
  id, agent_name: agent, title: id, created_at: at, last_message_at: at, message_count: 1,
})

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

// ---------------------------------------------------------------------------
// The count — the AC
// ---------------------------------------------------------------------------

describe('#2198 the sidebar list is one request', () => {
  it('issues exactly ONE GET for an N-agent roster', async () => {
    const store = signedInStore(['scout', 'scribe', 'sage'])
    axios.get.mockResolvedValueOnce({ data: { sessions: [row('s1', 'scout', '2026-08-05T00:00:00Z')] } })

    await store.fetchAllSessions()

    expect(axios.get).toHaveBeenCalledTimes(1)
    expect(axios.get).toHaveBeenCalledWith(BATCH_URL, expect.anything())
  })

  it('is INDEPENDENT of roster size — 40 agents is still one GET', async () => {
    const store = signedInStore(Array.from({ length: 40 }, (_, i) => `a${i}`))
    axios.get.mockResolvedValueOnce({ data: { sessions: [] } })

    await store.fetchAllSessions()

    expect(axios.get).toHaveBeenCalledTimes(1)
  })

  it('issues ZERO GETs for an empty roster', async () => {
    // Pins the same property workspaceRoomsGate.spec.js F17b asserts: a batch
    // that fires unconditionally would break it.
    const store = signedInStore([])
    await expect(store.fetchAllSessions()).resolves.toEqual([])
    expect(axios.get).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// The output contract the sidebar depends on
// ---------------------------------------------------------------------------

describe('#2198 output contract', () => {
  it('every row carries agent_name, and the list is most-recent-first', async () => {
    // `agent_name` now comes from the DB row instead of being stamped on
    // client-side, so it has to be asserted rather than assumed.
    const store = signedInStore()
    axios.get.mockResolvedValueOnce({
      data: {
        sessions: [
          row('old', 'scribe', '2026-08-01T00:00:00Z'),
          row('new', 'scout', '2026-08-09T00:00:00Z'),
        ],
      },
    })

    const out = await store.fetchAllSessions()
    expect(out.map((t) => t.id)).toEqual(['new', 'old'])
    expect(out.every((t) => !!t.agent_name)).toBe(true)
  })

  it('drops rows whose agent is not on the DISPLAYED roster', async () => {
    // The backend scopes by the caller's roster — that is the access boundary.
    // This second filter is a rendering rule: a thread whose agent the sidebar
    // does not show would route nowhere.
    const store = signedInStore(['scout'])
    axios.get.mockResolvedValueOnce({
      data: {
        sessions: [
          row('keep', 'scout', '2026-08-05T00:00:00Z'),
          row('drop', 'not-shown', '2026-08-06T00:00:00Z'),
        ],
      },
    })

    const out = await store.fetchAllSessions()
    expect(out.map((t) => t.id)).toEqual(['keep'])
  })
})

// ---------------------------------------------------------------------------
// Resilience — the failure mode the batch inverted
// ---------------------------------------------------------------------------

describe('#2198 one request must not become one point of failure', () => {
  it('a failing refresh does NOT blank an already-populated sidebar', async () => {
    const store = signedInStore()
    axios.get.mockResolvedValueOnce({ data: { sessions: [row('s1', 'scout', '2026-08-05T00:00:00Z')] } })
    const first = await store.fetchAllSessions()
    expect(first).toHaveLength(1)

    axios.get.mockRejectedValueOnce(Object.assign(new Error('boom'), { response: { status: 500 } }))
    const second = await store.fetchAllSessions()

    expect(second.map((t) => t.id)).toEqual(['s1'])
    expect(store.sessionsFailed).toBe(true)
  })

  it('never rejects, so bootstrap() can never be aborted by it', async () => {
    const store = signedInStore()
    axios.get.mockRejectedValueOnce(Object.assign(new Error('boom'), { response: { status: 500 } }))
    await expect(store.fetchAllSessions()).resolves.toEqual([])
  })

  it('clears the failure flag once a refresh succeeds again', async () => {
    const store = signedInStore()
    axios.get.mockRejectedValueOnce(Object.assign(new Error('boom'), { response: { status: 500 } }))
    await store.fetchAllSessions()
    expect(store.sessionsFailed).toBe(true)

    axios.get.mockResolvedValueOnce({ data: { sessions: [row('s1', 'scout', '2026-08-05T00:00:00Z')] } })
    await store.fetchAllSessions()
    expect(store.sessionsFailed).toBe(false)
  })

  it('does NOT fan out on a 5xx — that would turn one failure into N', async () => {
    const store = signedInStore(['a', 'b', 'c'])
    axios.get.mockRejectedValueOnce(Object.assign(new Error('boom'), { response: { status: 500 } }))
    await store.fetchAllSessions()
    expect(axios.get).toHaveBeenCalledTimes(1)
  })
})

// ---------------------------------------------------------------------------
// Deploy skew — transitional, delete with the fallback
// ---------------------------------------------------------------------------

describe('#2198 deploy-skew fallback (TRANSITIONAL)', () => {
  it('falls back to the per-agent fan-out on a 404 and still renders the list', async () => {
    // A cached bundle can reach a backend without the route. Emptying the
    // sidebar there would be the most client-visible failure in the product.
    const store = signedInStore(['scout', 'scribe'])
    axios.get
      .mockRejectedValueOnce(Object.assign(new Error('nope'), { response: { status: 404 } }))
      .mockResolvedValueOnce({ data: { sessions: [row('s1', 'scout', '2026-08-05T00:00:00Z')] } })
      .mockResolvedValueOnce({ data: { sessions: [row('s2', 'scribe', '2026-08-09T00:00:00Z')] } })

    const out = await store.fetchAllSessions()

    expect(out.map((t) => t.id)).toEqual(['s2', 's1'])
    expect(out.every((t) => !!t.agent_name)).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Structural — what no unit test can reach
// ---------------------------------------------------------------------------

describe('#2198 refreshThreads cannot abort bootstrap', () => {
  it('catches BOTH halves of its Promise.all', () => {
    // `bootstrap()` awaits refreshThreads() before resolveAgentQuery() and the
    // deep-link branch. An uncaught rejection there does not merely empty the
    // sidebar — it breaks deep-link landing. The store already swallows, so
    // this is the belt; both are cheap and the consequence is not.
    const src = portalSource()
    const body = src.slice(src.indexOf('async function refreshThreads'))
    const block = body.slice(0, body.indexOf('\n}'))
    expect(block).toMatch(/fetchAllSessions\(\)\s*\.catch\(/)
    expect(block).toMatch(/fetchChatState\(\)\s*\.catch\(/)
  })
})
