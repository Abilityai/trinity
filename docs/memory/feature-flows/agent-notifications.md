# Feature: Agent Notifications (NOTIF-001)

## Overview
Enables agents to send structured notifications to the Trinity platform. Agents decide when and what to notify based on their own logic. Notifications are persisted to SQLite, broadcast in real-time via WebSocket, and queryable via REST API with filtering, acknowledgment, and dismissal capabilities.

## User Story
- As an **agent developer**, I want to instruct agents to send notifications under certain conditions so that important events are surfaced to users.
- As a **platform user**, I want to see a stream of notifications from my agents so that I know when something needs attention.
- As a **system operator**, I want to query notifications by agent, priority, or type so that I can monitor platform health.

## Entry Points
- **MCP Tool**: `send_notification` in `src/mcp-server/src/tools/notifications.ts:40-117`
- **UI**: `src/frontend/src/views/Operations.vue:95-106` - "Clear All" button on the Operations page Notifications tab (#1017)
- **API Endpoints**:
  - `POST /api/notifications` - Create notification (MCP clients)
  - `GET /api/notifications` - List with filters
  - `POST /api/notifications/dismiss-all` - Bulk-dismiss all non-dismissed notifications (#1017)
  - `GET /api/notifications/{id}` - Get single notification
  - `POST /api/notifications/{id}/acknowledge` - Acknowledge
  - `POST /api/notifications/{id}/dismiss` - Dismiss
  - `GET /api/agents/{name}/notifications` - Agent-specific list
  - `GET /api/agents/{name}/notifications/count` - Pending count

---

## Data Layer

### Database Schema (`src/backend/database.py:308-328`)

**Table: agent_notifications**
```sql
CREATE TABLE IF NOT EXISTS agent_notifications (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    notification_type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT,
    priority TEXT DEFAULT 'normal',
    category TEXT,
    metadata TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT
);

CREATE INDEX idx_notifications_agent ON agent_notifications(agent_name, created_at DESC);
CREATE INDEX idx_notifications_status ON agent_notifications(status);
CREATE INDEX idx_notifications_priority ON agent_notifications(priority);
```

### Field Definitions

| Field | Type | Description |
|-------|------|-------------|
| `id` | TEXT | UUID with `notif_` prefix, e.g., `notif_abc123xyz789` |
| `agent_name` | TEXT | Source agent name (from MCP auth context) |
| `notification_type` | TEXT | `alert`, `info`, `status`, `completion`, `question` |
| `title` | TEXT | Short summary (required, max 200 chars) |
| `message` | TEXT | Detailed message (optional) |
| `priority` | TEXT | `low`, `normal`, `high`, `urgent` |
| `category` | TEXT | Free-form grouping: `progress`, `anomaly`, `health`, `error`, etc. |
| `metadata` | TEXT | JSON object with structured data |
| `status` | TEXT | `pending`, `acknowledged`, `dismissed` |
| `created_at` | TEXT | ISO8601 timestamp with Z suffix |
| `acknowledged_at` | TEXT | When status changed from pending |
| `acknowledged_by` | TEXT | User ID who acknowledged |

### Pydantic Models (`src/backend/db_models.py:561-602`)

```python
class NotificationCreate(BaseModel):
    notification_type: str  # alert, info, status, completion, question
    title: str              # Required, max 200 chars
    message: Optional[str] = None
    priority: str = "normal"  # low, normal, high, urgent
    category: Optional[str] = None
    metadata: Optional[dict] = None

class Notification(BaseModel):
    id: str
    agent_name: str
    notification_type: str
    title: str
    message: Optional[str] = None
    priority: str = "normal"
    category: Optional[str] = None
    metadata: Optional[dict] = None
    status: str = "pending"
    created_at: str
    acknowledged_at: Optional[str] = None
    acknowledged_by: Optional[str] = None

class NotificationList(BaseModel):
    count: int
    notifications: List[Notification]

class NotificationAcknowledge(BaseModel):
    id: str
    status: str
    acknowledged_at: str
    acknowledged_by: str
```

---

## MCP Layer

### Tool: send_notification (`src/mcp-server/src/tools/notifications.ts:40-117`)

**Registration** (`src/mcp-server/src/server.ts:227-232`):
```typescript
const notificationTools = createNotificationTools(client, requireApiKey);
server.addTool(notificationTools.sendNotification);
```

**Parameters**:

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `notification_type` | enum | Yes | `alert`, `info`, `status`, `completion`, `question` |
| `title` | string | Yes | Short summary (max 200 chars) |
| `message` | string | No | Detailed explanation |
| `priority` | enum | No | `low`, `normal` (default), `high`, `urgent` |
| `category` | string | No | Free-form category for grouping |
| `metadata` | object | No | Any structured data |

**Example Call**:
```json
{
  "tool": "send_notification",
  "arguments": {
    "notification_type": "completion",
    "title": "Daily report generated",
    "message": "Processed 15,000 records. Report saved to content/reports/2026-02-20.pdf",
    "priority": "normal",
    "category": "progress",
    "metadata": {
      "records_processed": 15000,
      "output_path": "content/reports/2026-02-20.pdf"
    }
  }
}
```

**Response**:
```json
{
  "success": true,
  "notification_id": "notif_abc123xyz789",
  "agent_name": "research-agent",
  "created_at": "2026-02-20T10:30:00.000Z"
}
```

### API Client Method (`src/mcp-server/src/client.ts:750-780`)

```typescript
async createNotification(data: {
  notification_type: string;
  title: string;
  message?: string;
  priority?: string;
  category?: string;
  metadata?: Record<string, unknown>;
}): Promise<{
  id: string;
  agent_name: string;
  notification_type: string;
  title: string;
  // ... full Notification type
}> {
  return this.request("POST", "/api/notifications", data);
}
```

**Authentication Flow**:
1. MCP tool receives `context.session.mcpApiKey` from auth context
2. Creates user-scoped TrinityClient with API key as Bearer token
3. Calls `POST /api/notifications`
4. Backend extracts `agent_name` from MCP auth context via the User model (see NOTIF-003 fix below)

**Agent Attribution Flow (NOTIF-003)**:
The correct agent name attribution involves three layers:

1. **MCP Key Validation** (`src/backend/db/mcp_keys.py:190-236`):
   - `validate_mcp_api_key()` returns dict with `agent_name` field for agent-scoped keys
   - Returns `scope: "agent"` to indicate agent-scoped key

2. **User Model Hydration** (`src/backend/dependencies.py:147-154`):
   ```python
   # For agent-scoped keys, include the agent_name
   agent_name = mcp_key_info.get("agent_name") if mcp_key_info.get("scope") == "agent" else None
   return User(
       id=user["id"],
       username=user["username"],
       email=user.get("email"),
       role=user["role"],
       agent_name=agent_name  # Populated from MCP key info
   )
   ```

3. **Notification Router** (`src/backend/routers/notifications.py:114-117`):
   ```python
   # Get agent name from user context
   # For agent-scoped keys, current_user.agent_name is the agent sending the notification
   # For user-scoped keys, we fall back to username (for manual testing/admin use)
   agent_name = current_user.agent_name if current_user.agent_name else current_user.username
   ```

**User Model** (`src/backend/models.py:56-63`):
```python
class User(BaseModel):
    """Authenticated user."""
    id: int
    username: str
    email: Optional[str] = None
    role: str = "user"
    # For agent-scoped MCP API keys, this is the agent name
    agent_name: Optional[str] = None
```

---

## Backend Layer

### Router (`src/backend/routers/notifications.py`)

**Module Setup** (lines 1-47):
- Imports `get_current_user`, `AuthorizedAgent` dependencies, `get_accessible_agents` accessor, and `platform_audit_service`/`AuditEventType` (#1017)
- `DismissAllRequest` body model defined in-router (lines 26-28)
- Global variables for WebSocket managers (set during app startup)

**WebSocket Manager Injection** (lines 38-47):
```python
_websocket_manager = None
_filtered_websocket_manager = None

def set_websocket_manager(manager):
    global _websocket_manager
    _websocket_manager = manager

def set_filtered_websocket_manager(manager):
    global _filtered_websocket_manager
    _filtered_websocket_manager = manager
```

**Broadcast Helper** (lines 50-70):
```python
async def _broadcast_notification(notification: Notification):
    event = {
        "type": "agent_notification",
        "notification_id": notification.id,
        "agent_name": notification.agent_name,
        "notification_type": notification.notification_type,
        "title": notification.title,
        "priority": notification.priority,
        "category": notification.category,
        "timestamp": notification.created_at
    }
    # Broadcast to main WebSocket (all UI clients)
    if _websocket_manager:
        await _websocket_manager.broadcast(json.dumps(event))
    # Broadcast to filtered WebSocket (Trinity Connect clients)
    if _filtered_websocket_manager:
        await _filtered_websocket_manager.broadcast_filtered(event)
```

### Endpoints

| Line | Endpoint | Method | Description |
|------|----------|--------|-------------|
| 77-124 | `/api/notifications` | POST | Create notification |
| 127-181 | `/api/notifications` | GET | List with filters |
| 184-231 | `/api/notifications/dismiss-all` | POST | Bulk-dismiss all non-dismissed (#1017) |
| 234-247 | `/api/notifications/{id}` | GET | Get single |
| 250-279 | `/api/notifications/{id}/acknowledge` | POST | Acknowledge |
| 282-311 | `/api/notifications/{id}/dismiss` | POST | Dismiss |
| 318-345 | `/api/agents/{name}/notifications` | GET | Agent-specific list |
| 348-356 | `/api/agents/{name}/notifications/count` | GET | Pending count |

**Route registration order**: `/notifications/dismiss-all` is registered BEFORE the `/notifications/{notification_id}` routes so the literal path segment `dismiss-all` is never captured as a notification ID (Architectural Invariant #4).

**Create Notification** (`POST /api/notifications`, lines 77-124):
```python
@router.post("/notifications", response_model=Notification, status_code=201)
async def create_notification(
    data: NotificationCreate,
    current_user: User = Depends(get_current_user)
):
    # Validate notification_type (alert, info, status, completion, question)
    # Validate priority (low, normal, high, urgent)
    # Validate title length (max 200 chars)

    # Get agent name from user context (NOTIF-003 fix)
    # For agent-scoped keys, current_user.agent_name is the agent sending the notification
    # For user-scoped keys, we fall back to username (for manual testing/admin use)
    agent_name = current_user.agent_name if current_user.agent_name else current_user.username

    notification = db.create_notification(agent_name, data)
    await _broadcast_notification(notification)
    return notification
```

**List Notifications** (`GET /api/notifications`, lines 127-181):
- Query params: `agent_name`, `status`, `priority` (comma-separated), `limit` (1-500, default 50)
- Filters results to agents from `get_accessible_agents(current_user)`; explicit `agent_name` outside that set → 403
- Returns `NotificationList` with count and notifications array

**Dismiss All** (`POST /api/notifications/dismiss-all`, lines 184-231, #1017):
- Body: `DismissAllRequest` `{agent_name?: string}` (model defined in the router at lines 26-28)
- Dismisses ALL non-dismissed (pending + acknowledged) notifications in one SQL UPDATE — sets `status='dismissed'`, `acknowledged_at`, `acknowledged_by`
- Scoped via the **same** `get_accessible_agents(current_user)` accessor the GET list endpoint uses, so "what I see" == "what I can clear"
- Explicit `agent_name` outside the accessible set → 403; empty accessible set → no-op `{"dismissed": 0}` (tri-state contract enforced in the DB layer)
- Ignores priority/type filters by design (a "Clear All" button clears the whole feed, including rows beyond the loaded page)
- When `dismissed > 0`: audit-logged via `platform_audit_service.log(event_type=AuditEventType.NOTIFICATION, event_action="dismiss_all", ...)` with `{dismissed, agent_name}` details, and broadcasts ONE `notifications_cleared` WebSocket event (main manager only)
- Response: `{"dismissed": <count>}` (200)

**Agent Notifications** (`GET /api/agents/{name}/notifications`, lines 318-345):
- Uses `AuthorizedAgent` dependency (owner, shared, or admin required)
- Query params: `status`, `limit` (1-500, default 50)

### CRUD Operations (`src/backend/db/notifications.py`)

**Class: NotificationOperations** (lines 18-366)

| Line | Method | Description |
|------|--------|-------------|
| 21-76 | `create_notification()` | Insert new notification, return model |
| 78-102 | `get_notification()` | Get by ID, parse metadata JSON |
| 104-159 | `list_notifications()` | Query with filters, ORDER BY created_at DESC |
| 161-182 | `list_agent_notifications()` | Delegate to list_notifications |
| 184-221 | `acknowledge_notification()` | Update status to 'acknowledged' |
| 223-254 | `dismiss_notification()` | Update status to 'dismissed' |
| 256-298 | `dismiss_all()` | Bulk UPDATE of pending+acknowledged rows (#1017) |
| 300-317 | `delete_agent_notifications()` | Delete all for agent |
| 319-342 | `count_pending_notifications()` | Count pending with optional agent filter |
| 344-366 | `_row_to_notification()` | Convert SQLite row to Pydantic model |

**dismiss_all tri-state accessible-set contract** (`db/notifications.py:256-298`, #1017):
```python
def dismiss_all(self, dismissed_by, agent_name=None, accessible_agent_names=None) -> int:
    # accessible_agent_names: None = no filter; empty set = no-op (return 0);
    # non-empty = "AND agent_name IN (?,...)" SQL filter
```
- Targets `status IN ('pending', 'acknowledged')` — the visible feed shows both, so "Clear All" must clear both
- Optional `agent_name` adds a further `AND agent_name = ?` narrowing
- Returns `cursor.rowcount` (number of rows dismissed)
- Facade delegation: `database.py:1435-1439` `dismiss_all_notifications()`

**ID Generation** (line 35):
```python
notification_id = f"notif_{secrets.token_urlsafe(12)}"
```

**Metadata Serialization** (line 39):
```python
metadata_json = json.dumps(data.metadata) if data.metadata else None
```

### Database Delegation (`src/backend/database.py:1592-1617`)

```python
# Agent Notifications (delegated to db/notifications.py) - NOTIF-001

def create_notification(self, agent_name: str, data):
    return self._notification_ops.create_notification(agent_name, data)

def get_notification(self, notification_id: str):
    return self._notification_ops.get_notification(notification_id)

def list_notifications(self, agent_name=None, status=None, priority=None, limit=100):
    return self._notification_ops.list_notifications(agent_name, status, priority, limit)

# ... additional delegation methods
```

### Main.py Integration (`src/backend/main.py`)

**Import** (line 62):
```python
from routers.notifications import (
    router as notifications_router,
    set_websocket_manager as set_notifications_ws_manager,
    set_filtered_websocket_manager as set_notifications_filtered_ws_manager
)
```

**WebSocket Manager Setup** (lines 178-179):
```python
set_notifications_ws_manager(manager)
set_notifications_filtered_ws_manager(filtered_manager)
```

**Router Mount** (line 319):
```python
app.include_router(notifications_router)  # Agent Notifications (NOTIF-001)
```

---

## WebSocket Events

### Event Type: agent_notification

**Broadcast Pattern**:
1. **Main WebSocket** (`/ws`): All connected UI clients receive all notifications
2. **Filtered WebSocket** (`/ws/events`): Trinity Connect clients receive only notifications from accessible agents

**Event Structure**:
```json
{
  "type": "agent_notification",
  "notification_id": "notif_abc123xyz789",
  "agent_name": "research-agent",
  "notification_type": "completion",
  "title": "Analysis complete",
  "priority": "normal",
  "category": "progress",
  "timestamp": "2026-02-20T10:30:00.000Z"
}
```

**Filtered Broadcast** (`routers/notifications.py:68-70`):
```python
# Broadcast to filtered WebSocket (Trinity Connect clients)
if _filtered_websocket_manager:
    await _filtered_websocket_manager.broadcast_filtered(event)
```

The `FilteredWebSocketManager` (in `main.py`) extracts `agent_name` from the event and only forwards to clients who have access to that agent.

### Event Type: notifications_cleared (#1017)

Emitted once per successful dismiss-all (only when `dismissed > 0`), main WebSocket only (`routers/notifications.py:221-229`):
```json
{
  "type": "notifications_cleared",
  "data": {
    "count": 12,
    "agent_name": null
  }
}
```

A thin trigger: a count, and the agent when the clear was for one. With `agent_name: null`
the `/ws` scope filter has nothing to key on, so the event reaches every operator client;
it therefore names nobody (#3392). It is listed in `FLEET_LEVEL_ALLOWLIST`
(`tests/unit/test_ent467_ws_agent_scope.py`) for that reason.

Frontend handler (`src/frontend/src/utils/websocket.js:151-154`): routes `notifications_cleared` → `notificationsStore.fetchPendingCount()` + `fetchNotifications()` so every connected client's badge and loaded list refresh, not just the operator who clicked.

---

## Frontend Layer (#1017 — Clear All)

### State Management
- `src/frontend/src/stores/notifications.js:187-200` - `dismissAll(agentName = null)` action: `api.post('/api/notifications/dismiss-all', {agent_name: agentName})` via the shared `api.js` client (Invariant #7), then refetches `fetchNotifications()` + `fetchPendingCount()`. Unlike the per-id `bulkDismiss()` helper below it (one request per loaded row), one server-side call clears rows beyond the loaded page.

### Components
- `src/frontend/src/views/Operations.vue:95-106` - "Clear All" button on the Operations page tab strip (shared across Needs Response / Notifications / Resolved tabs; shown when `isOperatorTab && clearableCount > 0`, `data-testid="ops-clear-all"`)
- `Operations.vue:180-190` - `ConfirmDialog` (danger variant). The Notifications-tab message warns the action ignores the current type/priority filters and clears beyond the loaded page: "This dismisses every non-dismissed notification from your accessible agents — including any not shown by the current filters — for all operators of these agents." (lines 289-298)
- `Operations.vue:300-318` - `confirmClearAll()` dispatches per tab; the `notifications` branch calls `notificationsStore.dismissAll()` (line 308)

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Invalid notification_type | 400 | "Invalid notification_type. Must be one of: alert, info, status, completion, question" |
| Invalid priority | 400 | "Invalid priority. Must be one of: low, normal, high, urgent" |
| Title too long | 400 | "Title too long (max 200 characters)" |
| Title empty | 400 | "Title is required" (MCP tool) |
| Notification not found | 404 | "Notification not found" |
| Invalid status filter | 400 | "Invalid status. Must be: pending, acknowledged, or dismissed" |
| Invalid priority filter | 400 | "Invalid priorities: {invalid_list}" |
| dismiss-all with `agent_name` outside accessible set | 403 | "Access denied to agent" |
| dismiss-all with zero accessible agents | 200 | `{"dismissed": 0}` (no-op, not an error) |
| No MCP API key | Error | "MCP API key authentication required but no API key found in request context" (MCP tool) |

---

## Security Considerations

1. **Authentication Required**: All endpoints require valid JWT or MCP API key
2. **Agent Name Extraction** (NOTIF-003): For agent-scoped MCP keys, `agent_name` is extracted from `mcp_key_info` during authentication and stored in `User.agent_name` field. This ensures notifications are correctly attributed to the sending agent (cannot be spoofed).
3. **Authorization on Agent Endpoints**: `GET /api/agents/{name}/notifications` uses `AuthorizedAgent` dependency (owner, shared, or admin)
4. **WebSocket Filtering**: Filtered WebSocket only sends notifications for agents the client has access to
5. **Input Validation**: Title length, enum values, JSON metadata all validated server-side
6. **Dismiss-All Scoping** (#1017): Uses the same `get_accessible_agents()` accessor as the GET list endpoint, so the clearable set can never exceed the visible set. The DB layer enforces a tri-state contract (`None` = no filter, empty set = no-op, non-empty = SQL IN) so an empty accessible set never falls through to "dismiss everything". Successful bulk dismissals are audit-logged (`AuditEventType.NOTIFICATION` / `dismiss_all`).

---

## Testing

### Prerequisites
- [ ] Backend running: `./scripts/deploy/start.sh`
- [ ] Database initialized with `agent_notifications` table
- [ ] At least one running agent with MCP API key
- [ ] Valid MCP API key (create via Settings > MCP API Keys)

### 1. Verify Table Schema
**Action**:
```bash
sqlite3 ~/trinity-data/trinity.db "PRAGMA table_info(agent_notifications)"
```

**Expected**:
- 12 columns: id, agent_name, notification_type, title, message, priority, category, metadata, status, created_at, acknowledged_at, acknowledged_by

**Verify**:
- [ ] All columns present with correct types

### 2. Test MCP Tool (send_notification)
**Action**:
- In Claude Code with MCP server connected:
```
Use the send_notification tool with:
- notification_type: "completion"
- title: "Test notification"
- priority: "normal"
```

**Expected**:
```json
{
  "success": true,
  "notification_id": "notif_...",
  "agent_name": "...",
  "created_at": "..."
}
```

**Verify**:
```bash
sqlite3 ~/trinity-data/trinity.db "SELECT * FROM agent_notifications ORDER BY created_at DESC LIMIT 1"
```
- [ ] Notification created with correct fields

### 3. Test WebSocket Broadcast
**Action**:
- Open browser console, connect to WebSocket at `/ws`
- Send a notification via MCP tool
- Watch WebSocket frames

**Expected**:
```json
{"type": "agent_notification", "notification_id": "...", "agent_name": "...", ...}
```

**Verify**:
- [ ] Event received in WebSocket
- [ ] All expected fields present

### 4. Test REST API Endpoints
**Action**:
```bash
# List all
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/notifications"

# List with filters
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/notifications?status=pending&priority=high,urgent"

# Get single
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/notifications/notif_xxx"

# Acknowledge
curl -X POST -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/notifications/notif_xxx/acknowledge"

# Dismiss
curl -X POST -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/notifications/notif_xxx/dismiss"
```

**Verify**:
- [ ] List returns correct count and notifications
- [ ] Filters work correctly
- [ ] Acknowledge changes status to "acknowledged"
- [ ] Dismiss changes status to "dismissed"

### 4b. Test Dismiss All (#1017)
**Action**:
```bash
# Dismiss everything (all accessible agents)
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{}' "http://localhost:8000/api/notifications/dismiss-all"

# Dismiss for one agent only
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_name": "my-agent"}' "http://localhost:8000/api/notifications/dismiss-all"
```

**Verify**:
- [ ] Returns `{"dismissed": N}` where N = pending + acknowledged rows for accessible agents
- [ ] Second identical call returns `{"dismissed": 0}` (idempotent)
- [ ] `agent_name` for an inaccessible agent returns 403
- [ ] `notifications_cleared` WebSocket event observed on `/ws` (only when N > 0)
- [ ] Audit row: `sqlite3 ~/trinity-data/trinity.db "SELECT event_action, details FROM audit_log WHERE event_action='dismiss_all' ORDER BY id DESC LIMIT 1"`

**Automated tests**: `tests/test_ops_clear_all.py` — `TestDismissAllNotifications` (happy path + idempotency exercised at the DB layer because the `get_accessible_agents` accessor is Docker-backed; auth-required, foreign-agent 403, and zero-agent no-op covered at the API level).

### 5. Test Agent-Specific Endpoints
**Action**:
```bash
# Get agent notifications
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/agents/my-agent/notifications"

# Get pending count
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/agents/my-agent/notifications/count"
```

**Verify**:
- [ ] Returns only notifications for specified agent
- [ ] Count matches actual pending notifications

### Edge Cases

**Title Validation**:
```bash
# Too long title (>200 chars)
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"notification_type":"info","title":"'"$(printf 'x%.0s' {1..250})"'"}' \
  "http://localhost:8000/api/notifications"
```
- [ ] Returns 400 with "Title too long" message

**Invalid Enum Values**:
```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"notification_type":"invalid","title":"Test"}' \
  "http://localhost:8000/api/notifications"
```
- [ ] Returns 400 with valid options listed

**Cleanup**:
```bash
sqlite3 ~/trinity-data/trinity.db "DELETE FROM agent_notifications WHERE title LIKE 'Test%'"
```

**Last Tested**: Not yet tested
**Status**: Pending verification

---

## Related Flows

- **Upstream**:
  - Agent Terminal (`agent-terminal.md`) - Agents run tools that may trigger notifications
  - Scheduling (`scheduling.md`) - Scheduled tasks may send completion notifications
  - Agent Collaboration (`agent-to-agent-collaboration.md`) - Collaboration events may generate notifications

- **Downstream**:
  - Trinity Connect (`trinity-connect.md`) - Filtered WebSocket receives notification events
  - Activity Stream (`activity-stream.md`) - Similar WebSocket broadcast pattern

- **Related**:
  - MCP Orchestration (`mcp-orchestration.md`) - send_notification is one of 44+ MCP tools
  - MCP API Keys (`mcp-api-keys.md`) - Authentication for MCP tool calls

---

## Future Enhancements (Out of Scope)

1. **UI for Viewing Notifications** - Dashboard panel or dedicated page
2. **Email/Slack/Webhook Forwarding** - External notification delivery
3. **Notification Routing Rules** - Filter and route by type/priority
4. **Auto-Dismiss/Retention Policies** - Automatic cleanup of old notifications
5. **Badge/Indicator in NavBar** - Visual notification count

---

## Implementation Notes

**Date**: 2026-02-20
**Requirement**: NOTIF-001 - Agent Notifications
**Spec Document**: `docs/requirements/AGENT_NOTIFICATIONS.md`
**Related Docs**:
- `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` (agent instructions template)

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-06-11 | **#1017 Dismiss All**: New `POST /api/notifications/dismiss-all` (registered before `/{notification_id}` routes) bulk-dismisses pending + acknowledged notifications scoped to the caller's accessible agents in one SQL UPDATE. DB layer `db/notifications.py:dismiss_all()` with tri-state accessible-set contract; facade `database.py:dismiss_all_notifications`. Audit event `NOTIFICATION/dismiss_all` + WS event `notifications_cleared`. Frontend: `stores/notifications.js:dismissAll()`, `utils/websocket.js` refresh handler, Clear All button + ConfirmDialog on Operations → Notifications tab. Tests: `tests/test_ops_clear_all.py::TestDismissAllNotifications`. |
| 2026-02-20 | **NOTIF-003 Bug Fix**: Fixed agent attribution - notifications now correctly show agent name instead of API key owner. Root cause: `agent_name` from agent-scoped MCP keys was not being passed to `User` model in `get_current_user()`. Fix: Added `agent_name` field to User model (models.py:62-63), updated `get_current_user()` to extract from mcp_key_info (dependencies.py:147-154), simplified notification router logic (notifications.py:106-109). |
| 2026-02-20 | Initial implementation (Phase 1: Backend + MCP Tool) |
