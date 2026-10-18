/**
 * Evaluations — the Operations › Evaluations tab's one domain store
 * (trinity-enterprise#938; Invariant #6/#7: store = domain, single API client).
 *
 * Reads ONLY the ent#937 routes — the tab adds no aggregate route and never
 * reads a table directly. The request set is pinned twice: the store spec
 * asserts every URL this file issues is in `EVALUATION_URLS`, and
 * `tests/unit/test_938_tab_guard.py` drives that same set through the real
 * router under four principals, so "Admin / operator only" is a property of
 * the API, never of UI hiding (ruling T1).
 *
 * Period → query (A10): the API's named windows are `day` and `week`, so 7 days
 * is `period=week` and anything else is `custom` with ISO-Z bounds the API
 * validates itself (≤ 92 d, 422 `invalid_period` — rendered as the API's
 * message, the previous data kept).
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'
import { apiErrorMessage } from '../utils/apiError'

export const AGGREGATE_URL = '/api/evaluations/aggregate'
export const FAILED_URL = '/api/evaluations/failed'
export const TREND_URL = '/api/evaluations/trend'
/** Every URL this store may issue — the "ent#937 only" contract, as data. */
export const EVALUATION_URLS = [AGGREGATE_URL, FAILED_URL, TREND_URL]
/** The one series the trend block plots (a count-backed rate, never a judge score). */
export const TREND_METRIC = 'eval.checks_passed_rate'
/** The trend route's own `days` ceiling. */
export const MAX_TREND_DAYS = 92
/** The failed route's page size (its own default; it caps at 200). */
export const FAILED_PAGE = 50

export const PERIODS = [
  { id: '7d', label: 'Last 7 days' },
  { id: '30d', label: 'Last 30 days' },
  { id: 'custom', label: 'Custom range' },
]

const DAY_MS = 86_400_000
const isoZ = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
const dayStart = (ymd) => `${ymd}T00:00:00Z`
const utcDay = (ms) => new Date(ms).toISOString().slice(0, 10)

/**
 * The ent#937 query for a picker state. `null` while a custom range is still
 * half-typed — nothing to ask yet, not an error.
 * @param {{period: string, since?: string, until?: string}} picker  since/until are YYYY-MM-DD
 * @param {Date} [now]  injectable for tests
 */
export function periodQuery({ period, since = '', until = '' }, now = new Date()) {
  if (period === '7d') return { period: 'week' }
  if (period === '30d') {
    return { period: 'custom', since: isoZ(now.getTime() - 30 * DAY_MS), until: isoZ(now.getTime()) }
  }
  if (!since || !until) return null
  // Inclusive calendar days: `until` is the start of the day after the one picked.
  return { period: 'custom', since: dayStart(since), until: isoZ(Date.parse(dayStart(until)) + DAY_MS) }
}

/**
 * The window just before `{since, until}` — same length, ending at `since` —
 * as a custom query, or `null` when the bounds the API answered with cannot
 * be read. The KPI and per-agent deltas compare against it.
 */
export function previousWindow(since, until) {
  const lo = Date.parse(since)
  const hi = Date.parse(until)
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return null
  return { period: 'custom', since: isoZ(lo - (hi - lo)), until: isoZ(lo) }
}

/** A rate's change in percentage points, `null` when either side has none. */
export function rateDelta(now, before) {
  if (typeof now !== 'number' || typeof before !== 'number') return null
  return Math.round((now - before) * 1000) / 10
}

/** The most frequent failed check id (ties → alphabetical), `null` for none. */
export function topFailedCheck(perCheckFail) {
  const entries = Object.entries(perCheckFail || {}).filter(([, n]) => n > 0)
  if (!entries.length) return null
  entries.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  return { id: entries[0][0], count: entries[0][1] }
}

/**
 * The UTC days `[since, until)` spans, oldest first — the trend's x-axis, so
 * a day with no stored point is a gap, never a dropped day or a false zero.
 * `[]` when the bounds cannot be read.
 */
export function windowDays(since, until) {
  const lo = Date.parse(since)
  const hi = Date.parse(until)
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return []
  const days = []
  for (let t = Date.parse(dayStart(utcDay(lo))); t < hi && days.length < MAX_TREND_DAYS + 1; t += DAY_MS) {
    days.push(utcDay(t))
  }
  return days
}

/**
 * The trend route's `days` for a window: it reads `now - days .. now`, so the
 * count runs back from NOW to the window's first day — a custom window that
 * ended in the past plots its own days (review I1). `clipped` when the window
 * opens before the route's ceiling reaches.
 * @param {string} since  ISO-Z
 * @param {number} [nowMs]  injectable for tests
 */
export function trendDays(since, nowMs = Date.now()) {
  const lo = Date.parse(since)
  if (!Number.isFinite(lo)) return { days: 0, clipped: false }
  const need = Math.max(1, Math.ceil((nowMs - lo) / DAY_MS) + 1)
  return { days: Math.min(need, MAX_TREND_DAYS), clipped: need > MAX_TREND_DAYS }
}

export const useEvaluationsStore = defineStore('evaluations', () => {
  // Latest request wins (review I5): each loader stamps its request and drops
  // an answer that a later request has overtaken, so two quick picker changes
  // never leave the earlier window's numbers under the later picker state.
  const seq = { load: 0, prev: 0, failed: 0, pairs: 0, trend: 0 }
  // --- picker state ---
  const period = ref('7d')
  const since = ref('')
  const until = ref('')
  const agent = ref('')

  // --- the aggregate (by=agent) ---
  const aggregate = ref(null)
  const loading = ref(false)
  const hasLoaded = ref(false)
  const error = ref(null)
  const errorCode = ref(null)
  const lastLoadedAt = ref(null)
  // The previous window's aggregate, for deltas only. `null` when it has not
  // loaded or its read failed — the deltas then read "—", the current
  // numbers stand on their own.
  const prevAggregate = ref(null)

  // --- the failed list (C3) — its own block state, so a failed page never
  // blanks the KPIs. `pair` narrows it to one caller → callee edge.
  const failed = ref({ items: [], total: 0, truncated: false })
  const failedLoading = ref(false)
  const failedLoaded = ref(false)
  const failedError = ref(null)
  const pair = ref(null)

  // --- the pairs stub (C3): aggregate by=pair over the same window.
  const pairsAggregate = ref(null)
  const pairsError = ref(null)

  // --- the trend (C6): the stored daily series for the selected agent; with
  // no agent, the fleet series (C7). The API hands the fleet series only to a
  // reader who already sees every agent — anyone else gets no points, and the
  // block then says "pick an agent".
  const trend = ref(null)
  const trendLoading = ref(false)
  const trendLoaded = ref(false)
  const trendError = ref(null)

  const evaluatedCount = computed(() => aggregate.value?.total?.evaluated ?? 0)
  const invalidPeriod = computed(() => errorCode.value === 'invalid_period')

  // KPI row (C2). No judge-quality or agreement tile: those wait on the
  // calibration placement decision (ent#938 ruling addendum).
  const kpis = computed(() => {
    const t = aggregate.value?.total
    if (!t) return []
    const p = prevAggregate.value?.total
    return [
      { id: 'evaluated', label: 'Runs evaluated', value: t.evaluated, rate: false,
        delta: p ? t.evaluated - p.evaluated : null },
      { id: 'completion_rate', label: 'Completed', value: t.completion_rate ?? null, rate: true,
        delta: p ? rateDelta(t.completion_rate, p.completion_rate) : null },
      { id: 'checks_passed_rate', label: 'Checks passed', value: t.checks_passed_rate ?? null, rate: true,
        delta: p ? rateDelta(t.checks_passed_rate, p.checks_passed_rate) : null },
    ]
  })

  // Failed-check counts across the window, most frequent first.
  const perCheck = computed(() => Object.entries(aggregate.value?.total?.per_check_fail || {})
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([id, count]) => ({ id, count })))

  // Delegated pairs; with an agent selected, only the pairs it is a side of.
  const pairRows = computed(() => (pairsAggregate.value?.groups || [])
    .filter((g) => g.key?.caller && g.key?.callee)
    .filter((g) => !agent.value || g.key.caller === agent.value || g.key.callee === agent.value)
    .map((g) => ({ caller: g.key.caller, callee: g.key.callee, evaluated: g.evaluated,
      checksPassedRate: g.checks_passed_rate ?? null })))

  // The chart's axis and values: one slot per UTC day of the window the
  // aggregate answered with, the stored point's rate as a percentage, `null`
  // (a gap) where no point was written.
  const trendChart = computed(() => {
    const dates = windowDays(aggregate.value?.since, aggregate.value?.until)
    const byDay = new Map((trend.value?.points || []).map((p) => [String(p.ts).slice(0, 10), p.value]))
    const values = dates.map((d) => (typeof byDay.get(d) === 'number'
      ? Math.round(byDay.get(d) * 1000) / 10 : null))
    return {
      dates,
      values,
      points: trend.value?.points?.length ?? 0,
      // The window opens before the trend route's 92-day reach: its first
      // days are outside the series, not "no graded days".
      clipped: dates.length > 0 && trendDays(aggregate.value?.since).clipped,
      // The first day the series has a point, when that is after the window
      // opens — the fleet series is young, or the agent's first graded run is.
      startsOn: (() => {
        const first = dates.find((d, i) => values[i] !== null)
        return first && first !== dates[0] ? first : null
      })(),
    }
  })

  const failedHasMore = computed(() => failed.value.items.length < failed.value.total)

  // Per-agent rows, worst checks-passed rate first (ungraded last, then by
  // name) — the default order the table opens in.
  const agentRows = computed(() => {
    const prevBy = new Map((prevAggregate.value?.groups || []).map((g) => [g.key?.agent, g]))
    const rows = (aggregate.value?.groups || []).map((g) => {
      const prev = prevBy.get(g.key?.agent)
      return {
        agent: g.key?.agent ?? '',
        evaluated: g.evaluated,
        completionRate: g.completion_rate ?? null,
        checksPassedRate: g.checks_passed_rate ?? null,
        delta: prev ? rateDelta(g.checks_passed_rate, prev.checks_passed_rate) : null,
        topFailedCheck: topFailedCheck(g.per_check_fail),
      }
    })
    return rows.sort((a, b) => {
      const ra = a.checksPassedRate ?? Infinity
      const rb = b.checksPassedRate ?? Infinity
      return ra - rb || a.agent.localeCompare(b.agent)
    })
  })

  function params() {
    const q = periodQuery({ period: period.value, since: since.value, until: until.value })
    if (!q) return null
    return { by: 'agent', ...q, ...(agent.value ? { agent: agent.value } : {}) }
  }

  async function load() {
    const p = params()
    if (!p) return
    const mine = ++seq.load
    loading.value = true
    error.value = null
    errorCode.value = null
    try {
      const { data } = await api.get(AGGREGATE_URL, { params: p })
      if (mine !== seq.load) return
      aggregate.value = data
      hasLoaded.value = true
      lastLoadedAt.value = Date.now()
      await Promise.all([loadPrevious(p, data), loadFailed(), loadPairs(), loadTrend()])
    } catch (err) {
      if (mine !== seq.load) return
      // A failed refresh keeps `aggregate` on screen (viewState → stale);
      // never a synthetic empty payload in a catch.
      error.value = apiErrorMessage(err, "Couldn't load evaluations")
      errorCode.value = err?.response?.data?.detail?.code ?? null
    } finally {
      if (mine === seq.load) loading.value = false
    }
  }

  async function loadPrevious(p, data) {
    const prev = previousWindow(data?.since, data?.until)
    const mine = ++seq.prev
    if (!prev) { prevAggregate.value = null; return }
    try {
      const { data: before } = await api.get(AGGREGATE_URL, { params: { ...p, ...prev } })
      if (mine === seq.prev) prevAggregate.value = before
    } catch {
      if (mine !== seq.prev) return
      // Deltas are context, not the reading: a failed previous-window read
      // drops them to "—" and leaves the current numbers (and no banner).
      prevAggregate.value = null
    }
  }

  function failedParams(offset) {
    const q = periodQuery({ period: period.value, since: since.value, until: until.value })
    if (!q) return null
    const filter = pair.value
      ? { by: 'pair', caller: pair.value.caller, callee: pair.value.callee }
      : { by: 'agent', ...(agent.value ? { agent: agent.value } : {}) }
    return { ...filter, ...q, limit: FAILED_PAGE, offset }
  }

  async function loadFailed({ more = false } = {}) {
    const p = failedParams(more ? failed.value.items.length : 0)
    if (!p) return
    const mine = ++seq.failed
    failedLoading.value = true
    failedError.value = null
    try {
      const { data } = await api.get(FAILED_URL, { params: p })
      if (mine !== seq.failed) return
      failed.value = {
        items: more ? [...failed.value.items, ...data.items] : data.items,
        total: data.total,
        truncated: data.truncated,
      }
      failedLoaded.value = true
    } catch (err) {
      if (mine !== seq.failed) return
      // The rows already on screen stay; the block says the read failed.
      failedError.value = apiErrorMessage(err, "Couldn't load failed runs")
    } finally {
      if (mine === seq.failed) failedLoading.value = false
    }
  }

  function loadMoreFailed() {
    return loadFailed({ more: true })
  }

  async function loadPairs() {
    const q = periodQuery({ period: period.value, since: since.value, until: until.value })
    if (!q) return
    const mine = ++seq.pairs
    pairsError.value = null
    try {
      const { data } = await api.get(AGGREGATE_URL, { params: { by: 'pair', ...q } })
      if (mine === seq.pairs) pairsAggregate.value = data
    } catch (err) {
      if (mine === seq.pairs) pairsError.value = apiErrorMessage(err, "Couldn't load delegated pairs")
    }
  }

  async function loadTrend() {
    if (!windowDays(aggregate.value?.since, aggregate.value?.until).length) return
    const { days } = trendDays(aggregate.value?.since)
    const mine = ++seq.trend
    // Another agent's line is not a stale view of this one: drop it.
    if ((trend.value?.dims?.agent ?? '') !== agent.value) {
      trend.value = null
      trendLoaded.value = false
    }
    trendLoading.value = true
    trendError.value = null
    try {
      const { data } = await api.get(TREND_URL, {
        params: {
          metric: TREND_METRIC,
          ...(agent.value ? { agent: agent.value } : {}),   // none → the fleet series
          days,
        },
      })
      if (mine !== seq.trend) return
      trend.value = data
      trendLoaded.value = true
    } catch (err) {
      if (mine === seq.trend) trendError.value = apiErrorMessage(err, "Couldn't load the trend")
    } finally {
      if (mine === seq.trend) trendLoading.value = false
    }
  }

  /** Narrow the failed list to one caller → callee edge (`null` clears). */
  function setPair(next) {
    pair.value = next ? { caller: next.caller, callee: next.callee } : null
    return loadFailed()
  }

  function setPeriod(next) {
    period.value = next
    return load()
  }

  function setCustomRange(from, to) {
    since.value = from
    until.value = to
    return load()
  }

  function setAgent(name) {
    agent.value = name || ''
    pair.value = null
    return load()
  }

  function clearError() {
    error.value = null
    errorCode.value = null
  }

  return {
    period, since, until, agent,
    aggregate, prevAggregate, loading, hasLoaded, error, errorCode, lastLoadedAt,
    failed, failedLoading, failedLoaded, failedError, failedHasMore, pair,
    pairsAggregate, pairsError, pairRows, perCheck,
    trend, trendLoading, trendLoaded, trendError, trendChart,
    evaluatedCount, invalidPeriod, kpis, agentRows,
    load, loadFailed, loadMoreFailed, loadPairs, loadTrend, setPair,
    setPeriod, setCustomRange, setAgent, clearError,
  }
})
