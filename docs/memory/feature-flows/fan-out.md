# Feature: Fan-Out Parallel Task Dispatch (FANOUT-001)

## Overview
Dispatches N independent tasks to an agent in parallel (throttled by an asyncio semaphore) and returns aggregated per-task results — or, with `async_mode`, a receipt to poll. Each subtask follows the standard TaskExecutionService path for full dashboard observability.

**Since #2524 the aggregate is a QUERY over `fan_out_id`, not a dict of `execute_task` return values.** That is what lets a fan-out run on the durable pull queue (a pull-claimed subtask returns no `TaskExecutionResult` to collect) and what lets a batch be read after the request that started it is gone.

## Recent Changes
- **#2524 (async fan-out join)**: the sync aggregate is rebuilt from the batch's rows. Every subtask row carries `fan_out_id` plus the caller's own `fan_out_task_id` (new column, surfaced as `task_id` on the #2670 GET), rows are created at slot grant (never up front — see "Row lifecycle"), and a queued subtask is waited on through `sync_waiter.wait_for_fan_out_batch`, woken by the terminal fan-out. Adds `async_mode` (backend + MCP). The outer deadline bounds the wait, not the work; with no deadline the wait covers `ceil(N / concurrency)` waves. `fan_out` joins `pull_pilot.PULL_REACHABLE_TRIGGERS`.
- **Issue #2670 (fan_out answers with a fan_out_id, and a batch can be polled)**: a batch is now resolvable after the fact. The new `GET /api/agents/{name}/fan-out/{fan_out_id}` rebuilds the aggregate from the `schedule_executions` rows stamped with `fan_out_id` at dispatch, and answers while the batch is still running. MCP `fan_out` is now bounded by `MCP_CHAT_TIMEOUT_MS` (not `(timeout_seconds ?? 7200) + 60`) and on gateway abort returns a `{status: "fan_out_timeout", fan_out_id, ...}` receipt; the new `get_fan_out_result` tool polls it. The batch id is attached to the idempotency claim the moment it is minted (`on_started` hook), so an in-flight duplicate 409 now carries something pollable.
- **Issue #418 (feature/418-inter-agent-timeout)**: `timeout_seconds` is now optional and governs only the outer fan-out-wide deadline. Individual subtasks are always bounded by the target agent's configured `execution_timeout_seconds` (TIMEOUT-001). Previously a hardcoded 600s default capped every subtask regardless of per-agent configuration.

## User Story
As an agent orchestrator, I want to fan out multiple independent tasks to an agent in parallel so that embarrassingly parallel workloads (batch predictions, parallel analysis, ensemble methods) complete faster than sequential execution.

## Entry Points
- **API**: `POST /api/agents/{name}/fan-out` -- authenticated endpoint (dispatch)
- **API**: `GET /api/agents/{name}/fan-out/{fan_out_id}` -- authenticated endpoint (poll a batch, #2670)
- **MCP**: `fan_out` tool (dispatch) and `get_fan_out_result` tool (poll, #2670) registered in MCP server

No frontend UI entry point exists; this is an API/MCP-only feature.

## MCP Layer

### Tool Registration
- `src/mcp-server/src/server.ts:595` -- `createChatTools(...)` in the `toolGroups` list (registers `fan_out`); `server.ts:609` -- `createExecutionTools(...)` (registers `get_fan_out_result`). Every group is registered through `addAllTools()` → `addToolWithAudit()` (`server.ts:583`), never a direct `server.addTool`.

### Tool Definition — `fan_out`
- `src/mcp-server/src/tools/chat.ts:602-732` -- `fan_out` tool
- Parameters: `agent_name`, `tasks[]`, `timeout_seconds` (optional; bounds the wait, not the subtasks — when omitted the backend waits out the whole batch), `max_concurrency`, `model`, `system_prompt`, `allowed_tools`, `async_mode` (#2524 — return `{fan_out_id, status: "accepted"}` immediately; poll with `get_fan_out_result`)
- Access control: calls `checkAgentAccess()` (same rules as `chat_with_agent`)
- Derives a deterministic `Idempotency-Key` over `[caller, agent_name, "fan_out", model, JSON.stringify(tasks)]` (plus `"async"` when `async_mode` is set, so a sync call never replays an async receipt) (`deriveMcpIdempotencyKey`, RELIABILITY-006) — an *identical* re-send replays the same batch, a *reworded* one derives a new key and dispatches all N tasks again. The tool description now spells this out together with the `fan_out_timeout` receipt shape (#2670).
- Delegates to `TrinityClient.fanOut()`; returns `FanOutDispatchResult` **or** `FanOutTimeoutReceipt` as JSON

### Tool Definition — `get_fan_out_result` (#2670)
- `src/mcp-server/src/tools/executions.ts:200-247` -- `get_fan_out_result` tool
- Parameters: `agent_name`, `fan_out_id` (returned by `fan_out`, or carried on a `fan_out_timeout` receipt)
- Access control: `checkAgentAccess()` — the same `{self} ∪ permitted` gate as `get_execution_result` beside it (Invariant #13 / #1104)
- Delegates to `TrinityClient.getFanOutResult()`; returns `FanOutBatchStatus` as JSON

### Client Methods
- `src/mcp-server/src/client.ts:1377-1520` -- `fanOut()` method
- Sets headers: `Authorization`, `X-Via-MCP`, `X-Source-Agent`, `Idempotency-Key`; `X-MCP-Key-ID` / `X-MCP-Key-Name` are still sent but inert (#2389 — the backend derives provenance from the presented bearer)
- Builds request body with `tasks`, `agent`, `max_concurrency`, `policy`, `model`, `system_prompt`, `allowed_tools`; `timeout_seconds` is conditionally spread in only when the caller provided it, so the backend sees `None` on omission and falls back to per-agent `execution_timeout_seconds`
- HTTP ceiling (#2670): bounded by `MCP_CHAT_TIMEOUT_MS` (default 25000 ms — the same knob `/chat` and `/task` use, `||` not `??` for the #1076 empty-string shadow) via `AbortController`. The old `(timeout_seconds ?? 7200) + 60` ceiling (#418) was correct against the backend but irrelevant to the party that gives up first: the MCP client's own 30–60s gateway timeout killed the JSON-RPC call long before 7260s and the caller saw a bare `fetch failed` while N executions kept running.
- On `AbortError` **only** (not `TypeError` — a request that never landed must not be attributed to a batch), `findRecentFanOut()` reads the last 50 rows via `getRecentExecutionsForRecovery()` (`GET /api/agents/{name}/executions?limit=50`, separately bounded by `MCP_RECOVERY_TIMEOUT_MS`, no 401-reauth) and `pickRecentFanOut()` (`client.ts:203`) picks the batch. Filters: `triggered_by === "fan_out"`, `fan_out_id` present, `source_mcp_key_id` matches when both sides have one, the row's `message` is one this call dispatched, `started_at` within `timeoutMs + 10s`. Status is deliberately not filtered (a live batch is a mix of done and running rows). Exactly one distinct `fan_out_id` → `fanOutReceipt()`; zero or more than one → a loud error pointing at `list_recent_executions` and warning that a reworded re-send double-dispatches.
- A `409` in-flight duplicate is read via `extractIdempotencyExecutionId()` — for this route the id is the **batch** id — and turned into the same receipt (`"A fan-out with this idempotency key is already running"`); otherwise `API error (409)`.
- Calls `POST /api/agents/{name}/fan-out`
- Receipt design, attribution rules, and why the *batch* (not the row) is the unit of ambiguity: see [mcp-orchestration.md](mcp-orchestration.md) → "Gateway-Timeout Receipt — all three sync routes" → "Route three — `fan_out` (#2670)". Not repeated here.

- `src/mcp-server/src/client.ts:1582-1587` -- `getFanOutResult(agentName, fanOutId)` → `GET /api/agents/{name}/fan-out/{fan_out_id}` through the ordinary `request()` (the caller's full budget — this is a normal tool call, not an abort-path lookup)

### Types
- `src/mcp-server/src/types.ts:288` -- `FanOutDispatchResult` (the completed `POST /fan-out` aggregate)
- `src/mcp-server/src/types.ts:315` -- `FanOutTimeoutReceipt`: `{status: "fan_out_timeout", agent, fan_out_id, execution_ids, task_count, message}`. `execution_ids` is the rows found at abort time and may be a **subset** (a slot-starved subtask has no row yet) — evidence, never a manifest.
- `src/mcp-server/src/types.ts:327` -- `FanOutBatchStatus` (mirror of the backend model below)
- `src/mcp-server/src/types.ts:280` -- `ScheduleExecution.fan_out_id` (the list-row field `pickRecentFanOut()` groups on)

## Backend Layer

### Router
- `src/backend/routers/fan_out.py` -- imported at `main.py:61`, registered via `app.include_router(fan_out_router)` at `main.py:1290`
- Prefix: `/api/agents`, tag: `fan-out`

### Request Validation (Pydantic)
```python
class FanOutRequest(BaseModel):
    tasks: List[FanOutTask]                    # 1-50 tasks, unique IDs
    agent: str = "self"                        # v1: self-only
    timeout_seconds: Optional[int] = None      # 10-3600 when set; None = per-agent default (#418)
    max_concurrency: int = 3                   # 1-10
    policy: str = "best-effort"                # only value supported
    model: Optional[str]
    system_prompt: Optional[str]
    allowed_tools: Optional[List[str]]
    async_mode: Optional[bool] = False         # #2524: accept + poll instead of blocking
```

- Task IDs: regex `^[a-zA-Z0-9_-]{1,64}$`, must be unique
- Max tasks: 50 (`MAX_TASKS`)
- Max concurrency: 10 (`MAX_CONCURRENCY`)
- Timeout range: 10-3600 seconds (or `None` for per-agent default, #418). The validator short-circuits and returns `None` when the field is omitted.
- Policy: only `"best-effort"` supported in v1
- Cross-agent fan-out (`agent != "self"` and `agent != name`): returns 400

### Endpoint Handler
- `src/backend/routers/fan_out.py:44` -- `fan_out()` (`@router.post("/{name}/fan-out", response_model=FanOutResponse)`)
- Auth: `get_current_user` + `get_authorized_agent`
- Origin tracking headers: `X-Source-Agent`, `X-Via-MCP`; MCP key provenance comes from the presented bearer (`current_user.mcp_key_id` / `mcp_key_name`, #2389), not from headers
- Router header (line 1): `# mcp: chat.ts (fan_out) + executions.ts (get_fan_out_result → GET /{name}/fan-out/{fan_out_id})`

### Business Logic Flow
1. Validate `request.agent` is `"self"` or matches path `name` (v1 restriction)
2. `idempotency_service.begin(make_agent_scope(name), idempotency_key)` (RELIABILITY-006): a replay that is still in flight → 409 (shape below); a finished replay → the stored `FanOutResponse` snapshot with `X-Idempotent-Replay: true`
3. Convert `FanOutTask` list to `FanOutTaskInput` dataclasses
4. Determine `source_agent` from header or path name
5. Call `FanOutService.execute()` with all parameters + origin tracking fields + `on_started=lambda fid: idempotency_service.attach_execution(idem, fid)` (#2670 — the batch id lands on the claim the moment it is minted, not at `complete()`)
6. Map `FanOutResult` to `FanOutResponse` Pydantic model; `idempotency_service.complete(idem, fan_out_id, response.model_dump())` stores the aggregate as the replay snapshot

### Batch status endpoint (#2670)
- `src/backend/routers/fan_out.py:181-211` -- `get_fan_out_status()` (`@router.get("/{name}/fan-out/{fan_out_id}", response_model=FanOutBatchStatus)`)
- Auth: `get_authorized_agent` on the path — the same dependency the POST uses, and the only access decision on this route

Why it exists: `POST /fan-out` builds its aggregate in memory and returns it exactly once. A caller whose HTTP call was killed by its own gateway timeout therefore had nothing to poll while N executions kept running — the third route of the #914 class (`/chat` #914, `/task` #2661), and the one that exceeds the ceiling most reliably, since a batch runs longer than any single task in it by construction. The GET deliberately reads `schedule_executions`, **not** the idempotency snapshot: the snapshot is written by `complete()`, i.e. only once the whole batch has finished, so it cannot answer the question a timed-out caller is actually asking — *what is happening right now*. The rows can, because `fan_out_id` is stamped on each of them at dispatch (FANOUT-001). `test_the_route_reads_rows_not_the_idempotency_snapshot` pins this.

Flow:
1. Shape check `_FAN_OUT_ID_RE = ^fo_[A-Za-z0-9_-]{1,64}$` (`fan_out.py:178`) — the id is server-minted (`fo_` + `secrets.token_urlsafe(12)`), so this only keeps junk out of the query; a miss is a **404**, never a 400 (a "that isn't an id shape" answer would be free information), and the DB is not touched
2. `db.get_fan_out_executions(name, fan_out_id)` — an empty result is a **404**, not an empty aggregate (`{total: 0, status: "completed"}` would tell a polling caller their batch finished)
3. `build_fan_out_batch_status(name, fan_out_id, rows)` → `FanOutBatchStatus`

Enumeration-safe (Invariant #8): a malformed id, an unknown id, and an id belonging to another agent are one uniform `404 "Fan-out not found"`.

#### `build_fan_out_batch_status()` -- `src/backend/services/fan_out_service.py:261-332`
A pure fold over execution-row dicts — no DB, no clock, no `await` (`test_the_fold_is_pure` greps the source for them). Lives in the service rather than the router because "what does this batch add up to" is a business question (Invariant #1).
- Rows without an `id` are dropped, not counted
- Per-task `status` is the **execution** status verbatim (`queued`, `running`, `pending_retry`, `success`, `failed`, `cancelled`, `skipped`, …) — NOT the dispatch response's `completed`/`failed` pair. A poll of a live batch has to distinguish "waiting for a slot" from "running", and a two-value vocabulary would report a healthy queued subtask as a failure.
- Counts: `running` = rows whose status is in `_NON_TERMINAL = {queued, running, pending_retry}`; `completed` = rows with `status == "success"`; `failed` = `total - running - completed` (so `cancelled` and `skipped` are failures — only `success` is a success). `pending_retry` is the one that is easy to miss: a subtask awaiting a #271 retry is neither done nor lost, and counting it as failed would tell a polling caller the batch is finished while a row is about to run again.
- Batch `status`, decided **in this order**: `running` if any row is non-terminal; else `completed` if `failed == 0`; else `failed` if `completed == 0`; else `partial`. `running` outranks every verdict because reporting one early is what makes a polling caller stop polling. `partial` exists because best-effort is the default policy — four of five succeeding is neither a success nor a failure.
- `deadline_exceeded` is absent by construction: it is the *dispatcher's* verdict on its own outer deadline, held in memory by the POST that timed out, and not a property of any row — so this surface cannot observe it and does not invent it.

#### `on_started` hook -- `src/backend/services/fan_out_service.py:85-122`
`FanOutService.execute(on_started: Optional[Callable[[str], None]] = None)` calls the hook exactly once with the freshly minted `fan_out_id`, immediately after `secrets.token_urlsafe(12)` and **before** `get_task_execution_service()`, the semaphore, or any subtask dispatch. It is wrapped in `try/except Exception` → `logger.warning("[FanOut] %s on_started hook raised; continuing", ..., exc_info=True)`: bookkeeping must never fail a batch that is otherwise fine. The router passes `idempotency_service.attach_execution(idem, fid)` so the batch id is on the idempotency claim for the whole run; attaching at `complete()` (the previous behaviour) recorded it exactly when nobody needed it any more, because a fan-out's whole window — the one in which a concurrent duplicate arrives, and in which this call's own gateway gives up — is while it runs. `test_the_started_hook_fires_before_any_dispatch_and_cannot_fail_the_batch` pins the ordering.

#### In-flight duplicate 409 -- `src/backend/routers/fan_out.py:85-100`
A replayed `Idempotency-Key` whose original is still running now raises `409` with the same `{"error": "request_in_progress", "message": ..., "execution_id": <fan_out_id>}` shape `/chat` and `/task` use. It was a bare string, so the #2661 MCP client — which reads `detail.execution_id` off a 409 — could never turn a fan-out 409 into a receipt. The `execution_id` field carries the **batch** id, because that is what the GET resolves.

### FanOutService
- `src/backend/services/fan_out_service.py:67` -- `FanOutService` class
- Singleton via `get_fan_out_service()` (module-level `_fan_out_service`)

#### `execute()` method (#2524 shape)
1. Generate `fan_out_id` = `fo_{secrets.token_urlsafe(12)}`, then fire `on_started(fan_out_id)` if supplied (#2670 — best-effort, see above; nothing has been dispatched yet)
2. **Spawn** `_dispatch_all` — the batch is not owned by the request. It snapshots the agent's subscription once (SUB-004), then for each task: acquire `asyncio.Semaphore(max_concurrency)` → `db.create_task_execution(..., fan_out_id, fan_out_task_id=task.id, subscription_id)` → `task_service.execute_task()` with `triggered_by="fan_out"`, that `execution_id`, and **`timeout_seconds=None`** so TaskExecutionService resolves the agent's `execution_timeout_seconds` (TIMEOUT-001, #418). A returned `error_code` is kept in-process (the row has no column for it).
3. `async_mode=True` → return `{fan_out_id, status="accepted", total}` now.
4. Otherwise, within one budget (`_wait_budget`): wait for the dispatch task (shielded — the deadline never cancels it), **then** `sync_waiter.wait_for_fan_out_batch(...)` for rows that came back `queued`. On timeout the batch status is `deadline_exceeded`.
5. `build_aggregate(agent_name, fan_out_id, order=[t.id ...])` — read from the rows, in the caller's input order. A task with no row is `running` while the dispatch is still going (waiting for a slot), `failed` once it has finished (row creation failed).

#### Row lifecycle: created at slot grant, never up front (#2524 review)
A row created up front and left waiting behind the semaphore is a hidden queue every recovery path misreads. As `RUNNING` (what `create_task_execution` writes) it matches the #106 no-session sweep (`NO_SESSION_TIMEOUT_SECONDS = 60`), the watchdog's orphan reconcile (absent from the agent, no live-dispatcher marker) and the stale sweep — all anchored at `started_at`, i.e. admission (#2433/#2435) — so the undispatched tail was bulk-FAILed, the join woke the sync caller with a fabricated aggregate, and the turns still ran and billed. As `QUEUED` it is claimable by `claim_next_queued` (backlog drain, pull workers) while `_dispatch_all` also dispatches it — a double run. Creating the row at grant (as pre-#2524 code did via `execute_task`) avoids both and keeps `duration_ms` a measure of the run, not of the wait.

The cost: a batch is fully visible only once its last subtask has a slot. A GET mid-batch sees the rows dispatched so far (the #2670 receipt already treats `execution_ids` as evidence, not a manifest), and a backend restart loses the not-yet-dispatched tail — `_dispatch_all` is in-process with no startup recovery, for `async_mode` exactly as for a sync call. Under pull the semaphore self-releases in milliseconds, so every row exists almost immediately.

**`max_concurrency` keeps its meaning, and needed no branch.** The semaphore still wraps the `execute_task` call. On push that call spans the whole turn, so it paces dispatch exactly as before — deleting it would fire N concurrent dispatches at an agent whose `max_parallel_tasks` is 3 and turn the excess into `CapacityFull` failures. Under pull the same call returns in milliseconds (the row is queued, not run), so the semaphore self-releases and real concurrency becomes the agent's worker pool — #1081 Phase 5's "capacity becomes physical", arrived at by construction.

⚠️ **The outer deadline bounds the WAIT, not the work (#2524, contract change).** It used to wrap the `gather` in `asyncio.timeout`, cancelling in-flight subtasks and reporting them `failed`/`timeout`. A queued or claimed row is not the backend's to cancel, and on push that cancellation was always half-illusory — it abandoned the HTTP call while the agent kept running (and billing for) the turn. A still-open subtask — including one still waiting for a slot — now reports **`status="running"`** with no `error_code`; the batch still reports `deadline_exceeded`. A caller that branches on the batch status is unaffected; one that treats every non-`completed` subtask as failed sees a third value. **After a deadline the GET is the source of truth**, not the returned aggregate.

**Default wait budget.** An explicit `timeout_seconds` is honoured verbatim. Without one the wait is `ceil(N / min(max_concurrency, max_parallel_tasks)) × execution_timeout_seconds + 120s` — the worst case for a batch of individually-bounded subtasks at that parallelism (previously an unbounded `gather`), so a batch that used to return complete results still does.

**`error_code`.** Populated on the sync response from `execute_task`'s return value (push path). `schedule_executions` has no column for it, so a pull-claimed subtask and every GET result carry none.

#### The join
- `join_fan_out_on_terminal(execution_id)` — called from `event_dispatch_service.spawn_task_terminal_event`, the wrapper every CAS-won terminal writer already goes through (push applier, pull sink, lease reaper, cleanup), beside #2523's loop advance. Deliberately NOT inside `emit_task_terminal_event`, which returns early when no event subscription matches — the common case.
- One PK read on every terminal in the fleet to ask "does this row carry a `fan_out_id`?"; a batch COUNT (`idx_executions_fan_out_status`) only when it does.
- It fires on "no open row", which is not "batch complete" while later subtasks have no row yet. Safe because the sync caller registers its waiter only after the dispatch has finished; an earlier signal finds no waiter and is a no-op.
- Idempotent by construction: it only *signals*, and signalling an absent or already-resolved waiter is a no-op.
- `_dispatch_all` also calls it directly on a non-QUEUED return, because `execute_task`'s fast-fail paths (capacity, circuit-open, ephemeral budget) write a FAILED row without reaching a CAS-won terminal writer, so no terminal event fires for them.
- `_fail_subtask` (a raised `execute_task`) is a terminal writer: on a won CAS it closes the dispatch activity (#1804) and calls `spawn_task_terminal_event` (#1578 emit + join).

Log line format: `[FanOut] Starting {fan_out_id}: {N} tasks on '{agent}' (concurrency={max_concurrency}, deadline={deadline_desc}, async={async_mode})` where `deadline_desc` is either `"{N}s"` or `"per-batch"`.

### Data Models
```python
@dataclass
class FanOutTaskInput:
    id: str
    message: str

@dataclass
class FanOutTaskResult:
    id: str
    status: str           # "completed" | "failed" | "running" (open at a deadline)
    response: Optional[str]
    error: Optional[str]
    error_code: Optional[str]
    execution_id: Optional[str]
    cost: Optional[float]
    context_used: Optional[int]
    duration_ms: Optional[int]

@dataclass
class FanOutResult:
    fan_out_id: str
    status: str           # "completed" | "deadline_exceeded" | "accepted"
    total: int
    completed: int
    failed: int
    results: List[FanOutTaskResult]
```

#### Batch read-back models (#2670) -- `src/backend/models.py:2928-2958`
Deliberately **not** a reuse of `FanOutResponse`: the aggregate has a state the dispatch response cannot have (`running`) and lacks one it does have (`deadline_exceeded`). Two different questions, two shapes.
```python
class FanOutBatchTask(BaseModel):
    execution_id: str
    task_id: Optional[str]         # the caller's FanOutTask.id (row column fan_out_task_id, #2524); NULL on older rows
    status: str                    # execution status verbatim: queued|running|pending_retry|success|failed|cancelled|skipped|...
    message: Optional[str]         # the dispatched message — the only tie back on rows that predate task_id
    response: Optional[str]
    error: Optional[str]
    cost: Optional[float]
    context_used: Optional[int]
    duration_ms: Optional[int]
    model_used: Optional[str]
    started_at: Optional[str]      # ISO-Z (normalised via _norm_ts, #1474)
    completed_at: Optional[str]

class FanOutBatchStatus(BaseModel):
    agent_name: str
    fan_out_id: str
    status: str                    # "running" | "completed" | "partial" | "failed"
    total: int
    completed: int                 # rows with status == "success"
    failed: int                    # terminal rows that are not "success"
    running: int                   # rows in {queued, running, pending_retry}
    results: List[FanOutBatchTask] # started_at ASC — not a contract; match by task_id
```
An empty batch is unreachable here — the route 404s rather than reporting a batch of zero.

## Data Layer

### Database Migration
- `src/backend/db/migrations.py:1152` -- `_migrate_execution_fan_out_id()`
- Migration #33 (`execution_fan_out_id`) in the `MIGRATIONS` list
- Adds `fan_out_id TEXT` column to `schedule_executions` table
- Creates index: `idx_executions_fan_out ON schedule_executions(fan_out_id)` — the index the #2670 batch read walks
- **#2524, dual-track (Invariant #9)**: `_migrate_execution_fan_out_task_id()` in `db/migrations.py` **and** Alembic `0062_execution_fan_out_task_id` (chained after `0061_execution_open_canvas`). Adds `fan_out_task_id TEXT` plus a composite `idx_executions_fan_out_status ON schedule_executions(fan_out_id, status)` — the join COUNTs non-terminal rows for one batch on every fan-out terminal, which the single-column index cannot serve without reading the whole batch.

### Model
- `src/backend/db_models.py:266` -- `fan_out_id: Optional[str]` on `ScheduleExecution` dataclass

### Execution Record Creation
The former monolithic `db/schedules.py` was split into the `db/schedules/` mixin package (#1481); execution rows live in `ScheduleExecutionsMixin`.
- `src/backend/db/schedules/executions.py:108` -- `create_task_execution()` accepts `fan_out_id` parameter
- `src/backend/db/schedules/executions.py:170` -- INSERT includes `fan_out_id` column
- `src/backend/db/schedules/executions.py:68` -- row mapper (`_row_to_schedule_execution`, line 36) reads `fan_out_id` from result set
- `src/backend/db/schedules/executions.py:667` -- `get_agent_executions_summary()` projects `fan_out_id` onto list rows (`ExecutionSummary.fan_out_id`) — the field the MCP recovery lookup groups on

### Batch Read (#2670)
- `count_fan_out_open(fan_out_id)` (#2524) — COUNT of `{queued, running, pending_retry}` rows; the join and `wait_for_fan_out_batch` run it
- `src/backend/db/schedules/executions.py` -- `get_fan_out_executions(agent_name, fan_out_id, limit=200) -> List[dict]`
- `src/backend/database.py:1884` -- facade method delegating to `self._schedule_ops.get_fan_out_executions(...)`
- Projects only what the aggregate needs: `id, fan_out_task_id, status, started_at, completed_at, duration_ms, message, response, error, cost, context_used, model_used`. Also the source of `FanOutService`'s sync aggregate (#2524)
- Scoped by **both** `agent_name` and `fan_out_id`: the id is server-minted and unguessable, but the route that exposes this is agent-gated, so the query must not be able to return another agent's rows even if an id were somehow reused (`test_the_read_is_scoped_by_agent_as_well_as_by_batch_id`)
- Ordered `started_at ASC`. Not a contract: a row claimed by a pull worker or re-queued has `started_at` re-stamped, so callers match by `task_id`
- `limit=200` is a belt: `MAX_TASKS` bounds a batch at 50 at creation, but a re-queued subtask can add a row, so it is not exact
- `started_at`/`completed_at` normalised through `_norm_ts` so this surface can never serialise a naive timestamp (#1474)

### TaskExecutionService Integration
- `src/backend/services/task_execution_service.py:1001` -- `triggered_by` accepts `"fan_out"` (trigger type)
- `src/backend/services/task_execution_service.py:1014` -- `fan_out_id` parameter, passed through to `db.create_task_execution()` at line 1109
- Each subtask gets its own execution record, capacity slot, and activity tracking via the standard path

## Side Effects
- **Execution Records**: Each subtask creates a `schedule_executions` row with `triggered_by="fan_out"` and shared `fan_out_id`
- **Capacity Slots**: Each subtask acquires/releases a parallel execution slot via `SlotService`
- **Activity Tracking**: Standard activity tracking from `TaskExecutionService` applies per subtask
- **WebSocket**: Standard execution status broadcasts from `TaskExecutionService` apply per subtask
- **No dedicated fan-out WebSocket event**: The fan-out itself does not broadcast; individual subtask events flow through existing channels
- **Idempotency claim** (`idempotency_keys`, RELIABILITY-006): when an `Idempotency-Key` is sent, the batch id is attached to the claim at mint time (`attach_execution`, #2670) and the full `FanOutResponse` is stored as the replay snapshot at `complete()`; `fail()` releases the claim if `execute()` raises

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| No tasks provided | 422 | "At least one task is required" |
| Too many tasks (>50) | 422 | "Maximum 50 tasks per fan-out" |
| Duplicate task IDs | 422 | "Duplicate task IDs: {dupes}" |
| Invalid task ID format | 422 | "Task ID must be 1-64 alphanumeric..." |
| Concurrency out of range | 422 | "max_concurrency must be between 1 and 10" |
| Timeout out of range | 422 | "timeout_seconds must be between 10 and 3600" (only validated when field is set; `None`/omitted is accepted) |
| Unsupported policy | 422 | "Only 'best-effort' policy is supported" |
| Cross-agent target | 400 | "Fan-out target must be 'self' or '{name}'" |
| Agent not found | 404 | From `get_authorized_agent` dependency |
| Auth failure | 401 | From `get_current_user` dependency |
| Wait deadline exceeded | 200 | `status: "deadline_exceeded"`; still-open subtasks (including ones not yet dispatched) report `status: "running"` and keep running — poll the GET (#2524) |
| Per-subtask timeout (per-agent config) | 200 | Per-task `status: "failed"` with `error_code: "timeout"` from TaskExecutionService; other subtasks continue |
| Individual subtask failure | 200 | Per-task `status: "failed"` with `error` and `error_code` |
| In-flight duplicate `Idempotency-Key` (POST) | 409 | `{"error": "request_in_progress", "message": "...", "execution_id": "<fan_out_id>"}` — same shape as `/chat`/`/task`; the id is the batch id (#2670) |
| Batch not found (GET) — malformed id, unknown id, or another agent's id | 404 | "Fan-out not found" (one uniform answer, Invariant #8; a malformed id never reaches the DB) |
| MCP `fan_out` aborted by `MCP_CHAT_TIMEOUT_MS` | tool result | `{status: "fan_out_timeout", agent, fan_out_id, execution_ids, task_count, message}` when exactly one batch is identifiable; otherwise an error pointing at `list_recent_executions` (#2670) |

## Request/Response Example

### Request
```json
POST /api/agents/my-agent/fan-out
{
  "tasks": [
    {"id": "task-1", "message": "Analyze Q1 revenue"},
    {"id": "task-2", "message": "Analyze Q2 revenue"},
    {"id": "task-3", "message": "Analyze Q3 revenue"}
  ],
  "max_concurrency": 3,
  "timeout_seconds": 300,
  "model": "sonnet"
}
```

### Response
```json
{
  "fan_out_id": "fo_abc123def456",
  "status": "completed",
  "total": 3,
  "completed": 3,
  "failed": 0,
  "results": [
    {
      "id": "task-1",
      "status": "completed",
      "response": "Q1 revenue was...",
      "execution_id": "exec_xyz",
      "cost": 0.05,
      "context_used": 12000,
      "duration_ms": 8500
    },
    ...
  ]
}
```

### Poll a batch (#2670)
```
GET /api/agents/my-agent/fan-out/fo_abc123def456
```
Mid-run (null optional fields elided):
```json
{
  "agent_name": "my-agent",
  "fan_out_id": "fo_abc123def456",
  "status": "running",
  "total": 3,
  "completed": 1,
  "failed": 0,
  "running": 2,
  "results": [
    {"execution_id": "exec_xyz", "status": "success", "message": "Analyze Q1 revenue",
     "response": "Q1 revenue was...", "cost": 0.05, "context_used": 12000, "duration_ms": 8500,
     "started_at": "2026-09-13T10:00:00Z", "completed_at": "2026-09-13T10:00:08Z"},
    {"execution_id": "exec_abc", "status": "running", "message": "Analyze Q2 revenue",
     "started_at": "2026-09-13T10:00:00Z"},
    {"execution_id": "exec_def", "status": "queued",  "message": "Analyze Q3 revenue",
     "started_at": "2026-09-13T10:00:01Z"}
  ]
}
```
Once every row is terminal, `status` becomes `completed` (all `success`), `partial` (some), or `failed` (none). Match results to the caller's `tasks[].id` by `results[].task_id` (#2524).

## Testing

### Prerequisites
- Backend running at `http://localhost:8000`
- At least one running agent

### Test Steps
1. **Action**: Send fan-out request with 3 tasks
   **Expected**: All 3 tasks complete, `status: "completed"`
   **Verify**: `GET /api/agents/{name}/executions` shows 3 records with same `fan_out_id`

2. **Action**: Send fan-out with `max_concurrency: 1`
   **Expected**: Tasks execute sequentially (only 1 at a time)
   **Verify**: Execution timestamps show sequential pattern

3. **Action**: Send fan-out with very short `timeout_seconds: 10` and complex tasks
   **Expected**: `status: "deadline_exceeded"`, unfinished tasks report `status: "running"`
   **Verify**: the GET later shows them terminal on their rows

4. **Action**: Send fan-out with `agent: "other-agent"`
   **Expected**: 400 error "Cross-agent fan-out is not yet supported"

5. **Action**: Send fan-out with duplicate task IDs
   **Expected**: 422 validation error

6. **Action**: Send a fan-out, then `GET /api/agents/{name}/fan-out/{fan_out_id}` while it runs (or call `get_fan_out_result` via MCP) (#2670)
   **Expected**: `status: "running"` with `running > 0` while any row is `queued`/`running`/`pending_retry`; after every row is terminal, `completed` / `partial` / `failed`; per-task `status` is the execution status verbatim
   **Verify**: `total` equals the task count; a made-up id (`fo_nope`), a malformed one (`not-an-id`), and another agent's id all return `404 "Fan-out not found"`

7. **Action** (MCP): call `fan_out` with tasks that outlive `MCP_CHAT_TIMEOUT_MS` (#2670)
   **Expected**: a `{status: "fan_out_timeout", fan_out_id, execution_ids, task_count, message}` receipt instead of `fetch failed`; the batch keeps running
   **Verify**: `get_fan_out_result(agent_name, fan_out_id)` resolves the same batch; `npx tsx src/mcp-server/scripts/verify_914.ts <agent> fanout` drives this end-to-end

### Automated
- `tests/unit/test_2524_fanout_async_join.py` — the join (wakes only on the last open row, idempotent, cheap on non-fan-out terminals, reached from `spawn_task_terminal_event` with independent guards), rows created at slot grant (never up front, never swept while waiting), the queued path leaving the batch open, `max_concurrency` pacing push, a raised dispatch closing its row as a proper terminal writer, the batch-sized wait budget, `error_code` carried from push results, the sync edge adapter
- `tests/unit/test_2524_fanout_real_schema.py` — `fan_out_task_id`, `get_fan_out_executions` and `count_fan_out_open` against the real schema
- `tests/unit/test_2670_fan_out_receipt.py` — the aggregate rule (`running` outranks verdicts, `pending_retry` is live, `cancelled`/`skipped` are failures, `deadline_exceeded` never reported, per-task status verbatim, id-less rows dropped, the fold is pure), the read surface (malformed id is the same 404 as unknown and never reaches the DB, empty batch is 404 not an empty aggregate, query scoped by agent, `get_authorized_agent` gate, rows-not-snapshot), and the two backend contracts the MCP client depends on (409 shape, batch id attached before dispatch, hook cannot fail the batch)
- `src/mcp-server/src/client.test.ts` — `pickRecentFanOut` (`describe("#2670 pickRecentFanOut")`): N rows of one batch is the expected shape, two distinct `fan_out_id`s is ambiguity → `undefined`, filters on trigger / `fan_out_id` presence / key / message / window, and the per-call-site trigger sets stay disjoint (`FAN_OUT_RECOVERY_TRIGGERS` is `["fan_out"]` and neither the chat nor the task set includes it)

## Architecture Notes
- Concurrency is managed by `asyncio.Semaphore` -- safe because asyncio is single-threaded (no preemption between awaits)
- `asyncio.gather(return_exceptions=True)` in the spawned `_dispatch_all` ensures all subtasks are dispatched even if one raises; the spawned task is strongly referenced (`_inflight_batches`) so it cannot be collected mid-batch
- The deadline never cancels dispatch: the sync caller waits on `asyncio.shield(dispatch)`
- `sync_waiter`'s fan-out registry is in-process: multi-worker, a terminal on another worker is found by the 5s DB poll, which bounds latency there
- The batch's execution rows are its **only durable record** (#2670): the POST aggregate is assembled from them when returned, and the idempotency snapshot exists only after the batch finishes. Anything that needs to observe a batch after the fact — the GET, the MCP receipt recovery — resolves through `fan_out_id` on `schedule_executions`, never through a fan-out table (there is none)
- v1 is self-only (agent fans out to itself); cross-agent fan-out is a future extension

## Related Flows
- [task-execution-service.md](task-execution-service.md) -- Each subtask uses the standard execution path
- [parallel-capacity.md](parallel-capacity.md) -- Subtasks consume parallel execution slots
- [parallel-headless-execution.md](parallel-headless-execution.md) -- Similar stateless execution model
- [mcp-orchestration.md](mcp-orchestration.md) -- MCP tool registration; the gateway-timeout receipt design for all three sync routes, including "Route three — `fan_out` (#2670)" (why the receipt names a `fan_out_id`, why the batch is the unit of ambiguity, why status is not filtered)
- [AUDIT-001-execution-origin-tracking.md](AUDIT-001-execution-origin-tracking.md) -- Origin tracking headers
