# Feature Flow: Parallel Execution Capacity

> **⚠️ INTERNAL AS OF 2026-04-26 (#428):** `SlotService` is no longer a public capacity API. It is now a private internal of [`CapacityManager`](capacity-management.md), which is the single facade callers should use. The Redis ZSET (`agent:slots:{name}`), per-agent dynamic TTL, and atomic ZADD-with-count semantics described below are unchanged — they are the implementation backing `CapacityManager.acquire/release/reclaim_stale`. New callers should reach for [`capacity-management.md`](capacity-management.md) instead of importing `SlotService` directly.
>
> **Requirement**: CAPACITY-001 - Per-Agent Parallel Execution Capacity
> **Status**: Implemented (Phase 1: Backend, Phase 2: Frontend UI)
> **Created**: 2026-02-28
> **Updated**: 2026-04-26 (#428: SlotService internalized behind CapacityManager)
> **Priority**: P1

## Revision History

| Date | Changes |
|------|---------|
| 2026-05-17 | #869: `_cleanup_stale_slots_for_agent` now uses per-slot metadata TTL instead of per-agent cutoff; prevents false reclaim when schedule timeout > agent default |
| 2026-04-13 | BACKLOG-001 (#260): Async `/task` no longer returns 429 when slots are full — requests spill into a persistent SQLite backlog and drain via a new `SlotService.register_on_release` callback. True 429 only when the per-agent `max_backlog_depth` is also exceeded. See [persistent-task-backlog.md](persistent-task-backlog.md). |
| 2026-03-21 | Issue #98: Chat executions (`/api/chat`) now acquire capacity slots, making SlotService the single source of truth for agent load across all execution types |
| 2026-03-12 | TIMEOUT-001: Slot TTL now dynamic (agent timeout + 5 min buffer), not fixed 30 min. Aligns with per-agent configurable execution timeout. |
| 2026-03-09 | Scheduled tasks now route through TaskExecutionService via internal API — capacity meter shows slot usage for cron/manual schedule executions |
| 2026-03-04 | EXEC-024: Slot management split - sync path delegated to TaskExecutionService, public links gain slot enforcement |
| 2026-03-03 | Phase 2: Frontend UI - CapacityMeter component, store plumbing, Agents page + Dashboard timeline integration |
| 2026-02-28 | Initial implementation - Database, Redis slots, REST API, task endpoint integration |

## Overview

This feature implements configurable per-agent parallel execution capacity with Redis-based slot tracking. Each agent has a `max_parallel_tasks` setting (1-10, default 3), and the `/api/agents/{name}/task` endpoint enforces this limit by returning HTTP 429 when capacity is reached.

The frontend displays slot usage as a vertical capacity meter bar on the Agents page and Dashboard timeline view, polled every 5 seconds via `GET /api/agents/slots`.

## Problem Statement

**Prior Limitation**: Trinity had no limit on parallel `/api/task` executions per agent. An orchestrator could spawn unlimited concurrent tasks, causing:
1. Resource exhaustion (CPU, memory, API tokens)
2. Cost explosion (unbounded Claude API usage)
3. Agent unavailability for critical operations

**Solution**: Slot-based capacity tracking with Redis ZSET for automatic cleanup.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Parallel Capacity Flow                                                  │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Client                                                                  │
│     │                                                                    │
│     ├── POST /api/agents/{name}/task                                    │
│     │                                                                    │
│     v                                                                    │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  Backend — Slot Management (EXEC-024 split)                     │     │
│  │                                                                  │     │
│  │  SYNC path (chat.py:714-730 → task_execution_service.py):       │     │
│  │  1. Create execution record in database (chat.py:602-613)       │     │
│  │  2. Delegate to TaskExecutionService.execute_task()              │     │
│  │     ├── Acquire slot (task_execution_service.py:164)             │     │
│  │     ├── If full → Return failed result ("at capacity")          │     │
│  │     ├── Execute task with retry                                  │     │
│  │     └── Release slot in finally block (line 372)                │     │
│  │  3. Router translates failed result → 429 (chat.py:746-750)     │     │
│  │                                                                  │     │
│  │  ASYNC path (chat.py:642-711):                                  │     │
│  │  1. Create execution record in database (chat.py:602-613)       │     │
│  │  2. Router acquires slot directly (chat.py:644-651)             │     │
│  │  3. If full → 429 response (chat.py:653-663)                   │     │
│  │  4. Spawn _run_async_task_with_persistence() (router pre-acquired)│    │
│  │  5. TaskExecutionService releases slot in finally (slot_already_held=True)│
│  │                                                                  │     │
│  │  PUBLIC path (public.py:315-322 → task_execution_service.py):   │     │
│  │  1. Delegate to TaskExecutionService.execute_task()              │     │
│  │     ├── Creates execution record + acquires slot internally     │     │
│  │     └── Release slot in finally block                           │     │
│  │  2. Router translates failed result → 429 (public.py:326-330)  │     │
│  │                                                                  │     │
│  │  SCHEDULED path (internal.py → task_execution_service.py):      │     │
│  │  1. Scheduler calls POST /api/internal/execute-task              │     │
│  │  2. Delegate to TaskExecutionService.execute_task()              │     │
│  │     ├── Skips record creation (execution_id pre-created)        │     │
│  │     ├── Acquires slot + tracks activity internally              │     │
│  │     └── Release slot in finally block                           │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                                                          │
│  ┌─────────────────────┐         ┌─────────────────────────────────┐    │
│  │  SQLite             │         │  Redis                          │    │
│  │  agent_ownership    │         │  agent:slots:{name} (ZSET)      │    │
│  │  max_parallel_tasks │         │  agent:slot:{name}:{id} (HASH)  │    │
│  └─────────────────────┘         └─────────────────────────────────┘    │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  Frontend (5-second polling)                                    │     │
│  │                                                                  │     │
│  │  stores/agents.js  ──► GET /api/agents/slots                   │     │
│  │  stores/network.js ──► GET /api/agents/slots                   │     │
│  │       │                                                          │     │
│  │       v                                                          │     │
│  │  CapacityMeter.vue (vertical bar with discrete cells)           │     │
│  │       │                                                          │     │
│  │       ├── Agents.vue (desktop tile, tablet tile)                │     │
│  │       └── ReplayTimeline.vue (dashboard timeline tile)          │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Data Flow

### 1. Slot Acquisition (Task Start)

There are now four paths that acquire slots, depending on the caller:

**Sync mode** (authenticated `/task` endpoint, `async_mode=false`):
```
POST /api/agents/{name}/task  (sync)
       │
       v
  chat.py:714-730 — delegates to TaskExecutionService
       │
       v
  task_execution_service.py:162-184
  ┌─────────────────────────────────────────────────┐
  │ 1. db.get_max_parallel_tasks(name)              │
  │ 2. slot_service.acquire_slot(...)               │
  │    ├── Clean stale slots (ZREMRANGEBYSCORE)     │
  │    ├── Check: ZCARD < max?                      │
  │    ├── If YES: ZADD + HSET metadata             │
  │    └── Return True/False                        │
  │ 3. If not acquired → Return failed result       │
  │    (router translates to 429)                   │
  └─────────────────────────────────────────────────┘
```

**Async mode** (authenticated `/task` endpoint, `async_mode=true`):
```
POST /api/agents/{name}/task  (async)
       │
       v
  chat.py:642-663 — router acquires slot directly
  ┌─────────────────────────────────────────────────┐
  │ 1. db.get_max_parallel_tasks(name)              │
  │ 2. slot_service.acquire_slot(...)               │
  │ 3. If not acquired → 429 Too Many Requests     │
  │ 4. Spawn `_run_async_task_with_persistence()`   │
  └─────────────────────────────────────────────────┘
```

**Public link** (`POST /api/public/chat/{token}`):
```
POST /api/public/chat/{token}
       │
       v
  public.py:315-322 — delegates to TaskExecutionService
       │
       v
  task_execution_service.py:162-184  (same as sync above)
```

**Scheduled** (cron/manual via dedicated scheduler):
```
Scheduler Service
       │
       v
  POST /api/internal/execute-task  (internal.py)
       │
       v
  task_execution_service.py:162-184  (same as sync above)
```

### 2. Slot Release (Task Complete)

```
Task completes (success or failure)
       │
       ├── Sync + Public path:
       │   task_execution_service.py:370-375 (finally block)
       │
       └── Async path:
           _run_async_task_with_persistence() → chat.py:554-557 (finally block)
       │
       v
┌─────────────────────────────────────────────────┐
│ slot_service.release_slot(agent_name, exec_id)  │
│                                                  │
│ 1. ZREM agent:slots:{name} {execution_id}       │
│ 2. DEL agent:slot:{name}:{execution_id}         │
└─────────────────────────────────────────────────┘
```

### 3. Capacity Query

```
GET /api/agents/{name}/capacity
       │
       v
┌─────────────────────────────────────────────────┐
│ 1. db.get_max_parallel_tasks(name)              │
│ 2. slot_service.get_slot_state(name, max_tasks) │
│    → active_slots, available_slots, slot_list   │
│ 3. Return AgentCapacity response                │
└─────────────────────────────────────────────────┘
```

### 4. Frontend Polling (Phase 2)

```
Every 5 seconds (agents store / network store):
       │
       ├── GET /api/agents/slots
       │   → { agents: { name: { max, active } } }
       │
       v
┌─────────────────────────────────────────────────┐
│ agents.js:  slotStats[agentName] = { max, active } │
│ network.js: node.data.slotStats = { max, active }  │
│                                                      │
│ CapacityMeter.vue renders vertical bar:              │
│   - N discrete cells (N = max)                       │
│   - Filled cells = active (bottom-to-top)            │
│   - Color by utilization %                           │
└─────────────────────────────────────────────────┘
```

## Key Files

### Database Layer (SQLite)

| File | Line | Purpose |
|------|------|---------|
| `src/backend/db/migrations.py` | 28, 59, 557-570 | Migration #21: Add max_parallel_tasks column |
| `src/backend/db/agents.py` | 519-580 | `get_max_parallel_tasks()`, `set_max_parallel_tasks()`, `get_all_agents_parallel_capacity()` |

### Redis Slot Service

| File | Line | Purpose |
|------|------|---------|
| `src/backend/services/slot_service.py` | 1-322 | SlotService class with all slot operations |
| `src/backend/services/slot_service.py` | 54-62 | SlotService.__init__() - Redis connection |
| `src/backend/services/slot_service.py` | 77-128 | acquire_slot() - Atomic slot acquisition |
| `src/backend/services/slot_service.py` | 130-152 | release_slot() - Slot release and cleanup |
| `src/backend/services/slot_service.py` | 154-199 | get_slot_state() - Detailed slot info |
| `src/backend/services/slot_service.py` | 201-221 | get_all_slot_states() - Bulk query for Dashboard |
| `src/backend/services/slot_service.py` | 247-271 | cleanup_stale_slots() - 30-min TTL enforcement |

### REST API Endpoints

| File | Line | Purpose |
|------|------|---------|
| `src/backend/routers/agents.py` | 238-263 | `GET /api/agents/slots` - Bulk slot state |
| `src/backend/routers/agents.py` | 1365-1416 | `GET /api/agents/{name}/capacity` - Get capacity |
| `src/backend/routers/agents.py` | 1419-1461 | `PUT /api/agents/{name}/capacity` - Update capacity |

### Task Endpoint Integration (EXEC-024 split)

**TaskExecutionService** (sync + public path):

| File | Line | Purpose |
|------|------|---------|
| `src/backend/services/task_execution_service.py` | 1-391 | Unified execution lifecycle service |
| `src/backend/services/task_execution_service.py` | 112-128 | `execute_task()` entry point |
| `src/backend/services/task_execution_service.py` | 162-169 | Slot acquisition (sync/public) |
| `src/backend/services/task_execution_service.py` | 171-184 | At-capacity handling (returns failed result) |
| `src/backend/services/task_execution_service.py` | 370-375 | Slot release in `finally` block |

**chat.py** (async path + router delegation):

| File | Line | Purpose |
|------|------|---------|
| `src/backend/routers/chat.py` | 20 | Import get_slot_service |
| `src/backend/routers/chat.py` | 21-23 | Import get_task_execution_service |
| `src/backend/routers/chat.py` | 642-663 | Async mode: router acquires slot directly |
| `src/backend/routers/chat.py` | 653-663 | Async mode: 429 response when at capacity |
| `src/backend/routers/chat.py` | 697 | Async mode: `release_slot=True` flag |
| `src/backend/routers/chat.py` | 370-558 | `_run_async_task_with_persistence()` with slot release in finally |
| `src/backend/routers/chat.py` | 554-557 | Async mode: slot release in `finally` block |
| `src/backend/routers/chat.py` | 714-730 | Sync mode: delegates to TaskExecutionService |
| `src/backend/routers/chat.py` | 746-750 | Sync mode: translates "at capacity" result to 429 |

**public.py** (public link path):

| File | Line | Purpose |
|------|------|---------|
| `src/backend/routers/public.py` | 26 | Import get_task_execution_service |
| `src/backend/routers/public.py` | 311-322 | Public chat delegates to TaskExecutionService |
| `src/backend/routers/public.py` | 326-330 | Translates "at capacity" result to 429 |

**internal.py** (scheduled execution path):

| File | Line | Purpose |
|------|------|---------|
| `src/backend/routers/internal.py` | 20 | Import get_task_execution_service |
| `src/backend/routers/internal.py` | 174-182 | `InternalTaskExecutionRequest` model |
| `src/backend/routers/internal.py` | 186-223 | `POST /api/internal/execute-task` delegates to TaskExecutionService |

### Pydantic Models

| File | Line | Purpose |
|------|------|---------|
| `src/backend/db_models.py` | 893-923 | `CapacityUpdate`, `SlotInfo`, `AgentCapacity`, `BulkSlotState` |

### Frontend: CapacityMeter Component (Phase 2)

| File | Line | Purpose |
|------|------|---------|
| `src/frontend/src/components/CapacityMeter.vue` | 1-61 | Vertical bar component with discrete cells |
| `src/frontend/src/components/CapacityMeter.vue` | 22-27 | Props: `active`, `max`, `height`, `width` |
| `src/frontend/src/components/CapacityMeter.vue` | 29 | `capped` computed: `Math.min(active, max)` |
| `src/frontend/src/components/CapacityMeter.vue` | 38-45 | `fillClass` computed: color thresholds by utilization % |
| `src/frontend/src/components/CapacityMeter.vue` | 49-60 | `capacity-pulse` CSS animation for 100% utilization |

### Frontend: Store Plumbing (Phase 2)

| File | Line | Purpose |
|------|------|---------|
| `src/frontend/src/stores/agents.js` | 16 | `slotStats: {}` state declaration |
| `src/frontend/src/stores/agents.js` | 616-638 | `fetchSlotStats()` action - calls `GET /api/agents/slots` |
| `src/frontend/src/stores/agents.js` | 677 | `fetchSlotStats()` called on polling start |
| `src/frontend/src/stores/agents.js` | 683 | `fetchSlotStats()` called in 5-second polling interval |
| `src/frontend/src/stores/network.js` | 51 | `slotStats` ref declaration |
| `src/frontend/src/stores/network.js` | 967-997 | `fetchSlotStats()` - calls API + threads onto `node.data.slotStats` |
| `src/frontend/src/stores/network.js` | 1008 | `fetchSlotStats()` called on polling start |
| `src/frontend/src/stores/network.js` | 1014 | `fetchSlotStats()` called in 5-second polling interval |

### Frontend: View Integration (Phase 2)

| File | Line | Purpose |
|------|------|---------|
| `src/frontend/src/views/Agents.vue` | 688 | Import CapacityMeter component |
| `src/frontend/src/views/Agents.vue` | 419-425 | Desktop layout: meter as flex sibling, `self-stretch`, width=6, height=48 |
| `src/frontend/src/views/Agents.vue` | 533-539 | Tablet layout: meter with `v-if`, width=10, height=28 |
| `src/frontend/src/views/Agents.vue` | 908-910 | `getSlotStats()` helper reads from `agentsStore.slotStats` |
| `src/frontend/src/components/ReplayTimeline.vue` | 373 | Import CapacityMeter component |
| `src/frontend/src/components/ReplayTimeline.vue` | 204-210 | Timeline tile: meter, `self-stretch`, width=6, height=52 |
| `src/frontend/src/components/ReplayTimeline.vue` | 394 | `slotStats` prop declaration |
| `src/frontend/src/components/ReplayTimeline.vue` | 667 | Thread `slotStats` onto each agent row |
| `src/frontend/src/views/Dashboard.vue` | 231 | Pass `:slot-stats="slotStats"` to ReplayTimeline |
| `src/frontend/src/views/Dashboard.vue` | 523 | Destructure `slotStats` from network store |

### Tests

| File | Line | Purpose |
|------|------|---------|
| `tests/test_capacity.py` | 1-383 | 24 tests for capacity endpoints and validation |

## Frontend Integration (Phase 2)

### CapacityMeter Component

`src/frontend/src/components/CapacityMeter.vue` -- a reusable vertical bar that shows parallel execution slot usage as discrete cells.

**Props**:

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `active` | Number | 0 | Number of currently occupied slots |
| `max` | Number | 1 | Maximum parallel slots (determines cell count) |
| `height` | Number | 36 | Height in pixels (0/falsy allows CSS flex stretch) |
| `width` | Number | 12 | Width in pixels |

**Rendering**:
- Uses `flex-col-reverse` so cells fill from bottom to top
- `gap-px` between cells, `flex-1` per cell for equal sizing
- Each cell is a `div` with `rounded-sm` and `transition-colors duration-300`
- Active cells use `fillClass`, inactive cells use `bg-gray-200 dark:bg-gray-700`
- Native `title` attribute shows `"N/M slots"` on hover

**Color Thresholds** (by `utilization = capped / max * 100`):

| Utilization | Color | Class |
|-------------|-------|-------|
| 0% | Gray | `bg-gray-200 dark:bg-gray-700` |
| 1-49% | Green | `bg-green-500` |
| 50-79% | Yellow | `bg-yellow-500` |
| 80-99% | Orange | `bg-orange-500` |
| 100% | Red + pulse | `bg-red-500` + `capacity-pulse` animation |

The `capacity-pulse` animation is a CSS keyframe that oscillates opacity between 1.0 and 0.5 on a 1.2-second cycle, applied only to filled cells when at 100% utilization.

### Store Plumbing

Both the agents store (for Agents page) and network store (for Dashboard) fetch slot data from the same endpoint.

**agents.js** (`src/frontend/src/stores/agents.js`):
- State: `slotStats: {}` (line 16) -- map of agent name to `{ max, active }`
- Action: `fetchSlotStats()` (lines 616-638) -- `GET /api/agents/slots`, parses `response.data.agents` object map
- Integrated into `startContextPolling()` (lines 669-687) -- called immediately and every 5 seconds
- Graceful degradation: 404 errors are silently ignored (endpoint may not exist in older backends)

**network.js** (`src/frontend/src/stores/network.js`):
- State: `slotStats` ref (line 51)
- Action: `fetchSlotStats()` (lines 967-997) -- same API call, but also threads data onto `node.data.slotStats` for graph nodes
- Integrated into `startContextPolling()` (lines 1000-1018) -- called immediately and every 5 seconds

### View Integration

**1. Agents.vue -- Desktop layout** (line 419-425):
- Meter sits as a flex sibling alongside the two-row content block (grid row + tags row)
- `class="ml-1 flex-shrink-0 self-stretch"` -- full tile height via flex stretching
- Props: `width=6`, `height=48`
- Always rendered (no `v-if`); defaults to `active=0, max=3` when no slot data

**2. Agents.vue -- Tablet layout** (line 533-539):
- Conditionally rendered with `v-if="getSlotStats(agent.name)"`
- Props: `width=10`, `height=28`

**3. ReplayTimeline.vue -- Dashboard timeline** (line 204-210):
- Meter added to every agent tile in the timeline agent panel
- `class="ml-1.5 flex-shrink-0 self-stretch"` -- full tile height
- Props: `width=6`, `height=52`
- Always rendered (no `v-if`); defaults to `active=0, max=3` when no slot data
- Data flows: Dashboard.vue passes `slotStats` prop (line 231) -> ReplayTimeline threads onto row objects (line 667-728)

**4. AgentNode.vue -- Dashboard graph** (NOT integrated):
- Capacity meter was tested in graph nodes but ultimately removed -- not displayed there.

## API Specifications

### GET /api/agents/{name}/capacity

**Authentication**: Required (JWT)
**Authorization**: Must have access to agent

**Response** (200):
```json
{
  "agent_name": "research-agent",
  "max_parallel_tasks": 5,
  "active_slots": 3,
  "available_slots": 2,
  "slots": [
    {
      "slot_number": 1,
      "execution_id": "abc123",
      "started_at": "2026-02-28T10:00:00",
      "message_preview": "Research the market...",
      "duration_seconds": 45
    },
    {
      "slot_number": 2,
      "execution_id": "def456",
      "started_at": "2026-02-28T10:01:00",
      "message_preview": "Analyze competitor...",
      "duration_seconds": 30
    }
  ]
}
```

### PUT /api/agents/{name}/capacity

**Authentication**: Required (JWT)
**Authorization**: Agent owner only

**Request**:
```json
{
  "max_parallel_tasks": 5
}
```

**Response** (200):
```json
{
  "message": "Capacity updated",
  "agent_name": "research-agent",
  "max_parallel_tasks": 5
}
```

**Validation Errors** (400):
- `max_parallel_tasks` not provided
- Value not an integer
- Value < 1 or > 10

### GET /api/agents/slots (Bulk)

**Authentication**: Required (JWT)
**Purpose**: Dashboard and Agents page polling endpoint (every 5 seconds)

**Response** (200):
```json
{
  "agents": {
    "research-agent": {"max": 5, "active": 3},
    "writer-agent": {"max": 3, "active": 1},
    "orchestrator": {"max": 5, "active": 5}
  },
  "timestamp": "2026-02-28T10:05:00Z"
}
```

**Note**: Response is an object map (not an array). Frontend iterates with `Object.entries()`.

### POST /api/agents/{name}/task (Updated)

**429 Response** (at capacity):
```json
{
  "detail": "Agent 'research-agent' is at capacity (5 parallel tasks). Try again later."
}
```

Headers: `Retry-After: 30` (optional, not currently implemented)

## Database Schema

### SQLite: agent_ownership table

```sql
-- Column added by migration #21
ALTER TABLE agent_ownership ADD COLUMN max_parallel_tasks INTEGER DEFAULT 3;
```

### Redis: Slot Tracking

**Slot Set (ZSET)**:
```
Key: agent:slots:{agent_name}
Type: Sorted Set
Score: Unix timestamp (execution start time)
Member: execution_id

Operations:
- ZADD agent:slots:research-agent 1709114400 "exec_abc123"
- ZCARD agent:slots:research-agent  # Count active
- ZREM agent:slots:research-agent "exec_abc123"  # Release
- ZREMRANGEBYSCORE agent:slots:research-agent 0 {30_min_ago}  # Cleanup
```

**Slot Metadata (HASH)**:
```
Key: agent:slot:{agent_name}:{execution_id}
Type: Hash
Fields:
  - started_at: ISO timestamp
  - message_preview: First 100 chars of message
  - slot_number: Assigned slot (1-N)
TTL: Dynamic (agent timeout + 5 min buffer, EXPIRE)
```

## Slot Lifecycle

As of EXEC-024, slot acquisition/release is handled by different code paths depending on the execution mode:

| Execution Mode | Slot Acquire | Slot Release |
|----------------|-------------|--------------|
| **Sync** (authenticated `/task`) | `TaskExecutionService.execute_task()` (line 164) | `TaskExecutionService` finally block (line 372) |
| **Async** (authenticated `/task`, `async_mode=true`) | `chat.py` router directly (line 646) | `_run_async_task_with_persistence()` finally (line 557) |
| **Public** (`/api/public/chat/{token}`) | `TaskExecutionService.execute_task()` (line 164) | `TaskExecutionService` finally block (line 372) |
| **Scheduled** (cron/manual via scheduler) | `TaskExecutionService.execute_task()` via `POST /api/internal/execute-task` | `TaskExecutionService` finally block (line 372) |

**Note**: Prior to EXEC-024, public link executions bypassed slot management entirely. They now go through `TaskExecutionService` and are subject to the same capacity limits as authenticated requests. As of 2026-03-09, scheduled executions also route through `TaskExecutionService` via the internal API, fixing the bug where scheduled tasks did not appear in the capacity meter.

### 1. Slot Acquisition Logic

```python
# src/backend/services/slot_service.py:77-128
async def acquire_slot(self, agent_name, execution_id, max_parallel_tasks, message_preview=""):
    # 1. Clean stale slots (> agent timeout + 5 min)
    await self._cleanup_stale_slots_for_agent(agent_name, slot_ttl)

    # 2. Check capacity
    current_count = self.redis.zcard(slots_key)
    if current_count >= max_parallel_tasks:
        return False  # At capacity

    # 3. Add slot
    self.redis.zadd(slots_key, {execution_id: time.time()})

    # 4. Store metadata with dynamic TTL
    self.redis.hset(metadata_key, mapping={...})
    self.redis.expire(metadata_key, slot_ttl)  # agent timeout + 5 min

    return True
```

### 2. Slot Release Logic

```python
# src/backend/services/slot_service.py:130-152
async def release_slot(self, agent_name, execution_id):
    # 1. Remove from ZSET
    self.redis.zrem(slots_key, execution_id)

    # 2. Delete metadata
    self.redis.delete(metadata_key)
```

### 3. Stale Slot Cleanup

**As of #869 (2026-05-17):** Staleness is checked per-slot from stored metadata, not via a single per-agent `ZREMRANGEBYSCORE` cutoff. Each slot's metadata HASH includes the `timeout_seconds` stored at acquire time; the effective TTL for that slot is `stored_timeout + SLOT_TTL_BUFFER`. The per-agent default from `agent_timeouts` is only a fallback when metadata is absent. This prevents false reclaims when a schedule's execution timeout exceeds the agent-level default.

```python
# src/backend/services/slot_service.py:223-245
async def _cleanup_stale_slots_for_agent(self, agent_name):
    # Iterate all slots in the ZSET; compute each slot's effective TTL
    # from its stored metadata (timeout_seconds + SLOT_TTL_BUFFER).
    # Falls back to per-agent default when metadata is missing.
    for execution_id in self.redis.zrange(slots_key, 0, -1):
        slot_meta = self.redis.hgetall(metadata_key)
        stored_timeout = int(slot_meta.get("timeout_seconds", agent_default_timeout))
        effective_ttl = stored_timeout + SLOT_TTL_BUFFER
        if started_at + effective_ttl < time.time():
            self.redis.zrem(slots_key, execution_id)
            self.redis.delete(metadata_key)
```

## Error Handling

| Error Case | HTTP Status | Detail |
|------------|-------------|--------|
| Agent not found | 404 | "Agent not found" |
| Access denied | 403 | "Access denied" |
| Not owner (PUT) | 403 | "Only owners can change capacity settings" |
| At capacity (task) | 429 | "Agent '{name}' is at capacity ({N} parallel tasks). Try again later." |
| Invalid capacity value | 400 | "max_parallel_tasks must be an integer between 1 and 10" |
| Missing field | 400 | "max_parallel_tasks is required" |
| Slots API 404 (frontend) | N/A | Silently ignored; meter defaults to 0/3 |

## Security Considerations

1. **Authorization**: Only agent owners can modify `max_parallel_tasks`
2. **Validation**: Strict range enforcement (1-10)
3. **Atomic operations**: Redis operations prevent race conditions
4. **TTL safety net**: Orphaned slots auto-expire (agent timeout + 5 min buffer)
5. **No credential exposure**: Message previews truncated to 100 chars

## Testing

### Test Coverage

| Test Class | Tests | Purpose |
|------------|-------|---------|
| TestCapacityAuthentication | 3 | Auth requirements for all endpoints |
| TestCapacityGet | 4 | GET /capacity structure and defaults |
| TestCapacityUpdate | 9 | PUT /capacity validation and persistence |
| TestBulkSlotState | 4 | GET /slots structure and content |
| TestSlotTracking | 1 | Slot acquisition/release during task |
| TestCapacityEnforcement | 1 | Capacity limit documentation |
| TestSlotInfoStructure | 1 | Slot detail field validation |

### Running Tests

```bash
cd tests && pytest test_capacity.py -v
```

### Manual UI Testing (Phase 2)

1. **Action**: Navigate to Agents page with running agents
   **Expected**: Each agent tile shows a vertical capacity bar on the right edge
   **Verify**: Bar is gray when no tasks running, green/yellow/orange/red as slots fill

2. **Action**: Navigate to Dashboard timeline view
   **Expected**: Each agent row in the timeline panel shows a capacity bar
   **Verify**: Bar updates every 5 seconds as tasks start/complete

3. **Action**: Trigger enough parallel tasks to fill an agent's capacity
   **Expected**: Bar turns red and pulses at 100% utilization
   **Verify**: After tasks complete, bar returns to lower utilization color

## Related Flows

- **Upstream**: [parallel-headless-execution.md](parallel-headless-execution.md) - Task endpoint that enforces capacity
- **Upstream**: [execution-queue.md](execution-queue.md) - Sequential queue (bypassed by parallel tasks)
- **Related**: [agents-page-ui-improvements.md](agents-page-ui-improvements.md) - Agents page tile layout where meter is displayed
- **Related**: [dashboard-timeline-view.md](dashboard-timeline-view.md) - Dashboard timeline where meter is displayed
- **Related**: [agent-network.md](agent-network.md) - Dashboard graph view (meter NOT displayed here)

## Future Enhancements (Phases 3-4)

### Phase 3: Timeline Swim Lanes
- Fixed N lanes per agent (N = max_parallel_tasks)
- Execution bars rendered in assigned slot lanes
- Empty lanes shown as faint placeholders

### Phase 4: MCP Tools & UI Configuration
- `set_agent_capacity(name, max)` MCP tool
- `get_agent_capacity(name)` MCP tool
- Capacity slider in Agent Detail UI
