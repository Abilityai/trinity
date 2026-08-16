import { defineStore } from 'pinia'

// The page size moved to a dependency-free leaf in #2162 so the Workspace
// store can share this one number instead of typing a third copy of it.
import { REPORT_ROWS_PAGE as ROWS_PAGE } from '../utils/reportPaging'
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

// Export download (#1536). Deliberately NOT a plain `<a href>`: Trinity
// authenticates with a Bearer JWT held in localStorage and attached by the
// api.js interceptor, so a raw browser navigation to the export URL carries no
// credential and the endpoint answers 401. (That was the original shipped bug —
// the code comment justifying the anchor assumed cookie auth this platform does
// not use.) Fetch through the shared client so the interceptor runs, then hand
// the browser a blob URL. Mirrors `agents.js:getFilePreviewBlob`.
//
// The server-supplied Content-Disposition filename is honoured when present, so
// the downloaded name still comes from the backend rather than being rebuilt here.
export async function downloadReportExport(reportId, format) {
  const res = await api.get(`/api/reports/${reportId}/export`, {
    params: { format },
    responseType: 'blob',
  })

  let filename = `report-${reportId}.${format}`
  const cd = res.headers?.['content-disposition'] || res.headers?.['Content-Disposition']
  const match = cd && /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd)
  if (match) filename = decodeURIComponent(match[1])

  const url = URL.createObjectURL(res.data)
  try {
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    // Revoke on the next tick — revoking synchronously can cancel the download
    // in some browsers before it is handed to the download manager.
    setTimeout(() => URL.revokeObjectURL(url), 10_000)
  }
}

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
  const rowMeta = ref({})            // report_id -> {total, loaded} for tabular reports (#1537)

  // #1539: same filter shape as the fleet slice, minus `agent` (the panel is
  // already scoped to one). Reset on agent switch so a filter typed on one
  // agent's tab doesn't silently hide another's reports.
  const filters = ref({ report_type: '', hours: 168, search: '' })

  const _loadInFlight = new Set()

  function setAgent(name) {
    if (agentName.value !== name) {
      agentName.value = name
      reports.value = []
      error.value = null
      filters.value = { report_type: '', hours: 168, search: '' }
    }
  }

  function setFilter(key, value) {
    filters.value = { ...filters.value, [key]: value }
    fetchReports()
  }

  function _params() {
    const p = { hours: filters.value.hours, limit: 100 }
    if (filters.value.report_type) p.report_type = filters.value.report_type
    if (filters.value.search) p.search = filters.value.search
    return p
  }

  async function fetchReports() {
    if (!agentName.value || loading.value) return
    loading.value = true
    error.value = null
    try {
      const res = await api.get(`/api/agents/${agentName.value}/reports`, {
        params: _params(),
      })
      reports.value = res.data
    } catch (err) {
      error.value = err.response?.data?.detail || err.message
    } finally {
      loading.value = false
    }
  }

  // Lazy-load a single report's full payload (only when a card expands).
  // #1537: a `table` report is fetched a PAGE at a time. The cap is now 5 MiB,
  // so "expand the card" must not mean "ship the whole blob" — the row reader
  // returns columns once plus a window, and `rowMeta` carries the true total so
  // the card can offer more. Every other display_hint is a bounded document and
  // still fetches whole.
  async function loadPayload(reportId, displayHint) {
    if (payloads.value[reportId] || _loadInFlight.has(reportId)) return
    _loadInFlight.add(reportId)
    try {
      if (displayHint === 'table') {
        const res = await api.get(`/api/reports/${reportId}/rows`, {
          params: { offset: 0, limit: ROWS_PAGE },
        })
        payloads.value = {
          ...payloads.value,
          [reportId]: { payload: { columns: res.data.columns, rows: res.data.rows } },
        }
        rowMeta.value = { ...rowMeta.value, [reportId]: { total: res.data.total, loaded: res.data.rows.length } }
        return
      }
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


  // Append the next page of a tabular report's rows (#1537).
  async function loadMoreRows(reportId) {
    const meta = rowMeta.value[reportId]
    const current = payloads.value[reportId]
    if (!meta || !current || meta.loaded >= meta.total) return
    const res = await api.get(`/api/reports/${reportId}/rows`, {
      params: { offset: meta.loaded, limit: ROWS_PAGE },
    })
    const merged = [...current.payload.rows, ...res.data.rows]
    payloads.value = {
      ...payloads.value,
      [reportId]: { payload: { columns: res.data.columns, rows: merged } },
    }
    rowMeta.value = { ...rowMeta.value, [reportId]: { total: res.data.total, loaded: merged.length } }
  }

  function toggleExpanded(reportId) {
    expandedId.value = expandedId.value === reportId ? null : reportId
    if (expandedId.value) {
      // display_hint drives the fetch shape (#1537): `table` pages, everything
      // else fetches whole. It comes from the summary already in hand, so no
      // extra request is needed to decide.
      const hint = (reports.value.find((r) => r.id === reportId) || {}).display_hint
      loadPayload(reportId, hint)
    }
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
    reports, agentName, loading, error, expandedId, payloads, rowMeta, filters,
    setAgent, setFilter, fetchReports, loadPayload, loadMoreRows, deleteReport, toggleExpanded,
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
  const rowMeta = ref({})            // #1537 tabular paging state
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

  // #1537: a `table` report is fetched a PAGE at a time. The cap is now 5 MiB,
  // so "expand the card" must not mean "ship the whole blob" — the row reader
  // returns columns once plus a window, and `rowMeta` carries the true total so
  // the card can offer more. Every other display_hint is a bounded document and
  // still fetches whole.
  async function loadPayload(reportId, displayHint) {
    if (payloads.value[reportId] || _loadInFlight.has(reportId)) return
    _loadInFlight.add(reportId)
    try {
      if (displayHint === 'table') {
        const res = await api.get(`/api/reports/${reportId}/rows`, {
          params: { offset: 0, limit: ROWS_PAGE },
        })
        payloads.value = {
          ...payloads.value,
          [reportId]: { payload: { columns: res.data.columns, rows: res.data.rows } },
        }
        rowMeta.value = { ...rowMeta.value, [reportId]: { total: res.data.total, loaded: res.data.rows.length } }
        return
      }
      const res = await api.get(`/api/reports/${reportId}`)
      payloads.value = { ...payloads.value, [reportId]: res.data }
    } catch {
      // ignore
    } finally {
      _loadInFlight.delete(reportId)
    }
  }


  // Append the next page of a tabular report's rows (#1537).
  async function loadMoreRows(reportId) {
    const meta = rowMeta.value[reportId]
    const current = payloads.value[reportId]
    if (!meta || !current || meta.loaded >= meta.total) return
    const res = await api.get(`/api/reports/${reportId}/rows`, {
      params: { offset: meta.loaded, limit: ROWS_PAGE },
    })
    const merged = [...current.payload.rows, ...res.data.rows]
    payloads.value = {
      ...payloads.value,
      [reportId]: { payload: { columns: res.data.columns, rows: merged } },
    }
    rowMeta.value = { ...rowMeta.value, [reportId]: { total: res.data.total, loaded: merged.length } }
  }

  function toggleExpanded(reportId) {
    expandedId.value = expandedId.value === reportId ? null : reportId
    if (expandedId.value) {
      // display_hint drives the fetch shape (#1537): `table` pages, everything
      // else fetches whole. It comes from the summary already in hand, so no
      // extra request is needed to decide.
      const hint = (reports.value.find((r) => r.id === reportId) || {}).display_hint
      loadPayload(reportId, hint)
    }
  }

  function handleWebSocketEvent(data) {
    if (!active.value) return
    if (data.type !== 'agent_report') return
    if (loading.value) return
    refresh()
  }

  return {
    reports, stats, loading, statsLoading, error, expandedId, payloads, rowMeta, filters, active,
    setActive, fetchReports, fetchStats, refresh, setFilter, loadPayload, loadMoreRows, toggleExpanded,
    handleWebSocketEvent,
  }
})
