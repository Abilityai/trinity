/**
 * Notifications Store
 *
 * Pinia store for managing agent notifications.
 * Part of NOTIF-002 (Events Page UI).
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import axios from 'axios'
import api from '../api'

export const useNotificationsStore = defineStore('notifications', () => {
  // State
  const notifications = ref([])
  const pendingCount = ref(0)
  const loading = ref(false)
  const error = ref(null)
  // #1926: only a SUCCEEDED fetch licenses the empty state. Before this flips,
  // an empty `notifications` means "loading" or "failed", never "no events".
  const hasLoaded = ref(false)
  const totalCount = ref(0)
  const hasMore = ref(true)

  // Filters
  const filters = ref({
    status: 'pending',      // pending, acknowledged, dismissed, or ''
    priority: '',           // low, normal, high, urgent, or '' (comma-separated for multiple)
    agentName: '',          // filter by agent
    showDismissed: false,
    limit: 50,
    offset: 0
  })

  // Selected items for bulk actions
  const selectedIds = ref([])

  // Getters
  const pendingNotifications = computed(() =>
    notifications.value.filter(n => n.status === 'pending')
  )

  const acknowledgedNotifications = computed(() =>
    notifications.value.filter(n => n.status === 'acknowledged')
  )

  const hasPendingNotifications = computed(() => pendingCount.value > 0)

  const hasUrgentPending = computed(() =>
    notifications.value.some(n => n.status === 'pending' && (n.priority === 'urgent' || n.priority === 'high'))
  )

  const agentCounts = computed(() => {
    const counts = {}
    notifications.value.forEach(n => {
      counts[n.agent_name] = (counts[n.agent_name] || 0) + 1
    })
    return counts
  })

  const filteredNotifications = computed(() => {
    let result = [...notifications.value]

    // Filter out dismissed unless showDismissed is true
    if (!filters.value.showDismissed) {
      result = result.filter(n => n.status !== 'dismissed')
    }

    return result
  })

  // Actions
  async function fetchNotifications(options = {}) {
    loading.value = true
    error.value = null

    try {
      const token = localStorage.getItem('token')
      const params = new URLSearchParams()

      const status = options.status ?? filters.value.status
      const priority = options.priority ?? filters.value.priority
      const agentName = options.agentName ?? filters.value.agentName
      const limit = options.limit ?? filters.value.limit
      const offset = options.offset ?? filters.value.offset

      if (status) {
        params.append('status', status)
      }
      if (priority) {
        params.append('priority', priority)
      }
      if (agentName) {
        params.append('agent_name', agentName)
      }
      params.append('limit', limit)

      const response = await axios.get(`/api/notifications?${params}`, {
        headers: { Authorization: `Bearer ${token}` },
      })

      // If appending (offset > 0), add to existing; otherwise replace
      if (offset > 0) {
        notifications.value = [...notifications.value, ...(response.data.notifications || [])]
      } else {
        notifications.value = response.data.notifications || []
      }

      totalCount.value = response.data.count || 0
      hasMore.value = notifications.value.length < totalCount.value

      // Update pending count from filtered results
      updatePendingCount()
      hasLoaded.value = true
    } catch (err) {
      console.error('Failed to fetch notifications:', err)
      error.value = err.response?.data?.detail || 'Failed to load notifications'
    } finally {
      loading.value = false
    }
  }

  async function fetchPendingCount() {
    try {
      const token = localStorage.getItem('token')
      // #1143: use the dedicated count endpoint — the list endpoint's `count`
      // is page-capped (len of returned page), so limit=1 clamped the badge to 1.
      const response = await axios.get('/api/notifications/count?status=pending', {
        headers: { Authorization: `Bearer ${token}` },
      })
      pendingCount.value = response.data.count || 0
    } catch (err) {
      console.error('Failed to fetch pending count:', err)
    }
  }

  function updatePendingCount() {
    pendingCount.value = notifications.value.filter(n => n.status === 'pending').length
  }

  async function acknowledgeNotification(notificationId) {
    try {
      const token = localStorage.getItem('token')
      await axios.post(`/api/notifications/${notificationId}/acknowledge`, {}, {
        headers: { Authorization: `Bearer ${token}` },
      })

      // Update local state
      const notification = notifications.value.find(n => n.id === notificationId)
      if (notification) {
        notification.status = 'acknowledged'
        notification.acknowledged_at = new Date().toISOString()
      }
      pendingCount.value = Math.max(0, pendingCount.value - 1)

      return true
    } catch (err) {
      console.error('Failed to acknowledge notification:', err)
      throw err
    }
  }

  async function dismissNotification(notificationId) {
    try {
      const token = localStorage.getItem('token')
      await axios.post(`/api/notifications/${notificationId}/dismiss`, {}, {
        headers: { Authorization: `Bearer ${token}` },
      })

      // Update local state
      const notification = notifications.value.find(n => n.id === notificationId)
      if (notification) {
        notification.status = 'dismissed'
      }

      // If not showing dismissed, remove from list
      if (!filters.value.showDismissed) {
        notifications.value = notifications.value.filter(n => n.id !== notificationId)
      }

      pendingCount.value = Math.max(0, pendingCount.value - 1)

      return true
    } catch (err) {
      console.error('Failed to dismiss notification:', err)
      throw err
    }
  }

  // Server-side Clear All (#1017): one call dismisses every non-dismissed
  // notification for accessible agents — including ones beyond the loaded
  // page — unlike the per-id bulk helpers below.
  async function dismissAll(agentName = null) {
    error.value = null
    try {
      const response = await api.post('/api/notifications/dismiss-all', {
        agent_name: agentName,
      })
      await fetchNotifications()
      await fetchPendingCount()
      return response.data
    } catch (err) {
      error.value = err.response?.data?.detail || 'Failed to dismiss notifications'
      throw err
    }
  }

  async function bulkAcknowledge(ids) {
    const results = await Promise.allSettled(
      ids.map(id => acknowledgeNotification(id))
    )
    selectedIds.value = []
    return results
  }

  async function bulkDismiss(ids) {
    const results = await Promise.allSettled(
      ids.map(id => dismissNotification(id))
    )
    selectedIds.value = []
    return results
  }

  // Add a notification from WebSocket event
  function addNotification(notification) {
    // Prepend to list if it matches current filters
    const matchesStatus = !filters.value.status || notification.status === filters.value.status
    const matchesPriority = !filters.value.priority || filters.value.priority.includes(notification.priority)
    const matchesAgent = !filters.value.agentName || notification.agent_name === filters.value.agentName

    if (matchesStatus && matchesPriority && matchesAgent) {
      // Check if already exists
      const exists = notifications.value.some(n => n.id === notification.id)
      if (!exists) {
        notifications.value.unshift(notification)
      }
    }

    // Always update pending count
    if (notification.status === 'pending') {
      pendingCount.value += 1
    }
  }

  // Polling for badge updates
  let pollInterval = null

  function startPolling(intervalMs = 60000) {
    stopPolling()
    fetchPendingCount()
    pollInterval = setInterval(() => {
      if (!document.hidden) fetchPendingCount()  // PERF-269: skip when tab backgrounded
    }, intervalMs)
  }

  function stopPolling() {
    if (pollInterval) {
      clearInterval(pollInterval)
      pollInterval = null
    }
  }

  // Set filters and refetch
  function setFilters(newFilters) {
    filters.value = { ...filters.value, ...newFilters, offset: 0 }
    fetchNotifications()
  }

  function clearFilters() {
    filters.value = {
      status: 'pending',
      priority: '',
      agentName: '',
      showDismissed: false,
      limit: 50,
      offset: 0,
    }
    fetchNotifications()
  }

  function loadMore() {
    if (hasMore.value && !loading.value) {
      filters.value.offset = notifications.value.length
      fetchNotifications({ offset: filters.value.offset })
    }
  }

  function toggleSelected(notificationId) {
    const index = selectedIds.value.indexOf(notificationId)
    if (index === -1) {
      selectedIds.value.push(notificationId)
    } else {
      selectedIds.value.splice(index, 1)
    }
  }

  function selectAll() {
    selectedIds.value = notifications.value.map(n => n.id)
  }

  function clearSelection() {
    selectedIds.value = []
  }

  return {
    // State
    notifications,
    pendingCount,
    loading,
    error,
    hasLoaded,
    totalCount,
    hasMore,
    filters,
    selectedIds,
    // Getters
    pendingNotifications,
    acknowledgedNotifications,
    hasPendingNotifications,
    hasUrgentPending,
    agentCounts,
    filteredNotifications,
    // Actions
    fetchNotifications,
    fetchPendingCount,
    acknowledgeNotification,
    dismissNotification,
    dismissAll,
    bulkAcknowledge,
    bulkDismiss,
    addNotification,
    startPolling,
    stopPolling,
    setFilters,
    clearFilters,
    loadMore,
    toggleSelected,
    selectAll,
    clearSelection,
  }
})
