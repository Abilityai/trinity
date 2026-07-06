import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import axios from 'axios'
import { useAgentsStore } from './agents'
import { parseUTC, getTimestampMs } from '@/utils/timestamps'

export const useNetworkStore = defineStore('network', () => {
  // State
  const agents = ref([])
  const nodes = ref([])
  const collaborationEdges = ref([])  // Edges from collaboration history
  const permissionEdges = ref([])     // Edges from permissions

  // Only show permission-based edges
  const edges = computed(() => permissionEdges.value)
  const collaborationHistory = ref([])
  const lastEventTime = ref(null)
  const activeCollaborations = ref(0)
  const websocket = ref(null)
  const isConnected = ref(false)
  const intentionalDisconnect = ref(false) // Prevents reconnection after intentional disconnect
  const nodePositions = ref({}) // Store node positions in localStorage
  const historicalCollaborations = ref([]) // Persistent data from Activity Stream
  const totalCollaborationCount = ref(0)
  const timeRangeHours = ref(24) // Default to last 24 hours
  const isLoadingHistory = ref(false)
  const contextStats = ref({}) // Map of agent name -> context stats
  const executionStats = ref({}) // Map of agent name -> execution stats
  const slotStats = ref({}) // Map of agent name -> { max, active } for capacity meters
  const circuitBreakers = ref({}) // Map of agent name -> dispatch-breaker state (#526; only OPEN breakers present)
  // #47: agents with a WS-observed in-flight execution — agent name -> { since }.
  // Drives the Grid tiles' "▶ working" chip + elapsed timer between the 15s
  // context-stats polls; the poll reconciles any entry a missed event leaks.
  const workingState = ref({})
  const contextPollingInterval = ref(null) // Interval ID for context polling
  const agentRefreshInterval = ref(null) // Interval ID for agent list refresh
  const activityRefreshInterval = ref(null) // Interval ID for activity refresh (fallback)
  const websocketHeartbeatInterval = ref(null) // Interval ID for WebSocket keepalive ping
  const runningToggleLoading = ref({}) // Map of agent name -> boolean (loading state for start/stop)
  // #1266 perceived-performance: initial fleet load can take 20s+ (#1265), so the
  // Dashboard/timeline render skeletons off these flags instead of looking frozen.
  // `loading` starts true so the very first paint is a skeleton, not the empty
  // "No agents" state; `loadError` keeps a failed load distinct from an infinite
  // skeleton or a real empty fleet.
  const loading = ref(true)       // true while the initial agents/fleet fetch is in flight
  const loadError = ref(false)    // true when the last agents fetch failed
  const schedules = ref([]) // Enabled schedules for timeline markers
  // #306 Redis Streams reconnect replay: remember the last event id we saw so
  // a network blip replays missed events instead of dropping them.
  const lastEventId = ref(null)

  // View mode state (grid | graph | timeline) — default timeline, persisted.
  // trinity-enterprise#47 adds 'grid' as a third mode; legacy saved values
  // ('graph'/'timeline') keep working unchanged.
  const VIEW_MODES = ['grid', 'graph', 'timeline']
  const savedViewMode = localStorage.getItem('trinity-dashboard-view')
  const viewMode = ref(VIEW_MODES.includes(savedViewMode) ? savedViewMode : 'timeline')
  const isTimelineMode = computed(() => viewMode.value === 'timeline')
  const isPlaying = ref(false)
  const replaySpeed = ref(10) // 10x default
  const currentEventIndex = ref(0)
  const replayInterval = ref(null)
  const replayStartTime = ref(null)
  const replayElapsedMs = ref(0)

  // Computed
  const activeCollaborationCount = computed(() => {
    return edges.value.filter(edge => edge.animated).length
  })

  const lastEventTimeFormatted = computed(() => {
    if (!lastEventTime.value) return 'Never'
    const now = Date.now()
    const diff = now - getTimestampMs(lastEventTime.value)

    if (diff < 1000) return 'Just now'
    if (diff < 60000) return `${Math.floor(diff / 1000)}s ago`
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
    return `${Math.floor(diff / 3600000)}h ago`
  })

  // Replay computed properties
  const totalEvents = computed(() => historicalCollaborations.value.length)

  const totalDuration = computed(() => {
    if (historicalCollaborations.value.length < 2) return 0
    const first = parseUTC(historicalCollaborations.value[historicalCollaborations.value.length - 1].timestamp)
    const last = parseUTC(historicalCollaborations.value[0].timestamp)
    return last - first
  })

  const playbackPosition = computed(() => {
    if (totalEvents.value === 0) return 0
    return (currentEventIndex.value / totalEvents.value) * 100
  })

  const timelineStart = computed(() => {
    // Always provide a valid time range for the timeline, even with no events
    // This ensures the timeline grid is visible and ready to show live events
    const now = new Date()
    const defaultStart = new Date(now.getTime() - timeRangeHours.value * 60 * 60 * 1000)

    if (historicalCollaborations.value.length === 0) {
      return defaultStart.toISOString()
    }
    // Use oldest event or default start, whichever is earlier
    const oldestEvent = parseUTC(historicalCollaborations.value[historicalCollaborations.value.length - 1].timestamp)
    return oldestEvent < defaultStart ? oldestEvent.toISOString() : defaultStart.toISOString()
  })

  const timelineEnd = computed(() => {
    // Always use "now" as the end time in live mode so new events can appear
    return new Date().toISOString()
  })

  const currentTime = computed(() => {
    if (currentEventIndex.value >= historicalCollaborations.value.length) return timelineEnd.value
    if (currentEventIndex.value === 0) return timelineStart.value
    return historicalCollaborations.value[historicalCollaborations.value.length - currentEventIndex.value].timestamp
  })

  // Filter tags for system views
  const filterTags = ref([])
  // Filter by owner
  const filterOwner = ref(localStorage.getItem('trinity-dashboard-filter-owner') || '')

  // Actions
  function setFilterTags(tags) {
    filterTags.value = tags || []
    fetchAgents() // Refetch when filter changes
  }

  function setFilterOwner(owner) {
    filterOwner.value = owner || ''
    if (owner) {
      localStorage.setItem('trinity-dashboard-filter-owner', owner)
    } else {
      localStorage.removeItem('trinity-dashboard-filter-owner')
    }
    // Re-convert agents to nodes with owner filter applied
    const filtered = filterOwner.value
      ? agents.value.filter(a => (a.owner || null) === (filterOwner.value === '__unassigned__' ? null : filterOwner.value))
      : agents.value
    convertAgentsToNodes(filtered)
  }

  async function fetchAgents() {
    loading.value = true
    loadError.value = false
    try {
      const params = {}
      if (filterTags.value.length > 0) {
        params.tags = filterTags.value.join(',')
      }
      const response = await axios.get('/api/agents', { params })
      agents.value = response.data
      // Apply client-side owner filter before converting to nodes
      const filtered = filterOwner.value
        ? response.data.filter(a => (a.owner || null) === (filterOwner.value === '__unassigned__' ? null : filterOwner.value))
        : response.data
      convertAgentsToNodes(filtered)
      await fetchPermissionEdges()
    } catch (error) {
      console.error('Failed to fetch agents:', error)
      loadError.value = true
    } finally {
      // Always clear loading so a failed/empty load shows its own state, never an
      // infinite skeleton (#1266).
      loading.value = false
    }
  }

  // Returns [a, b] sorted alphabetically — used to deduplicate bidirectional edges
  function getCanonicalEdgePair(x, y) {
    return x < y ? [x, y] : [y, x]
  }

  async function fetchPermissionEdges() {
    try {
      const token = localStorage.getItem('token')
      if (!token) return

      // Single bulk API call instead of N per-agent calls (#359)
      const response = await axios.get('/api/agents/permissions-edges', {
        headers: { Authorization: `Bearer ${token}` }
      })
      const edges = response.data.edges || []

      // Collect canonical pairs to avoid two overlapping edges for bidirectional permissions
      const pairMap = new Map()

      edges.forEach(({ source, target }) => {
        const sourceExists = nodes.value.some(n => n.id === source)
        const targetExists = nodes.value.some(n => n.id === target)
        if (!sourceExists || !targetExists) return

        const [a, b] = getCanonicalEdgePair(source, target)
        const key = `perm-${a}-${b}`
        if (!pairMap.has(key)) {
          pairMap.set(key, { source: a, target: b, hasForward: false, hasReverse: false })
        }
        const pair = pairMap.get(key)
        if (source === a) pair.hasForward = true
        else pair.hasReverse = true
      })

      const newEdges = []
      pairMap.forEach((pair, key) => {
        const edge = {
          id: key,
          source: pair.source,
          target: pair.target,
          type: 'smoothstep',
          animated: false,
          style: {
            stroke: '#94a3b8',
            strokeWidth: 1.5,
            strokeDasharray: '5,3',
            opacity: 0.7
          },
          data: { type: 'permission', hasForward: pair.hasForward, hasReverse: pair.hasReverse }
        }
        if (pair.hasForward) edge.markerEnd = { type: 'arrowclosed', color: '#94a3b8', width: 12, height: 12 }
        if (pair.hasReverse) edge.markerStart = { type: 'arrowclosed', color: '#94a3b8', width: 12, height: 12 }
        newEdges.push(edge)
      })

      permissionEdges.value = newEdges
    } catch (error) {
      console.error('[Permissions] Failed to fetch permission edges:', error)
    }
  }

  async function fetchHistoricalCollaborations(hours = null) {
    isLoadingHistory.value = true

    try {
      const hoursToQuery = hours || timeRangeHours.value

      // Calculate start time (X hours ago)
      const startTime = new Date()
      startTime.setHours(startTime.getHours() - hoursToQuery)

      // Fetch ALL execution types (not just collaborations)
      const params = {
        activity_types: 'agent_collaboration,schedule_start,schedule_end,chat_start,chat_end,self_task',
        start_time: startTime.toISOString(),
        limit: 500
      }

      const response = await axios.get('/api/activities/timeline', { params })

      // Parse activities into timeline events
      const events = response.data.activities
        .filter(activity => {
          // Skip activities without details
          if (!activity.details) return false

          // Filter out regular user chats (keep automated executions only)
          if (activity.activity_type && activity.activity_type.startsWith('chat_')) {
            const details = typeof activity.details === 'string'
              ? JSON.parse(activity.details)
              : activity.details || {}

            // Keep: agent-initiated, mcp-triggered, schedule-triggered, or manual tasks (parallel_mode)
            // Skip: regular user chat sessions (triggered_by='user' without parallel_mode)
            if (activity.triggered_by === 'user' && !details.parallel_mode) {
              return false
            }
          }

          return true
        })
        .map(activity => {
          try {
            const details = typeof activity.details === 'string'
              ? JSON.parse(activity.details)
              : activity.details

            // For execution events (chat_start, schedule_start): use agent_name (the executor)
            // For collaboration events: use details.source_agent (the caller) for arrows
            const isCollaboration = activity.activity_type === 'agent_collaboration'

            return {
              source_agent: isCollaboration ? (details.source_agent || activity.agent_name) : activity.agent_name,
              target_agent: details.target_agent || null,  // null for non-collaboration events
              timestamp: activity.started_at || activity.created_at,
              activity_id: activity.id,
              // Execution ID for navigation to Execution Detail page
              // Priority: top-level related_execution_id (database ID) > details fallbacks
              execution_id: activity.related_execution_id || details.execution_id || details.related_execution_id || null,
              status: activity.activity_state,
              duration_ms: activity.duration_ms,
              // Activity type tracking for color coding
              activity_type: activity.activity_type,
              triggered_by: activity.triggered_by,
              schedule_name: details.schedule_name || null,
              error: activity.error,
              // #514: previews surfaced on timeline hover. Backend stores
              // first 100 chars of command in details.message_preview at
              // activity start; first 200 chars of sanitized response in
              // details.response_preview on completion.
              command_preview: details.message_preview || '',
              response_preview: details.response_preview || ''
            }
          } catch (e) {
            console.warn('Failed to parse activity details:', e)
            return null
          }
        })
        .filter(e => e !== null)

      historicalCollaborations.value = events
      totalCollaborationCount.value = events.length

      // Create initial inactive edges from collaboration events only
      const collaborationEvents = events.filter(e => e.target_agent)
      createHistoricalEdges(collaborationEvents)

      console.log(`[Collaboration] Loaded ${events.length} historical events (${collaborationEvents.length} collaborations) from last ${hoursToQuery}h`)

    } catch (error) {
      console.error('Failed to fetch historical collaborations:', error)
      // Don't fail silently - show user feedback
      if (error.response?.status === 401) {
        console.error('Authentication required for historical data')
      }
    } finally {
      isLoadingHistory.value = false
    }
  }

  function createHistoricalEdges(collaborations) {
    // Group by canonical pair to avoid two overlapping edges for bidirectional communication
    const edgeMap = new Map()

    collaborations.forEach(collab => {
      const [a, b] = getCanonicalEdgePair(collab.source_agent, collab.target_agent)
      const edgeId = `e-${a}-${b}`

      if (!edgeMap.has(edgeId)) {
        edgeMap.set(edgeId, {
          id: edgeId,
          source: a,
          target: b,
          count: 1,
          lastTimestamp: collab.timestamp,
          timestamps: [collab.timestamp],
          hasForward: false,
          hasReverse: false
        })
      } else {
        const existing = edgeMap.get(edgeId)
        existing.count++
        existing.timestamps.push(collab.timestamp)
        if (getTimestampMs(collab.timestamp) > getTimestampMs(existing.lastTimestamp)) {
          existing.lastTimestamp = collab.timestamp
        }
      }
      const existing = edgeMap.get(edgeId)
      if (collab.source_agent === a) existing.hasForward = true
      else existing.hasReverse = true
    })

    // Clear existing collaboration edges before rebuilding
    collaborationEdges.value = []

    // Create inactive edges for all historical collaborations
    edgeMap.forEach((edgeData, edgeId) => {
      // Only add if both nodes exist
      const sourceExists = nodes.value.some(n => n.id === edgeData.source)
      const targetExists = nodes.value.some(n => n.id === edgeData.target)

      if (sourceExists && targetExists) {
        const edge = {
          id: edgeId,
          source: edgeData.source,
          target: edgeData.target,
          type: 'smoothstep',
          animated: false,
          className: 'collaboration-edge-inactive',
          style: {
            stroke: '#cbd5e1',
            strokeWidth: 2,
            opacity: 0.5,
            transition: 'all 0.5s ease-in-out'
          },
          label: edgeData.count > 1 ? `${edgeData.count}x` : undefined,
          labelStyle: {
            fontSize: '10px',
            fill: '#64748b'
          },
          data: {
            collaborationCount: edgeData.count,
            lastTimestamp: edgeData.lastTimestamp,
            timestamps: edgeData.timestamps,
            hasForward: edgeData.hasForward,
            hasReverse: edgeData.hasReverse
          }
        }
        if (edgeData.hasForward) edge.markerEnd = { type: 'arrowclosed', color: '#cbd5e1', width: 15, height: 15 }
        if (edgeData.hasReverse) edge.markerStart = { type: 'arrowclosed', color: '#cbd5e1', width: 15, height: 15 }
        collaborationEdges.value.push(edge)
      }
    })
  }

  function convertAgentsToNodes(agentList) {
    // Load saved positions from localStorage
    const savedPositions = loadNodePositions()

    // Separate system agent from regular agents
    const systemAgent = agentList.find(a => a.is_system)
    const regularAgents = agentList.filter(a => !a.is_system)

    // Group agents by their primary tag (first tag) for layout
    const tagGroups = new Map()
    const untaggedAgents = []

    regularAgents.forEach(agent => {
      const tags = agent.tags || []
      if (tags.length > 0) {
        const primaryTag = tags[0] // Use first tag for grouping
        if (!tagGroups.has(primaryTag)) {
          tagGroups.set(primaryTag, [])
        }
        tagGroups.get(primaryTag).push(agent)
      } else {
        untaggedAgents.push(agent)
      }
    })

    // Layout configuration - equidistant spacing within groups, more between groups
    const nodeWidth = 320
    const nodeHeight = 270           // Actual rendered card height (~180px min + button + padding)
    const withinGroupPadding = 40    // Equal spacing within a group (both horizontal and vertical)
    const betweenGroupPadding = 100  // Extra space between different groups
    const offsetX = 100
    const offsetY = systemAgent ? 280 : 100

    const result = []

    // Sort tags by group size (largest first) for better layout
    const sortedTags = Array.from(tagGroups.keys()).sort((a, b) =>
      tagGroups.get(b).length - tagGroups.get(a).length
    )

    // Calculate group dimensions and arrange in a smart grid
    const groupLayouts = []
    sortedTags.forEach(tag => {
      const agents = tagGroups.get(tag)
      // Arrange group in compact grid (prefer square-ish)
      const cols = Math.ceil(Math.sqrt(agents.length))
      const rows = Math.ceil(agents.length / cols)
      groupLayouts.push({
        tag,
        agents,
        cols,
        rows,
        width: cols * (nodeWidth + withinGroupPadding) - withinGroupPadding,
        height: rows * (nodeHeight + withinGroupPadding) - withinGroupPadding
      })
    })

    // Add untagged as a "group" if any
    if (untaggedAgents.length > 0) {
      const cols = Math.min(Math.ceil(Math.sqrt(untaggedAgents.length)), 5)
      const rows = Math.ceil(untaggedAgents.length / cols)
      groupLayouts.push({
        tag: null,
        agents: untaggedAgents,
        cols,
        rows,
        width: cols * (nodeWidth + withinGroupPadding) - withinGroupPadding,
        height: rows * (nodeHeight + withinGroupPadding) - withinGroupPadding
      })
    }

    // Bin-pack groups into rows to minimize total width
    // Simple greedy: place groups left-to-right, wrap when exceeds max width
    const maxRowWidth = 1800
    const groupPositions = []
    let currentX = offsetX
    let currentY = offsetY
    let rowHeight = 0

    groupLayouts.forEach(group => {
      // Check if we need to wrap to next row
      if (currentX > offsetX && currentX + group.width > maxRowWidth) {
        currentX = offsetX
        currentY += rowHeight + betweenGroupPadding
        rowHeight = 0
      }

      groupPositions.push({ ...group, x: currentX, y: currentY })
      currentX += group.width + betweenGroupPadding
      rowHeight = Math.max(rowHeight, group.height)
    })

    // Add system agent centered above all groups
    if (systemAgent) {
      const totalWidth = Math.max(...groupPositions.map(g => g.x + g.width)) - offsetX
      const systemDefaultPosition = {
        x: offsetX + totalWidth / 2 - 200,
        y: 30
      }
      result.push({
        id: systemAgent.name,
        type: 'system-agent',
        data: {
          label: systemAgent.name,
          status: systemAgent.status,
          type: systemAgent.type || 'system',
          owner: systemAgent.owner,
          runtime: systemAgent.runtime || 'claude-code',
          githubRepo: systemAgent.github_repo || null,
          is_system: true,
          autonomy_enabled: false,
          activityState: systemAgent.status === 'running' ? 'idle' : 'offline',
          memoryLimit: systemAgent.memory_limit || null,
          cpuLimit: systemAgent.cpu_limit || null,
          tags: systemAgent.tags || [],
          avatarUrl: systemAgent.avatar_url || null
        },
        position: savedPositions[systemAgent.name] || systemDefaultPosition,
        draggable: true
      })
    }

    // Place agents within each group
    groupPositions.forEach(group => {
      group.agents.forEach((agent, index) => {
        const col = index % group.cols
        const row = Math.floor(index / group.cols)

        const defaultPosition = {
          x: group.x + col * (nodeWidth + withinGroupPadding),
          y: group.y + row * (nodeHeight + withinGroupPadding)
        }

        result.push({
          id: agent.name,
          type: 'agent',
          data: {
            label: agent.name,
            status: agent.status,
            owner: agent.owner,
            runtime: agent.runtime || 'claude-code',
            githubRepo: agent.github_repo || null,
            is_system: false,
            autonomy_enabled: agent.autonomy_enabled || false,
            activityState: agent.status === 'running' ? 'idle' : 'offline',
            memoryLimit: agent.memory_limit || null,
            cpuLimit: agent.cpu_limit || null,
            tags: agent.tags || [],
            avatarUrl: agent.avatar_url || null
          },
          position: savedPositions[agent.name] || defaultPosition,
          draggable: true
        })
      })
    })

    nodes.value = result
  }

  async function connectWebSocket() {
    // Reset intentional disconnect flag when intentionally connecting
    intentionalDisconnect.value = false

    const token = localStorage.getItem('token')
    if (!token) {
      console.log('[Collaboration] No auth token, skipping WebSocket connection')
      return
    }

    // Prevent duplicate connections
    if (websocket.value?.readyState === WebSocket.OPEN) {
      console.log('[Collaboration] WebSocket already connected')
      return
    }

    // #550: mint single-use ticket; JWT never enters the WebSocket URL.
    let ticket
    try {
      const { data } = await axios.post('/api/ws/ticket')
      ticket = data.ticket
    } catch (err) {
      console.error('[Collaboration] failed to mint WS ticket', err)
      return
    }

    let wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws?ticket=${encodeURIComponent(ticket)}`
    if (lastEventId.value) {
      wsUrl += `&last-event-id=${encodeURIComponent(lastEventId.value)}`
    }

    try {
      websocket.value = new WebSocket(wsUrl)

      websocket.value.onopen = () => {
        console.log('[Collaboration] WebSocket connected')
        isConnected.value = true

        // Start heartbeat to keep connection alive (prevents idle timeout)
        startWebSocketHeartbeat()
      }

      websocket.value.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          if (data._eid) {
            lastEventId.value = data._eid
          }

          if (data.type === 'resync_required') {
            // #306: stream cursor was trimmed (or reject), reload authoritative state.
            console.warn('[Collaboration] resync_required:', data.reason)
            lastEventId.value = null
            try { fetchAgents() } catch (_) {}
            try { fetchHistoricalCollaborations() } catch (_) {}
            return
          }
          if (data.type === 'agent_collaboration') {
            handleCollaborationEvent(data)
          } else if (data.type === 'agent_status') {
            handleAgentStatusChange(data)
          } else if (data.type === 'agent_started' || data.type === 'agent_stopped') {
            // #47: the backend broadcasts these on start/stop — sync both
            // render surfaces so another session's toggle propagates live.
            handleAgentStatusChange({
              agent_name: data.name,
              status: data.type === 'agent_started' ? 'running' : 'stopped'
            })
          } else if (data.type === 'agent_deleted') {
            handleAgentDeleted(data)
          } else if (data.type === 'agent_activity') {
            handleActivityStatusChange(data)
          } else if (data.type === 'pong') {
            // Heartbeat response received - connection is alive
            console.log('[Collaboration] Heartbeat pong received')
          }
        } catch (error) {
          console.error('[Collaboration] Failed to parse WebSocket message:', error)
        }
      }

      websocket.value.onerror = (error) => {
        console.error('[Collaboration] WebSocket error:', error)
        isConnected.value = false
      }

      websocket.value.onclose = (event) => {
        console.log('[Collaboration] WebSocket disconnected')
        isConnected.value = false

        // Stop heartbeat
        stopWebSocketHeartbeat()

        // Don't reconnect on auth rejection (code 4001)
        if (event.code === 4001) {
          console.log('[Collaboration] Authentication rejected, not reconnecting')
          return
        }

        // Only reconnect if this was NOT an intentional disconnect
        if (!intentionalDisconnect.value) {
          setTimeout(() => {
            if (!isConnected.value && !intentionalDisconnect.value) {
              console.log('[Collaboration] Attempting to reconnect...')
              connectWebSocket()
            }
          }, 5000)
        }
      }
    } catch (error) {
      console.error('[Collaboration] Failed to connect WebSocket:', error)
    }
  }

  function handleCollaborationEvent(event) {
    console.log('[Collaboration] Received event:', event)

    // Add to in-memory history (for real-time feed)
    collaborationHistory.value.unshift(event)
    if (collaborationHistory.value.length > 100) {
      collaborationHistory.value = collaborationHistory.value.slice(0, 100)
    }

    // Add to historical collaborations (persistent)
    historicalCollaborations.value.unshift({
      source_agent: event.source_agent,
      target_agent: event.target_agent,
      timestamp: event.timestamp,
      status: 'completed'
    })

    // Increment total count
    totalCollaborationCount.value++

    // Update last event time
    lastEventTime.value = event.timestamp

    // Animate edge
    animateEdge(event.source_agent, event.target_agent)
  }

  function handleAgentStatusChange(event) {
    // Update node status
    const node = nodes.value.find(n => n.id === event.agent_name)
    if (node) {
      node.data.status = event.status
    }

    // Update agent in list
    const agent = agents.value.find(a => a.name === event.agent_name)
    if (agent) {
      agent.status = event.status
    }
  }

  function handleAgentDeleted(event) {
    // Remove node
    nodes.value = nodes.value.filter(n => n.id !== event.agent_name)

    // Remove collaboration edges connected to this agent
    collaborationEdges.value = collaborationEdges.value.filter(e =>
      e.source !== event.agent_name && e.target !== event.agent_name
    )

    // Remove permission edges connected to this agent
    permissionEdges.value = permissionEdges.value.filter(e =>
      e.source !== event.agent_name && e.target !== event.agent_name
    )

    // Remove from agents list
    agents.value = agents.value.filter(a => a.name !== event.agent_name)
  }

  // #47: keep the WS-driven working map current. `chat_start`/`schedule_start`
  // beginning marks the agent working; the same activity reaching a terminal
  // state — or the matching `_end` activity — clears it. Parallel executions
  // per agent are approximated (last event wins); the 15s context-stats poll
  // reconciles.
  function _updateWorkingState(event) {
    const name = event.agent_name
    if (!name) return
    const type = event.activity_type || ''
    const state = event.activity_state
    if ((type === 'chat_start' || type === 'schedule_start') && state === 'started') {
      workingState.value = {
        ...workingState.value,
        [name]: { since: event.timestamp || new Date().toISOString() }
      }
    } else if (
      type === 'chat_end' ||
      type === 'schedule_end' ||
      ((type === 'chat_start' || type === 'schedule_start') && state !== 'started')
    ) {
      if (workingState.value[name]) {
        const next = { ...workingState.value }
        delete next[name]
        workingState.value = next
      }
    }
  }

  // Handle activity status changes (completion, failure) from WebSocket
  function handleActivityStatusChange(event) {
    _updateWorkingState(event)
    // Update activity in historicalCollaborations
    const activity = historicalCollaborations.value.find(
      a => a.activity_id === event.activity_id
    )

    if (activity) {
      // Update status - this is the key fix for timeline bars not completing
      activity.status = event.activity_state
      activity.duration_ms = event.details?.duration_ms || activity.duration_ms
      activity.error = event.error

      console.log(`[Collaboration] Activity ${event.activity_id} updated to ${event.activity_state}`)

      // Trigger Vue reactivity
      historicalCollaborations.value = [...historicalCollaborations.value]
    } else if (event.activity_state === 'started') {
      // New activity started - add to list
      const newActivity = {
        source_agent: event.agent_name,
        target_agent: event.details?.target_agent || null,
        timestamp: event.timestamp,
        activity_id: event.activity_id,
        execution_id: event.details?.execution_id || event.details?.related_execution_id || null,
        status: event.activity_state,
        duration_ms: null,
        activity_type: event.activity_type,
        triggered_by: event.details?.triggered_by || 'system',
        schedule_name: event.details?.schedule_name || null,
        error: null
      }

      historicalCollaborations.value.unshift(newActivity)
      totalCollaborationCount.value++

      console.log(`[Collaboration] New activity ${event.activity_id} added (${event.activity_type})`)
    }
  }

  // WebSocket heartbeat to prevent idle timeout (sends ping every 30 seconds)
  function startWebSocketHeartbeat() {
    stopWebSocketHeartbeat() // Clear any existing heartbeat

    websocketHeartbeatInterval.value = setInterval(() => {
      if (websocket.value?.readyState === WebSocket.OPEN) {
        // Send a ping message - server should respond with pong (or echo)
        // If the server doesn't respond, the connection will eventually timeout
        // and trigger onclose, which will reconnect
        try {
          websocket.value.send(JSON.stringify({ type: 'ping' }))
          console.log('[Collaboration] Heartbeat ping sent')
        } catch (error) {
          console.error('[Collaboration] Failed to send heartbeat:', error)
          // Force reconnection if send fails
          isConnected.value = false
          websocket.value?.close()
        }
      } else if (!intentionalDisconnect.value) {
        // WebSocket not open and we didn't intentionally disconnect - try reconnecting
        console.log('[Collaboration] WebSocket not open, attempting reconnect...')
        isConnected.value = false
        stopWebSocketHeartbeat()
        connectWebSocket()
      }
    }, 30000) // Every 30 seconds

    console.log('[Collaboration] WebSocket heartbeat started (every 30s)')
  }

  function stopWebSocketHeartbeat() {
    if (websocketHeartbeatInterval.value) {
      clearInterval(websocketHeartbeatInterval.value)
      websocketHeartbeatInterval.value = null
      console.log('[Collaboration] WebSocket heartbeat stopped')
    }
  }

  // Track active edge timeouts for extending visibility
  const edgeTimeouts = ref({})

  function animateEdge(sourceId, targetId, extendedDuration = false) {
    const [a, b] = getCanonicalEdgePair(sourceId, targetId)
    const edgeId = `e-${a}-${b}`
    const isForward = sourceId === a  // a→b direction

    // Clear any existing timeout for this edge (edge stays active longer if retriggered)
    if (edgeTimeouts.value[edgeId]) {
      clearTimeout(edgeTimeouts.value[edgeId])
      delete edgeTimeouts.value[edgeId]
    }

    // Check if edge already exists in collaborationEdges (the source of truth)
    let edge = collaborationEdges.value.find(e => e.id === edgeId)

    if (!edge) {
      // Create new edge with fade-in effect
      edge = {
        id: edgeId,
        source: a,
        target: b,
        type: 'smoothstep',
        animated: true,
        className: 'collaboration-edge-active',
        style: {
          stroke: 'url(#collaboration-gradient)',
          strokeWidth: 3,
          opacity: 0,
          transition: 'all 0.5s ease-in-out',
          filter: 'drop-shadow(0 0 4px rgba(6, 182, 212, 0.5))'
        },
        data: {
          gradientId: edgeId,
          collaborationCount: 1,
          timestamps: [new Date().toISOString()],
          targetAgent: targetId,
          hasForward: isForward,
          hasReverse: !isForward
        }
      }
      if (isForward) edge.markerEnd = { type: 'arrowclosed', color: '#06b6d4', width: 18, height: 18 }
      else edge.markerStart = { type: 'arrowclosed', color: '#06b6d4', width: 18, height: 18 }

      collaborationEdges.value.push(edge)

      // Trigger fade-in animation
      setTimeout(() => {
        const e = collaborationEdges.value.find(e => e.id === edgeId)
        if (e) e.style.opacity = 1
      }, 50)
    } else {
      // Update existing edge to active state with glow
      edge.animated = true
      edge.type = 'smoothstep'
      edge.className = 'collaboration-edge-active'
      edge.style = {
        stroke: 'url(#collaboration-gradient)',
        strokeWidth: 3,
        opacity: 1,
        transition: 'all 0.5s ease-in-out',
        filter: 'drop-shadow(0 0 4px rgba(6, 182, 212, 0.5))'
      }

      // Update direction tracking and set active arrowhead for this direction
      if (edge.data) {
        if (isForward) edge.data.hasForward = true
        else edge.data.hasReverse = true
      }
      if (isForward) edge.markerEnd = { type: 'arrowclosed', color: '#06b6d4', width: 18, height: 18 }
      else edge.markerStart = { type: 'arrowclosed', color: '#06b6d4', width: 18, height: 18 }

      // Increment collaboration count for this edge
      if (edge.data) {
        edge.data.collaborationCount = (edge.data.collaborationCount || 0) + 1
        edge.data.timestamps = edge.data.timestamps || []
        edge.data.timestamps.push(new Date().toISOString())
        edge.data.targetAgent = targetId

        // Update label if count > 1
        if (edge.data.collaborationCount > 1) {
          edge.label = `${edge.data.collaborationCount}x`
          edge.labelStyle = {
            fontSize: '10px',
            fill: '#06b6d4',
            fontWeight: 'bold'
          }
        }
      }
    }

    // Increment active collaborations
    activeCollaborations.value++

    // Trigger Vue reactivity for nested object changes
    collaborationEdges.value = [...collaborationEdges.value]

    // Extended duration: 8 seconds (time for target agent to process and update context)
    // Standard duration: 3 seconds
    const fadeDelay = extendedDuration ? 8000 : 6000

    // Set timeout to fade edge back to inactive
    edgeTimeouts.value[edgeId] = setTimeout(() => {
      fadeEdgeAnimation(edgeId)
      delete edgeTimeouts.value[edgeId]
    }, fadeDelay)
  }

  function fadeEdgeAnimation(edgeId) {
    const edge = collaborationEdges.value.find(e => e.id === edgeId)
    if (edge) {
      // First fade the glow
      edge.style = {
        ...edge.style,
        filter: 'drop-shadow(0 0 2px rgba(148, 163, 184, 0.3))',
        strokeWidth: 2.5
      }

      // Trigger Vue reactivity for nested object changes
      collaborationEdges.value = [...collaborationEdges.value]

      // Then fade to inactive state after 800ms
      setTimeout(() => {
        clearEdgeAnimation(edgeId)
      }, 800)
    }
  }

  function clearEdgeAnimation(edgeId) {
    const edge = collaborationEdges.value.find(e => e.id === edgeId)
    if (edge) {
      edge.animated = false
      edge.type = 'smoothstep'
      edge.className = 'collaboration-edge-inactive'
      edge.style = {
        stroke: '#cbd5e1',
        strokeWidth: 2,
        opacity: 0.5,
        transition: 'all 0.8s ease-out',
        filter: 'none'
      }
      // Restore direction-aware arrowheads
      edge.markerEnd = edge.data?.hasForward ? { type: 'arrowclosed', color: '#cbd5e1', width: 15, height: 15 } : undefined
      edge.markerStart = edge.data?.hasReverse ? { type: 'arrowclosed', color: '#cbd5e1', width: 15, height: 15 } : undefined

      // Keep the count label but make it gray
      if (edge.data && edge.data.collaborationCount > 1) {
        edge.label = `${edge.data.collaborationCount}x`
        edge.labelStyle = {
          fontSize: '10px',
          fill: '#64748b'
        }
      }

      // Trigger Vue reactivity for nested object changes
      collaborationEdges.value = [...collaborationEdges.value]

      // Decrement active collaborations
      activeCollaborations.value = Math.max(0, activeCollaborations.value - 1)
    }
  }

  function saveNodePositions() {
    const positions = {}
    nodes.value.forEach(node => {
      positions[node.id] = node.position
    })
    localStorage.setItem('trinity-collaboration-node-positions', JSON.stringify(positions))
  }

  function loadNodePositions() {
    try {
      const saved = localStorage.getItem('trinity-collaboration-node-positions')
      return saved ? JSON.parse(saved) : {}
    } catch (error) {
      console.error('Failed to load node positions:', error)
      return {}
    }
  }

  function resetNodePositions() {
    localStorage.removeItem('trinity-collaboration-node-positions')
    fetchAgents() // Reload with default positions
  }

  function disconnectWebSocket() {
    // Set flag BEFORE closing to prevent reconnection
    intentionalDisconnect.value = true

    // Stop heartbeat
    stopWebSocketHeartbeat()

    if (websocket.value) {
      websocket.value.close()
      websocket.value = null
      isConnected.value = false
    }
  }

  function onNodeDragStop(event) {
    // Save positions when user stops dragging
    saveNodePositions()
  }

  // Fetch context stats from backend
  async function fetchContextStats() {
    try {
      const response = await axios.get('/api/agents/context-stats')
      const agentStats = response.data.agents

      // Update context stats map
      const newStats = {}
      agentStats.forEach(stat => {
        newStats[stat.name] = {
          contextPercent: stat.contextPercent,
          contextUsed: stat.contextUsed,
          contextMax: stat.contextMax,
          activityState: stat.activityState,
          lastActivityTime: stat.lastActivityTime
        }
      })
      contextStats.value = newStats

      // #47: reconcile the WS working map — drop entries for agents the
      // authoritative poll no longer reports active (missed end events).
      // Entries younger than one poll period are spared: a response computed
      // before the execution started must not evict a fresh WS entry.
      const staleCutoff = Date.now() - 20000
      const leaked = Object.keys(workingState.value).filter(
        name =>
          newStats[name] &&
          newStats[name].activityState !== 'active' &&
          new Date(workingState.value[name].since || 0).getTime() < staleCutoff
      )
      if (leaked.length > 0) {
        const next = { ...workingState.value }
        leaked.forEach(name => delete next[name])
        workingState.value = next
      }

      // Update node data with new stats
      nodes.value.forEach(node => {
        const stats = newStats[node.id]
        if (stats) {
          node.data = {
            ...node.data,
            contextPercent: stats.contextPercent,
            contextUsed: stats.contextUsed,
            contextMax: stats.contextMax,
            activityState: stats.activityState,
            lastActivityTime: stats.lastActivityTime
          }
        }
      })

      console.log('[Collaboration] Context stats updated')
    } catch (error) {
      console.error('Failed to fetch context stats:', error)
    }
  }

  // Fetch execution stats from backend
  async function fetchExecutionStats() {
    try {
      const response = await axios.get('/api/agents/execution-stats', {
        params: { include_7d: true }
      })
      const agentStats = response.data.agents

      // Update execution stats map
      const newStats = {}
      agentStats.forEach(stat => {
        newStats[stat.name] = {
          taskCount: stat.task_count_24h,
          successCount: stat.success_count,
          failedCount: stat.failed_count,
          runningCount: stat.running_count,
          successRate: stat.success_rate,
          totalCost: stat.total_cost,
          lastExecutionAt: stat.last_execution_at,
          schedulesTotal: stat.schedules_total || 0,
          schedulesEnabled: stat.schedules_enabled || 0,
          taskCount7d: stat.task_count_7d || 0,
          successRate7d: stat.success_rate_7d || 0
        }
      })
      executionStats.value = newStats

      // Update node data with execution stats
      nodes.value.forEach(node => {
        const stats = newStats[node.id]
        if (stats) {
          node.data = {
            ...node.data,
            executionStats: stats
          }
        }
      })

      console.log('[Collaboration] Execution stats updated')
    } catch (error) {
      console.error('Failed to fetch execution stats:', error)
    }
  }

  // Fetch slot stats for capacity meters
  async function fetchSlotStats() {
    try {
      const response = await axios.get('/api/agents/slots')
      const agentsMap = response.data.agents || {}
      // #526: per-agent dispatch-breaker state — only OPEN breakers are present.
      const circuitMap = response.data.circuit_breakers || {}

      const newStats = {}
      Object.entries(agentsMap).forEach(([name, stat]) => {
        newStats[name] = {
          max: stat.max || 1,
          active: stat.active || 0
        }
      })
      slotStats.value = newStats
      // #47: expose the breaker map directly for non-VueFlow consumers
      // (the Grid tiles read it by agent name).
      circuitBreakers.value = circuitMap

      // Thread slot stats + circuit-breaker state onto node data. Setting
      // circuitBreaker to null when absent clears a badge once the breaker
      // recovers (every agent with capacity stats appears here).
      nodes.value.forEach(node => {
        const stats = newStats[node.id]
        if (stats) {
          node.data = {
            ...node.data,
            slotStats: stats,
            circuitBreaker: circuitMap[node.id] || null
          }
        }
      })
    } catch (error) {
      // Slots endpoint may not exist yet - fail silently
      if (error.response?.status !== 404) {
        console.error('Failed to fetch slot stats:', error)
      }
    }
  }

  // PERF-269: Skip polling when tab is backgrounded
  function _pollIfVisible(fn) {
    if (!document.hidden) fn()
  }

  // Start polling context and execution stats every 15 seconds (PERF-269: was 5s)
  function startContextPolling() {
    if (contextPollingInterval.value) {
      clearInterval(contextPollingInterval.value)
    }

    // Fetch immediately
    fetchContextStats()
    fetchExecutionStats()
    fetchSlotStats()

    // Then poll every 15 seconds (PERF-269: reduced from 5s)
    contextPollingInterval.value = setInterval(() => {
      _pollIfVisible(() => {
        fetchContextStats()
        fetchExecutionStats()
        fetchSlotStats()
      })
    }, 15000)

    console.log('[Collaboration] Started context polling (every 15s)')
  }

  // Stop polling context stats
  function stopContextPolling() {
    if (contextPollingInterval.value) {
      clearInterval(contextPollingInterval.value)
      contextPollingInterval.value = null
      console.log('[Collaboration] Stopped context polling')
    }
  }

  // Start polling agent list every 30 seconds (PERF-269: was 10s)
  function startAgentRefresh() {
    if (agentRefreshInterval.value) {
      clearInterval(agentRefreshInterval.value)
    }

    // Poll every 30 seconds (PERF-269: reduced from 10s)
    agentRefreshInterval.value = setInterval(async () => {
      if (document.hidden) return  // PERF-269: skip when tab backgrounded
      try {
        // Respect filter tags when refreshing (ORG-001 fix)
        const params = {}
        if (filterTags.value.length > 0) {
          params.tags = filterTags.value.join(',')
        }
        const response = await axios.get('/api/agents', { params })
        const newAgents = response.data

        // Check if agent list has changed (new agents or deleted agents)
        const currentAgentNames = new Set(agents.value.map(a => a.name))
        const newAgentNames = new Set(newAgents.map(a => a.name))

        const hasChanges =
          newAgents.length !== agents.value.length ||
          !newAgents.every(a => currentAgentNames.has(a.name))

        if (hasChanges) {
          console.log('[Collaboration] Agent list changed, refreshing...')
          agents.value = newAgents
          convertAgentsToNodes(newAgents)
        }
      } catch (error) {
        console.error('[Collaboration] Failed to refresh agents:', error)
      }
    }, 30000)

    console.log('[Collaboration] Started agent refresh polling (every 30s)')
  }

  // Stop polling agent list
  function stopAgentRefresh() {
    if (agentRefreshInterval.value) {
      clearInterval(agentRefreshInterval.value)
      agentRefreshInterval.value = null
      console.log('[Collaboration] Stopped agent refresh polling')
    }
  }

  // Start polling activity data as fallback (in case WebSocket misses events)
  function startActivityRefresh() {
    if (activityRefreshInterval.value) {
      clearInterval(activityRefreshInterval.value)
    }

    // Poll every 60 seconds as fallback (WebSocket should handle most updates)
    activityRefreshInterval.value = setInterval(() => {
      if (document.hidden) return  // PERF-269: skip when tab backgrounded
      // Only refresh if we're in timeline mode (to avoid unnecessary API calls)
      if (isTimelineMode.value) {
        console.log('[Collaboration] Activity refresh polling triggered')
        fetchHistoricalCollaborations()
      }
    }, 60000) // Every 60 seconds

    console.log('[Collaboration] Started activity refresh polling (every 60s)')
  }

  // Stop polling activity data
  function stopActivityRefresh() {
    if (activityRefreshInterval.value) {
      clearInterval(activityRefreshInterval.value)
      activityRefreshInterval.value = null
      console.log('[Collaboration] Stopped activity refresh polling')
    }
  }

  // View Mode Functions (grid | graph | timeline)
  function setViewMode(mode) {
    if (!VIEW_MODES.includes(mode)) mode = 'graph'
    viewMode.value = mode
    localStorage.setItem('trinity-dashboard-view', mode)

    // Keep WebSocket and shared stats polling active in ALL views for live
    // updates; only the timeline's activity-refresh fallback is mode-scoped.
    stopReplay()
    if (mode === 'timeline') {
      startActivityRefresh()
      console.log('[Collaboration] Switched to Timeline view (live mode)')
    } else {
      stopActivityRefresh()
      console.log(`[Collaboration] Switched to ${mode === 'grid' ? 'Grid' : 'Graph'} view`)
    }
  }

  // Legacy function for backwards compatibility
  function setReplayMode(mode) {
    setViewMode(mode ? 'timeline' : 'graph')
  }

  function startReplay() {
    if (historicalCollaborations.value.length === 0) {
      console.warn('[Collaboration] No historical data to replay')
      return
    }

    isPlaying.value = true
    replayStartTime.value = Date.now()

    // Start from current index or beginning
    if (currentEventIndex.value === 0) {
      resetAllEdges()
      // Store base context values for each agent (use current or default to 10%)
      nodes.value.forEach(node => {
        if (node.data.status === 'running') {
          baseContextValues.value[node.id] = node.data.contextPercent || 10
          node.data.activityState = 'idle'
        }
      })
      nodes.value = [...nodes.value]
    }

    console.log(`[Collaboration] Starting replay from event ${currentEventIndex.value + 1} at ${replaySpeed.value}x speed`)

    // Schedule next event
    scheduleNextEvent()
  }

  function pauseReplay() {
    isPlaying.value = false
    if (replayInterval.value) {
      clearTimeout(replayInterval.value)
      replayInterval.value = null
    }
    console.log(`[Collaboration] Paused replay at event ${currentEventIndex.value + 1}`)
  }

  function stopReplay() {
    isPlaying.value = false
    currentEventIndex.value = 0
    replayElapsedMs.value = 0

    if (replayInterval.value) {
      clearTimeout(replayInterval.value)
      replayInterval.value = null
    }

    // Clear all activity timeouts
    Object.keys(activityTimeouts.value).forEach(key => {
      clearTimeout(activityTimeouts.value[key])
    })
    activityTimeouts.value = {}

    // Clear base context values
    baseContextValues.value = {}

    // Reset all edges to inactive state
    resetAllEdges()

    // Reset all agents to idle state
    nodes.value.forEach(node => {
      if (node.data.status === 'running') {
        node.data.activityState = 'idle'
      }
    })
    nodes.value = [...nodes.value]

    console.log('[Collaboration] Stopped replay')
  }

  function setReplaySpeed(speed) {
    replaySpeed.value = speed
    console.log(`[Collaboration] Replay speed set to ${speed}x`)

    // If currently playing, restart from current position with new speed
    if (isPlaying.value) {
      pauseReplay()
      setTimeout(() => startReplay(), 100)
    }
  }

  // Track activity timeouts for replay mode
  const activityTimeouts = ref({})

  function setNodeActivityState(agentName, state) {
    const node = nodes.value.find(n => n.id === agentName)
    if (node) {
      node.data = {
        ...node.data,
        activityState: state
      }
      // Trigger Vue reactivity
      nodes.value = [...nodes.value]
    }
  }

  // Store base context values for replay reset
  const baseContextValues = ref({})

  function simulateContextSpike(agentName, spike) {
    const node = nodes.value.find(n => n.id === agentName)
    if (node) {
      const baseContext = baseContextValues.value[agentName] || 10
      // Show temporary spike above base value
      node.data = {
        ...node.data,
        contextPercent: Math.min(100, baseContext + spike)
      }
      // Trigger Vue reactivity
      nodes.value = [...nodes.value]
    }
  }

  function resetContextToBase(agentName) {
    const node = nodes.value.find(n => n.id === agentName)
    if (node) {
      const baseContext = baseContextValues.value[agentName] || 10
      node.data = {
        ...node.data,
        contextPercent: baseContext
      }
      nodes.value = [...nodes.value]
    }
  }

  function simulateAgentActivity(sourceAgent, targetAgent) {
    // Clear any existing timeouts for these agents
    if (activityTimeouts.value[sourceAgent]) {
      clearTimeout(activityTimeouts.value[sourceAgent])
    }
    if (activityTimeouts.value[targetAgent]) {
      clearTimeout(activityTimeouts.value[targetAgent])
    }

    // Source agent becomes active immediately (initiating the collaboration)
    setNodeActivityState(sourceAgent, 'active')
    // Show context spike for source
    simulateContextSpike(sourceAgent, 5 + Math.random() * 10) // +5-15% spike

    // Target agent becomes active after a short delay (processing the request)
    setTimeout(() => {
      setNodeActivityState(targetAgent, 'active')
      // Show context spike for target (larger, processing incoming message)
      simulateContextSpike(targetAgent, 10 + Math.random() * 15) // +10-25% spike
    }, 300)

    // Source agent returns to idle after 2 seconds, reset context
    activityTimeouts.value[sourceAgent] = setTimeout(() => {
      setNodeActivityState(sourceAgent, 'idle')
      resetContextToBase(sourceAgent)
      delete activityTimeouts.value[sourceAgent]
    }, 2000)

    // Target agent returns to idle after 3 seconds, reset context
    activityTimeouts.value[targetAgent] = setTimeout(() => {
      setNodeActivityState(targetAgent, 'idle')
      resetContextToBase(targetAgent)
      delete activityTimeouts.value[targetAgent]
    }, 3000)
  }

  function scheduleNextEvent() {
    if (!isPlaying.value) return
    if (currentEventIndex.value >= historicalCollaborations.value.length) {
      // Replay complete
      console.log('[Collaboration] Replay complete')
      isPlaying.value = false
      // Reset all agents to idle
      nodes.value.forEach(node => {
        if (node.data.status === 'running') {
          node.data.activityState = 'idle'
        }
      })
      nodes.value = [...nodes.value]
      return
    }

    // Get events in chronological order (reverse of historicalCollaborations)
    const chronologicalEvents = [...historicalCollaborations.value].reverse()
    const currentEvent = chronologicalEvents[currentEventIndex.value]
    const nextEvent = chronologicalEvents[currentEventIndex.value + 1]

    // Animate current edge
    animateEdge(currentEvent.source_agent, currentEvent.target_agent)

    // Simulate agent activity states (green blinking)
    simulateAgentActivity(currentEvent.source_agent, currentEvent.target_agent)

    // Calculate delay to next event
    let delay = 500 // Default 500ms if last event
    if (nextEvent) {
      const realTimeDelta = getTimestampMs(nextEvent.timestamp) - getTimestampMs(currentEvent.timestamp)
      delay = realTimeDelta / replaySpeed.value
      delay = Math.max(delay, 100) // Min 100ms to prevent too fast
    }

    // Update progress
    currentEventIndex.value++
    replayElapsedMs.value = Date.now() - replayStartTime.value

    // Schedule next
    replayInterval.value = setTimeout(scheduleNextEvent, delay)
  }

  function jumpToTime(targetTimestamp) {
    // Find event closest to target time
    const chronologicalEvents = [...historicalCollaborations.value].reverse()
    const targetMs = getTimestampMs(targetTimestamp)
    const index = chronologicalEvents.findIndex(event =>
      getTimestampMs(event.timestamp) >= targetMs
    )

    if (index !== -1) {
      currentEventIndex.value = index

      // Reset edges and replay up to this point
      resetAllEdges()

      console.log(`[Collaboration] Jumped to event ${index + 1} at ${targetTimestamp}`)

      // If playing, restart from new position
      if (isPlaying.value) {
        pauseReplay()
        setTimeout(() => startReplay(), 100)
      }
    }
  }

  function jumpToEvent(index) {
    if (index >= 0 && index < historicalCollaborations.value.length) {
      currentEventIndex.value = index

      // Reset edges
      resetAllEdges()

      console.log(`[Collaboration] Jumped to event ${index + 1}`)

      // If playing, restart from new position
      if (isPlaying.value) {
        pauseReplay()
        setTimeout(() => startReplay(), 100)
      }
    }
  }

  function resetAllEdges() {
    // Set all collaboration edges to inactive state
    collaborationEdges.value.forEach(edge => {
      edge.animated = false
      edge.type = 'smoothstep'
      edge.className = 'collaboration-edge-inactive'
      edge.style = {
        stroke: '#cbd5e1',
        strokeWidth: 2,
        opacity: 0.5,
        transition: 'all 0.5s ease-in-out',
        filter: 'none'
      }
      // Restore direction-aware arrowheads
      edge.markerEnd = edge.data?.hasForward ? { type: 'arrowclosed', color: '#cbd5e1', width: 15, height: 15 } : undefined
      edge.markerStart = edge.data?.hasReverse ? { type: 'arrowclosed', color: '#cbd5e1', width: 15, height: 15 } : undefined

      // Keep count labels gray
      if (edge.data && edge.data.collaborationCount > 1) {
        edge.label = `${edge.data.collaborationCount}x`
        edge.labelStyle = {
          fontSize: '10px',
          fill: '#64748b'
        }
      }
    })

    // Trigger Vue reactivity for nested object changes
    collaborationEdges.value = [...collaborationEdges.value]

    activeCollaborations.value = 0
  }

  function getEventPosition(event) {
    if (!timelineStart.value || !timelineEnd.value) return 0

    const eventTime = getTimestampMs(event.timestamp)
    const startTime = getTimestampMs(timelineStart.value)
    const endTime = getTimestampMs(timelineEnd.value)

    if (endTime === startTime) return 0

    return ((eventTime - startTime) / (endTime - startTime)) * 100
  }

  function handleTimelineClick(clickX, timelineWidth) {
    const percent = (clickX / timelineWidth) * 100
    jumpToTimelinePosition(percent)
  }

  function jumpToTimelinePosition(percent) {
    if (!timelineStart.value || !timelineEnd.value) return

    const startTime = getTimestampMs(timelineStart.value)
    const endTime = getTimestampMs(timelineEnd.value)
    const targetTime = startTime + ((endTime - startTime) * (percent / 100))

    jumpToTime(new Date(targetTime).toISOString())
  }

  // Toggle autonomy mode for an agent
  async function toggleAutonomy(agentName) {
    // #47: both render surfaces must reflect the change — the graph reads
    // node.data, the Grid tiles read the agents[] list. Resolve current
    // state from either (an owner-filtered graph may lack the node).
    const node = nodes.value.find(n => n.id === agentName)
    const agent = agents.value.find(a => a.name === agentName)
    if (!node && !agent) {
      console.error('[Network] Agent not found:', agentName)
      return { success: false, error: 'Agent not found' }
    }

    const currentState = node ? node.data.autonomy_enabled : agent.autonomy_enabled
    const newState = !currentState

    try {
      const token = localStorage.getItem('token')
      const response = await axios.put(
        `/api/agents/${agentName}/autonomy`,
        { enabled: newState },
        { headers: { Authorization: `Bearer ${token}` } }
      )

      // Update both render surfaces
      if (node) node.data.autonomy_enabled = newState
      if (agent) agent.autonomy_enabled = newState

      console.log(`[Network] Autonomy ${newState ? 'enabled' : 'disabled'} for ${agentName}`)

      return {
        success: true,
        enabled: newState,
        schedulesUpdated: response.data.schedules_updated
      }
    } catch (error) {
      console.error('[Network] Failed to toggle autonomy:', error)
      return {
        success: false,
        error: error.response?.data?.detail || 'Failed to update autonomy mode'
      }
    }
  }

  // Fetch enabled schedules for timeline markers
  async function fetchSchedules() {
    try {
      const response = await axios.get('/api/ops/schedules', {
        params: { enabled_only: true }
      })
      schedules.value = response.data.schedules || []
      console.log(`[Collaboration] Loaded ${schedules.value.length} enabled schedules for timeline markers`)
    } catch (error) {
      console.error('[Collaboration] Failed to fetch schedules:', error)
      schedules.value = []
    }
  }

  // Toggle agent running state (start/stop)
  async function toggleAgentRunning(agentName) {
    // #47: update both render surfaces (graph nodes AND the agents[] list
    // the Grid tiles read).
    const node = nodes.value.find(n => n.id === agentName)
    const agent = agents.value.find(a => a.name === agentName)
    if (!node && !agent) {
      console.error('[Network] Agent not found:', agentName)
      return { success: false, error: 'Agent not found' }
    }

    const isRunning = (node ? node.data.status : agent.status) === 'running'
    runningToggleLoading.value[agentName] = true

    try {
      const token = localStorage.getItem('token')
      const newStatus = isRunning ? 'stopped' : 'running'
      await axios.post(
        `/api/agents/${agentName}/${isRunning ? 'stop' : 'start'}`,
        {},
        { headers: { Authorization: `Bearer ${token}` } }
      )
      if (node) {
        node.data.status = newStatus
        node.data.activityState = isRunning ? 'offline' : 'idle'
      }
      if (agent) agent.status = newStatus
      console.log(`[Network] Agent ${agentName} ${isRunning ? 'stopped' : 'started'}`)

      return { success: true, status: newStatus }
    } catch (error) {
      console.error('[Network] Failed to toggle agent running:', error)
      return {
        success: false,
        error: error.response?.data?.detail || 'Failed to toggle agent'
      }
    } finally {
      runningToggleLoading.value[agentName] = false
    }
  }

  // Check if an agent is in the process of toggling running state
  function isTogglingRunning(agentName) {
    return runningToggleLoading.value[agentName] || false
  }

  return {
    // State
    loading,        // #1266 initial-load flag (drives Dashboard/timeline skeletons)
    loadError,      // #1266 distinct failed-load state
    agents,
    nodes,
    edges,
    collaborationHistory,
    lastEventTime,
    activeCollaborations,
    isConnected,
    historicalCollaborations,
    totalCollaborationCount,
    timeRangeHours,
    isLoadingHistory,
    contextStats,
    executionStats,
    slotStats,
    circuitBreakers,
    workingState,
    schedules,
    filterTags,
    filterOwner,
    // View mode / Replay state
    viewMode,
    isTimelineMode,
    isPlaying,
    replaySpeed,
    currentEventIndex,
    replayElapsedMs,

    // Computed
    activeCollaborationCount,
    lastEventTimeFormatted,
    // Replay computed
    totalEvents,
    totalDuration,
    playbackPosition,
    timelineStart,
    timelineEnd,
    currentTime,

    // Actions
    fetchAgents,
    fetchPermissionEdges,
    setFilterTags,
    setFilterOwner,
    fetchHistoricalCollaborations,
    fetchHistoricalCommunications: fetchHistoricalCollaborations, // Alias for new terminology
    createHistoricalEdges,
    convertAgentsToNodes,
    connectWebSocket,
    disconnectWebSocket,
    handleCollaborationEvent,
    handleAgentStatusChange,
    handleAgentDeleted,
    animateEdge,
    fadeEdgeAnimation,
    clearEdgeAnimation,
    saveNodePositions,
    loadNodePositions,
    resetNodePositions,
    onNodeDragStop,
    fetchContextStats,
    fetchExecutionStats,
    fetchSlotStats,
    startContextPolling,
    stopContextPolling,
    startAgentRefresh,
    stopAgentRefresh,
    // Activity refresh (fallback polling)
    startActivityRefresh,
    stopActivityRefresh,
    // WebSocket heartbeat
    startWebSocketHeartbeat,
    stopWebSocketHeartbeat,
    // Activity status handler
    handleActivityStatusChange,
    // View mode / Replay actions
    setViewMode,
    setReplayMode,
    startReplay,
    pauseReplay,
    stopReplay,
    setReplaySpeed,
    jumpToTime,
    jumpToEvent,
    resetAllEdges,
    getEventPosition,
    handleTimelineClick,
    jumpToTimelinePosition,
    toggleAutonomy,
    // Schedule markers
    fetchSchedules,
    // Running state toggle
    toggleAgentRunning,
    isTogglingRunning,
    runningToggleLoading
  }
})
