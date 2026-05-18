/**
 * Operator Queue Store (OPS-001)
 *
 * Pinia store for the Operating Room UI.
 * Fetches data from backend API with real-time WebSocket updates.
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import axios from 'axios'
import { useAuthStore } from './auth'

// Agent display helpers
const AGENT_COLORS = [
  'bg-blue-500', 'bg-emerald-500', 'bg-purple-500', 'bg-amber-500',
  'bg-rose-500', 'bg-cyan-500', 'bg-indigo-500', 'bg-teal-500'
]

function getAgentProfile(name) {
  const initials = name.split('-').map(w => w[0]?.toUpperCase()).join('').slice(0, 2)
  // Deterministic color from name
  let hash = 0
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash)
  const color = AGENT_COLORS[Math.abs(hash) % AGENT_COLORS.length]
  return { initials, color, role: 'Agent' }
}

export const useOperatorQueueStore = defineStore('operatorQueue', () => {
  const authStore = useAuthStore()

  // State
  const items = ref([])
  const expandedItemId = ref(null)
  const loading = ref(false)
  const error = ref(null)
  const activeTab = ref('open') // 'open' or 'resolved'
  let _pollTimer = null

  // Getters
  const openItems = computed(() => {
    const result = items.value.filter(i => i.status === 'pending')
    const priorityOrder = { critical: 0, high: 1, medium: 2, low: 3 }
    result.sort((a, b) => {
      const pa = priorityOrder[a.priority] ?? 4
      const pb = priorityOrder[b.priority] ?? 4
      if (pa !== pb) return pa - pb
      return new Date(a.created_at) - new Date(b.created_at)
    })
    return result
  })

  const resolvedItems = computed(() => {
    return items.value
      .filter(i => i.status === 'responded' || i.status === 'acknowledged' || i.status === 'recovered')
      .sort((a, b) => {
        const aTime = a.context?.recovered_at || a.responded_at || a.created_at
        const bTime = b.context?.recovered_at || b.responded_at || b.created_at
        return new Date(bTime) - new Date(aTime)
      })
  })

  const pendingCount = computed(() =>
    items.value.filter(i => i.status === 'pending').length
  )

  const criticalCount = computed(() =>
    items.value.filter(i => i.status === 'pending' && i.priority === 'critical').length
  )

  const openItemsByAgent = computed(() => {
    const groups = {}
    for (const item of openItems.value) {
      if (!groups[item.agent_name]) {
        groups[item.agent_name] = []
      }
      groups[item.agent_name].push(item)
    }
    return groups
  })

  function getProfile(agentName) {
    return getAgentProfile(agentName)
  }

  // API Actions
  async function fetchItems() {
    loading.value = true
    error.value = null
    try {
      const response = await axios.get('/api/operator-queue', {
        params: { limit: 200 },
        headers: authStore.authHeader
      })
      items.value = response.data.items || []
    } catch (err) {
      error.value = err.response?.data?.detail || err.message
      // Don't clear items on error — keep stale data visible
    } finally {
      loading.value = false
    }
  }

  async function respondToItem(id, response, responseText = '') {
    const item = items.value.find(i => i.id === id)
    if (!item) return

    try {
      await axios.post(
        `/api/operator-queue/${id}/respond`,
        { response, response_text: responseText || null },
        { headers: authStore.authHeader }
      )

      // Optimistic update
      item.status = 'responded'
      item.response = response
      item.response_text = responseText
      item.responded_by_email = authStore.userEmail || authStore.user?.username
      item.responded_at = new Date().toISOString()
      expandedItemId.value = null

      // Auto-advance: expand next open item
      const nextOpen = openItems.value.find(i => i.id !== id && i.status === 'pending')
      if (nextOpen) {
        expandedItemId.value = nextOpen.id
      }
    } catch (err) {
      error.value = err.response?.data?.detail || err.message
    }
  }

  async function acknowledgeItem(id) {
    await respondToItem(id, 'acknowledged', '')
  }

  function toggleExpand(id) {
    expandedItemId.value = expandedItemId.value === id ? null : id
  }

  // WebSocket event handler
  function handleWebSocketEvent(data) {
    if (data.type === 'operator_queue_new') {
      // New item from agent — refetch to get full data
      fetchItems()
    } else if (data.type === 'operator_queue_responded') {
      // Another operator responded — update locally
      const item = items.value.find(i => i.id === data.data?.id)
      if (item) {
        item.status = 'responded'
        item.response = data.data.response
        item.responded_by_email = data.data.responded_by_email
        item.responded_at = new Date().toISOString()
      }
    } else if (data.type === 'operator_queue_acknowledged') {
      // Agent acknowledged — update locally
      const item = items.value.find(i => i.id === data.data?.id)
      if (item) {
        item.status = 'acknowledged'
      }
    } else if (data.type === 'operator_queue_recovered') {
      // Backend verified the underlying failure has recovered.
      const item = items.value.find(i => i.id === data.data?.id)
      if (item) {
        item.status = 'recovered'
        item.priority = item.priority === 'critical' || item.priority === 'high' ? 'low' : item.priority
        item.context = {
          ...(item.context || {}),
          resolution_state: 'recovered_unacknowledged',
          recovered_at: data.data?.recovered_at || new Date().toISOString()
        }
      } else {
        fetchItems()
      }
    }
  }

  // Polling (fallback for when WebSocket misses events)
  function startPolling(interval = 15000) {
    stopPolling()
    fetchItems() // Initial fetch
    _pollTimer = setInterval(fetchItems, interval)
  }

  function stopPolling() {
    if (_pollTimer) {
      clearInterval(_pollTimer)
      _pollTimer = null
    }
  }

  return {
    items,
    expandedItemId,
    loading,
    error,
    activeTab,
    openItems,
    resolvedItems,
    pendingCount,
    criticalCount,
    openItemsByAgent,
    getProfile,
    fetchItems,
    toggleExpand,
    respondToItem,
    acknowledgeItem,
    handleWebSocketEvent,
    startPolling,
    stopPolling,
  }
})
