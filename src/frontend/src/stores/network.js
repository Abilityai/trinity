import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import axios from 'axios'

export const useNetworkStore = defineStore('network', () => {
  // State
  const agents = ref([])
  const nodes = ref([])
  const edges = ref([])
  const collaborationHistory = ref([])
  const lastEventTime = ref(null)
  const activeCollaborations = ref(0)
  const websocket = ref(null)
  const isConnected = ref(false)
  const nodePositions = ref({}) // Store node positions in localStorage
  const historicalCollaborations = ref([]) // Persistent data from Activity Stream
  const totalCollaborationCount = ref(0)
  const timeRangeHours = ref(24) // Default to last 24 hours
  const isLoadingHistory = ref(false)
  const contextStats = ref({}) // Map of agent name -> context stats
  const contextPollingInterval = ref(null) // Interval ID for context polling
  const agentRefreshInterval = ref(null) // Interval ID for agent list refresh

  // Replay mode state
  const isReplayMode = ref(false)
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
    const diff = now - new Date(lastEventTime.value).getTime()

    if (diff < 1000) return 'Just now'
    if (diff < 60000) return `${Math.floor(diff / 1000)}s ago`
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
    return `${Math.floor(diff / 3600000)}h ago`
  })

  // Replay computed properties
  const totalEvents = computed(() => historicalCollaborations.value.length)

  const totalDuration = computed(() => {
    if (historicalCollaborations.value.length < 2) return 0
    const first = new Date(historicalCollaborations.value[historicalCollaborations.value.length - 1].timestamp)
    const last = new Date(historicalCollaborations.value[0].timestamp)
    return last - first
  })

  const playbackPosition = computed(() => {
    if (totalEvents.value === 0) return 0
    return (currentEventIndex.value / totalEvents.value) * 100
  })

  const timelineStart = computed(() => {
    if (historicalCollaborations.value.length === 0) return null
    return historicalCollaborations.value[historicalCollaborations.value.length - 1].timestamp
  })

  const timelineEnd = computed(() => {
    if (historicalCollaborations.value.length === 0) return null
    return historicalCollaborations.value[0].timestamp
  })

  const currentTime = computed(() => {
    if (currentEventIndex.value >= historicalCollaborations.value.length) return timelineEnd.value
    if (currentEventIndex.value === 0) return timelineStart.value
    return historicalCollaborations.value[historicalCollaborations.value.length - currentEventIndex.value].timestamp
  })

  // Actions
  async function fetchAgents() {
    try {
      const response = await axios.get('/api/agents')
      agents.value = response.data
      convertAgentsToNodes(response.data)
    } catch (error) {
      console.error('Failed to fetch agents:', error)
    }
  }

  async function fetchHistoricalCollaborations(hours = null) {
    isLoadingHistory.value = true

    try {
      const hoursToQuery = hours || timeRangeHours.value

      // Calculate start time (X hours ago)
      const startTime = new Date()
      startTime.setHours(startTime.getHours() - hoursToQuery)

      const params = {
        activity_types: 'agent_collaboration',
        start_time: startTime.toISOString(),
        limit: 500
      }

      const response = await axios.get('/api/activities/timeline', { params })

      // Parse activities into collaboration events
      const collaborations = response.data.activities
        .filter(activity => activity.details)
        .map(activity => {
          try {
            const details = typeof activity.details === 'string'
              ? JSON.parse(activity.details)
              : activity.details

            return {
              source_agent: details.source_agent || activity.agent_name,
              target_agent: details.target_agent,
              timestamp: activity.started_at || activity.created_at,
              activity_id: activity.id,
              status: activity.activity_state,
              duration_ms: activity.duration_ms
            }
          } catch (e) {
            console.warn('Failed to parse activity details:', e)
            return null
          }
        })
        .filter(c => c !== null && c.target_agent) // Only keep valid collaborations

      historicalCollaborations.value = collaborations
      totalCollaborationCount.value = collaborations.length

      // Create initial inactive edges from historical data
      createHistoricalEdges(collaborations)

      console.log(`[Collaboration] Loaded ${collaborations.length} historical collaborations from last ${hoursToQuery}h`)

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
    // Group by source-target pair to avoid duplicate edges
    const edgeMap = new Map()

    collaborations.forEach(collab => {
      const edgeId = `e-${collab.source_agent}-${collab.target_agent}`

      if (!edgeMap.has(edgeId)) {
        edgeMap.set(edgeId, {
          id: edgeId,
          source: collab.source_agent,
          target: collab.target_agent,
          count: 1,
          lastTimestamp: collab.timestamp,
          timestamps: [collab.timestamp]
        })
      } else {
        const existing = edgeMap.get(edgeId)
        existing.count++
        existing.timestamps.push(collab.timestamp)
        if (new Date(collab.timestamp) > new Date(existing.lastTimestamp)) {
          existing.lastTimestamp = collab.timestamp
        }
      }
    })

    // Create inactive edges for all historical collaborations
    edgeMap.forEach((edgeData, edgeId) => {
      // Only add if both nodes exist
      const sourceExists = nodes.value.some(n => n.id === edgeData.source)
      const targetExists = nodes.value.some(n => n.id === edgeData.target)

      if (sourceExists && targetExists) {
        const existingEdge = edges.value.find(e => e.id === edgeId)

        if (!existingEdge) {
          edges.value.push({
            id: edgeId,
            source: edgeData.source,
            target: edgeData.target,
            type: 'smoothstep', // Changed from 'default' to 'smoothstep' for better routing
            animated: false,
            className: 'collaboration-edge-inactive',
            style: {
              stroke: '#cbd5e1',
              strokeWidth: 2,
              opacity: 0.5,
              transition: 'all 0.5s ease-in-out'
            },
            markerEnd: {
              type: 'arrowclosed',
              color: '#cbd5e1',
              width: 15, // Reduced from 20
              height: 15  // Reduced from 20
            },
            label: edgeData.count > 1 ? `${edgeData.count}x` : undefined,
            labelStyle: {
              fontSize: '10px',
              fill: '#64748b'
            },
            data: {
              collaborationCount: edgeData.count,
              lastTimestamp: edgeData.lastTimestamp,
              timestamps: edgeData.timestamps
            }
          })
        }
      }
    })
  }

  function convertAgentsToNodes(agentList) {
    // Load saved positions from localStorage
    const savedPositions = loadNodePositions()

    // Calculate grid layout with better spacing to prevent overlap
    const gridSize = Math.ceil(Math.sqrt(agentList.length))
    const spacing = 350 // Increased from 250 to prevent overlap
    const offsetX = 150
    const offsetY = 150

    nodes.value = agentList.map((agent, index) => {
      const row = Math.floor(index / gridSize)
      const col = index % gridSize

      const defaultPosition = {
        x: offsetX + col * spacing,
        y: offsetY + row * spacing
      }

      return {
        id: agent.name,
        type: 'agent',
        data: {
          label: agent.name,
          status: agent.status,
          type: agent.type || 'business-assistant',
          owner: agent.owner,
          githubRepo: agent.github_repo || null,
          is_system: agent.is_system || false,
          // Set initial activityState based on running status to avoid "Offline" flash
          activityState: agent.status === 'running' ? 'idle' : 'offline'
        },
        position: savedPositions[agent.name] || defaultPosition,
        draggable: true
      }
    })
  }

  function connectWebSocket() {
    const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`

    try {
      websocket.value = new WebSocket(wsUrl)

      websocket.value.onopen = () => {
        console.log('[Collaboration] WebSocket connected')
        isConnected.value = true
      }

      websocket.value.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)

          if (data.type === 'agent_collaboration') {
            handleCollaborationEvent(data)
          } else if (data.type === 'agent_status') {
            handleAgentStatusChange(data)
          } else if (data.type === 'agent_deleted') {
            handleAgentDeleted(data)
          }
        } catch (error) {
          console.error('[Collaboration] Failed to parse WebSocket message:', error)
        }
      }

      websocket.value.onerror = (error) => {
        console.error('[Collaboration] WebSocket error:', error)
        isConnected.value = false
      }

      websocket.value.onclose = () => {
        console.log('[Collaboration] WebSocket disconnected')
        isConnected.value = false

        // Attempt reconnection after 5 seconds
        setTimeout(() => {
          if (!isConnected.value) {
            console.log('[Collaboration] Attempting to reconnect...')
            connectWebSocket()
          }
        }, 5000)
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

    // Remove edges connected to this agent
    edges.value = edges.value.filter(e =>
      e.source !== event.agent_name && e.target !== event.agent_name
    )

    // Remove from agents list
    agents.value = agents.value.filter(a => a.name !== event.agent_name)
  }

  // Track active edge timeouts for extending visibility
  const edgeTimeouts = ref({})

  function animateEdge(sourceId, targetId, extendedDuration = false) {
    const edgeId = `e-${sourceId}-${targetId}`

    // Clear any existing timeout for this edge (edge stays active longer if retriggered)
    if (edgeTimeouts.value[edgeId]) {
      clearTimeout(edgeTimeouts.value[edgeId])
      delete edgeTimeouts.value[edgeId]
    }

    // Check if edge already exists
    let edge = edges.value.find(e => e.id === edgeId)

    if (!edge) {
      // Create new edge with fade-in effect
      edge = {
        id: edgeId,
        source: sourceId,
        target: targetId,
        type: 'smoothstep', // Changed to smoothstep for better routing
        animated: true,
        className: 'collaboration-edge-active',
        style: {
          stroke: 'url(#collaboration-gradient)',
          strokeWidth: 3,
          opacity: 0,
          transition: 'all 0.5s ease-in-out',
          filter: 'drop-shadow(0 0 4px rgba(6, 182, 212, 0.5))'
        },
        markerEnd: {
          type: 'arrowclosed',
          color: '#06b6d4',
          width: 18,
          height: 18
        },
        data: {
          gradientId: edgeId,
          collaborationCount: 1,
          timestamps: [new Date().toISOString()],
          targetAgent: targetId
        }
      }
      edges.value.push(edge)

      // Trigger fade-in animation
      setTimeout(() => {
        edge.style.opacity = 1
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
      edge.markerEnd = {
        type: 'arrowclosed',
        color: '#06b6d4',
        width: 18,
        height: 18
      }

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
    const edge = edges.value.find(e => e.id === edgeId)
    if (edge) {
      // First fade the glow
      edge.style = {
        ...edge.style,
        filter: 'drop-shadow(0 0 2px rgba(148, 163, 184, 0.3))',
        strokeWidth: 2.5
      }

      // Then fade to inactive state after 800ms
      setTimeout(() => {
        clearEdgeAnimation(edgeId)
      }, 800)
    }
  }

  function clearEdgeAnimation(edgeId) {
    const edge = edges.value.find(e => e.id === edgeId)
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
      edge.markerEnd = {
        type: 'arrowclosed',
        color: '#cbd5e1',
        width: 15, // Reduced from 20
        height: 15  // Reduced from 20
      }

      // Keep the count label but make it gray
      if (edge.data && edge.data.collaborationCount > 1) {
        edge.label = `${edge.data.collaborationCount}x`
        edge.labelStyle = {
          fontSize: '10px',
          fill: '#64748b'
        }
      }

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

  // Start polling context stats every 5 seconds
  function startContextPolling() {
    if (contextPollingInterval.value) {
      clearInterval(contextPollingInterval.value)
    }

    // Fetch immediately
    fetchContextStats()

    // Then poll every 5 seconds
    contextPollingInterval.value = setInterval(() => {
      fetchContextStats()
    }, 5000)

    console.log('[Collaboration] Started context polling (every 5s)')
  }

  // Stop polling context stats
  function stopContextPolling() {
    if (contextPollingInterval.value) {
      clearInterval(contextPollingInterval.value)
      contextPollingInterval.value = null
      console.log('[Collaboration] Stopped context polling')
    }
  }

  // Start polling agent list every 10 seconds (for new/deleted agents)
  function startAgentRefresh() {
    if (agentRefreshInterval.value) {
      clearInterval(agentRefreshInterval.value)
    }

    // Poll every 10 seconds
    agentRefreshInterval.value = setInterval(async () => {
      try {
        const response = await axios.get('/api/agents')
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
    }, 10000)

    console.log('[Collaboration] Started agent refresh polling (every 10s)')
  }

  // Stop polling agent list
  function stopAgentRefresh() {
    if (agentRefreshInterval.value) {
      clearInterval(agentRefreshInterval.value)
      agentRefreshInterval.value = null
      console.log('[Collaboration] Stopped agent refresh polling')
    }
  }

  // Replay Mode Functions
  function setReplayMode(mode) {
    isReplayMode.value = mode
    localStorage.setItem('trinity-replay-mode', mode ? 'true' : 'false')

    if (mode) {
      // Entering replay mode - disconnect WebSocket and stop polling
      disconnectWebSocket()
      stopContextPolling()
      stopAgentRefresh()
      console.log('[Collaboration] Switched to Replay mode')
    } else {
      // Exiting replay mode - reconnect WebSocket and start polling
      stopReplay()
      connectWebSocket()
      startContextPolling()
      startAgentRefresh()
      console.log('[Collaboration] Switched to Live mode')
    }
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

    // Reset all edges to inactive state
    resetAllEdges()

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

  function scheduleNextEvent() {
    if (!isPlaying.value) return
    if (currentEventIndex.value >= historicalCollaborations.value.length) {
      // Replay complete
      console.log('[Collaboration] Replay complete')
      isPlaying.value = false
      return
    }

    // Get events in chronological order (reverse of historicalCollaborations)
    const chronologicalEvents = [...historicalCollaborations.value].reverse()
    const currentEvent = chronologicalEvents[currentEventIndex.value]
    const nextEvent = chronologicalEvents[currentEventIndex.value + 1]

    // Animate current edge
    animateEdge(currentEvent.source_agent, currentEvent.target_agent)

    // Calculate delay to next event
    let delay = 500 // Default 500ms if last event
    if (nextEvent) {
      const realTimeDelta = new Date(nextEvent.timestamp) - new Date(currentEvent.timestamp)
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
    const index = chronologicalEvents.findIndex(event =>
      new Date(event.timestamp) >= new Date(targetTimestamp)
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
    // Set all edges to inactive state
    edges.value.forEach(edge => {
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
      edge.markerEnd = {
        type: 'arrowclosed',
        color: '#cbd5e1',
        width: 15, // Reduced from 20
        height: 15  // Reduced from 20
      }

      // Keep count labels gray
      if (edge.data && edge.data.collaborationCount > 1) {
        edge.label = `${edge.data.collaborationCount}x`
        edge.labelStyle = {
          fontSize: '10px',
          fill: '#64748b'
        }
      }
    })

    activeCollaborations.value = 0
  }

  function getEventPosition(event) {
    if (!timelineStart.value || !timelineEnd.value) return 0

    const eventTime = new Date(event.timestamp).getTime()
    const startTime = new Date(timelineStart.value).getTime()
    const endTime = new Date(timelineEnd.value).getTime()

    if (endTime === startTime) return 0

    return ((eventTime - startTime) / (endTime - startTime)) * 100
  }

  function handleTimelineClick(clickX, timelineWidth) {
    const percent = (clickX / timelineWidth) * 100
    jumpToTimelinePosition(percent)
  }

  function jumpToTimelinePosition(percent) {
    if (!timelineStart.value || !timelineEnd.value) return

    const startTime = new Date(timelineStart.value).getTime()
    const endTime = new Date(timelineEnd.value).getTime()
    const targetTime = startTime + ((endTime - startTime) * (percent / 100))

    jumpToTime(new Date(targetTime).toISOString())
  }

  return {
    // State
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
    // Replay state
    isReplayMode,
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
    startContextPolling,
    stopContextPolling,
    startAgentRefresh,
    stopAgentRefresh,
    // Replay actions
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
    jumpToTimelinePosition
  }
})
