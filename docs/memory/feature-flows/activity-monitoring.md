# Feature: Activity Monitoring (In-Memory Real-Time)

## Overview
Real-time visibility into Claude Code tool execution during agent chat sessions. Shows currently running tools, tool counts, timeline with durations, and drill-down details. This is an **in-memory, ephemeral** system optimized for live monitoring.

**Note**: As of 2025-12-02, this is complemented by the **Unified Activity Stream** (Req 9.7) which provides persistent database tracking. See `activity-stream.md` for the persistent layer.

## User Story
As an agent operator, I want to see what tools Claude is using in real-time so that I can understand agent behavior and debug issues.

## Comparison: In-Memory vs Persistent Activity Tracking

| Feature | Activity Monitoring (This Flow) | Unified Activity Stream |
|---------|--------------------------------|------------------------|
| Storage | In-memory (agent container) | Database (SQLite) |
| Persistence | Lost on container restart | Permanent |
| Scope | Current session only | All sessions, all time |
| Granularity | Tool-level with full I/O | Activity-level with metadata |
| Polling | 5-second HTTP polling | Database queries + WebSocket |
| Purpose | Real-time debugging | Analytics, audit, history |
| Access | Per-agent endpoint | Cross-agent timeline queries |
| Performance | Fast (in-memory) | Indexed queries |

## Entry Points
- **UI**: none on this path — `UnifiedActivityPanel.vue` was never integrated and was deleted in #2492 (the Operations page, #1109, is the live activity surface)
- **API**: `GET /api/agents/{name}/activity` - Poll for activity summary
- **API**: `GET /api/agents/{name}/activity/{tool_id}` - Get full tool call details
- **API**: `DELETE /api/agents/{name}/activity` - Clear session activity

**Note**: `UnifiedActivityPanel.vue` was never integrated into AgentDetail and was **deleted in #2492**. Activity polling still runs via the `useSessionActivity.js` composable; the sections below that describe the panel are a historical record.

---

## Frontend Layer

### Components

**UnifiedActivityPanel.vue** (`src/frontend/src/components/UnifiedActivityPanel.vue:1-312`)
- Status indicator with pulsing dot when running (line 11-16)
- Tool chips sorted by frequency, e.g., "Read x12, Bash x5" (line 57-67)
- Expanded timeline with timestamps and durations (line 70-134)
- Detail modal showing full input/output on click (line 136-205)
- Output truncation to 5000 chars (line 305-310)

**Component not currently used** - exists but not imported in AgentDetail.vue.

### Composable (`src/frontend/src/composables/useSessionActivity.js`)

**State** (lines 64-78):
```javascript
const sessionInfo = ref({
  context_tokens: 0,
  context_window: 200000,
  context_percent: 0,
  total_cost_usd: 0,
  message_count: 0
})

const sessionActivity = ref({
  status: 'idle',
  active_tool: null,
  tool_counts: {},
  timeline: [],
  totals: { calls: 0, duration_ms: 0, started_at: null }
})
```

**Polling Logic** (lines 115-125):
```javascript
const startActivityPolling = () => {
  loadSessionActivity() // Load immediately
  activityRefreshInterval = setInterval(loadSessionActivity, 5000) // Then every 5 seconds
}

const stopActivityPolling = () => {
  if (activityRefreshInterval) {
    clearInterval(activityRefreshInterval)
    activityRefreshInterval = null
  }
}
```

### Store Methods (`src/frontend/src/stores/agents.js`)

**getSessionActivity** (line 372-378):
```javascript
async getSessionActivity(name) {
  const authStore = useAuthStore()
  const response = await axios.get(`/api/agents/${name}/activity`, {
    headers: authStore.authHeader
  })
  return response.data
}
```

**getActivityDetail** (line 380-386):
```javascript
async getActivityDetail(name, toolId) {
  const authStore = useAuthStore()
  const response = await axios.get(`/api/agents/${name}/activity/${toolId}`, {
    headers: authStore.authHeader
  })
  return response.data
}
```

**clearSessionActivity** (line 388-393):
```javascript
async clearSessionActivity(name) {
  const authStore = useAuthStore()
  const response = await axios.delete(`/api/agents/${name}/activity`, {
    headers: authStore.authHeader
  })
  return response.data
}
```

### AgentDetail Integration (`src/frontend/src/views/AgentDetail.vue`)

**Composable Usage** (lines 406-413):
```javascript
// Session activity composable
const {
  sessionInfo,
  startActivityPolling,
  stopActivityPolling,
  loadSessionInfo,
  resetSessionActivity
} = useSessionActivity(agent, agentsStore)
```

**Polling Control** (lines 491-504):
```javascript
// Watch agent status for stats, activity, and git polling
watch(() => agent.value?.status, (newStatus) => {
  if (newStatus === 'running') {
    startStatsPolling()
    startActivityPolling()
    // ...
  } else {
    stopStatsPolling()
    stopActivityPolling()
    // ...
    resetSessionActivity()
  }
})
```

---

## Backend Layer (`src/backend/routers/chat.py`)

### Endpoints
| Lines | Endpoint | Purpose |
|-------|----------|---------|
| 742-784 | `GET /api/agents/{name}/activity` | Activity summary |
| 787-818 | `GET /api/agents/{name}/activity/{tool_id}` | Tool call details |
| 821-851 | `DELETE /api/agents/{name}/activity` | Clear activity |

### Proxy Pattern (lines 742-784)
```python
@router.get("/{name}/activity")
async def get_agent_activity(
    name: str,
    current_user: User = Depends(get_current_user)
):
    """Get session activity for real-time monitoring."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        return {
            "status": "idle",
            "active_tool": None,
            "tool_counts": {},
            "timeline": [],
            "totals": { "calls": 0, "duration_ms": 0, "started_at": None }
        }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://agent-{name}:8000/api/activity",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        return {"status": "idle", ...}  # Graceful fallback
```

---

## Agent Layer (`docker/base-image/agent_server/`)

### Module Structure
```
docker/base-image/agent_server/
  state.py              # AgentState class with session_activity
  routers/activity.py   # Activity API endpoints
  services/activity_tracking.py  # Tool tracking functions
  utils/helpers.py      # Input summary generation
```

### Session Activity State (`state.py:68-80`)
```python
def _create_empty_activity(self) -> Dict:
    """Create empty session activity structure"""
    return {
        "status": "idle",
        "active_tool": None,
        "tool_counts": {},
        "timeline": [],
        "totals": {
            "calls": 0,
            "duration_ms": 0,
            "started_at": None
        }
    }
```

Tool outputs stored separately for drill-down (`state.py:40`):
```python
self.tool_outputs: Dict[str, str] = {}
```

### Activity Router (`routers/activity.py`)

**GET /api/activity** (lines 11-23):
```python
@router.get("/api/activity")
async def get_session_activity():
    """Get session activity summary for real-time monitoring."""
    return agent_state.session_activity
```

**GET /api/activity/{tool_id}** (lines 26-50):
```python
@router.get("/api/activity/{tool_id}")
async def get_tool_call_detail(tool_id: str):
    """Get full details for a specific tool call."""
    for entry in agent_state.session_activity["timeline"]:
        if entry["id"] == tool_id:
            full_output = agent_state.tool_outputs.get(tool_id, entry.get("output_summary", ""))
            return {
                "id": entry["id"],
                "tool": entry["tool"],
                "input": entry["input"],
                "output": full_output,
                "duration_ms": entry["duration_ms"],
                "started_at": entry["started_at"],
                "ended_at": entry["ended_at"],
                "success": entry["success"]
            }
    raise HTTPException(status_code=404, detail=f"Tool call {tool_id} not found")
```

**DELETE /api/activity** (lines 53-66):
```python
@router.delete("/api/activity")
async def clear_session_activity():
    """Clear session activity (called when starting a new session)."""
    agent_state.session_activity = agent_state._create_empty_activity()
    agent_state.tool_outputs = {}
    return {"status": "cleared", "message": "Session activity cleared"}
```

### Activity Tracking Service (`services/activity_tracking.py`)

**start_tool_execution** (lines 11-51):
```python
def start_tool_execution(tool_id: str, tool: str, input_data: Dict[str, Any]):
    """Record start of a tool execution"""
    now = datetime.now()
    display_name = get_tool_name(tool, input_data)
    input_summary = get_input_summary(tool, input_data)

    agent_state.session_activity["status"] = "running"
    agent_state.session_activity["active_tool"] = {
        "name": display_name,
        "input_summary": input_summary,
        "started_at": now.isoformat()
    }
    # Timeline entry inserted at beginning (newest first)
    agent_state.session_activity["timeline"].insert(0, timeline_entry)
    agent_state.session_activity["tool_counts"][display_name] += 1
    agent_state.session_activity["totals"]["calls"] += 1
```

**complete_tool_execution** (lines 54-84):
```python
def complete_tool_execution(tool_id: str, success: bool, output: str = None):
    """Record completion of a tool execution"""
    now = datetime.now()
    for entry in agent_state.session_activity["timeline"]:
        if entry["id"] == tool_id and entry["status"] == "running":
            started_at = datetime.fromisoformat(entry["started_at"])
            duration_ms = int((now - started_at).total_seconds() * 1000)
            entry["ended_at"] = now.isoformat()
            entry["duration_ms"] = duration_ms
            entry["success"] = success
            entry["status"] = "completed"
            entry["output_summary"] = truncate_output(output) if output else None
            agent_state.session_activity["totals"]["duration_ms"] += duration_ms
            break

    if output:
        agent_state.tool_outputs[tool_id] = output  # Store for drill-down
    agent_state.session_activity["active_tool"] = None
```

### Input Summary Generation (`utils/helpers.py:58-98`)

| Tool | Summary Format |
|------|----------------|
| Read/Edit/Write | Shortened file path: `.../dir/file.py` |
| Glob | Pattern: `**/*.ts` |
| Grep | Pattern in quotes: `"pattern"` |
| Bash | First 50 chars of command |
| Task | Description or prompt (50 chars) |
| WebFetch | Hostname from URL |
| WebSearch | Query (40 chars) |
| MCP tools | First relevant parameter |

### Claude Code Integration (`services/claude_code.py`)

Tool tracking is called during stream-JSON parsing (lines 193, 235, 336, 379):
```python
# When tool_use block is parsed
start_tool_execution(tool_id, tool_name, tool_input)

# When tool_result block is parsed
complete_tool_execution(tool_id, not is_error, tool_output)
```

### ThreadPoolExecutor (`services/claude_code.py:28-30`)
```python
# Thread pool for running blocking subprocess operations
# This allows FastAPI to handle other requests (like /api/activity polling) during execution
_executor = ThreadPoolExecutor(max_workers=1)
```

---

## Data Structures

### SessionActivity Response
```json
{
  "status": "running",
  "active_tool": {
    "name": "Read",
    "input_summary": ".../main.py",
    "started_at": "2025-11-28T10:30:00.000Z"
  },
  "tool_counts": {
    "Read": 12,
    "Bash": 5,
    "Grep": 3
  },
  "timeline": [
    {
      "id": "tool_123",
      "tool": "Read",
      "input": {"file_path": "/path/to/file.py"},
      "input_summary": ".../file.py",
      "output_summary": "# First 500 chars...",
      "duration_ms": 45,
      "started_at": "2025-11-28T10:30:00.000Z",
      "ended_at": "2025-11-28T10:30:00.045Z",
      "success": true,
      "status": "completed"
    }
  ],
  "totals": {
    "calls": 20,
    "duration_ms": 5430,
    "started_at": "2025-11-28T10:30:00.000Z"
  }
}
```

### ToolCallDetail Response (drill-down)
```json
{
  "id": "tool_123",
  "tool": "Read",
  "input": {"file_path": "/path/to/file.py"},
  "output": "# Full file contents here...",
  "duration_ms": 45,
  "started_at": "2025-11-28T10:30:00.000Z",
  "ended_at": "2025-11-28T10:30:00.045Z",
  "success": true
}
```

---

## Side Effects
- **No WebSocket**: Uses polling (5-second interval)
- **No Database**: Activity stored in-memory on agent container
- **No Audit Log**: Activity is ephemeral

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | "Agent not found" |
| Agent not running | 400 | "Agent is not running" |
| Tool call not found | 404 | "Tool call {id} not found" |
| Agent unreachable | (returns empty) | Falls back to empty activity |

---

## Security Considerations

- Authorization via `get_current_user` dependency
- Agent container only accessible via Docker internal network
- Full tool outputs stored only in agent memory (not persisted)
- Output truncated to 5000 chars in drill-down modal (UnifiedActivityPanel.vue:305-310)

---

## Testing

### Manual Testing
```bash
# Start an agent
curl -X POST http://localhost:8000/api/agents/test-agent/start \
  -H "Authorization: Bearer $TOKEN"

# Send a chat message (triggers tool execution)
curl -X POST http://localhost:8000/api/agents/test-agent/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "List all files in the current directory"}'

# Poll activity while agent is working
curl http://localhost:8000/api/agents/test-agent/activity \
  -H "Authorization: Bearer $TOKEN"

# Get specific tool call details
curl http://localhost:8000/api/agents/test-agent/activity/tool_123 \
  -H "Authorization: Bearer $TOKEN"

# Clear activity
curl -X DELETE http://localhost:8000/api/agents/test-agent/activity \
  -H "Authorization: Bearer $TOKEN"
```

---

## Status
**Last Updated**: 2026-01-23
**Verified**: Line numbers verified against current codebase

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-01-23 | **Full verification**: Updated all line numbers. Documented that UnifiedActivityPanel exists but is NOT integrated into AgentDetail. Added detail on activity router, tracking service, and helpers module. Added store method line numbers. Clarified module structure. |
| 2026-01-12 | **Polling interval optimization**: Changed from 2-second to 5-second polling interval for reduced API load. Updated composable `useSessionActivity.js:117`. |
| 2025-12-30 | **Updated line numbers**: Backend chat.py activity endpoints now at lines 681-790 (moved due to code additions). Updated agent-server references from monolithic `agent-server.py` to modular `agent_server/` package structure. |
| 2025-12-02 | Initial documentation |

---

## Related Flows

- **Upstream**: Agent Terminal (`agent-terminal.md`) or scheduled executions - Triggers Claude Code execution
- **Parallel**: Unified Activity Stream (`activity-stream.md`) - Persistent database tracking (Req 9.7)
- **Downstream**: None (terminal display feature)

## Complementary Systems

This in-memory activity monitoring system works alongside the persistent activity stream:

1. **In-Memory (This Flow)**: Optimized for real-time UI updates, full tool I/O, current session only
2. **Persistent (activity-stream.md)**: Optimized for historical queries, analytics, cross-agent timelines

Both systems track the same tool executions but serve different purposes. The in-memory system is faster for live monitoring, while the persistent system enables long-term analysis and compliance.

## Known Limitations

1. **UI Not Integrated (resolved by deletion)**: `UnifiedActivityPanel.vue` was never imported by `AgentDetail.vue` and was deleted in #2492; the Operations page owns the activity UI.

2. **Data Loss on Restart**: All activity data is lost when the agent container restarts (by design - ephemeral).

3. **Single Session**: Only tracks the current session. Previous session activity is cleared on new session start.
