/**
 * The Workspace Reports tab's store contract (#2162).
 *
 * Wiring the shared renderers into the tab is mostly template work, but it
 * moves three failure modes into the store, and each of them is invisible to a
 * source-structure guard:
 *
 *   1. **The agent-switch race.** A reset cannot cancel a promise already in
 *      flight. Today the wrong render is transient — the tab guard is
 *      `!reports.length`, so opening Reports on the new agent refetches and
 *      self-corrects. The moment a `reportsLoaded` flag is added (and contract
 *      #15 requires one, so the empty state stops firing after a failure) that
 *      accidental correction is GONE: the new agent is marked
 *      loaded-with-the-old-agent's-data for the life of the mount. The flag and
 *      the generation guard have to ship together, which is why they are pinned
 *      together here.
 *
 *   2. **The "Load more" terminal guard.** Without it a click at
 *      `loaded === total` appends nothing, `loaded` never moves, and
 *      `ReportTable`'s `meta.total > rows.length` never goes false — a
 *      permanently visible, permanently inert button.
 *
 *   3. **Windowed vs whole.** `row_meta` is the ONLY signal that a payload was
 *      actually paged. Synthesising one for a bounded document would put a
 *      Load-more footer under a KPI tile set that can never satisfy it.
 *
 * Store fetchers ARE testable in this project even without a component-mount
 * harness — `fleetGridFailuresFetch.spec.js` established the vi.mock('axios') +
 * Pinia shape, and its subject is the same class of proof: that the store only
 * ever hands the UI an honest input.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

// The store reads localStorage at state construction and installs an axios
// interceptor at import; vitest runs these in `environment: 'node'`. Harness
// requirement, same shape as workspaceRoomsGate.spec.js.
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

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({
    isAuthenticated: true,
    authHeader: { Authorization: 'Bearer platform-jwt' },
  }),
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
import { useClientPortalStore } from '@/stores/clientPortal'
import { REPORT_ROWS_PAGE } from '@/utils/reportPaging'

/** A deferred promise, so a response can be resolved AFTER an agent switch. */
function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const tabular = (total, from = 0, count = REPORT_ROWS_PAGE) => ({
  payload: {
    columns: ['name'],
    rows: Array.from({ length: count }, (_, i) => [`row-${from + i}`]),
  },
  row_meta: { total, offset: from, limit: REPORT_ROWS_PAGE },
})

let store

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useClientPortalStore()
  store.resetAgentReports('alpha')
})

// ---------------------------------------------------------------------------
// 1. The agent-switch race
// ---------------------------------------------------------------------------

describe('the agent-switch race', () => {
  it("discards agent A's list when it resolves after a switch to B", async () => {
    const inflight = deferred()
    axios.get.mockReturnValueOnce(inflight.promise)

    const loading = store.loadAgentReports('alpha')

    // The user clicks agent B while A's request is still open.
    store.resetAgentReports('beta')

    inflight.resolve({ data: { reports: [{ id: 'a1', title: "alpha's report" }] } })
    await loading

    expect(store.reports).toEqual([])
    // The decisive assertion: B must NOT be marked loaded. If it were, the tab
    // guard would skip the refetch and A's reports would render under B's name
    // for the life of the mount.
    expect(store.reportsLoaded).toBe(false)
    expect(store.reportsAgent).toBe('beta')
  })

  it("discards agent A's FAILURE after a switch, so B shows no stale error", async () => {
    const inflight = deferred()
    axios.get.mockReturnValueOnce(inflight.promise)

    const loading = store.loadAgentReports('alpha')
    store.resetAgentReports('beta')
    inflight.reject(new Error('network'))
    await loading

    expect(store.reportsError).toBeNull()
  })

  it('discards a late report PAYLOAD, so one agent\'s report cannot open under another', async () => {
    const inflight = deferred()
    axios.get.mockReturnValueOnce(inflight.promise)

    const loading = store.loadAgentReport('alpha', 'r1')
    store.resetAgentReports('beta')
    inflight.resolve({ data: { payload: { secret: 'alpha only' } } })
    await loading

    expect(store.reportPayloads).toEqual({})
  })

  it('discards a late "load more" page rather than merging it into another agent', async () => {
    axios.get.mockResolvedValueOnce({ data: tabular(500) })
    await store.loadAgentReport('alpha', 'r1')

    const inflight = deferred()
    axios.get.mockReturnValueOnce(inflight.promise)
    const more = store.loadMoreReportRows('alpha', 'r1')

    store.resetAgentReports('beta')
    inflight.resolve({ data: tabular(500, REPORT_ROWS_PAGE) })
    await more

    expect(store.reportPayloads).toEqual({})
    expect(store.reportRowMeta).toEqual({})
  })

  it('self-corrects when a load is issued for a different agent without a reset', async () => {
    axios.get.mockResolvedValueOnce({ data: { reports: [{ id: 'a1' }] } })
    await store.loadAgentReports('alpha')
    expect(store.reports).toHaveLength(1)

    axios.get.mockResolvedValueOnce({ data: { reports: [] } })
    await store.loadAgentReports('beta')

    expect(store.reportsAgent).toBe('beta')
    expect(store.reports).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// 2. "Load more"
// ---------------------------------------------------------------------------

describe('load more', () => {
  it('issues no request once every row is held', async () => {
    // total === one page, so the first fetch already holds everything.
    axios.get.mockResolvedValueOnce({ data: tabular(REPORT_ROWS_PAGE) })
    await store.loadAgentReport('alpha', 'r1')
    expect(store.reportRowMeta.r1).toEqual({ total: REPORT_ROWS_PAGE, loaded: REPORT_ROWS_PAGE })

    axios.get.mockClear()
    await store.loadMoreReportRows('alpha', 'r1')

    expect(axios.get).not.toHaveBeenCalled()
  })

  it('issues no request for a report that was never windowed', async () => {
    axios.get.mockResolvedValueOnce({ data: { payload: { tiles: [] } } })
    await store.loadAgentReport('alpha', 'r1')

    axios.get.mockClear()
    await store.loadMoreReportRows('alpha', 'r1')

    expect(axios.get).not.toHaveBeenCalled()
  })

  it('appends the next page and advances loaded against a stable total', async () => {
    axios.get.mockResolvedValueOnce({ data: tabular(250) })
    await store.loadAgentReport('alpha', 'r1')

    axios.get.mockResolvedValueOnce({ data: tabular(250, REPORT_ROWS_PAGE) })
    await store.loadMoreReportRows('alpha', 'r1')

    expect(store.reportPayloads.r1.rows).toHaveLength(REPORT_ROWS_PAGE * 2)
    expect(store.reportPayloads.r1.rows[REPORT_ROWS_PAGE][0]).toBe(`row-${REPORT_ROWS_PAGE}`)
    expect(store.reportRowMeta.r1).toEqual({ total: 250, loaded: REPORT_ROWS_PAGE * 2 })
    // The offset asked for is where the previous page ended, not a page index.
    expect(axios.get.mock.calls.at(-1)[1].params).toEqual({
      rows_offset: REPORT_ROWS_PAGE, rows_limit: REPORT_ROWS_PAGE,
    })
  })
})

// ---------------------------------------------------------------------------
// 3. Windowed vs whole
// ---------------------------------------------------------------------------

describe('the fetch shape', () => {
  it('always asks for a window — the SERVER decides whether one applies', async () => {
    axios.get.mockResolvedValueOnce({ data: tabular(500) })
    await store.loadAgentReport('alpha', 'r1')

    const [url, config] = axios.get.mock.calls[0]
    expect(url).toContain('/agents/alpha/reports/r1')
    expect(config.params).toEqual({ rows_offset: 0, rows_limit: REPORT_ROWS_PAGE })
  })

  it('threads row_meta through as {total, loaded}', async () => {
    axios.get.mockResolvedValueOnce({ data: tabular(12431) })
    await store.loadAgentReport('alpha', 'r1')

    expect(store.reportRowMeta.r1).toEqual({ total: 12431, loaded: REPORT_ROWS_PAGE })
  })

  it('records NO meta when the server sent none, so no paging footer renders', async () => {
    axios.get.mockResolvedValueOnce({ data: { payload: { markdown: '# hi' } } })
    await store.loadAgentReport('alpha', 'r1')

    expect(store.reportPayloads.r1).toEqual({ markdown: '# hi' })
    expect(store.reportRowMeta.r1).toBeUndefined()
  })

  it('records no meta when row_meta arrives without a row list', async () => {
    // Defensive: meta without rows would produce {total: N, loaded: undefined}
    // and a footer comparing against NaN.
    axios.get.mockResolvedValueOnce({ data: { payload: { tiles: [] }, row_meta: { total: 9 } } })
    await store.loadAgentReport('alpha', 'r1')

    expect(store.reportRowMeta.r1).toBeUndefined()
  })
})

// ---------------------------------------------------------------------------
// 4. Honest states
// ---------------------------------------------------------------------------

describe('honest states', () => {
  it('a failed list fetch sets the error and leaves loaded FALSE', async () => {
    axios.get.mockRejectedValueOnce(new Error('boom'))
    await store.loadAgentReports('alpha')

    expect(store.reportsError).toBeTruthy()
    // The empty copy gates on `reportsLoaded`, so this is what stops "this
    // agent hasn't published any reports" from rendering for a network fault.
    expect(store.reportsLoaded).toBe(false)
    expect(store.reports).toEqual([])
  })

  it('a successful retry clears the error', async () => {
    axios.get.mockRejectedValueOnce(new Error('boom'))
    await store.loadAgentReports('alpha')

    axios.get.mockResolvedValueOnce({ data: { reports: [{ id: 'r1' }] } })
    await store.loadAgentReports('alpha')

    expect(store.reportsError).toBeNull()
    expect(store.reportsLoaded).toBe(true)
  })

  it('a failed payload fetch records an error and NO payload', async () => {
    axios.get.mockRejectedValueOnce(new Error('boom'))
    await store.loadAgentReport('alpha', 'r1')

    expect(store.reportErrors.r1).toBeTruthy()
    // Never in reportPayloads: an {error: …} object there is handed to the
    // renderer and presented AS a report — worse than the bug being fixed.
    expect(store.reportPayloads.r1).toBeUndefined()
  })

  it('a retry after a failed payload fetch clears the error and renders', async () => {
    axios.get.mockRejectedValueOnce(new Error('boom'))
    await store.loadAgentReport('alpha', 'r1')

    axios.get.mockResolvedValueOnce({ data: { payload: { markdown: 'ok' } } })
    await store.loadAgentReport('alpha', 'r1')

    expect(store.reportErrors.r1).toBeUndefined()
    expect(store.reportPayloads.r1).toEqual({ markdown: 'ok' })
  })

  it('clearReportError dismisses without retrying', async () => {
    axios.get.mockRejectedValueOnce(new Error('boom'))
    await store.loadAgentReport('alpha', 'r1')

    axios.get.mockClear()
    store.clearReportError('r1')

    expect(store.reportErrors.r1).toBeUndefined()
    expect(axios.get).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// 5. Duplicate work
// ---------------------------------------------------------------------------

describe('in-flight and cache guards', () => {
  it('a rapid double expand issues ONE request', async () => {
    const inflight = deferred()
    axios.get.mockReturnValueOnce(inflight.promise)

    const first = store.loadAgentReport('alpha', 'r1')
    const second = store.loadAgentReport('alpha', 'r1')

    inflight.resolve({ data: { payload: { markdown: 'hi' } } })
    await Promise.all([first, second])

    // Each request re-reads the whole blob server-side, so a duplicate is not
    // merely wasteful here.
    expect(axios.get).toHaveBeenCalledTimes(1)
  })

  it('re-expanding an already-loaded report issues no request', async () => {
    axios.get.mockResolvedValueOnce({ data: { payload: { markdown: 'hi' } } })
    await store.loadAgentReport('alpha', 'r1')

    axios.get.mockClear()
    await store.loadAgentReport('alpha', 'r1')

    expect(axios.get).not.toHaveBeenCalled()
  })

  it('resetAgentReports drops every per-report cache, not only the list', async () => {
    axios.get.mockResolvedValueOnce({ data: tabular(500) })
    await store.loadAgentReport('alpha', 'r1')

    store.resetAgentReports('beta')

    expect(store.reportPayloads).toEqual({})
    expect(store.reportRowMeta).toEqual({})
    expect(store.reportErrors).toEqual({})
  })
})
