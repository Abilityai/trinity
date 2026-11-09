/**
 * Guardrail events (trinity-enterprise#165 S4, GUARD-005) — what the agents'
 * guardrail hooks refused or hid, as the platform has received it.
 *
 * Two reads, each with its own loaded latch and error, so a failed refresh of
 * one keeps its data on screen (design-system p15) and never touches the other:
 *
 * - `fleet` — `GET /api/ops/guardrail-stats?hours=24`, the Operations › Health
 *   card. Every agent in scope comes back, events or not, with its coverage.
 * - `agent` — `GET /api/agents/{name}/guardrail-events?since=<24 h ago>`, the
 *   Settings › Guardrails "Recent events" list. Agent-scoped: AgentDetail is
 *   KeepAlive'd, so a read that lands after the page moved to another agent is
 *   dropped (the per-read sequence below), never shown under the wrong name.
 *
 * A read that fails keeps what loaded before. `hasLoaded` is set only by a
 * read that succeeded, so an empty state always means "the fetch returned
 * zero" (`utils/loadingState.js::viewState`). All HTTP goes through the shared
 * `api` client (Invariant #7).
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { sinceHours } from '../utils/guardrailEvents'

export const FLEET_WINDOW_HOURS = 24
export const AGENT_WINDOW_HOURS = 24
export const AGENT_PAGE_SIZE = 50

function errorText(e, fallback) {
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && typeof detail.reason === 'string') return detail.reason
  return e?.message || fallback
}

export const useGuardrailEventsStore = defineStore('guardrailEvents', () => {
  // ---- fleet --------------------------------------------------------------
  const fleet = ref(null)
  const fleetHasLoaded = ref(false)
  const fleetLoading = ref(false)
  const fleetError = ref(null)
  const fleetLoadedAt = ref(null)
  let fleetSeq = 0

  async function loadFleet(hours = FLEET_WINDOW_HOURS) {
    const seq = ++fleetSeq
    fleetLoading.value = true
    try {
      const { data } = await api.get('/api/ops/guardrail-stats', { params: { hours } })
      if (seq !== fleetSeq) return false
      fleet.value = data
      fleetError.value = null
      fleetHasLoaded.value = true
      fleetLoadedAt.value = new Date()
      return true
    } catch (e) {
      if (seq !== fleetSeq) return false
      fleetError.value = errorText(e, "Couldn't load the guardrail counts")
      return false
    } finally {
      if (seq === fleetSeq) fleetLoading.value = false
    }
  }

  // ---- one agent ----------------------------------------------------------
  const agentName = ref(null)
  const events = ref([])
  const reporting = ref(null)
  const nextCursor = ref(null)
  const agentHasLoaded = ref(false)
  const agentLoading = ref(false)
  const agentError = ref(null)
  let agentSeq = 0

  function setAgent(name) {
    if (agentName.value === name) return
    agentName.value = name
    events.value = []
    reporting.value = null
    nextCursor.value = null
    agentHasLoaded.value = false
    agentError.value = null
    agentSeq++ // a read still in flight for the previous agent lands nowhere
  }

  async function loadAgent(name, { hours = AGENT_WINDOW_HOURS, now = Date.now() } = {}) {
    setAgent(name)
    const seq = ++agentSeq
    agentLoading.value = true
    try {
      const { data } = await api.get(`/api/agents/${encodeURIComponent(name)}/guardrail-events`, {
        params: { since: sinceHours(hours, now), limit: AGENT_PAGE_SIZE },
      })
      if (seq !== agentSeq || name !== agentName.value) return false
      events.value = Array.isArray(data?.events) ? data.events : []
      reporting.value = data?.reporting ?? null
      nextCursor.value = data?.next_cursor ?? null
      agentError.value = null
      agentHasLoaded.value = true
      return true
    } catch (e) {
      if (seq !== agentSeq || name !== agentName.value) return false
      agentError.value = errorText(e, "Couldn't load the guardrail events")
      return false
    } finally {
      if (seq === agentSeq) agentLoading.value = false
    }
  }

  return {
    fleet, fleetHasLoaded, fleetLoading, fleetError, fleetLoadedAt, loadFleet,
    agentName, events, reporting, nextCursor, agentHasLoaded, agentLoading, agentError,
    setAgent, loadAgent,
  }
})
