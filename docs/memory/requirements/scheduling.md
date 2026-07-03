# Requirements — Scheduling, Autonomy, Pipelines & Loops

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 10. Scheduling & Autonomy

### 10.1 Agent Scheduling
- **Status**: ✅ Implemented (2025-11-28)
- **Description**: Cron-based automation with APScheduler
- **Key Features**: Schedule CRUD, timezone support, execution history, manual trigger
- **Flow**: `docs/memory/feature-flows/scheduling.md`

### 10.2 Autonomy Mode
- **Status**: ✅ Implemented (2026-01-01)
- **Description**: Master toggle for agent autonomous operation
- **Key Features**: Dashboard toggle, enables/disables all schedules
- **Flow**: `docs/memory/feature-flows/autonomy-mode.md`

### 10.3 Execution Queue
- **Status**: ✅ Implemented
- **Description**: Redis-based queue preventing parallel execution conflicts
- **Flow**: `docs/memory/feature-flows/execution-queue.md`

### 10.4 Execution Termination
- **Status**: ✅ Implemented (2026-01-12)
- **Description**: Stop running executions via process registry
- **Key Features**: SIGINT/SIGKILL flow, queue release, activity tracking
- **Flow**: `docs/memory/feature-flows/execution-termination.md`

### 10.4.1 Signal-Exit Classification Correctness (#904)
- **Status**: ✅ Implemented (2026-05-21)
- **GitHub Issue**: #904
- **Description**: When a Claude subprocess inside an agent container is killed by an external signal (SIGKILL from cgroup OOM, schedule timeout, operator cancel), the error path must classify it as a signal kill — not as a subscription auth failure. Previously, the chat (`/api/chat`) path on the agent server lacked the `_classify_signal_exit` call that the headless path had (added by #516), so the same OOM kill produced different error strings depending on which entry point dispatched the work. The fallback heuristic in `headless_executor.py` also worded the zero-tokens 503 detail as "(possible authentication issue)", which downstream substring matchers in `services/subscription_auto_switch.py` and `src/scheduler/service.py` treated as a real auth signal — firing a futile SUB-003 auto-switch on every cgroup OOM and burning the 2h skip-list slot for the alternative subscription.
- **Key Features**:
  - Chat path (`docker/base-image/agent_server/services/claude_code.py`) now calls `_classify_signal_exit(return_code, metadata)` before the generic `if return_code != 0` block — same contract as the headless path. SIGKILL/SIGTERM/SIGINT exits raise 504 with the explicit "Execution terminated by SIGKILL after N tool calls / M turns" detail.
  - `headless_executor.py` zero-tokens fallback (the `return_code > 0 and input_tokens == 0 and output_tokens == 0` branch) no longer says "authentication issue" — the new detail is `"Execution failed with no output (exit code N): {stderr}"`. The dedicated "Authentication failure" 503 raised on a confirmed `is_auth_failure_message` match a few lines above remains the only path that surfaces the auth phrasing.
  - `_diagnose_exit_failure` (line 155, `error_classifier.py`) no longer returns the bare "Subscription token may be expired or revoked. Generate a new one with 'claude setup-token'." string for the OAuth-without-API-key case. The new wording is "Process failed with exit code N and no diagnostic output. Common causes: OOM kill (raise agent memory), schedule timeout (extend timeout_seconds), expired subscription token (`claude setup-token`)." — it lists token expiry as one of several possibilities instead of declaring it the diagnosis.
- **SUB-003 interaction**: see §20.4 — `is_auth_failure` now skips messages containing signal/OOM/timeout markers so even if a residual wording carries an indicator, an unambiguous SIGKILL won't trigger auto-switch.
- **Files**:
  - `docker/base-image/agent_server/services/claude_code.py` — wire `_classify_signal_exit`
  - `docker/base-image/agent_server/services/headless_executor.py` — reword zero-tokens 503
  - `docker/base-image/agent_server/services/error_classifier.py` — reword `_diagnose_exit_failure` OAuth-only branch
  - `src/backend/services/subscription_auto_switch.py` — negative markers in `is_auth_failure`
  - `src/scheduler/service.py` — same negative markers in `_is_auth_failure`

### 10.4.2 Backend Agent-Call Semaphore (#904 RC-1)
- **Status**: ✅ Implemented (2026-05-21)
- **GitHub Issue**: #904 (RC-1 surface)
- **Description**: Backpressure on outbound agent HTTP calls from
  `task_execution_service.agent_post_with_retry`. Before this, one
  misbehaving agent whose `/api/chat` or `/api/task` held for several
  minutes could leave N parallel coroutines `await`ing on `httpx.post`
  while each periodically issued **synchronous** `sqlite3` calls
  (`db/connection.py`). Under enough contention the synchronous
  writes stalled the event loop long enough that the Docker
  healthcheck (10s) flipped the backend to `unhealthy` and the
  dashboard's parallel API fan-out (`/api/agents`,
  `/api/ops/fleet/health`, `/api/operator-queue?status=pending`,
  `/api/agents/execution-stats`) appeared frozen to the operator.
  Restarting the offending agent container was the only workaround.
- **Key Features**:
  - **Per-agent semaphore** sized to the agent's
    `max_parallel_tasks` (default 3, set via
    `db.get_max_parallel_tasks`). Lazily created the first time a
    call to that agent reaches the wrapper. Limits how many backend
    coroutines can be mid-call to a single agent at once — a
    misbehaving agent can never dominate the backend's available
    coroutines beyond its own ceiling.
  - **Global semaphore** capped at
    `BACKEND_AGENT_CALL_LIMIT` env var (default 8). Bounds the total
    fan-out across all agents. With a default of 8, the backend
    always has spare async capacity for dashboard / health requests
    even when every agent is mid-call.
  - **Backward-compatible queue wait**: acquires use
    `asyncio.wait_for(..., timeout=BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S)`
    with a **default of 3600s** — matches the platform's max
    `execution_timeout_seconds` (TIMEOUT-001, default 3600s, #665).
    Pre-#904 the worst-case wall-clock per call was the agent
    timeout (~610s default); 3600s leaves a generous margin so any
    call that would have eventually succeeded still does. The cap
    is NOT a "fail short-tail calls fast" knob — it's a deadlock
    safety valve (see below). Past the timeout the wrapper raises
    `BackendAgentCallBudgetExhausted`, translated to HTTP 503 in
    `execute_task` / `routers/chat.py`. Set
    `BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S=0` to disable entirely (opt-in
    — accepts deadlock risk for zero false 503s).
  - **Deadlock safety valve**: agent-to-agent chains
    (`chat_with_agent` MCP tool, X→Y→Z collaborations) can
    deadlock when concurrent chain depth exceeds the global
    semaphore. Each chain holds a slot for its outer caller while
    waiting on the next hop, which itself wants a slot. With
    `cap=8` and >8 simultaneous deep chains the system would hang
    forever without a timeout. The 3600s ceiling surfaces such a
    deadlock as a 503 within an hour, lets the queue drain, and
    keeps the system unstuck.
  - **Fail-closed-but-fair**: when the per-agent or global cap is
    saturated, the caller waits the configured timeout, then 503s.
    The agent's task-execution slot
    (`CapacityManager.admit`) is released on the 503 path so the
    same `execution_id` can be retried.
  - **Configurable** via env vars (no DB schema change):
    - `BACKEND_AGENT_CALL_LIMIT` (int, default 8) — global cap
    - `BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S` (float, default 3600) —
      acquire timeout; 0 = wait forever
- **Observability**:
  - `[TaskExecService] Acquired agent-call slot for {agent} (agent_inflight=N/M, global_inflight=K/L)`
    on every successful acquire (debug level for hot path).
  - `[TaskExecService] Backend call budget exhausted for {agent}
    after {wait_ms}ms (agent_cap={N}, global_cap={M})` warning on
    timeout — surfaces in Vector platform.json.
- **Out of scope (separate follow-ups)**:
  - **Sync→async DB**: the sqlite3 calls that stall the event loop
    remain synchronous. The semaphore mitigates the contention by
    bounding fan-out, but a true fix needs
    `run_in_executor`-wrapped DB calls. Larger refactor; tracked
    separately.
  - **RC-4 cgroup OOM observability**: not addressed in this PR.
- **Files**:
  - `src/backend/services/task_execution_service.py` — semaphore
    primitives + `agent_post_with_retry` integration
  - `src/backend/services/agent_call_limiter.py` — extracted
    primitives (kept slim — module-level singletons +
    `BackendAgentCallBudgetExhausted` exception)
  - `src/backend/config.py` — env-var read of the two new knobs
  - `docker-compose.yml` — env-var pass-through for backend service

### 10.5 Model Selection for Tasks & Schedules (MODEL-001)
- **Status**: ✅ Implemented (2026-03-02)
- **Description**: Select which Claude model to use for task execution and scheduled runs
- **Key Features**: ModelSelector combobox with presets (Opus 4.5/4.6, Sonnet 4.5/4.6, Haiku 4.5), custom model input, localStorage persistence, model_used audit trail in execution records
- **Requirements**: `docs/requirements/MODEL_SELECTION_TASKS_SCHEDULES.md`

### 10.6 Scheduler Async Fire-and-Forget (SCHED-ASYNC-001)
- **Status**: ✅ Implemented (2026-03-11)
- **Requirement ID**: SCHED-ASYNC-001
- **GitHub Issue**: #101
- **Description**: Replace blocking HTTP call from scheduler to backend with async fire-and-forget dispatch + DB polling to prevent TCP connection drops during long-running tasks
- **Key Features**:
  - Backend accepts `async_mode=True` on `/api/internal/execute-task`, spawns background task, returns immediately
  - Scheduler POSTs with 30s timeout, then polls DB every `poll_interval` seconds until execution completes
  - Status overwrite guard: scheduler checks current DB status before marking exceptions as `failed`
  - Backward compatible: old backends without async_mode support work as sync fallback
  - Configurable `POLL_INTERVAL` env var (default 10s)
- **Root Cause**: TCP connection drops after 15-30 min on long-running scheduled tasks, causing false `failed` status even though agent work completed successfully

### 10.6.1 Conditional Schedule Pre-Check (SCHED-COND-001)
- **Status**: ✅ Implemented (2026-04-22)
- **Requirement ID**: SCHED-COND-001
- **GitHub Issue**: #454
- **Description**: Optional template-supplied hook that lets a scheduled cron tick be skipped deterministically — the scheduler calls a new internal backend endpoint which `docker exec`s the executable `~/.trinity/pre-check` file inside the target agent container; non-empty stdout becomes the chat prompt, empty stdout + exit 0 records a skipped execution. The hook is language-agnostic (interpreter chosen by shebang). Eliminates Claude token cost on empty polls for poll-driven agents (PR reviewers, inbox monitors, alert routers, RSS watchers).
- **Key Features**:
  - Contract: agent templates drop an executable `~/.trinity/pre-check` file with a shebang (`#!/usr/bin/env python3`, `#!/bin/bash`, …). Trinity execs it directly — no `python3` prefix, no language assumption. Stdout is the chat prompt; empty stdout + exit 0 = skip; non-zero exit = fail-open.
  - Backend endpoint: `POST /api/internal/agents/{name}/pre-check` (X-Internal-Secret gated) runs the script via `execute_command_in_container` — the same primitive used by `git_service.py` (persistent-state allowlist, #384 S3), `ssh_service.py`, `agent_service/terminal.py`, `adapters/message_router.py`, `routers/system_agent.py`, `routers/voice.py`.
  - Fail-open: script absent, non-zero exit, timeout, backend 5xx / malformed response → scheduler fires as usual. A broken pre-check never silently suppresses scheduled work.
  - Message override: non-empty stdout replaces `schedule.message` for that one invocation — lets the agent inject real work items (e.g. the PR list) into the chat prompt.
  - Skip record: empty stdout writes a row to `schedule_executions` with `status='skipped'`, reason, and zero cost — visible in the Trinity UI alongside successful runs.
  - Manual triggers bypass pre-check entirely (explicit operator intent always fires).
  - Zero DB schema change (reuses existing `ExecutionStatus.SKIPPED` + `create_skipped_execution`).
  - **No new HTTP edge**: scheduler calls backend, backend `docker exec`s into agent. Topology stays "scheduler → backend → agent" (Invariant #11).
- **Test plan**: 13 unit tests covering backend-response translation (hook absent / non-zero exit / empty stdout / fire-with-message / 404 / 5xx / connection error / malformed JSON) + scheduler branch behaviors (skip, override, fail-open, manual-bypass). Full 162-test scheduler suite passes.
- **Root Cause**: No platform primitive for "deterministic gate before LLM invocation." Previously required per-template daemons backgrounded inside agent containers — invisible to Trinity UI, reimplemented per template, no skip metrics.

### 10.7 Per-Agent Execution Timeout (TIMEOUT-001)
- **Status**: ✅ Implemented (2026-03-12)
- **Requirement ID**: TIMEOUT-001
- **GitHub Issue**: #99
- **Description**: Configurable execution timeout per agent, consistent across all trigger methods
- **Key Features**:
  - `execution_timeout_seconds` column in `agent_ownership` (default 900s = 15 min)
  - All execution paths (task API, chat, scheduler, MCP, paid endpoints) use agent's timeout
  - Per-execution override still supported when explicitly provided
  - Slot TTL dynamically calculated as agent timeout + 5 min buffer
  - API: `GET/PUT /api/agents/{name}/timeout`
  - Validation: 60-7200s (1 min to 2 hours)
- **Flow**: `docs/memory/feature-flows/parallel-capacity.md` (updated), `docs/memory/feature-flows/task-execution-service.md` (updated)
- **Headless per-tool stall watchdog (#1369, 2026-06-29)**: the headless executor's
  per-tool stall watchdog (`headless_executor.py`) — which kills an execution when a
  `tool_use` has no `tool_result` for too long (introduced #970/#973 to bound a hung
  stdio MCP `tools/call`; reason-labeled `stall_no_output` by #1094) — is now (a)
  **scoped to `mcp__*` tools only** so legitimately long built-ins (`Bash`/`Read`/`Task`
  sub-agents, already bounded by `execution_timeout_seconds`) are never stall-killed, and
  (b) **operator-configurable** via `AGENT_TOOL_STALL_LIMIT_S` (default **1800s**, raised
  from the old flat 300s; `0`/negative = disabled, relying on the execution-timeout
  backstop). The value is read per-run inside the agent container and threaded from the
  backend env at create/recreate (`crud.py`/`lifecycle.py`) — creation-time like
  `AGENT_TMP_SIZE`, so existing agents pick up a change on **recreate**, not a plain
  restart. Tradeoff: a genuinely hung built-in (or a hang *inside* a `Task` sub-agent,
  which surfaces as an open `Task` in the parent log) now falls to the execution-timeout
  budget instead of the 300s kill. Superseded long-term by the pull/work-stealing model
  (#1081/#1083), which retires the watchdog entirely.

### 10.8 Persistent Task Backlog (BACKLOG-001)
- **Status**: ✅ Implemented (2026-04-13); internalized behind `CapacityManager` (#428, 2026-04-26)
- **Requirement ID**: BACKLOG-001
- **GitHub Issue**: #260, internalized by #428
- **Description**: Async `/task` requests that arrive at full parallel capacity spill into a durable SQLite-backed FIFO backlog instead of returning HTTP 429. Reached via the unified `CapacityManager.acquire(..., overflow_policy="queue_persistent", overflow_payload=...)` facade; queued items drain automatically when slots free via the manager's release-callback wiring; 60s `CapacityManager.run_maintenance()` tick expires stale rows and drains orphans after restart.
- **Key Features**:
  - New `QUEUED` value on `TaskExecutionStatus`; reuses `schedule_executions` with `queued_at` + `backlog_metadata` columns
  - Partial index `idx_executions_queued` for cheap O(log n) FIFO claim via atomic `UPDATE ... RETURNING`
  - Per-agent `max_backlog_depth` setting (default 50, validated 1-200)
  - True HTTP 429 only when the backlog is also full
  - Terminate-while-queued short-circuit (no container interaction)
  - Agent delete cascades to cancel queued rows
  - Frontend: amber `queued` badge in Tasks tab and Execution Detail
  - Identity captured at enqueue and replayed at drain (no re-auth, matches scheduler pattern)
- **Flow**: `docs/memory/feature-flows/persistent-task-backlog.md`

### 10.8.1 Fleet-Wide Parallel-Capacity Ceiling (#506)
- **Status**: ✅ Implemented (2026-06-29)
- **GitHub Issue**: #506
- **Description**: Two-tier model for per-agent `max_parallel_tasks` (CAPACITY-001). An
  **admin** sets a fleet-wide **ceiling** (`max_parallel_tasks_ceiling`, default 10, range
  1–32); **owners** pick a per-agent value within that ceiling. Closes the gap where the
  per-agent write validated against a hardcoded `10`, letting one owner saturate the host,
  and the value was not deployment-tunable (small VPS vs large box). The ceiling is stored in
  the generic `system_settings` key/value store — **no DB migration** (no `migrations.py` /
  Alembic entry).
- **Key Features**:
  - Admin endpoints `GET/PUT /api/settings/max-parallel-tasks-ceiling` (range-validated 1–32,
    admin-only, audit-logged). The generic catch-all `PUT /{key}` is **blocked** for this key
    (422 → dedicated route) so the range can't be bypassed.
  - Per-agent `PUT /api/agents/{name}/capacity` validates against the live ceiling, not a
    constant. The GET response returns `max_parallel_tasks` (stored), `ceiling`, and
    `effective_max_parallel_tasks` = `min(stored, ceiling)`; `available_slots` is computed from
    the effective value.
  - **Runtime clamp (clamp-on-use, never rewrite stored)**: the `CapacityManager` facade
    (`acquire` / `get_slot_state` / `get_all_states`) clamps the cap to the ceiling — covering
    chat ×3, `task_execution_service`, the dashboard, and any future facade reader — plus the
    two genuine facade-bypasses (`backlog_service` drain, `agent_call_limiter`). The getter is
    fail-open (a settings-read failure defaults to 10 rather than crashing dispatch) and
    read-side range-clamps a stray out-of-range stored value into `[1,32]` (defense-in-depth: a
    `0` can't fail-close the fleet, a `999` can't defeat the host cap); no
    per-process cache (backend runs `--workers 2`), so a lowered ceiling applies instantly and
    consistently across workers.
  - Canary B-02 (no-queued-without-slots-full) compares slot count against the **effective**
    cap so a lowered ceiling doesn't false-fire; S-02 (no overbooking) intentionally keeps the
    stored cap as a valid upper bound.
  - Owner UI: per-agent "Parallel Capacity" card in the agent Settings tab (input bounded by
    the ceiling, shows `active/effective`, warns when stored > ceiling). Admin UI: "Fleet
    Capacity" section in Settings → General.
- **Known limitation**: `agent_call_limiter` caches its per-agent semaphore cap at first
  access and never re-reads it — a *live* agent's semaphore does not shrink when the ceiling
  (or its own `max_parallel_tasks`) drops until process restart. Pre-existing behavior for
  per-agent edits, not a regression; new agents get the clamped cap immediately. Semaphore
  resize is out of scope.

### 10.9 Business Task Validation (VALIDATE-001)
- **Status**: ✅ Implemented (2026-04-14)
- **Requirement ID**: VALIDATE-001
- **GitHub Issue**: #294
- **Description**: Post-execution validation phase that runs a clean-context Claude session with auditor framing to verify business task completion. Separates technical success (Claude ran without errors) from business success (intended work was done).
- **Key Features**:
  - Per-schedule `validation_enabled`, `validation_prompt`, `validation_timeout_seconds` config
  - `business_status` field on executions: `pending_validation`, `validated`, `failed_validation`, `skipped`
  - Linked validation execution records via `validates_execution_id` / `validation_execution_id`
  - Default auditor prompt with explicit framing and JSON response format
  - Fallback text inference when JSON parsing fails
  - Operator queue notification on validation failure
- **Flow**: `docs/memory/feature-flows/business-validation.md`

### 10.10 Idempotency Keys at Trigger Boundaries (RELIABILITY-006)
- **Status**: ✅ Implemented (2026-06-02)
- **Requirement ID**: RELIABILITY-006
- **GitHub Issue**: #525
- **Description**: An `Idempotency-Key` contract at every execution-creating trigger boundary. The same key within a 24h window produces exactly one execution; duplicates short-circuit with the original result (`HTTP 200/202 + X-Idempotent-Replay: true`) instead of dispatching a second execution. Closes the producer-boundary dedup gap that the unified funnel made more acute — webhook re-deliveries, MCP client retries, and scheduler→backend network blips no longer create phantom executions.
- **Key Features**:
  - New `idempotency_keys` table — `PRIMARY KEY (scope, idempotency_key)` gives the atomic claim; `(execution_id, status, response_snapshot, created_at)` carry the replay payload. Cross-process safe (uvicorn workers + standalone scheduler share one DB file).
  - Enforcement at each **router boundary** (not solely the service) because sync `/chat` runs an inline path and `/api/webhooks/{token}` creates no execution: `/chat`, `/task` (async+sync, self-task), `/api/internal/execute-task`, `/api/webhooks/{token}`, `/api/agents/{name}/fan-out`.
  - Webhook auto-derives a key from `(token, body_hash)` when none supplied — covers naive senders that retry without idempotency awareness.
  - Scheduler sends a deterministic `Idempotency-Key: sched:{execution_id}` so a transient backend 5xx + resend resolves to the same key; intentional #271 retries (fresh execution_id) are not suppressed.
  - MCP `chat_with_agent` / `fan_out` forward a deterministic key over the call args so a transport retry dedupes.
  - Header is OPTIONAL on chat/task/MCP (absent → no dedup, full back-compat); upfront at-capacity rejections release the claim so the caller can retry; in-flight duplicate → 409.
  - Audit event `idempotent_replay` on every replay (duplicate-storms observable); 24h TTL purge folded into the cleanup-service retention sweep.
- **Architectural Invariant**: #18 — every new trigger type must accept an `Idempotency-Key` before merge.

#### 10.10.1 Effect-Scoped Idempotency for Outbound Side Effects
- **Status**: ✅ Implemented (2026-06-23)
- **GitHub Issue**: #1084
- **Description**: Extends RELIABILITY-006 past the trigger boundary to the **sink**. Trigger-boundary dedup stops a re-POSTed `/chat`/webhook from creating a *second execution*, but it does not reach an agent's individual outbound tool calls — so a re-delivered turn (the at-least-once semantics pull-mode / work-stealing will introduce, Epic #1045/#1081) re-emits the same side effect (re-sends a message, places a second call, re-mints a share, double-charges a payment). The same `services/idempotency_service.py` adds a per-sink guard enforced at the action, per resolved action identity. **No schema/migration change** — reuses the `idempotency_keys` table and the 24h TTL (already exceeds the lease window).
- **Key Features**:
  - New `effect_guard(effect_type, identifying_args, *, execution_id, agent_name, dedup_label, payment_request_id)` async context manager over the existing `begin`/`complete`/`fail` lifecycle.
  - Two scopes: `effect:{execution_id}` for messages/voip/share_file (after `resolve_and_validate_execution` confirms the execution belongs to the agent — generalizes MEM-001); `payment:{agent_request_id}` for Nevermined settles (native exactly-once token).
  - Key = `{effect_type}:sha256(execution_id ∥ effect_type ∥ resolved_identifying_args ∥ dedup_label)` over **resolved, immutable** identity only (recipient/channel/account/filename) — **never** the LLM-generated body (non-deterministic across a re-run → would defeat dedup). `dedup_label` lets an agent intentionally repeat an effect to the same target in one turn.
  - `in_flight ≠ completed`: a completed replay returns the stored sanitized snapshot (no re-emit); an in-flight replay raises `EffectInProgressError` → router **409** (never a silent skip-and-succeed).
  - Wired sinks: `proactive_message_service.send_message`, `voip_service.place_outbound_call`, `agent_shared_files_service.create_share`, `nevermined_payment_service.settle_payment_once`. MCP `execution_id` + `dedup_label` params on `send_message`/`call_user`/`share_file` (Invariant #13); **fail-open when absent** (safe today — pull-mode re-delivery is OFF).
- **Pull-mode gate**: turning pull-mode default-ON for any side-effect-bearing agent additionally REQUIRES (a) trusted runtime injection of `execution_id` and (b) fail-closed-when-absent — a **BLOCKING prerequisite** on Epic #1045/#1081 (documented on `dispatch_async_eligible`). git push is idempotent-by-construction and needs no key. *(Reframed in `TARGET_ARCHITECTURE.md` v2: gating is **per-effect, not per-agent** — read-only + reversible + confined-irreversible effects default on; only irreversible-un-confineable effects gate via the async operator queue (#1402). `effect_guard` becomes the reversible/backend-sink slice; retry-with-prior-trace (#1401) is the general recovery.)*
- **Flow**: `docs/memory/feature-flows/effect-idempotency.md`

### 10.11 Dispatch Circuit Breaker (RELIABILITY-007)
- **Status**: ✅ Implemented (2026-05-30); default-OFF opt-in canary
- **Requirement ID**: RELIABILITY-007
- **GitHub Issue**: #526
- **Description**: Per-agent **producer-side** circuit breaker at the dispatch layer. When an agent is *auth-dead* (reachable but answering HTTP 503 → execution `error_code == AUTH`), the breaker fast-fails NEW executions with HTTP 503 instead of poisoning the persistent backlog with doomed tasks, fails the doomed backlog immediately, and self-heals via a half-open probe. Distinct from and namespace-isolated from the transport-reachability breaker (#631) — the two never contaminate each other's counter.
- **Key Features**:
  - New `services/dispatch_breaker.py` — consecutive-failure state machine (`closed → open → half-open(probe) → closed`) in Redis `agent:dispatch:{name}` via atomic Lua (threshold 3, base cooldown 30s → 300s exponential backoff, one-probe-at-a-time `SET NX EX` lock); fail-open on Redis down, never raises
  - **AUTH-only counting** (D10): TIMEOUT / AGENT_ERROR never count, to avoid false trips on long/bad-prompt tasks
  - **No-enqueue invariant** (D2 + F1): `CapacityManager.acquire(breaker_enabled=…)` raises `CircuitOpen` *before* the overflow branch; a half-open probe is admitted only into a free slot (never enqueued) so the invariant spans the half-open window
  - Drain-on-trip: `task_execution_service` records outcomes at the terminals and on `→open` backgrounds `db.fail_queued_for_agent` (QUEUED→FAILED) + clear in-memory queue + audit; 60s `run_maintenance` breaker-aware backstop re-fails still-open-breaker backlog if an inline drain is lost (~60s worst case, not 24h)
  - Shared Redis plumbing extracted to top-level `redis_breaker_util.py` (fail-open client, Lua `ScriptCache`, decode helpers) reused by both breakers
  - Per-agent `agent_ownership.circuit_breaker_enabled` opt-in column (default OFF) + global `DISPATCH_BREAKER_ENABLED` env master switch — both must be on to engage
  - Operator API: `GET/PUT /api/agents/{name}/circuit-breaker` (read = authorized, config = owner-only + audit); `reset` (admin) clears BOTH breakers; unified block embedded in `GET /api/agents/{name}` and agent-health detail; `circuit_breakers` field on `/api/agents/slots` (pipelined HGETALL, no SCAN)
  - Frontend: distinct ⚡ "circuit open" danger badge in `AgentHeader` (detail page) and `AgentNode` (dashboard graph)
  - Exposes `record_failure("missed_heartbeat")` as the #307 heartbeat seam
- **Flow**: `docs/memory/feature-flows/dispatch-circuit-breaker.md`, `docs/memory/feature-flows/capacity-management.md`, `docs/memory/feature-flows/task-execution-service.md`

### 10.11.1 Correlated-Failure / Thundering-Herd Controls (#1085)
- **Status**: ✅ Implemented (2026-06-28); backend controls default-OFF behind one master flag, agent-side jitter ships unflagged
- **GitHub Issue**: #1085
- **Description**: Make the live #1083 fire-and-forget re-delivery path safe at fleet scale — a backend restart re-sends ~N persisted terminal envelopes plus in-flight callback retries, hammering the result-callback endpoint in lockstep. Adds **jittered re-poll/reconnect**, **per-agent + fleet-wide re-delivery rate caps**, and a **shared-cause pause** that halts re-delivery for the whole fleet on a common fault (Claude-API outage, expired platform key, a bad skill pushed fleet-wide). Built as reusable primitives that the future pull-mode re-delivery (Epic #1045/#1081) consumes unchanged. Everything is **fail-open**; **no DB schema change** (all state is Redis).
- **Key Features**:
  - **Jitter (agent-side, unflagged)** — `result_callback._deliver` uses decorrelated jitter (`min(cap, uniform(base, prev*3))`, AWS pattern) and honors a server `Retry-After` as a floor; `resend_pending_results` adds a one-shot initial-jitter (≤60s) + per-envelope jitter so a restart smears the t≈0 sweep burst; `main.py` capacity-loop period jittered so replicas don't realign. Jitter helper duplicated agent-side, not vendored (Invariant #5 governs mirrored contracts, not utility math)
  - **Re-delivery rate caps (backend)** — callback endpoint gates on `services/rate_limiter.check` keys `redelivery:fleet` (≈10/s) + `redelivery:agent:{name}`; over-limit → **503 + Retry-After** (not 429 — 503 stays retryable, so a throttled callback is never dropped: startup sweep + lease reaper backstop)
  - **Shared-cause pause** (`services/redelivery_governor.py`) — records AUTH/BILLING terminals on the CAS-`won` branch in `apply_result` (no replay double-count) into a Redis ZSET counting **distinct agents** (one crash-looper can't arm it); at `≥ CORRELATED_FAILURE_THRESHOLD` distinct agents sets a TTL'd `governor:pause` (auto-expiry, no explicit unpause). Three flag-gated read points: callback endpoint (503), lease reaper hold-off (keeps async rows RUNNING), capacity drain hold-off (keeps 24h `expire_stale`)
  - **BILLING populated** — `result_callback._STATUS_MAP` maps agent `429 → ("billing","rate_limit")` (enum existed but was never set) so a fleet-wide Claude-API 429 storm arms the detector alongside AUTH; `terminal_reason` stays `rate_limit` (cancel-relabel guard unaffected)
  - **Config** (all in `config.py`): `REDELIVERY_GOVERNOR_ENABLED` (master, default false), `REDELIVERY_FLEET_LIMIT`/`_WINDOW_SECONDS` (600/60), `REDELIVERY_AGENT_LIMIT`/`_WINDOW_SECONDS` (20/60), `CORRELATED_FAILURE_THRESHOLD` (20), `CORRELATED_FAILURE_WINDOW_SECONDS` (120), `CORRELATED_PAUSE_TTL_SECONDS` (300, < lease window), `REDELIVERY_PAUSE_RETRY_AFTER_SECONDS` (30). Surfaced as `redelivery_governor_enabled` in `GET /api/settings/feature-flags` for soak observability
- **Flow**: `docs/memory/feature-flows/redelivery-governor.md`

### 10.12 Unified Executions Dashboard (EXEC-022)
- **Status**: ✅ Implemented (2026-05-15)
- **Requirement ID**: EXEC-022
- **GitHub Issue**: #18
- **Description**: Fleet-level execution history dashboard giving operators a single view across all agent task runs, with filtering, live stat cards, and real-time updates.
- **Key Features**:
  - `GET /api/executions` — paginated execution list (status/trigger/hours/agent/search filters, offset pagination, LIMIT 50)
  - `GET /api/executions/stats` — single-pass conditional aggregation: total, success_count, failed_count, total_cost (windowed by `hours`), running_count and queued_count (always live)
  - Access control: admins see all agents, non-admins see only accessible agents via shared `accessible_agent_names()` helper
  - Frontend `/executions` page: 4 stat cards, running-now strip, filter bar, load-more list with per-row status tints and stop/navigate actions
  - NavBar running-count badge (yellow when >0)
  - Pinia store with 30s polling + `agent_activity` WebSocket refresh guard
- **Flow**: `docs/memory/feature-flows/executions-dashboard.md`

---

## 34. Agent-Defined Pipelines (#919)

### 34.1 Standardized Pipeline Introspection Surface (#919)
- **Status**: ✅ Implemented (2026-06-26)
- **Implements**: Issue #919
- **Description**: Trinity-compatible agents that run long-running
  multi-stage pipelines (perception → incubation → synthesis → publish →
  measure, or similar shapes) expose their pipeline state to Trinity
  through two standardized read-only file paths inside the agent
  container. Trinity stays a fleet orchestrator — it does **not** own
  the DAG, the execution semantics, or the recovery policy. The agent
  owns all of that via existing primitives (schedules CRUD, events,
  operator queue, pre-check hook, `dashboard.yaml`). Trinity's only
  contribution is making the agent's pipeline state uniformly
  discoverable.
- **File convention** (the canonical contract):
  - `~/.trinity/pipelines/<pipeline_id>.yaml` — pipeline definition
    (DAG, stages, transitions, preconditions, retry/escalation policy)
  - `~/.trinity/pipeline-state/<pipeline_id>/<instance_id>.json` —
    runtime state (current stage, attempt count, health, blockers,
    open escalations, per-stage metrics)
- **MCP tools** (thin file-reads via the existing `agent_files`
  router — no new backend endpoints, no new DB tables, no parsing of
  pipeline semantics in backend code):
  - `list_agent_pipelines(agent_name)` — enumerates pipelines from
    `~/.trinity/pipelines/*.yaml` with health summaries
  - `get_agent_pipeline_state(agent_name, pipeline_id, instance_id?)`
    — returns parsed state JSON; 404 (not 500) on missing pipeline
    or instance
- **Schemas**: `docs/schemas/agent-pipeline.schema.json` and
  `docs/schemas/agent-pipeline-state.schema.json` define both files
  authoritatively and are shipped alongside the
  Trinity-Compatible Agent Guide.
- **Operator-queue convention**: when an agent files an escalation
  related to a pipeline, the queue item's `context` JSON includes
  `{ pipeline_id, instance_id, stage }` so escalations group by
  pipeline in the UI. No backend schema change — `operator_queue.context`
  already accepts free-form JSON.
- **Heartbeat skill (agent-side, not Trinity)**: the agent runs a
  single `pipeline-tick` skill on a cron schedule that owns stage
  advancement, retry, and escalation. The pre-check hook gates the
  heartbeat so it's near-free when no pipeline needs attention. The
  heartbeat is shipped by the `agent-dev:add-pipeline` plugin in
  `abilityai/abilities`, not by Trinity.
- **Out of scope**: DAG execution engine in backend; cross-agent DAGs
  (expressed as event chains between independent per-agent pipelines);
  GUI editor for `pipeline.yaml`; persisting pipeline state in
  Trinity's database.
- **Implementation** (2026-06-26): shipped as an **MCP-only** change —
  `src/mcp-server/src/tools/pipelines.ts` adds the two tools over the
  **existing** `GET /api/agents/{name}/files` (recursive list) and
  `/files/download` (read) surfaces via two new thin client methods
  (`listAgentFiles`/`downloadAgentFile`, sharing a `_fetch` helper). No
  backend router/service, no agent-server endpoint, no DB table (Invariant
  #8/#13 satisfied by reuse). Hardening: `pipeline_id`/`instance_id` are
  zod-validated `^[A-Za-z0-9._-]+$` and reject `..`/`/`/encoded-slashes
  **before** any download (the download endpoint has no deny-list — a P1
  traversal guard); definition YAML is parsed with a 256 KiB pre-parse size
  cap + duplicate-key rejection + alias-expansion guard, and a malformed
  single file is an item-level error that never aborts the list. Latest
  instance is selected by state-file mtime (tie-break: lexical
  `instance_id`), keeping the read fan-out at one download per pipeline
  (capped at 50 pipelines, truncation logged). Error contract: only a 404
  maps to empty/not-found; a 400 (agent stopped) or 5xx (unreachable)
  surfaces as a distinct real error. Authoritative file schemas live in
  `docs/schemas/agent-pipeline.schema.json` and
  `agent-pipeline-state.schema.json`; the agent guide documents the
  contract, the operator-queue `context` convention, and the adoption note.
## 35. Schedule Timeout Validation (#929)

### 35.1 Agent Cap as Schedule Ceiling (#929)
- **Status**: ✅ Implemented (#930)
- **Implements**: Issue #929
- **Description**: `agent_ownership.execution_timeout_seconds` becomes
  a hard ceiling for `agent_schedules.timeout_seconds`. The two
  settings previously coexisted as independent knobs with no
  enforcement between them — schedules silently won, and the agent
  cap applied only to the chat/ad-hoc fallback path. That divergence
  trapped operators who assumed `min(agent, schedule)` semantics from
  the side-by-side UI. Approach A from #929: validate at write time
  so the operator's mental model snaps into place — the agent cap is
  a real ceiling, exceeded values fail fast at config time instead
  of silently surviving until SIGKILL.
- **Validation rules**:
  - `POST /api/agents/{name}/schedules` — 400 if
    `body.timeout_seconds > agent.execution_timeout_seconds`.
  - `PUT /api/agents/{name}/schedules/{id}` — 400 if the new
    `timeout_seconds` would exceed the agent cap.
  - `PUT /api/agents/{name}/timeout` — 400 if the new agent cap
    would drop below any non-deleted schedule's `timeout_seconds`
    (caller must raise the cap before lowering individual schedules,
    or vice versa).
- **Error contract**: 400 responses use FastAPI `HTTPException` with
  a structured detail dict so clients can branch on the cause:
  ```json
  {
    "error": "schedule_timeout_exceeds_agent_cap",
    "message": "Schedule timeout 7200s exceeds agent execution_timeout_seconds 3600s. Raise the agent cap via PUT /api/agents/{name}/timeout first.",
    "agent_cap_seconds": 3600,
    "requested_seconds": 7200
  }
  ```
  and respectively `agent_timeout_below_active_schedules` for the
  agent-cap-lowering path (carries
  `max_schedule_timeout_seconds` + the offending schedule list).
- **DB accessor**:
  `db.find_active_schedules_exceeding_timeout(agent_name, ceiling)` —
  returns `[{id, name, timeout_seconds}, …]` for every non-soft-deleted
  schedule whose `timeout_seconds > ceiling`, ordered DESC. Powers the
  agent-timeout endpoint's 400 detail payload (operator sees which
  schedules block the cap-lowering). Schedule endpoints compare
  directly against `db.get_execution_timeout(agent_name)`.
- **No retro-validation**: pre-existing rows that violate the
  invariant (`schedule.timeout_seconds > agent.execution_timeout_seconds`)
  are left alone — the migration story is "next edit fixes it." The
  agent-cap-lowering check still sees those rows so the operator can't
  make the gap *worse*.
- **Orthogonal SIGKILL error-message fix** (same PR): the agent-side
  signal-exit classifier in
  `docker/base-image/agent_server/services/error_classifier.py`
  emitted `"Likely cause: schedule/agent timeout exceeded, OOM kill,
  or operator cancel."`. With the cap enforced at write time, the
  "/agent" disjunction is dead — schedules can never run past the
  cap. Message simplified to surface the schedule timeout
  unambiguously.
- **Out of scope**: exposing `timeout_seconds_effective` /
  `capped_by` on the schedule response (Approach B from #929 —
  would be trivially identical to `timeout_seconds` under A and
  pure clutter); retrofitting the SIGKILL message to know whether
  OOM vs timeout fired (agent has no signal for that distinction).

---

## 37. MCP Chat Timeout Recovery (#914)

### 37.1 `chat_with_agent` Gateway-Timeout Receipt (#914)
- **Status**: ✅ Implemented (#933)
- **Implements**: Issue #914
- **Description**: `mcp__trinity__chat_with_agent` in default sync
  mode (`parallel=false`) holds the MCP-gateway → backend → agent HTTP
  chain open for the entire agent execution. When the agent takes
  longer than the MCP client's request timeout (~30-60s observed),
  the tool call returns the generic `fetch failed` — but the request
  was successfully queued on Trinity and the agent IS running it.
  Naive retry then queues duplicates that Trinity's
  concurrent-duplicate guard kills mid-execution, burning compute and
  agent time. This change is the MCP-client-surface fix for #408 /
  #428's long-running dispatch family.
- **Approach**: an MCP-server-side timeout (~25s, under the typical
  gateway ceiling) aborts the backend `fetch` early. The MCP server
  then queries `GET /api/agents/{name}/executions` for a recent
  matching execution row (`triggered_by in {mcp, agent}`,
  `source_mcp_key_id == this key`, non-terminal status, started
  within the last ~30s) and returns a **structured receipt** to the
  caller instead of letting `fetch failed` propagate:
  ```json
  {
    "status": "queued_timeout",
    "agent": "bdr-agent",
    "execution_id": "fZv-iXtUXSolY1wzPO7T6w",
    "message": "MCP gateway timeout — task still running on the agent. Poll get_execution_result(execution_id) instead of retrying."
  }
  ```
- **No-match fallback**: when the lookup turns up nothing (no rows
  on the agent, or the executions endpoint itself is unreachable),
  the abort error is rethrown with a clearer hint so the caller
  knows to check the dashboard before retrying. The receipt is a
  best-effort heuristic, not an atomic protocol guarantee.
- **MCP `chat_with_agent` tool description** is updated to
  document the new `queued_timeout` return shape so the caller's
  agent / LLM can branch on it correctly.
- **Configurability**: `MCP_CHAT_TIMEOUT_MS` env var on the MCP
  server (default 25000) lets operators dial the abort window for
  unusually slow networks. The 25s default sits comfortably below
  the 30-60s gateway ceiling observed in the issue.
- **Out of scope**:
  - Real push-completion redesign (#408 / #428) — this is the
    cheap interim until that lands.
  - Idempotency keys (#914 comment) — needs new backend column
    + write-path coordination; bigger surface.
  - Backend-side change to return `execution_id` as a streaming
    response header on long calls — would obsolete the heuristic
    lookup but requires a `chat_with_agent` API contract change.
## 38. Sequential Agent Loops (#740)

### 38.1 `run_agent_loop` MCP Tool + Backend Loop Service (#740 — Phase 1)
- **Status**: 🚧 In Progress
- **Implements**: Issue #740
- **Description**: Server-side primitive for sequential bounded
  repetition of agent tasks. Complements `chat_with_agent` (single
  turn) and `fan_out` (parallel batch) with a third execution
  pattern: run a task N times in order, each iteration optionally
  using the previous response. Caller fires once, gets a `loop_id`,
  and disconnects — loop state lives in the backend.
- **Modes**:
  - **Fixed** (`stop_signal` unset): runs exactly `max_runs` times.
  - **Until** (`stop_signal` set, recommended sentinel `[[DONE]]`):
    stops early when any iteration's response contains the signal.
- **Endpoints**:
  - `POST /api/agents/{name}/loops` — start a loop. Returns
    `{loop_id, status: "queued", agent_name, max_runs}` immediately
    (fire-and-disconnect). Body: `message` (template, supports
    `{{run}}` 1-indexed and `{{previous_response}}` truncated to the
    last 2000 chars), `max_runs` (1–100, required), `stop_signal`,
    `delay_seconds` (between runs, default 0), `timeout_per_run`
    (defaults to agent's configured `execution_timeout_seconds`),
    `model`, `allowed_tools`.
  - `GET /api/loops/{loop_id}` — status + per-run summaries + last
    full response.
  - `POST /api/loops/{loop_id}/stop` — graceful stop. Sets
    `should_stop`; the current iteration finishes, the loop exits.
    Returns `{status: "stopping" | "already_done"}`.
- **MCP tools**: `run_agent_loop`, `get_loop_status`, `stop_loop`.
  Permission rules match `chat_with_agent` (owner/admin/shared or
  explicit `agent_permissions` for agent-scoped keys).
- **Execution model**: each iteration goes through the standard
  `task_execution_service.execute_task()` path → `capacity_manager`
  admit/slot → execute → release. Each iteration is recorded in
  `schedule_executions` with `triggered_by="loop"` and `loop_id` set
  so the dashboard/timeline shows iterations as normal execution
  rows tagged with their loop. Sequential: iteration N+1 does not
  start until iteration N's row reaches a terminal status.
- **Template substitution**: applied before each iteration.
  `{{run}}` → `"1"`, `"2"`, … `{{previous_response}}` → empty on
  iteration 1, otherwise the previous iteration's response trimmed
  to the trailing 2000 chars.
- **Stop signal check**: substring match (`stop_signal in response`)
  applied to the full response after each iteration. Recommended
  sentinel `[[DONE]]` is documentation only — the loop honors any
  user-supplied string.
- **Terminal states + stop reasons**:
  - `completed` / `max_runs_reached` — fixed mode hit `max_runs`,
    or until mode hit `max_runs` without seeing the signal.
  - `completed` / `stop_signal_matched` — until mode saw the signal.
  - `stopped` / `user_stopped` — `POST /loops/{id}/stop` triggered.
  - `failed` / `error` — an iteration's task execution returned a
    non-success terminal status; loop aborts at the failed iteration.
  - `interrupted` / `interrupted` — backend restart while running.
- **Restart recovery**: the cleanup-service startup hook re-marks
  any `agent_loops` row in `running` status as `interrupted` with
  `stop_reason="interrupted"`. Loops do not auto-resume —
  callers re-issue if needed.
- **WebSocket events**: `loop_run_completed` per iteration (carries
  `run_number`, `execution_id`, `cost`, `duration_ms`),
  `loop_completed` once when the loop exits any terminal state.
- **Storage**: two new tables in main SQLite DB.
  - `agent_loops` (id, agent_name, message_template, max_runs,
    stop_signal, delay_seconds, timeout_per_run, model,
    allowed_tools JSON, status, runs_completed, stop_reason,
    last_response, started_by_user_id, started_by_user_email,
    source_agent_name, source_mcp_key_id, source_mcp_key_name,
    created_at, started_at, completed_at).
  - `agent_loop_runs` (id, loop_id, run_number, execution_id,
    status, response, cost, duration_ms, started_at, completed_at)
    — one row per iteration; `execution_id` joins back to
    `schedule_executions`.
  - `schedule_executions.loop_id TEXT` column added for the
    timeline-tag join.
- **Out of scope (Phase 1)**: dedicated dashboard surface for loops
  (current timeline is sufficient — iterations appear as normal
  rows; a follow-up PR may add a collapse-group affordance);
  auto-resume after restart; cross-agent loops (`agent` parameter
  is `"self"` only for v1, matching `fan_out`).

### 38.2 Loop-level wall-clock deadline (#1156)
- **Status**: ✅ Implemented
- **Implements**: Issue #1156
- **Description**: A third hard stop alongside the `max_runs` iteration
  cap and the (separately tracked) cost budget: an optional total
  wall-clock deadline so a loop legally configured today (`max_runs=100`
  × `timeout_per_run` up to 2h + `delay_seconds`) cannot run for days.
- **Parameter**: optional `max_duration_seconds` (int, 1 – 604800 = 7d;
  NULL/omitted disables). Accepted on `POST /api/agents/{name}/loops`,
  persisted on `agent_loops.max_duration_seconds`, exposed via the
  `run_agent_loop` MCP tool.
- **Enforcement**: deadline measured from `started_at`; checked only at
  iteration boundaries — before starting the next run and before/after
  the inter-run delay (the `delay_seconds` sleep is capped to the
  remaining budget, never sleeping past the deadline). An in-flight run
  is never killed mid-turn, so actual overshoot is bounded by one
  `timeout_per_run`.
- **Terminal state**: expiry stops the loop with terminal status
  `stopped` and `stop_reason="deadline_exceeded"`.
- **Validation**: reject (400) `max_duration_seconds` smaller than the
  effective per-run timeout (`timeout_per_run`, else the agent's
  `execution_timeout_seconds`) — otherwise no iteration could finish
  before the deadline.
- **Observability**: `GET /api/loops/{loop_id}` returns
  `max_duration_seconds` and a computed `elapsed_seconds` (from
  `started_at` to `completed_at` or now); the Loops UI shows the
  deadline + elapsed when set.
- **Out of scope**: interrupting an in-flight run mid-turn; persisting
  elapsed across a backend restart (loops do not auto-resume).

### 38.3 Loop-level cost budget (#1155)
- **Status**: ✅ Implemented
- **Implements**: Issue #1155
- **Description**: An optional per-loop USD spend ceiling — a fourth hard
  stop alongside `max_runs`, the deadline (#1156), and `stop_signal`. A
  `max_runs=100` loop on an expensive model previously had no cost bound.
- **Parameter**: optional `max_cost_usd` (float, `gt=0`, no upper cap so
  sub-cent budgets are allowed; NULL/omitted disables). Accepted on
  `POST /api/agents/{name}/loops`, persisted on
  `agent_loops.max_cost_usd`, exposed via the `run_agent_loop` MCP tool.
- **Enforcement (iteration-boundary gate)**: the runner accumulates each
  completed run's cost in memory and, *before starting the next run*,
  stops the loop once accumulated cost meets/exceeds the budget. Checked
  *after* the deadline check. Only finite, positive costs accumulate; a
  NaN/inf cost is ignored (so it can't poison the accumulator); a
  NULL/unknown cost counts as **0** (fail-open per AC — `max_runs` still
  bounds the loop) and emits one `logger.warning` per such run when a
  budget is active.
- **Honest semantics (not a mid-run hard cap)**: the current run always
  finishes, so one run — **including the first** — can overshoot the
  budget by any amount. The gate is "checked between runs"; an in-flight
  run is never killed mid-turn.
- **Precedence (boundary-only)**: per iteration the order is
  `user_stopped` → `deadline_exceeded` → `budget_exhausted` → run →
  `stop_signal_matched`; natural exit `max_runs_reached`. `budget_exhausted`
  fires *only when a next iteration would start over budget* — a run that
  crosses the budget but is also the final `max_runs` run or matches
  `stop_signal` yields those reasons instead.
- **Terminal state**: terminal status `stopped` with
  `stop_reason="budget_exhausted"`.
- **Observability**: `GET /api/loops/{loop_id}` returns `max_cost_usd` and
  a `total_cost` **computed on read** as the sum of `agent_loop_runs.cost`
  (NULL→0; `0.0` for a zero-run loop — no stored column to drift); the
  Loops UI shows spend / budget when set.
- **Out of scope**: mid-run cost interruption (would need streaming cost
  callbacks the runtime doesn't expose); a stored `total_cost` column;
  stopping when cost is unknown (contradicts the fail-open AC).
### 38.4 No-progress / doom-loop detection (#1157)
- **Status**: ✅ Implemented
- **Implements**: Issue #1157
- **Description**: A loop feeding `{{previous_response}}` forward can get
  stuck re-emitting the same response every iteration, burning the entire
  remaining `max_runs` budget while making zero progress (the classic
  autonomous-agent "doom loop"). Iteration caps don't catch it. Detect it
  by fingerprinting each successful run's response and stopping once K
  consecutive runs are identical.
- **Parameter**: optional `no_progress_threshold` (int; `0` disables;
  **default 3** for new loops; `1` rejected → 422, since "repeated
  identical" needs ≥2). Accepted on `POST /api/agents/{name}/loops`,
  persisted on `agent_loops.no_progress_threshold` (nullable — **NULL ⇒
  disabled** so loops created before this change keep today's behavior),
  exposed via the `run_agent_loop` MCP tool and the Loops UI.
- **Detection**: SHA-256 of the **full** response text normalized by
  collapsing whitespace runs to single spaces and stripping
  (`" ".join(text.split())`) — preserves word boundaries (`"foo bar"` ≠
  `"foobar"`); empty / None / whitespace-only all normalize to one
  fingerprint (repeated empty output IS a doom loop and counts). Counter +
  last-fingerprint are **runner-local** (no per-run persistence). **Exact-hash
  only** — no fuzzy/semantic similarity (out of scope; would need an LLM
  judge).
- **Terminal state**: stops the loop with terminal status `stopped` and
  `stop_reason="no_progress"`.
- **Precedence**: `stop_signal_matched` wins (checked first in the success
  branch); a pending `user_stopped` or passed `deadline_exceeded` also
  outranks `no_progress` (re-checked before the no-progress break) — an
  explicit operator Stop or deadline must never be relabeled "no progress".
- **Known limitation / mitigation**: a loop that legitimately repeats an
  identical confirmation while making external progress will be stopped.
  Mitigated (not solved) by the `0`-to-disable escape, the MCP tool /
  UI helper text, and the default-on behavior-change note — NULL⇒disabled
  shields in-flight loops.
- **Out of scope**: fuzzy/semantic similarity; progress-identity vs
  response-identity (a tool-call/external-effect fingerprint); persisting
  the fingerprint/counter; retro-applying detection to in-flight loops.

---
