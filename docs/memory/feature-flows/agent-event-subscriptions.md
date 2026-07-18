# Feature: Agent Event Subscriptions (EVT-001)

## Overview
Lightweight pub/sub system enabling inter-agent event-driven pipelines. Agents emit named events, other agents subscribe and receive templated async tasks when matching events fire.

## User Story
As an agent operator, I want agents to subscribe to events from other agents so that multi-agent pipelines trigger automatically when upstream agents produce results.

## Entry Points
- **MCP Tool**: `emit_event` -- agent emits a named event with structured payload
- **MCP Tool**: `subscribe_to_event` -- agent subscribes to events from another agent
- **MCP Tool**: `list_event_subscriptions` -- agent lists its subscriptions
- **MCP Tool**: `delete_event_subscription` -- agent removes a subscription
- **API**: `POST /api/events` -- emit event (MCP auth determines source agent)
- **API**: `POST /api/agents/{name}/emit-event` -- emit event for a specific agent
- **API**: `POST /api/agents/{name}/event-subscriptions` -- create subscription
- **API**: `GET /api/agents/{name}/event-subscriptions` -- list subscriptions
- **API**: `GET /api/event-subscriptions/{id}` -- get subscription by ID
- **API**: `PUT /api/event-subscriptions/{id}` -- update subscription
- **API**: `DELETE /api/event-subscriptions/{id}` -- delete subscription
- **API**: `GET /api/agents/{name}/events` -- list events for agent
- **API**: `GET /api/events` -- list all events

## Frontend Layer
No dedicated UI components. This feature is consumed entirely through the MCP tool interface and REST API.

## MCP Layer

### Tool Registration
- `src/mcp-server/src/server.ts:268-272` -- 4 tools registered via `createEventTools()`

### Tool Definitions
- `src/mcp-server/src/tools/events.ts:18-242` -- `createEventTools()` factory

| MCP Tool | Client Method | API Call |
|----------|---------------|----------|
| `emit_event` (line 38) | `client.emitEvent()` | `POST /api/events` |
| `subscribe_to_event` (line 95) | `client.createEventSubscription()` | `POST /api/agents/{name}/event-subscriptions` |
| `list_event_subscriptions` (line 163) | `client.listEventSubscriptions()` | `GET /api/agents/{name}/event-subscriptions?direction=` |
| `delete_event_subscription` (line 216) | `client.deleteEventSubscription()` | `DELETE /api/event-subscriptions/{id}` |

### Client Methods
- `src/mcp-server/src/client.ts:886-962` -- 4 API wrapper methods

### Auth Context
- `subscribe_to_event` and `list_event_subscriptions` require `authContext.agentName` (agent-scoped MCP key)
- `emit_event` uses `POST /api/events` where source agent is determined server-side from `current_user.agent_name`

## Backend Layer

### Router
- `src/backend/routers/event_subscriptions.py` -- 9 endpoints, prefix `/api`
- Registered in `src/backend/main.py:454`

### Endpoints

| Method | Path | Handler | Line | Auth |
|--------|------|---------|------|------|
| POST | `/api/agents/{name}/event-subscriptions` | `create_event_subscription()` | 179 | `OwnedAgent` |
| GET | `/api/agents/{name}/event-subscriptions` | `list_event_subscriptions()` | 245 | `AuthorizedAgent` |
| GET | `/api/event-subscriptions/{id}` | `get_event_subscription()` | 283 | `get_current_user` |
| PUT | `/api/event-subscriptions/{id}` | `update_event_subscription()` | 298 | `get_current_user` + owner check |
| DELETE | `/api/event-subscriptions/{id}` | `delete_event_subscription()` | 335 | `get_current_user` + owner check |
| POST | `/api/events` | `emit_event()` | 366 | `get_current_user` |
| POST | `/api/agents/{name}/emit-event` | `emit_event_for_agent()` | 418 | `AuthorizedAgent` |
| GET | `/api/agents/{name}/events` | `list_agent_events()` | 464 | `AuthorizedAgent` |
| GET | `/api/events` | `list_all_events()` | 474 | `get_current_user` |

### Core Event Emission Flow (lines 366-415)
1. Determine source agent from `current_user.agent_name` (MCP key) or `current_user.username`
2. Validate `event_type` format: `^[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)*$`
3. Find matching enabled subscriptions via `db.find_matching_event_subscriptions(source_agent, event_type)`
4. Persist event to `agent_events` table
5. Fire-and-forget `asyncio.create_task(_trigger_subscription(...))` for each match
6. Broadcast event via WebSocket

### Subscription Trigger Flow (lines 97-154)
1. Interpolate `{{payload.field}}` placeholders in `target_message` using `_interpolate_template()`
2. Prepend event context: `[Event from {source}: {type}]`
3. POST to `http://localhost:8000/api/agents/{subscriber}/task` with:
   - `message`: interpolated template
   - `async_mode: true` (fire-and-forget)
   - `system_prompt`: event metadata
   - Headers: `Authorization` (internal JWT), `X-Source-Agent`, `X-Via-MCP`
4. Internal JWT generated via `_get_internal_token()` (5-minute TTL, admin subject)

### Template Interpolation (lines 55-76)
- Pattern: `{{payload.field}}` or `{{payload.nested.field}}`
- Regex: `\{\{(payload(?:\.[a-zA-Z0-9_]+)+)\}\}`
- Missing fields left as-is (no error)

### Permission Model (lines 200-211)
- On `create_event_subscription`: subscriber agent must have `agent_permissions` entry allowing it to call the source agent
- Self-subscription (subscriber == source) always allowed
- Check via `db.is_agent_permitted(subscriber, source)`

### Agent Deletion Cleanup
- `src/backend/routers/agents.py:380-384` -- deletes all subscriptions where agent is subscriber OR source

## Data Layer

### Pydantic Models
- `src/backend/db_models.py:1010-1057`

| Model | Purpose |
|-------|---------|
| `EventSubscriptionCreate` (line 1010) | Create request: `source_agent`, `event_type`, `target_message`, `enabled` |
| `EventSubscriptionUpdate` (line 1018) | Update request: optional `event_type`, `target_message`, `enabled` |
| `EventSubscription` (line 1025) | Full subscription record with `id`, `subscriber_agent`, `source_agent`, timestamps |
| `EventSubscriptionList` (line 1038) | List response: `count` + `subscriptions[]` |
| `AgentEvent` (line 1044) | Persisted event: `id`, `source_agent`, `event_type`, `payload`, `subscriptions_triggered` |
| `AgentEventList` (line 1054) | List response: `count` + `events[]` |

### Database Tables
- `src/backend/db/schema.py:606-630`

**`agent_event_subscriptions`** (line 607):
- `id` TEXT PK (format: `esub_{urlsafe_token}`)
- `subscriber_agent` TEXT NOT NULL
- `source_agent` TEXT NOT NULL
- `event_type` TEXT NOT NULL
- `target_message` TEXT NOT NULL
- `enabled` INTEGER DEFAULT 1
- `created_at`, `updated_at` TEXT NOT NULL
- `created_by` TEXT NOT NULL
- UNIQUE(`subscriber_agent`, `source_agent`, `event_type`)

**`agent_events`** (line 622):
- `id` TEXT PK (format: `evt_{urlsafe_token}`)
- `source_agent` TEXT NOT NULL
- `event_type` TEXT NOT NULL
- `payload` TEXT (JSON string)
- `subscriptions_triggered` INTEGER DEFAULT 0
- `created_at` TEXT NOT NULL

### Indexes (lines 760-764)
- `idx_event_subs_subscriber` on `subscriber_agent`
- `idx_event_subs_source` on `source_agent`
- `idx_event_subs_source_type` on `(source_agent, event_type)` -- used for matching
- `idx_events_source` on `(source_agent, created_at DESC)` -- used for listing
- `idx_events_type` on `event_type`

### Database Operations
- `src/backend/db/event_subscriptions.py` -- `EventSubscriptionOperations` class
- Delegated through `src/backend/database.py:1361-1389`

| Method | DB Operation |
|--------|-------------|
| `create_subscription()` (line 24) | INSERT into `agent_event_subscriptions` |
| `get_subscription()` (line 66) | SELECT by ID |
| `list_subscriptions()` (line 82) | SELECT with optional filters (subscriber, source, enabled) |
| `update_subscription()` (line 119) | UPDATE fields dynamically |
| `delete_subscription()` (line 163) | DELETE by ID |
| `delete_agent_subscriptions()` (line 174) | DELETE WHERE subscriber OR source = agent |
| `find_matching_subscriptions()` (line 189) | SELECT WHERE source_agent AND event_type AND enabled=1 |
| `create_event()` (line 211) | INSERT into `agent_events` |
| `list_events()` (line 249) | SELECT with optional source/type filters |

## Side Effects

### WebSocket Broadcast
- `src/backend/routers/event_subscriptions.py:79-94`
- Both `manager.broadcast()` and `filtered_manager.broadcast_filtered()` are called
- Managers injected at startup: `src/backend/main.py:207-208`

```json
{
  "type": "agent_event",
  "event_id": "evt_...",
  "source_agent": "oracle-1",
  "event_type": "prediction.resolved",
  "subscriptions_triggered": 2,
  "timestamp": "2026-03-26T..."
}
```

### Async Task Dispatch
- Each matching subscription triggers `POST /api/agents/{subscriber}/task` as fire-and-forget
- Uses `asyncio.create_task()` -- failures logged but do not block the emit response
- Internal auth via short-lived JWT (5-min TTL)

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Source agent not found | 400 | `Source agent '{name}' not found` |
| No permission to source | 403 | `Agent '{name}' does not have permission to communicate with '{source}'` |
| Invalid event_type format | 400 | `event_type must be a dot-separated identifier` |
| Duplicate subscription | 409 | `Subscription already exists for {sub} -> {source}:{type}` |
| Subscription not found | 404 | `Event subscription not found` |
| Not owner (update/delete) | 403 | `Only the owner can modify/delete event subscriptions` |
| Task trigger failure | (logged) | Warning logged, does not fail the emit response |

## Security

- **Subscription creation** requires `OwnedAgent` (owner access to subscribing agent)
- **Permission gate**: subscriber must have `agent_permissions` entry for source agent (except self-subscription)
- **Event emission** authenticated via JWT or MCP API key; source agent derived from auth context
- **Internal task dispatch** uses ephemeral JWT (admin, 5-min TTL) to avoid credential leakage
- **Update/Delete** restricted to subscription owner via `db.can_user_share_agent()`

## Testing

### Prerequisites
- Backend running at localhost:8000
- Two agents deployed with a permission link between them

### Test Steps

1. **Create subscription**
   ```bash
   curl -X POST http://localhost:8000/api/agents/agent-b/event-subscriptions \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"source_agent":"agent-a","event_type":"task.completed","target_message":"Process result: {{payload.result}}"}'
   ```
   **Expected**: 201 with subscription object

2. **Emit event**
   ```bash
   curl -X POST http://localhost:8000/api/agents/agent-a/emit-event \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"event_type":"task.completed","payload":{"result":"success"}}'
   ```
   **Expected**: 201 with `subscriptions_triggered: 1`
   **Verify**: agent-b receives async task with message `Process result: success`

3. **List subscriptions**
   ```bash
   curl http://localhost:8000/api/agents/agent-b/event-subscriptions?direction=subscriber \
     -H "Authorization: Bearer $TOKEN"
   ```
   **Expected**: 200 with subscription list

4. **Permission denied**
   ```bash
   # Without agent_permissions between agent-c and agent-a
   curl -X POST http://localhost:8000/api/agents/agent-c/event-subscriptions \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"source_agent":"agent-a","event_type":"task.completed","target_message":"test"}'
   ```
   **Expected**: 403

## Related Flows
- [agent-permissions.md](agent-permissions.md) -- Permission model gating subscriptions
- [agent-chat.md](agent-chat.md) -- Task endpoint used for subscription triggers
- [task-completion-events.md](task-completion-events.md) -- #1578 SYSTEM-emitted `agent.task.completed`/`failed` reusing this delivery machinery (deterministic backend producer, reserved `agent.task.*` namespace); the dispatch primitives (`trigger_subscription`) now live in `services/event_dispatch_service.py`
