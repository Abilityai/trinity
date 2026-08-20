# Feature: Subscription Usage Tracking

## Overview
Per-subscription rolling token and cost usage across two time windows (5h and 7d), plus — since #471 — failure-event counters and **live provider headroom** (actual 5h/7d utilization % + reset times). **Dedup rule (#471, verified live):** a modern turn writes BOTH a `schedule_executions` row and (for `/chat` + persisted `/task`) a cost-bearing `chat_messages` row, so `schedule_executions` is the SOLE source for cost / context-estimate / turn count; `chat_messages` contributes only `output_tokens` (which executions don't carry). Summing both — the pre-#471 behavior — counted every chat turn's cost twice.

## User Story
As a platform admin, I want to see aggregate usage, live headroom, and failure pressure per Claude Max/Pro subscription so that I can understand load distribution, spot which agent burns the quota, and see how much is left before limits hit.

## Entry Points
- **UI**: Settings → Integrations → Claude Subscriptions (`components/settings/SubscriptionsPanel.vue`, extracted from Settings.vue in #471) — Pressure column per row; expanded row shows the usage/headroom block, per-agent breakdown (lazy), and the Refresh (probe) button. Dashboard pressure badges: `GET /api/agents/subscription-pressure` (see below).
- **API**: `GET /api/subscriptions/{subscription_id}/usage` (extended), `GET /{id}/usage/breakdown`, `POST /{id}/usage/refresh`, `GET|PUT /api/subscriptions/settings/headroom-auto-refresh`. Accepts subscription UUID or name as the path parameter.
- **UI (Dashboard Grid)**: the **Subscription pressure** tile (ent#259, `components/tiles/SubscriptionPressureTile.vue`) — one row per SUBSCRIPTION, the inverse unit of the per-agent chip below. Composes `GET /api/subscriptions` + `/{id}/usage` per subscription in `stores/subscriptions.js::fetchPressureData` on the Grid's 60s batch poll; **no endpoint of its own** (operator ruling, ent#259 2026-08-19). Admin-only, since the payload carries per-subscription spend. Its display rules — a real utilization % only while `headroomIsFresh`, "429s" from the `rate_limit` kind only, `input_tokens` kept off the row face as context occupancy — are pinned in `utils/subscriptionPressureTile.js`; see [dashboard-grid-view.md](dashboard-grid-view.md) § Subscription pressure.

## #471 — Live headroom (provider truth)

**Facts established 2026-08-19 against a real stored `sk-ant-oat01-` setup token:**
- `GET https://api.anthropic.com/api/oauth/usage` → **403 `permission_error` (missing `user:profile` scope)** — that endpoint requires an interactive-login token and is DEAD for the tokens Trinity stores (the mechanism behind closed PR #2170). Do not resurrect it.
- `POST /v1/messages` under the same token returns the full `anthropic-ratelimit-unified-*` header set: `{5h,7d}-utilization` (fraction 0..1), `-reset` (unix), per-window `-status`, `representative-claim`, overage status.

**`services/subscription_headroom_service.py`** reads those headers via a **micro-ping probe** (`max_tokens=1` Haiku message, ~a dozen tokens of the subscription's own quota, visible in the Anthropic console — release-noted):
- **Click** (`POST /{id}/usage/refresh`, admin): one probe, floored ≥60s apart per subscription; works Redis-down via a per-worker in-process floor.
- **Ambient** (default ON, `subscription_headroom_auto_refresh` system setting): refresh when the cached snapshot is older than `SUBSCRIPTION_HEADROOM_REFRESH_SECONDS` (900), demand-driven by reads — an unwatched instance probes nothing. **Fail-CLOSED when Redis is unreachable**: `_read_snapshot` returns `(redis_ok, snapshot)` and ambient probing requires an ANSWERED cache read — a client object existing proves nothing about the server (learnings 2026-08-19).
- **Batch path never blocks**: `get_headroom(wait=False)` (used by `pressure_states` behind the dashboard poll) spawns the refresh as a strong-ref background task and serves the stale snapshot with its honest age.
- Snapshot in Redis `subscription:headroom:{id}` (7d TTL, best-effort DEL on subscription delete); probe single-flighted (`SingleFlightLock` #1920). A probe 429 updates the snapshot's `status` only — never `subscription_rate_limit_events` (platform-caused, not agent work). 401/403 → `status: invalid_token`. Missing header family on a 200 → throttled WARNING (provider drift is loud) + degrade to observed.
- **Contract (the #2170 inversion)**: every `/usage` response carries `source: "anthropic"|"observed"` + `headroom.snapshot_age_seconds`; the DB-derived windows/counters are ALWAYS populated — the observed arm is load-bearing, provider data is enrichment. `rate_limited_now` has ONE derivation (`decorate_usage`): the 2h `is_subscription_rate_limited` predicate OR a fresh (≤2× refresh interval) provider verdict.

**Failure counters (#471)**: `failure_events_24h` + `failure_events_by_kind` (`rate_limit` | `auth` | `unknown` for pre-#471 NULL rows) from `subscription_rate_limit_events`, which now persists `failure_kind` (dual-track migration `rate_limit_events_failure_kind` / Alembic `0040`). Events are recorded BEFORE the auto-switch enabled gate (see subscription-auto-switch.md) so opted-out operators keep observability.

**Fleet pressure**: `GET /api/agents/subscription-pressure` (registered before `/{agent_name}`, Invariant #4) — per accessible agent (`visible_agent_names`, the shared pure-DB ent#384 helper): `auth_mode` (the `AgentAuthStatus` vocabulary via the shared `derive_auth_mode`), `subscription_name`, `failure_events_24h`, `rate_limited_now`, `utilization_5h_pct` + `headroom_source`. Explicit `response_model` (ent#334). Feeds the AgentTile chip + AgentListPanel badge via the sync-health two-store poll discipline (`fleetGrid.js` + `agents.js`); shared badge predicate in `utils/subscriptionPressure.js` (vitest-covered).

## Backend Layer

### Endpoint
- `src/backend/routers/subscriptions.py:118` - `get_subscription_usage()`
  - Admin-only (`require_admin`)
  - Resolves path param by UUID first, falls back to name lookup
  - Returns `SubscriptionUsage` Pydantic model

```
GET /api/subscriptions/{subscription_id}/usage
Authorization: Bearer <admin_token>
```

Response shape:
```json
{
  "subscription_id": "<uuid>",
  "window_5h": {
    "input_tokens": 12000,
    "output_tokens": 4500,
    "cost_usd": 0.18,
    "message_count": 37
  },
  "window_7d": {
    "input_tokens": 210000,
    "output_tokens": 75000,
    "cost_usd": 3.12,
    "message_count": 512
  },
  "agents": ["agent-a", "agent-b"]
}
```

### Resolution Logic
1. Try `db.get_subscription(subscription_id)` by UUID
2. If not found, try `db.get_subscription_by_name(subscription_id)`
3. If still not found, raise 404
4. Call `db.get_subscription_usage(subscription.id)` with the resolved UUID

### Business Logic
- `agents` field reflects **current** assignments from `agent_ownership`, not historical data
- Usage windows query historical records via the snapshotted `subscription_id` column — correct even when agents switch subscriptions between queries

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
    COALESCE(SUM(cost), 0.0)         AS cost_usd,
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
    COALESCE(SUM(cost), 0.0)       AS cost_usd,
    COUNT(*)                        AS exec_count
FROM schedule_executions
WHERE subscription_id = ?
  AND started_at >= ?
  AND status NOT IN ('running', 'pending')
```

Totals are summed: `input_tokens = chat.input_tokens + exec.input_tokens`, `message_count = chat.message_count + exec.exec_count`.

### Pydantic Models
- `src/backend/db_models.py:672` - `SubscriptionUsageWindow` (input_tokens, output_tokens, cost_usd, message_count)
- `src/backend/db_models.py:680` - `SubscriptionUsage` (subscription_id, window_5h, window_7d, agents)

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
