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
/** Every URL this store may issue — the "ent#937 only" contract, as data. */
export const EVALUATION_URLS = [AGGREGATE_URL]

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

  const evaluatedCount = computed(() => aggregate.value?.total?.evaluated ?? 0)
  const invalidPeriod = computed(() => errorCode.value === 'invalid_period')

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
    } catch (err) {
      // A failed refresh keeps `aggregate` on screen (viewState → stale);
      // never a synthetic empty payload in a catch.
      error.value = apiErrorMessage(err, "Couldn't load evaluations")
      errorCode.value = err?.response?.data?.detail?.code ?? null
    } finally {
      loading.value = false
    }
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
    return load()
  }

  function clearError() {
    error.value = null
    errorCode.value = null
  }

  return {
    period, since, until, agent,
    aggregate, loading, hasLoaded, error, errorCode, lastLoadedAt,
    evaluatedCount, invalidPeriod,
    load, setPeriod, setCustomRange, setAgent, clearError,
  }
})
