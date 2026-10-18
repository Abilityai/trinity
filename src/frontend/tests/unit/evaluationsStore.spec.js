/**
 * trinity-enterprise#938 (C1) — stores/evaluations.js, the Evaluations tab's
 * one domain store. Pins the period → ent#937 query mapping (A10), that every
 * request the store issues is one of the ent#937 URLs (the "no new route, no
 * direct table reads" contract — the same set `test_938_tab_guard.py` drives
 * under four principals), and the honest-state transitions: a failed first
 * load is `failed`, a failed refresh (422 `invalid_period` included) keeps the
 * data and carries the API's own message.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const http = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../src/api', () => ({ default: { get: (...a) => http.get(...a) } }))

import {
  useEvaluationsStore, periodQuery, EVALUATION_URLS, AGGREGATE_URL,
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
    expect(http.get).toHaveBeenCalledTimes(1)
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
    expect(urls.length).toBe(4)
    for (const u of urls) expect(EVALUATION_URLS).toContain(u)
    const last = http.get.mock.calls.at(-1)[1].params
    expect(last).toMatchObject({ by: 'agent', period: 'custom', agent: 'fin',
      since: '2026-10-01T00:00:00Z', until: '2026-10-04T00:00:00Z' })
    expect(http.get.mock.calls[1][1].params.agent).toBeUndefined()   // no agent → no key
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
