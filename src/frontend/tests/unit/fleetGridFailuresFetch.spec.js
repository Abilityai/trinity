/**
 * The manufactured-green route that lives in the STORE (ent#100).
 *
 * `utils/executionFailure.js::failuresTileState` is swept exhaustively over
 * every fault combination, and that sweep proves the green "No failures in 24h
 * ✓" is unreachable except from a fully-healthy input. What it structurally
 * CANNOT prove is that the store only ever hands it a healthy input honestly:
 * the fetchers decide what `loaded` / `error` / the row list mean, and a
 * fetcher that launders a fault into "zero failures, loaded, no error" reopens
 * the exact hole one layer up, invisibly to the pure-function tests.
 *
 * The specific hazard: `GET /api/executions` answers a BARE JSON ARRAY, so the
 * fetcher must decide what a 200 that is NOT an array means. Coercing it to
 * `[]` is the tempting, wrong answer — it is indistinguishable from a healthy
 * empty fleet. The sibling `/stats` fetcher already gets this right (an
 * unreadable `failed_count` becomes `null`, i.e. UNKNOWN, which the state
 * machine refuses to confirm), so the two are asserted together here to keep
 * them from drifting apart.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
})

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({
    authHeader: { Authorization: 'Bearer jwt' },
    user: { role: 'user' },
  }),
}))

vi.mock('@/stores/executions', () => ({
  useExecutionsStore: () => ({ fetchAgentAnalytics: vi.fn() }),
}))

// Same reason as the two mocks above, with one extra tooth (ent#259): the real
// `stores/subscriptions` imports `src/api.js`, which REPLACES `.get` on the
// axios instance to add request dedup (PERF-269). The mock below returns one
// shared object from `create()`, so merely importing the real store would
// overwrite the `vi.fn()` this file drives — every assertion then dies on
// `axios.get.mockImplementation is not a function`, pointing at the harness
// rather than at anything under test.
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
import { failuresTileState } from '@/utils/executionFailure'
import { registerWidget, GRID_WIDGETS } from '@/utils/gridWidgets'

// The tile's catalog entry, registered WITHOUT importing catalog.js — that file
// imports .vue components and this suite is node-environment. `refreshBatchData`
// gates the two GETs on the tile being enabled, so the entry has to exist for
// the real code path to run at all.
if (!GRID_WIDGETS.some((w) => w.id === 'recent-failures')) {
  registerWidget({ id: 'recent-failures', title: 'Recent failures', component: {}, defaultOn: true })
}

/**
 * Route the mocked axios per URL, and run the store's real batch refresh.
 * Driving `refreshBatchData()` rather than a fetcher directly is deliberate:
 * it also proves the enabled-tile gate actually reaches these two GETs.
 */
async function refreshWith({ list, stats }) {
  axios.get.mockImplementation((url) => {
    if (url === '/api/executions') {
      return list instanceof Error ? Promise.reject(list) : Promise.resolve({ data: list, headers: {} })
    }
    if (url === '/api/executions/stats') {
      return stats instanceof Error ? Promise.reject(stats) : Promise.resolve({ data: stats, headers: {} })
    }
    return Promise.resolve({ data: {}, headers: {} }) // the three sibling batch fetches
  })
  const store = useFleetGridStore()
  await store.refreshBatchData()
  return store
}

/** The tile's own reading of whatever the store currently holds. */
const tileOf = (s, rosterSize = 3) =>
  failuresTileState({
    listLoaded: s.failuresListLoaded,
    listError: s.failuresListError,
    statsLoaded: s.failuresStatsLoaded,
    statsError: s.failuresStatsError,
    itemCount: (s.recentFailures || []).length,
    failed24h: s.failures24h,
    rosterSize,
  })

const ROW = {
  id: 'exec-1', agent_name: 'prospector', started_at: '2026-08-12T10:00:00Z',
  triggered_by: 'schedule', error_summary: 'Timed out after 3600s',
}

describe('fetchRecentFailures — a 200 that is not the documented array is a FAULT', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('accepts the real shape: a bare JSON array', async () => {
    const s = await refreshWith({ list: [ROW], stats: { failed_count: 1 } })

    expect(s.recentFailures).toHaveLength(1)
    expect(s.failuresListLoaded).toBe(true)
    expect(s.failuresListError).toBe(false)
  })

  it.each([
    ['an empty body', ''],
    ['an object (the .items shape this endpoint does NOT use)', { items: [] }],
    ['a null body', null],
    ['an HTML interstitial', '<html>proxy</html>'],
  ])('refuses to read %s as "zero failures"', async (_label, body) => {
    const s = await refreshWith({ list: body, stats: { failed_count: 0 } })

    // The load did NOT succeed, so the tile cannot reach the confirmed ✓ —
    // which is the whole point: `[]` + loaded + no-error is byte-identical to
    // a healthy empty fleet.
    expect(s.failuresListError).toBe(true)
    expect(s.failuresListLoaded).toBe(false)
  })

  it('THE REGRESSION: a malformed 200 cannot manufacture the green ✓', async () => {
    // /stats legitimately reports zero, so the ONLY thing standing between this
    // and a false all-clear is how the list fetcher reads its own fault.
    const s = await refreshWith({ list: '', stats: { failed_count: 0 } })

    expect(s.failures24h).toBe(0)
    expect(tileOf(s).emptyTitle).not.toContain('✓')
  })

  it('keeps the last known-good rows rather than blanking them', async () => {
    await refreshWith({ list: [ROW], stats: { failed_count: 1 } })
    const s = await refreshWith({ list: undefined, stats: { failed_count: 1 } })

    // Stale-while-revalidate: a bad refresh over loaded rows still renders them.
    expect(s.recentFailures).toHaveLength(1)
    expect(tileOf(s).state).toBe('ready')
  })

  it('a thrown request is an error, never an empty success', async () => {
    const s = await refreshWith({ list: new Error('network down'), stats: { failed_count: 0 } })

    expect(s.failuresListError).toBe(true)
    expect(s.failuresListLoaded).toBe(false)
  })
})

describe('fetchFailureStats — an unreadable count is UNKNOWN, not zero', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('reads a real count', async () => {
    const s = await refreshWith({ list: [], stats: { failed_count: 3 } })
    expect(s.failures24h).toBe(3)
  })

  it.each([
    ['a missing field', {}],
    ['a non-numeric field', { failed_count: 'three' }],
    ['a null body', null],
  ])('degrades %s to null, which the tile refuses to confirm', async (_l, body) => {
    // The list GET is healthy and empty here — so `null` vs `0` is the ONLY
    // thing standing between this and a green all-clear.
    const s = await refreshWith({ list: [], stats: body })

    expect(s.failures24h).toBeNull()
    expect(tileOf(s).emptyTitle).not.toContain('✓')
  })
})
