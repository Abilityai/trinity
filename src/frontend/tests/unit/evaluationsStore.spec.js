/**
 * trinity-enterprise#938 (C1) — stores/evaluations.js, the Evaluations tab's
 * one domain store. Pins the period → ent#937 query mapping (A10), that every
 * request the store issues is one of the ent#937 URLs (the "no new route, no
 * direct table reads" contract — the same set `test_938_tab_guard.py` drives
 * under four principals), and the honest-state transitions: a failed first
 * load is `failed`, a failed refresh (422 `invalid_period` included) keeps the
 * data and carries the API's own message.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const http = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../src/api', () => ({ default: { get: (...a) => http.get(...a) } }))

import {
  useEvaluationsStore, periodQuery, EVALUATION_URLS, AGGREGATE_URL, FAILED_URL, TREND_URL,
} from '../../src/stores/evaluations'

const NOW = new Date('2026-10-18T12:00:00Z')
const body = (evaluated, extra = {}) => ({
  by: 'agent', since: 's', until: 'u', truncated: false,
  total: { evaluated, completed: evaluated, graded: evaluated, passed: evaluated, per_check_fail: {}, judged: 0 },
  groups: [], ...extra,
})
const fail = (status, detail) => {
  const e = new Error(`Request failed with status code ${status}`)
  e.response = { status, data: { detail } }
  return e
}

describe('periodQuery — picker state → ent#937 query (A10)', () => {
  it('7 days is the API\'s own week window', () => {
    expect(periodQuery({ period: '7d' }, NOW)).toEqual({ period: 'week' })
  })
  it('30 days is custom with ISO-Z bounds exactly 30 days apart, ending now', () => {
    const q = periodQuery({ period: '30d' }, NOW)
    expect(q.period).toBe('custom')
    expect(q.until).toBe('2026-10-18T12:00:00Z')
    expect(q.since).toBe('2026-09-18T12:00:00Z')
  })
  it('a half-typed custom range asks nothing', () => {
    expect(periodQuery({ period: 'custom', since: '2026-10-01' }, NOW)).toBeNull()
    expect(periodQuery({ period: 'custom', until: '2026-10-01' }, NOW)).toBeNull()
  })
  it('a full custom range is inclusive of both picked days', () => {
    expect(periodQuery({ period: 'custom', since: '2026-10-01', until: '2026-10-03' }, NOW))
      .toEqual({ period: 'custom', since: '2026-10-01T00:00:00Z', until: '2026-10-04T00:00:00Z' })
  })
})

describe('useEvaluationsStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    http.get.mockReset()
  })

  it('loads the by=agent aggregate for the default 7-day window', async () => {
    http.get.mockResolvedValue({ data: body(3) })
    const store = useEvaluationsStore()
    expect(store.period).toBe('7d')
    await store.load()
    // C3: the aggregate first, then the failed list and the pairs stub for
    // the same window (no previous-window read: these bounds are not ISO).
    expect(http.get.mock.calls.map(([u, c]) => [u, c.params.by])).toEqual([
      [AGGREGATE_URL, 'agent'], [FAILED_URL, 'agent'], [AGGREGATE_URL, 'pair'],
    ])
    const [url, { params }] = http.get.mock.calls[0]
    expect(url).toBe(AGGREGATE_URL)
    expect(params).toEqual({ by: 'agent', period: 'week' })
    expect(store.hasLoaded).toBe(true)
    expect(store.evaluatedCount).toBe(3)
    expect(store.error).toBeNull()
  })

  it('issues only ent#937 URLs — across every period and the agent filter', async () => {
    http.get.mockResolvedValue({ data: body(1) })
    const store = useEvaluationsStore()
    await store.load()
    await store.setPeriod('30d')
    await store.setAgent('fin')
    await store.setPeriod('custom')                       // half-typed: no request
    await store.setCustomRange('2026-10-01', '2026-10-03')
    const urls = http.get.mock.calls.map(([u]) => u)
    expect(urls.length).toBe(12)                          // 4 loads × (aggregate, failed, pairs)
    for (const u of urls) expect(EVALUATION_URLS).toContain(u)
    const agg = http.get.mock.calls.filter(([u, c]) => u === AGGREGATE_URL && c.params.by === 'agent')
    expect(agg.length).toBe(4)
    const last = agg.at(-1)[1].params
    expect(last).toMatchObject({ by: 'agent', period: 'custom', agent: 'fin',
      since: '2026-10-01T00:00:00Z', until: '2026-10-04T00:00:00Z' })
    expect(agg[1][1].params.agent).toBeUndefined()        // no agent → no key
  })

  it('a failed first load is failed, not empty: nothing loaded, the error named', async () => {
    http.get.mockRejectedValue(fail(500, 'boom'))
    const store = useEvaluationsStore()
    await store.load()
    expect(store.hasLoaded).toBe(false)
    expect(store.aggregate).toBeNull()
    expect(store.error).toBe('boom')
    expect(store.invalidPeriod).toBe(false)
  })

  it('a bad custom window keeps the previous data and carries the API\'s message', async () => {
    http.get.mockResolvedValueOnce({ data: body(5) })
    const store = useEvaluationsStore()
    await store.load()
    http.get.mockRejectedValueOnce(fail(422, { code: 'invalid_period', message: 'the window is at most 92 days' }))
    store.period = 'custom'
    await store.setCustomRange('2026-01-01', '2026-10-01')
    expect(store.evaluatedCount).toBe(5)                   // data kept — never a synthetic empty
    expect(store.hasLoaded).toBe(true)
    expect(store.error).toBe('the window is at most 92 days')
    expect(store.invalidPeriod).toBe(true)
    store.clearError()
    expect(store.error).toBeNull()
    expect(store.invalidPeriod).toBe(false)
  })
})

describe('trend day range — derived from the selected window (review I1)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    http.get.mockReset()
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
  })
  afterEach(() => vi.useRealTimers())

  const windowed = (since, until) => async (url) => (url === TREND_URL
    ? { data: { metric: 'eval.checks_passed_rate', dims: { scope: 'fleet' }, points: [] } }
    : { data: body(1, { since, until }) })

  it('a custom window that ended in the past reaches back to its own first day', async () => {
    http.get.mockImplementation(windowed('2026-08-01T00:00:00Z', '2026-09-01T00:00:00Z'))
    const store = useEvaluationsStore()
    store.period = 'custom'
    await store.setCustomRange('2026-08-01', '2026-08-31')
    const [, { params }] = http.get.mock.calls.find(([u]) => u === TREND_URL)
    // 78.5 days from Aug 1 00:00 to now, rounded up, plus the day itself: the
    // route's `now - days` then opens before the window does.
    expect(params.days).toBe(80)
    expect(store.trendChart.dates[0]).toBe('2026-08-01')
    expect(store.trendChart.clipped).toBe(false)
  })

  it('a window older than the trend route keeps says so instead of reading as empty', async () => {
    http.get.mockImplementation(windowed('2026-06-01T00:00:00Z', '2026-06-30T00:00:00Z'))
    const store = useEvaluationsStore()
    store.period = 'custom'
    await store.setCustomRange('2026-06-01', '2026-06-29')
    const [, { params }] = http.get.mock.calls.find(([u]) => u === TREND_URL)
    expect(params.days).toBe(92)
    expect(store.trendChart.clipped).toBe(true)
  })
})

describe('overlapping loads — the latest request wins (review I5)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    http.get.mockReset()
  })

  it('an earlier window resolving last never replaces the later one on screen', async () => {
    let releaseFirst
    const first = new Promise((r) => { releaseFirst = r })
    http.get.mockImplementation(async (url, { params }) => {
      if (url === AGGREGATE_URL && params.by === 'agent' && params.period === 'week') {
        await first
        return { data: body(7) }
      }
      if (url === FAILED_URL) {
        if (params.period === 'week') await first
        return { data: { items: [{ execution_id: params.period }], total: 1, truncated: false } }
      }
      return { data: body(30) }
    })
    const store = useEvaluationsStore()
    const a = store.load()                                  // 7 d, slow
    const b = store.setPeriod('30d')                        // 30 d, fast
    await b
    expect(store.evaluatedCount).toBe(30)
    releaseFirst()
    await a
    expect(store.evaluatedCount).toBe(30)                   // the stale week answer was dropped
    expect(store.loading).toBe(false)
    expect(store.failed.items.map((i) => i.execution_id)).toEqual(['custom'])
  })
})
