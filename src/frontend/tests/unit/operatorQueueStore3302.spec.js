/**
 * #3302 — the Operations store's counts and lists stay true past any window.
 *
 * The store read one `limit: 200` page and derived the NavBar badge, the
 * Operations subtitle, the Needs Response list and the Resolved list from it.
 * Past 200 pending every count stopped at 200 and — because the route lists
 * pending rows first — the Resolved list read EMPTY. These specs drive the real
 * store against the in-memory route (`helpers/fakeOperatorQueue.js`).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }
  return { default: inst }
})
vi.mock('@/api', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})
import axios from 'axios'
import api from '@/api'
import { useOperatorQueueStore } from '@/stores/operatorQueue'
import { QUEUE_RESPONSE_NOT_RECORDED, bulkCancelBatches, BULK_CANCEL_MAX_IDS } from '@/utils/operatorQueue'
import { makeQueue, rowsOf } from './helpers/fakeOperatorQueue'
import { REQUEST_TIMEOUT_MS } from '@/utils/boundedHttp'

const flush = () => new Promise((r) => setTimeout(r, 0))

/** Route the store's reads to a fake; `hold(n)` keeps the n-th read pending until released. */
function serve(queue) {
  const held = new Map()
  let invocations = 0
  axios.get.mockImplementation((url, config = {}) => {
    if (url !== '/api/operator-queue') return Promise.reject(new Error(`unexpected ${url}`))
    const gate = held.get(++invocations)
    // A held read is ANSWERED now and DELIVERED on release — a slow response
    // carries the state the server had when it read, not the state at arrival.
    const answer = queue.get(config.params)
    return gate ? gate.promise.then(() => answer) : answer
  })
  return {
    /** Hold the n-th read (counted from the first call to axios.get). Register it BEFORE the read starts. */
    hold(n) {
      let release
      const promise = new Promise((r) => { release = r })
      held.set(n, { promise })
      return () => release()
    },
  }
}

const listReads = (queue) => queue.calls.filter((c) => c.status == null)

describe('operatorQueue store — counts and lists past the old 200-row window (#3302)', () => {
  let store
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    store = useOperatorQueueStore()
  })
  afterEach(() => store.stopPolling())

  it('every pending ask is listed and counted, and the Resolved list is not crowded out', async () => {
    const pending = [
      ...rowsOf(240, { agents: 12 }),
      ...rowsOf(10, { prefix: 'crit', priority: 'critical', agents: 5 }),
    ]
    const resolved = rowsOf(300, { prefix: 'done', status: 'responded', agents: 7 })
    serve(makeQueue([...pending, ...resolved]))
    await store.fetchItems()

    expect(store.openItems).toHaveLength(250)
    expect(store.pendingCount).toBe(250)
    expect(store.criticalCount).toBe(10)
    expect(store.pendingComplete).toBe(true)
    expect(Object.keys(store.openItemsByAgent)).toHaveLength(12)
    expect(store.resolvedItems).toHaveLength(200)
    expect(store.resolvedTotal).toBe(300)
    expect(store.hasLoaded).toBe(true)
  })

  it('a walk that cannot finish leaves a stated partial list with the exact count and a read critical count', async () => {
    const queue = makeQueue([
      ...rowsOf(700, { agents: 28 }),
      ...rowsOf(3, { prefix: 'crit', priority: 'critical' }),
    ], { walkError: { status: 503, on: 'start' } })
    serve(queue)
    await store.fetchItems()

    expect(store.pendingComplete).toBe(false)
    expect(store.openItems.length).toBeLessThan(703)
    expect(store.pendingCount).toBe(703)
    expect(store.pendingCountExact).toBe(true)
    expect(store.criticalCount).toBe(3)
    expect(queue.calls).toContainEqual({ status: 'pending', priority: 'critical', limit: 1 })
  })

  it('off the Operations page a WebSocket trigger reads the two counts, not the list', async () => {
    const queue = makeQueue([
      ...rowsOf(260, { agents: 11 }),
      ...rowsOf(4, { prefix: 'crit', priority: 'critical' }),
      ...rowsOf(50, { prefix: 'done', status: 'cancelled' }),
    ])
    serve(queue)
    store.handleWebSocketEvent({ type: 'operator_queue_new', data: {} })
    await flush()

    expect(queue.calls).toEqual([
      { status: 'pending', limit: 1 },
      { status: 'pending', priority: 'critical', limit: 1 },
    ])
    expect(store.pendingCount).toBe(264)
    expect(store.criticalCount).toBe(4)
  })

  it('on the Operations page (polling) the same trigger refreshes the list', async () => {
    const queue = makeQueue(rowsOf(3))
    serve(queue)
    store.startPolling(60_000)
    await flush()
    const before = listReads(queue).length
    store.handleWebSocketEvent({ type: 'operator_queue_sync', data: {} })
    await flush()
    expect(listReads(queue).length).toBeGreaterThan(before)
  })

  it('a count read that lands after the list went live does not overwrite the list\'s count', async () => {
    // Started on another page by a queue event, slower than the list read the
    // Operations page then makes: the list owns the counts once it is live.
    const queue = makeQueue(rowsOf(5))
    const { hold } = serve(queue)
    const releaseAll = hold(1)
    const releaseCritical = hold(2)
    store.fetchCounts() // answered now (5 pending), delivered late
    Object.assign(queue.rows[0], { status: 'responded', disposed_at: '2026-12-01T00:00:00Z' })
    store.startPolling(60_000) // read 3: the list, 4 pending
    await flush()
    expect(store.pendingCount).toBe(4)
    releaseAll()
    releaseCritical()
    await flush()
    expect(store.pendingCount).toBe(4)
    expect(store.openItems).toHaveLength(4)
  })

  it('a count read overtaken by an answer is dropped, not painted over the answer', async () => {
    const queue = makeQueue(rowsOf(5))
    const { hold } = serve(queue)
    await store.fetchItems() // read 1: the list, not live
    const releaseAll = hold(2)
    const releaseCritical = hold(3)
    store.fetchCounts() // answered now (5 pending), delivered late
    axios.post.mockResolvedValueOnce({ data: {} })
    await store.respondToItem(store.openItems[0].id, 'yes', '')
    expect(store.pendingCount).toBe(4)
    releaseAll()
    releaseCritical()
    await flush()
    expect(store.pendingCount).toBe(4)
  })

  it('a burst of triggers while a read is out costs one more read, not one per trigger', async () => {
    const queue = makeQueue(rowsOf(3))
    const { hold } = serve(queue)
    const release = hold(1)
    store.startPolling(60_000) // the initial read is call 1, held
    for (let i = 0; i < 6; i += 1) store.handleWebSocketEvent({ type: 'operator_queue_sync', data: {} })
    release()
    await flush(); await flush()
    expect(listReads(queue)).toHaveLength(2)
  })

  it('a read that left before an answer was recorded cannot paint the answered card back', async () => {
    const queue = makeQueue(rowsOf(2))
    const { hold } = serve(queue)
    await store.fetchItems() // read 1
    const answered = store.openItems[0].id
    const release = hold(2)
    const stale = store.fetchItems() // read 2 leaves while the ask is still pending
    axios.post.mockResolvedValueOnce({ data: {} })
    expect(await store.respondToItem(answered, 'yes', '')).toBe(true)
    // the server now holds the answer; the read already out does not
    Object.assign(queue.rows.find((r) => r.id === answered), { status: 'responded', disposed_at: '2026-12-01T00:00:00Z' })
    release()
    await stale
    expect(store.items.find((i) => i.id === answered).status).toBe('responded')
  })

  it('a read overtaken by an answer reads again, so whoever awaits it sees the state after the answer', async () => {
    const queue = makeQueue(rowsOf(2))
    const { hold } = serve(queue)
    await store.fetchItems() // read 1
    const [a, b] = store.openItems.map((i) => i.id)
    const release = hold(2)
    const awaited = store.fetchItems() // read 2 leaves, then an answer overtakes it
    axios.post.mockResolvedValueOnce({ data: {} })
    await store.respondToItem(a, 'yes', '')
    // the server now holds the answer, and b's agent changed b meanwhile
    Object.assign(queue.rows.find((r) => r.id === a), { status: 'responded', disposed_at: '2026-12-01T00:00:00Z' })
    Object.assign(queue.rows.find((r) => r.id === b), { sync_state: 'changed', sync_detail: 'question' })
    release()
    await awaited
    expect(store.items.find((i) => i.id === b).sync_state).toBe('changed')
    expect(store.items.find((i) => i.id === a).status).toBe('responded')
  })

  it('the not-recorded notice survives the read a queue event queued behind the refetch', async () => {
    // A 409 means someone else ended the ask, and that ending broadcasts — so a
    // queue event landing while the respond's refetch is out is the common case.
    const queue = makeQueue(rowsOf(2))
    const { hold } = serve(queue)
    store.startPolling(60_000) // read 1
    await flush()
    axios.post.mockRejectedValueOnce({ response: { status: 409, data: { detail: 'gone' } } })
    const release = hold(2) // the respond's refetch
    const answering = store.respondToItem(store.openItems[0].id, 'yes', '')
    await flush()
    store.handleWebSocketEvent({ type: 'operator_queue_cancelled', data: {} })
    release()
    expect(await answering).toBe(false)
    await flush()
    expect(store.error).toBe(QUEUE_RESPONSE_NOT_RECORDED)
  })

  it('the not-recorded notice also survives when the refetch joins a read that was already out', async () => {
    const queue = makeQueue(rowsOf(2))
    const { hold } = serve(queue)
    await store.fetchItems() // read 1
    const id = store.openItems[0].id
    const release = hold(2)
    store.fetchItems() // read 2 — a poll — is out when the answer is refused
    axios.post.mockRejectedValueOnce({ response: { status: 409, data: { detail: 'gone' } } })
    const answering = store.respondToItem(id, 'yes', '') // its refetch joins read 2 and queues one more
    await flush()
    release()
    expect(await answering).toBe(false)
    await flush()
    expect(store.error).toBe(QUEUE_RESPONSE_NOT_RECORDED)
  })

  it('an answer marks the row on screen even when a read replaced the list while the POST was out', async () => {
    serve(makeQueue(rowsOf(3)))
    await store.fetchItems()
    const id = store.openItems[0].id
    let resolvePost
    axios.post.mockImplementationOnce(() => new Promise((r) => { resolvePost = r }))
    const answering = store.respondToItem(id, 'yes', '')
    await store.fetchItems() // lands while the POST is out: new row objects
    resolvePost({ data: {} })
    expect(await answering).toBe(true)
    expect(store.items.find((i) => i.id === id).status).toBe('responded')
    expect(store.openItems).toHaveLength(2)
    expect(store.pendingCount).toBe(2)
  })

  it('an answer takes the badge down at once, critical included', async () => {
    serve(makeQueue([...rowsOf(5), ...rowsOf(2, { prefix: 'crit', priority: 'critical' })]))
    await store.fetchItems()
    expect(store.pendingCount).toBe(7)
    const crit = store.openItems.find((i) => i.priority === 'critical')
    axios.post.mockResolvedValueOnce({ data: {} })
    await store.respondToItem(crit.id, 'yes', '')
    expect(store.pendingCount).toBe(6)
    expect(store.criticalCount).toBe(1)
  })

  it('every queue read is bounded: one hung request cannot freeze the one-at-a-time read for the life of the tab', async () => {
    serve(makeQueue([...rowsOf(260), ...rowsOf(30, { prefix: 'done', status: 'expired' })]))
    await store.fetchItems()
    await store.fetchCounts()
    const calls = axios.get.mock.calls
    expect(calls.length).toBeGreaterThan(3)
    expect(calls.every(([, config]) => config.timeout === REQUEST_TIMEOUT_MS)).toBe(true)
  })

  it('a failed refresh keeps the list and the counts on screen', async () => {
    const queue = makeQueue(rowsOf(4), { fail: (_p, n) => n > 1 })
    serve(queue)
    await store.fetchItems()
    await store.fetchItems()
    expect(store.error).toBeTruthy()
    expect(store.openItems).toHaveLength(4)
    expect(store.pendingCount).toBe(4)
  })
})

describe('operatorQueue store — Clear All over the 500-id route limit (#3302)', () => {
  let store
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    store = useOperatorQueueStore()
  })

  it('cancels every id in batches of at most 500, each agent\'s asks in exactly one batch', async () => {
    serve(makeQueue(rowsOf(700, { agents: 28 }))) // 25 per agent, the per-agent cap
    await store.fetchItems()
    api.post.mockImplementation((_url, body) => Promise.resolve({ data: { cancelled: body.ids.length, skipped: 0, batch_id: 'b' } }))
    const ids = store.openItems.map((i) => i.id)

    const result = await store.bulkCancel(ids)

    const batches = api.post.mock.calls.map(([, body]) => body.ids)
    expect(batches.length).toBeGreaterThan(1)
    expect(batches.every((b) => b.length <= 500)).toBe(true)
    expect(batches.flat().sort()).toEqual([...ids].sort())
    const agentOf = new Map(store.items.map((i) => [i.id, i.agent_name]))
    const batchesPerAgent = new Map()
    batches.forEach((b, n) => b.forEach((id) => {
      const set = batchesPerAgent.get(agentOf.get(id)) || new Set()
      set.add(n)
      batchesPerAgent.set(agentOf.get(id), set)
    }))
    expect([...batchesPerAgent.values()].every((s) => s.size === 1)).toBe(true)
    expect(result).toMatchObject({ cancelled: 700, skipped: 0 })
  })

  it('a failed batch after a good one refetches first, and its error is still on screen afterwards', async () => {
    const queue = makeQueue(rowsOf(700, { agents: 28 }))
    serve(queue)
    await store.fetchItems()
    const readsBefore = listReads(queue).length
    api.post
      .mockResolvedValueOnce({ data: { cancelled: 500, skipped: 0, batch_id: 'b1' } })
      .mockRejectedValueOnce({ response: { status: 500, data: { detail: 'boom' } } })

    await expect(store.bulkCancel(store.openItems.map((i) => i.id))).rejects.toBeTruthy()
    expect(listReads(queue).length).toBeGreaterThan(readsBefore)
    expect(store.error).toBeTruthy()
    expect(store.error).not.toBe(QUEUE_RESPONSE_NOT_RECORDED)
  })
})

describe('bulkCancelBatches — whole agents per batch (#3302)', () => {
  const asks = (agent, n) => Array.from({ length: n }, (_, i) => ({ id: `${agent}-${i}`, agent_name: agent }))

  it('is the route limit', () => {
    expect(BULK_CANCEL_MAX_IDS).toBe(500)
  })

  it('a list within the limit is one batch, in the order given', () => {
    const items = [...asks('a', 3), ...asks('b', 2)]
    expect(bulkCancelBatches(items)).toEqual([items.map((i) => i.id)])
  })

  it('never splits an agent that fits, packing whole agents up to the limit', () => {
    const items = [...asks('a', 3), ...asks('b', 3), ...asks('c', 3)]
    expect(bulkCancelBatches(items, 5)).toEqual([
      ['a-0', 'a-1', 'a-2'],
      ['b-0', 'b-1', 'b-2'],
      ['c-0', 'c-1', 'c-2'],
    ])
  })

  it('splits only an agent larger than the limit on its own', () => {
    const items = [...asks('a', 2), ...asks('big', 7), ...asks('c', 1)]
    const batches = bulkCancelBatches(items, 3)
    expect(batches.every((b) => b.length <= 3)).toBe(true)
    expect(batches.flat()).toEqual(items.map((i) => i.id))
    expect(batches.find((b) => b.includes('a-0'))).toEqual(['a-0', 'a-1'])
  })

  it('an empty list is no batches', () => {
    expect(bulkCancelBatches([])).toEqual([])
  })
})
