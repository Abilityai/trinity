/**
 * #3302 — the fleet grid's "needs response" chip reads every pending ask.
 *
 * The chip map came from one `status=pending, limit: 200` page. An agent whose
 * asks all sat past it got no chip, and an agent with asks on both sides of the
 * cut reported a younger "oldest" age than it had. The chip has nowhere to say
 * "truncated", so the read walks (determined in the dossier, not chosen).
 * Driven through the store's real `refreshBatchData`, against the in-memory
 * route (`helpers/fakeOperatorQueue.js`).
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
  useAuthStore: () => ({ authHeader: { Authorization: 'Bearer jwt' }, user: { role: 'user' } }),
}))
vi.mock('@/stores/executions', () => ({
  useExecutionsStore: () => ({ fetchAgentAnalytics: vi.fn() }),
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
import { useFleetGridStore, OP_QUEUE_PARTIAL_GRACE_MS } from '@/stores/fleetGrid'
import { makeQueue, rowsOf } from './helpers/fakeOperatorQueue'
import { REQUEST_TIMEOUT_MS } from '@/utils/boundedHttp'

function serve(queue) {
  axios.get.mockImplementation((url, config = {}) =>
    url === '/api/operator-queue'
      ? queue.get(config.params)
      : Promise.resolve({ data: {}, headers: {} }))
}

// `late` filed its asks first, so they are the OLDEST: last in the route's
// pending order, past a first page of `busy`'s newer ones.
function fleet(busyCount) {
  return [
    ...rowsOf(5, { prefix: 'late', agent_name: 'agent-late', type: 'approval', at: '2026-01-01T00:00:00Z' }),
    ...rowsOf(busyCount, { prefix: 'busy', agents: 20, at: '2026-06-01T00:00:00Z' }),
  ]
}

describe('fleet grid — operator-queue chip data past the old 200-row page (#3302)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('an agent whose asks all sit past the first page still gets its chip, with its true count and age', async () => {
    serve(makeQueue(fleet(240)))
    const store = useFleetGridStore()
    await store.refreshBatchData()
    expect(store.opQueuePending['agent-late']).toEqual({
      count: 5,
      oldestCreatedAt: '2026-01-01T00:00:00.000Z',
      hasApproval: true,
    })
    expect(Object.keys(store.opQueuePending)).toHaveLength(21)
  })

  it('a read that cannot finish after a complete one keeps the last complete map (no chip silently dropped)', async () => {
    const opts = {}
    serve(makeQueue(fleet(520), opts))
    const store = useFleetGridStore()
    await store.refreshBatchData()
    expect(store.opQueuePending['agent-late']?.count).toBe(5)

    opts.walkError = { status: 503, on: 'start' }
    await store.refreshBatchData()
    expect(store.opQueuePending['agent-late']?.count).toBe(5)
  })

  it('the last complete map is trusted only for a grace period; after it, partial reads are shown', async () => {
    // Redis down with more than a page pending makes EVERY read partial; the
    // chips must not stay frozen on a map from before the outage forever.
    const opts = {}
    serve(makeQueue(fleet(520), opts))
    const store = useFleetGridStore()
    const now = vi.spyOn(Date, 'now')
    const t0 = Date.parse('2026-10-20T12:00:00Z')
    now.mockReturnValue(t0)
    await store.refreshBatchData() // complete
    opts.walkError = { status: 503, on: 'start' }
    now.mockReturnValue(t0 + 60_000)
    await store.refreshBatchData() // partial, inside the grace period: last complete map kept
    expect(store.opQueuePending['agent-late']?.count).toBe(5)
    now.mockReturnValue(t0 + OP_QUEUE_PARTIAL_GRACE_MS + 1_000)
    await store.refreshBatchData() // partial, past it: the partial map, which cannot see agent-late
    expect(store.opQueuePending['agent-late']).toBeUndefined()
    expect(Object.keys(store.opQueuePending).length).toBeGreaterThan(0)
    now.mockRestore()
  })

  it('the chip read is bounded, so a hung request cannot hold the shared read forever', async () => {
    serve(makeQueue(fleet(240)))
    await useFleetGridStore().refreshBatchData()
    const queueCalls = axios.get.mock.calls.filter(([url]) => url === '/api/operator-queue')
    expect(queueCalls.length).toBeGreaterThan(0)
    expect(queueCalls.every(([, config]) => config.timeout === REQUEST_TIMEOUT_MS)).toBe(true)
  })

  it('overlapping refreshes share one read instead of racing to write the map', async () => {
    const queue = makeQueue(fleet(10))
    serve(queue)
    const store = useFleetGridStore()
    await Promise.all([store.refreshBatchData(), store.refreshBatchData()])
    expect(queue.calls.filter((c) => c.status === 'pending' && !c.cursor)).toHaveLength(1)
  })
})
