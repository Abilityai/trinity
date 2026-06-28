import { ref } from 'vue'
import axios from 'axios'
import { useAgentsStore } from '../stores/agents'
import { useNotificationsStore } from '../stores/notifications'
import { useOperatorQueueStore } from '../stores/operatorQueue'
import { useExecutionsStore } from '../stores/executions'
import { useLoopsStore } from '../stores/loops'
import { useReportsStore, useFleetReportsStore } from '../stores/reports'

const ws = ref(null)
const isConnected = ref(false)
// #306 Redis Streams reconnect replay: track the last Redis stream id we saw
// so a brief disconnect replays missed events rather than dropping them.
let lastEventId = null

// #550: mint a fresh single-use ticket immediately before each connect.
// Tickets are 30s TTL and consumed on use, so reconnects always re-fetch.
async function fetchWsTicket() {
  const { data } = await axios.post('/api/ws/ticket')
  return data.ticket
}

export function useWebSocket() {
  const agentsStore = useAgentsStore()
  const notificationsStore = useNotificationsStore()
  const operatorQueueStore = useOperatorQueueStore()
  const executionsStore = useExecutionsStore()
  const loopsStore = useLoopsStore()
  const reportsStore = useReportsStore()
  const fleetReportsStore = useFleetReportsStore()

  const connect = async () => {
    if (ws.value) return

    const token = localStorage.getItem('token')
    if (!token) {
      console.log('WebSocket: No auth token, skipping connection')
      return
    }

    let ticket
    try {
      ticket = await fetchWsTicket()
    } catch (err) {
      console.error('WebSocket: failed to mint ticket, retrying in 5s', err)
      setTimeout(connect, 5000)
      return
    }

    let wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws?ticket=${encodeURIComponent(ticket)}`
    if (lastEventId) {
      wsUrl += `&last-event-id=${encodeURIComponent(lastEventId)}`
    }
    ws.value = new WebSocket(wsUrl)

    ws.value.onopen = () => {
      isConnected.value = true
      console.log('WebSocket connected')
    }

    ws.value.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data._eid) {
          lastEventId = data._eid
        }
        handleMessage(data)
      } catch (error) {
        console.error('Failed to parse WebSocket message:', error)
      }
    }

    ws.value.onclose = (event) => {
      isConnected.value = false
      ws.value = null
      // Don't reconnect on auth rejection (code 4001)
      if (event.code === 4001) {
        console.log('WebSocket: Authentication rejected, not reconnecting')
        return
      }
      console.log('WebSocket disconnected')
      // Attempt to reconnect after 5 seconds
      setTimeout(connect, 5000)
    }

    ws.value.onerror = (error) => {
      console.error('WebSocket error:', error)
    }
  }

  const disconnect = () => {
    if (ws.value) {
      ws.value.close()
      ws.value = null
      isConnected.value = false
    }
  }

  const handleMessage = (data) => {
    // #306 reconnect replay: server signals "your last-event-id was trimmed,
    // full-refetch to rehydrate." Clear the cursor so the next reconnect
    // starts live and refetch anything event-driven.
    if (data.type === 'resync_required') {
      console.warn('[WebSocket] resync_required:', data.reason)
      lastEventId = null
      try { agentsStore.fetchAgents && agentsStore.fetchAgents() } catch (_) {}
      try { notificationsStore.fetchPendingCount && notificationsStore.fetchPendingCount() } catch (_) {}
      return
    }
    switch (data.event) {
      case 'agent_created':
        // Add to list (createAgent() no longer pushes to avoid race conditions)
        // Still check for duplicates in case of reconnection/replay
        if (!agentsStore.agents.find(a => a.name === data.data.name)) {
          agentsStore.agents.push(data.data)
        }
        break
      case 'agent_deleted':
        agentsStore.agents = agentsStore.agents.filter(a => a.name !== data.data.name)
        break
      case 'agent_started':
        agentsStore.updateAgentStatus(data.data.name, 'running')
        break
      case 'agent_stopped':
        agentsStore.updateAgentStatus(data.data.name, 'stopped')
        break
      case 'agent_notification':
        // Real-time notification from an agent
        // The WebSocket event contains: notification_id, agent_name, notification_type, title, priority, category, timestamp
        // We update the pending count and can add to list if we have full details
        notificationsStore.fetchPendingCount()
        // If we have enough data, we can add a partial notification
        if (data.notification_id && data.agent_name && data.title) {
          notificationsStore.addNotification({
            id: data.notification_id,
            agent_name: data.agent_name,
            notification_type: data.notification_type || 'info',
            title: data.title,
            priority: data.priority || 'normal',
            category: data.category || null,
            status: 'pending',
            created_at: data.timestamp || new Date().toISOString(),
            message: null,
            metadata: null,
          })
        }
        break
      default:
        // Handle events keyed by 'type' instead of 'event'
        if (data.type === 'operator_queue_new' || data.type === 'operator_queue_responded' || data.type === 'operator_queue_acknowledged' || data.type === 'operator_queue_cleared') {
          operatorQueueStore.handleWebSocketEvent(data)
        }
        // #1017: bulk dismiss by an operator — refresh badge + loaded list
        if (data.type === 'notifications_cleared') {
          notificationsStore.fetchPendingCount()
          notificationsStore.fetchNotifications()
        }
        if (data.type === 'agent_activity') {
          executionsStore.handleWebSocketEvent(data)
        }
        // #1106: loop progress events (broadcast fleet-wide, keyed by type).
        // The store filters by the agent currently shown in LoopsPanel.
        if (data.type === 'loop_run_completed' || data.type === 'loop_completed') {
          loopsStore.handleWebSocketEvent(data)
        }
        // #918: agent report thin trigger (broadcast fleet-wide, keyed by type).
        // The agent store filters by the agent on screen; the fleet store does a
        // guarded refresh. Each is a no-op when its panel isn't mounted.
        if (data.type === 'agent_report') {
          reportsStore.handleWebSocketEvent(data)
          fleetReportsStore.handleWebSocketEvent(data)
        }
        break
    }
  }

  return {
    connect,
    disconnect,
    isConnected
  }
}
