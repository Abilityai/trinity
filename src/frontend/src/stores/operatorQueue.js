/**
 * Operator Queue Store (OPS-001)
 *
 * Pinia store for the Operating Room UI.
 * Fetches data from backend API with real-time WebSocket updates.
 */

import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import axios from 'axios'
import api from '../api'
import { useAuthStore } from './auth'
import { apiErrorMessage } from '../utils/apiError'
import { decideAutoExpand } from '../utils/loadingState'

// Agent display helpers
const AGENT_COLORS = [
  'bg-blue-500', 'bg-emerald-500', 'bg-accent-purple-500', 'bg-state-autonomous-500',
  'bg-state-locked-500', 'bg-cyan-500', 'bg-action-primary-500', 'bg-teal-500'
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
  // #1927: the landing rule's arming bit. True until a human toggles any card
  // (they have taken control of expansion — principle 5), re-armed whenever the
  // open set drains to zero so the next 0→N arrival lands expanded again.
  const autoExpandArmed = ref(true)
  const loading = ref(false)
  const error = ref(null)
  // #1926: "the list is empty" is only true once a fetch has SUCCEEDED and
  // returned zero. Before the first success, `items.length === 0` means
  // "loading" or "failed" — consumers must gate their empty state on this.
  const hasLoaded = ref(false)
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

  // Resolved feed includes cancelled/expired (#1017) — before that, items
  // cancelled from Needs Response vanished from the UI entirely.
  const RESOLVED_STATUSES = ['responded', 'acknowledged', 'cancelled', 'expired']

  const resolvedItems = computed(() => {
    return items.value
      .filter(i => RESOLVED_STATUSES.includes(i.status))
      .sort((a, b) => new Date(b.responded_at || b.created_at) - new Date(a.responded_at || a.created_at))
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
      hasLoaded.value = true
    } catch (err) {
      error.value = apiErrorMessage(err, 'Request failed')
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
      if (err.response?.status === 409) {
        // Item left 'pending' under us (e.g. another operator cleared the
        // queue) — the response was NOT recorded (#1017).
        error.value = 'This item was cancelled or answered by another operator — your response was not recorded.'
        fetchItems()
      } else {
        error.value = apiErrorMessage(err, 'Request failed')
      }
    }
  }

  // Bulk actions (#1017)
  async function bulkCancel(ids) {
    if (!ids || ids.length === 0) return { cancelled: 0, skipped: 0 }
    error.value = null
    try {
      const response = await api.post('/api/operator-queue/bulk-cancel', { ids })
      await fetchItems()
      return response.data
    } catch (err) {
      error.value = apiErrorMessage(err, 'Request failed')
      throw err
    }
  }

  async function clearResolved(agentName = null) {
    error.value = null
    try {
      const response = await api.post('/api/operator-queue/clear-resolved', {
        agent_name: agentName,
      })
      await fetchItems()
      return response.data
    } catch (err) {
      error.value = apiErrorMessage(err, 'Request failed')
      throw err
    }
  }

  async function acknowledgeItem(id) {
    await respondToItem(id, 'acknowledged', '')
  }

  function toggleExpand(id) {
    expandedItemId.value = expandedItemId.value === id ? null : id
    // A human chose — no poll, WS delta or remount may override it (#1927).
    autoExpandArmed.value = false
  }

  // #1927: auto-expand the first open item ONCE per armed episode. Lives beside
  // the expansion state it governs (design-system p21), not in the view — the
  // store is a singleton fed by WS events and earlier visits, so a view-local
  // "once per mount" either misses a warm store or re-expands over a remembered
  // collapse. `decideAutoExpand` checks MEMBERSHIP of expandedItemId in the open
  // set, so an id that was answered while the operator was away never blocks
  // the rule forever. `respondToItem`'s auto-advance is a separate, unchanged rule.
  function maybeAutoExpand() {
    const id = decideAutoExpand({
      armed: autoExpandArmed.value,
      openIds: openItems.value.map(i => i.id),
      expandedId: expandedItemId.value,
    })
    if (id == null) return false
    expandedItemId.value = id
    autoExpandArmed.value = false
    return true
  }

  // Re-arm when the queue drains, whatever drained it (poll, respond, WS, clear).
  watch(() => openItems.value.length, (len) => {
    if (len === 0) autoExpandArmed.value = true
  })

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
    } else if (data.type === 'operator_queue_cleared') {
      // Bulk clear by an operator (#1017) — refetch authoritative state
      fetchItems()
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
    autoExpandArmed,
    loading,
    error,
    hasLoaded,
    activeTab,
    openItems,
    resolvedItems,
    pendingCount,
    criticalCount,
    openItemsByAgent,
    getProfile,
    fetchItems,
    toggleExpand,
    maybeAutoExpand,
    respondToItem,
    acknowledgeItem,
    bulkCancel,
    clearResolved,
    handleWebSocketEvent,
    startPolling,
    stopPolling,
  }
})
