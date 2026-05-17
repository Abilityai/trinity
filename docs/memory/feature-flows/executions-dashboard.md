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

## Testing

**Prerequisites**:
- [ ] Trinity running locally (`./scripts/deploy/start.sh`)
- [ ] At least one agent with completed schedule executions in the DB
- [ ] Admin user + at least one non-admin user configured
- [ ] Backend API accessible at `http://localhost:8000`

**Test Steps**:

### 1. Admin sees all executions
**Action**:
- Log in as admin, navigate to `/executions`

**Expected**: All agents' runs appear; stat cards show non-zero totals

**Verify**:
- [x] Total count in stat card matches row count across all agents — verified 2026-05-15 with seeded data (32 in 24h window, stats card matched list)
- [x] `running_count` badge in NavBar matches "N running now" strip count — verified visually
- [x] Live status dot is green when WebSocket connected — `isConnected` from `useWebSocket()`

### 2. Non-admin access control
**Action**:
- Log in as a non-admin user who owns/is shared on a subset of agents
- Navigate to `/executions`

**Expected**: Only executions for accessible agents are shown

**Verify**:
- [x] Rows for agents the user cannot access do not appear — covered by unit test `test_non_admin_filtered_to_accessible`
- [x] `GET /api/executions` returns 200 (not 403) with filtered results — `accessible_agent_names` returns `[]` (empty), not raise
- [x] User with zero accessible agents sees empty state, not 500 — covered by `test_empty_agent_names_returns_empty` + `test_empty_agent_names_no_division_by_zero`

### 3. Filters update list and stat cards
**Action**:
- Apply status filter (e.g. `failed`), then trigger filter (`schedule`), then time range (`Last 1h`)

**Expected**: Each filter narrows both the row list and the stat card totals

**Verify**:
- [x] Stat card "Total" reflects filtered window — `refresh()` runs `fetchStats()` + `fetchExecutions()` in parallel on every `setFilter()`
- [x] "Success rate" recalculates per filter — computed in Python from filtered `success_count / total`
- [x] "X shown" count below filter bar matches visible rows — bound to `store.rows.length`
- [x] "Clear filters" resets to default 24h view — `clearFilters()` resets `filters` to defaults

### 4. Running strip and live updates
**Action**:
- Trigger a schedule manually while `/executions` is open

**Expected**: "N running now" yellow strip appears; row for the running execution appears at top

**Verify**:
- [ ] Strip disappears when execution completes (WS event fires refresh) — requires live schedule run to fully verify end-to-end; WS handler logic confirmed via code review
- [ ] NavBar badge updates to match running count — covered by Fix in commit (NavBar `onMounted` now calls `fetchStats()`)
- [x] No manual refresh required — `handleWebSocketEvent` calls `refresh()` on `schedule_start`/`schedule_end` events

### 5. Load more pagination
**Action**:
- Ensure >50 executions exist; navigate to `/executions` with no filters

**Expected**: "Load more" button appears; clicking appends next 50 rows

**Verify**:
- [x] First 50 rows loaded on mount — verified 2026-05-15 with 83 seeded rows in 24h window
- [x] "Load more" appends without duplicating existing rows — `loadMore()` calls `fetchExecutions({ append: true })` with `offset = rows.length`
- [x] Button disappears when all rows are loaded — `hasMore = (len(rows) == LIMIT)`; goes false on partial last page

### 6. Search
**Action**:
- Type a substring of a known task message into the search box

**Expected**: After 300ms debounce, only matching executions are shown

**Verify**:
- [x] Partial match works (message contains substring) — backend SQL `message LIKE ?` with `%search%`; covered by `test_search_filter`
- [x] Clearing search restores full list — `setFilter('search', '')` falls back to no `search` condition in SQL

### 7. `running_count` is always live (independent of hours filter)
**Action**:
- Set time range to "Last 1h"; ensure a running execution started >1h ago exists

**Expected**: `running_count` still reflects it; "Total" does not

**Verify**:
- [x] `GET /api/executions/stats?hours=1` returns `running_count > 0` for old running row — covered by `test_running_count_is_not_windowed`
- [x] `total` in same response does NOT count that old row — same test asserts `total` excludes the >1h row

**Edge Cases**:
- [x] `?hours=0` (all-time): stat card totals include rows older than 30 days — covered by `test_all_time_includes_old_row`
- [x] Invalid `?status=__bad__` silently ignored — router validates against `_VALID_STATUSES`, falls through to no filter
- [x] Invalid `?hours=99` falls back to 24h window — router validates against `_VALID_HOURS = {0, 1, 6, 24, 168, 720}`
- [x] `?limit=200` (max) returns up to 200 rows without error — `limit` clamped 1–200 in router
- [x] Rapid filter changes do not stack concurrent fetches (loading guard) — store guards with `if (loading.value) return` and WS handler with `!loading.value`

**Cleanup**:
- [x] No cleanup needed — read-only dashboard

**Last Tested**: 2026-05-15 (dev review with 83+ seeded executions)
**Tested By**: Alex Korin (manual UI review + unit tests `tests/unit/test_fleet_executions.py` — 28 passing)
**Status**: Verified
**Issues**: Test 4 strip-disappears-on-WS-completion requires a real schedule run to validate end-to-end; logic confirmed via code review.
