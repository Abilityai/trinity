/**
 * ent#259 — the manufactured-empty route that lives in the STORE.
 *
 * `utils/subscriptionPressureTile.js` proves the tile's empty state ("No
 * subscriptions configured") is only reachable from `loaded && !error`. What
 * that sweep structurally CANNOT prove is that the store sets those flags
 * honestly: the fetcher decides what `listLoaded` / `listError` mean, so a
 * fetcher that launders a fault into "loaded, no error, zero rows" reopens the
 * hole one layer up — and the resulting screen tells an operator their
 * subscriptions are not configured, which is a different and much worse claim
 * than "could not read them".
 *
 * `GET /api/subscriptions` answers a BARE JSON ARRAY, so the fetcher must
 * decide what a 200 that is NOT an array means. Coercing it to `[]` is the
 * tempting, wrong answer: indistinguishable from a genuinely empty fleet. This
 * mirrors `fleetGridFailuresFetch.spec.js`, which pins the same rule for the
 * ent#100 tile's two GETs.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/api', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})

import api from '@/api'
import { useSubscriptionsStore } from '@/stores/subscriptions'

const SUBS = [
  { id: 's1', name: 'Max #1' },
  { id: 's2', name: 'Max #2' },
]

/** Route the mocked client per URL. */
function route({ list, usage }) {
  api.get.mockImplementation((url) => {
    if (url === '/api/subscriptions') {
      return list instanceof Error ? Promise.reject(list) : Promise.resolve({ data: list })
    }
    const id = url.split('/')[3]
    const entry = usage?.[id]
    return entry instanceof Error ? Promise.reject(entry) : Promise.resolve({ data: entry ?? {} })
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  api.get.mockReset()
})

describe('fetchPressureData — a fault is never an empty fleet', () => {
  it('accepts the real shape: a bare JSON array', async () => {
    route({ list: SUBS, usage: {} })
    const store = useSubscriptionsStore()
    await store.fetchPressureData()

    expect(store.listLoaded).toBe(true)
    expect(store.listError).toBe(false)
    expect(store.subscriptions).toHaveLength(2)
  })

  it('refuses to read an object (the paginated shape this endpoint does NOT use) as zero subscriptions', async () => {
    route({ list: { items: [] } })
    const store = useSubscriptionsStore()
    await store.fetchPressureData()

    expect(store.listError).toBe(true)
    expect(store.listLoaded).toBe(false)
  })

  it('refuses to read a null body as zero subscriptions', async () => {
    route({ list: null })
    const store = useSubscriptionsStore()
    await store.fetchPressureData()

    expect(store.listError).toBe(true)
    expect(store.listLoaded).toBe(false)
  })

  it('refuses to read an HTML interstitial as zero subscriptions', async () => {
    route({ list: '<!doctype html><title>Sign in</title>' })
    const store = useSubscriptionsStore()
    await store.fetchPressureData()

    expect(store.listError).toBe(true)
    expect(store.listLoaded).toBe(false)
  })

  it('THE REGRESSION: a malformed 200 cannot manufacture "No subscriptions configured"', async () => {
    route({ list: { unexpected: true } })
    const store = useSubscriptionsStore()
    await store.fetchPressureData()

    // The tile's state machine reads exactly these two flags; `loaded=false`
    // is what keeps it out of the empty branch.
    expect(store.listLoaded && !store.listError).toBe(false)
  })

  it('does not request usage for a roster it could not read', async () => {
    route({ list: null })
    const store = useSubscriptionsStore()
    await store.fetchPressureData()

    expect(api.get).toHaveBeenCalledTimes(1)
  })
})

describe('fetchPressureData — degradation keeps the last good reading', () => {
  it('keeps the previous roster when a later poll fails', async () => {
    route({ list: SUBS, usage: {} })
    const store = useSubscriptionsStore()
    await store.fetchPressureData()

    route({ list: new Error('network') })
    await store.fetchPressureData()

    // Stale-while-revalidate: a failed poll is not "no subscriptions", and the
    // tile discloses the staleness rather than blanking real rows.
    expect(store.subscriptions).toHaveLength(2)
    expect(store.listLoaded).toBe(true)
    expect(store.listError).toBe(true)
  })

  it('clears the error flag once a poll succeeds again', async () => {
    route({ list: new Error('down') })
    const store = useSubscriptionsStore()
    await store.fetchPressureData()
    expect(store.listError).toBe(true)

    route({ list: SUBS, usage: {} })
    await store.fetchPressureData()
    expect(store.listError).toBe(false)
  })

  it('isolates one subscription\'s failed usage read from its siblings', async () => {
    route({
      list: SUBS,
      usage: { s1: new Error('boom'), s2: { subscription_id: 's2', window_5h: {}, window_7d: {} } },
    })
    const store = useSubscriptionsStore()
    await store.fetchPressureData()

    // The failed one carries the honest marker the tile renders as
    // "unavailable"; the healthy one is untouched.
    expect(store.usageBySub.s1).toEqual({ error: true })
    expect(store.usageBySub.s2.subscription_id).toBe('s2')
    expect(store.listError).toBe(false)
  })

  it('requests usage once per subscription', async () => {
    route({ list: SUBS, usage: {} })
    const store = useSubscriptionsStore()
    await store.fetchPressureData()

    const usageCalls = api.get.mock.calls.filter(([url]) => url.endsWith('/usage'))
    expect(usageCalls).toHaveLength(2)
  })

  it('makes no usage requests for an empty roster', async () => {
    route({ list: [], usage: {} })
    const store = useSubscriptionsStore()
    await store.fetchPressureData()

    // A genuinely empty fleet: loaded, no error — the ONE input that lets the
    // tile show "No subscriptions configured".
    expect(store.listLoaded).toBe(true)
    expect(store.listError).toBe(false)
    expect(api.get).toHaveBeenCalledTimes(1)
  })
})
