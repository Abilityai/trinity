/**
 * Per-user, server-persisted Grid state — the store contract (trinity-enterprise#413).
 *
 * The Grid's layout, tile prefs and org toggles used to be three
 * browser-global localStorage keys: shared by every user of one browser, lost
 * on every other device. They are now the USER's server record, with a
 * per-user browser cache in front of it. This suite drives the two real stores
 * (`fleetGrid` + the `userPreferences` engine) against a mocked axios and
 * pins the properties the review found load-bearing:
 *
 *   - load order: server record → per-user cache → legacy blob (adopted ONCE,
 *     by the first identity) → default;
 *   - adoption actually WINS: `syncLayout` overlays the in-memory layout over
 *     the saved map, so a bump alone would re-normalise the old positions;
 *   - no write leaves before the initial GET settles, and no write is ever
 *     unconditional (`base_updated_at` is null = insert-only, string = CAS);
 *   - a gesture made before the GET lands is not overwritten by the record;
 *   - 409 is origin-aware (gesture retries once with the new base; reconcile
 *     adopts) and bounded;
 *   - a failed load/save keeps the grid working from the cache and is honest
 *     (`layoutSource`, `persistNotice`), and 401 is not "unreachable";
 *   - an identity change drops the queue synchronously — nothing fires under
 *     the next user's token — and wipes the in-memory board;
 *   - Reset clears the caller's record and the caller's cache.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { reactive, nextTick } from 'vue'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
})

// A REACTIVE auth stand-in: the identity-change tests flip `principalId`.
const auth = reactive({
  authHeader: { Authorization: 'Bearer jwt' },
  user: { role: 'user' },
  principalId: 'alice',
})
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
vi.mock('@/stores/executions', () => ({
  useExecutionsStore: () => ({ fetchAgentAnalytics: vi.fn(), analyticsCache: {} }),
}))
vi.mock('@/stores/subscriptions', () => ({
  useSubscriptionsStore: () => ({ fetchPressureData: vi.fn() }),
}))
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import axios from 'axios'
import { useFleetGridStore } from '@/stores/fleetGrid'
import { useUserPreferencesStore } from '@/stores/userPreferences'
import {
  LAYOUT_KEY,
  LAYOUT_KEY_V1,
  WIDGET_PREFS_KEY,
  ORG_KEY,
  LEGACY_ADOPTED_KEY,
  userScopedKey,
} from '@/utils/gridStorageKeys'

const AGENTS = ['a1', 'a2']
const PREFS_URL = '/api/users/me/preferences'

function httpError(status, data = {}) {
  const e = new Error(`HTTP ${status}`)
  e.response = { status, data }
  return e
}

/** Mock the GET with a server record set; `null` → network failure. */
function serverHas(preferences) {
  axios.get.mockImplementation((url) => {
    if (url !== PREFS_URL) return Promise.resolve({ data: {}, headers: {} })
    if (preferences === null) return Promise.reject(new Error('network'))
    if (preferences instanceof Error) return Promise.reject(preferences)
    return Promise.resolve({ data: { preferences }, headers: {} })
  })
}
function rec(value, updated_at = 't1') {
  return { value, updated_at }
}
/** Every PUT succeeds, echoing a fresh updated_at. */
function putsSucceed() {
  let n = 0
  axios.put.mockImplementation(() => Promise.resolve({ data: { updated_at: `srv${++n}` } }))
}
function putBodies() {
  return axios.put.mock.calls.map(([url, body]) => ({ key: url.split('/').pop(), ...body }))
}
async function settle() {
  await vi.runAllTimersAsync()
  await nextTick()
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.useFakeTimers()
  axios.get.mockReset()
  axios.put.mockReset()
  axios.delete.mockReset()
  axios.delete.mockResolvedValue({ data: { deleted: true } })
  auth.principalId = 'alice'
  putsSucceed()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('load order', () => {
  it('a user with nothing stored anywhere gets the default layout and reports it', async () => {
    serverHas({})
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    expect(store.layoutSource).toBe('pending')
    await store.loadPreferences()
    await settle()
    expect(store.layoutSource).toBe('default')
    expect(Object.keys(store.layout)).toEqual(expect.arrayContaining(AGENTS))
  })

  it('the server record wins over the per-user cache — and actually replaces the positions', async () => {
    localStorage.setItem(
      userScopedKey(LAYOUT_KEY, 'alice'),
      JSON.stringify({ a1: { c: 0, r: 0 }, a2: { c: 1, r: 0 } })
    )
    serverHas({ grid_layout: rec({ a1: { c: 5, r: 5 }, a2: { c: 6, r: 5 } }) })
    const store = useFleetGridStore()
    store.syncLayout(AGENTS) // first paint from the cache
    expect(store.layout.a1).toEqual({ c: 0, r: 0 })
    const gen = store.layoutGeneration
    await store.loadPreferences()
    await settle()
    expect(store.layoutGeneration).toBe(gen + 1)
    store.syncLayout(AGENTS) // what FleetGrid's watcher does on the bump
    expect(store.layout.a1).toEqual({ c: 5, r: 5 })
    expect(store.layout.a2).toEqual({ c: 6, r: 5 })
    expect(store.layoutSource).toBe('server')
    // The cache now mirrors the record.
    expect(JSON.parse(localStorage.getItem(userScopedKey(LAYOUT_KEY, 'alice'))).a1).toEqual({ c: 5, r: 5 })
  })

  it('a legacy browser-global blob is adopted into the record once, insert-only', async () => {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ a1: { c: 3, r: 3 }, a2: { c: 4, r: 3 } }))
    serverHas({})
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    expect(store.layout.a1).toEqual({ c: 3, r: 3 }) // first paint from legacy
    await store.loadPreferences()
    await settle()
    expect(store.layoutSource).toBe('local')
    const bodies = putBodies().filter((b) => b.key === 'grid_layout')
    expect(bodies.length).toBe(1)
    expect(bodies[0].base_updated_at).toBeNull()
    expect(bodies[0].value.a1).toEqual({ c: 3, r: 3 })
    // Claimed by this identity; the legacy key itself is left in place.
    expect(localStorage.getItem(LEGACY_ADOPTED_KEY)).toBe('alice')
    expect(localStorage.getItem(LAYOUT_KEY)).not.toBeNull()
  })

  it('a v1 legacy blob is still read when v2 is absent', async () => {
    localStorage.setItem(LAYOUT_KEY_V1, JSON.stringify({ a1: { c: 2, r: 2 }, a2: { c: 3, r: 2 } }))
    serverHas({})
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    expect(store.layout.a1).toEqual({ c: 2, r: 2 })
  })

  it('a second identity on the same browser does NOT inherit the adopted legacy blob', async () => {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ a1: { c: 3, r: 3 }, a2: { c: 4, r: 3 } }))
    localStorage.setItem(LEGACY_ADOPTED_KEY, 'alice')
    auth.principalId = 'bob'
    serverHas({})
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    expect(store.layout.a1).not.toEqual({ c: 3, r: 3 })
    await store.loadPreferences()
    await settle()
    expect(store.layoutSource).toBe('default')
  })

  it('two users on one browser read only their own cache', () => {
    localStorage.setItem(userScopedKey(LAYOUT_KEY, 'alice'), JSON.stringify({ a1: { c: 0, r: 0 }, a2: { c: 1, r: 0 } }))
    localStorage.setItem(userScopedKey(LAYOUT_KEY, 'bob'), JSON.stringify({ a1: { c: 7, r: 7 }, a2: { c: 8, r: 7 } }))
    auth.principalId = 'bob'
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    expect(store.layout.a1).toEqual({ c: 7, r: 7 })
  })

  it('widget prefs and org toggles follow the same path', async () => {
    serverHas({
      grid_widgets: rec({ executions: false }),
      grid_org: rec({ zones: false, lines: true }),
    })
    const store = useFleetGridStore()
    expect(store.widgetPrefs).toEqual({})
    expect(store.orgPrefs).toEqual({ zones: true, lines: true })
    await store.loadPreferences()
    await settle()
    expect(store.widgetPrefs).toEqual({ executions: false })
    expect(store.orgPrefs).toEqual({ zones: false, lines: true })
    expect(JSON.parse(localStorage.getItem(userScopedKey(ORG_KEY, 'alice')))).toEqual({ zones: false, lines: true })
    expect(JSON.parse(localStorage.getItem(userScopedKey(WIDGET_PREFS_KEY, 'alice')))).toEqual({ executions: false })
  })
})

describe('writes', () => {
  it('nothing leaves before the GET settles, then the write carries the known base', async () => {
    let resolveGet
    axios.get.mockImplementation(() => new Promise((r) => { resolveGet = r }))
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    const load = store.loadPreferences()
    store.moveTile('a1', 9, 9)
    await vi.advanceTimersByTimeAsync(5000)
    expect(axios.put).not.toHaveBeenCalled()
    resolveGet({ data: { preferences: { grid_layout: rec({ a1: { c: 0, r: 0 }, a2: { c: 1, r: 0 } }, 'T') } }, headers: {} })
    await load
    await settle()
    const bodies = putBodies()
    expect(bodies.length).toBe(1)
    expect(bodies[0].base_updated_at).toBe('T')
    // The gesture made before the record landed is what was written, not the
    // record: the user's edit is not overwritten by a late GET.
    expect(bodies[0].value.a1).toEqual({ c: 9, r: 9 })
    expect(store.layout.a1).toEqual({ c: 9, r: 9 })
  })

  it('rapid gestures debounce into one PUT, and it is the last state', async () => {
    serverHas({})
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    await store.loadPreferences()
    await settle()
    axios.put.mockClear()
    store.moveTile('a1', 4, 4)
    store.moveTile('a1', 5, 5)
    store.moveTile('a1', 6, 6)
    await settle()
    const bodies = putBodies().filter((b) => b.key === 'grid_layout')
    expect(bodies.length).toBe(1)
    expect(bodies[0].value.a1).toEqual({ c: 6, r: 6 })
  })

  it('a gesture that 409s retries ONCE with the new base and wins', async () => {
    serverHas({ grid_layout: rec({ a1: { c: 0, r: 0 }, a2: { c: 1, r: 0 } }, 'T1') })
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    await store.loadPreferences()
    await settle()
    store.syncLayout(AGENTS)
    axios.put.mockReset()
    axios.put
      .mockRejectedValueOnce(httpError(409, { detail: { current: rec({ a1: { c: 2, r: 2 }, a2: { c: 1, r: 0 } }, 'T2') } }))
      .mockResolvedValueOnce({ data: { updated_at: 'T3' } })
    store.moveTile('a1', 8, 8)
    await settle()
    const bodies = putBodies()
    expect(bodies.map((b) => b.base_updated_at)).toEqual(['T1', 'T2'])
    expect(bodies[1].value.a1).toEqual({ c: 8, r: 8 })
    expect(store.layout.a1).toEqual({ c: 8, r: 8 })
  })

  it('a gesture that 409s twice stops — bounded, no loop', async () => {
    serverHas({ grid_layout: rec({ a1: { c: 0, r: 0 }, a2: { c: 1, r: 0 } }, 'T1') })
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    await store.loadPreferences()
    await settle()
    store.syncLayout(AGENTS)
    axios.put.mockReset()
    axios.put.mockRejectedValue(httpError(409, { detail: { current: rec({ a1: { c: 2, r: 2 }, a2: { c: 1, r: 0 } }, 'T9') } }))
    store.moveTile('a1', 8, 8)
    await settle()
    expect(axios.put).toHaveBeenCalledTimes(2)
  })

  it('a reconcile-born write that 409s adopts the server record instead of clobbering it', async () => {
    serverHas({ grid_layout: rec({ a1: { c: 0, r: 0 }, a2: { c: 1, r: 0 } }, 'T1') })
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    await store.loadPreferences()
    await settle()
    store.syncLayout(AGENTS)
    await settle()
    axios.put.mockReset()
    axios.put.mockRejectedValue(httpError(409, { detail: { current: rec({ a1: { c: 2, r: 2 }, a2: { c: 3, r: 2 }, a3: { c: 0, r: 5 } }, 'T2') } }))
    // A roster change in THIS (stale) tab: newcomer a3 → reconcile-born write.
    store.syncLayout([...AGENTS, 'a3'])
    await settle()
    expect(axios.put).toHaveBeenCalledTimes(1)
    store.syncLayout([...AGENTS, 'a3']) // the generation watcher's re-sync
    expect(store.layout.a1).toEqual({ c: 2, r: 2 })
    expect(store.layout.a3).toEqual({ c: 0, r: 5 })
    expect(useUserPreferencesStore().baseFor('grid_layout')).toBe('T2')
  })
})

describe('honest fallback', () => {
  it('a failed load keeps the cached layout, names the failure, and the grid stays editable', async () => {
    localStorage.setItem(userScopedKey(LAYOUT_KEY, 'alice'), JSON.stringify({ a1: { c: 1, r: 1 }, a2: { c: 2, r: 1 } }))
    serverHas(httpError(503))
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    await store.loadPreferences()
    await settle()
    expect(store.layoutSource).toBe('local')
    expect(store.persistNotice).toMatch(/HTTP 503/)
    store.moveTile('a1', 3, 3)
    expect(store.layout.a1).toEqual({ c: 3, r: 3 })
    expect(JSON.parse(localStorage.getItem(userScopedKey(LAYOUT_KEY, 'alice'))).a1).toEqual({ c: 3, r: 3 })
    expect(axios.put).not.toHaveBeenCalled() // no base known — never unconditional
    store.dismissPersistNotice()
    expect(store.persistNotice).toBeNull()
  })

  it('a failed save keeps the value local, says so, and retries on the next change', async () => {
    serverHas({})
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    await store.loadPreferences()
    await settle()
    axios.put.mockReset()
    axios.put.mockRejectedValueOnce(new Error('network'))
    store.moveTile('a1', 3, 3)
    await settle()
    expect(store.persistNotice).toMatch(/network error/)
    axios.put.mockResolvedValueOnce({ data: { updated_at: 'ok' } })
    store.moveTile('a1', 4, 4)
    await settle()
    expect(store.persistNotice).toBeNull()
    expect(putBodies().at(-1).value.a1).toEqual({ c: 4, r: 4 })
  })

  it('401 is a logout, not "unreachable"', async () => {
    serverHas(httpError(401))
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    await store.loadPreferences()
    await settle()
    expect(store.persistNotice).toBeNull()
    expect(store.layoutSource).toBe('default')
  })
})

describe('identity change', () => {
  it('drops a pending write synchronously and wipes the board — nothing fires under the next user', async () => {
    serverHas({})
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    await store.loadPreferences()
    await settle()
    axios.put.mockClear()
    store.moveTile('a1', 3, 3)
    auth.principalId = 'bob' // logout → another login, no reload
    await settle()
    expect(axios.put).not.toHaveBeenCalled()
    expect(store.layout).toEqual({})
    expect(store.widgetPrefs).toEqual({})
    expect(store.layoutSource).toBe('pending')
    expect(useUserPreferencesStore()._pendingSize()).toBe(0)
  })

  it('a GET that lands after the identity moved is discarded', async () => {
    let resolveGet
    axios.get.mockImplementation(() => new Promise((r) => { resolveGet = r }))
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    const load = store.loadPreferences()
    auth.principalId = 'bob'
    resolveGet({ data: { preferences: { grid_layout: rec({ a1: { c: 5, r: 5 }, a2: { c: 6, r: 5 } }) } }, headers: {} })
    await load
    await settle()
    expect(useUserPreferencesStore().records).toEqual({})
    expect(store.layout).toEqual({})
  })
})

describe('reset', () => {
  it('Reset deletes the caller\'s record and cache, then re-persists the default insert-only', async () => {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ a1: { c: 3, r: 3 }, a2: { c: 4, r: 3 } }))
    serverHas({ grid_layout: rec({ a1: { c: 3, r: 3 }, a2: { c: 4, r: 3 } }, 'T1') })
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    await store.loadPreferences()
    await settle()
    axios.put.mockReset()
    putsSucceed()
    store.resetLayout(AGENTS)
    await settle()
    expect(axios.delete).toHaveBeenCalledWith(`${PREFS_URL}/grid_layout`, expect.anything())
    expect(localStorage.getItem(LAYOUT_KEY)).toBeNull()
    const bodies = putBodies().filter((b) => b.key === 'grid_layout')
    expect(bodies.length).toBe(1)
    expect(bodies[0].base_updated_at).toBeNull()
    expect(store.layout.a1).toEqual({ c: 0, r: 0 })
  })

  it('Reset tiles clears the record and leaves an explicit empty cache so legacy is not re-adopted', async () => {
    localStorage.setItem(WIDGET_PREFS_KEY, JSON.stringify({ executions: false }))
    serverHas({})
    const store = useFleetGridStore()
    expect(store.widgetPrefs).toEqual({ executions: false })
    await store.loadPreferences()
    await settle()
    store.resetWidgets()
    await settle()
    expect(axios.delete).toHaveBeenCalledWith(`${PREFS_URL}/grid_widgets`, expect.anything())
    expect(localStorage.getItem(userScopedKey(WIDGET_PREFS_KEY, 'alice'))).toBe('{}')
    expect(store.widgetPrefs).toEqual({})
  })
})

describe('tab going away', () => {
  it('flushPending sends the debounced write with keepalive and the known base', async () => {
    serverHas({ grid_layout: rec({ a1: { c: 0, r: 0 }, a2: { c: 1, r: 0 } }, 'T1') })
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true }))
    globalThis.fetch = fetchMock
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    await store.loadPreferences()
    await settle()
    store.syncLayout(AGENTS)
    await settle()
    axios.put.mockClear()
    store.moveTile('a1', 8, 8)
    store.flushPending()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`${PREFS_URL}/grid_layout`)
    expect(init.keepalive).toBe(true)
    expect(init.headers.Authorization).toBe('Bearer jwt')
    expect(JSON.parse(init.body).base_updated_at).toBe('T1')
    await settle()
    expect(axios.put).not.toHaveBeenCalled() // the debounce was consumed
    delete globalThis.fetch
  })

  it('an unmount flush (mode switch) goes through the normal client and records the new base', async () => {
    serverHas({ grid_layout: rec({ a1: { c: 0, r: 0 }, a2: { c: 1, r: 0 } }, 'T1') })
    const store = useFleetGridStore()
    store.syncLayout(AGENTS)
    await store.loadPreferences()
    await settle()
    store.syncLayout(AGENTS)
    await settle()
    axios.put.mockReset()
    axios.put.mockResolvedValueOnce({ data: { updated_at: 'T2' } })
    store.moveTile('a1', 8, 8)
    store.flushPending({ keepalive: false })
    await nextTick()
    await Promise.resolve()
    expect(axios.put).toHaveBeenCalledTimes(1)
    expect(putBodies()[0].base_updated_at).toBe('T1')
    await settle()
    expect(useUserPreferencesStore().baseFor('grid_layout')).toBe('T2')
    expect(useUserPreferencesStore()._pendingSize()).toBe(0)
  })
})
