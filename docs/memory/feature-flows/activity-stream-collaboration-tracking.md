# Feature: Activity Stream Collaboration Tracking

**Last Updated**: 2026-01-23

## Overview
End-to-end tracking of agent-to-agent collaborations from MCP tool invocation through database persistence, real-time WebSocket broadcasting, and historical visualization in the Dashboard with replay capabilities.

## User Story
As a platform administrator, I want to track and visualize all agent collaboration events so that I can monitor interaction patterns, analyze collaboration frequency, and understand multi-agent workflows over time.

## Entry Points
- **MCP Tool**: `chat_with_agent` - Agent A invokes Trinity MCP to chat with Agent B
- **Backend API**: `POST /api/agents/{name}/chat` with `X-Source-Agent` header
- **Timeline API**: `GET /api/activities/timeline?activity_types=agent_collaboration`
- **WebSocket**: `ws://[host]/ws` - Real-time collaboration broadcasts

## Agent Layer

### MCP Client Implementation
**File**: `src/mcp-server/src/client.ts`

#### chat() Method (Lines 336-378)
Accepts optional `sourceAgent` parameter for collaboration tracking:

```typescript
async chat(name: string, message: string, sourceAgent?: string): Promise<ChatResponse | { error: string; queue_status: "busy" | "queue_full"; retry_after: number; agent: string; details?: Record<string, unknown> }> {
  // Prepare headers
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(this.token && { Authorization: `Bearer ${this.token}` }),
    "X-Via-MCP": "true",  // Always mark as MCP call for task tracking
  };

  // Add X-Source-Agent header for collaboration tracking
  if (sourceAgent) {
    headers["X-Source-Agent"] = sourceAgent;
  }

  const response = await fetch(`${this.baseUrl}/api/agents/${encodeURIComponent(name)}/chat`, {
    method: "POST",
    headers,
    body: JSON.stringify({ message }),
  });
}
```

**Key Feature**: Passes `X-Source-Agent` HTTP header to backend when agent initiates chat.

### MCP Chat Tool
**File**: `src/mcp-server/src/tools/chat.ts`

#### chatWithAgent Tool (Lines 132-270)
Extracts source agent from auth context and passes to client:

```typescript
execute: async (
  {
    agent_name,
    message,
    parallel,
    model,
    allowed_tools,
    system_prompt,
    timeout_seconds,
  },
  context
) => {
  // Get auth context from FastMCP session
  const authContext = requireApiKey ? context?.session : undefined;

  // Get authenticated client for this request
  const apiClient = getClient(authContext);

  const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name);

  if (!accessCheck.allowed) {
    return JSON.stringify({ error: "Access denied", reason: accessCheck.reason });
  }

  // Log successful collaboration (Lines 224-227)
  if (authContext?.scope === "agent") {
    console.log(`[Agent Collaboration] ${authContext.agentName} -> ${agent_name} (${parallel ? 'parallel' : 'sequential'})`);
  }

  // Pass source agent for collaboration tracking (Lines 229-230)
  const sourceAgent = authContext?.scope === "agent" ? authContext.agentName : undefined;
  const response = await apiClient.chat(agent_name, message, sourceAgent);
  return JSON.stringify(response, null, 2);
}
```

**Flow**:
1. Check if caller has access to target agent (permission system for agent-scoped keys)
2. Extract source agent from MCP auth context (agent-scoped API key)
3. Pass `sourceAgent` parameter to `client.chat()`
4. Client adds `X-Source-Agent` header to HTTP request

## Backend Layer

### Chat Router with Activity Tracking
**File**: `src/backend/routers/chat.py`

#### WebSocket Manager Injection (Lines 81-88)
```python
# WebSocket manager (injected from main.py)
_websocket_manager = None

def set_websocket_manager(manager):
    """Set WebSocket manager for broadcasting collaboration events."""
    global _websocket_manager
    _websocket_manager = manager
```

Called from `main.py` to inject WebSocket manager into router.

#### Collaboration Broadcasting (Lines 91-103)
```python
async def broadcast_collaboration_event(source_agent: str, target_agent: str, action: str = "chat"):
    """Broadcast agent collaboration event to all WebSocket clients."""
    if _websocket_manager:
        event = {
            "type": "agent_collaboration",
            "source_agent": source_agent,
            "target_agent": target_agent,
            "action": action,
            "timestamp": datetime.utcnow().isoformat()
        }
        await _websocket_manager.broadcast(json.dumps(event))
    else:
        print(f"[Warning] WebSocket manager not set, skipping collaboration broadcast")
```

**Event Schema**:
- `type`: "agent_collaboration"
- `source_agent`: Name of initiating agent
- `target_agent`: Name of target agent
- `action`: "chat" (default)
- `timestamp`: ISO 8601 timestamp

#### Chat Endpoint with Activity Detection (Lines 106-416)
```python
@router.post("/{name}/chat")
async def chat_with_agent(
    name: str,
    request: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None),
    x_via_mcp: Optional[str] = Header(None)
):
```

**Detection Logic** (Lines 186-210):
```python
# Broadcast collaboration event if this is agent-to-agent communication
collaboration_activity_id = None
if x_source_agent:
    await broadcast_collaboration_event(
        source_agent=x_source_agent,
        target_agent=name,
        action="chat"
    )

    # Track agent collaboration activity
    collaboration_activity_id = await activity_service.track_activity(
        agent_name=x_source_agent,  # Activity belongs to source agent
        activity_type=ActivityType.AGENT_COLLABORATION,
        user_id=current_user.id,
        triggered_by="agent",
        related_execution_id=task_execution_id,  # Database execution ID for structured queries
        details={
            "source_agent": x_source_agent,
            "target_agent": name,
            "action": "chat",
            "message_preview": request.message[:100],
            "execution_id": task_execution_id,  # Also in details for WebSocket events
            "queue_status": queue_result
        }
    )
```

**Parent-Child Activity Tracking** (Lines 219-235):
```python
# Track chat start activity
# triggered_by: "agent" for agent-to-agent, "mcp" for user MCP calls, "user" for UI chat
activity_triggered_by = "agent" if x_source_agent else ("mcp" if x_via_mcp else "user")
chat_activity_id = await activity_service.track_activity(
    agent_name=name,
    activity_type=ActivityType.CHAT_START,
    user_id=current_user.id,
    triggered_by=activity_triggered_by,
    parent_activity_id=collaboration_activity_id,  # Link to collaboration if agent-initiated
    related_execution_id=task_execution_id,  # Database execution ID for structured queries
    details={
        "message_preview": request.message[:100],
        "source_agent": x_source_agent,
        "execution_id": task_execution_id,  # Also in details for WebSocket events
        "queue_status": queue_result
    }
)
```

> **#1332:** the collaboration close-sites (`routers/chat.py` `_complete_collaboration_activity`
> and `execute_parallel_task`) no longer hard-code `COMPLETED if SUCCESS else FAILED`.
> They call the shared `models.activity_state_for_terminal(result.status)` helper, so a
> user-cancelled collaboration turn closes the activity as `CANCELLED` (not `FAILED`) and
> reads as a distinct, non-failure terminal in the collaboration timeline / replay.

**Activity Completion** (Lines 330-341):
```python
# Complete collaboration activity if this was agent-to-agent
if collaboration_activity_id:
    await activity_service.complete_activity(
        activity_id=collaboration_activity_id,
        status="completed",
        details={
            "related_chat_message_id": assistant_message.id,
            "response_length": len(response_data.get("response", "")),
            "execution_time_ms": execution_time_ms,
            "execution_id": task_execution_id  # Use database execution ID, not queue ID
        }
    )
```

**Error Handling for Collaboration Activities** (Lines 401-407):
```python
# Complete collaboration activity on failure (was missing - caused activities to stay in "started" state)
if collaboration_activity_id:
    await activity_service.complete_activity(
        activity_id=collaboration_activity_id,
        status="failed",
        error=error_msg
    )
```

**Hierarchy**:
```
AGENT_COLLABORATION (source agent)
  |-- CHAT_START (target agent)
      |-- TOOL_CALL (each tool used)
```

### Activity Service
**File**: `src/backend/services/activity_service.py`

#### track_activity() Method (Lines 46-107)
Central service for creating activity records:

```python
async def track_activity(
    self,
    agent_name: str,
    activity_type: ActivityType,
    user_id: Optional[int] = None,
    triggered_by: str = "user",
    parent_activity_id: Optional[str] = None,
    related_chat_message_id: Optional[str] = None,
    related_execution_id: Optional[str] = None,
    details: Optional[Dict] = None
) -> str:
    # Create activity in database
    activity = ActivityCreate(
        agent_name=agent_name,
        activity_type=activity_type,
        activity_state=ActivityState.STARTED,
        parent_activity_id=parent_activity_id,
        user_id=user_id,
        triggered_by=triggered_by,
        related_chat_message_id=related_chat_message_id,
        related_execution_id=related_execution_id,
        details=details
    )

    activity_id = db.create_activity(activity)

    # Broadcast via WebSocket
    await self._broadcast_activity_event(
        agent_name=agent_name,
        activity_id=activity_id,
        activity_type=activity_type.value,
        activity_state="started",
        action=self._get_action_description(activity_type, details),
        details=details
    )

    return activity_id
```

**Returns**: UUID of created activity for linking child activities.

#### complete_activity() Method (Lines 109-159)
Marks activity as completed or failed:

```python
async def complete_activity(
    self,
    activity_id: str,
    status: str = "completed",
    details: Optional[Dict] = None,
    error: Optional[str] = None
) -> bool:
    # Get activity to broadcast details
    activity = db.get_activity(activity_id)
    if not activity:
        return False

    # Update in database
    success = db.complete_activity(activity_id, status, details, error)

    if success:
        # Broadcast completion via WebSocket
        await self._broadcast_activity_event(...)
```

#### Action Description Generator (Lines 214-244)
Human-readable activity descriptions:

```python
def _get_action_description(self, activity_type: ActivityType, details: Optional[Dict] = None) -> str:
    if activity_type == ActivityType.AGENT_COLLABORATION:
        if details and "target_agent" in details:
            return f"Collaborating with: {details['target_agent']}"
        return "Agent collaboration"
```

### Activities API Router
**File**: `src/backend/routers/activities.py`

#### Timeline Endpoint (Lines 15-55)
Cross-agent activity timeline with access control:

```python
@router.get("/timeline")
async def get_activity_timeline(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    activity_types: Optional[str] = Query(None, description="Comma-separated list of activity types"),
    limit: int = 100,
    current_user: User = Depends(get_current_user)
):
    # Parse activity types
    types_list = None
    if activity_types:
        types_list = [t.strip() for t in activity_types.split(',')]

    # Get all activities
    all_activities = db.get_activities_in_range(
        start_time=start_time,
        end_time=end_time,
        activity_types=types_list,
        limit=limit * 2  # Get more than needed for filtering
    )

    # Get accessible agents for this user
    from routers.agents import get_accessible_agents
    accessible_agents = get_accessible_agents(current_user)
    accessible_agent_names = {agent['name'] for agent in accessible_agents}

    # Filter activities to only include accessible agents
    filtered_activities = [
        activity for activity in all_activities
        if activity['agent_name'] in accessible_agent_names
    ][:limit]

    return {
        "count": len(filtered_activities),
        "activities": filtered_activities
    }
```

**Access Control**: Only returns activities for agents user can access (owner, shared, or admin).

**Query Parameters**:
- `start_time`: ISO 8601 timestamp (e.g., "2025-12-01T00:00:00Z")
- `end_time`: ISO 8601 timestamp
- `activity_types`: Comma-separated types (e.g., "agent_collaboration")
- `limit`: Max results (default 100)

## Database Layer

### Activity Operations
**File**: `src/backend/db/activities.py`

#### create_activity() (Lines 40-72)
```python
def create_activity(self, activity: 'ActivityCreate') -> str:
    activity_id = str(uuid.uuid4())
    now = utc_now_iso()  # Use UTC with 'Z' suffix for frontend compatibility

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO agent_activities (
                id, agent_name, activity_type, activity_state, parent_activity_id,
                started_at, user_id, triggered_by, related_chat_message_id,
                related_execution_id, details, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (...))
        conn.commit()
    return activity_id
```

#### get_activities_in_range() (Lines 154-185)
```python
def get_activities_in_range(self, start_time: Optional[str] = None,
                            end_time: Optional[str] = None,
                            activity_types: Optional[List[str]] = None,
                            limit: int = 100) -> List[Dict]:
    query = "SELECT * FROM agent_activities WHERE 1=1"
    params = []

    if start_time:
        query += " AND created_at >= ?"
        params.append(start_time)

    if end_time:
        query += " AND created_at <= ?"
        params.append(end_time)

    if activity_types:
        placeholders = ",".join("?" * len(activity_types))
        query += f" AND activity_type IN ({placeholders})"
        params.extend(activity_types)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
```

### agent_activities Table Schema
**Location**: `src/backend/database.py` (Lines 475-497)

```sql
CREATE TABLE agent_activities (
    id TEXT PRIMARY KEY,                -- UUID
    agent_name TEXT NOT NULL,           -- Agent the activity belongs to
    activity_type TEXT NOT NULL,        -- 'agent_collaboration', 'chat_start', 'tool_call', etc.
    activity_state TEXT NOT NULL,       -- 'started', 'completed', 'failed', 'cancelled' (#1332)
    parent_activity_id TEXT,            -- Link to parent activity (nullable)
    started_at TEXT NOT NULL,           -- ISO 8601 timestamp
    completed_at TEXT,                  -- When activity finished
    duration_ms INTEGER,                -- Execution time
    user_id INTEGER,                    -- User who triggered (nullable)
    triggered_by TEXT NOT NULL,         -- 'user', 'agent', 'schedule', 'system', 'mcp'
    related_chat_message_id TEXT,       -- FK to chat_messages
    related_execution_id TEXT,          -- FK to schedule_executions
    details TEXT,                       -- JSON metadata
    error TEXT,                         -- Error message if failed
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for efficient querying (database.py lines 629-635)
CREATE INDEX idx_activities_agent ON agent_activities(agent_name, created_at DESC);
CREATE INDEX idx_activities_type ON agent_activities(activity_type);
CREATE INDEX idx_activities_state ON agent_activities(activity_state);
CREATE INDEX idx_activities_user ON agent_activities(user_id);
CREATE INDEX idx_activities_parent ON agent_activities(parent_activity_id);
CREATE INDEX idx_activities_chat_msg ON agent_activities(related_chat_message_id);
CREATE INDEX idx_activities_execution ON agent_activities(related_execution_id);
```

### Example Activity Record
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "agent_name": "research-agent",
  "activity_type": "agent_collaboration",
  "activity_state": "completed",
  "parent_activity_id": null,
  "started_at": "2025-12-02T15:30:45.123456Z",
  "completed_at": "2025-12-02T15:30:48.456789Z",
  "duration_ms": 3333,
  "user_id": 1,
  "triggered_by": "agent",
  "related_chat_message_id": "msg-uuid-456",
  "related_execution_id": "exec-uuid-123",
  "details": {
    "source_agent": "research-agent",
    "target_agent": "writing-agent",
    "action": "chat",
    "message_preview": "Please write a summary of...",
    "response_length": 1024,
    "execution_time_ms": 3333,
    "execution_id": "exec-uuid-123"
  },
  "error": null,
  "created_at": "2025-12-02T15:30:45.123456Z"
}
```

## Frontend Layer

### Network Store
**File**: `src/frontend/src/stores/network.js`

#### State Variables (Lines 45-52)
```javascript
const historicalCollaborations = ref([]) // Persistent data from Activity Stream
const totalCollaborationCount = ref(0)
const timeRangeHours = ref(24)            // Default to last 24 hours
const isLoadingHistory = ref(false)
const contextStats = ref({})              // Map of agent name -> context stats
const executionStats = ref({})            // Map of agent name -> execution stats
```

#### View Mode / Replay State (Lines 55-63)
```javascript
// View mode state (graph vs timeline) - default to timeline, persist to localStorage
const savedViewMode = localStorage.getItem('trinity-dashboard-view')
const isTimelineMode = ref(savedViewMode ? savedViewMode === 'timeline' : true)
const isPlaying = ref(false)
const replaySpeed = ref(10) // 10x default
const currentEventIndex = ref(0)
const replayInterval = ref(null)
const replayStartTime = ref(null)
const replayElapsedMs = ref(0)
```

#### fetchHistoricalCollaborations() Method (Lines 131-222)
Fetches collaboration history from Activity Stream API:

```javascript
async function fetchHistoricalCollaborations(hours = null) {
  isLoadingHistory.value = true

  try {
    const hoursToQuery = hours || timeRangeHours.value

    // Calculate start time (X hours ago)
    const startTime = new Date()
    startTime.setHours(startTime.getHours() - hoursToQuery)

    // Fetch ALL execution types (not just collaborations)
    const params = {
      activity_types: 'agent_collaboration,schedule_start,schedule_end,chat_start,chat_end',
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
        // ... event parsing logic
      })

    historicalCollaborations.value = events
    totalCollaborationCount.value = events.length

    // Create initial inactive edges from collaboration events only
    const collaborationEvents = events.filter(e => e.target_agent)
    createHistoricalEdges(collaborationEvents)
  } finally {
    isLoadingHistory.value = false
  }
}
```

#### createHistoricalEdges() Method (Lines 224-292)
Creates inactive edges with collaboration counts:

```javascript
function createHistoricalEdges(collaborations) {
  // Group by source-target pair to avoid duplicate edges
  const edgeMap = new Map()

  collaborations.forEach(collab => {
    const edgeId = `e-${collab.source_agent}-${collab.target_agent}`
    // ... edge aggregation logic
  })

  // Clear existing collaboration edges before rebuilding
  collaborationEdges.value = []

  // Create inactive edges for all historical collaborations
  edgeMap.forEach((edgeData, edgeId) => {
    if (sourceExists && targetExists) {
      collaborationEdges.value.push({
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
        markerEnd: {
          type: 'arrowclosed',
          color: '#cbd5e1',
          width: 15,
          height: 15
        },
        label: edgeData.count > 1 ? `${edgeData.count}x` : undefined,
        data: {
          collaborationCount: edgeData.count,
          lastTimestamp: edgeData.lastTimestamp,
          timestamps: edgeData.timestamps
        }
      })
    }
  })
}
```

**Edge Styling**:
- **Inactive** (historical): Gray (#cbd5e1), 2px width, 50% opacity, count label
- **Active** (real-time): Cyan gradient, 3px width, 100% opacity, animated flow with glow

#### Real-Time Event Handling (Lines 431-456)
Merges real-time events with historical data:

```javascript
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

  // Animate edge (switches from gray inactive to cyan active)
  animateEdge(event.source_agent, event.target_agent)
}
```

#### Replay Mode Functions (Lines 864-1107)
Complete replay functionality with timeline scrubbing:

```javascript
function setViewMode(mode) {
  const isTimeline = mode === 'timeline'
  isTimelineMode.value = isTimeline
  localStorage.setItem('trinity-dashboard-view', mode)

  // Keep WebSocket and polling active in BOTH views for live updates
  // Timeline view now shows live events, so we need real-time data
  if (isTimeline) {
    // Entering timeline mode - stop any active replay playback
    stopReplay()
    console.log('[Collaboration] Switched to Timeline view (live mode)')
  } else {
    // Entering graph mode
    stopReplay()
    console.log('[Collaboration] Switched to Graph view')
  }
}

function startReplay() { ... }
function pauseReplay() { ... }
function stopReplay() { ... }
function setReplaySpeed(speed) { ... }
function jumpToTime(targetTimestamp) { ... }
function jumpToEvent(index) { ... }
```

#### Context and Execution Stats Polling (Lines 770-838)
```javascript
// Start polling context and execution stats every 5 seconds
function startContextPolling() {
  if (contextPollingInterval.value) {
    clearInterval(contextPollingInterval.value)
  }

  // Fetch immediately
  fetchContextStats()
  fetchExecutionStats()

  // Then poll every 5 seconds
  contextPollingInterval.value = setInterval(() => {
    fetchContextStats()
    fetchExecutionStats()
  }, 5000)

  console.log('[Collaboration] Started context polling (every 5s)')
}

// Start polling agent list every 10 seconds (for new/deleted agents)
function startAgentRefresh() {
  // Poll every 10 seconds
  agentRefreshInterval.value = setInterval(async () => {
    // ... agent list refresh logic
  }, 10000)

  console.log('[Collaboration] Started agent refresh polling (every 10s)')
}
```

### Dashboard UI
**File**: `src/frontend/src/views/Dashboard.vue` (751 lines total)

#### Header Statistics (Lines 8-61)
```html
<div class="flex items-center space-x-3 text-xs text-gray-500">
  <span>{{ agents.length }} agents</span>
  <span>{{ runningCount }} running</span>
  <span>{{ totalCollaborationCount }} messages ({{ timeRangeHours }}h)</span>
  <span v-if="activeCollaborationCount > 0">{{ activeCollaborationCount }} active</span>
</div>
```

#### Mode Toggle (Lines 66-85)
Toggle between Graph and Timeline modes with styled buttons.

#### Time Range Filter (Lines 87-98)
```html
<select v-model="selectedTimeRange" @change="onTimeRangeChange">
  <option :value="1">1h</option>
  <option :value="6">6h</option>
  <option :value="24">24h</option>
  <option :value="72">3d</option>
  <option :value="168">7d</option>
</select>
```

#### Timeline View Component (Lines 141-164)
```html
<ReplayTimeline
  v-if="isTimelineMode"
  :agents="agents"
  :nodes="nodes"
  :events="historicalCollaborations"
  :timeline-start="timelineStart"
  :timeline-end="timelineEnd"
  :current-event-index="currentEventIndex"
  :total-events="totalEvents"
  :total-duration="totalDuration"
  :replay-elapsed-ms="replayElapsedMs"
  :replay-speed="replaySpeed"
  :is-playing="isPlaying"
  :context-stats="contextStats"
  :execution-stats="executionStats"
  :is-live-mode="true"
  :time-range-hours="selectedTimeRange"
  @play="handlePlay"
  @pause="handlePause"
  @stop="handleStop"
  @speed-change="handleSpeedChange"
  @toggle-autonomy="handleToggleAutonomy"
/>
```

#### History Panel (Lines 275-337)
Split into "Live Feed" and "Historical" sections with collapsible UI.

#### Component Lifecycle (Lines 414-443)
```javascript
onMounted(async () => {
  // 1. Fetch agents first
  await networkStore.fetchAgents()

  // 2. Fetch historical communication data from Activity Stream
  await networkStore.fetchHistoricalCommunications()

  // 3. Connect WebSocket for real-time updates
  networkStore.connectWebSocket()

  // 4. Start polling for context stats
  networkStore.startContextPolling()

  // 5. Start polling for agent list updates
  networkStore.startAgentRefresh()

  // 6. Initialize observability (checks if OTel is enabled)
  await observabilityStore.fetchStatus()

  // 7. Fit view after initial load
  setTimeout(() => {
    fitView({ padding: 0.2, duration: 800 })
  }, 100)
})
```

## Side Effects

### WebSocket Broadcasts

#### agent_collaboration Event
Broadcast when `X-Source-Agent` header detected:

```json
{
  "type": "agent_collaboration",
  "source_agent": "research-agent",
  "target_agent": "writing-agent",
  "action": "chat",
  "timestamp": "2025-12-02T15:30:45.123456"
}
```

**Subscribers**: All connected WebSocket clients (dashboard, detail pages).

#### agent_activity Event
Broadcast when activity state changes:

```json
{
  "type": "agent_activity",
  "agent_name": "research-agent",
  "activity_id": "a1b2c3d4-...",
  "activity_type": "agent_collaboration",
  "activity_state": "completed",
  "action": "Collaborating with: writing-agent",
  "timestamp": "2025-12-02T15:30:48.456789",
  "details": {
    "related_chat_message_id": "msg-uuid-456",
    "response_length": 1024,
    "execution_time_ms": 3333
  },
  "error": null
}
```

### Database Writes

**agent_activities Table**:
- INSERT on collaboration start (triggered_by="agent")
- UPDATE on collaboration completion (sets completed_at, duration_ms, details)
- INSERT for CHAT_START child activity (parent_activity_id set)
- INSERT for each TOOL_CALL (linked to chat activity)

**chat_messages Table**:
- Normal chat persistence continues
- `related_chat_message_id` links activity to message

### Audit Logging

Standard audit event for chat interactions:
```python
await log_audit_event(
    event_type="agent_interaction",
    action="chat",
    user_id=current_user.username,
    agent_name=name,
    resource=f"agent-{name}",
    result="success"
)
```

**Note**: Collaboration events are tracked in `agent_activities`, not separate audit logs.

## Error Handling

| Error Case | HTTP Status | Handling | User Feedback |
|------------|-------------|----------|---------------|
| Activity API auth failure | 401 | Re-authenticate | Login redirect |
| Timeline query timeout | 500 | Log error, continue | Show cached data |
| Invalid activity type filter | 200 | Ignore invalid types | Returns matching records |
| Access denied (agent) | 403 | Filter from results | Silent (not shown) |
| Database connection error | 500 | Retry with backoff | "Failed to load history" |
| WebSocket disconnect | N/A | Auto-reconnect (5s) | "Disconnected" indicator |
| JSON parse error (details) | N/A | Skip record | Log warning, continue |
| Queue full | 429 | Return queue status | "Agent is busy" message |

**Graceful Degradation**:
- If timeline API fails, dashboard still shows real-time events
- If WebSocket fails, historical data still visible
- If activity creation fails, chat continues (non-blocking)

## Security Considerations

### Authentication & Authorization
- **JWT Required**: All API endpoints require valid JWT token
- **Access Control**: Timeline API filters activities by accessible agents
  - Owner: See all activities for owned agents
  - Shared: See activities for shared agents
  - Admin: See all activities
- **MCP Auth**: Agent-scoped API keys identify source agent with permission system

### Data Privacy
- **No Message Content**: Activity details only include 100-char preview
- **Agent Names Only**: No sensitive agent configuration in events
- **User ID Tracking**: Links activities to users for accountability

### Rate Limiting
- **Execution Queue**: Prevents parallel execution on same agent (max 3 queued)
- **Queue Full Response**: Returns 429 with retry_after when queue full

## Performance Considerations

### Database Optimization
- **Indexes**: 7 indexes for efficient queries (agent, type, state, user, parent, chat_msg, execution)
- **Query Limits**: Default 100 records, max 500 with pagination
- **Time Range**: Queries only specified time window (not full table scan)

### Frontend Optimization
- **Lazy Loading**: Historical data fetched only when dashboard opens
- **Pagination**: Limited to 15 events in history panel (scrollable)
- **Edge Deduplication**: Single edge per agent pair, count label shows frequency
- **Context Polling**: Every 5 seconds for context stats and activity state
- **Agent Refresh**: Every 10 seconds for agent list updates

### Scalability Limits
- **Tested**: 50 agents, 100 collaborations/hour
- **Database**: SQLite handles 10K+ activities without degradation
- **Graph Rendering**: Vue Flow performs well with 100+ edges

## Testing

### Test Case 1: Agent-to-Agent Collaboration with Activity Tracking
**Objective**: Verify end-to-end collaboration tracking from MCP to database to dashboard

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Agent A uses `chat_with_agent` tool targeting Agent B | MCP adds `X-Source-Agent: agent-a` header | Check HTTP headers in network tab |
| 2 | Backend receives request | Broadcasts WebSocket event, creates activity | Console log: "Agent Collaboration agent-a -> agent-b" |
| 3 | Check database | Activity record exists in `agent_activities` | Query: `SELECT * FROM agent_activities WHERE activity_type='agent_collaboration'` |
| 4 | Check dashboard | Edge from A->B animates cyan | Real-time visualization |
| 5 | Wait 6 seconds | Edge fades to gray | Animation stops |
| 6 | Click "Refresh" | Gray edge persists with count | Historical data loaded |

**Status**: Working (2025-12-19)

### Test Case 2: Historical Data Loading with Time Range Filter
**Objective**: Verify timeline API filters by time range

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Create 5 collaborations (Agent A -> B) | 5 activity records in database | Check `agent_activities` table |
| 2 | Open Dashboard | Shows "5 messages (24h)" in header | Default 24h range |
| 3 | Change filter to "1h" | Counter updates to show only recent | Query with 1h start_time |
| 4 | Change filter to "7d" | Counter shows all 5 | Query with 7d start_time |
| 5 | Verify edges | Gray edge shows "5x" label | Edge data.collaborationCount = 5 |

**Status**: Working (2025-12-19)

### Test Case 3: Access Control for Activities Timeline
**Objective**: Verify users only see activities for accessible agents

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | User A owns Agent A, Agent B | Timeline shows A and B activities | All activities visible |
| 2 | User B owns Agent C (not shared) | Timeline shows only C activities | A/B activities filtered out |
| 3 | User A shares Agent A with User B | Timeline now includes Agent A | Shared access granted |
| 4 | Admin user views timeline | Shows all activities | Admin bypass |

**Status**: Working (2025-12-19)

### Test Case 4: Parent-Child Activity Hierarchy
**Objective**: Verify activity hierarchy links correctly

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Agent A -> Agent B collaboration | Creates AGENT_COLLABORATION activity | `parent_activity_id = NULL` |
| 2 | Chat message processed | Creates CHAT_START activity | `parent_activity_id = collaboration_id` |
| 3 | Agent B uses Bash tool | Creates TOOL_CALL activity | `parent_activity_id = chat_id` |
| 4 | Query database | All three linked | Join on parent_activity_id |

**SQL Verification**:
```sql
SELECT
  a1.activity_type as parent,
  a2.activity_type as child,
  a3.activity_type as grandchild
FROM agent_activities a1
LEFT JOIN agent_activities a2 ON a2.parent_activity_id = a1.id
LEFT JOIN agent_activities a3 ON a3.parent_activity_id = a2.id
WHERE a1.activity_type = 'agent_collaboration'
```

**Status**: Working (2025-12-19)

### Test Case 5: Replay Mode
**Objective**: Verify replay functionality

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Switch to Timeline mode | View switches, live events shown | Visual confirmation |
| 2 | Click Play | Events replay with edge animations | Visual confirmation |
| 3 | Adjust speed to 20x | Playback speeds up | Events flow faster |
| 4 | Click timeline | Jumps to clicked position | Event index updates |
| 5 | Switch to Graph mode | Graph view displayed | Real-time updates work |

**Status**: Working (2025-12-19)

### Edge Cases

#### No Historical Data
- **Scenario**: New installation, no past collaborations
- **Expected**: Dashboard shows agents with no edges, "0 messages"
- **Behavior**: Works correctly, no errors

#### Single Collaboration Direction
- **Scenario**: Agent A -> B (50 times), never B -> A
- **Expected**: Single directed edge with "50x" label
- **Behavior**: Correct, edge remains unidirectional

#### Time Zone Handling
- **Scenario**: User in different timezone
- **Expected**: All timestamps stored as UTC, displayed in local time
- **Behavior**: Correct, ISO 8601 UTC timestamps in database

#### Large Time Range
- **Scenario**: Select "Last Week" with 1000+ collaborations
- **Expected**: API returns limit (500), pagination needed
- **Behavior**: Works, but no pagination UI yet (shows first 500)

#### Queue Full
- **Scenario**: Agent receives 4+ concurrent requests
- **Expected**: Returns 429 with queue status
- **Behavior**: Correct, client receives retry_after guidance

### Known Issues

1. **No Pagination UI**: Timeline API supports pagination but frontend does not implement
2. **No Export**: Historical data cannot be exported (CSV, JSON)
3. **No Filtering**: Cannot filter by source/target agent in UI
4. **No Analytics**: No aggregation, charts, or heatmaps

## Related Flows

### Upstream Flows
- **[Agent Chat](agent-chat.md)**: Chat endpoint handles X-Source-Agent header
- **[MCP Orchestration](mcp-orchestration.md)**: Agent-scoped API keys provide auth context

### Downstream Flows
- **[Agent Network](agent-network.md)**: Visualizes activity stream data on Dashboard
- **[Activity Monitoring](activity-monitoring.md)**: Real-time activity feed for single agent

## Future Enhancements

### Planned (Requirement 9.7 Extensions)
1. **Analytics Dashboard**: Collaboration frequency charts, most-connected agents
2. **Advanced Filtering**: Filter by source, target, time range, status
3. **Export**: CSV/JSON export for analysis
4. **Pagination**: Frontend pagination for large datasets
5. **Search**: Full-text search in activity details
6. **Notifications**: Alert on specific collaboration patterns
7. **Retention Policy**: Auto-delete activities older than 90 days

### Technical Debt
1. **No Unit Tests**: Add pytest tests for activity_service methods
2. **No Integration Tests**: Add tests for full collaboration flow
3. **Hard-coded Limit**: Make 500-record limit configurable
4. **No Caching**: Add Redis caching for frequent queries

## References

### Code Files
> Line numbers verified 2026-01-23
- `src/backend/routers/chat.py:106-416` - Chat endpoint with collaboration tracking
- `src/backend/routers/activities.py:15-55` - Timeline endpoint
- `src/backend/services/activity_service.py:46-244` - Activity service
- `src/backend/db/activities.py:40-185` - Activity database operations
- `src/frontend/src/stores/network.js:1-1283` - Network store (1283 lines total)
- `src/frontend/src/views/Dashboard.vue:1-751` - Dashboard (751 lines total)
- `src/mcp-server/src/client.ts:336-378` - MCP client chat method
- `src/mcp-server/src/tools/chat.ts:132-270` - MCP chat_with_agent tool

### Documentation
- **Requirements**: `docs/memory/requirements.md` REQ-9.7 (Activity Stream)
- **Architecture**: `docs/memory/architecture.md` Activity Tracking section
- **Related Flow**: `docs/memory/feature-flows/agent-network.md`

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-01-23 | **Full verification and update**: Verified all line numbers against current codebase. Updated chat.py references (endpoint now 106-416 with new execution queue, X-Via-MCP header support). Updated network.js references (now 1283 lines with execution stats, view mode state). Updated Dashboard.vue references (now 751 lines). Added triggered_by="mcp" for user MCP calls. Updated MCP client references (chat method now 336-378 with X-Via-MCP header). Noted error handling for collaboration activity completion on failure (Lines 401-407). |
| 2026-01-12 | **Polling interval optimization**: Context polling changed from 5s to 10s, agent refresh changed from 10s to 15s. Updated Frontend Optimization section. |
| 2025-12-30 | Line numbers verified. Updated activity service and database references. |
| 2025-12-19 | Initial documentation |
