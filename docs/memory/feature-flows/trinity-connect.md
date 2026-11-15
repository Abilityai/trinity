# Feature: Trinity Connect (Local-Remote Agent Sync)

> **Requirement ID**: SYNC-001
> **Status**: Implemented (2026-02-05)
> **Priority**: HIGH

## Overview

Trinity Connect enables real-time coordination between local Claude Code instances and Trinity-hosted agents. It provides an event-driven mechanism where remote agents can "wake up" local Claude Code when something happens.

## User Story

As a user running Claude Code locally, I want to receive real-time notifications when my Trinity agents complete tasks, so I can coordinate work between local and remote environments without polling.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     Event-Driven Local Agent Loop                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  1. Claude Code invokes: ./trinity-listen.sh                            │
│                              │                                           │
│                              ▼                                           │
│  2. Script connects to /ws/events?token=trinity_mcp_xxx                 │
│     Server validates MCP key, gets user's accessible agents              │
│                              │                                           │
│                              │  ← [Waits, receiving only filtered events]│
│                              │                                           │
│  3. Trinity Agent completes task ─────────────────────────►│            │
│     Server checks: Is this agent accessible to listener?   │            │
│     YES → Forward event                                    │            │
│                              │◄──────────────────────────────┘           │
│                              ▼                                           │
│  4. Script receives event, PRINTS it, EXITS                             │
│                              │                                           │
│                              ▼                                           │
│  5. Bash tool completes → Claude Code sees output                       │
│                              │                                           │
│                              ▼                                           │
│  6. Claude reacts to the event (processes work, responds, etc.)         │
│                              │                                           │
│                              ▼                                           │
│  7. Claude restarts listener: ./trinity-listen.sh                       │
│                              │                                           │
│                              └──────────► GOTO step 2                    │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Entry Points

- **Script**: `scripts/trinity-listen.sh` - Blocking event listener
- **API**: `WebSocket /ws/events?token=<MCP_API_KEY>` - Event stream endpoint

## Components

### 1. WebSocket Endpoint (`/ws/events`)

**Location**: `src/backend/main.py:370-435`

Dedicated WebSocket endpoint for external listeners with:
- MCP API key authentication via `?token=` query parameter
- Server-side event filtering based on user's accessible agents
- Ping/pong keepalive support
- Agent list refresh command
- A non-admin key's agent list follows access changes: it is re-read from the key owner's current account on an access change, about once a minute, and on `refresh` (trinity-enterprise#863; see [websocket-event-bus.md](websocket-event-bus.md))

**Protocol**:
```
Client → Server: ?token=trinity_mcp_xxx (in URL)
Server → Client: {"type": "connected", "user": "email", "accessible_agents": [...]}
Server → Client: {"type": "agent_activity", ...}  # Filtered events
Client → Server: "ping"
Server → Client: "pong"
Client → Server: "refresh"
Server → Client: {"type": "refreshed", "accessible_agents": [...]}
```

### 2. FilteredWebSocketManager

**Location**: `src/backend/main.py:107-167`

Manages filtered WebSocket connections:
- Tracks connections with user identity and accessible agents
- Filters events server-side before forwarding
- Extracts agent name from various event field formats
- Admin users see all events

```python
class FilteredWebSocketManager:
    connections: Dict[WebSocket, Dict]  # ws -> {email, is_admin, accessible_agents}

    async def connect(websocket, email, is_admin, accessible_agents)
    def disconnect(websocket)
    def update_accessible_agents(websocket, accessible_agents)
    async def broadcast_filtered(event: dict)
```

### 3. Database Method

**Location**: `src/backend/db/agents.py:467-497`

```python
def get_accessible_agent_names(user_email: str, is_admin: bool = False) -> List[str]
```

Returns list of agent names the user can access:
- Admin: All agents in the platform
- User: Owned agents + shared agents (via agent_sharing table)

### 4. Listener Script

**Location**: `scripts/trinity-listen.sh`

Blocking shell script that:
- Connects to `/ws/events` with MCP API key
- Filters events by agent name and/or state
- Prints matching event and exits
- Supports websocat or wscat WebSocket clients

**Usage**:
```bash
export TRINITY_API_KEY="trinity_mcp_xxx"
./trinity-listen.sh                    # All events
./trinity-listen.sh my-agent           # Specific agent
./trinity-listen.sh all completed      # Filter by state
./trinity-listen.sh my-agent completed # Both filters
```

## Event Types

Events are broadcast from multiple sources and filtered by the `/ws/events` endpoint:

| Event Type | Source | Trigger |
|------------|--------|---------|
| `agent_activity` | `activity_service.py:138` | Chat, task, tool call completions |
| `agent_started` | `routers/agents.py:324-329` | Agent container started |
| `agent_stopped` | `routers/agents.py:356-361` | Agent container stopped |
| `schedule_execution_completed` | Dedicated scheduler (`src/scheduler/`) | Scheduled task finished |
| `agent_collaboration` | `activity_service.py` | Agent-to-agent MCP call |

### Event Payloads

**agent_activity (completed)**:
```json
{
  "type": "agent_activity",
  "agent_name": "my-agent",
  "activity_id": "act_123",
  "activity_type": "chat_start",
  "activity_state": "completed",
  "details": {
    "context_used": 5000,
    "context_max": 200000,
    "cost_usd": 0.02
  }
}
```

**agent_started**:
```json
{
  "type": "agent_started",
  "name": "my-agent",
  "data": {
    "name": "my-agent",
    "trinity_injection": "success"
  }
}
```

**agent_stopped**:
```json
{
  "type": "agent_stopped",
  "name": "my-agent",
  "data": {"name": "my-agent"}
}
```

## Backend Layer

### Broadcast Hooks

Events are broadcast to both the main WebSocket manager (UI) and the filtered manager (Trinity Connect):

1. **Activity Service** (`activity_service.py:200-207`):
   - Main broadcast: `manager.broadcast(json.dumps(event))`
   - Filtered broadcast: `filtered_manager.broadcast_filtered(event)`

2. **Dedicated Scheduler Service** (`src/scheduler/service.py`):
   - Publishes events to Redis channel `scheduler:events`
   - Backend subscribes and relays to WebSocket managers
   - Main broadcast: via Redis pub/sub
   - Filtered broadcast: via Redis pub/sub to `filtered_manager.broadcast_filtered()`

3. **Agents Router** (`routers/agents.py:324-340, 356-368`):
   - Both broadcasts for agent_started/agent_stopped events

### Initialization

**Location**: `src/backend/main.py:172-188`

```python
# Inject filtered manager into routers and services
set_agents_filtered_ws_manager(filtered_manager)
activity_service.set_filtered_websocket_manager(filtered_manager)

# Subscribe to scheduler events from Redis and relay to filtered manager
# (Dedicated scheduler publishes to scheduler:events channel)
```

## Data Layer

### Access Control Query

```sql
-- Get accessible agent names for a user
SELECT DISTINCT agent_name FROM (
    SELECT ao.agent_name FROM agent_ownership ao
    JOIN users u ON ao.owner_id = u.id
    WHERE LOWER(u.email) = LOWER(?)
    UNION
    SELECT agent_name FROM agent_sharing
    WHERE LOWER(shared_with_email) = LOWER(?)
)
```

## Security Considerations

1. **Authentication**: MCP API key required - no anonymous access
2. **Server-Side Filtering**: Events filtered at server, not client - prevents information leakage
3. **Permission Model**: Uses existing `can_user_access_agent()` logic - owner, shared, or admin
4. **Key Rotation**: If key is compromised, revoke in Settings → API Keys
5. **No Sensitive Data**: Events contain metadata only, not actual content

## Error Handling

| Error | Code | Response |
|-------|------|----------|
| Missing token | 4001 | Close connection: "MCP API key required" |
| Invalid token | 4001 | Close connection: "Invalid or inactive MCP API key" |
| Connection drop | - | Client should reconnect |

## Testing

### Prerequisites
- [ ] Backend running at http://localhost:8000
- [ ] At least one agent created
- [ ] MCP API key created in Settings → API Keys
- [ ] websocat or wscat installed (`brew install websocat`)

### Test Steps

#### 1. WebSocket Authentication

**Action**: Connect with valid MCP key
```bash
export TRINITY_API_KEY="trinity_mcp_xxx"
websocat "ws://localhost:8000/ws/events?token=$TRINITY_API_KEY"
```

**Expected**: Receive connected message with accessible agents list
```json
{"type": "connected", "user": "user@example.com", "accessible_agents": ["agent-a", "agent-b"]}
```

#### 2. Authentication Failure

**Action**: Connect with invalid key
```bash
websocat "ws://localhost:8000/ws/events?token=invalid"
```

**Expected**: Connection closed with code 4001

#### 3. Event Filtering

**Action**:
1. Connect as user who owns agent-a but not agent-b
2. In another terminal, trigger execution on agent-a
3. Trigger execution on agent-b

**Expected**:
- Receive event for agent-a
- Do NOT receive event for agent-b

#### 4. Listener Script

**Action**:
```bash
export TRINITY_API_KEY="trinity_mcp_xxx"
./scripts/trinity-listen.sh my-agent completed &
# In another terminal: trigger a task on my-agent
```

**Expected**: Script prints event and exits

### Edge Cases
- [ ] Connection timeout after extended inactivity
- [ ] Agent name changes during connection (refresh command)
- [ ] Multiple listeners with same API key

**Last Tested**: 2026-02-05
**Status**: ✅ Implemented

## Related Features

- **MCP API Keys**: `docs/memory/feature-flows/mcp-api-keys.md`
- **Activity Stream**: `docs/memory/feature-flows/activity-stream.md`
- **Execution Queue**: `docs/memory/feature-flows/execution-queue.md`
- **Scheduling**: `docs/memory/feature-flows/scheduling.md`

## Files Changed

| File | Changes |
|------|---------|
| `src/backend/main.py` | Added FilteredWebSocketManager class, /ws/events endpoint |
| `src/backend/db/agents.py` | Added get_accessible_agent_names() method |
| `src/backend/database.py` | Exposed get_accessible_agent_names() |
| `src/backend/services/activity_service.py` | Added filtered broadcast calls |
| `src/scheduler/service.py` | Publishes events to Redis `scheduler:events` channel |
| `src/backend/routers/agents.py` | Added filtered broadcasts for agent_started/stopped |
| `scripts/trinity-listen.sh` | New listener script |

## Revision History

| Date | Changes |
|------|---------|
| 2026-02-11 | Updated to reflect scheduler consolidation - schedule events now via Redis pub/sub from dedicated scheduler |
| 2026-02-05 | Initial implementation |
