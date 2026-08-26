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
- **Status**: ✅ Implemented (2026-01-01); gate semantics corrected 2026-08-03 (#1945)
- **Description**: Master gate for agent autonomous operation
- **Key Features**: Dashboard toggle; gates every cron fire for the agent
- **Flow**: `docs/memory/feature-flows/autonomy-mode.md`

#### 10.2.1 Autonomy is a Gate, Not a Bulk Edit (#1945)
- **Status**: ✅ Implemented (2026-08-03)
- **GitHub Issue**: #1945
- **Description**: `set_autonomy_status_logic` used to loop `db.set_schedule_enabled(id, enabled)` over every schedule on the agent, unfiltered and in both directions — so the agent-level gate and the per-schedule `enabled` flag shared one write path and only one survived. The first toggle destroyed per-schedule intent: an owner-disabled (or template-authored `enabled: false`) schedule was silently re-armed on the next autonomy-on, and autonomy-off was a set-all rather than a pause. Since a template can materialize up to 20 declared schedules at creation, one unrelated toggle could arm all of them at once (LLM cost amplification).
- **Requirement**: the autonomy toggle MUST write only `agent_ownership.autonomy_enabled`. Per-schedule `enabled` is owner intent — nothing may rewrite it except an explicit per-schedule change (Schedules tab / `POST .../schedules/{id}/enable|disable` / `update_schedule`) or an explicit admin fleet op (`/api/ops/schedules/pause|resume`, `emergency_stop`).
- **Enforcement**: the scheduler's cron-only gate (`src/scheduler/service.py::_execute_schedule_with_lock` → `get_autonomy_enabled`) is authoritative and unchanged — it now carries the whole load, so an enabled schedule on a paused agent is a normal state: skipped, no execution row, `next_run_at` projection advanced (#1472), shown in the UI as "Will not fire — autonomy off" (#1796). A manual trigger still bypasses autonomy by design.
- **No schema change**: the fix is the removal of a write — no new column, no migration, nothing to keep in dual track.
- **Upgrade behavior**: existing rows are never rewritten. An agent already flattened to all-disabled by a pre-#1945 toggle stays that way (the erased intent is unrecoverable); the toggle response says so explicitly instead of silently re-arming.
- **API**: `PUT /api/agents/{name}/autonomy` drops `schedules_updated` (a count of a write that no longer happens) in favor of `total_schedules`, `enabled_schedules`, and a server-authored `message`.
- **Tests**: `tests/unit/test_1945_autonomy_preserves_schedule_intent.py` (AC5 off→on cycle, no-write proof via unchanged `updated_at`/`next_run_at`, response contract, scheduler-gate source pin); `tests/unit/test_1557_autonomy_breaker_decoupled.py` updated — its "still suppresses proactive work" guard now pins the gate write and forbids the fan-out.

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

### 10.4.3 Completed-Turn Recovery on `error_during_execution` (#1870)
- **Status**: ✅ Implemented (2026-08-02)
- **GitHub Issue**: #1870
- **Description**: Claude Code can report a `result` line with `is_error: true` /
  `subtype: error_during_execution` for a turn that actually **finished**. The
  reproduction is a fan-out turn: the model reaches `stop_reason: end_turn`, a
  background subagent's `<task-notification>` lands *after* it, the follow-on turn
  is interrupted, and the CLI's terminal-state check sees a non-terminal last
  message — so it reports the whole turn as failed
  (`[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null`).
  `_finalize_headless_result` treated that as terminal and raised HTTP 502,
  discarding a complete assistant answer that was sitting on disk. Observed 4× in
  ~30 runs of one schedule. The existing #678 recovery could not be reused: its
  boundary walk anchors on the newest string-content user record, and the trailing
  `<task-notification>` **is** one — so the boundary lands *past* the completed
  answer and the forward scan returns nothing. The notification is inside any
  plausible time window, so adding a `since` parameter would not have helped
  either; the boundary rule itself was the defect.
- **Key Features**:
  - New additive recovery surface
    `jsonl_recovery._recover_completed_turn_from_jsonl(session_id, since_iso)`,
    consulted from inside the existing `execution_error` branch via
    `headless_executor._try_recover_completed_turn(ctx)`. On a hit the recovered
    text becomes the response and the turn returns **200 / SUCCESS**; on a miss the
    502 is unchanged. `_recover_response_from_jsonl` (#678) is **not modified**.
  - **Three independent gates, all required** — *main-thread only* × *turn-scoped* ×
    *finished*. "Finished" is on-disk `message.stop_reason == "end_turn"`, and the
    **last** qualifying assistant record must *itself* carry it (an interrupted
    mid-tool thread cannot be rescued by an earlier marker). `max_tokens` and
    `stop_sequence` are rejected. Sub-thread records (`isSidechain` / `isMeta`) are
    excluded from marker selection, the boundary walk **and** text collection — a
    subagent's `end_turn` plus its string-content prompt otherwise satisfy both the
    marker and boundary tests, so a crashed main thread would return 200 carrying a
    subagent's internal thought.
  - **The recovered answer is the marker message's `message.id` group, fail-closed
    when that group holds no text** — never a text window ending at the marker. A
    thinking-enabled final message is written as *two* records sharing one
    `message.id` (`thinking` then `text`), and **both** carry `end_turn`; measured
    over 1,075 real transcripts, **40.6% of markers are thinking-only**.
    `_read_jsonl_records` drops the final partial line on an interrupted write —
    and this bug *is* the interrupted-tail case — so a window rule would silently
    store the turn's narration *without* the answer as a 200 SUCCESS, with no error
    and no retry. Grouping by `message.id` is also exactly `result_text` semantics,
    so a recovered response is the same artifact a clean success stores. A marker
    carrying no `message.id` (unobserved — 0 of 6,663 corpus markers) falls back to a
    `(boundary, marker]` window walk, but **only after the marker record is confirmed
    to carry text itself**: without an id the thinking/text split cannot be grouped,
    so an unguarded window would return the turn's earlier *narration* — answer
    absent — as a 200 SUCCESS, re-opening the same regression on the one path that
    would ever run if the field disappeared.
  - **Staleness guard**: the marker's timestamp must parse and fall within
    `[task_start_iso, now + 300s]`. Without the lower bound, an aborted turn on a
    `--resume` session would recover the *previous* turn's answer as this turn's
    success. The upper bound exists because `task_start_iso` is a naive-UTC value
    stamped `Z`, so a container clock ahead of UTC would fail **open**. An
    unparseable `since_iso` declines.
  - **Distinguishing signals — a recovered turn is SUCCESS but never
    indistinguishable from a clean one.** (1) `ExecutionMetadata.recovered_terminal`
    (agent-side, additive, defaulted) is the machine-readable marker, deliberately
    **separate** from `recovered_from_jsonl`, which #678 already sets whenever mere
    *telemetry* is back-filled from disk — overloading it would make "a reported
    failure became a success" unmeasurable. Both are set on recovery. (2) A
    `_RECOVERY_NOTICE` line is prepended to the response so an operator reading the
    deliverable cannot mistake it; it names the partial-checkpoint risk explicitly,
    because a recovered answer can legitimately be a mid-turn checkpoint rather than
    the final deliverable. The notice is part of the stored response and therefore
    flows into loop templating (`{{previous_response}}`) and fan-out joins — that is
    deliberate and pinned by test.
  - **Where each signal actually lands (verified, not assumed).**
    `task_execution_service.apply_result`'s success branch cherry-picks exactly six
    metadata keys (`cache_read_tokens`/`cache_creation_tokens`/`input_tokens`,
    `context_window`, `cost_usd`, `session_id`, `compact_events`) and drops the
    rest; `schedule_executions` has no metadata column, and the #1083 async callback
    converges on the same applier. So `recovered_terminal` — like `error_type` and
    `recovered_from_jsonl` before it — is **agent-side and on-the-wire only, not
    persisted to a DB column today**. The signals that genuinely survive onto the
    execution row are the **recovery notice** (it rides inside the stored `response`
    text) and the **agent log line** (captured by Vector). That makes the notice the
    only persisted operator-facing signal, not a secondary nicety — worth knowing
    before anyone proposes reducing it to a footnote. Teaching the backend to read
    `recovered_terminal` is a follow-up; `apply_result`'s success branch is the
    single chokepoint. The field is additive and defaulted, so an old backend with a
    new image is safe and a newer backend can start reading it with no coordination.
  - **Observability in both directions**: a hit logs
    `event=completed_turn_recovered_from_jsonl`; **every** decline logs
    `event=completed_turn_recovery_declined reason=<no_since_iso|file_missing|
    no_session_id|invalid_session_id|no_records|no_marker|marker_no_timestamp|
    future_marker|malformed_message|stale_marker|sub_thread_only|not_finished|
    no_boundary|no_text|exception>`. A fail-closed gate that silently stops firing is
    otherwise indistinguishable from "the bug never happened", so the reasons are
    deliberately split by **the action they imply**, not by where the code returned:
    `marker_no_timestamp` / `malformed_message` mean the on-disk format moved (fix
    the parser), `future_marker` means the container clock is wrong (fix the clock),
    while `stale_marker`, `sub_thread_only` and `not_finished` are the guard working
    as designed — `not_finished` (the turn was genuinely interrupted) being the
    expected steady-state decline, and it carries the observed `stop_reason` as a
    `stop_reason=<token>` field, echoed only when it matches `^[a-z_]{1,32}$` because
    the value comes from an untrusted file. Logs carry session id, that token and
    character counts only — never the text.
  - Recovery is scoped to `error_type == "execution_error"` only; `rate_limit` /
    `max_turns` / `authentication_failed` short-circuit above it and are unaffected.
    A recovery exception degrades to today's 502, never a 500.
- **⚠️ Coverage bound — permanent, with no fallback**: this reads a JSONL that only
  exists when session persistence is on. `headless_executor` auto-enables it for
  `timeout_seconds > 600` only, and `task_execution_service.execute_task` never
  passes `persist_session=True` — so **every schedule or webhook whose agent has
  `execution_timeout_seconds <= 600` writes no JSONL and #1870 remains unfixed for
  it, silently.** There is no in-memory alternative: stream-json carries no
  completion signal (`message.stop_reason` measured `None` in 179/179 real
  assistant records, versus 99.96% populated on disk). **The only operator lever is
  raising the agent's execution timeout above 600s** (`PUT /api/agents/{name}/timeout`,
  §10.7). When diagnosing "the fix isn't firing", check for
  `event=jsonl_persistence_auto_enabled` in the agent's logs *first* — its absence is
  the answer, not a gate problem.
- **Rollout**: the change is inside `trinity-agent-base`. A running fleet keeps
  discarding completed turns until `./scripts/deploy/build-base-image.sh` runs **and
  each agent is cold-recreated** (a plain restart does not always adopt, #1809).
- **Files**:
  - `docker/base-image/agent_server/services/jsonl_recovery.py` — `_recover_completed_turn_from_jsonl`, `_is_main_thread`, `_is_before`
  - `docker/base-image/agent_server/services/headless_executor.py` — `_try_recover_completed_turn`, `_RECOVERY_NOTICE`, the `execution_error` branch
  - `docker/base-image/agent_server/models.py` — `ExecutionMetadata.recovered_terminal`

### 10.4.4 Telemetry + Transcript on a Failing `error_during_execution` / 504 Row (#1853)
- **Status**: ✅ Implemented (2026-08-16)
- **GitHub Issue**: #1853
- **Description**: An execution that ends `error_during_execution` (Claude Code
  reports `is_error` with a non-terminal last message — commonly a fan-out whose
  `<task-notification>` lands after `stop_reason=end_turn` and interrupts the
  follow-on turn) was written to `schedule_executions` with **status + error
  string only**: no `claude_session_id`, no `cost`, no `execution_log`. Measured
  on an ops instance: 26 `success` rows all carried log + session_id + cost; 6
  `failed` rows carried none — the whole failure class was **undiagnosable after
  the fact**. This is the observability substrate for the Aug-14 "system agent
  identifies errors and proposes solutions" initiative (reliability lane; TOWARD
  the #1401 recovery-trace target). Two siblings shipped adjacent and did **not**
  touch this: #1944 (#1870) recovers only the *misclassified-completed* subclass
  to SUCCESS; #1938 (#1849) fixed only the error *string*.
- **Root cause (two structural gaps)**:
  1. **Agent**: the `execution_error` 502 raise carried a bare `detail="Execution
     error: <msg>"` — unlike the sibling 504 timeout path (`_timeout_504_detail`,
     #1201) which carries `{message, termination_reason, metadata}`.
     `ctx.metadata` (session_id, cost_usd, duration_ms, tokens, context_window)
     and `ctx.raw_messages` (the full stream-json transcript) were in scope at the
     raise but discarded.
  2. **Backend**: `apply_result`'s FAILED branch never passed `execution_log` /
     `claude_session_id` (the SUCCESS branch passes both, sanitized).
     `cost` / `context` already salvaged from the body — null only because the
     body was bare.
- **Chosen approach — B (inline the sanitized transcript)**: the FAILED branch
  **mirrors the SUCCESS branch** — it stores `execution_log =
  sanitize_execution_log(json.dumps(raw_messages))`, a derived `tool_calls`
  summary (#1741 — a summary, never a second transcript copy), `claude_session_id`
  (UUID-validated agent-side), and the already-salvaged cost/context. The
  transcript lives in the DB row, retrievable via the **same API as SUCCESS**,
  surviving agent stop **and** delete, inheriting the 30-day
  `execution_log_retention_days` window (#772). Chosen over a JSONL-survival-only
  approach because SUCCESS already inlines the full sanitized transcript (avg 573
  KB) on **every** run — failures are far rarer, so inlining on failure is
  *strictly cheaper* than the hot path already pays — and it needs **no** reaper
  keep-window, no new query, no reap race, and no retrieval endpoint. It also
  captures **short-timeout runs** (`timeout ≤ 600s`, no JSONL persisted): the
  transcript comes from in-memory `ctx.raw_messages`, not the on-disk JSONL, so it
  is not gated on #678 persistence.
- **Key Features**:
  - **Agent — `_execution_error_502_detail(ctx, message)`** (beside
    `_timeout_504_detail`): returns `{message, metadata, execution_log}` where
    `metadata = sanitize_dict(ctx.metadata.model_dump())` with `session_id`
    resolved as `metadata.session_id or ctx.claude_session_uuid` **and
    UUID-shape-validated** via `_valid_session_id`, and `execution_log =
    ctx.raw_messages`. The error `message` text is **byte-identical** to before
    (preserves #1938's error-string fix and the backend's resume-not-found
    self-heal, which reads `detail["message"]`).
  - **Agent — `_valid_session_id(x)`**: persists the fallback session id only when
    it matches the strict JSONL-filename UUID shape (mirrors the reaper's
    `_UUID_RE`), else `None`. The fallback source `ctx.claude_session_uuid` can be
    an untrusted `resume_session_id` (a log-forging vector — `sanitize_dict` does
    not strip newlines), so the validation is a security gate, not cosmetics.
  - **Agent — `_timeout_504_detail` gains the same** validated `session_id`
    fallback + `execution_log = ctx.raw_messages`, so the 504 timeout path now
    persists session_id + transcript **for real** (previously session_id was null
    whenever the reader wedged, and it carried no transcript). Both 502 and 504
    route through the same backend FAILED branch, so one backend change covers
    both.
  - **Backend — `_extract_agent_error` returns `(error_msg, metadata,
    execution_log)`**; the `except httpx.HTTPError` handler threads the extracted
    transcript + validated `session_id` onto the **existing**
    `TerminalEnvelope.execution_log` / `.session_id` fields (previously unset on
    failure).
  - **Backend — FAILED branch of `apply_result` mirrors the SUCCESS branch**:
    reuses the identical `sanitize_execution_log` + `extract_tool_calls`
    (defense-in-depth redaction happens at the backend applier, exactly as
    SUCCESS), and passes `execution_log` / `tool_calls` / `claude_session_id` into
    the **existing** `db.update_execution_status` call. No schema change
    (`execution_log` / `claude_session_id` / `cost` / `tool_calls` already exist
    and `update_execution_status` already accepts all of them). `duration_ms` is
    already computed inside `update_execution_status`.
- **Invariants respected**: **single terminal applier (#1483)** — only the payload
  of the *existing* FAILED branch is widened; **no** new CAS-writing path and
  `_write_terminal_and_gate` is untouched. **CAS-won close/emit contracts
  (#1578/#1804)** — every side-effect stays gated on the existing `won` bool; the
  new columns are added to the SET clause *above* the `if won and activity_id:`
  gate and **no predicate is widened**.
- **⚠️ Named residuals (the Aug-14 initiative must NOT assume every FAILED row
  carries telemetry)**: `_write_terminal_and_gate` terminals (backend-detected
  timeout / budget / crash) still land bare — they carry no agent metadata, and
  are out of the single-applier scope. The standalone-scheduler RETRY-001 FAILED
  writes (`src/scheduler/`) are also outside this applier. Both are follow-ups.
- **Graceful degrade / mixed-fleet**: `_extract_agent_error` already handles a
  bare-string `detail` (old image) — new-backend + old-image sends a bare string
  → `execution_log` / `session_id` stay null (= today's behaviour). Safe both
  directions.
- **Rollout**: the agent half is inside `trinity-agent-base`. A running fleet
  keeps writing bare FAILED rows until `./scripts/deploy/build-base-image.sh` runs
  **and each agent is cold-recreated** (a plain restart does not always adopt,
  #1809). The backend half is live on backend deploy.
- **Files**:
  - `docker/base-image/agent_server/services/headless_executor.py` —
    `_execution_error_502_detail`, `_valid_session_id`, the `execution_error`
    branch, `_timeout_504_detail`, and the corrected auto-persist comment (real
    behaviour is a 6h sweep / 1h age-guard = 1–7h effective, not "24h")
  - `src/backend/services/task_execution_service.py` — `_extract_agent_error`
    (3-tuple), the `except httpx.HTTPError` handler, `apply_result` FAILED branch

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
  - Two scopes: `effect:{execution_id}` for messages/voip/share_file (after `resolve_and_validate_execution` confirms the execution belongs to the agent — generalizes MEM-001); `payment:{agent_request_id}` for Nevermined settles (a Nevermined observability id, **not** a provider exactly-once token — the local guard enforces at-most-once per id; residual at-least-once retry tracked by #1408).
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

### 10.13 Pull-Mode Re-Delivery Cap & Async Operator Human-Gate (#1402)
- **Status**: ✅ Implemented — cap/park mechanism shipped 2026-07-12 (#1550, Phase 3); async-contract surface shipped 2026-07-15 (#1402)
- **GitHub Issue**: #1402 (sub of #1081); companion #1401 (recovery trace); pull-path prompt delivery was #1629 (fixed in #1633, merged to dev — see below)
- **Description**: Under pull/work-stealing (#1081), lease-expiry re-delivery re-runs failed turns. Two cases must escalate to a human instead of looping: a **poison task** that fails every re-delivery, and an **irreversible-and-un-confineable effect** the platform cannot make safe (agent's own keys / `gh` / `curl` — no confined Trinity tool in the path). Both land in the **asynchronous operator queue** (OPS-001). There are **no synchronous user gates**: a turn that needs a human parks a request and ENDS — it never holds a worker waiting on a person.
- **Re-delivery cap (mechanism — shipped Phase 3)**:
  - `schedule_executions.redelivery_count` + fleet-wide `MAX_REDELIVERY` (env, default 3); distinct from #678's `retry_count` (reader-race auto-retry).
  - Under the cap the lease reaper re-queues the SAME row (`execution_id` preserved — effect_guard #1084 and idempotency #525 are execution_id-scoped); at the cap it poison-parks: operator alert FIRST (idempotent `poison-{execution_id}` item), then CAS-FAILs the row (`poison_lease` tag) — never silently dropped.
  - **Counter semantics: raw attempts, NO reset-on-progress.** Partial progress is invisible to the platform (the #548/#333 trace-fidelity gap), and resetting on apparent progress would let an intermittently-progressing poison task loop forever. Re-litigating requires new trace-fidelity evidence, not a config change.
  - Per-agent cap override: **deferred** (seam documented in `lease_reaper_service.get_max_redelivery`). Design note: a per-agent cap of 0 is the only *deterministic platform-side* park-first lever for effect-bearing pilot agents — every lease expiry escalates straight to a human (tradeoff: every transient crash becomes an operator interrupt). Record kept so the future pilot decision isn't re-derived.
- **Async human-gate contract (AUTHORED CONTRACT, NOT ENFORCEMENT)**: the platform cannot intercept un-confineable effects by definition; the lever is the contract surfaced to agents in the platform system prompt (`platform_prompt_service.PLATFORM_INSTRUCTIONS` → Operator Communication) and `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`:
  - **Fire-and-park, never block-and-wait**: park the request → end the turn → process `responded` items at the start of a later turn. Task-mode guidance (`_mode_guidance`) carries an explicit carve-out so "execute to completion, don't ask questions" doesn't contradict parking.
  - **Ask before irreversible actions**: payments, messages/emails through the agent's own credentials, public posts, destructive deletions — park an `approval` first when uncertain; do reversible parts first, gate only the irreversible step.
  - **Collision-safe request ids**: derived from the current execution id (`approval-{execution_id}-{slug}`) — execution-id derivation makes re-parks under re-delivery idempotent. Cross-agent uniqueness is now enforced platform-side (#1631, shipped): `operator_queue.id` is a platform-minted uuid and the agent's string moves to a surrogate `request_id` column with `UNIQUE(agent_name, request_id)`, so two agents reusing the same id no longer collide (previously `operator_queue.id` was a global PK with `on_conflict_do_nothing` that silently swallowed the loser).
  - **No-next-turn limitation (documented, not solved)**: responses are written back to the agent's queue file within ~5s but are processed only at a future turn; an agent with no schedule/heartbeat must include resume instructions in the request. The respond→re-trigger dispatch path is a deferred follow-up (#1630). Recommended `expires_at`; an `expired` flip means "not approved — do not proceed".
  - The gate is **honor-system** — a compliance/recovery contract and audit trail, not a security boundary (the agent writes and reads its own queue file). Rails needing a hard guarantee use confined Trinity-owned tools (#1408) or stay human-operated.
- **Still blocking pull default-on for effect-bearing agents** (closing #1402 does NOT green-light it): trace fidelity #548/#333; #1401 `prior_trace` injection (claim-response field reserved, unwired); fail-closed `execution_id` injection (§10.10.1 gate); operator-queue flooding caps (#1632 — the approval channel has no rate limit or size caps).
- **No longer blocking**: pull-path platform-prompt delivery + re-delivery banner — #1629, fixed by #1633 (`services/pull_coordination_service.py` composes the platform prompt on the claim path, fail-open). The issue stays open until the next release cut per the SDLC; the code is on dev, so pull-claimed turns DO receive this contract.
- **Flow**: `docs/memory/feature-flows/operating-room.md` (queue protocol + poison-park); `docs/memory/feature-flows/cleanup-service.md` (reaper scheduling)

### 10.14 Agent Self-Reminders (#1296)
- **Status**: ✅ Implemented — #1296 (P2, theme-devex)
- **GitHub Issue**: #1296 (sub of Epic #1045 "Agent Infrastructure")
- **Description**: While running, an agent schedules a **one-shot, future re-invocation of itself** with a message it picks ("remind me to check this PR in 2h", "follow up tomorrow 9am"). The time-deferred sibling of `run_agent_loop` (§38): a loop runs iterations back-to-back *now*; a reminder fires *once, later*. When the timer fires, Trinity dispatches a **normal execution of that same agent** carrying the reminder message, through the standard `capacity_manager` admit/slot path (shares the agent's `max_parallel_tasks` budget, exactly like loops), with `triggered_by="reminder"`. The agent can **list** pending reminders and **cancel** one before it fires.
- **Storage**: dedicated durable `agent_reminders` table (NOT an overload of the cron `agent_schedules` table, whose every consumer assumes a NOT-NULL cron). Reminder executions still land in `schedule_executions` (via the standard dispatch), so Executions/Overview visibility is preserved regardless of storage. 5-state machine: `pending → firing → fired` (delivered), `firing → pending` (transient-failure release for retry), `firing → failed` (bounded-attempts terminal), `pending → cancelled`.
- **Fire home = the standalone `src/scheduler/` container** (single-instance), NOT the `--workers 2` backend (an in-backend timer would double-fire without a leader lock). One-shot APScheduler `DateTrigger` per pending reminder, armed from the DB (near-clone of the RETRY-001 machinery). Discovery: boot recovery (`initialize()` reconcile after `_recover_pending_retries`) + the 60s sync-loop reconcile (`_reconcile_reminders`, its **own** try/except independent of the cron-sync try) + the full-reload path (`reload_schedules()`).
- **AC #1 (create)**: via an agent-scoped MCP key, schedule a one-shot self-reminder with **either** `fire_at` (absolute ISO) **XOR** `delay_seconds` (relative) + a message.
- **AC #2 (fire)**: dispatch a normal execution of the same agent through `capacity_manager` admit/slot (no parallel dispatch path); `triggered_by="reminder"` reflected on the execution row, in the Overview "Reminders" bucket, the fleet Executions `?triggered_by=` filter, and the FAILED-alert classification.
- **AC #3 (durable)**: survives a backend/scheduler restart and still fires (or is deterministically reconciled when past-due). Delivery is **at-least-once, bounded, observable**: the `firing` intermediate makes the fire atomic (single-fire) while letting a fire that did NOT land (backend warmup 503, connection failure, crash mid-fire) be retried by the reconcile, bounded at `MAX_REMINDER_FIRE_ATTEMPTS` (default 3) → terminal `failed`. A dispatch TimeoutException is treated as **outcome-unknown → assume-dispatched** (`firing→fired`, execution row NOT force-FAILED — let the poll finalize) to avoid a double-execution on the common "backend slow, task ran" case; only a clean pre-start failure (non-200 / connection error) marks the attempt FAILED (status-guarded) and retries.
- **AC #4 (list/cancel)**: `GET .../reminders` (default `?status=pending`) and `POST .../reminders/{id}/cancel` (CAS `pending → cancelled`, tenant-scoped by `agent_name`; already-cancelled → 200 no-op; fired → 409; foreign id → uniform 404).
- **AC #5 (self-only auth)**: an agent-scoped key may set/list/cancel reminders **only for itself** — the exact reports self-gate (`current_user.agent_name == name`) on top of `AuthorizedAgent`; a sibling agent the owner shares → 403. Connector-scoped callers are rejected (`_reject_connector_principal` + off the connector allowlist — reminders schedule future budget-consuming executions, which a consumption-only connector key must not). Ephemeral/ghost keys are 403'd (reminders deliberately OUT of the #69 ghost-key fence — a pending reminder can outlive a discarded ghost).
- **AC #6 (idempotency)**: create accepts `Idempotency-Key` (Invariant #18); absent → the backend auto-derives a key over the **raw input** (`agent_name`, `message`, and the literal `delay_seconds=N`/`fire_at=<string>` spec, NOT the resolved instant — else a `delay_seconds` retry resolves a different instant each call and defeats dedup). Terminal (`cancelled`/`fired`) stored rows are **excluded from replay** so a cancel-then-recreate-identical yields a fresh pending reminder. The fire boundary is idempotency-keyed for free via `sched:{execution_id}`.
- **AC #7 (abuse bounds)**, env-tunable, enforced at create: `MAX_PENDING_REMINDERS_PER_AGENT` (default 25 → 429 on a real pending count), a **durable** `MAX_REMINDERS_PER_AGENT_PER_DAY` (default 100 → 429, the non-fail-open backstop against self-perpetuation since a reminder can itself call `set_reminder`), `REMINDER_MIN_DELAY_SECONDS` (default 60, ≥ the 60s reload interval so a reminder can be armed before it is due), `REMINDER_MAX_DELAY_SECONDS` (default 30 days, < the 180-day soft-delete name reservation), `REMINDER_MESSAGE_MAX_CHARS` (default 4000), a per-agent create rate-limit (`agent_reminder:{name}`, fail-open flood guard), and a **timeout clamp to the agent cap** (the #929 primitive → 400 if above, so a reminder can't hold a slot past the operator-set `execution_timeout_seconds`).
- **Autonomy hold**: `get_active_reminders` filters `autonomy_enabled=1` AND `deleted_at IS NULL` — disabling an agent's autonomy holds its pending reminders (they resume, past-due-fire, when re-enabled); a soft-deleted/renamed agent's reminders follow via the `AGENT_REFS` cascade (CASCADE policy → wiped on purge, re-keyed on rename).
- **Retention**: `agent_reminders_retention_days` (default 90, `0` = off) — the cleanup sweep DELETEs terminal (`fired`/`cancelled`/`failed`) rows past the window (`pending`/`firing` never deleted), chunked, gated through the #1644 blast-radius guard; registered in `RETENTION_OPS_KEYS`, surfaced read-only at `GET /api/settings/retention`. #1638 floor discipline: the default is the wide/safe value.
- **Flow**: `docs/memory/feature-flows/agent-self-reminders.md`

### 10.15 CAS-Won Terminal Owns the Activity Close (#1804)
- **Status**: ✅ Implemented (2026-07-28, Issue #1804)
- **GitHub Issue**: #1804 (sub of Epic #1259); prior single-producer patches #45, #767, #1332
- **Description**: Closing an execution's paired `agent_activities` dispatch row is part of the
  **terminal-write contract**, not a property of whoever happens to hold the `activity_id` local.
  Before this, only the dispatching coroutine closed the activity, and only when it won the CAS —
  so every recovery writer (watchdog, startup recovery, both bulk sweeps, both backend-shutdown
  `CancelledError` handlers, the lease reaper, the pull sink) wrote the execution terminal and
  walked away. The row stayed `activity_state='started'` (Timeline rendered the agent as still
  working) until the generic 120-minute backstop closed it with a fabricated
  `duration_ms = now − started_at` — a 15-minute run permanently recorded as a ~120-minute failure.
- **The contract**: *every writer that wins a terminal CAS on `schedule_executions` closes the
  paired dispatch activity.* The 120-minute `mark_stale_activities_failed` sweep is a **backstop
  for the unclaimed**, not the primary closer (and #429 deletes it — a per-site patch would leave
  no owner).
- **One owner**: `activity_service.close_execution_activity(execution_id, terminal_status, …)`
  (+ the sync `spawn_close_execution_activity` wrapper for the synchronous pull sink), mirroring
  `event_dispatch_service.spawn_task_terminal_event` (#1578). It maps the terminal via the shared
  `models.activity_state_for_terminal` (#1332 — never a second mapping), delegates to
  `activity_service.complete_activity` so the `agent_activity` WebSocket broadcast survives, and is
  **fail-open**: it can never affect an already-committed terminal write.
- **The close is itself a CAS** (`db.complete_activity` → `ActivityCloseOutcome`
  `UPDATED | ALREADY_CLOSED | NOT_FOUND`). Making a second closer *safe* is what makes "the CAS
  winner owns it" true — you cannot guarantee only one writer tries. The activity predicate
  **mirrors** the execution predicate (`db/schedules/executions.py`) rather than inventing a
  stricter one:
  - incoming COMPLETED (from SUCCESS) → `activity_state IN ('started','failed')` — an authoritative close
    MAY upgrade a provisional FAILED (the #1083 late-SUCCESS-after-lease-expiry path);
  - incoming CANCELLED / FAILED → `activity_state = 'started'` — nothing overwrites an
    authoritative close, so a double close never clobbers `completed_at`/`duration_ms`/`error`.
  The **lookup** is widened to agree with the write: an authoritative terminal searches
  `started|failed`, a provisional terminal searches `started` only. A lookup narrower than the CAS
  makes the whole fix inert.
- **Split by cardinality**: single-row recovery paths use the per-row helper (WS broadcast
  preserved); the two bulk sweeps use `db.close_open_activities_for_executions` — one transaction,
  no per-row WebSocket, driven by the `collect_failed` rows #1714 already collects (no new query).
  A WS herd during post-outage recovery is exactly what #1085 exists to prevent.
- **Observability**: `CleanupReport.activities_closed_on_recovery`. Post-merge signal —
  `stale_activities` trends to ~0 while `activities_closed_on_recovery` picks up the volume;
  a non-zero `stale_activities` means a producer is still unowned.
- **Guard**: `tests/unit/test_1804_terminal_activity_parity.py` is anchored on **terminal writes**
  (`update_execution_status` / `mark_execution_failed_by_watchdog` / `fail_stale_slot_execution`),
  not on completion-event emission — the emit set is a strict subset (both shutdown handlers write
  a terminal and emit nothing), which is how those two writers hid from review.
- **Flow**: `docs/memory/feature-flows/activity-stream.md`,
  `docs/memory/feature-flows/task-execution-service.md`

### 10.16 Template-Declared Schedules at Creation (trinity-enterprise#89)
- **Status**: ✅ Implemented (2026-08-02, trinity-enterprise#89, sub of Epic #122)
- **GitHub Issue**: trinity-enterprise#89 (code lands as a public `abilityai/trinity` PR)
- **Description**: An agent template's `template.yaml` may declare a `schedules:` block. The
  abilities plugin ecosystem (`create-agent` wizards, `agent-dev:add-pipeline`, `trinity:onboard`)
  treats that block as the **design source of truth** for an agent's recurring tasks, but Trinity
  never read it — only `/trinity:sync` reconciled it onto a live instance. An agent created from the
  same template through the UI, the API, or MCP got **none** of its declared schedules. Creation now
  materializes them through the existing `db.create_schedule` path, for **both** `github:` and
  `local:` templates.

#### The declared block
```yaml
schedules:
  - name: daily-briefing          # required, non-empty string, ≤ 200 chars, unique in the block
    cron: "0 9 * * *"             # required, non-empty string, strict 5-field Unix cron
    message: /daily-briefing       # required, non-empty string, ≤ 10 000 chars (truncated beyond)
    enabled: true                  # optional bool; anything else → False + a named error
    timezone: Europe/London        # optional string, IANA zone (see below); default "UTC"
    description: ...               # optional string, ≤ 1000 chars (`purpose:` is an accepted alias)
    id: sched-1                    # IGNORED — Trinity mints its own schedule id
```
- Required-key shape mirrors `system_service.validate_manifest` (`name`/`cron`/`message`).
- **Unknown keys are ignored** (AC #1) — a plugin may carry extra design metadata.
- `timeout_seconds`, `model`, and `allowed_tools` are deliberately **not surfaced**.
  `ScheduleCreate.timeout_seconds` defaults to `None` = "inherit the agent's
  `execution_timeout_seconds`" (#913), so every materialized schedule sits inside the §35 agent-cap
  ceiling **by construction** and can never trip `schedule_timeout_exceeds_agent_cap`.

#### Reader — `services/template_schedules.py` (new leaf)
- Public surface: `schedule_shape_errors(block) -> List[str]` and
  `normalize_declared_schedules(block) -> List[dict]`, both over one private `_parse()` so the
  error list and the accepted list can never disagree. Mirrors the sibling
  `credential_shape_errors` / `credential_mcp_server_names` convention (trinity-enterprise#128).
- **Total function contract — `template.yaml` is untrusted input and the reader never raises.**
  `yaml.safe_load(...) or {}` can yield a scalar, a list, or a mapping at any level:

  | Input | Behaviour |
  |---|---|
  | absent / `None` | `[]`, no error (a commented-out block is not a problem) |
  | `schedules: "yes"` / `5` / `true` / `{a: b}` | `[]` + named error |
  | `[null]`, non-mapping entry | entry dropped + named error |
  | missing / non-string / empty `name`, `cron`, `message` | entry dropped + named error |
  | non-string `timezone` | entry dropped + named error |
  | invalid cron (`@daily`, 6-field, `99 99 * * *`) or unknown timezone | entry dropped + named error |
  | timezone pytz knows but the runtime's IANA database cannot resolve (#1823) | entry dropped + named error naming the missing tz data — **never** a 500 |
  | `name` > 200 / `description` > 1000 | entry dropped + named error |
  | `message` > 10 000 | truncated + named error |
  | duplicate `name` within the block | second and later dropped + named error |
  | non-bool `enabled` | entry kept with `enabled=False` + named error (fail-safe) |
  | more than `MAX_DECLARED_SCHEDULES` (20) entries | truncated to 20 + named error |
- **Error-string discipline**: an error carries the **entry index, the key, and a YAML type name**
  — never the `name` or `message` *value*. Those strings are author-controlled, unbounded, and
  prompt-injection-shaped, and the error list is persisted into the compatibility report
  (`agent_compatibility_results.checks_json`), rendered in the UI, and echoed into the catalog
  response. The **cron** string is the one echoed value — bounded and printable-filtered by a local
  4-line helper (the leaf cannot import `template_service._sanitize_for_warning` without closing an
  import cycle; the two are comment-linked).
- **Strict cron at the reader** (`services/schedule_validation.validate_cron_expression`, the same
  validator the dedicated scheduler registers with, #1472). This is a **materialization** gate, not
  a report verdict: `_calculate_next_run_at` *swallows* a bad cron and returns `None`
  (`db/schedules/crud.py`), and `set_schedule_enabled` never re-validates — so an unvalidated entry
  would create a **zombie schedule**: a row that exists, shows no next run, and never fires.
- **A valid `timezone` is one BOTH resolvers accept (#1823)** — not "a pytz zone", which is what this
  doc said and what #1472 implemented. The scheduler's chain is
  `pytz.timezone(name)` → `CronTrigger(timezone=…)` → APScheduler `astimezone()` →
  `zoneinfo.ZoneInfo(name)`. pytz bundles its own complete database and accepts every IANA
  *backward-compatibility alias* (`Europe/Kiev`) everywhere; `zoneinfo` reads the system database
  plus the optional `tzdata` wheel, so it is the strictly narrower — and actually binding —
  constraint. `validate_timezone` probes both, and `validate_cron_expression` **delegates** to it, so
  the create route (which calls only the latter) and the update route (which calls the former)
  cannot enforce different contracts. Aliases are **supported, not normalized**: the shipped images
  install `tzdata-legacy` + the `tzdata` wheel, guarded by
  `tests/unit/test_1823_tz_capability_parity.py`. An unresolvable zone is a named 400 at the API and
  a **permanent** (bounded, non-retrying) `_add_job` failure in the scheduler — never a 500, and
  never the 60s retry storm that froze `next_run_at`.
- `MAX_DECLARED_SCHEDULES = 20` lives in the reader, so the catalog surface and the materializer
  inherit the same cap. (Verified: the platform has no other per-agent schedule cap.)

#### Catalog surface (AC #1)
- Both builders surface `schedules` (the **normalized** list, not the raw block) and
  `schedule_errors`: `template_service._build_template` (GitHub) and `_build_local_template`
  (local). Parity is explicit — the pre-existing asymmetry (`persistent_state` is surfaced only by
  the GitHub builder) means it cannot be assumed.
- `_template_schedules()` logs exactly one WARNING per malformed template naming the id; the
  template still lists. A broken `schedules:` block costs that template its schedule metadata, not
  its place in the catalog (the trinity-enterprise#128 / #1835 contract).
- **Both GitHub catalog list paths are now fenced** per-template (`get_all_templates`), matching the
  local path's fence. They were bare list comprehensions, so any raise inside `_build_template`
  would 500 the whole GitHub half of `GET /api/templates`.

#### Materialization at creation (AC #2)
- `_TemplateResolution.declared_schedules` is the **normalized carrier populated by both resolver
  branches**. It is deliberately *not* `template_data`: that field is raw template YAML on the
  `local:` path and `{}` on the `github:` path, and `_stage_config_files` gates credential-file
  generation on `if template_data:` — merging the two shapes would change credential generation for
  every GitHub agent.
- The `github:` branch fetches the raw `template.yaml` via
  `template_service.fetch_template_metadata_for_create(repo, pat=…, ref=…)` — the
  **creation-resolved PAT** (per-agent → per-user → global, ent#162) and the **parsed `@branch`
  ref**, bypassing the 10-minute catalog cache. The catalog's own metadata fetch uses the *global*
  platform PAT with no `?ref=`, so a user creating from their own private repo with their own
  per-user token would silently get zero schedules, and `github:owner/repo@feature` would
  materialize `main`'s. The fetch is **non-fatal and never silently empty**: any failure logs a
  WARNING naming the repo, the status, and the likely cause.
- `crud.reconcile_declared_schedules(agent_name, declared, owner_username)` runs inside
  `_materialize_agent_files`, beside the `persistent_state` (#383) and `data_paths` (#1169) steps.
  **Every step in that function is non-fatal by design — creation must never fail because a
  schedule didn't materialize.** The helper is shaped as a *reconcile* primitive (takes
  `agent_name`, not `AgentConfig`) so a future operator-triggered "re-apply template" can reuse it.
- Ghost agents are skipped at the caller (`config.ephemeral`) — schedules on an ephemeral agent are
  a 400 by ent#69 fleet hygiene, and a new caller must exclude ghosts itself.
- `db.create_schedule` **returns `None`** (it does not raise) on three paths — unknown user, no
  agent access, and the #1445 `is_agent_live` no-orphan gate. The materializer checks the return
  value and counts a falsy result as *failed*, mirroring `system_service`. The summary INFO line
  reports created / skipped-existing / skipped-invalid / failed derived from **actual outcomes**,
  never from the input length.
- Ordering is already correct: `_register_agent` runs before `_materialize_agent_files`, so
  `is_agent_live()` is satisfied.

#### `enabled:` semantics (AC #3)
- A declared `enabled: true` **is honored**; an unspecified `enabled` defaults to **`False`**.
  Passed explicitly to `ScheduleCreate`, whose own default is `True`.
- Safety rests on the platform master gate, not on the flag: `agent_ownership.autonomy_enabled` is
  `INTEGER DEFAULT 0` and `register_agent_owner()` has no parameter for it, so a newly created agent
  can never have autonomy on; a cron fire against an autonomy-off agent is skipped with no execution
  row. Honoring also gives the UI a real "Next run" — `db.create_schedule` computes `next_run_at`
  only when `enabled` is true.
- **Documented caveat (pre-existing, and the actual control):** `set_autonomy_status` force-enables
  **every** schedule on the agent when an owner turns autonomy on, with no filter — so per-schedule
  `enabled` intent is erased at the first toggle whether the row was written `0` or `1`. Forcing
  `False` at creation would therefore prevent nothing. Making `set_autonomy_status` stop clobbering
  per-schedule intent is the real guard and is tracked as **#1945** (P2, `type-bug`).

#### Idempotency (AC #4) — three places, all required
1. **At creation** — name-match read-then-skip against `db.list_agent_schedules(agent_name)` before
   each insert. No recreate hook is added: container recreate goes through `lifecycle.py`, and an
   eager re-materialize would **resurrect schedules an operator deliberately deleted**.
2. **Within the declared block** — `normalize_declared_schedules` dedupes by name (first wins,
   named error), and the materializer adds each created name to the seen-set as it goes.
3. **Manifest deploy** — `system_service.create_schedules` runs *after* `create_agent_internal`, so
   post-#89 it is the second schedule producer for the same agent. It gains the same name-match
   skip; otherwise a manifest declaring `daily-briefing` on a template that also declares it yields
   two rows.
- There is **no** `UNIQUE(agent_name, name)` index on `agent_schedules` and none is added: it is a
  schema change (Invariant #3, dual-track) and would fail on installs already holding
  duplicate-named schedules. Known blind spot: `list_agent_schedules` excludes soft-deleted rows, so
  a soft-deleted schedule of the same name does not suppress a re-create.

#### Compatibility validation (AC #5)
- **T-018** (`soft`, `static`, category `T`, not `claude_only`): *"schedules block entries are
  well-formed"* — **structural** validity only (presence/type of `name`/`cron`/`message`, entry
  shape, block shape, cap), via `template_schedules.schedule_shape_errors`. It deliberately does
  **not** report cron syntax: **A-002** already ships as *"cron expressions are valid"*, and two
  contradicting cron authorities in the same report on the same field is worse than either alone.
- **A-002 is corrected in the same change.** Its private `_valid_cron` was a per-field
  `^[\d*/,\-]+$` regex — it **rejected** `0 9 * * MON` (valid) and **accepted** `99 99 * * *`
  (invalid). It now delegates to `validate_cron_expression`, so the report agrees with both the
  scheduler and the materializer. *Validate a config with the same parser the executor uses.*
- **T-018 fails closed.** `run_static` converts a raising check into `skipped`, and `_counts` counts
  only `status == "fail"`, so a raising *soft* check drops `soft_count` 1→0 and flips
  `overall_status` from `issues` to `compatible` — a validator that breaks reports "healthy", and
  `_report_from_persisted` replays that clean bill of health from `checks_json` on every
  stopped-agent read. T-018 therefore catches its own `Exception` and returns `_fail`, carrying
  `type(e).__name__` **only** (never `str(e)`, which would embed untrusted template content into a
  persisted, UI-rendered blob).
- Two in-radius corrections ship with it: **`c_p006`** (a **HARD** check) iterated
  `data.get("schedules") or []` with no `isinstance(..., list)` guard, unlike all four of its
  siblings — `schedules: 5` raised `TypeError` and made a HARD check silently vanish from
  `hard_count`; and **`run_static` now logs** the swallow (it previously recorded `check_error` with
  no log line anywhere, for all ~100 checks).

#### Blast radius and non-goals
- **Zero on the default install.** Of the bundled templates only `demo-analyst` and
  `demo-researcher` declare `schedules:` and both are `hidden: true`; `local:scout/sage/scribe` (the
  ent#124 default manifest fleet) declare none. Zero declared schedules ⇒ zero behaviour change.
  No migration, no feature flag, no new endpoint, no new Pydantic model.
- **Invariant removed, named deliberately:** `config/manifests/default-system.yaml` records as an
  ent#124 decision *"No `schedules:` — a zero-credential fresh install must not accumulate failing
  cron executions."* #89 transfers that control from the manifest author to the template author with
  no opt-out (a manifest can only add schedules, never suppress a template's).
- **Amplification to note, not gate:** a schedule `message` carries the same trust level as that
  template's `CLAUDE.md` and skills — no new trust boundary. But an agent-scoped MCP key holding
  `create_agent` can now spawn an agent from an arbitrary `github:` template *and* mint recurring
  autonomous tasks on it. Bounded by `autonomy_enabled=0`, `MAX_DECLARED_SCHEDULES`, the ghost skip,
  and ent#69's ephemeral-caller refusal.
- **Out of scope**: no `deploy_system` fallback to a template's declared block when the manifest
  omits one; no recreate/repull hook; no `timeout_seconds`/`model`/`allowed_tools` surface.
- **Flow**: `docs/memory/feature-flows/template-processing.md`,
  `docs/memory/feature-flows/scheduling.md`,
  `docs/memory/feature-flows/agent-compatibility-validation.md`

### 10.17 Client-Side Cron Validation (#925)
- **Status**: ✅ Implemented (2026-08-13)
- **GitHub Issue**: #925
- **Description**: The schedule form validated cron expressions only at save time — the backend's
  400 was the first feedback — and a stored-invalid row (legacy croniter-era data, or rows created
  before a validator tightening) sat in the list indistinguishable from a healthy one. The frontend
  now pre-validates as the user types (design-system p17) and marks invalid stored rows.
- **Requirement — mirror contract, not a second grammar**: the client validator
  (`src/frontend/src/utils/cronValidation.js`, pure leaf, **zero npm deps**) is a hand-rolled
  mirror of the backend's exact acceptance grammar —
  `services/schedule_validation.py::validate_cron_expression` = strict 5-field split +
  `_dow_to_apscheduler` translation (ported verbatim, branch order included) + APScheduler 3.11
  field rules (`AllExpression`/`RangeExpression`/name-range prefix expressions, step-span rule,
  Python-truthiness `last or MAX` fallback — deliberately `||` not `??` in JS). Cron libs
  (`cron-parser`, `cronstrue`) were rejected: they implement a foreign grammar with ≥8 proven
  verdict disagreements (`@daily`, 6-field, dow `0-6`, `last`, `7/2`, `L`/`#`, step-span,
  `5/2`). This is the learnings.md #1472 lesson ("validate with the parser that registers")
  applied one level up: the client validates with a pinned mirror of that parser.
- **Drift alarm — shared fixture asserted by both suites**: the grammar contract is
  `tests/fixtures/cron-grammar-cases.json` (~110 rows, **generated from a probe against the live
  backend validator, never hand-typed**). `tests/unit/test_925_cron_grammar_fixture.py` re-proves
  every row against `validate_cron_expression` in the backend CI env (an APScheduler bump that
  changes the grammar fails there loudly); `src/frontend/tests/unit/cronValidation.spec.js`
  asserts the client mirror agrees row-for-row. Quirk rows (prefix-matched names `MON/999` /
  `jan/0` / `lastx`, comma-translation asymmetry `0-6,1` vs `0-6`, falsy-zero `0-0/2`) are
  pinned and must NOT be "fixed" — parity outranks tidiness.
- **Fail-open posture**: the validator is total (`String(expr ?? '')`) and a top-level catch
  returns `{valid: true}` + `console.error` — an internal port bug must not brick the panel or
  block saves. The backend 400 remains the enforcement authority; client validation is UX only.
- **Form gating**: inline error in a reserved-footprint slot (the format-hint line doubles as the
  error slot — no modal jump, p4/p6); submit disabled **only when non-empty AND invalid** — an
  empty cron keeps the native `required` bubble path; while **editing**, an invalid (incl.
  empty/whitespace) stored cron shows its error unconditionally so the disabled Update button is
  never unexplained; while creating, errors appear only after first blur (no mid-typing flash).
- **List surface**: each stored row with an invalid `cron_expression` renders a warning triangle
  inside its cron chip, tooltip + aria-label exactly `Invalid cron expression` (shape + hue,
  p24). Presets are sourced from the exported `CRON_PRESETS` so "presets never warn" is tested
  against the shipped list.
- **Documented divergence (client-stricter only)**: the client accepts ASCII digits only where
  Python's `int()`/`\d` also accept Unicode digits / `+` signs / underscores — rejecting e.g.
  `٥ * * * *` that the server accepts. Direction is client-stricter on absurd input; the
  damaging direction (client-invalid/server-valid false warnings on real input) is guarded by
  the quirk rows. The fixture marks such rows `divergence: "client-stricter"`.
- **Out of scope**: no backend runtime change (400 contract byte-preserved); no server-side
  migration/repair of stored invalid rows; no client timezone validation (the form select is a
  fixed known-good list; cron field validity is timezone-independent).
- **Flow**: `docs/memory/feature-flows/scheduling.md` (§ Client-side cron validation)

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

### 38.5 Configurable Loop Failure Policy (#1167)

**Description**: A per-loop policy controls what happens when an iteration
fails. Default is fail-fast (backward compatible); `continue` mode tolerates a
failed iteration and proceeds, bounded so a fully-broken agent still terminates.

- **FR-1 — `on_failure`**: `abort` (default — first failed iteration ends the
  loop as `failed`/`stop_reason=error`, current behavior) or `continue`.
- **FR-2 — `max_consecutive_failures`** (default 3, range 1–100): in `continue`
  mode the loop aborts as `failed` with `stop_reason=max_consecutive_failures`
  once this many iterations fail in a row; a success resets the streak.
- **FR-3 — Both failure surfaces** honored: a raised exception from
  `execute_task` AND a non-success `TaskExecutionResult` (TIMEOUT / AGENT_ERROR
  / CIRCUIT_OPEN / AUTH). Each failed iteration finalizes its `agent_loop_runs`
  row as `failed`, then (continue mode) the loop proceeds to the next run.
- **FR-4 — Terminal status**: a continue-mode loop that reaches `max_runs` (or
  matches its stop-signal) with ≥1 tolerated failure finalizes as
  `completed_with_errors`; the `failed_runs` count is surfaced on the loop row
  and API/UI.
- **FR-5 — `{{previous_response}}`**: carries the last *successful* response — a
  failed iteration does not overwrite it.
- **FR-6 — Plumbed through all surfaces** (Invariant #13): `agent_loops` schema
  + migration, `POST /api/agents/{name}/loops`, MCP `run_agent_loop`, and the
  Loops panel UI. Unset = `abort`, a strict no-op for existing callers.

---

### 38.6 Per-run timeout is bounded by the agent's own ceiling (ent#338)
- **Status**: ✅ Implemented
- **Implements**: trinity-enterprise#338
- **Description**: `agent_ownership.execution_timeout_seconds` is the per-agent
  ceiling, and **nothing downstream re-applied it** for loops:
  `task_execution_service` reads the cap only when the caller passed no
  `timeout_seconds`, so an explicit `timeout_per_run` went straight to dispatch.
  A loop could therefore run iterations *longer* than the ceiling its owner set —
  a bypass rather than a number displayed wrongly, and one that multiplies,
  since `max_runs` reaches 100.
- **Behaviour**: `POST /api/agents/{name}/loops` refuses `timeout_per_run > cap`
  with **400** and a structured `detail`
  (`error: "loop_timeout_exceeds_agent_cap"`, `agent_cap_seconds`,
  `requested_seconds`) so a UI can show the real bound instead of guessing.
  Equal to the cap is allowed.
- **Refuse, not clamp** — mirroring #929 for schedules, the closest sibling
  (explicit, human-set config). ent#458 puts these guardrails on screen *before*
  Start, so a silent clamp would begin a loop whose bounds differ from the ones
  the user was shown. Reminders (§10.14, #1296) clamp instead, deliberately: there
  the timeout comes from an agent's own mid-turn request with no human reading a
  form.
- **Ordering**: the cap check runs **before** #1156's deadline comparison, so
  that comparison can never quote a per-run timeout the caller is not allowed to
  have. Pinned in `tests/unit/test_ent338_loop_timeout_cap.py`.
- **Fail-open on an unreadable cap**: a settings-read failure skips the check
  rather than blocking the loop. This is a resource ceiling, not a security
  gate, and the pre-ent#338 behaviour was no check at all — so degrading to that
  on a DB blip costs nothing that was not already true, while failing closed
  would take loops down whenever the settings read did.

### 38.7 Loops in the Workspace — run and watch from chat (ent#458)
- **Status**: ✅ Implemented (AC #3 deferred — see below)
- **Implements**: trinity-enterprise#458
- **Description**: A workspace user starts, watches and stops loops from the
  conversation they belong to. A collapsed strip above the composer stays quiet
  until an agent in this chat is looping, then shows each participating agent's
  active loops, how much of each guardrail is left, and a Stop that is always
  available.
- **No new backend surface.** ent#458 scopes this to the **platform-authenticated
  door** (ent#78's auth-path invariant), so the panel calls the existing operator
  loop endpoints with the operator's own JWT. An external client holding a portal
  token never mounts it — hidden, not disabled, because a disabled control
  advertises a capability that credential can never satisfy.
- **Guardrails are visible before Start** (AC #1): the form shows the run limit
  and cost budget as fields, and states the self-stopping defaults it does not
  ask about (`no_progress_threshold`, `max_consecutive_failures`). Those values
  mirror `models.StartLoopRequest` and are **pinned against it** by
  `portalLoops.spec.js`, since they are cross-language and cannot be imported.
  `max_runs` is deliberately excluded from that mirror: it is REQUIRED on the
  server, so the form's `10` is a suggestion, and pinning it as a "default"
  would enshrine a fiction.
- **Honest terminal words** (AC #3's vocabulary, applied to the live panel):
  `stop_reason` is never flattened to "Stopped" — cost budget, time limit,
  no-progress, agent-reported-done, user-stopped and max-runs-reached are six
  different situations with six different next actions, and `max_runs_reached`
  reads as **Done** rather than "Stopped" on both the completed and stopped rows.
- **Headroom reports `null` for a guardrail that was never set**, never 0% or
  100%: "no budget" and "budget untouched" are different facts and a bar drawn
  at either extreme asserts the wrong one. An overshoot clamps to the end of the
  track, because the runtime lets the current run finish and cost can legitimately
  exceed its budget (§38.3).
- **Live, degrading to poll** (AC #4): loop events are already broadcast
  fleet-wide and the Workspace runs inside the same app shell, so the existing
  global WebSocket carries them — the one handler now routes each event to
  **two** stores, the operator one filtered to the agent on Agent Detail and the
  workspace one filtered to the chat's participants (the `reportsStore` +
  `fleetReportsStore` shape, #918). A 12s backstop poll runs **only while
  something is active**, so an idle tab issues no traffic, and an unknown status
  is treated as NOT active so the panel can never sit claiming work is in flight
  forever.
- **Partial failure keeps the data**: one unreachable agent in a room degrades to
  "this list may be incomplete" rather than blanking the panel (#2382's rule).
- **State**: `stores/portalLoops.js`, participant-scoped — deliberately a second
  store rather than a reuse of the agent-at-a-time `stores/loops.js`, which the
  operator panel owns; a shared singleton would have each surface clearing the
  other's list on navigation (the `skillsLibrary` vs `skills` split, ent#263).
- **Deferred**: AC #3 (loop history in the Activity tab) waits on ent#457, which
  builds that tab. Stated rather than silently dropped: this issue explicitly
  says history "lives in #457's Activity tab" and must not grow a parallel
  surface, so the honest sequencing is to render there once it exists.

