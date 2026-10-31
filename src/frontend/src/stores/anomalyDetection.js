/**
 * Anomaly detection — the agent Schedules tab's domain store (trinity-enterprise#74;
 * Invariant #6/#7: store = domain, single API client).
 *
 * One agent at a time. Reads the agent's schedule view (each schedule's
 * baseline state and any open incident) and its stored policy; writes the
 * agent's opt-in / remediation and closes an incident. The guard is the API
 * (entitlement + admin + person); the Schedules tab shows the panel only to an
 * admin on an entitled install, which mirrors it.
 *
 * Plus the fleet list behind Operations › Anomalies (ruled 2026-10-29): every
 * agent's incidents by status, and the open count the tab's badge shows.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'
import { apiErrorMessage } from '../utils/apiError'

export const FEATURE_ID = 'anomaly_detection'
export const BASE = '/api/enterprise/anomaly-detection'
export const schedulesUrl = (name) => `${BASE}/agents/${encodeURIComponent(name)}/schedules`
export const settingsUrl = (name) => `${BASE}/agents/${encodeURIComponent(name)}/settings`
export const resolveUrl = (id) => `${BASE}/incidents/${encodeURIComponent(id)}/resolve`
export const INCIDENTS_URL = `${BASE}/incidents`
export const FLEET_LIMIT = 200

export const useAnomalyDetectionStore = defineStore('anomalyDetection', () => {
  const agent = ref(null)
  const view = ref(null)            // { effective, schedules }
  const stored = ref(null)          // the agent's own settings row (what a PUT replaces)
  const loading = ref(false)
  const hasLoaded = ref(false)
  const error = ref(null)
  const saving = ref(false)
  const saveError = ref(null)
  const resolving = ref('')         // incident id with a close in flight
  const resolveError = ref(null)
  let gen = 0
  // Operations › Anomalies — every agent's incidents for one status.
  const fleetStatus = ref('open')
  const fleetIncidents = ref([])
  const fleetLoading = ref(false)
  const fleetHasLoaded = ref(false)
  const fleetError = ref(null)
  const openCount = ref(0)          // the tab badge; refreshed with every open read
  let fleetGen = 0

  const effective = computed(() => view.value?.effective || null)
  const watching = computed(() => !!effective.value?.enabled)
  const bySchedule = computed(() =>
    Object.fromEntries((view.value?.schedules || []).map((s) => [s.schedule_id, s])))

  function reset(name) {
    gen += 1
    agent.value = name
    view.value = null
    stored.value = null
    hasLoaded.value = false
    error.value = null
    saveError.value = null
    resolveError.value = null
  }

  async function load(name) {
    if (agent.value !== name) reset(name)
    const mine = gen
    loading.value = true
    try {
      const [v, s] = await Promise.all([api.get(schedulesUrl(name)), api.get(settingsUrl(name))])
      if (mine !== gen) return
      view.value = v.data
      stored.value = s.data?.agent || null
      hasLoaded.value = true
      error.value = null
    } catch (e) {
      if (mine !== gen) return
      error.value = apiErrorMessage(e, "Couldn't load anomaly detection for this agent")
    } finally {
      if (mine === gen) loading.value = false
    }
  }

  /** Change the agent's opt-in or remediation, keeping the rest of its row. */
  async function save(patch) {
    if (!agent.value) return false
    const name = agent.value
    saving.value = true
    saveError.value = null
    try {
      await api.put(settingsUrl(name), { ...(stored.value || {}), ...patch })
      await load(name)
      return true
    } catch (e) {
      saveError.value = apiErrorMessage(e, "That setting didn't save")
      return false
    } finally {
      saving.value = false
    }
  }

  async function loadFleet(status = fleetStatus.value) {
    if (status !== fleetStatus.value) {
      fleetStatus.value = status
      fleetIncidents.value = []
      fleetHasLoaded.value = false
    }
    const mine = ++fleetGen
    fleetLoading.value = true
    try {
      const r = await api.get(INCIDENTS_URL, { params: { status, limit: FLEET_LIMIT } })
      if (mine !== fleetGen) return
      fleetIncidents.value = r.data?.incidents || []
      if (status === 'open') openCount.value = fleetIncidents.value.length
      fleetHasLoaded.value = true
      fleetError.value = null
    } catch (e) {
      if (mine !== fleetGen) return
      fleetError.value = apiErrorMessage(e, "Couldn't load anomaly incidents")
    } finally {
      if (mine === fleetGen) fleetLoading.value = false
    }
  }

  /** The badge count alone, without touching the list on screen. */
  async function loadOpenCount() {
    try {
      const r = await api.get(INCIDENTS_URL, { params: { status: 'open', limit: FLEET_LIMIT } })
      openCount.value = (r.data?.incidents || []).length
    } catch {
      // A badge is context: a failed count leaves the last one.
    }
  }

  async function resolveIncident(id) {
    resolving.value = id
    resolveError.value = null
    try {
      await api.post(resolveUrl(id))
      if (fleetHasLoaded.value) await loadFleet()
      else await loadOpenCount()
      if (agent.value) await load(agent.value)
      return true
    } catch (e) {
      resolveError.value = apiErrorMessage(e, "The incident didn't close")
      return false
    } finally {
      resolving.value = ''
    }
  }

  return {
    agent, view, stored, loading, hasLoaded, error, saving, saveError, resolving, resolveError,
    effective, watching, bySchedule, reset, load, save, resolveIncident,
    fleetStatus, fleetIncidents, fleetLoading, fleetHasLoaded, fleetError, openCount,
    loadFleet, loadOpenCount,
  }
})
