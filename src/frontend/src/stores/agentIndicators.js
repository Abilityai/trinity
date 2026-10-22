/**
 * Agent behavioral indicators — the agent Overview's Behavior section
 * (trinity-enterprise#939; Invariant #6/#7: store = domain, single API client).
 *
 * Read-only over the ent#937 evaluations: two GETs, no writes. One agent at a
 * time — switching agents resets the state so one agent's indicators never
 * flash on another's page. The guard is the API (`require_person` + the agent
 * access gate); an agent key is refused there and reads its failed checks
 * through the evaluation summary instead.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { apiErrorMessage } from '../utils/apiError'

export const WINDOW_DAYS = [7, 14, 30]
export const DEFAULT_DAYS = 14
export const indicatorsUrl = (name) => `/api/agents/${encodeURIComponent(name)}/evaluations/indicators`
export const runsUrl = (name, indicator) => `${indicatorsUrl(name)}/${encodeURIComponent(indicator)}/runs`

export const useAgentIndicatorsStore = defineStore('agentIndicators', () => {
  const agent = ref(null)
  const days = ref(DEFAULT_DAYS)
  const data = ref(null)
  const loading = ref(false)
  const hasLoaded = ref(false)
  const error = ref(null)
  const lastLoadedAt = ref(null)

  // The drill-down: the runs behind one indicator (optionally one check).
  const drill = ref(null)          // { indicator, check } | null
  const drillData = ref(null)
  const drillLoading = ref(false)
  const drillLoaded = ref(false)
  const drillError = ref(null)
  let gen = 0

  function reset(name) {
    gen += 1
    agent.value = name
    data.value = null
    hasLoaded.value = false
    error.value = null
    lastLoadedAt.value = null
    closeDrill()
  }

  async function load(name, opts = {}) {
    if (agent.value !== name) reset(name)
    if (opts.days) days.value = opts.days
    const mine = gen
    loading.value = true
    try {
      const r = await api.get(indicatorsUrl(name), { params: { days: days.value } })
      if (mine !== gen) return
      data.value = r.data
      hasLoaded.value = true
      lastLoadedAt.value = new Date()
      error.value = null
    } catch (e) {
      if (mine !== gen) return
      error.value = apiErrorMessage(e, "Couldn't load this agent's behavior")
    } finally {
      if (mine === gen) loading.value = false
    }
  }

  async function setDays(n) {
    if (!WINDOW_DAYS.includes(n) || !agent.value) return
    days.value = n
    // A new window is new data: the previous window's numbers must not stand
    // in for it while it loads.
    data.value = null
    hasLoaded.value = false
    gen += 1
    await load(agent.value)
    if (drill.value) await openDrill(drill.value.indicator, drill.value.check)
  }

  async function openDrill(indicator, check = null) {
    if (!agent.value) return
    const same = drill.value && drill.value.indicator === indicator && drill.value.check === check
    drill.value = { indicator, check }
    if (!same) {
      drillData.value = null
      drillLoaded.value = false
    }
    drillError.value = null
    const mine = gen
    drillLoading.value = true
    try {
      const params = { days: days.value }
      if (check) params.check = check
      const r = await api.get(runsUrl(agent.value, indicator), { params })
      if (mine !== gen || drill.value?.indicator !== indicator || drill.value?.check !== check) return
      drillData.value = r.data
      drillLoaded.value = true
    } catch (e) {
      if (mine !== gen) return
      drillError.value = apiErrorMessage(e, "Couldn't load the runs behind this indicator")
    } finally {
      if (mine === gen) drillLoading.value = false
    }
  }

  function closeDrill() {
    drill.value = null
    drillData.value = null
    drillLoaded.value = false
    drillError.value = null
    drillLoading.value = false
  }

  return {
    agent, days, data, loading, hasLoaded, error, lastLoadedAt,
    drill, drillData, drillLoading, drillLoaded, drillError,
    reset, load, setDays, openDrill, closeDrill,
  }
})
