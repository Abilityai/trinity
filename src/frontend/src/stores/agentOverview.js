/**
 * Agent Detail Overview — the reads behind the tab (trinity-enterprise#1317).
 *
 * One store, one agent at a time. Every read is a SOURCE with its own
 * `{ data, hasLoaded, error }`, so a section can tell "no data yet" from "the
 * read succeeded and there is none" from "the read failed" (design principle
 * 15) — the three things the old panel's bare refs could not tell apart.
 *
 * Two rules the panel depends on:
 *
 * 1. **Switching agents blanks every source first.** The panel is reused across
 *    agents; a read that fails for the new agent must leave that section
 *    failed, never showing the previous agent's figure (#3815).
 * 2. **A response is written only if its agent is still the one on screen.**
 *    Each write re-checks `agent` after its await, so a slow read for the
 *    agent you just left cannot land on the one you opened.
 *
 * 3. **Only the newest read of a source may write.** Each read takes a
 *    sequence number; a response that is no longer the newest is dropped, so
 *    picking 30d then 7d cannot end with 30d's numbers under a 7d label, and a
 *    late failure cannot mark fresh data stale.
 *
 * A refresh keeps `data` in place while it runs and keeps it if it fails
 * (`error` set, `hasLoaded` still true → the section stays up, marked stale).
 * The two windowed sources record the window their data is FOR (`window` on the
 * source), and a window switch whose read fails puts the control back, so a
 * label never names a window the numbers under it are not from.
 */
import { defineStore } from 'pinia'
import { reactive, ref } from 'vue'
import api from '@/api'
import { pendingCountOf, readPendingQueue } from '@/utils/operatorQueueRead'
import { useAgentsStore } from './agents'

export const OVERVIEW_SOURCES = [
  'analytics', 'summary', 'live', 'recent', 'schedules',
  'notifications', 'asks', 'sync', 'health', 'history', 'metrics',
]

// What changes minute to minute. Everything else is read on open and on a
// window switch only.
const LIVE_SOURCES = ['live', 'recent', 'notifications', 'asks']

const blank = () => ({ data: null, hasLoaded: false, error: null, window: null })
const WINDOWED = new Set(['analytics', 'summary'])

export const useAgentOverviewStore = defineStore('agentOverview', () => {
  const agent = ref(null)
  const window = ref('7d')
  const sources = reactive(Object.fromEntries(OVERVIEW_SOURCES.map((k) => [k, blank()])))

  function reset(name) {
    agent.value = name
    for (const key of OVERVIEW_SOURCES) sources[key] = blank()
  }

  const path = (name, rest) => `/api/agents/${encodeURIComponent(name)}${rest}`

  const readers = {
    analytics: (name) => api.get(path(name, '/analytics'), { params: { window: window.value } }).then((r) => r.data),
    summary: (name) => api.get(path(name, '/schedules/analytics-summary'), { params: { window: window.value } }).then((r) => r.data),
    live: (name) => api.get('/api/executions/stats', { params: { agent: name } }).then((r) => r.data),
    recent: (name) => api.get('/api/executions', { params: { agent: name, limit: 5 } }).then((r) => r.data || []),
    schedules: (name) => api.get(path(name, '/schedules')).then((r) => r.data || []),
    notifications: (name) => api.get(path(name, '/notifications/count')).then((r) => r.data?.pending_count || 0),
    sync: (name) => api.get(path(name, '/git/sync-state')).then((r) => r.data || null),
    health: (name) => api.get(`/api/monitoring/agents/${encodeURIComponent(name)}`).then((r) => r.data),
    history: (name) => api
      .get(`/api/monitoring/agents/${encodeURIComponent(name)}/history`, { params: { check_type: 'network', hours: 168, limit: 1000 } })
      .then((r) => r.data?.checks || []),
    metrics: (name) => useAgentsStore().getAgentMetrics(name).then((d) => d?.metrics || []),
  }

  // The newest read per source. A response writes only while it is still that.
  const newest = {}
  const begin = (key) => { newest[key] = (newest[key] || 0) + 1; return newest[key] }
  const current = (name, key, seq) => agent.value === name && newest[key] === seq

  function commit(name, key, seq, data, win = null) {
    if (!current(name, key, seq)) return
    sources[key] = { data, hasLoaded: true, error: null, window: win }
  }

  function fail(name, key, seq, error) {
    if (!current(name, key, seq)) return
    sources[key] = { ...sources[key], error }
  }

  // #3302: the agent's pending count is the list route's `total`. An unverified
  // total (null) means the route withheld a row that was not this viewer's to
  // see: the page's own rows are a floor until the pending asks are read once.
  async function readAsks(name) {
    const get = (params) => api.get('/api/operator-queue', { params })
    const seq = begin('asks')
    try {
      const body = (await get({ agent_name: name, status: 'pending', limit: 1 })).data
      const counted = pendingCountOf(body)
      commit(name, 'asks', seq, counted ?? (Number(body?.count) || 0))
      if (counted == null && body?.has_more) {
        try {
          const read = await readPendingQueue(get, { params: { agent_name: name } })
          commit(name, 'asks', seq, read.total ?? read.items.length)
        } catch {
          // the fallback failed: the floor stays
        }
      }
    } catch (error) {
      fail(name, 'asks', seq, error)
    }
  }

  async function read(name, key) {
    if (key === 'asks') return readAsks(name)
    const seq = begin(key)
    const win = WINDOWED.has(key) ? window.value : null
    try {
      commit(name, key, seq, await readers[key](name), win)
    } catch (error) {
      fail(name, key, seq, error)
    }
  }

  /** Open the Overview for `name`: blank on a new agent, then read everything. */
  async function load(name, { window: win } = {}) {
    if (!name) return
    if (agent.value !== name) reset(name)
    if (win) window.value = win
    await Promise.all(OVERVIEW_SOURCES.map((key) => read(name, key)))
  }

  /** Switch the window. The current charts stay up until the new ones land. */
  async function setWindow(win) {
    if (win === window.value) return
    window.value = win
    const name = agent.value
    if (!name) return
    await Promise.all([read(name, 'analytics'), read(name, 'summary')])
    // The switch did not land (and nothing newer was asked for): the charts on
    // screen are still the previous window's, so the control goes back to it.
    const shown = sources.analytics.window
    if (agent.value === name && window.value === win && shown && shown !== win) {
      window.value = shown
      // The schedules rollup may have switched on its own; bring it back in step.
      if (sources.summary.window !== shown) await read(name, 'summary')
    }
  }

  /** The cheap, frequent refresh while the tab is on screen. */
  async function refreshLive() {
    const name = agent.value
    if (!name) return
    await Promise.all(LIVE_SOURCES.map((key) => read(name, key)))
  }

  /** Retry one failed section. */
  function retry(key) {
    return agent.value ? read(agent.value, key) : undefined
  }

  return { agent, window, sources, load, setWindow, refreshLive, retry, reset }
})
