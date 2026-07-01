import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

// Agent reports UI stores (#918).
//
// Two separate stores by design (review/Codex #7): the per-agent ReportsPanel
// and the fleet ReportsPanelFleet must not share loading/error/state, so the
// agent panel's clear() on unmount can never wipe fleet state and a WS refresh
// gate on one can't be confused by the other.
//
// Reports arrive as a THIN WebSocket trigger (agent_name, report_id,
// report_type, created_at) — no payload on the wire. The trigger refetches via
// the access-controlled REST endpoints. Full payloads load lazily per-report.
// All HTTP goes through the shared api.js client (Invariant #7).

// ---------------------------------------------------------------------------
// Agent-scoped store: backs the Agent Detail "Reports" tab.
// ---------------------------------------------------------------------------
export const useReportsStore = defineStore('reports', () => {
  const reports = ref([])            // ReportSummary[] (metadata only) for the agent
  const agentName = ref(null)        // the agent ReportsPanel is currently showing
  const loading = ref(false)
  const error = ref(null)
  const expandedId = ref(null)       // survives tab remount (globally-unique id)
  const payloads = ref({})           // report_id -> full report (lazy cache)

  const _loadInFlight = new Set()

  function setAgent(name) {
    if (agentName.value !== name) {
      agentName.value = name
      reports.value = []
      error.value = null
    }
  }

  async function fetchReports() {
    if (!agentName.value || loading.value) return
    loading.value = true
    error.value = null
    try {
      const res = await api.get(`/api/agents/${agentName.value}/reports`, {
        params: { limit: 100 },
      })
      reports.value = res.data
    } catch (err) {
      error.value = err.response?.data?.detail || err.message
    } finally {
      loading.value = false
    }
  }

  // Lazy-load a single report's full payload (only when a card expands).
  async function loadPayload(reportId) {
    if (payloads.value[reportId] || _loadInFlight.has(reportId)) return
    _loadInFlight.add(reportId)
    try {
      const res = await api.get(`/api/reports/${reportId}`)
      payloads.value = { ...payloads.value, [reportId]: res.data }
    } catch {
      // 404 (deleted/no-access) — leave uncached; the renderer shows an error state.
    } finally {
      _loadInFlight.delete(reportId)
    }
  }

  async function deleteReport(reportId) {
    if (!agentName.value) return
    try {
      await api.delete(`/api/agents/${agentName.value}/reports/${reportId}`)
      reports.value = reports.value.filter((r) => r.id !== reportId)
      const { [reportId]: _drop, ...rest } = payloads.value
      payloads.value = rest
      if (expandedId.value === reportId) expandedId.value = null
      return true
    } catch (err) {
      error.value = err.response?.data?.detail || err.message
      return false
    }
  }

  function toggleExpanded(reportId) {
    expandedId.value = expandedId.value === reportId ? null : reportId
    if (expandedId.value) loadPayload(reportId)
  }

  // Thin trigger broadcast fleet-wide; only react for the agent on screen.
  function handleWebSocketEvent(data) {
    if (!agentName.value) return
    if (data.type !== 'agent_report') return
    if (data.agent_name !== agentName.value) return
    fetchReports()
  }

  function clearAgent() {
    agentName.value = null
    reports.value = []
    error.value = null
  }

  return {
    reports, agentName, loading, error, expandedId, payloads,
    setAgent, fetchReports, loadPayload, deleteReport, toggleExpanded,
    handleWebSocketEvent, clearAgent,
  }
})

// ---------------------------------------------------------------------------
// Fleet store: backs the Operations → Reports tab.
// ---------------------------------------------------------------------------
export const useFleetReportsStore = defineStore('fleetReports', () => {
  const reports = ref([])            // ReportSummary[] across accessible agents
  const stats = ref(null)            // FleetReportStats
  const loading = ref(false)
  const statsLoading = ref(false)
  const error = ref(null)
  const expandedId = ref(null)
  const payloads = ref({})
  const filters = ref({ agent: '', report_type: '', hours: 168, search: '' })
  const active = ref(false)          // true only while ReportsPanelFleet is mounted

  const _loadInFlight = new Set()

  function setActive(value) {
    active.value = value
  }

  function _params() {
    const p = { hours: filters.value.hours, limit: 100 }
    if (filters.value.agent) p.agent = filters.value.agent
    if (filters.value.report_type) p.report_type = filters.value.report_type
    if (filters.value.search) p.search = filters.value.search
    return p
  }

  async function fetchReports() {
    loading.value = true
    error.value = null
    try {
      const res = await api.get('/api/reports', { params: _params() })
      reports.value = res.data
    } catch (err) {
      error.value = err.response?.data?.detail || err.message
    } finally {
      loading.value = false
    }
  }

  async function fetchStats() {
    statsLoading.value = true
    try {
      const p = { hours: filters.value.hours }
      if (filters.value.agent) p.agent = filters.value.agent
      if (filters.value.report_type) p.report_type = filters.value.report_type
      const res = await api.get('/api/reports/stats', { params: p })
      stats.value = res.data
    } catch {
      // stats are best-effort KPI tiles; don't surface as a blocking error
    } finally {
      statsLoading.value = false
    }
  }

  async function refresh() {
    await Promise.all([fetchReports(), fetchStats()])
  }

  function setFilter(key, value) {
    filters.value = { ...filters.value, [key]: value }
    refresh()
  }

  async function loadPayload(reportId) {
    if (payloads.value[reportId] || _loadInFlight.has(reportId)) return
    _loadInFlight.add(reportId)
    try {
      const res = await api.get(`/api/reports/${reportId}`)
      payloads.value = { ...payloads.value, [reportId]: res.data }
    } catch {
      // ignore
    } finally {
      _loadInFlight.delete(reportId)
    }
  }

  function toggleExpanded(reportId) {
    expandedId.value = expandedId.value === reportId ? null : reportId
    if (expandedId.value) loadPayload(reportId)
  }

  function handleWebSocketEvent(data) {
    if (!active.value) return
    if (data.type !== 'agent_report') return
    if (loading.value) return
    refresh()
  }

  return {
    reports, stats, loading, statsLoading, error, expandedId, payloads, filters, active,
    setActive, fetchReports, fetchStats, refresh, setFilter, loadPayload, toggleExpanded,
    handleWebSocketEvent,
  }
})
