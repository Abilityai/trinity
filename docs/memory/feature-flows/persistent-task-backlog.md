# Feature Flow: Persistent Task Backlog

> **⚠️ INTERNAL AS OF 2026-04-26 (#428):** `BacklogService` is no longer a public API. It is the persistent overflow store inside [`CapacityManager`](capacity-management.md), reached via `acquire(..., overflow_policy="queue_persistent", overflow_payload=PersistentTaskPayload(...))`. The SQL columns (`schedule_executions.queued_at`, `backlog_metadata`, status `'queued'`), drain-on-release behaviour, 24h expiry, and partial index are unchanged. New callers should reach for [`capacity-management.md`](capacity-management.md) instead of importing `BacklogService` directly.
>
> **Requirement**: BACKLOG-001 — Persistent task backlog for over-capacity requests
> **Status**: Implemented
> **Created**: 2026-04-13
> **Updated**: 2026-04-26 (#428: BacklogService internalized behind CapacityManager)
> **GitHub Issue**: [#260](https://github.com/abilityai/trinity/issues/260), extended by [#498](https://github.com/abilityai/trinity/issues/498) (sync long-poll), internalized by [#428](https://github.com/abilityai/trinity/issues/428)
> **Priority**: P1
> **Related**: [capacity-management.md](capacity-management.md), [parallel-capacity.md](parallel-capacity.md), [task-execution-service.md](task-execution-service.md), [parallel-headless-execution.md](parallel-headless-execution.md)

## Overview

When a `POST /api/agents/{name}/task` request arrives and all of the agent's
parallel execution slots (CAPACITY-001) are occupied, the request is spilled
into a durable SQLite-backed FIFO backlog instead of returning HTTP 429. When
a slot frees, the oldest queued item for that agent is drained automatically
via a `SlotService` release callback. True HTTP 429 is only returned when the
backlog itself is also at its configured depth.

Both modes share the same backlog (issue #498):
- **Async (`async_mode=true`)**: Caller gets HTTP 202 with `execution_id`
  immediately and polls for the result. The backlog drains in the background.
- **Sync (`async_mode=false`)**: Caller's HTTP connection is held open and
  long-polls until the queued execution reaches a terminal status, then the
  result is returned inline on the same connection. Total connection hold is
  bounded by `2 × effective_timeout` (queue wait + execution).

Queued rows survive backend restarts. A 60-second maintenance task in the
backend process expires rows older than 24 hours and drains orphans left
behind when a release callback couldn't fire (e.g. process crash).

## Problem Statement

Before BACKLOG-001 (#260), `async_mode=true` requests at capacity were dropped
on the floor with a 429 response. Bursty MCP fan-out scenarios (agents
orchestrating other agents via `chat_with_agent(async=true)`) routinely hit
the 3-slot default cap and lost work. Clients had to implement their own
retry-with-backoff logic, and there was no first-class backpressure signal.

Before #498, sync calls (`async_mode=false`) bypassed the backlog entirely —
hitting capacity returned a terminal 429 even though the backlog could have
absorbed the overflow. Observed in production: ~40% terminal-failure rate
from one MCP fan-out caller (214 capacity rejections / 24h, 0 enqueues from
the same caller across 541 dispatches). #498 closed that gap by spilling
sync calls to the same backlog and long-polling on the open HTTP connection.

The backlog gives Trinity:
- **Spill-over by default** for both sync and async — no lost requests below
  the backlog depth cap
- **Restart durability** — queued items survive backend restarts via SQLite
- **Bounded resource envelope** — per-agent `max_backlog_depth` (default 50,
  hard cap 200) + 24h stale expiry
- **Transparent to pollers** — existing `GET /api/agents/{name}/executions/{id}`
  returns `status=queued` while the row waits to drain
- **Transparent to sync callers** — same response shape as immediate-slot path,
  just with extra wait time

## Architecture Diagram

```
                  POST /api/agents/{name}/task
                         async_mode=true
                                │
                   ┌────────────▼─────────────┐
                   │  routers/chat.py         │
                   │  (submit_parallel_task)  │
                   └────────────┬─────────────┘
                                │
                  create_task_execution (status=running)
                                │
                  slot_service.acquire_slot()
                                │
             ┌──────────────────┴───────────────────┐
             │                                      │
     slot acquired                           slot full
             │                                      │
             ▼                                      ▼
    _run_async_task_with_persistence()  backlog.enqueue()
             │                                      │
             ▼                          ┌───────────┴───────────┐
     finally: release_slot              │                       │
             │                    depth < cap             depth >= cap
             ▼                          │                       │
    release callbacks fire              ▼                       ▼
             │                 HTTP 202 queued              HTTP 429
             ▼
  backlog.on_slot_released(name)
             │
             ▼
    backlog.drain_next(name)
             │
      ┌──────┴──────┐
      │             │
  sentinel     no queued
  acquire     items → exit
      │
      ▼
  claim_next_queued  ◄── atomic UPDATE ... RETURNING
      │
      ▼
  swap sentinel → real execution_id slot
      │
      ▼
  reconstruct ParallelTaskRequest from backlog_metadata
      │
      ▼
  asyncio.create_task(
      _run_async_task_with_persistence(
          identity from backlog_metadata,
          # is_self_task / self_task_activity_id / inject_result
          # threaded through so SELF-EXEC-001 (#264) survives queueing
      )
  )
```

Parallel path (safety net):

```
   backend startup
        │
        ▼
   main.py lifespan
        │
        ├── register_on_release(backlog.on_slot_released)
        │
        └── asyncio.create_task(_backlog_maintenance_loop)
                    │
                    │   every 60s:
                    │     - expire_stale(max_age_hours=24)
                    │     - drain_orphans_all() → picks up rows that missed
                    │       their callback after a restart or crash
                    ▼
```

### Sync long-poll path (issue #498)

```
                  POST /api/agents/{name}/task
                         async_mode=false
                                │
                  router pre-acquires slot
                                │
             ┌──────────────────┴───────────────────┐
             │                                      │
     slot acquired                           slot full
             │                                      │
             ▼                                      ▼
    execute_task(slot_already_held=True)    backlog.enqueue()
    → return inline result                          │
                                        ┌───────────┴───────────┐
                                        │                       │
                                  depth < cap             depth >= cap
                                        │                       │
                                        ▼                       ▼
                              wait_for_sync_terminal       HTTP 429
                              (event + 5s DB-poll fallback)
                                        │
                          ┌─────────────┼─────────────┐
                          │             │             │
                  signaled by      poll detects     timeout
                  drain finally    terminal flip    (2 × effective_timeout)
                          │             │             │
                          ▼             ▼             ▼
              return inline result   reconstruct     HTTP 504
              (full TaskExecResult)  from DB row     (execution may
                                     → return        still complete
                                                      in background)
```

The drain machinery is shared with the async path — `_run_async_task_with_persistence`
runs the queued task identically, then signals `_sync_waiters` from its `finally`
block. Sync waiters wake on the same event the async chat-session-persistence
broadcast fires on.

## Database Schema

Migration `backlog_support` (append-only, reuses existing table):

```sql
ALTER TABLE schedule_executions ADD COLUMN queued_at TEXT;
ALTER TABLE schedule_executions ADD COLUMN backlog_metadata TEXT;
ALTER TABLE agent_ownership ADD COLUMN max_backlog_depth INTEGER DEFAULT 50;

CREATE INDEX idx_executions_queued
ON schedule_executions(agent_name, queued_at)
WHERE status = 'queued';
```

The partial index is the key to cheap FIFO claim — only queued rows are
indexed. `claim_next_queued` reads at most one row via the subquery,
making the operation O(log n) on backlog size.

**Pull claim order (#2842, #2843).** The backend drain above claims oldest
first. A pull pilot's worker claims through the same method with two extra
rules: rows whose `triggered_by` is in `pull_pilot.INTERACTIVE_TRIGGERS` go
first (strict precedence, no anti-starvation rule), and a row whose
`conversation_key` already has a `running` row is skipped. The partial unique
index below stops two workers running one conversation at once:

```sql
CREATE UNIQUE INDEX idx_executions_one_running_turn
ON schedule_executions(agent_name, conversation_key)
WHERE status = 'running' AND conversation_key IS NOT NULL;
```

`backlog_service.enqueue` stamps `conversation_key` (`chat_session_id`, else
`resume_session_id`) on pilot agents only. The ordering expression cannot use
`idx_executions_queued`; per-agent queues are small.

### backlog_metadata JSON Shape

Captured at enqueue time, replayed on drain. No credential values, only
identity and request parameters:

```json
{
  "message": "...",
  "model": "sonnet",
  "allowed_tools": ["Read", "Bash"],
  "system_prompt": "...",
  "timeout_seconds": 900,
  "max_turns": null,
  "save_to_session": false,
  "user_message": "...",
  "create_new_session": false,
  "chat_session_id": null,
  "resume_session_id": null,
  "inject_result": false,
  "user_id": 42,
  "user_email": "user@example.com",
  "subscription_id": "sub-xyz",
  "x_source_agent": null,
  "x_mcp_key_id": "mcp-key-abc",
  "x_mcp_key_name": "my-key",
  "triggered_by": "manual",
  "collaboration_activity_id": null,
  "is_self_task": false,
  "self_task_activity_id": null
}
```

## Lifecycle

```
queued ──(drain)──▶ running ──(success)──▶ success
   │                   │
   │                   └──(agent error)──▶ failed
   │
   ├──(user terminate)──▶ cancelled
   │
   ├──(agent delete)──▶ cancelled (reason=agent_deleted)
   │
   └──(stale >24h)──▶ failed (reason=backlog expired)
```

## Components

### Router — `src/backend/routers/chat.py`

**Enqueue (async path, replaces old 429 block)**:

```python
if not slot_acquired:
    from services.backlog_service import get_backlog_service
    backlog = get_backlog_service()
    enqueued = await backlog.enqueue(
        agent_name=name,
        execution_id=execution_id,
        request=request,
        effective_timeout=effective_timeout,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username,
        subscription_id=_task_subscription_id,
        x_source_agent=x_source_agent,
        x_mcp_key_id=x_mcp_key_id,
        x_mcp_key_name=x_mcp_key_name,
        triggered_by=triggered_by,
        collaboration_activity_id=collaboration_activity_id,
        is_self_task=is_self_task,
        self_task_activity_id=self_task_activity_id,
    )
    if enqueued:
        return {"status": "queued", "execution_id": execution_id, ...}
    # else: backlog full → real 429
```

**Terminate (short-circuit for queued rows)**:

```python
_exec_row = db.get_execution(task_execution_id)
if _exec_row and _exec_row.status == TaskExecutionStatus.QUEUED:
    cancelled = db.cancel_queued_execution(
        task_execution_id, reason="Cancelled by user while queued"
    )
    if cancelled:
        return {"status": "cancelled_while_queued", ...}
```

### Service — `src/backend/services/backlog_service.py` (NEW)

| Method | Purpose |
|---|---|
| `enqueue(...)` | Check depth, persist `backlog_metadata`, flip row to QUEUED. Returns False if at cap. |
| `drain_next(agent_name)` | Acquire sentinel slot → atomically claim row → swap to real execution_id slot → reconstruct `ParallelTaskRequest` (including `inject_result`) → spawn `_run_async_task_with_persistence` (#496: was `_execute_task_background`, deleted by #95). |
| `on_slot_released(agent_name)` | Callback registered with SlotService. Tries `drain_next` once per release. |
| `expire_stale(max_age_hours=24)` | Maintenance: mark old queued rows as FAILED. |
| `drain_orphans_all()` | Maintenance: iterate agents with queued work, drain one item each. |
| `cancel_all_backlog(agent_name, reason)` | Called on agent delete. |

Design invariants:
- Slot acquired **before** row is claimed — prevents RUNNING-without-slot orphans.
- Single-statement `UPDATE ... WHERE id=(SELECT ... LIMIT 1) RETURNING` — atomic claim.
- `_run_async_task_with_persistence` is late-imported inside `_spawn_drain` to
  avoid a `routers.chat` ↔ `services.backlog_service` cycle. **#496 regression
  guard**: `tests/unit/test_backlog.py::TestLazyImportTarget` parses
  `routers/chat.py` via AST and asserts the import target exists; a paired test
  asserts the lazy-import string in `services/backlog_service.py` matches the
  validated allow-list. Catches both directions of drift without booting the
  backend (the symptom that allowed #496 to ship: a `SimpleNamespace` mock
  injected the missing symbol back in, masking the production `ImportError`).
- Drain spawn failures emit a stable log token `backlog_drain_spawn_failed`
  so log-based detection (Vector / dashboards) can catch import drift or
  similar spawn-time regressions at fleet scale rather than per-row.
- Self-task fields (`is_self_task`, `self_task_activity_id`) are captured at
  enqueue and threaded through drain so SELF-EXEC-001 (#264) `inject_result`
  semantics survive backlog overflow.
- Identity replayed from `backlog_metadata`; no re-auth at drain time.

### Slot Service — `src/backend/services/slot_service.py`

Added release callback hook:

```python
def register_on_release(self, callback: Callable[[str], Awaitable[None]]) -> None: ...

async def release_slot(self, agent_name, execution_id):
    ...  # existing Redis ZSET + metadata cleanup
    for cb in self._on_release_callbacks:
        asyncio.create_task(self._safe_invoke(cb, agent_name))
```

Callback invocation is isolated (`_safe_invoke` catches exceptions per
callback), fire-and-forget via `create_task`, and gracefully handles "no
running loop" (returns silently and lets the 60s maintenance task drain the
work on the next tick).

### Lifespan — `src/backend/main.py`

```python
from services.slot_service import get_slot_service
from services.backlog_service import get_backlog_service
_backlog = get_backlog_service()
get_slot_service().register_on_release(_backlog.on_slot_released)

async def _backlog_maintenance_loop():
    await asyncio.sleep(15)  # small delay to not slow boot
    while True:
        try:
            await _backlog.expire_stale(max_age_hours=24)
            await _backlog.drain_orphans_all()
        except Exception as exc:
            logger.warning(f"[Backlog] maintenance tick failed: {exc}")
        await asyncio.sleep(60)

asyncio.create_task(_backlog_maintenance_loop())
```

### Delete cascade — `src/backend/routers/agents.py`

After stopping the container and deleting schedules, the delete path calls
`backlog.cancel_all_backlog(agent_name, reason="agent_deleted")` so orphan
queued rows don't linger in the database.

### Sync Waiter — `src/backend/services/sync_waiter.py` (NEW, #498)

In-process registry that lets sync HTTP callers long-poll a queued execution
on the same connection. Two primitives:

- `signal_sync_waiter(execution_id, result, chat_session_id)` — called from
  `_run_async_task_with_persistence` finally block. Looks up the registered
  future and completes it with the rich `TaskExecutionResult`. No-op when no
  waiter is registered (the normal async fire-and-forget path) or when the
  caller already cancelled.
- `wait_for_sync_terminal(execution_id, timeout)` — registers a future,
  starts a 5s DB-poll fallback task, then `asyncio.wait(FIRST_COMPLETED)`s
  on either signal. Returns the rich payload on signal, returns `None` on
  poll-fallback hit (caller reconstructs from DB row), raises `TimeoutError`
  if neither fires.

The registry is in-process — multi-worker deployments would need pubsub to
fan signals across processes; that's not the current backend shape (single
worker).

The poll fallback covers terminal-flip sites that don't go through the drain:
corrupt-metadata in `_spawn_drain`, `expire_stale_queued`, `cancel_all_backlog`,
and `cleanup_service` recovery. Latency cost is bounded at one poll interval
(default 5s).

## Configuration

Per-agent backlog depth is stored in `agent_ownership.max_backlog_depth`:

```python
db.get_max_backlog_depth(agent_name)     # default 50
db.set_max_backlog_depth(agent_name, n)  # validates 1-200
```

No REST endpoint for this setting yet — the default (50) is sensible for
most workloads and the issue explicitly deferred UI configuration. Custom
values can be set directly via the internal DB method or a future Settings
page if demand emerges.

## Observability

- Every enqueue and drain logs agent_name, execution_id, and current
  depth at `INFO` level.
- Drain failures (corrupt metadata, lost slot race, background spawn
  failure) log at `ERROR` with the execution_id and cause.
- Queued rows are visible in existing tools:
  - `GET /api/agents/{name}/executions/{id}` returns `status=queued`
  - `GET /api/agents/{name}/executions` lists queued rows alongside other
    statuses
  - Frontend: `ExecutionDetail.vue` and `TasksPanel.vue` render a dedicated
    amber badge for `status=queued`.
- MCP `get_execution_result` polling transparently surfaces the `queued`
  status without tool changes.

## Failure Modes

| Failure | Behaviour |
|---|---|
| Corrupt `backlog_metadata` JSON | Row marked FAILED with reason, slot released, drain continues with next item. |
| Slot acquisition fails after claim | Row released back to QUEUED via `release_claim_to_queued`; next callback retries. |
| Backend crash mid-drain | Row stays RUNNING with no Claude session ID — existing cleanup service recovers it within the timeout window. New queued rows are drained by the 60s maintenance loop on restart. |
| Agent container gone when drain fires | `_run_async_task_with_persistence` surfaces an HTTP error via `TaskExecutionService`, row marked FAILED. |
| Concurrent drains on same agent | Atomic UPDATE guarantees only one callback wins the row; others get None and release their sentinel slots. |
| Cancel-while-queued | Terminate endpoint short-circuits, row moves to CANCELLED. The claim SQL's `WHERE status='queued'` filter naturally skips cancelled rows, so the drain path is race-safe. |

## Testing

Unit tests: `tests/unit/test_backlog.py` (33 tests, no backend/Docker required).

Coverage:
- TaskExecutionStatus.QUEUED enum value
- Migration (columns + partial index)
- `ResourcesMixin.get/set_max_backlog_depth` (including range validation 1-200)
- ScheduleOperations backlog queries: transition to queued, atomic FIFO claim,
  agent isolation, release-claim-back, single cancel, bulk cancel for agent,
  stale expiry (normal + tiny-threshold), list agents with queued
- `BacklogService.enqueue`: under cap succeeds, at cap rejected,
  self-task fields captured for SELF-EXEC-001 round-trip (#496)
- `BacklogService.drain_next`: empty noop, failed-claim releases slot,
  corrupt metadata marks failed, slot-acquire-failure noop, happy path
  spawns background task with reconstructed request,
  self-task fields threaded through to `_run_async_task_with_persistence` (#496)
- `SlotService.register_on_release` + `release_slot` fan-out, per-callback
  exception isolation
- **#496 regression guards**: AST-based check that
  `_run_async_task_with_persistence` is defined in `routers/chat.py`, and
  inverse check that the lazy-import string in
  `services/backlog_service.py` matches the validated allow-list

### Prerequisites

- Backend running at http://localhost:8000
- Redis running (SlotService)
- Test agent created with known `max_parallel_tasks`

### Manual Verification Scenarios

#### 1. Spill to backlog under capacity pressure
**Action**:
- Set `max_parallel_tasks=1` on a test agent (via `PUT /api/agents/{name}/capacity`).
- Send 3 consecutive `POST /api/agents/{name}/task?async_mode=true` requests.
**Expected**:
- Request 1: `status=accepted` (running).
- Request 2 & 3: `status=queued` with HTTP 200 body.
- Poll `GET /api/agents/{name}/executions/{id}`: status transitions
  `queued → running → success` in FIFO order.

#### 2. True 429 when backlog is full
**Action**:
- Temporarily call `db.set_max_backlog_depth(name, 1)` via a Python shell.
- With `max_parallel_tasks=1`, send 3 async tasks rapidly.
**Expected**: request 3 receives `HTTP 429` with "backlog is full" detail.

#### 3. Restart durability
**Action**:
- Queue 2 tasks (as in scenario 1, second task in queued state).
- `docker compose restart backend`.
**Expected**: within ~75 seconds (15s startup delay + 60s tick), the second
task drains and completes. No manual intervention.

#### 4. Cancel while queued
**Action**:
- Queue a task via scenario 1.
- Call `POST /api/agents/{name}/executions/{id}/terminate`.
**Expected**: response `{"status":"cancelled_while_queued"}`; DB row in
CANCELLED state; task never runs.

#### 5. Delete cascade
**Action**:
- Queue 3 tasks for an agent.
- `DELETE /api/agents/{name}`.
**Expected**: all queued rows transition to CANCELLED with
`error="agent_deleted"`; no orphan rows remain.

### Edge Cases
- [ ] Queued task for deleted agent — should fail cleanly during drain
- [ ] Multiple agents with queued work — fair per-agent drain (each agent
      gets one drain per maintenance tick)
- [ ] Backlog entries older than 24h — expired to FAILED on next tick

**Last Tested**: 2026-04-25 (unit tests; manual scenarios pending)
**Status**: ✅ 33 unit tests passing; integration testing recommended post-merge

## Changelog

| Date | Change |
|---|---|
| 2026-04-13 | Initial implementation (PR #316). |
| 2026-04-25 | **#496 fix**: lazy-import target updated from `_execute_task_background` (deleted by #95) to `_run_async_task_with_persistence`. Drain had been silently failing with `ImportError` since #95 because the existing happy-path test injected a `SimpleNamespace` stub into `sys.modules["routers.chat"]` with whatever attribute name it expected. AST-based regression guards added. Self-task fields (`is_self_task`, `self_task_activity_id`, `inject_result`) now captured at enqueue and rehydrated on drain so SELF-EXEC-001 (#264) survives backlog overflow. Drain spawn failures emit stable log token `backlog_drain_spawn_failed`. Stale `task_activity_id` field dropped from metadata (the post-#95 service tracks CHAT_START itself). |

## Acceptance Criteria Coverage

All nine acceptance criteria from issue #260 are met:

- [x] Async task at capacity returns HTTP 202 with `status: "queued"` instead of 429
- [x] Queued tasks auto-execute when slots free up (FIFO order)
- [x] `GET /api/agents/{name}/executions/{id}` shows `queued → running → success` lifecycle
- [x] Backlog persists across backend restarts (60s maintenance tick drains orphans)
- [x] Configurable `max_backlog_depth` per agent (default 50)
- [x] True 429 only when backlog is also full
- [x] Queued items can be cancelled
- [x] Stale queued items expire after 24h
- [x] MCP `get_execution_result` works for queued/backlog items without changes

## What Doesn't Change

- MCP tool signatures — `chat_with_agent(async=true)` automatically gains
  backlog behaviour.
- Frontend routing — the Tasks tab and execution detail views render
  queued rows via the existing list endpoints; only the status badge
  mapping needed to learn about `queued`.
- Sync mode — `async_mode=false` remains blocking and is unaffected.
- Fan-out — `FANOUT-001` uses `TaskExecutionService` directly in parallel
  mode and bypasses the backlog entirely.
- Scheduler — scheduled executions are created in RUNNING state via the
  scheduler container and never touch the backlog.

## Files Modified

### Backend

| File | Change |
|---|---|
| `src/backend/models.py` | +1 line: `TaskExecutionStatus.QUEUED = "queued"` |
| `src/backend/db_models.py` | +3 lines: `queued_at`, `backlog_metadata` on `ScheduleExecution` |
| `src/backend/db/migrations.py` | +37 lines: `_migrate_backlog_support` + registry entry |
| `src/backend/db/schedules.py` | +231 lines: 10 backlog query methods + row mapping |
| `src/backend/db/agent_settings/resources.py` | +45 lines: `get/set_max_backlog_depth` (1-200 validation) |
| `src/backend/database.py` | +38 lines: delegate methods |
| `src/backend/services/slot_service.py` | +49 lines: release callback hook + safe invocation |
| `src/backend/services/backlog_service.py` | **NEW** 320 lines — core service |
| `src/backend/routers/chat.py` | +69 lines: enqueue-on-capacity, terminate-while-queued |
| `src/backend/routers/agents.py` | +10 lines: delete cascade |
| `src/backend/main.py` | +27 lines: callback registration + 60s maintenance loop |

### Frontend

| File | Change |
|---|---|
| `src/frontend/src/views/ExecutionDetail.vue` | +1 line: amber `queued` badge |
| `src/frontend/src/components/TasksPanel.vue` | +2 lines: amber `queued` badge (text + dot) |

### Tests

| File | Change |
|---|---|
| `tests/unit/test_backlog.py` | **NEW** 29 tests — enum, migration, query methods, service |
| `tests/unit/conftest.py` | +42 lines: backend import bootstrap (evicts shadow `utils`) |
| `tests/registry.json` | +7 lines: register new test file |
