# Feature: Unified Activity Stream

## Overview
Centralized activity tracking system that persists all agent activities (chat sessions, tool calls, schedule executions) to SQLite with real-time WebSocket broadcasting. Provides granular observability, cross-agent timeline queries, and full audit trail with parent-child relationships for tool calls within chat sessions.

## User Story
As a platform operator, I want to track all agent activities in a unified system so that I can monitor performance, debug issues, analyze usage patterns, and build visualizations across multiple agents and time ranges.

## Entry Points
- **Backend Service**: `src/backend/services/activity_service.py:46-107` - `track_activity()` method
- **Backend Service**: `src/backend/services/activity_service.py:109-159` - `complete_activity()` method
- **API Endpoints**:
  - `GET /api/agents/{name}/activities` - Per-agent query
  - `GET /api/activities/timeline` - Cross-agent timeline query

---

## Architecture

### Design Decisions

**1. Dual Storage for Tool Calls**
- **chat_messages.tool_calls**: JSON array for fast access with chat history
- **agent_activities**: Granular rows for filtering, indexing, and timeline queries
- Rationale: Different access patterns require different structures

**2. Cost/Context in Existing Tables**
- Observability metrics stay in source tables (chat_messages, schedule_executions)
- Accessed via JOIN on related_chat_message_id / related_execution_id
- Rationale: Avoid data duplication, single source of truth

**3. Full Backward Compatibility**
- Existing APIs unchanged
- In-memory activity tracking still works
- New system runs in parallel
- Rationale: Zero-risk deployment

**4. Parent-Child Relationships**
- Tool calls link to parent chat via parent_activity_id
- Enables hierarchical queries and drill-down
- Rationale: Essential for understanding tool execution context

---

## Data Layer

### Database Schema (`src/backend/database.py:474-497`)

**Table: agent_activities**
```sql
CREATE TABLE IF NOT EXISTS agent_activities (
    id TEXT PRIMARY KEY,                           -- UUID
    agent_name TEXT NOT NULL,                      -- Which agent
    activity_type TEXT NOT NULL,                   -- chat_start, tool_call, schedule_start, etc.
    activity_state TEXT NOT NULL,                  -- started, completed, failed, cancelled (#1332)
    parent_activity_id TEXT,                       -- Link to parent (tool -> chat)
    started_at TEXT NOT NULL,                      -- ISO8601 timestamp
    completed_at TEXT,                             -- ISO8601 timestamp
    duration_ms INTEGER,                           -- Calculated on completion
    user_id INTEGER,                               -- FK to users table
    triggered_by TEXT NOT NULL,                    -- user, schedule, agent, system
    related_chat_message_id TEXT,                  -- FK to chat_messages
    related_execution_id TEXT,                     -- FK to schedule_executions
    details TEXT,                                  -- JSON: activity-specific metadata
    error TEXT,                                    -- Error message if failed
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (parent_activity_id) REFERENCES agent_activities(id),
    FOREIGN KEY (related_chat_message_id) REFERENCES chat_messages(id),
    FOREIGN KEY (related_execution_id) REFERENCES schedule_executions(id)
)
```

**Indexes** (`database.py:629-635`)
```sql
idx_activities_agent         ON (agent_name, created_at DESC)  -- Per-agent queries
idx_activities_type          ON (activity_type)                -- Filter by type
idx_activities_state         ON (activity_state)               -- Filter by state
idx_activities_user          ON (user_id)                      -- Per-user queries
idx_activities_parent        ON (parent_activity_id)           -- Hierarchy traversal
idx_activities_chat_msg      ON (related_chat_message_id)      -- JOIN to chat
idx_activities_execution     ON (related_execution_id)         -- JOIN to schedules
```

### Activity Types (`src/backend/models.py:123-145`)

```python
class ActivityType(str, Enum):
    # Chat activities
    CHAT_START = "chat_start"          # Chat message received
    CHAT_END = "chat_end"              # Chat completed (not yet used)
    TOOL_CALL = "tool_call"            # Tool execution (child of chat_start)

    # Schedule activities
    SCHEDULE_START = "schedule_start"  # Schedule execution started
    SCHEDULE_END = "schedule_end"      # Schedule completed (not yet used)

    # Collaboration activities
    AGENT_COLLABORATION = "agent_collaboration"  # Agent-to-agent communication

    # Execution control activities
    EXECUTION_CANCELLED = "execution_cancelled"  # Execution terminated by user

    # Future activity types (not yet implemented)
    FILE_ACCESS = "file_access"
    MODEL_CHANGE = "model_change"
    CREDENTIAL_RELOAD = "credential_reload"
    GIT_SYNC = "git_sync"
```

### Activity States (`models.py:147-151`)
```python
class ActivityState(str, Enum):
    STARTED = "started"      # Activity in progress
    COMPLETED = "completed"  # Successfully completed
    FAILED = "failed"        # Failed with error
    CANCELLED = "cancelled"  # User-cancelled terminal, distinct from FAILED (#1332)
```

> **#1332:** every activity close-site derives its terminal state from the shared
> `models.activity_state_for_terminal(status)` helper (SUCCESS→COMPLETED,
> CANCELLED→CANCELLED, else→FAILED) instead of a hard-coded
> `COMPLETED if success else FAILED`, so a user-cancelled execution reads as a
> distinct, non-failure terminal in activity-derived views.

---

## Service Layer

### ActivityService (`src/backend/services/activity_service.py`)

**Initialization** (lines 18-35)
```python
class ActivityService:
    def __init__(self):
        self.websocket_manager = None       # Set by main.py
        self.subscribers: List[Callable] = []  # Future extensibility

    def set_websocket_manager(self, manager):
        """Called from main.py:121 during app startup"""
        self.websocket_manager = manager
```

**Core Methods**

| Line | Method | Purpose |
|------|--------|---------|
| 46-107 | `track_activity()` | Create new activity, broadcast start |
| 109-159 | `complete_activity()` | Update completion, broadcast end |
| 161-163 | `get_current_activities()` | Query in-progress activities |
| 165-200 | `_broadcast_activity_event()` | WebSocket broadcasting |
| 202-212 | `_notify_subscribers()` | Extensibility for plugins |
| 214-244 | `_get_action_description()` | Human-readable descriptions |

> **Transport**: `websocket_manager.broadcast(...)` and
> `filtered_websocket_manager.broadcast_filtered(...)` are thin shims over the
> Redis Streams event bus introduced in #306 — events are durably logged and
> replayable on reconnect. See
> [websocket-event-bus.md](websocket-event-bus.md). The activity service's
> event shape is unchanged.

**Activity Creation Flow** (`activity_service.py:46-107`)
```python
async def track_activity(
    agent_name: str,
    activity_type: ActivityType,
    user_id: Optional[int] = None,
    triggered_by: str = "user",
    parent_activity_id: Optional[str] = None,
    related_chat_message_id: Optional[str] = None,
    related_execution_id: Optional[str] = None,
    details: Optional[Dict] = None
) -> str:
    # 1. Create database record
    activity = ActivityCreate(...)
    activity_id = db.create_activity(activity)

    # 2. Broadcast via WebSocket
    await self._broadcast_activity_event(
        agent_name=agent_name,
        activity_id=activity_id,
        activity_type=activity_type.value,
        activity_state="started",
        action=self._get_action_description(activity_type, details),
        details=details
    )

    # 3. Notify subscribers (future extensibility)
    await self._notify_subscribers({...})

    return activity_id
```

**Activity Completion Flow** (`activity_service.py:109-159`)
```python
async def complete_activity(
    activity_id: str,
    status: str = "completed",  # or "failed" / "cancelled" (#1332)
    details: Optional[Dict] = None,
    error: Optional[str] = None
) -> bool:
    # 1. Get existing activity
    activity = db.get_activity(activity_id)
    if not activity:
        return False

    # 2. Update database (calculates duration)
    success = db.complete_activity(activity_id, status, details, error)

    # 3. Broadcast completion
    await self._broadcast_activity_event(...)

    # 4. Notify subscribers
    await self._notify_subscribers({...})

    return success
```

---

## Integration Points

### Chat Integration (`src/backend/routers/chat.py`)

**Chat Start Tracking** (lines 220-232)
```python
# Track chat start activity
chat_activity_id = await activity_service.track_activity(
    agent_name=name,
    activity_type=ActivityType.CHAT_START,
    user_id=current_user.id,
    triggered_by="agent" if x_source_agent else ("mcp" if x_via_mcp else "user"),
    parent_activity_id=collaboration_activity_id,
    related_execution_id=task_execution_id,
    details={
        "message_preview": request.message[:100],
        "source_agent": x_source_agent,
        "execution_id": task_execution_id
    }
)
```

**Tool Call Tracking** (lines 298-313)
```python
# Track each tool call (granular tracking)
for tool_call in execution_log_simplified:
    await activity_service.track_activity(
        agent_name=name,
        activity_type=ActivityType.TOOL_CALL,
        user_id=current_user.id,
        triggered_by="agent" if x_source_agent else "user",
        parent_activity_id=chat_activity_id,  # Link to parent chat
        related_chat_message_id=assistant_message.id,
        related_execution_id=task_execution_id,
        details={
            "tool_name": tool_call.get("tool", "unknown"),
            "duration_ms": tool_call.get("duration_ms"),
            "success": tool_call.get("success", True)
        }
    )
```

**Chat Completion** (lines 316-328)
```python
# Track chat completion
await activity_service.complete_activity(
    activity_id=chat_activity_id,
    status="completed",
    details={
        "related_chat_message_id": assistant_message.id,
        "context_used": session_data.get("context_tokens"),
        "context_max": session_data.get("context_window"),
        "cost_usd": metadata.get("cost_usd"),
        "execution_time_ms": execution_time_ms,
        "tool_count": len(execution_log_simplified),
        "execution_id": task_execution_id
    }
)
```

**Error Handling** (lines 385-391)
```python
await activity_service.complete_activity(
    activity_id=chat_activity_id,
    status="failed",
    error=error_msg
)
```

**Execution Cancellation Tracking** (lines 1134-1146)
```python
# Track termination activity (new activity type)
await activity_service.track_activity(
    agent_name=name,
    activity_type=ActivityType.EXECUTION_CANCELLED,
    user_id=current_user.id,
    triggered_by="user",
    related_execution_id=task_execution_id,
    details={
        "execution_id": execution_id,
        "task_execution_id": task_execution_id,
        "status": result.get("status"),
        "returncode": result.get("returncode")
    }
)
```

### Schedule Integration (via Internal API)

> **Note (2026-02-11)**: The embedded scheduler (`src/backend/services/scheduler_service.py`) has been removed. Schedule activity tracking is now handled by the **Dedicated Scheduler Service** (`src/scheduler/`) which calls internal API endpoints.

**Internal API Endpoints** (used by dedicated scheduler):
- `POST /api/internal/activities/track` - Start tracking an activity
- `POST /api/internal/activities/{id}/complete` - Mark activity as completed/failed

**Schedule Start Tracking** (dedicated scheduler calls internal API):
```python
# Dedicated scheduler makes HTTP call to backend
response = await httpx.post(
    f"{BACKEND_URL}/api/internal/activities/track",
    json={
        "agent_name": schedule.agent_name,
        "activity_type": "schedule_start",
        "user_id": schedule.owner_id,
        "triggered_by": "schedule",  # or "manual" for manual triggers
        "related_execution_id": execution.id,
        "details": {
            "schedule_id": schedule.id,
            "schedule_name": schedule.name,
            "cron_expression": schedule.cron_expression
        }
    }
)
activity_id = response.json()["activity_id"]
```

**Schedule Completion** (dedicated scheduler calls internal API):
```python
await httpx.post(
    f"{BACKEND_URL}/api/internal/activities/{activity_id}/complete",
    json={
        "status": "completed",
        "details": {
            "context_used": task_response.metrics.context_used,
            "context_max": task_response.metrics.context_max,
            "cost_usd": task_response.metrics.cost_usd,
            "tool_count": len(execution_log) if execution_log else 0
        }
    }
)
```

**Schedule Failure** (dedicated scheduler calls internal API):
```python
await httpx.post(
    f"{BACKEND_URL}/api/internal/activities/{activity_id}/complete",
    json={
        "status": "failed",
        "error": error_msg
    }
)
```

**Backend Internal Router** (`src/backend/routers/internal.py`):
- Receives activity tracking requests from scheduler
- Calls `activity_service.track_activity()` and `activity_service.complete_activity()`
- No external authentication (internal Docker network only)

### Agent Collaboration Tracking (`src/backend/routers/chat.py:194-206`)

```python
# Track agent collaboration activity
collaboration_activity_id = await activity_service.track_activity(
    agent_name=x_source_agent,  # Activity belongs to source agent
    activity_type=ActivityType.AGENT_COLLABORATION,
    user_id=current_user.id,
    triggered_by="agent",
    related_execution_id=task_execution_id,
    details={
        "source_agent": x_source_agent,
        "target_agent": name,
        "action": "chat",
        "message_preview": request.message[:100]
    }
)
```

---

## API Layer

### Per-Agent Activity Query (`src/backend/routers/agents.py:576-599`)

**Endpoint**: `GET /api/agents/{agent_name}/activities`

**Parameters**:
- `activity_type` (optional): Filter by type (chat_start, tool_call, etc.)
- `activity_state` (optional): Filter by state (started, completed, failed, cancelled)
- `limit` (default: 100): Max activities to return

**Authorization**: Uses `AuthorizedAgentByName` dependency - owner, shared, or admin

**Response**:
```json
{
  "agent_name": "my-agent",
  "count": 25,
  "activities": [
    {
      "id": "uuid-123",
      "agent_name": "my-agent",
      "activity_type": "chat_start",
      "activity_state": "completed",
      "parent_activity_id": null,
      "started_at": "2025-12-02T10:30:00.000Z",
      "completed_at": "2025-12-02T10:30:05.234Z",
      "duration_ms": 5234,
      "user_id": 1,
      "triggered_by": "user",
      "related_chat_message_id": "msg-456",
      "related_execution_id": null,
      "details": {
        "message_preview": "Hello, list files...",
        "context_used": 2500,
        "context_max": 200000,
        "cost_usd": 0.003,
        "tool_count": 3
      },
      "error": null,
      "created_at": "2025-12-02T10:30:00.000Z"
    }
  ]
}
```

### Cross-Agent Timeline Query

Two endpoints are available:

**1. agents.py** (`routers/agents.py:602-634`)
- **Endpoint**: `GET /api/agents/activities/timeline`

**2. activities.py** (`routers/activities.py:15-55`)
- **Endpoint**: `GET /api/activities/timeline`

**Parameters**:
- `start_time` (optional): ISO8601 timestamp (e.g., "2025-12-02T10:00:00")
- `end_time` (optional): ISO8601 timestamp
- `activity_types` (optional): Comma-separated list (e.g., "chat_start,tool_call")
- `limit` (default: 100): Max activities to return

**Authorization**: Filters results to only agents user has access to

**Response**:
```json
{
  "count": 42,
  "start_time": "2025-12-02T10:00:00",
  "end_time": "2025-12-02T12:00:00",
  "activity_types": ["chat_start", "schedule_start"],
  "activities": [
    {
      "id": "uuid-789",
      "agent_name": "agent-1",
      "activity_type": "schedule_start",
      "activity_state": "completed",
      "started_at": "2025-12-02T11:45:00.000Z",
      "duration_ms": 12340,
      "details": {
        "schedule_name": "Daily Report",
        "cost_usd": 0.015
      }
    }
  ]
}
```

---

## WebSocket Events

### Activity Started Event (`activity_service.py:179-189`)

```json
{
  "type": "agent_activity",
  "agent_name": "my-agent",
  "activity_id": "uuid-123",
  "activity_type": "chat_start",
  "activity_state": "started",
  "action": "Processing: Hello, list files...",
  "timestamp": "2025-12-02T10:30:00.000Z",
  "details": {
    "message_preview": "Hello, list files..."
  },
  "error": null
}
```

### Activity Completed Event

```json
{
  "type": "agent_activity",
  "agent_name": "my-agent",
  "activity_id": "uuid-123",
  "activity_type": "chat_start",
  "activity_state": "completed",
  "action": "Completed: chat_start",
  "timestamp": "2025-12-02T10:30:05.234Z",
  "details": {
    "context_used": 2500,
    "context_max": 200000,
    "cost_usd": 0.003,
    "context": {
      "used": 2500,
      "max": 200000,
      "percentage": 1.25
    }
  },
  "error": null
}
```

### Tool Call Event

```json
{
  "type": "agent_activity",
  "agent_name": "my-agent",
  "activity_id": "uuid-456",
  "activity_type": "tool_call",
  "activity_state": "started",
  "action": "Using tool: Read",
  "timestamp": "2025-12-02T10:30:01.000Z",
  "details": {
    "tool_name": "Read",
    "duration_ms": null,
    "success": null
  },
  "error": null
}
```

### Frontend Event Handling (REFRESH-001 - Added 2026-02-27)

The Dashboard Timeline view handles `agent_activity` events to keep the timeline fresh.

**Handler** (`src/frontend/src/stores/network.js`):
```javascript
function handleActivityStatusChange(event) {
  // Refresh timeline when activity completes
  if (event.activity_state === 'completed') {
    fetchHistoricalCollaborations()
  }
}
```

**WebSocket Message Routing** (in `connectWebSocket()`):
```javascript
websocket.onmessage = (event) => {
  const data = JSON.parse(event.data)
  switch (data.type) {
    case 'agent_activity':
      handleActivityStatusChange(data)
      break
    case 'pong':
      // Heartbeat response - connection is alive
      break
    // ... other handlers
  }
}
```

**Purpose**:
- When activity completes, timeline refreshes to show updated execution bars
- Part of three-layer refresh solution for Dashboard Timeline:
  1. WebSocket heartbeat (prevents disconnection)
  2. Activity event handler (real-time updates)
  3. Fallback polling (safety net)

**Related Documentation**:
- [dashboard-timeline-view.md](dashboard-timeline-view.md) - Timeline Refresh Mechanisms section
- [agent-network.md](agent-network.md) - WebSocket Heartbeat and Timeline Activity Refresh sections

---

## Database Operations

### CRUD Methods (`src/backend/db/activities.py`)

| Line | Method | Purpose |
|------|--------|---------|
| 40-72 | `create_activity()` | Insert new activity, returns UUID |
| 74-116 | `complete_activity()` | Update state, duration, merge details |
| 118-126 | `get_activity()` | Get single activity by ID |
| 128-152 | `get_agent_activities()` | Query per-agent with filters |
| 154-185 | `get_activities_in_range()` | Cross-agent timeline query |
| 187-193 | `get_current_activities()` | Get in-progress activities |
| 19-38 | `_row_to_activity()` | Convert row to dict |

**Details Merging** (`db/activities.py:94-97`)
```python
# Merge existing details with new details
existing_details = json.loads(row[1]) if row[1] else {}
if details:
    existing_details.update(details)
```

**Duration Calculation** (`db/activities.py:89-92`)
```python
from utils.helpers import utc_now_iso, parse_iso_timestamp

started_at = parse_iso_timestamp(row[0])  # Timezone-aware UTC parsing
completed_at = parse_iso_timestamp(utc_now_iso())
duration_ms = int((completed_at - started_at).total_seconds() * 1000)
```

> **Timezone Note (2026-01-15)**: All timestamps use UTC with 'Z' suffix. See [Timezone Handling Guide](/docs/TIMEZONE_HANDLING.md) for details on using `utc_now_iso()`, `parse_iso_timestamp()` and frontend equivalents `parseUTC()`, `getTimestampMs()`.

---

## Query Patterns

### Get Chat Session with Tool Calls
```sql
-- Get chat activity with all child tool calls
SELECT a.*, tc.*
FROM agent_activities a
LEFT JOIN agent_activities tc ON tc.parent_activity_id = a.id
WHERE a.activity_type = 'chat_start'
  AND a.agent_name = 'my-agent'
  AND a.created_at > '2025-12-02T00:00:00'
ORDER BY a.created_at DESC, tc.started_at ASC
```

### Get Activities with Cost/Context
```sql
-- JOIN to chat_messages for observability data
SELECT a.*, cm.cost, cm.context_used, cm.context_max, cm.tool_calls
FROM agent_activities a
LEFT JOIN chat_messages cm ON a.related_chat_message_id = cm.id
WHERE a.activity_type = 'chat_start'
  AND a.agent_name = 'my-agent'
ORDER BY a.created_at DESC
```

### Get Schedule Execution History
```sql
-- JOIN to schedule_executions for full details
SELECT a.*, se.response, se.error, se.cost, se.tool_calls
FROM agent_activities a
JOIN schedule_executions se ON a.related_execution_id = se.id
WHERE a.activity_type = 'schedule_start'
  AND a.agent_name = 'my-agent'
ORDER BY a.started_at DESC
```

### Cross-Agent Activity Timeline
```sql
-- All activities across agents in time range
SELECT *
FROM agent_activities
WHERE created_at >= '2025-12-02T10:00:00'
  AND created_at <= '2025-12-02T12:00:00'
  AND activity_type IN ('chat_start', 'schedule_start')
ORDER BY created_at DESC
LIMIT 100
```

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | "Agent not found" |
| No access permission | 403 | "You don't have permission to access this agent" |
| Activity not found | 404 | Activity not found (complete_activity returns False) |
| Invalid time range | 400 | (Handled by datetime parsing) |
| Database error | 500 | Internal error |

---

## Security Considerations

1. **Authorization**: All queries check agent access - owner, shared user, or admin
2. **User Filtering**: Timeline query filters to only accessible agents
3. **No Sensitive Data**: Tool inputs/outputs NOT stored in activities (stored in chat_messages)
4. **Audit Compliance**: All activities have user_id, timestamp, and audit trail
5. **Rate Limiting**: Not implemented (consider for timeline queries)

---

## Performance Considerations

1. **Indexes**: 7 indexes for fast queries on common patterns
2. **JSON Details**: Stored as TEXT, parsed on read (acceptable for current scale)
3. **Limit Enforcement**: All queries use LIMIT (default 100)
4. **Pagination**: Not implemented (future enhancement)
5. **Archival**: No automatic cleanup (consider retention policy)

---

## Testing

**Prerequisites**:
- [ ] Backend running: `./scripts/deploy/start.sh`
- [ ] Database initialized with agent_activities table
- [ ] At least one running agent
- [ ] User authenticated

### 1. Verify Table Schema
**Action**:
```bash
sqlite3 ~/trinity-data/trinity.db "PRAGMA table_info(agent_activities)"
```

**Expected**:
- 15 columns: id, agent_name, activity_type, activity_state, parent_activity_id, started_at, completed_at, duration_ms, user_id, triggered_by, related_chat_message_id, related_execution_id, details, error, created_at

**Verify**:
- [ ] All columns present
- [ ] Correct types (TEXT, INTEGER)
- [ ] created_at has DEFAULT

### 2. Test Chat Activity Tracking
**Action**:
- Send chat message: `POST /api/agents/{name}/chat {"message": "List files"}`
- Wait for response

**Expected**:
- chat_start activity created
- tool_call activities created for each tool
- chat_start activity completed

**Verify**:
```bash
# Check activities were created
sqlite3 ~/trinity-data/trinity.db "SELECT * FROM agent_activities WHERE agent_name='my-agent' ORDER BY created_at DESC LIMIT 10"

# Verify tool calls link to parent
sqlite3 ~/trinity-data/trinity.db "SELECT id, activity_type, parent_activity_id FROM agent_activities WHERE agent_name='my-agent' ORDER BY created_at DESC"
```

- [ ] chat_start activity exists with status "completed"
- [ ] tool_call activities exist with parent_activity_id pointing to chat_start
- [ ] duration_ms calculated for completed activities
- [ ] details JSON contains expected fields

### 3. Test WebSocket Broadcasting
**Action**:
- Open browser console to network tab, filter WebSocket
- Send chat message
- Watch WebSocket frames

**Expected**:
- `{"type": "agent_activity", "activity_state": "started", "activity_type": "chat_start"}`
- `{"type": "agent_activity", "activity_state": "started", "activity_type": "tool_call"}`
- `{"type": "agent_activity", "activity_state": "completed"}`

**Verify**:
- [ ] Started events broadcast immediately
- [ ] Completed events include duration_ms
- [ ] Context percentage calculated if present
- [ ] Tool call events include tool_name

### 4. Test Per-Agent Query API
**Action**:
```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/agents/my-agent/activities?limit=10"
```

**Expected**:
```json
{
  "agent_name": "my-agent",
  "count": 10,
  "activities": [...]
}
```

**Verify**:
- [ ] Returns expected count
- [ ] Activities sorted by created_at DESC
- [ ] All fields populated
- [ ] details parsed as JSON

**Test Filters**:
```bash
# Filter by type
curl "http://localhost:8000/api/agents/my-agent/activities?activity_type=tool_call"

# Filter by state
curl "http://localhost:8000/api/agents/my-agent/activities?activity_state=completed"

# Combine filters
curl "http://localhost:8000/api/agents/my-agent/activities?activity_type=chat_start&activity_state=completed&limit=5"
```

- [ ] Type filter works
- [ ] State filter works
- [ ] Limit respected

### 5. Test Timeline Query API
**Action**:
```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/activities/timeline?start_time=2025-12-02T00:00:00&limit=20"
```

**Expected**:
```json
{
  "count": 20,
  "activities": [...]
}
```

**Verify**:
- [ ] Returns activities from all accessible agents
- [ ] Respects start_time filter
- [ ] Activities sorted by created_at DESC

**Test Access Control**:
- [ ] Admin sees all agents
- [ ] Regular user only sees owned/shared agents
- [ ] Unauthorized agent activities filtered out

### 6. Test Schedule Integration
**Action**:
- Create schedule: `POST /api/agents/{name}/schedules`
- Trigger manually: `POST /api/agents/{name}/schedules/{id}/trigger`
- Wait for completion

**Expected**:
- schedule_start activity created
- schedule_start activity completed with execution details

**Verify**:
```bash
sqlite3 ~/trinity-data/trinity.db "SELECT * FROM agent_activities WHERE activity_type='schedule_start' ORDER BY created_at DESC LIMIT 5"
```

- [ ] schedule_start activity exists
- [ ] related_execution_id links to schedule_executions
- [ ] details contains schedule_id, schedule_name, cron_expression
- [ ] cost and tool_count in completion details

### 7. Test Error Handling
**Action**:
- Send chat to stopped agent (should fail quickly)
- Check activity status

**Expected**:
- chat_start activity created
- chat_start activity marked as "failed" with error message

**Verify**:
- [ ] Failed activity has error field populated
- [ ] activity_state = "failed"
- [ ] WebSocket broadcast includes error

**Test Schedule Failure**:
- Create schedule with invalid agent
- Trigger execution

- [ ] schedule_start marked as "failed"
- [ ] error field contains error message

### 8. Test Agent Collaboration Tracking
**Action**:
- Send chat from one agent to another using X-Source-Agent header
- Check activities for both agents

**Expected**:
- agent_collaboration activity created on source agent
- chat_start activity created on target agent with parent_activity_id

**Verify**:
- [ ] Collaboration activity has source_agent and target_agent in details
- [ ] Parent-child relationship established correctly

### Edge Cases

**Large Result Sets**:
```bash
# Request more than 100 activities
curl "http://localhost:8000/api/agents/my-agent/activities?limit=500"
```
- [ ] Verify limit is respected (no more than limit returned)

**Parent-Child Relationships**:
```bash
# Verify tool calls link to correct parent
sqlite3 ~/trinity-data/trinity.db "
  SELECT a.id, a.activity_type, tc.activity_type, tc.parent_activity_id
  FROM agent_activities a
  LEFT JOIN agent_activities tc ON tc.parent_activity_id = a.id
  WHERE a.activity_type = 'chat_start'
  LIMIT 1
"
```
- [ ] Tool calls have correct parent_activity_id
- [ ] parent_activity_id is NULL for top-level activities

**Time Range Queries**:
```bash
# Query with no results
curl "http://localhost:8000/api/activities/timeline?start_time=2020-01-01T00:00:00&end_time=2020-01-02T00:00:00"
```
- [ ] Returns empty timeline: {"count": 0, "activities": []}

**Details Merging**:
- [ ] Verify complete_activity() merges new details with existing
- [ ] Test: Create activity with details, complete with additional details
- [ ] Check: Both sets of details present in final record

**Cleanup**:
```bash
# Optional: Clear test activities
sqlite3 ~/trinity-data/trinity.db "DELETE FROM agent_activities WHERE agent_name='test-agent'"
```

**Last Tested**: 2025-12-20
**Tested By**: Code Review Verification
**Status**: Verified
**Issues**: None known

---

## Related Flows

- **Upstream**:
  - Agent Chat (`agent-chat.md`) - Creates chat_start and tool_call activities
  - Scheduling (`scheduling.md`) - Creates schedule_start activities

- **Downstream**:
  - Activity Monitoring (`activity-monitoring.md`) - Uses in-memory tracking (parallel system)
  - Collaboration Dashboard (`collaboration-dashboard.md`) - Could visualize activity streams

- **Related**:
  - Persistent Chat Tracking (`persistent-chat-tracking.md`) - Shares database infrastructure
  - Agent Logs & Telemetry (`agent-logs-telemetry.md`) - Complementary observability

---

## Future Enhancements

1. **Pagination**: Cursor-based pagination for large result sets
2. **Aggregations**: Summary statistics, cost totals, tool usage reports
3. **Retention Policy**: Automatic archival/deletion of old activities
4. **Frontend UI**: Timeline visualization, activity explorer
5. **Real-time Dashboard**: WebSocket-powered live activity feed
6. **Export**: CSV/JSON export for analysis
7. **Webhooks**: Trigger external systems on activity events
8. **Rate Limiting**: Protect timeline endpoint from abuse
9. **Caching**: Redis cache for frequently accessed activities
10. **Archival**: Move old activities to separate table for performance

---

## Trinity Connect Integration (Added 2026-02-05)

Activity events are now broadcast to two WebSocket channels:

1. **Main WebSocket Manager** (`/ws`): All connected UI clients receive all events
2. **Filtered WebSocket Manager** (`/ws/events`): Trinity Connect clients receive only events for their accessible agents

### Dual Broadcast Pattern

**Location**: `src/backend/services/activity_service.py:200-207`

```python
# Broadcast to main UI WebSocket
await manager.broadcast(json.dumps(event))

# Broadcast to filtered Trinity Connect WebSocket (server-side filtering)
await filtered_manager.broadcast_filtered(event)
```

### FilteredWebSocketManager

**Location**: `src/backend/main.py:107-167`

- Tracks connections with user identity and accessible agents list
- Extracts agent name from various event field formats (`agent_name`, `name`, `agent`)
- Filters events server-side before forwarding to external listeners
- Admin users see all events

### Event Types Broadcast to Trinity Connect

| Event Type | Source | Agent Name Field |
|------------|--------|------------------|
| `agent_activity` | activity_service.py | `agent_name` |
| `agent_started` | routers/agents.py | `name` |
| `agent_stopped` | routers/agents.py | `name` |
| `schedule_execution_completed` | dedicated scheduler (src/scheduler/) | `agent` |
| `agent_collaboration` | activity_service.py | `agent_name` |

### Initialization

**Location**: `src/backend/main.py:172-188`

```python
# Inject filtered manager into activity service
activity_service.set_filtered_websocket_manager(filtered_manager)
```

### Related Documentation

- **Trinity Connect**: [trinity-connect.md](trinity-connect.md) - Full feature flow for `/ws/events` endpoint

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-02-27 | **Dashboard Timeline Refresh (REFRESH-001)**: Frontend now handles `agent_activity` WebSocket events via `handleActivityStatusChange()` in network.js. When activity completes (`activity_state: completed`), refreshes historical collaborations to update Timeline view. Part of three-layer refresh solution: heartbeat + event handler + fallback polling. See [dashboard-timeline-view.md](dashboard-timeline-view.md) and [agent-network.md](agent-network.md). |
| 2025-12-02 | Initial implementation |
| 2025-12-30 | Previous review |
| 2026-01-15 | Added timezone handling documentation |
| 2026-02-11 | **Scheduler Consolidation**: Updated Schedule Integration section. Embedded scheduler removed; activity tracking now via internal API endpoints (`/api/internal/activities/track`, `/api/internal/activities/{id}/complete`) called by dedicated scheduler. |
| 2026-01-23 | Updated line numbers for database.py (schema at lines 474-497, indexes at 629-635), models.py (ActivityType at line 123, ActivityState at line 147), db/activities.py (create_activity at line 40, complete_activity at line 74). Added EXECUTION_CANCELLED activity type. Verified integration points in chat.py. Added main.py:121 for websocket_manager initialization. |
| 2026-02-05 | **Trinity Connect Integration**: Added dual broadcast pattern - events now sent to both main WebSocket (`/ws`) and filtered WebSocket (`/ws/events`). Added FilteredWebSocketManager section, event types table, and initialization details. |

---

## Implementation Notes

**Date**: 2025-12-02
**Last Updated**: 2026-01-23
**Requirement**: 9.7 - Unified Activity Stream
**Implemented By**: Feature implementation commit
**Related Docs**:
- `docs/ACTIVITY_TRACKING_ARCHITECTURE.md`
- `docs/CHANGE_REQUIREMENTS_ACTIVITY_STREAM.md`
