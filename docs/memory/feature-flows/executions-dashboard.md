# Feature Flow: Unified Executions Dashboard (EXEC-022 / #18)

> Fleet-level view of all agent task runs — filtering, live stat cards, real-time updates.

## UI → API → Database

```
/executions (Executions.vue)
  ├── onMounted → store.startPolling(30000)
  │     └── refresh() → GET /api/executions + GET /api/executions/stats
  ├── WS agent_activity event (schedule_start / schedule_end)
  │     └── store.handleWebSocketEvent() → refresh() [guarded by loading flag]
  ├── filter change → store.setFilter(key, value) → refresh()
  └── load more → store.loadMore() → GET /api/executions?offset=N
```

## Backend

### Router: `routers/executions.py`

Two endpoints, `/stats` registered before `""` to prevent FastAPI treating the literal
string `"stats"` as an execution ID path param.

**`GET /api/executions/stats`**
- Validates `hours` against `{0, 1, 6, 24, 168, 720}` (0 = all-time); invalid → 24
- Calls `accessible_agent_names(current_user)` → `None` (admin) or list
- Optional `agent` param narrows to a single agent via `_narrow_to_agent()`
- Returns `FleetExecutionStats`

**`GET /api/executions`**
- Same access control pattern
- Validates `status` against `_VALID_STATUSES`, `triggered_by` against `_VALID_TRIGGERS`
- `limit` clamped 1–200, `offset` ≥ 0
- Returns `List[FleetExecutionSummary]`; `hasMore = len(rows) == limit`

### DB: `db/schedules.py:ScheduleOperations`

**`get_fleet_executions(agent_names, *, status, triggered_by, hours, search, limit, offset)`**
- `agent_names=None` → admin path (no agent filter)
- `agent_names=[]` → non-admin with zero accessible agents → returns `[]` immediately
- Builds `conditions` / `params` lists dynamically; joins with `AND`
- `hours=0` skips the `started_at > ?` time filter (all-time)
- `search` matches `message LIKE ?` (prefix `%…%`)
- Selects: id, schedule_id, agent_name, status, started_at, completed_at,
  duration_ms, message, triggered_by, context_used, context_max, cost,
  `SUBSTR(error, 1, 200)` as `error_summary` (only for failed/error rows),
  source_user_id, source_user_email, source_agent_name, model_used,
  fan_out_id, business_status, validation_execution_id, queued_at

**`get_fleet_execution_stats(agent_names, hours)`**
- Single-pass conditional aggregation — windowed and live counts in one query
- `time_cond = "started_at > ?"` when `hours > 0`, else `"1"` (always true)
- `time_params = [iso_cutoff(hours)] * 4` repeated for 4 windowed CASE expressions
- `running_count` and `queued_count` use unconditional CASE (always live, not windowed)
- `success_rate` computed in Python: `round(success_count / total * 100, 1)` (0 when total=0)

### Access Control Helper

`services/agent_service/helpers.py:accessible_agent_names(current_user)`
- `admin` role → returns `None` (no SQL filter, sees all)
- Non-admin → returns `[a["name"] for a in get_accessible_agents(current_user)]`
- Shared by `routers/executions.py` and `routers/fleet.py`

## Frontend

### Store: `stores/executions.js`

| State | Type | Description |
|-------|------|-------------|
| `rows` | `ref([])` | Current page of execution rows |
| `stats` | `ref(null)` | `FleetExecutionStats` object |
| `loading` | `ref(false)` | `fetchExecutions` in flight |
| `statsLoading` | `ref(false)` | `fetchStats` in flight |
| `hasMore` | `ref(false)` | `len(rows) == LIMIT` |
| `filters` | `ref({...})` | `{ agent, status, triggered_by, hours: 24, search }` |

Key behaviours:
- `refresh()` runs `fetchExecutions()` + `fetchStats()` in `Promise.all` (parallel)
- `startPolling(30000)` immediately calls `refresh()` then sets 30s interval
- `handleWebSocketEvent()` guards with `!loading.value` — skips if fetch in flight
- `runningCount` computed from `stats.value?.running_count ?? 0`

### View: `views/Executions.vue`

- Live status dot uses `isConnected` from `useWebSocket()` — green when WS connected,
  yellow-pulse when polling fallback
- Success rate card uses threshold ladder: ≥90% success, ≥75% warning, ≥50% urgent, <50% danger
- Running strip appears when `store.runningCount > 0`
- Status filter includes all 7 values: running, queued, success, failed, error, cancelled, skipped
- Trigger filter includes all 9 values: schedule, manual, chat, session, agent, mcp, public, webhook, fan_out
- Search debounced 300ms
- Stop button calls `/api/agents/{name}/schedules/stop-execution/{id}`, falls back to detail navigation

### NavBar: `components/NavBar.vue`

- Executions link between Ops and Settings
- Badge shows `executionsStore.runningCount` when > 0 (yellow background)

## WebSocket Integration

`utils/websocket.js` → `handleMessage()` default case:
```js
if (data.type === 'agent_activity') {
  executionsStore.handleWebSocketEvent(data)
}
```

Store handler refreshes on `schedule_start` / `schedule_end` activity types only,
guarded by `!loading.value` to prevent concurrent fetches on burst events.

## Models

**`FleetExecutionSummary`** (`models.py`): id, schedule_id, agent_name, status,
started_at, completed_at, duration_ms, message, triggered_by, context_used,
context_max, cost, error_summary, source_user_id, source_user_email,
source_agent_name, model_used, fan_out_id, business_status,
validation_execution_id, queued_at.

**`FleetExecutionStats`** (`models.py`): total, success_count, failed_count,
running_count, queued_count, total_cost, success_rate, hours.
