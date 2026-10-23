import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import axios from 'axios'
import { http } from '../utils/boundedHttp'
import { useAuthStore } from './auth'

export const useExecutionsStore = defineStore('executions', () => {
  const authStore = useAuthStore()

  // --- state ---
  const rows = ref([])
  const stats = ref(null)
  const loading = ref(false)
  const statsLoading = ref(false)
  const error = ref(null)
  const hasMore = ref(false)

  const filters = ref({
    agent: '',
    status: '',
    triggered_by: '',
    hours: 24,
    search: '',
  })

  let _pollTimer = null
  const LIMIT = 50
  // #3451: `GET /api/executions` caps `limit` at 200.
  const MAX_PAGE = 200
  // #3451: true while ExecutionsPanel shows the list (it owns the poll). Only
  // then does a refresh re-read every row the reader loaded, and only then is a
  // WebSocket trigger worth a read — the socket feeds this store on every page.
  let _listLive = false
  // #3451: bumped by every read that replaces the list. A read that lands after
  // a newer one began is dropped, so a filter change outranks a poll or a Load
  // more still in flight.
  let _listGen = 0
  // #3451: one list read at a time. A refresh asked for while a read is in
  // flight (the poll, a WebSocket trigger, the panel's refresh after a Stop)
  // runs ONCE when it lands — on the window the reader has then, so it neither
  // races a Load more nor gets dropped. The list reads go through `boundedHttp`
  // (30 s): every trigger waits on the read in flight, so a read that never
  // settled would otherwise hold the list until a filter change.
  let _refreshQueued = false

  // --- agent Overview analytics (#1107) ---
  // Historical, window-keyed: fetched once per (agent, window) and cached.
  // NEVER refetched on the stats poll — only on explicit window change or
  // a forced refresh. Keyed `${name}:${window}`.
  const analyticsCache = ref({})
  const analyticsLoading = ref(false)

  // --- per-schedule performance rollups (#1115) ---
  // One compact call per (agent, window), cached + shared by the Overview
  // "Schedules performance" section AND the Schedules-tab inline stats — so
  // neither surface issues N per-schedule round-trips. Same fetch-once
  // discipline as analytics above. Keyed `${name}:${window}`.
  const schedulesSummaryCache = ref({})
  const schedulesSummaryLoading = ref(false)

  // --- getters ---
  const hasActiveFilters = computed(() =>
    filters.value.agent ||
    filters.value.status ||
    filters.value.triggered_by ||
    filters.value.hours !== 24 ||
    filters.value.search
  )

  const runningCount = computed(() => stats.value?.running_count ?? 0)

  function _filterParams(offset = 0) {
    const p = { limit: LIMIT, offset }
    if (filters.value.agent) p.agent = filters.value.agent
    if (filters.value.status) p.status = filters.value.status
    if (filters.value.triggered_by) p.triggered_by = filters.value.triggered_by
    if (filters.value.hours !== 24) p.hours = filters.value.hours
    if (filters.value.search) p.search = filters.value.search
    return p
  }

  // The newest `size` rows, each once, and whether any row lies past them. One
  // row beyond the window is requested so `hasMore` is read rather than guessed
  // from a full page (an exhausted list otherwise grew a Load more that loaded
  // nothing). The route has no tiebreak and pages by offset, so a run that
  // starts between two pages repeats a row — kept once.
  async function _readTop(size) {
    const seen = new Set()
    const out = []
    let offset = 0
    // Until one row past the window has come back, or the list ends: a repeat
    // takes a slot it did not fill, so the walk reads on rather than come up short.
    while (out.length <= size) {
      const limit = Math.min(MAX_PAGE, size + 1 - out.length)
      const res = await http.get('/api/executions', {
        params: { ..._filterParams(offset), limit },
        headers: authStore.authHeader,
      })
      for (const row of res.data) {
        if (!seen.has(row.id)) {
          seen.add(row.id)
          out.push(row)
        }
      }
      offset += res.data.length
      if (res.data.length < limit) break
    }
    return { page: out.slice(0, size), more: out.length > size }
  }

  // The read that owns `loading` landed: release it, then run the one refresh
  // that was asked for meanwhile.
  function _readLanded(gen) {
    if (gen !== _listGen) return
    loading.value = false
    if (_refreshQueued) {
      _refreshQueued = false
      refresh()
    }
  }

  // --- actions ---
  // #3451: `keepLoaded` re-reads every row on screen instead of page one. The
  // 30 s poll and the WebSocket refresh used to replace `rows` with the first 50,
  // dropping whatever Load more had added — and the reader's place with them.
  async function fetchExecutions({ keepLoaded = false } = {}) {
    const gen = ++_listGen
    _refreshQueued = false // this read answers every refresh asked for before it
    loading.value = true
    error.value = null
    try {
      const { page, more } = await _readTop(keepLoaded ? Math.max(LIMIT, rows.value.length) : LIMIT)
      if (gen !== _listGen) return
      rows.value = page
      hasMore.value = more
    } catch (err) {
      if (gen === _listGen) error.value = err.response?.data?.detail || err.message
    } finally {
      _readLanded(gen)
    }
  }

  async function fetchStats() {
    statsLoading.value = true
    try {
      const p = { hours: filters.value.hours }
      if (filters.value.agent) p.agent = filters.value.agent
      const res = await axios.get('/api/executions/stats', {
        params: p,
        headers: authStore.authHeader,
      })
      stats.value = res.data
    } catch {
      // stats are non-critical — don't surface an error for them
    } finally {
      statsLoading.value = false
    }
  }

  async function loadMore() {
    if (!hasMore.value || loading.value) return
    const gen = _listGen
    loading.value = true
    try {
      const res = await http.get('/api/executions', {
        params: { ..._filterParams(rows.value.length), limit: LIMIT + 1 },
        headers: authStore.authHeader,
      })
      if (gen !== _listGen) return // a filter change replaced the list meanwhile
      const shown = new Set(rows.value.map((r) => r.id))
      rows.value = [...rows.value, ...res.data.slice(0, LIMIT).filter((r) => !shown.has(r.id))]
      hasMore.value = res.data.length > LIMIT
    } catch (err) {
      if (gen === _listGen) error.value = err.response?.data?.detail || err.message
    } finally {
      _readLanded(gen)
    }
  }

  // A refresh keeps the reader's loaded rows while the list is on screen; with
  // no panel showing it there is nothing to keep, so it reads page one. Asked
  // for while a read is in flight, it waits for it (see `_refreshQueued`); a new
  // query (`keepLoaded: false`) never waits.
  async function refresh({ keepLoaded = _listLive } = {}) {
    if (keepLoaded && loading.value) {
      _refreshQueued = true
      return
    }
    await Promise.all([fetchExecutions({ keepLoaded }), fetchStats()])
  }

  // Agent Overview analytics (#1107). Returns the cached payload unless
  // `force` is set or the (agent, window) pair hasn't been fetched yet.
  async function fetchAgentAnalytics(name, window = '7d', { force = false } = {}) {
    const key = `${name}:${window}`
    if (!force && analyticsCache.value[key]) return analyticsCache.value[key]
    analyticsLoading.value = true
    try {
      const res = await axios.get(
        `/api/agents/${encodeURIComponent(name)}/analytics`,
        { params: { window }, headers: authStore.authHeader }
      )
      analyticsCache.value = { ...analyticsCache.value, [key]: res.data }
      return res.data
    } finally {
      analyticsLoading.value = false
    }
  }

  // Per-schedule performance summary (#1115). Cached per (agent, window);
  // returns the cached payload unless `force` or first fetch. One call backs
  // both the Overview section and the Schedules-tab inline stats.
  async function fetchSchedulesSummary(name, window = '7d', { force = false } = {}) {
    const key = `${name}:${window}`
    if (!force && schedulesSummaryCache.value[key]) return schedulesSummaryCache.value[key]
    schedulesSummaryLoading.value = true
    try {
      const res = await axios.get(
        `/api/agents/${encodeURIComponent(name)}/schedules/analytics-summary`,
        { params: { window }, headers: authStore.authHeader }
      )
      schedulesSummaryCache.value = { ...schedulesSummaryCache.value, [key]: res.data }
      return res.data
    } finally {
      schedulesSummaryLoading.value = false
    }
  }

  // A new query starts from page one.
  function setFilter(key, value) {
    filters.value[key] = value
    refresh({ keepLoaded: false })
  }

  function clearFilters() {
    filters.value = { agent: '', status: '', triggered_by: '', hours: 24, search: '' }
    refresh({ keepLoaded: false })
  }

  // WS: agent_activity events with schedule_start / schedule_end trigger a refresh.
  // We refetch rather than patch in-place because the event payload doesn't carry
  // the full row data (cost, duration, etc.) needed to update the list accurately.
  // #3451: only while a panel shows the list; a burst during a read collapses into
  // the one refresh that follows it (a Load more reads only the tail, so an event
  // it overlapped used to be lost until the next poll).
  function handleWebSocketEvent(data) {
    if (
      _listLive &&
      data.type === 'agent_activity' &&
      (data.activity_type === 'schedule_start' || data.activity_type === 'schedule_end')
    ) {
      refresh()
    }
  }

  // #3451: a panel that mounts starts from page one (its list opens at the top);
  // a tick during a read waits for it like any other refresh.
  function startPolling(interval = 30000) {
    stopPolling()
    _listLive = true
    refresh({ keepLoaded: false })
    _pollTimer = setInterval(() => refresh(), interval)
  }

  function stopPolling() {
    _listLive = false
    _refreshQueued = false
    if (_pollTimer) {
      clearInterval(_pollTimer)
      _pollTimer = null
    }
  }

  return {
    rows,
    stats,
    loading,
    statsLoading,
    error,
    hasMore,
    filters,
    hasActiveFilters,
    runningCount,
    analyticsCache,
    analyticsLoading,
    fetchAgentAnalytics,
    schedulesSummaryCache,
    schedulesSummaryLoading,
    fetchSchedulesSummary,
    fetchExecutions,
    fetchStats,
    loadMore,
    refresh,
    setFilter,
    clearFilters,
    handleWebSocketEvent,
    startPolling,
    stopPolling,
  }
})
