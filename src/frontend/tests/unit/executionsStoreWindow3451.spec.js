/**
 * #3451 — the fleet Executions list keeps what the reader loaded.
 *
 * The 30 s poll and the WebSocket refresh replaced `rows` with page one, so
 * every row Load more had added vanished within 30 s — and, once the list
 * scrolled inside its own region, the reader's place went with them. These
 * specs drive the real store against an in-memory `GET /api/executions` that
 * honours `limit`/`offset` the way the route does (newest first, `le=200`).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }
  return { default: inst }
})
import axios from 'axios'
import { useExecutionsStore } from '@/stores/executions'
import { REQUEST_TIMEOUT_MS } from '@/utils/boundedHttp'

const flush = () => new Promise((r) => setTimeout(r, 0))
const row = (n) => ({ id: `exec-${String(n).padStart(4, '0')}`, agent_name: 'a', status: 'success', started_at: `t${n}` })

/** The route: `total` rows newest-first; `arrive(k)` files k newer rows at the head. */
function serveExecutions(total) {
  let newest = total
  let list = Array.from({ length: total }, (_, i) => row(total - i))
  const calls = []
  const held = new Map()
  const between = new Map()
  axios.get.mockImplementation((url, config = {}) => {
    if (url === '/api/executions/stats') return Promise.resolve({ data: { total: list.length, running_count: 0 } })
    if (url !== '/api/executions') return Promise.reject(new Error(`unexpected GET ${url}`))
    const p = config.params || {}
    calls.push({ ...p })
    const n = calls.length
    // Answered now, delivered on release: a slow response carries the list as it was read.
    const source = p.status ? list.filter((r) => r.status === p.status) : list
    const answer = { data: source.slice(p.offset || 0, (p.offset || 0) + p.limit).map((r) => ({ ...r })) }
    between.get(n)?.()
    const gate = held.get(n)
    return gate ? gate.then(() => answer) : Promise.resolve(answer)
  })
  return {
    calls,
    listReads: () => calls.filter((c) => c.limit != null),
    arrive(k, extra = {}) {
      const fresh = Array.from({ length: k }, () => ({ ...row(++newest), ...extra }))
      list = [...fresh.reverse(), ...list]
    },
    /** Hold the n-th call until the returned release() runs. Register before it is made. */
    hold(n) {
      let release
      held.set(n, new Promise((r) => { release = r }))
      return () => release()
    },
    /** Run fn right after the n-th call is answered (before the store's next call). */
    after(n, fn) { between.set(n, fn) },
  }
}

const ids = (store) => store.rows.map((r) => r.id)

describe('executions store — a refresh keeps the loaded window (#3451)', () => {
  let store
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    store = useExecutionsStore()
  })
  afterEach(() => {
    store.stopPolling()
    vi.useRealTimers()
  })

  it('a refresh while the list is live re-reads every row Load more added', async () => {
    serveExecutions(400)
    store.startPolling(30000)
    await flush()
    expect(store.rows).toHaveLength(50)

    await store.loadMore()
    expect(store.rows).toHaveLength(100)

    await store.refresh()
    expect(store.rows).toHaveLength(100)
    expect(ids(store)[99]).toBe('exec-0301')
    expect(store.hasMore).toBe(true)
  })

  it('an exhausted list grows no Load more on refresh (one row past the window is read, not guessed)', async () => {
    serveExecutions(87)
    store.startPolling(30000)
    await flush()
    await store.loadMore()
    expect(store.rows).toHaveLength(87)
    expect(store.hasMore).toBe(false)

    await store.refresh()
    expect(store.rows).toHaveLength(87)
    expect(store.hasMore).toBe(false)
  })

  it('a list of exactly one page offers no Load more', async () => {
    serveExecutions(50)
    store.startPolling(30000)
    await flush()
    expect(store.rows).toHaveLength(50)
    expect(store.hasMore).toBe(false)
  })

  it('a window past the route\'s 200-row cap is read in pages of at most 200', async () => {
    const route = serveExecutions(400)
    store.startPolling(30000)
    await flush()
    for (let i = 0; i < 4; i += 1) await store.loadMore()
    expect(store.rows).toHaveLength(250)

    const before = route.listReads().length
    await store.refresh()
    const walk = route.listReads().slice(before)
    expect(walk.map((c) => [c.offset, c.limit])).toEqual([[0, 200], [200, 51]])
    expect(store.rows).toHaveLength(250)
    expect(store.hasMore).toBe(true)
  })

  it('rows that shift between two pages of a walk are shown once, and the window stays whole', async () => {
    const route = serveExecutions(400)
    store.startPolling(30000)
    await flush()
    for (let i = 0; i < 4; i += 1) await store.loadMore()

    // Three runs start between the walk's two requests: page two repeats the
    // last three rows of page one (the route has no tiebreak and pages by offset).
    // Dropping the repeats must not shrink the window or hide Load more.
    route.after(route.calls.length + 1, () => route.arrive(3)) // after the walk's first page
    await store.refresh()
    expect(new Set(ids(store)).size).toBe(store.rows.length)
    expect(store.rows).toHaveLength(250)
    expect(store.hasMore).toBe(true)
  })

  it('Load more does not repeat rows that shifted since the page before it', async () => {
    const route = serveExecutions(400)
    store.startPolling(30000)
    await flush()
    route.arrive(2)
    await store.loadMore()
    expect(new Set(ids(store)).size).toBe(store.rows.length)
  })

  it('a filter change outranks a poll in flight: the poll\'s late answer is dropped', async () => {
    const route = serveExecutions(400)
    store.startPolling(30000)
    await flush()
    // The next list read (the refresh) is held; the filter read after it is not.
    const release = route.hold(route.calls.length + 1)
    const poll = store.refresh()
    store.setFilter('status', 'failed')
    await flush()
    release()
    await poll
    await flush()
    expect(store.rows).toHaveLength(0) // no failed runs — never the poll's 50 unfiltered rows
  })

  it('a poll tick during Load more waits for it, then re-reads the whole window once', async () => {
    vi.useFakeTimers()
    const route = serveExecutions(400)
    store.startPolling(30000)
    await vi.advanceTimersByTimeAsync(0)
    const release = route.hold(route.calls.length + 1)
    const more = store.loadMore()
    const reads = route.listReads().length
    await vi.advanceTimersByTimeAsync(30_000)
    expect(route.listReads().length).toBe(reads) // nothing raced the append
    release()
    await more
    await vi.advanceTimersByTimeAsync(0)
    expect(route.listReads().slice(reads)).toEqual([{ limit: 101, offset: 0 }]) // ... and nothing was dropped
    expect(store.rows).toHaveLength(100)
  })

  it('a schedule event or the Stop refresh during Load more is not dropped, and several coalesce into one', async () => {
    const route = serveExecutions(400)
    store.startPolling(30000)
    await flush()
    const release = route.hold(route.calls.length + 1)
    const more = store.loadMore()
    const reads = route.listReads().length
    store.handleWebSocketEvent({ type: 'agent_activity', activity_type: 'schedule_end' })
    store.handleWebSocketEvent({ type: 'agent_activity', activity_type: 'schedule_start' })
    store.refresh() // ExecutionsPanel's refresh after a Stop
    await flush()
    expect(route.listReads().length).toBe(reads)
    release()
    await more
    await flush()
    expect(route.listReads().slice(reads)).toEqual([{ limit: 101, offset: 0 }])
    expect(store.rows).toHaveLength(100)
  })

  it('a schedule event reads nothing while no panel shows the list, and refreshes it while one does', async () => {
    const route = serveExecutions(400)
    store.handleWebSocketEvent({ type: 'agent_activity', activity_type: 'schedule_start' })
    await flush()
    expect(route.listReads()).toHaveLength(0)

    store.startPolling(30000)
    await flush()
    const reads = route.listReads().length
    store.handleWebSocketEvent({ type: 'agent_activity', activity_type: 'schedule_end' })
    await flush()
    expect(route.listReads().length).toBe(reads + 1)
  })

  it('every list read is bounded: one that never settles cannot hold the list forever', async () => {
    // The poll timer, the WebSocket refresh, Refresh and Load more all wait on
    // `loading`, so a read that never settled froze the list until a filter
    // change (learnings 2026-10-19: a wait-for-the-read-in-flight guard is only
    // as live as the request it waits on). A mock cannot hang — assert the bound.
    const route = serveExecutions(400)
    store.startPolling(30000)
    await flush()
    await store.loadMore()
    await store.refresh()
    const lists = axios.get.mock.calls.filter(([url]) => url === '/api/executions')
    expect(lists.length).toBeGreaterThanOrEqual(3)
    for (const [, config] of lists) expect(config.timeout).toBe(REQUEST_TIMEOUT_MS)
    expect(route.listReads().length).toBe(lists.length)
  })

  it('a panel that mounts again starts from page one', async () => {
    const route = serveExecutions(400)
    store.startPolling(30000)
    await flush()
    await store.loadMore()
    store.stopPolling()

    store.startPolling(30000)
    await flush()
    expect(route.listReads().at(-1)).toMatchObject({ offset: 0, limit: 51 })
    expect(store.rows).toHaveLength(50)
  })
})
