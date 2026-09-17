# Feature: Capacity Management (CapacityManager)

## Overview

Single public facade for per-agent execution capacity. Replaces the
three-class pyramid `ExecutionQueue` + `SlotService` + `BacklogService`
with one entry point: `services/capacity_manager.py`. Issue #428 (PR #527,
Tier 2.5 of `docs/archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md`).

## Why one facade

Three primitives had grown three different "is this agent free?" stories
that had to stay in sync at every caller site. Routers were directly
composing `ExecutionQueue.acquire` + `SlotService.acquire_slot` +
`BacklogService.enqueue` and reasoning about which combination of return
values meant "admitted vs queued vs reject vs 429." `CapacityManager`
collapses that decision into a single `acquire(...)` call gated by an
`overflow_policy` argument. `SlotService` and `BacklogService` survive as
private internals (each has a distinct, well-tested job); `ExecutionQueue`
is deleted — its N=1 count gate is now `SlotService`, and its in-memory
FIFO is a Redis LIST owned inline by `CapacityManager`.

## Public API

All callers reach for `get_capacity_manager()` from
`services/capacity_manager.py`. Full signatures live in that file (~480
LOC); summary table:

| Method | Purpose |
|--------|---------|
| `acquire(agent, exec_id, max_concurrent, *, overflow_policy, overflow_payload?, breaker_enabled=False, ...)` | Try to admit; on overflow, dispatch to chosen policy. Returns `AcquireResult{state, execution_id, queue_position?}`. Raises `CapacityFull` when at capacity AND overflow is unavailable/full. Raises `CircuitOpen` (before slot acquisition) when `breaker_enabled` AND the dispatch breaker is open (#526 — see [dispatch-circuit-breaker.md](dispatch-circuit-breaker.md)). |
| `release(agent, exec_id)` | Release a slot. In-memory queue is popped (bookkeeping); persistent backlog drains via internal slot-release callback. |
| `release_if_matches(agent, exec_id)` | Watchdog-safe release: only releases if `exec_id` actually holds a slot. Returns `bool`. |
| `SlotService.renew_slot(agent, exec_id)` — **not** on the facade; reached via `get_slot_service()` (#2433) | Re-anchor a HELD slot's lease at *now*: `ZADD XX CH` score + `EXPIRE` of the metadata hash to the slot's own stored `timeout_seconds + SLOT_TTL_BUFFER`, **together**. Synchronous (worker thread). Returns `False` — never resurrects — for a slot no longer held, **and refuses (score untouched) when the metadata hash has already expired** (#2435 review: `ZADD XX` succeeds while `EXPIRE` on a missing key is a silent no-op, so the old order reported a renewal it had not performed and re-anchored exactly the ZSET-without-hash state canary S-03 reports as `missing`); fail-open. See [Lease re-anchoring at dispatch](#lease-re-anchoring-at-dispatch-2433). |
| `get_status(agent, max_concurrent)` | `QueueStatus` for the `/api/agents/{name}/queue` endpoint (current + in-memory queue only; persistent backlog is exposed via executions endpoints). |
| `get_all_states(agent_capacities)` | Bulk capacity meter for the dashboard. |
| `get_slot_state(agent, max_concurrent)` | Per-slot detail for the agent_config router. |
| `reclaim_stale(agent_timeouts?)` | Reclaim slots whose dynamic TTL has expired. Used by the cleanup watchdog. |
| `force_release(agent)` | Emergency: clear ALL slots + the in-memory queue. Returns `ForceReleaseResult`. |
| `clear_in_memory_queue(agent)` | Clear only the overflow queue (running executions untouched). |
| `cancel_all_overflow(agent, reason)` | Cancel queued (in-memory + persistent) — used on agent deletion. Returns count of persistent cancellations. |
| `run_maintenance(max_age_hours=24)` | Periodic: expire stale persistent rows + drain orphaned backlog, then run the #526 breaker-aware backstop (`_backstop_open_breaker_backlog`). Writes the `canary:drain_tick_at` heartbeat on success. Called from `main.py` 60s loop. |

`get_capacity_manager()` returns a process-wide singleton.
`reset_capacity_manager()` exists for tests.

## Fleet-wide ceiling on `max_parallel_tasks` (#506)

Per-agent `max_parallel_tasks` is a **two-tier** model: an admin sets a
fleet-wide ceiling (`max_parallel_tasks_ceiling` in `system_settings`, default
10, range 1–32; generic key/value → **no migration**), owners pick a per-agent
value within it (`PUT /api/agents/{name}/capacity` validates against the live
ceiling, not a hardcoded 10). Admin endpoints: `GET/PUT
/api/settings/max-parallel-tasks-ceiling` (range-validated, audit-logged); the
generic catch-all `PUT /{key}` is blocked for this key (422).

The clamp is **runtime / clamp-on-use** — the stored value is never rewritten,
only the *effective* admit limit (`min(stored, ceiling)`) is capped. The helpers
live in `settings_service.py`:

| Helper | Used by |
|--------|---------|
| `get_max_parallel_tasks_ceiling()` | reads the setting; fail-open to default 10 on any read error; read-side range-clamps a stray out-of-range stored value into `[1,32]` (defense-in-depth so a `0` can't fail-close the fleet / a `999` can't defeat the cap); **no per-process cache** (backend runs `--workers 2`, so a TTL cache would let a stale worker admit above a just-lowered ceiling) |
| `clamp_to_ceiling(n)` | the `CapacityManager` facade — clamped at the top of `acquire()` and inside `get_slot_state()` / `get_all_states()`, covering chat ×3, `task_execution_service`, the dashboard, and any future facade reader |
| `get_effective_max_parallel_tasks(agent)` | the two genuine facade-bypasses: `backlog_service.drain_next` (the `real_slot` re-acquire reuses the same clamped local) and `agent_call_limiter` |

Canary **B-02** compares slot count against the **effective** cap (snapshot
carries `effective_max_parallel`) so a lowered ceiling doesn't false-fire;
**S-02** (no overbooking) keeps the stored cap as a valid upper bound (clamping
only lowers ZCARD vs stored). The `agent_config` GET surfaces `ceiling` +
`effective_max_parallel_tasks`; `available_slots` is computed from the effective
value (`available + active = effective`).

*Known limitation:* `agent_call_limiter` freezes its per-agent semaphore cap at
first access — a live agent's semaphore doesn't shrink on a ceiling/cap drop
until process restart (new agents get the clamped cap immediately). Semaphore
resize is out of scope.

**Frontend surfaces.** Admin sets the ceiling in `views/Settings.vue` ("Fleet
capacity ceiling", range `ceilingMin`–`ceilingMax`) via
`stores/settings.js` `getMaxParallelTasksCeiling()` / `setMaxParallelTasksCeiling(value)`.
Owners pick the per-agent value in `components/CapacityPanel.vue` ("Parallel
Capacity", `:max="ceiling"`) via `stores/agents.js`
`getAgentCapacity(name)` / `setAgentCapacity(name, n)` — the panel reads
`ceiling`/`effective_max_parallel_tasks` from the one `GET .../capacity` call,
so it renders the bound without the admin-only settings GET.

## Overflow policies

Three modes selected per call via the `overflow_policy` keyword:

| Policy | Behavior at capacity | When to use |
|--------|----------------------|-------------|
| `reject` | Raises `CapacityFull(reason="rejected")`. | Internal callers that have already pre-acquired upstream — e.g., `TaskExecutionService` (the router admitted the slot; the service is just being defensive). |
| `queue_in_memory` | LPUSH onto Redis LIST `agent:queue:{name}` bounded by `IN_MEMORY_DEPTH=3`. Returns `state="queued_in_memory"` with a 1-based `queue_position`. The caller still proceeds — the agent's Claude subprocess is the real serialization point; the queue exists for observability + crude rate limiting. Raises `CapacityFull(reason="in_memory_full")` at depth 3. | `/chat` (synchronous HTTP, short request, caller is blocked anyway). |
| `queue_persistent` | Marks the pre-created `schedule_executions` row `status='queued'` with `queued_at` + `backlog_metadata`. Returns `state="queued_persistent"`. Caller should reply 202 Accepted; the drain reconstructs the request later. Raises `CapacityFull(reason="persistent_full")` if the backlog is at its configured depth. Requires `overflow_payload: PersistentTaskPayload`. | `/task` (async + sync long-poll variants). Restart-durable. |

## Dispatch circuit-breaker gate (#526)

`acquire()` is the single chokepoint where the per-agent **dispatch breaker**
(RELIABILITY-007) gates new work. Step 0 — *before* slot acquisition and the
overflow branch — runs when the caller passes `breaker_enabled=True` AND the
global `DISPATCH_BREAKER_ENABLED` master switch is on:

```python
if breaker_enabled:                       # per-agent opt-in short-circuits the global read
    from config import DISPATCH_BREAKER_ENABLED
    if DISPATCH_BREAKER_ENABLED:
        breaker = DispatchBreaker(agent_name)
        if not breaker.allow_dispatch():  # deny → open within cooldown / sibling holds probe
            raise CircuitOpen(agent_name, breaker.retry_after_seconds())
        # allow_dispatch() True = CLOSED (no probe consumed) OR we hold the
        # half-open PROBE. A probe must lead to a REAL dispatch — spending it on
        # a backlog enqueue records no verdict and stalls backoff (#526 F1) — so
        # when the breaker is open, admit the probe ONLY into a free slot:
        if breaker.to_dict().get("state") == "open":
            if await self._slots.acquire_slot(...):
                return AcquireResult(state="admitted", execution_id=execution_id)
            raise CircuitOpen(agent_name, breaker.retry_after_seconds())  # full → fast-fail, NEVER enqueue
```

**No-enqueue invariant (#526 D2 + F1):** `CircuitOpen` is raised *before* the
`queue_persistent` / `queue_in_memory` branch — both for a `deny` and for a
half-open probe that can't get a free slot — so a doomed task is never written
to the backlog (the invariant now spans the half-open window too). The whole
point is to stop poisoning the persistent queue when an agent is auth-dead.
`/chat` doesn't feed the breaker, so it gates with a **pure state read**
(fast-fail 503 while open) and never consumes a probe; only the recording
`/task` path drives recovery. Callers map `CircuitOpen` to HTTP 503 +
`X-Circuit-Open` / `Retry-After` and close the pre-created `schedule_executions`
row `FAILED(circuit_open)`. When `breaker_enabled=False` (the default), the gate
is skipped entirely — disabled agents pay zero added cost on the dispatch hot
path (R3: `acquire` is byte-for-byte unchanged when off).

`run_maintenance()` also carries the breaker's recovery backstop:
`_backstop_open_breaker_backlog()` re-fails the queued backlog (FAILED) for any
agent whose dispatch breaker is still open — so a lost inline drain (the
fire-and-forget `fail_queued_for_agent` on the →open transition, owned by
`task_execution_service`) self-corrects in ~60s instead of waiting out the 24h
`expire_stale` window. Bounded to agents with queued rows; gated on the global
switch; never raises. Full breaker mechanics, the two-breaker split, and outcome
recording at the execution terminals live in
[dispatch-circuit-breaker.md](dispatch-circuit-breaker.md).

## Lease re-anchoring at dispatch (#2433)

A slot's lease is set at **admission**: `acquire_slot` writes `ZADD score=now`
and `EXPIRE metadata_hash (timeout_seconds + SLOT_TTL_BUFFER)` in one breath, and
`reclaim_stale` → `_cleanup_stale_slots_for_agent` later judges each slot by
`now − score > stored timeout + 300`. Between admission and the agent actually
receiving the turn sits a queue this module never modelled — the backend
agent-call semaphore in `services/agent_call_limiter.py`, acquired inside
`agent_post_with_retry` *after* the row is `running` and the slot held. A park
there spent the lease without running: `park + run > timeout + 300` measured
from admission got the slot TTL-reclaimed under a live execution (Phase 3 then
saw the agent still running it, skipped the FAILED write, and the agent ran
overbooked), and the same admission-anchored `started_at` let the registry-blind
Phase-1 `mark_stale_executions_failed` bulk-FAIL the row mid-run. The backlog
spill already defines `started_at` as *left the queue* (`db/schedules/queue.py`);
the limiter is a second, hidden queue, so the same rule applies to the lease.
Canonical description: architecture.md → *In-Flight Dispatch Proof-of-Life
(#2433)*.

`SlotService.renew_slot(agent_name, execution_id)` is the primitive:

- `ZADD … XX CH` score=now on `agent:slots:{name}` — `XX` so a released or
  TTL-reclaimed member is **never resurrected** (a renewal of a missing member
  returns `False` and leaves `ZCARD` alone), `CH` so the return value says
  whether anything moved.
- `EXPIRE agent:slot:{name}:{exec_id}` to the slot's **own** stored
  `timeout_seconds + SLOT_TTL_BUFFER` (`DEFAULT_SLOT_TTL_SECONDS` when the hash
  is unreadable), issued **with** the score move — canary S-03 reconstructs the
  initial TTL as `ttl_remaining + (read − score)`, so moving only one of the two
  would page `below_floor` on every renewed slot.
- Synchronous on purpose (called via `asyncio.to_thread` at grant and from the
  limiter refresher's worker thread); any Redis error → `False`, logged at
  DEBUG. Fail-open bookkeeping: a failed renewal degrades to today's
  admission-anchored lease, never to a blocked dispatch.

Two callers, both outside the facade — a third documented facade bypass beside
`backlog_service.drain_next` and `agent_call_limiter`'s cap read (#506):

| Caller | When | Paired with |
|--------|------|-------------|
| `agent_call_limiter._tick_sync` (the ONE per-process refresher) | every `INFLIGHT_TICK_SECONDS` (15s) tick, for every in-flight entry whose `phase == "parked"` | the cross-worker `execution:inflight:{execution_id}` marker refresh — so a park can never outlive a short per-schedule slot TTL |
| `task_execution_service._on_dispatch_granted` | once at semaphore grant, when the park reached `DISPATCH_RESTAMP_THRESHOLD_SECONDS` (5s) | `db.restamp_execution_dispatch(execution_id)` — `started_at = now`, admission kept in `queued_at`, CAS on `RUNNING` + NULL lease — so row clock and slot lease move together and `duration_ms` records the run, not the park |

A fast acquire (park < 5s) renews nothing — the hot path pays no Redis write.
An execution cancelled **while parked** releases its slot through the existing
`release_if_matches` (`chat_execution_service._cancel_inflight_if_parked`); the
dispatcher's own late FAILED write then loses the CAS, so a renewed lease never
outlives its CANCELLED row. Dispatch wiring:
[task-execution-service.md](task-execution-service.md); the watchdog's tri-state
`inflight_verdicts` read that withholds `release_if_matches` for a live
dispatcher: [cleanup-service.md](cleanup-service.md).

### Tests

- `tests/unit/test_2433_slot_renew.py` — score and hash TTL move together and
  the S-03 reconstruction stays at the floor; the slot's own stored timeout is
  used (a 7200s slot renews past 7200); default TTL without metadata; `False`
  (and `ZCARD` still 0) for a released slot; fail-open on a broken client.
- `tests/unit/test_2433_limiter_inflight.py::test_refresher_renews_slot_lease_only_while_parked`
  — the refresher renews `parked` entries and stops once they flip to `calling`.
- `tests/unit/test_2433_dispatch_wiring.py::test_park_at_grant_restamps_row_and_renews_slot`
  — exactly one `restamp_execution_dispatch` + one `renew_slot` per grant after a park.

## End-to-end flow

### `/chat` — short synchronous, in-memory queue

`src/backend/routers/chat.py` (chat endpoint):

1. Resolve `max_parallel_tasks` from agent ownership row. (Callers pass the **stored** value; `acquire` clamps it to the fleet ceiling internally — #506.)
2. `await capacity.acquire(agent_name=..., execution_id=..., max_concurrent=N, overflow_policy="queue_in_memory", source=USER, message=...)`.
3. On `state="admitted"` or `"queued_in_memory"`, proceed to call the agent container. (The in-memory queue position is informational; the agent serializes Claude subprocess execution itself.)
4. On `CapacityFull(reason="in_memory_full")` → 429 to client.
5. In `finally`: `await capacity.release(agent_name, execution_id)` — releases the slot AND pops the next in-memory bookkeeping entry.

### `/task` async — at-capacity → backlog → drain on release

`src/backend/routers/chat.py` (parallel task endpoint, async mode):

1. Create `schedule_executions` row eagerly via `db.create_task_execution` so the caller has an `execution_id` to return.
2. Build `PersistentTaskPayload(request, effective_timeout, user_id, ...)`.
3. `await capacity.acquire(..., overflow_policy="queue_persistent", overflow_payload=payload)`.
4. On `state="admitted"`: spawn the background task as usual.
5. On `state="queued_persistent"`: return 202 with the existing `execution_id` — no work happens yet.
6. On `CapacityFull(reason="persistent_full")` → 429 (backlog is also at depth).

When ANY slot for that agent is released later (any caller, any policy), `CapacityManager._on_slot_released` fires (registered with `SlotService.register_on_release` in the constructor), which calls `BacklogService.drain_next(agent_name)`. The drain atomically claims one queued row and re-spawns the persisted request via `_run_async_task_with_persistence`. This is the path that survives a backend restart — the rows are durable; the orphan-drain in `run_maintenance()` resumes them on the next boot.

The sync `/task` long-poll variant uses the same `queue_persistent` path and waits on `services/sync_waiter.py` for the eventual drain to flip the row to terminal state (#498).

### Termination

`src/backend/routers/chat.py` terminate endpoint calls
`capacity.force_release(agent_name)` to clear all slots + the in-memory
queue at once.

## Storage map

Keys/columns are intentionally unchanged from the predecessor classes so
in-flight executions across the upgrade keep working.

| Concern | Storage | Key / column |
|---------|---------|--------------|
| Active slot counter (per agent) | Redis ZSET | `agent:slots:{name}` (member = exec_id, score = unix ts of admission — moved to the dispatch instant by `renew_slot` after a park, #2433) |
| Per-slot metadata | Redis HASH | `agent:slot:{name}:{exec_id}` (auto-expires via dynamic TTL = `agent.timeout + 5min` buffer, set at admission and re-armed by `renew_slot` — always together with the score, #2433) |
| In-memory overflow queue | Redis LIST | `agent:queue:{name}` (LPUSH new, RPOP oldest, depth ≤ `IN_MEMORY_DEPTH=3`) |
| Persistent overflow backlog | SQLite | `schedule_executions` rows where `status='queued'` (driven by `queued_at` ASC for FIFO; `backlog_metadata` JSON holds the request to replay; partial index `idx_executions_queued`) |

## Maintenance & recovery

Two periodic loops keep capacity state honest:

- **`main.py` 60s loop → `capacity.run_maintenance(max_age_hours=24)`** —
  expires `status='queued'` rows older than 24h to FAILED, then calls
  `_backlog.drain_orphans_all()` to resume any backlog rows that didn't
  get a release callback (typically after a backend restart between
  enqueue and drain).
- **`services/cleanup_service.py` watchdog (5min tick)** — calls
  `capacity.reclaim_stale(agent_timeouts={...})` to release slots whose
  per-agent dynamic TTL has expired; uses `release_if_matches(agent, exec_id)` (TOCTOU-safe) when reconciling individual orphaned executions so it only releases slots the targeted execution actually holds.
  Since #2433 a slot whose execution is parked in the backend agent-call queue is renewed every 15s by the limiter's refresher, so `reclaim_stale`'s `now − score` age never crosses the TTL under a live dispatcher — and the watchdog consults `agent_call_limiter.inflight_verdicts` before recovering (and releasing) a row the agent does not know, withholding on `alive` and, for rows younger than `inflight_max_age_seconds()`, on `unknown` (see [cleanup-service.md](cleanup-service.md)).

## What replaced what

The CapacityManager facade is the only public surface. The earlier docs
(`execution-queue.md`, `parallel-capacity.md`, `persistent-task-backlog.md`)
now describe internal implementation details rather than independent caller
APIs.

| Old concept | CapacityManager equivalent |
|-------------|----------------------------|
| `ExecutionQueue.acquire(...)` (N=1 mutex + in-memory FIFO) | `acquire(..., overflow_policy="queue_in_memory")` with `max_concurrent=1` |
| `ExecutionQueue.release(...)` | `release(...)` |
| `ExecutionQueue.get_status(...)` | `get_status(...)` |
| `ExecutionQueue.force_release(...)` | `force_release(...)` |
| `SlotService.acquire_slot(...)` | `acquire(..., overflow_policy="reject")` |
| `SlotService.release_slot(...)` | `release(...)` |
| `SlotService.cleanup_stale_slots(...)` | `reclaim_stale(...)` |
| `SlotService.get_slot_state(...)` / `get_all_slot_states(...)` | `get_slot_state(...)` / `get_all_states(...)` |
| `SlotService.force_clear_slots(...)` | `force_release(...)` (combined with in-memory clear) |
| `BacklogService.enqueue(...)` | `acquire(..., overflow_policy="queue_persistent", overflow_payload=...)` |
| `BacklogService.drain_next(...)` | Internal: fired by SlotService release callback wired in `__init__` |
| `BacklogService.expire_stale(...)` + `drain_orphans_all(...)` | `run_maintenance(...)` |
| `BacklogService.cancel_all_backlog(...)` | `cancel_all_overflow(...)` (also clears in-memory) |
| Manual wiring of `SlotService.register_on_release(BacklogService.drain_next)` in `main.py` | Done internally in `CapacityManager.__init__` |

## Caller sites

| Caller | Location | Policy |
|--------|----------|--------|
| `/chat` | `src/backend/routers/chat.py` | `queue_in_memory` |
| `/task` async | `src/backend/routers/chat.py` | `queue_persistent` |
| `/task` sync long-poll | `src/backend/routers/chat.py` (waits on `sync_waiter`) | `queue_persistent` |
| Terminate endpoint | `src/backend/routers/chat.py` | `force_release` |
| `TaskExecutionService` | `src/backend/services/task_execution_service.py` | `reject` (router pre-acquired) — or `queue_persistent` when the agent is a `PULL_MODE_PILOT_AGENTS` pilot and `pull_owns_dispatch` claims the trigger (`schedule`/`webhook`/`reminder` #2391, `loop` #2523, `fan_out` #2524). The pilot-gated branch is what lets scheduled work be claimed by a pull worker; every non-pilot agent is unchanged. |
| Cleanup watchdog | `src/backend/services/cleanup_service.py` | `reclaim_stale` + `release_if_matches` |
| Limiter refresher (parked entries, 15s tick) | `src/backend/services/agent_call_limiter.py` (`_tick_sync`) | `SlotService.renew_slot` — direct, not via the facade (#2433) |
| Dispatch grant after a park ≥5s | `src/backend/services/task_execution_service.py` (`_on_dispatch_granted`) | `SlotService.renew_slot` — direct, beside `db.restamp_execution_dispatch` (#2433) |
| Terminate — execution still parked | `src/backend/services/chat_execution_service.py` (`_cancel_inflight_if_parked`) | `release_if_matches` (#2433) |
| Maintenance tick | `src/backend/main.py` (60s loop) | `run_maintenance` |
| `/api/agents/{name}/queue` | `src/backend/routers/agents.py` | `get_status` |
| Dashboard capacity meter | agent_config router | `get_all_states`, `get_slot_state` |
| Agent deletion | `src/backend/routers/agents.py` | `cancel_all_overflow` |

## Issue references

- **#428** — Tier 2.5 facade work (this flow). PR #527, branch `feature/428-capacity-manager`.
- **#526 (RELIABILITY-007)** — `acquire(breaker_enabled=…)` dispatch-breaker gate (raises `CircuitOpen` before overflow) + the `run_maintenance` backstop. See [dispatch-circuit-breaker.md](dispatch-circuit-breaker.md).
- **#2433** — `SlotService.renew_slot` + lease re-anchoring at dispatch: `park + run` must not exceed `timeout + 300` measured from admission, so the limiter's refresher renews a parked slot every tick and the grant after a park ≥5s renews it once more beside `db.restamp_execution_dispatch`. See [Lease re-anchoring at dispatch](#lease-re-anchoring-at-dispatch-2433).
- **CAPACITY-001** — `SlotService` (now internal). See [parallel-capacity.md](parallel-capacity.md).
- **BACKLOG-001 (#260)** — `BacklogService` (now internal). See [persistent-task-backlog.md](persistent-task-backlog.md).
- **EXEC-024** — `TaskExecutionService` consumer. See [task-execution-service.md](task-execution-service.md).
- **TIMEOUT-001** — per-agent dynamic slot TTL.
- `docs/archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md` — Tier 2.5 (Simplification) plan; this issue is the cornerstone of "one capacity surface, three policies."

## Related flows

- [parallel-capacity.md](parallel-capacity.md) — `SlotService` internals (Redis ZSET counter, dynamic TTL). Now an internal-only module reachable through `CapacityManager`.
- [persistent-task-backlog.md](persistent-task-backlog.md) — `BacklogService` internals (SQL row state machine, FIFO claim, drain). Now an internal-only module reachable through `CapacityManager`.
- [execution-queue.md](execution-queue.md) — historical doc for the deleted `ExecutionQueue` class. Behavior preserved via `acquire(..., max_concurrent=1, overflow_policy="queue_in_memory")` + `release(...)`.
- [task-execution-service.md](task-execution-service.md) — primary internal consumer (uses `reject` policy).
- [parallel-headless-execution.md](parallel-headless-execution.md) — `/task` endpoint flow; the persistent path is the queue-overflow story.
- [cleanup-service.md](cleanup-service.md) — uses `reclaim_stale` + `release_if_matches` for stale-slot recovery.
- [execution-termination.md](execution-termination.md) — uses `force_release`.
- [chat-turn-cancellation.md](chat-turn-cancellation.md) — #2433 parked-cancel arm: `_cancel_inflight_if_parked` releases the slot via `release_if_matches` before the dispatcher's grant, which then raises `BackendAgentCallCancelled` instead of POSTing.
- [dispatch-circuit-breaker.md](dispatch-circuit-breaker.md) — #526 per-agent dispatch breaker; `acquire()` is its enforcement chokepoint (the Step 0 gate) and `run_maintenance` carries its backlog backstop.
