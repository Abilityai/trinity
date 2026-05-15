import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import axios from 'axios'
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

  // --- actions ---
  async function fetchExecutions() {
    loading.value = true
    error.value = null
    try {
      const res = await axios.get('/api/executions', {
        params: _filterParams(0),
        headers: authStore.authHeader,
      })
      rows.value = res.data
      hasMore.value = res.data.length === LIMIT
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
    loading.value = true
    try {
      const res = await axios.get('/api/executions', {
        params: _filterParams(rows.value.length),
        headers: authStore.authHeader,
      })
      rows.value = [...rows.value, ...res.data]
      hasMore.value = res.data.length === LIMIT
    } catch (err) {
      error.value = err.response?.data?.detail || err.message
    } finally {
      loading.value = false
    }
  }

  async function refresh() {
    await Promise.all([fetchExecutions(), fetchStats()])
  }

  function setFilter(key, value) {
    filters.value[key] = value
    refresh()
  }

  function clearFilters() {
    filters.value = { agent: '', status: '', triggered_by: '', hours: 24, search: '' }
    refresh()
  }

  // WS: agent_activity events with schedule_start / schedule_end trigger a refresh.
  // We refetch rather than patch in-place because the event payload doesn't carry
  // the full row data (cost, duration, etc.) needed to update the list accurately.
  // Guard: if a refresh is already in flight, skip — the in-flight call will see the
  // latest state once it lands.
  function handleWebSocketEvent(data) {
    if (
      data.type === 'agent_activity' &&
      (data.activity_type === 'schedule_start' || data.activity_type === 'schedule_end') &&
      !loading.value
    ) {
      refresh()
    }
  }

  function startPolling(interval = 30000) {
    stopPolling()
    refresh()
    _pollTimer = setInterval(refresh, interval)
  }

  function stopPolling() {
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
