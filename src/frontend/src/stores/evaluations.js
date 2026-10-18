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
/** Every URL this store may issue — the "ent#937 only" contract, as data. */
export const EVALUATION_URLS = [AGGREGATE_URL, FAILED_URL]
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

export const useEvaluationsStore = defineStore('evaluations', () => {
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
    loading.value = true
    error.value = null
    errorCode.value = null
    try {
      const { data } = await api.get(AGGREGATE_URL, { params: p })
      aggregate.value = data
      hasLoaded.value = true
      lastLoadedAt.value = Date.now()
      await Promise.all([loadPrevious(p, data), loadFailed(), loadPairs()])
    } catch (err) {
      // A failed refresh keeps `aggregate` on screen (viewState → stale);
      // never a synthetic empty payload in a catch.
      error.value = apiErrorMessage(err, "Couldn't load evaluations")
      errorCode.value = err?.response?.data?.detail?.code ?? null
    } finally {
      loading.value = false
    }
  }

  async function loadPrevious(p, data) {
    const prev = previousWindow(data?.since, data?.until)
    if (!prev) { prevAggregate.value = null; return }
    try {
      const { data: before } = await api.get(AGGREGATE_URL, { params: { ...p, ...prev } })
      prevAggregate.value = before
    } catch {
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
    failedLoading.value = true
    failedError.value = null
    try {
      const { data } = await api.get(FAILED_URL, { params: p })
      failed.value = {
        items: more ? [...failed.value.items, ...data.items] : data.items,
        total: data.total,
        truncated: data.truncated,
      }
      failedLoaded.value = true
    } catch (err) {
      // The rows already on screen stay; the block says the read failed.
      failedError.value = apiErrorMessage(err, "Couldn't load failed runs")
    } finally {
      failedLoading.value = false
    }
  }

  function loadMoreFailed() {
    return loadFailed({ more: true })
  }

  async function loadPairs() {
    const q = periodQuery({ period: period.value, since: since.value, until: until.value })
    if (!q) return
    pairsError.value = null
    try {
      const { data } = await api.get(AGGREGATE_URL, { params: { by: 'pair', ...q } })
      pairsAggregate.value = data
    } catch (err) {
      pairsError.value = apiErrorMessage(err, "Couldn't load delegated pairs")
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
    evaluatedCount, invalidPeriod, kpis, agentRows,
    load, loadFailed, loadMoreFailed, loadPairs, setPair,
    setPeriod, setCustomRange, setAgent, clearError,
  }
})
