/**
 * #2198 — in-flight de-duplication for the Agent Detail fetches.
 *
 * Several independent triggers ask for the same agent-scoped GET at the same
 * moment (both lifecycle hooks of a KeepAlive'd component, a route watcher, a
 * status watcher, and a sibling panel), so the page issued the same request
 * several times per mount.
 *
 * Neither existing precedent collapses that, which is why `utils/inflight.js`
 * is a third mechanism rather than a reuse:
 *
 *   - `stores/executions.js` is a RESULT cache: two simultaneous first-calls
 *     still both hit the network, and every duplicate here is concurrent.
 *   - `stores/fleetGrid.js` is an in-flight SKIP: the second caller returns
 *     with NO value, which `AgentDetail.loadAgent()` cannot use.
 *
 * The three semantics below are each load-bearing, and the second is the one a
 * naive "just cache it" implementation gets wrong:
 *
 *   JOIN      every caller resolves with the winner's value;
 *   NOT CACHE two SEQUENTIAL calls still issue two requests — `waitForAgentStatus`
 *             polls `fetchAgent` in a loop and a sticky entry freezes it forever;
 *   HONEST    a rejection reaches every joiner and clears the entry, so one
 *             failure never poisons the next attempt (`fetchAgent` re-throws and
 *             AgentDetail's 404 branch depends on it).
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
  useAuthStore: () => ({ authHeader: { Authorization: 'Bearer jwt' }, isAuthenticated: true }),
}))

// `create()` MUST return a DISTINCT object here, unlike the portal specs.
// `src/api.js` is imported transitively by the agents store and it reassigns
// `api.get` at module scope (its own PERF-269 GET dedupe). With `create: () =>
// inst` — the shape the portal specs use, where nothing imports api.js — that
// assignment lands on the shared mock and clobbers `axios.get`, and every
// `mockResolvedValue` below fails with "not a function".
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  const inst = mk()
  return { default: Object.assign(inst, { create: () => mk() }) }
})

import axios from 'axios'
import { dedupe, once, inFlightCount, resetInFlight } from '@/utils/inflight'
import { useAgentsStore } from '@/stores/agents'

/** A promise we resolve by hand, so both callers are provably in flight. */
function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  resetInFlight()
})

// ---------------------------------------------------------------------------
// The primitive
// ---------------------------------------------------------------------------

describe('#2198 dedupe() joins, it does not skip or cache', () => {
  it('two concurrent calls run the work ONCE and both get the value', async () => {
    let calls = 0
    const d = deferred()
    const fn = () => { calls++; return d.promise }

    const a = dedupe('k', fn)
    const b = dedupe('k', fn)
    d.resolve('v')

    expect(await a).toBe('v')
    expect(await b).toBe('v')
    expect(calls).toBe(1)
  })

  it('two SEQUENTIAL calls run the work TWICE — it is a dedupe, not a cache', async () => {
    // `AgentDetail.waitForAgentStatus()` polls `fetchAgent` in a loop after a
    // start/stop. A sticky entry would return the pre-transition agent forever.
    let calls = 0
    const fn = () => { calls++; return Promise.resolve(calls) }

    expect(await dedupe('k', fn)).toBe(1)
    expect(await dedupe('k', fn)).toBe(2)
  })

  it('a rejection reaches EVERY joiner and leaves the map clean', async () => {
    let calls = 0
    const d = deferred()
    const fn = () => { calls++; return d.promise }

    const a = dedupe('k', fn)
    const b = dedupe('k', fn)
    d.reject(new Error('boom'))

    await expect(a).rejects.toThrow('boom')
    await expect(b).rejects.toThrow('boom')
    expect(calls).toBe(1)
    expect(inFlightCount()).toBe(0)

    // and the next attempt is not poisoned
    await expect(dedupe('k', () => Promise.resolve('ok'))).resolves.toBe('ok')
  })

  it('different keys never collide', async () => {
    const a = dedupe('a', () => Promise.resolve('A'))
    const b = dedupe('b', () => Promise.resolve('B'))
    expect(await a).toBe('A')
    expect(await b).toBe('B')
  })

  it('a synchronous throw surfaces as a rejection, not as a raw throw', async () => {
    await expect(dedupe('k', () => { throw new Error('sync') })).rejects.toThrow('sync')
    expect(inFlightCount()).toBe(0)
  })
})

describe('#2198 once() additionally remembers the resolved value', () => {
  it('answers a LATER caller without a second request', async () => {
    // Required for feature-flags: the two stores that read it were measured
    // 319ms apart, so the second starts after the first has resolved and a
    // pure in-flight join does not reach it.
    let calls = 0
    const fn = () => { calls++; return Promise.resolve(calls) }

    expect(await once('f', fn)).toBe(1)
    expect(await once('f', fn)).toBe(1)
    expect(calls).toBe(1)
  })

  it('force bypasses the memory, so an explicit refresh still refetches', async () => {
    let calls = 0
    const fn = () => { calls++; return Promise.resolve(calls) }

    await once('f', fn)
    expect(await once('f', fn, { force: true })).toBe(2)
  })

  it('never caches a FAILURE — a later caller must be able to retry', async () => {
    let calls = 0
    const fn = () => { calls++; return Promise.reject(new Error('x')) }

    await expect(once('f', fn)).rejects.toThrow('x')
    await expect(once('f', fn)).rejects.toThrow('x')
    expect(calls).toBe(2)
  })
})

// ---------------------------------------------------------------------------
// The store methods that carry the eight duplicate classes
// ---------------------------------------------------------------------------

describe('#2198 agents store: concurrent duplicates collapse', () => {
  const cases = [
    ['fetchAgent', (s) => s.fetchAgent('acme'), { name: 'acme', circuit_breaker: null }],
    ['getAgentInfo', (s) => s.getAgentInfo('acme'), { capabilities: [] }],
    ['getAgentDashboard', (s) => s.getAgentDashboard('acme'), { has_dashboard: true }],
    ['checkDashboardExists', (s) => s.checkDashboardExists('acme'), { has_dashboard: true }],
  ]

  for (const [name, call, payload] of cases) {
    it(`${name}: two concurrent callers issue ONE request`, async () => {
      const store = useAgentsStore()
      const d = deferred()
      axios.get.mockReturnValue(d.promise)

      const a = call(store)
      const b = call(store)
      d.resolve({ data: payload })
      await Promise.all([a, b])

      expect(axios.get).toHaveBeenCalledTimes(1)
    })

    it(`${name}: both callers receive the same answer`, async () => {
      const store = useAgentsStore()
      const d = deferred()
      axios.get.mockReturnValue(d.promise)

      const a = call(store)
      const b = call(store)
      d.resolve({ data: payload })

      // fleetGrid-style SKIP would leave one of these undefined — the reason
      // that precedent could not be reused.
      const [ra, rb] = await Promise.all([a, b])
      expect(ra).toEqual(rb)
      expect(ra).not.toBeUndefined()
    })

    it(`${name}: a SEQUENTIAL second call still issues a request`, async () => {
      const store = useAgentsStore()
      axios.get.mockResolvedValue({ data: payload })

      await call(store)
      await call(store)

      expect(axios.get).toHaveBeenCalledTimes(2)
    })
  }

  it('fetchAgent still re-throws, so the 404 not-found branch survives', async () => {
    const store = useAgentsStore()
    const err = Object.assign(new Error('nope'), { response: { status: 404 } })
    axios.get.mockRejectedValue(err)

    await expect(store.fetchAgent('ghost')).rejects.toThrow('nope')
    // and a joiner of the SAME failed flight rejects too
    axios.get.mockRejectedValue(err)
    const [a, b] = [store.fetchAgent('ghost'), store.fetchAgent('ghost')]
    await expect(a).rejects.toThrow('nope')
    await expect(b).rejects.toThrow('nope')
  })

  it('fetchAgent for two DIFFERENT agents issues two requests', async () => {
    const store = useAgentsStore()
    axios.get.mockResolvedValue({ data: { name: 'x', circuit_breaker: null } })

    await Promise.all([store.fetchAgent('a'), store.fetchAgent('b')])
    expect(axios.get).toHaveBeenCalledTimes(2)
  })
})
