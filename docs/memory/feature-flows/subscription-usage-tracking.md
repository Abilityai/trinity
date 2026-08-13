# Feature: Subscription Usage Tracking

## Overview
Per-subscription non-billing activity across two rolling windows (5h and 7d), covering both chat messages and scheduled task executions. Allowance utilization is a separate provider-backed signal: if Trinity cannot obtain a reliable percentage/window/reset, the API and UI return an explicit unavailable state instead of substituting token totals or Claude Code's dollar estimate.

## User Story
As a platform admin, I want to see which named Claude subscription funds each agent and its provider-backed allowance utilization when available, without mistaking an estimated dollar value for subscription spend.

## Entry Points
- **API**: `GET /api/subscriptions/{subscription_id}/usage`
- **UI**: agent header, Dashboard timeline/grid/list, and Settings subscription table. Accepts subscription UUID or name as the API path parameter.

## Backend Layer

### Endpoint
- `src/backend/routers/subscriptions.py` - `get_subscription_usage()`
  - Admin-only (`assert_admin` after authentication)
  - Resolves path param by UUID first, falls back to name lookup
  - Returns `SubscriptionUsage` Pydantic model

```
GET /api/subscriptions/{subscription_id}/usage
Authorization: Bearer <admin_token>
```

Response shape:
```json
{
  "billing_mode": "subscription",
  "provider": "anthropic",
  "subscription_id": "<uuid>",
  "subscription_name": "studio-max",
  "subscription_type": "max",
  "utilization": {
    "status": "unavailable",
    "percent": null,
    "window": null,
    "resets_at": null,
    "last_updated_at": null,
    "reason": "provider_signal_unavailable"
  },
  "window_5h": {
    "input_tokens": 12000,
    "output_tokens": 4500,
    "message_count": 37
  },
  "window_7d": {
    "input_tokens": 210000,
    "output_tokens": 75000,
    "message_count": 512
  },
  "agents": ["agent-a", "agent-b"]
}
```

### Resolution Logic
1. Try `db.get_subscription(subscription_id)` by UUID
2. If not found, try `db.get_subscription_by_name(subscription_id)`
3. If still not found, raise 404
4. Call `db.get_subscription_usage(...)` with the resolved UUID and public subscription identity

### Business Logic
- `agents` field reflects **current** assignments from `agent_ownership`, not historical data
- Usage windows query historical records via the snapshotted `subscription_id` column — correct even when agents switch subscriptions between queries

### Provider Signal Limitation
Claude setup-token authentication currently gives Trinity no reliable allowance counter. An `available` utilization state requires an authenticated provider signal that supplies, at minimum, percentage consumed, the named allowance window, observation time, and reset time when defined. Local token totals, rate-limit errors, and Claude Code dollar estimates are not valid substitutes; until that signal exists Trinity returns `provider_signal_unavailable`.

## Data Layer

### Schema Changes (migration #31: `subscription_usage_tracking`)
- `src/backend/db/schema.py`
- `src/backend/db/migrations.py:900` - `_migrate_subscription_usage_tracking()`

New columns added via `ALTER TABLE`:

| Table | Column | Type | Purpose |
|-------|--------|------|---------|
| `chat_messages` | `subscription_id` | TEXT | Subscription active when message was recorded |
| `chat_messages` | `output_tokens` | INTEGER | Output token count from agent response metadata |
| `chat_sessions` | `subscription_id` | TEXT | Subscription active when session was created |
| `schedule_executions` | `subscription_id` | TEXT | Subscription active when execution was created |

New indexes:
- `idx_chat_messages_subscription ON chat_messages(subscription_id, timestamp)`
- `idx_executions_subscription ON schedule_executions(subscription_id, started_at)`

### Usage Query
- `src/backend/db/subscriptions.py:606` - `SubscriptionOperations.get_subscription_usage()`
- `src/backend/database.py:1137` - delegation wrapper

Two windows computed by `_query_window(cutoff)`:

**Chat messages** (assistant role only):
```sql
SELECT
    COALESCE(SUM(context_used), 0)   AS input_tokens,
    COALESCE(SUM(output_tokens), 0)  AS output_tokens,
    COUNT(*)                          AS message_count
FROM chat_messages
WHERE subscription_id = ?
  AND role = 'assistant'
  AND timestamp >= ?
```

**Schedule executions** (terminal states only, no separate output_tokens):
```sql
SELECT
    COALESCE(SUM(context_used), 0) AS input_tokens,
    COUNT(*)                        AS exec_count
FROM schedule_executions
WHERE subscription_id = ?
  AND started_at >= ?
  AND status NOT IN ('running', 'pending')
```

Totals are summed: `input_tokens = chat.input_tokens + exec.input_tokens`, `message_count = chat.message_count + exec.exec_count`.

### Pydantic Models
- `src/backend/db_models.py` - `SubscriptionUsageWindow` (input_tokens, output_tokens, message_count)
- `src/backend/db_models.py` - discriminated agent usage models plus `SubscriptionUsage` (identity, utilization, activity windows, agents)

## Subscription ID Snapshot Strategy

The `subscription_id` is written at **record creation time**, not resolved at query time. This is intentional: an agent's subscription may be reassigned dynamically (SUB-003 auto-switch), so querying `agent_ownership.subscription_id` at read time would misattribute historical usage.

### Chat path (`src/backend/routers/chat.py`)
1. At request start, call `db.get_agent_subscription_id(agent_name)` — a lightweight single-column read
2. Store result as `_exec_subscription_id` / `_chat_subscription_id`
3. Pass `subscription_id` to `db.create_task_execution()` (execution record)
4. Pass `subscription_id` to `db.get_or_create_chat_session()` (session record — new sessions only)
5. Pass `subscription_id` and `output_tokens=metadata.get("output_tokens")` to `db.add_chat_message()` (assistant message record)

### Task execution service path (`src/backend/services/task_execution_service.py:178`)
When `execution_id` is not pre-provided by the caller:
1. Use caller-supplied `subscription_id` if present, otherwise call `db.get_agent_subscription_id(agent_name)` (best-effort, swallows exceptions)
2. Pass `subscription_id` to `db.create_task_execution()`

### Schedule execution path (`src/backend/db/schedules.py`)
- `create_task_execution()` at line ~450 accepts `subscription_id` parameter
- `create_schedule_execution()` at line ~520 accepts `subscription_id` parameter
- Both write it directly to `schedule_executions.subscription_id`

## Database Operations Summary

| Operation | File | Function | What changes |
|-----------|------|----------|--------------|
| Read subscription ID for agent | `db/subscriptions.py:450` | `get_agent_subscription_id()` | SELECT from `agent_ownership` |
| Write to chat_messages | `db/chat.py:105` | `add_chat_message()` | INSERT with `subscription_id`, `output_tokens` |
| Write to chat_sessions | `db/chat.py:60` | `get_or_create_chat_session()` | INSERT with `subscription_id` (new sessions) |
| Write to schedule_executions | `db/schedules.py:450` | `create_task_execution()` | INSERT with `subscription_id` |
| Write to schedule_executions | `db/schedules.py:520` | `create_schedule_execution()` | INSERT with `subscription_id` |
| Query usage windows | `db/subscriptions.py:606` | `get_subscription_usage()` | SELECT aggregates from both tables |

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Subscription not found by ID or name | 404 | "Subscription not found" |
| Non-admin caller | 403 | "Admin access required" |
| Usage query exception | 500 | "Failed to retrieve usage data" |
| `get_agent_subscription_id()` failure at record time | — | Swallowed silently; `subscription_id` stored as NULL |

## Side Effects
None. This is a read-only analytics endpoint. No WebSocket broadcasts, no activity tracking.

## Testing

### Prerequisites
- Backend running
- Admin token obtained via `POST /api/token`
- At least one subscription registered and assigned to an agent
- Agent has sent at least one chat message or completed at least one execution

### Test Steps

1. **Action**: `GET /api/subscriptions/{id}/usage` with admin token
   **Expected**: 200 with `window_5h` and `window_7d` populated
   **Verify**: `message_count` matches number of assistant messages attributed to subscription

2. **Action**: `GET /api/subscriptions/{name}/usage` using subscription name instead of UUID
   **Expected**: Same 200 response (name resolution fallback)

3. **Action**: `GET /api/subscriptions/nonexistent/usage`
   **Expected**: 404 "Subscription not found"

4. **Action**: Same request with non-admin token
   **Expected**: 403 "Admin access required"

5. **Action**: Check `chat_messages` table after a chat interaction
   **Expected**: `subscription_id` column populated on assistant message row; `output_tokens` populated with value from agent metadata

6. **Action**: Reassign agent to a different subscription, send another message
   **Expected**: New message row has new `subscription_id`; old messages retain original `subscription_id`

## Related Flows
- [subscription-management.md](feature-flows/subscription-management.md) - Subscription CRUD and agent assignment
- [subscription-auto-switch.md](feature-flows/subscription-auto-switch.md) - SUB-003 automatic subscription switching on rate-limit
- [subscription-credential-health.md](feature-flows/subscription-credential-health.md) - Credential health monitoring
- [authenticated-chat-tab.md](feature-flows/authenticated-chat-tab.md) - Chat flow that writes messages
- [task-execution-service.md](feature-flows/task-execution-service.md) - Execution lifecycle that writes execution records
