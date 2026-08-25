# Requirements — Security & Compliance, Operator Queue, Guardrails

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 20. Security & Compliance

### 20.1 Audit Trail System (SEC-001)
- **Status**: ✅ Complete (Phases 1, 2a, 2b, 3, 4, 5 shipped via #20 / PR #371, 2026-04-17).
- **Requirement ID**: SEC-001
- **Priority**: HIGH
- **Description**: Comprehensive audit logging for all user and agent actions with full actor attribution. Enables investigation, compliance reporting, and accountability.
- **Key Features**:
  - Append-only `audit_log` table with immutability triggers (UPDATE blocked unconditionally; DELETE blocked within 365-day retention)
  - Full actor attribution (user, agent, MCP client, system)
  - MCP API key tracking per tool call (all 71 tools wrapped transparently)
  - Hash chain (SHA-256) for tamper evidence with verify endpoint
  - Query API with filters, pagination, stats aggregation, and JSON/CSV export
  - Distinct from Process Engine audit (`audit_entries`) — coexist intentionally
- **Phase 1 Delivery**:
  - `audit_log` table + indexes + immutability triggers (`db/schema.py`, migration #31)
  - `PlatformAuditOperations` (`db/audit.py`)
  - `PlatformAuditService` with global instance (`services/platform_audit_service.py`)
  - Admin query API: `GET /api/audit-log`, `GET /api/audit-log/stats`, `GET /api/audit-log/{event_id}`
  - 29 unit tests (schema, query, filters, pagination, immutability, service actor resolution, error handling)
- **Phase 2a Delivery (agent lifecycle smoke test)**:
  - `routers/agents.py` emits audit rows after successful create / start / stop / delete
  - 5 integration-shape tests asserting the exact field layout produced by the handlers
- **Phase 2b Delivery**:
  - `auth.py` — login_success / login_failed (admin + email)
  - `sharing.py` — share / unshare
  - `credentials.py` — inject / export / import (CRED-002 file-injection ops)
  - `settings.py` — settings_change
  - `agent_rename.py` — rename
  - Request-ID correlation middleware (`X-Request-ID` header, UUID, passthrough)
- **Phase 3 Delivery (MCP tool call audit)**:
  - `src/mcp-server/src/audit.ts` — `withAudit` transparent wrapper
  - All 71 tools auto-wrapped at registration time in `server.ts`
  - Fire-and-forget POST to `/api/internal/audit` (shared-secret auth via `INTERNAL_API_SECRET`)
  - Captures tool name, auth context (user/agent/system scope), duration, success/failure with error message
- **Phase 4 Delivery (hash chain + export)**:
  - `POST /api/audit-log/hash-chain/enable?enabled=true|false` — runtime toggle
  - `POST /api/audit-log/verify?start_id=&end_id=` — chain integrity check
  - `GET /api/audit-log/export?format=json|csv` — compliance export
  - `_compute_hash` normalizes `details` field across write/read paths for stable SHA-256
- **Phase 5 Delivery (action coverage gaps)**:
  - `execution`: chat_started (`chat.py`), task_triggered (`schedules.py`), schedule_triggered (`internal.py`)
  - `authorization`: permission_grant / permission_revoke / permissions_set (`agent_files.py`)
  - `configuration`: autonomy_toggle / resource_limits (`agent_config.py`)
  - `mcp_operation`: key_create / key_revoke / key_delete (`mcp_keys.py`)
  - `git_operation`: sync / pull / init (`git.py`)
  - `system`: startup / shutdown (`main.py` lifespan), emergency_stop (`ops.py`)
  - `credentials`: oauth_complete (`slack.py` OAuth callback)
- **Event Categories** (actions tracked):
  - `AGENT_LIFECYCLE`: create, start, stop, delete, rename (recreate — no endpoint)
  - `EXECUTION`: chat_started, task_triggered, schedule_triggered
  - `AUTHENTICATION`: login_success, login_failed (logout / token_refresh — no endpoints in Trinity)
  - `AUTHORIZATION`: share, unshare, permission_grant, permission_revoke, permissions_set
  - `CONFIGURATION`: settings_change, resource_limits, autonomy_toggle
  - `CREDENTIALS`: inject, export, import, oauth_complete (CRED-002 replaced spec's create/delete/reload)
  - `MCP_OPERATION`: tool_call, key_create, key_revoke, key_delete
  - `GIT_OPERATION`: sync, pull, init (commit — folded into sync)
  - `SYSTEM`: startup, shutdown, emergency_stop
- **Architecture**: `docs/requirements/AUDIT_TRAIL_ARCHITECTURE.md`
- **Flow**: `docs/memory/feature-flows/audit-trail.md`
- **Test plan**: `docs/testing/audit-trail-manual-test-plan.md` (19 acceptance checks; 18/19 passed live, hash-chain verify bug fixed in-flight and re-verified)
- **Follow-up (optional)**: admin UI (no requirement in spec — API export satisfies compliance criterion); forward `schedule_id` / `schedule_name` from scheduler to `/api/internal/execute-task` so `schedule_triggered` audit carries that context.

### 20.2 Execution Origin Tracking (AUDIT-001)
- **Status**: ⏳ Pending Implementation
- **Requirement ID**: AUDIT-001
- **Priority**: HIGH
- **Description**: Track WHO triggered each execution with full actor attribution. Captures user identity, MCP API key info, and source agent for agent-to-agent calls.
- **Key Features**:
  - Extended `schedule_executions` schema with origin columns
  - User ID and email captured for manual and MCP triggers
  - MCP API key ID and name tracked for external calls
  - Source agent name tracked for agent-to-agent collaboration
  - UI display of origin info on Execution Detail page
  - Filter executions by trigger type (manual/schedule/mcp/agent)
- **New Database Columns**:
  - `source_user_id` (INTEGER) - FK to users table
  - `source_user_email` (TEXT) - Denormalized for queries
  - `source_agent_name` (TEXT) - Calling agent for agent-to-agent
  - `source_mcp_key_id` (TEXT) - MCP API key ID used
  - `source_mcp_key_name` (TEXT) - MCP API key name
- **Spec**: `docs/requirements/EXECUTION_ORIGIN_TRACKING.md`
- **Implementation Phases**:
  1. Database migration and backend CRUD updates
  2. MCP server header integration
  3. Frontend display and filtering

### 20.3 Subscription Management (SUB-002 — replaces SUB-001)
- **Status**: ✅ Implemented (2026-03-03)
- **Requirement ID**: SUB-002
- **Priority**: HIGH
- **Replaces**: SUB-001 (`.credentials.json` injection — removed)
- **Description**: Centralized management of Claude Max/Pro subscription tokens. Register long-lived tokens from `claude setup-token` (~1 year lifetime), assign to multiple agents via `CLAUDE_CODE_OAUTH_TOKEN` env var injection.
- **Key Features**:
  - Subscription registry storing encrypted tokens (AES-256-GCM)
  - MCP tools: `register_subscription`, `list_subscriptions`, `assign_subscription`, `get_agent_auth`, `delete_subscription`
  - REST endpoints: `POST/GET/DELETE /api/subscriptions`, `PUT/DELETE/GET /api/subscriptions/agents/{name}`
  - Token injected as `CLAUDE_CODE_OAUTH_TOKEN` env var on container creation
  - No file injection — env var persists across restarts automatically
  - Auth detection endpoint showing which method each agent uses
  - Fleet auth report at `/api/ops/auth-report`
- **Workflow**:
  1. User runs `claude setup-token` locally to generate long-lived token
  2. Registers subscription via MCP: `register_subscription("name", "sk-ant-oat01-...")`
  3. Assigns to agents: `assign_subscription("agent-name", "subscription-name")`
  4. Agent container is (re)created with `CLAUDE_CODE_OAUTH_TOKEN` env var; `ANTHROPIC_API_KEY` removed
- **Database**: `subscription_credentials` table, `subscription_id` FK on `agent_ownership`
- **Files**:
  - `src/backend/db/subscriptions.py` - Database operations
  - `src/backend/routers/subscriptions.py` - REST API
  - `src/backend/services/subscription_service.py` - Auth mode detection
  - `src/mcp-server/src/tools/subscriptions.ts` - MCP tools

### 20.3a Subscription Auto-Assign on Agent Creation (#74)
- **Status**: ✅ Implemented (2026-03-25)
- **GitHub Issue**: #74
- **Extends**: SUB-002
- **Description**: When a new agent is created, automatically assign the subscription with fewest assigned agents (round-robin). Tie-break: alphabetical by name. Falls back to platform API key if no subscriptions exist or token decryption fails. System agents (`trinity-system`) are unaffected (separate creation path).
- **Key Features**:
  - `get_least_used_subscription()` DB method (SQL: COUNT + ORDER BY)
  - Auto-assign logic in `create_agent_internal()` — token injected before container creation, DB assignment after `register_agent_owner()`
  - Graceful fallback: no subs → API key, decrypt fail → API key, exception → API key
- **Files**: `db/subscriptions.py`, `database.py`, `services/agent_service/crud.py`

### 20.4 Subscription Auto-Switch on Rate Limit (SUB-003)
- **Status**: ✅ Implemented (2026-03-21)
- **Requirement ID**: SUB-003
- **Extends**: SUB-002
- **Priority**: HIGH
- **Spec**: `docs/requirements/SUB-003-subscription-auto-switch.md`
- **Description**: Automatically switches an agent to a different subscription when it encounters 2+ consecutive rate-limit (429) errors. Requires opt-in system setting.
- **Preconditions**: Setting enabled + 2+ consecutive errors + alternative subscription available
- **Key Features**:
  - System setting `auto_switch_subscriptions` (default OFF) with Settings UI toggle
  - Rate-limit event tracking per (agent, subscription) with 2h window
  - Best-alternative selection: prefer fewer assigned agents, skip recently rate-limited
  - Activity event logged on auto-switch, notification sent to agent owner
  - Hooks into chat proxy 429 handler and background task failure path
- **Database**: `subscription_rate_limit_events` table
- **Files**:
  - `src/backend/db/subscriptions.py` - Rate-limit tracking queries
  - `src/backend/services/subscription_auto_switch.py` - Auto-switch orchestration
  - `src/backend/routers/subscriptions.py` - Setting endpoints
  - `src/backend/routers/chat.py` - 429 interception hooks
  - `src/frontend/src/views/Settings.vue` - Toggle UI
- **Negative markers on `is_auth_failure` (#904, 2026-05-21)**: substring match on `AUTH_INDICATORS` now short-circuits to False when the error message also contains an unambiguous signal-kill / OOM / timeout marker (`sigkill`, `sigterm`, `sigint`, `exit code -9`, `exit code 137`, `exit code 143`, `out of memory`, `oom`, `memory cgroup`, `terminated by`, `killed by`). Prevents the SUB-003 trigger from firing on cgroup OOM kills whose detail string happens to contain a word like "token" or "authentication" via downstream wrapping. The same exclusion list lives in `src/scheduler/service.py:_is_auth_failure` to keep the two surfaces from drifting (see §10.4.1).
- **Hot-reload, not recreate (#1089, 2026-06-13)**: the auto-switch no longer recreates the container — `_perform_auto_switch` hot-reloads the new token in place so in-flight turns on the agent survive. See §20.6.
- **Shadow-proof against `.env`-resident API keys (#2114, 2026-08-12)**: post-#1999 the spawn env re-reads `.env` at every spawn, so a stale `.env` `ANTHROPIC_API_KEY` (which Claude Code prefers over `CLAUDE_CODE_OAUTH_TOKEN`) shadowed subscription auth on every execution and made SUB-003 mis-attribute the identical auth failure to each subscription in turn — skip-listing every healthy one for 2h and reporting "no viable alternative". Three coordinated pieces: (1) `_hot_reload_subscription_token` sends `remove_api_key=True`, **gated on the agent's Claude runtime** (`trinity.agent-runtime` label — a legacy subscription row on a Gemini/Codex agent must not strip a `.env` key its own scripts may use; on those runtimes the key never shadows anything); (2) the agent server **arms a force-unset override at boot** (`arm_subscription_auth_guard`, `execution_env.py`) for `ANTHROPIC_API_KEY` + `ANTHROPIC_AUTH_TOKEN` when the container baseline carries a truthy `CLAUDE_CODE_OAUTH_TOKEN` on a Claude runtime — restart-durable, since `startup.sh` exports the rotated override token *before* the server launches so it is always in the boot baseline; expressed through the existing #1999 override layer, so `build_execution_env` stays a pure 4-layer function; (3) **the suppression is visible as data, never only a log**: `env_drift_report` marks force-unset keys `suppressed_for_spawn` (iteration set widened to include override-only keys), and the reload endpoint's response carries `env_shadow` — names of force-unset keys present in the current `.env` parse — which the backend logs at WARNING at switch time. `.env`-only API-key auth (agent with no subscription) is untouched by construction (no baseline token ⇒ no arm). Known accepted consequence: a *funded* `.env` key that was silently billing instead of the subscription flips to subscription auth after the base-image rebuild (release-noted; the managed path to API-key auth remains clear-subscription, which recreates). Deferred siblings (named follow-ups, not built): stale `.env` `CLAUDE_CODE_OAUTH_TOKEN` shadowing a rotated baseline token; `ANTHROPIC_BASE_URL` as a `PROTECTED_KEYS` candidate.
- **Retry the triggering execution after a successful switch (#792, 2026-06-27)**: previously, when a switch fired mid-execution the triggering row was marked FAILED. Interactive chat retries client-side (`routers/chat.py`) and recurring cron recovers next tick, but **one-shot triggers** (manual `…/schedules/{id}/trigger`, webhook, MCP `trigger_agent_schedule`) had no recovery. `TaskExecutionService.execute_task` now intercepts a returned 429/auth response **pre-`raise_for_status`** (mirroring the #678 reader-race retry); when SUB-003 reports a successful switch it re-issues the turn **once** with the **same `execution_id`** and the row lands SUCCESS. Details:
  - **Trigger surface**: the full SUB-003 surface via `classify_switch_failure(response)` (429 → rate_limit; 503/401/403/402 or `is_auth_failure` body → auth), not just status codes — so "any switch-success retries" holds.
  - **Budget**: one retry, guarded by a dedicated `subscription_switch_attempted` flag (NOT `retry_count`, which the #678 retry owns, so the two never suppress each other). A cascade (retry still failing) writes FAILED; the `except` handler is gated on the same flag so it does **not** switch a second time. The 2h skip-list prevents re-selecting the exhausted sub.
  - **Settle**: the retry **is** the readiness probe (`_SWITCH_RETRY_DELAY_S` short pre-delay only) — no circuit-aware `/health` poll (would poison the transport breaker on cold start) and no trust in `restart_result`'s string status.
  - **Cost/budget**: first-attempt cost salvaged into `previous_attempt_cost` (#678 R2 rollup); retry timeout capped to the **remaining** original budget so a post-long-run 429 can't balloon wall-clock/slot time.
  - **Same-`execution_id`** retry means #1084 `effect_guard` dedups wired outbound sinks; residual double-fire risk for arbitrary MCP tool calls is the same the #678 retry already accepts.
  - **Out of scope / follow-ups**: the #1083 fire-and-forget async path (`DISPATCH_ASYNC`, default OFF) routes 429s through the result-callback, bypassing this sync path; and a concurrent switch-lock *loser* (gets `None` from `handle_subscription_failure`) does not retry. Both deferred.
  - **Files**: `src/backend/services/task_execution_service.py` (`classify_switch_failure`, `_extract_agent_error`, `_salvage_attempt_cost`, pre-raise block, except-handler gate); tests `tests/unit/test_792_subscription_retry.py`.

### 20.5 Per-Subscription Usage Tracking (SUB-004)
- **Status**: ✅ Implemented (2026-04-01)
- **Requirement ID**: SUB-004
- **Extends**: SUB-002
- **Priority**: MEDIUM
- **Description**: Track token usage (input, output, cost) per subscription across all agents, enabling admins to see how much each subscription is being consumed. Snapshots subscription_id at execution time so usage history survives SUB-003 auto-switches.
- **Key Features**:
  - `subscription_id` column added to `task_executions` and `chat_sessions` tables (nullable, safe migration)
  - Admin-only `/api/subscriptions/{name}/usage` endpoint with dual-window aggregation (24h + 7d)
  - Per-agent breakdown of input/output tokens, execution count, and estimated cost
  - Snapshot strategy: subscription_id captured at execution time, not looked up retroactively
- **Database**: `subscription_id` columns on `task_executions`, `chat_sessions`
- **Files**:
  - `src/backend/db/subscriptions.py` - Usage aggregation queries
  - `src/backend/routers/subscriptions.py` - Usage endpoint
  - `src/backend/routers/chat.py` - Subscription ID capture at execution time
  - `src/backend/db/chat.py` - Session creation with subscription_id
  - `src/frontend/src/views/Settings.vue` - Usage display (if applicable)

### 20.5a Subscription Usage Observability + Live Headroom (#471, SUB-004 extension)
- **Status**: ✅ Implemented (2026-08-19; merged as PR #2316)
- **GitHub Issue**: abilityai/trinity#471 (P1, epic #1048); siblings trinity-enterprise#351 (agent-facing half), trinity-enterprise#259 (grid tile), #855 (spike — partially answered here)
- **Extends**: SUB-002/003/004
- **Description**: Surface the dark subscription-usage data (SUB-004 windows, SUB-003 failure events) in Settings → Subscriptions and as Dashboard pressure badges, and add **live headroom** — actual 5h/7d utilization % + reset times per subscription — sourced from the `anthropic-ratelimit-unified-*` response headers of a minimal probe call. **OSS-core by explicit decision (2026-08-19)**: visibility is ungated; the paid layer is governance (trinity-enterprise#166 spend caps). Never inferred backwards from the merge (ent#326 discipline).
- **Provider-signal facts (verified 2026-08-19, real stored setup token)**:
  - `GET /api/oauth/usage` → **403 `permission_error` (missing `user:profile` scope)** for `sk-ant-oat01-` setup tokens — that endpoint requires an interactive-login token and is dead for the tokens Trinity stores (the mechanism behind the closed PR #2170).
  - `POST /v1/messages` under the same token → 200 + full unified rate-limit headers (`{5h,7d}-utilization/-reset/-status`, `representative-claim`, overage status). This header channel is what the probe reads.
- **Headroom probe contract**: click-to-refresh (Settings, ≥60s apart per subscription) always available; **ambient refresh default-ON** behind the `subscription_headroom_auto_refresh` system-setting toggle (15-min floor, demand-driven — an unwatched instance probes nothing; **fail-CLOSED to observed-only when Redis is unavailable**, so a probe storm is structurally impossible). Probe = `max_tokens=1` Haiku message (~a dozen tokens of subscription quota per refresh, disclosed on the toggle; operators will see tiny platform-initiated entries in the Anthropic console — release-noted). Probe 429s update the snapshot only, never `subscription_rate_limit_events` (platform-caused, not agent work). Every reading carries `source: "anthropic"|"observed"` + snapshot age; the DB-derived observed block is ALWAYS populated (the load-bearing arm — the #2170 inversion).
- **Data-layer fixes shipped with it**: failure events recorded BEFORE the auto-switch enabled gate (previously an opted-out operator got permanently-zero observability); `failure_kind` column on `subscription_rate_limit_events` (the table conflates auth-class failures with 429s — writer had the param, never persisted); ONE `rate_limited_now` derivation (2h `is_subscription_rate_limited` OR fresh provider status) consumed by every surface.
- **Key surfaces**: extended `GET /api/subscriptions/{id}/usage` (+`failure_events_24h`, per-kind counts, `rate_limited_now`, `headroom` block); `GET /api/subscriptions/{id}/usage/breakdown` (per-agent, both windows, **ranked by `cost_usd` desc** — cost is model-weighted by construction, resolving the 2026-07-28 model-mix research item); `POST /api/subscriptions/{id}/usage/refresh` (click probe); `GET/PUT /api/subscriptions/settings/headroom-auto-refresh`; batch `GET /api/agents/subscription-pressure` (pure-DB accessible set — owned ∪ shared via the shared ent#384 helper; `auth_mode` reuses the `AgentAuthStatus` vocabulary). Tier 0 relabel: subscription-funded agents present cost as `≈ $X API-equivalent` (AgentHeader + fleet surfaces; per-execution cells stay metered `$`).
- **Cut**: Tier 4 bulk auto-assign (out of the operator's 2026-08-17 scope; SUB-003 covers the reactive per-agent case; bulk proactive migration filed separately). MCP tools + `~/.trinity/usage.json` stay in trinity-enterprise#351.

### 20.5b Subscription Headroom History + Failure-Event Retention (ent#433)
- **Status**: 🔨 In progress (2026-08-20)
- **GitHub Issue**: abilityai/trinity-enterprise#433 (P2, `theme-monetization`, epic ent#94); consumer sibling ent#259 (grid tile, merged point-in-time only as PR #2327)
- **Extends**: 20.5a (#471)
- **OSS-core by explicit decision**: the #471 gate ruling carries over — visibility is ungated, the paid layer is governance (ent#166). Recorded here so it is never inferred backwards from the mere fact that it merged (the ent#326 discipline).
- **Description**: #471 keeps exactly ONE last-known-good headroom snapshot per subscription (Redis, overwritten every probe), so "how close did we run to the 5h wall this week" is unanswerable. This adds the durable half: every probe result is persisted as a row, exposed as a bounded time series, and swept under a real retention window.
- **What a probe row records**: `subscription_id`, `fetched_at`, probe `status` (`ok|rate_limited|invalid_token|error|no_windows`), per-window `utilization_pct`/`resets_at`/`status` for 5h and 7d, `representative_claim`, `overage_status`, `unified_status`. **Every** probe that actually ran is persisted, including failures as status-only rows — otherwise a three-day dead token is byte-identical to nobody-watching, which contradicts the honest-gaps rule below. `no_windows` is a history-local classification of the one genuinely ambiguous case (`status='ok'` with neither window reported — reachable when only the bare top-level status header arrives), which would otherwise persist as an all-NULL row indistinguishable from a botched write.
- **A probe that never ran records nothing**, deliberately. A subscription with no usable token returns before any HTTP call, and persisting that would emit one row every 15 minutes forever for a purely configuration state — the highest-volume, lowest-information row in the design. **A gap therefore has three causes** — nobody watched, no usable token, or auto-refresh disabled — and no consumer may present a gap as any one of them.
- **The series is `last`-per-bucket, never `max`** — three independent reasons, all load-bearing:
  1. **Observer effect.** Probes are demand-driven (they fire only on an HTTP request), so samples-per-bucket is proportional to operator attention. `E[max of n]` rises with `n`, so an hour watched during an incident out-reads an identical unwatched hour — and the unattended overnight burn, the thing most worth seeing, gets the fewest samples and the lowest reading.
  2. **Two-peak ambiguity.** 5h and 7d are independent metrics that peak at different instants inside one bucket, so "the peak sample's timestamp" is undefined for a two-column response. `last` yields ONE correlated snapshot of both windows.
  3. **Invisible 429s.** A 429 can legitimately carry `status: rate_limited` with `utilization_pct: None`; under a `MAX(utilization)` read the single most important sample in the series vanishes and the chart flatlines through an outage.
  "How close did we run this week" is answered by max-**across** buckets at the consumer, which is a far less biased estimator than max-**within** bucket.
- **Honest gaps (the load-bearing contract)**: the series is legitimately sparse — an unwatched instance probes nothing. Consumers render gaps as gaps: never interpolate, never present a sparse series as continuous coverage. The payload therefore carries **both** the logical `bucket_start` **and** the real `fetched_at`. Emitting only non-empty buckets with real timestamps alone is *insufficient and was the original design error*: sample jitter and a true gap are indistinguishable from timestamp deltas (a 10:05 sample followed by 11:55 is 1h50m apart with NO gap; 10:55 followed by 12:05 is 1h10m apart WITH one). `bucket_start` is what makes gap detection decidable client-side, with zero synthetic fill.
- **Enrichment, never a dependency**: a history write can never affect the probe path's availability, latency, or correctness. The INSERT runs **after** the Redis snapshot write and **off the event loop** (`asyncio.to_thread`) — a plain `try/except` handles *errors* but not *blocking*, and the platform DB is DELETE-journal with a 30s busy timeout, so a sync write landing during the 03:30 backup or 04:30 VACUUM would stall the whole event loop (health checks, the WS dispatcher, every in-flight request). Ordering is pinned by test.
- **Probe cadence is unchanged and must stay unchanged.** History records what already happens. The write hook sits inside `_probe_and_store`, so it inherits #471's entire rate-bounding envelope for free (60s per-subscription floor, cross-worker single-flight, fail-CLOSED ambient gate) and adds no probe. A sparse chart must **not** become an argument for lowering `SUBSCRIPTION_HEADROOM_REFRESH_SECONDS` — that knob is unguarded and would multiply provider spend on the operator's own quota invisibly.
- **Retention (two windows, both new)**:
  - `subscription_headroom_retention_days` (default 30, `0`=off) — the new history table.
  - `subscription_failure_event_retention_days` (default 30, `0`=off) — **converts** `subscription_rate_limit_events`' previously **hardcoded 24h** sweep into a real window. That table held the platform's only record of *real agent work* hitting a rate limit, timestamped and attributed to the causing agent, and destroyed it at 24h with no operator control, no blast-radius guard, and no `GET /api/settings/retention` entry while every sibling table had all three. Widening to 30d is the #1638-safe direction (no install loses data) and cannot change any existing answer, because every consumer already time-filters (`hours=24` at the call site, a 2h predicate for `rate_limited_now`).
  - Both registered in `RETENTION_OPS_KEYS`, validated in `OPS_SETTINGS_VALIDATION`, each with exactly one `_guard_allows` blast-radius gate (#1644) whose candidate count shares the prune's own predicate by construction, surfaced automatically on `GET /api/settings/retention`, and logged at boot. Neither is in `COMMUNITY_FRESH_INSTALL_SEED` — the 5-day floor would silently truncate a 7-day default read window while the UI labelled it 7 days.
- **Read surface**: `GET /api/subscriptions/{id}/headroom/history?window=24h|7d|30d` — `assert_admin` (which also rejects agent principals, #1890), resolves by id OR name then 404 for parity with `/usage`, and returns bounded buckets (hour for 24h/7d, day for 30d). Selection is a SQL window function (`ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY fetched_at DESC)`), never a bare non-aggregated column beside `MAX()` — that is a SQLite-only extension that raises `GroupingError` on PostgreSQL.
- **Consumer**: this ships the backend only. The realistic ent#259 consumer is a compact in-tile **sparkline**, not a labelled trend chart — FleetGrid v1 renders every tile in exactly one cell (`cells` is declared and deliberately ignored), the tile body is `overflow: hidden` with no scroll, and its row list silently drops anything past a hardcoded cap. `SubscriptionsPanel.vue` is the roomier second consumer.
- **Storage shape**: a dedicated table, not the generic `product_events`. That table has no `subscription_id`, is bound to the activation-funnel's own retention window, and **egresses** — `telemetry_sharing_service.build_aggregate_payload` counts it by type and POSTs it on Tier-2 opt-in, so per-subscription quota telemetry does not belong there.

### 20.6 Credential Rotation via Hot-Reload, not Container Recreate (#1089)
- **Status**: ✅ Implemented (2026-06-13)
- **GitHub Issue**: #1089
- **Extends**: SUB-002 / SUB-003
- **Priority**: HIGH (`theme-reliability`)
- **Builds on**: #799 (per-agent `agent_switch_lock`)
- **Description**: Rotating an agent's subscription token used to **recreate the container**, making "rotate a credential" and "kill every in-flight turn" the same operation (#1037 collateral kills — one 429 on a shared subscription would auto-switch and destroy every parallel execution). Token rotation now goes through a surgical hot-reload of the running container; recreate is reserved for image/template/auth-**mode** changes. This removes the credential↔execution collision class structurally (TARGET_ARCHITECTURE §Agent Runtime).
- **Mechanism**: the agent server spawns Claude via `subprocess.Popen(..., env={**os.environ, ...})` and authenticates purely from the `CLAUDE_CODE_OAUTH_TOKEN` env var (no `.credentials.json` write); it is a single uvicorn worker. Mutating the agent-server process `os.environ["CLAUDE_CODE_OAUTH_TOKEN"]` makes the **next** Claude subprocess use the new token; **in-flight** subprocesses keep their already-inherited old token and finish.
- **Rotation paths converted to hot-reload**:
  1. **Auto-switch** (SUB-003): `_perform_auto_switch` hot-reloads instead of `_restart_agent` (runs inside the #799 `agent_switch_lock`).
  2. **Manual reassignment** (`PUT /api/subscriptions/agents/{name}`): a sub→sub swap hot-reloads under the lock; an auth-**mode** change (none/api-key → subscription) still recreates so `ANTHROPIC_API_KEY` is dropped and the OAuth token is baked into `Config.Env`.
  3. **Key rollover** (`POST /api/subscriptions` upsert): re-registering a subscription's token fans a best-effort hot-reload out to every running agent on that subscription (one agent's failure never fails the upsert nor blocks the others).
- **Key Features**:
  - Agent-server endpoint `POST /api/credentials/reload-token` (`{token, remove_api_key}`) — mutates `os.environ` + persists the token to the writable-layer override; does **not** rewrite `.env`/`.mcp.json` or re-inject Trinity MCP. `remove_api_key=True` force-unsets `ANTHROPIC_AUTH_TOKEN` alongside `ANTHROPIC_API_KEY` (same shadow class, #2114), and the response carries `env_shadow` — names (never values) of force-unset keys present in the current `.env` parse, computed via the module's own `parse_env_file`.
  - **Durable override (F2)**: the token is written to `/var/lib/trinity/oauth-token` (0600), deliberately **not** under `/home/developer` (the persisted workspace volume). `startup.sh` exports it before launching the agent server, so a plain fleet restart (`ops.py` raw stop+start, which bypasses `start_agent_internal`) keeps the rotated token. **Self-reconciling by Docker semantics**: the writable layer survives `stop`→`start` but is wiped on recreate (fresh layer), so a DB-driven recreate re-bakes `Config.Env` (DB token) and the stale override is gone — no marker logic.
  - **Back-compat fallback**: running containers on an older base image return **404** for the endpoint → the backend falls back to `_restart_agent` (identical to pre-#1089 behavior). Per #1037, recreate stays out of scope; the fallback inherits whatever #1037 lands. An agent only gains the endpoint once recreated onto a rebuilt base image (no automatic fleet-wide adoption).
- **Backend helpers** (`services/subscription_auto_switch.py`): `_hot_reload_subscription_token(agent_name)` (POST + restart fallback on 404/transport/no-token; `no_container`/`not_running` short-circuits) and `reload_subscription_for_all_agents(subscription_id)` (key-rollover fan-out under the lock).
- **Files**:
  - `docker/base-image/agent_server/routers/credentials.py` - `reload-token` endpoint + writable-layer override write
  - `docker/base-image/agent_server/models.py` - `TokenReloadRequest`/`TokenReloadResponse`
  - `docker/base-image/Dockerfile` - `mkdir+chown /var/lib/trinity` (Invariant #17 non-root)
  - `docker/base-image/startup.sh` - export override token before agent-server launch
  - `src/backend/services/subscription_auto_switch.py` - hot-reload helper + fan-out + auto-switch wire-in
  - `src/backend/routers/subscriptions.py` - manual sub→sub under lock + key-rollover fan-out
- **Known limitations**: cross-worker race on the process-local `agent_switch_lock` (prod `--workers 2`) is flagged for #1166/#799 (escalate to Redis `SETNX`); a bulk `delete_subscription` still leaves the deleted token live until next start (pre-existing, out of scope). Both self-heal via the durable override / `check_api_key_env_matches` reconciliation.

### 20.7 Enumeration Hardening — Differential-Response Oracles (#186)
- **Status**: ✅ Implemented (2026-07-04)
- **GitHub Issue**: #186 (Epic #1054 Security Hardening) — UnderDefense pentest 3.3.3 (CVSS 2.0 Low)
- **Description**: Closed two enumeration oracles built from differential API responses. (1) **User (email) enumeration** — `POST /api/auth/email/request` returned a distinct body (`"Verification code sent…"` + `expires_in_seconds`), a whitelist-only 429, and a slower whitelisted latency (blocking SMTP send). (2) **Agent enumeration** — the agent-access dependency family (and routers re-implementing it ad-hoc) returned `404 "Agent not found"` for a non-existent agent but `403 "Access denied"` for an existing-but-inaccessible one across 30+ endpoints; some `agent_config` GETs had no access check at all (404-vs-200 oracle + read-hole); the MCP `chat.ts` layer additionally **disclosed the owner username**.
- **Fix**:
  - **Deps (`dependencies.py`)**: all four helpers return a **uniform 404**, evaluating existence AND access before branching (equal timing) and running `_enforce_connector_scope` first. See architecture Invariant #8 (self-uniform rule).
  - **Email (`routers/auth.py`)**: identical generic body/status for all branches; over-limit returns the generic 200 (WARN-logged, no 429); email dispatched fire-and-forget (latency parity). See auth requirements §2.1.
  - **Router sweep**: `avatar.py` (→ `OwnedAgentByName`), `nevermined.py` (uniform 404 helpers), `event_subscriptions.py` (source-agent 400/403 → uniform 403), `schedules.py` webhook endpoints (→ `AuthorizedAgent`), `agent_config.py` capabilities/timeout/public-channel-model/guardrails GETs (→ `AuthorizedAgentByName`, closing the authz hole).
  - **MCP (Invariant #13)**: `chat.ts checkAgentAccess` returns one uniform reason and no owner username; `reports.ts`/`messages.ts` consumer classifiers treat the dep's 404 as not-authorized; `nevermined.ts` reports `configured:false` for an inaccessible agent (enumeration-safe).
- **Contract note**: access-first inline handlers stay **uniform 403** (already self-uniform); `DELETE /{agent_name}` stays 403 (system-agent semantics). The rule is *self-uniform, never 404-then-403* — not "always 404".
- **Tests**: `tests/unit/test_186_enumeration_uniformity.py` (real-DB dep uniformity + email body/no-429 + Tier-4 wiring guard); flipped asserts in `tests/test_access_control.py` and dep-override unit tests.

### 20.8 CI Secret Scanning (#1164)
- **Status**: ✅ Implemented (#1164)
- **GitHub Issue**: #1164 (Epic #1054 Security Hardening) — the deferred prevention half of #1158 / PR #1162
- **Priority**: MEDIUM (theme-security, complexity-low)
- **Description**: Commit-time secret scanner (`gitleaks` MIT CLI) that fails any PR whose changes introduce a credential — closing the #1158 gap where an embedded `re_`-prefixed Resend key shipped in the published CLI and was only caught by a later audit. Public repo → a committed secret is world-readable and permanent, so the guard runs on **every** PR (no path filter, no label gate — #878 lesson).
- **Key Features**:
  - `.github/workflows/secret-scan.yml`: the `gitleaks` **binary** (not the org-licensed `gitleaks/gitleaks-action`, which requires a paid `GITLEAKS_LICENSE` for org-owned repos and fails run 1), version + sha256-pinned, `permissions: contents: read`, scoped to the PR/push **commit range** via `git merge-base` (`--log-opts`). The `--exit-code 2` tri-branch distinguishes clean (0) / finding (2 → fail) / scanner-error (other → fail closed).
  - `.gitleaks.toml`: default ruleset (`[extend] useDefault = true`, keeping `sk-`/`ghp_`/`xox*`/`AKIA` + built-in stopwords) + a custom `trinity-resend-api-key` rule (`re_`-prefixed, entropy floor 3.8) + repo-specific allowlists. The v8.30.1 default set has **no** Resend rule, so the custom rule is the sole `re_` coverage; the repo-prefixed id can never override a future default rule.
  - `--redact=100`: findings are masked in the (public) CI log so a match never re-leaks the secret (learnings #1595: CI output is a credential sink).
- **Distinct from GUARD-002 (§28.2)**: GUARD-002 is a **runtime** hook that scans agent stdout/stderr at execution time; §20.8 is **commit-time source** scanning. Complementary layers, not a duplicate.
- **Enforcement status (be honest)**: the workflow runs on every PR, but the check is **NON-BLOCKING until a repo admin adds `secret-scan` to `dev`/`main` branch protection** (a repo-settings toggle a code PR cannot perform, tracked as a follow-up). Its unconditional trigger is precisely what makes requiring it safe (never left "Expected — waiting"). Until then, prevention is **detection-only** — a red scan does not block merge, so #1164 is "detection shipped; enforcement = follow-up", not "cannot reland / solved".
- **Non-scanned zones**: `tests/**` (~192 intentionally-fake fixtures), `docs/memory/**` (the live engineering docs — architecture/requirements/feature-flows — carry API-usage examples: curl `Authorization: Bearer` commands, truncated JWTs, `KEY=`/`condition=` samples the default `curl-auth-header`/`generic-api-key` rules flag; 8 such FPs were verified during #1164), and `docs/archive|releases|security-reports/**` (historical records; the CSO reports hold secret-pattern examples) are blanket path allowlists — gitleaks pre-skips allowlisted paths before per-finding regex, so these are documented non-scanned zones rather than a narrowing that wouldn't hold. A real credential belongs in `.env`/injection, never a test/doc file; GUARD-002 runtime scanning + human review are the complementary layers. `.env.example` is deliberately **NOT** excluded (a real key pasted there fires). *(Scope note for review: only `docs/memory/**` is excluded, not all of `docs/**` — a broader `docs/**` exclusion is deferred to reviewer judgement; other doc trees stay scanned and rely on the inline `gitleaks:allow` escape hatch for any example FP.)*
- **Honest limit**: the custom rule catches a **verbatim** `re_`-body key. It does **NOT** catch a re-split / XOR-obfuscated secret — verified: the settled #1158 leak's two base85 halves are undetectable by gitleaks under both `useDefault` and this config (neither half is a `re_` key nor keyword-adjacent, and generic entropy on base85 is unreliable in a codebase full of legitimate encoded data). Regex+entropy is defense-in-depth against a *re-land*; **credential rotation** (done in #1158) is the real defense against the original leak. Because the halves produce no finding, `.gitleaksignore` carries no #1158 fingerprint (documented there); it is a non-load-bearing baseline for any future known historical finding, and the range-scoped CI gate does not depend on it.

### 20.9 Stored User Credential — Per-User GitHub PAT (ent#162)
- **Status**: ✅ Implemented (v0.8.5 payload).
- **Credential-storage summary (cross-reference)**: the per-user GitHub token is a
  **stored user credential** — a new credential-bearing column
  `users.github_pat_encrypted`, an **AES-256-GCM JSON envelope** under
  **Invariant #12** (plaintext persistence forbidden; the column is listed among
  the Invariant #12 tables in `architecture.md`). Set/cleared self-service by its
  owner only; **the token is never echoed on read** (status/`configured` flag
  only) — a standing requirement, not just current behavior. Resolution keys on
  **agent ownership**, never a calling/sharing user, so a sharee cannot inject
  their PAT as an agent's git identity.
- **Full requirement (capability + resolution ladders + persist carve-out)**:
  `docs/memory/requirements/github.md` §11.10 — this section is the
  security-surface pointer; the resolution mechanics and the recreate-vs-create
  ladder distinction live there.

### 20.10 Machine Identities for Admin/Ops APIs (#2323)
- **Status**: ✅ Implemented.
- **Premise correction (recorded, because the issue as filed says the opposite)**:
  Trinity already had a machine identity for admin/ops surfaces. A `user`-scoped
  MCP key owned by an admin reaches every admin gate, is **already exempt from
  interactive 2FA** (the MFA gate is invoked only at the two login routes; key
  validation never passes through it), is already revocable, and already rotates
  by minting a second key while the first stays valid. What it lacked was
  **bounds, attribution, and expiry** — so the only 2FA-surviving option was a
  permanent, unattributable, unlimited admin credential, a worse posture than the
  control it worked around.
- **Admin gate is an allowlist (`ADMIN_GATE_SCOPES`)**: `require_admin` /
  `assert_admin` require `mcp_scope ∈ {None, "user", "system"}`. A scope that
  sets neither `agent_name` nor `connector_agent` previously walked both named
  rejections and inherited the owner's role. A principal lacking the attribute
  fails **closed** via a sentinel — never a `None` default, which is the
  privileged JWT value. See `architecture.md` Invariant #8.
- **`ops` scope — read-only, route-fenced, self-authorizing**:
  - Admin-minted and **human-only** to mint (`reject_non_interactive_principal` —
    the allowlist form; the guards used for `portal_delegate` are both no-ops for
    an ops principal, so an ops key could otherwise mint ops keys).
  - Fenced at the **single auth entry point** (`get_current_user`), beside the
    connector / ephemeral / portal_delegate fences. **Every entry is a `GET`**,
    asserted by a test that imports the constant; the method belt stops a future
    `POST /api/ops/*` inheriting read access under a prefix.
  - Kept **out** of `ADMIN_GATE_SCOPES`; an admin-gated ops route opts in with
    `assert_admin(..., allow_scopes={"ops"})`. Authority therefore comes from
    being an ops key, not from the owner's role — which is what makes it a
    machine identity rather than a human's proxy, and what stops every ops
    integration dying when that admin is offboarded.
  - Never carries an `agent_name` (`_AGENTLESS_SCOPES`): three sweeps — the
    canary orphan scan, the key orphan sweep, and the rename/purge cascade —
    find their work by filtering `scope IN ('agent','connector')`, so a
    non-agent scope holding an agent name is invisible to all three.
  - Excluded from the MCP tool surface **by construction**: `OPERATOR_SCOPES` is
    an allowlist pinned by its own test. This is a backend bearer credential.
  - Fence set is derived from the **measured** read set of the real consumer, not
    from the issue's wording (which named only `/api/ops/*` and would have
    shipped a credential unable to run the dashboard it exists for).
- **Audit attribution comes from the presented credential**: `models.User` carries
  `mcp_key_id`/`mcp_key_name`; `platform_audit_service.log()` derives the three
  `mcp_*` columns from `actor_user` when not passed explicitly, fixing ~70 call
  sites with no diff at any of them. `actor_type` stays `"user"` — the owner is
  the accountable party and is the only branch yielding an email, and the
  enterprise user-activity view matches on it. `GET /api/audit-log` gains
  `mcp_key_id` / `mcp_scope` filters so "what did that leaked key touch?" is
  answerable.
- **The `X-MCP-Key-Id` / `X-MCP-Key-Name` headers are gone from the backend**: they
  were `Header(None)` on six routes, validated nowhere, and persisted into the
  backlog replay blob — so honouring them let any authenticated caller forge the
  credential named in the two highest-volume audit events, surfacing minutes later
  on queue drain. The parameters were **removed**, not ignored. Review (#2389)
  found the removal had covered the *audit* path only: five routers still declared
  the headers and wrote them into **durable provenance columns** — `schedule_
  executions.source_mcp_key_id` (`/chat`, `/task`, schedule trigger), `agent_loops`,
  `agent_reminders`, the fan-out rows — and `backlog_service` still persisted both
  into `backlog_metadata`, the longest-lived copy of a request and the one surface
  canary G-04 scans and #1449 scrubs. One request therefore produced two provenance
  records that disagreed, and the forged one outlived the honest one. Every writer
  now derives from `current_user.mcp_key_id`/`mcp_key_name`; **no route declares
  either header** (guarded by a router-tree scan, so the class cannot return one
  endpoint at a time), and the blob no longer carries them — `_spawn_drain` never
  read either key back, so they were stored and never reconstructed. A pre-existing
  queued row still holding them drains unchanged (the drain reads the blob
  key-by-key with `.get()`). The MCP server still *sends* the headers; nothing on
  the backend reads them, and the values it sends are the same key the bearer
  already identifies.
- **Security fix carried by the same change**: the A2A inbound idempotency scope
  reads `mcp_key_id` off the principal and fell back to `username` because the
  field did not exist, so two agent-scoped keys of one owner shared a
  peer-controlled `messageId` namespace — caller B received caller A's full
  response text and B's task never ran. Reachable on an entitled install with ≥2
  `a2a_exposed` agents under one owner. **Deploy note**: the scope string moves
  from `a2a:{agent}:{username}` to `a2a:{agent}:{key_id}`, so a `messageId`
  replayed across the deploy re-executes instead of replaying (bounded by the 24h
  TTL).
- **Explicitly NOT delivered**: key expiry (`mcp_api_keys` still has no
  `expires_at`); narrowing the existing `user` scope (would break the fleet); a
  **write-capable** ops tier — the human-driven ops toolkit's 24 writes stay on
  password auth or a `user`-scoped key. The read fence does not retire the admin
  password.
- **Two costs inherited, not introduced** (flagged in review; stated so they are
  not discovered): key validation writes `last_used_at`/`usage_count` on **every**
  request, so a 10s poll takes a write lock every 10s; and
  `GET /api/ops/fleet/status` issues one HTTP round-trip **per agent**, through
  the client that hosts the transport circuit breaker. Neither is a regression —
  the Observatory already polls exactly these endpoints on an admin JWT at the
  same cadence, and this credential replaces that one rather than adding load.
  A `track_usage=False` path exists (the heartbeat uses it) and is the obvious
  lever if the write rate becomes a problem, at the cost of the key appearing
  dormant; caching for the fleet-status fan-out is a separate change.
- **Honest bound**: the credential narrows the **API** surface only. Ops tooling
  that mutates containers over SSH never touches the API; SSH remains the real
  privilege boundary on those hosts.

---

## 26. Operator Queue & Operating Room (OPS-001)

> **Requirements Doc**: [OPERATOR_QUEUE_OPERATING_ROOM.md](../requirements/OPERATOR_QUEUE_OPERATING_ROOM.md)
> **Feature Flow**: [operating-room.md](feature-flows/operating-room.md)

### 26.1 Agent-Side Protocol
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: OPS-001-AGENT
- **Description**: File-based operator queue (`~/.trinity/operator-queue.json`) for agent-to-platform communication. Request types: approval, question, alert. Meta-prompt section teaches agents the protocol.
- **Files**: `config/trinity-meta-prompt/prompt.md` (Operator Communication section)

### 26.2 Platform File Sync Service
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: OPS-001-SYNC
- **Description**: Background polling service (5s interval) syncs agent queue files with platform database. Reads new agent requests, writes operator responses back to agent files, handles expiration and acknowledgement.
- **Files**: `src/backend/services/operator_queue_service.py`

### 26.3 Backend REST API
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: OPS-001-API
- **Description**: REST API for queue items — list with filters, get single item, submit response, cancel, stats, agent-specific queries. WebSocket events for real-time updates.
- **Files**: `src/backend/routers/operator_queue.py`, `src/backend/db/operator_queue.py`
- **Tests**: `tests/test_operator_queue.py` (37 tests)

### 26.4 Operating Room UI
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: OPS-001-UI
- **Description**: Card-based inbox for processing agent requests. Single-column feed with agent avatars, Open/Resolved tabs, inline response controls with auto-advance. NavBar badge for pending count. WebSocket real-time updates with polling fallback.
- **Files**: OperatingRoom.vue, QueueCard.vue, ResolvedCard.vue, operatorQueue.js store, NavBar badge
- **Remaining**: Sound/desktop notifications for critical items

### 26.5 Agent Collaboration Skill
- **Status**: ⏳ Not Started
- **Requirement ID**: OPS-001-SKILL
- **Description**: Marketplace skill teaching agents how to write requests, read responses, escalate, and internalize operator preferences into memory.

### 26.6 MCP Tools
- **Status**: ⏳ Not Started
- **Requirement ID**: OPS-001-MCP
- **Description**: MCP tools for programmatic queue access — list items, respond to requests, get stats. Enables orchestrator agents to auto-process queue items.

### 26.7 Ingestion Rate / Depth / Size Caps (OPS-001-CAPS)
- **Status**: ✅ Implemented (2026-07-17, #1632)
- **Requirement ID**: OPS-001-CAPS
- **Priority**: HIGH (blocks pull-mode default-ON, #1081 / #1402)
- **Description**: The operator-queue **create path** (the agent-authored `~/.trinity/operator-queue.json` sync ingestion boundary in `_sync_agent`) previously accepted unbounded agent input with no per-agent ingestion cap. #1402 makes this queue the **approval channel for irreversible actions** (payments, destructive ops), so a compromised / prompt-injected agent that floods plausible "approve this" items causes **operator fatigue → reflexive approval**. XSS is already handled (DOMPurify); the exposure is **volume + social engineering**. The fix must bound a **hostile** agent, not just a runaway. Two independent bounds plus field hygiene, all at the one agent-authored seam:
  - **Depth cap (primary, DB-measured ⇒ Redis-independent)**: `db.count_operator_queue_pending_for_agent(agent)` is computed once per cycle; new items are admitted only while `pending + admitted < OPERATOR_QUEUE_MAX_PENDING_PER_AGENT` (default **25**). At the cap, ingestion **stops** (`break`, not drip — avoids the C1 per-cycle DoS of re-scanning a growing file) and the surplus is held behind **one aggregated summary alert**. Bounds per-agent pending rows to `MAX_PENDING (+ platform items)` regardless of Redis.
  - **Rate cap (burst smoothing, Redis, fail-open)**: per-agent `rate_limiter.check("operator_queue_create:{agent}", OPERATOR_QUEUE_CREATE_RATE_LIMIT=60, OPERATOR_QUEUE_CREATE_RATE_WINDOW=60)` + fleet-level `check("operator_queue_create:_fleet", OPERATOR_QUEUE_FLEET_CREATE_RATE_LIMIT=300, 60)` at the real create point only. Denied → item held this cycle, `break` the new-item scan. The fleet cap bounds a colluding / shared-upstream-injected fleet in aggregate (#1402 threat model, #1085 governor precedent).
  - **Field hygiene (`_clamp_ingested_item`, total helper, run INSIDE the #1525 create try/except)**: `title` truncated to `OPERATOR_QUEUE_TITLE_MAX` (300), `question` to `OPERATOR_QUEUE_QUESTION_MAX` (4000) — **truncate-with-marker** (losing a real approval is worse than a clamped one); `context` serialized >`OPERATOR_QUEUE_CONTEXT_MAX_BYTES` (8192) → replaced by a `{"_truncated":true,"_original_bytes":N,"execution_id":<validated ≤128 or dropped>}` marker (so the context cap can't be defeated by a verbatim `execution_id`); non-dict `context` → `{}` (fixes the pre-existing `create_item` `.get` crash class); `options` serialized >`OPERATOR_QUEUE_OPTIONS_MAX_BYTES` (4096) → dropped-with-marker; agent-supplied `created_at` **normalized to ingest time** (defeats future-date sort-pinning; `expires_at` still honored); `priority` validate-only (unknown → `medium`; legit `critical` untouched — the depth cap already bounds critical *volume*).
  - **Reserved-id guard + malformed-id reject**: an agent item whose `id` starts with a platform-reserved prefix (`queue-flood-`, `poison-`, `cb-dormant-`, `sync-failing-`, `git-bloat-`, `skill-not-found-`, `val_`, `system-seed-`, `base-image-stale-`, `alert-budget-` (#1677)) is **rejected** so an agent can't pre-create (and thereby self-suppress via `on_conflict_do_nothing`) its own flood alarm or the #1402 poison alert; an `id` longer than `OPERATOR_QUEUE_ID_MAX` (256) or not matching `^[A-Za-z0-9._:-]+$` is rejected (a PK can't be safely rewritten).
  - **Leader lock** (`opqueue:leader`, mirror monitoring #1464): only the lease-holding uvicorn worker runs `_poll_cycle`, so `--workers 2` no longer double-charges the limiter, double-broadcasts the alert, or double-scans the file. Fail-open to leader on Redis down.
  - **Summary/flood alert**: when depth-held or rate-skipped items occur, **one** `type:"alert"` operator-queue item is emitted via a platform **direct-DB create** (exempt), with an un-guessable `queue-flood-{agent}-{utc_now_iso()}` id, priority `high`, softened wording, and an in-memory `FLOOD_ALERT_COOLDOWN_SECONDS` (300) cooldown so it fires once per episode; wrapped so an emit failure never kills the sync.
  - **Generous DB belt** (`create_item`): rejects (`ValueError`) `title`>4 KiB, `question`>16 KiB, serialized `context`>64 KiB, `id`>512 — an order of magnitude above the service caps so platform items never trip it, but the "platform bypasses the boundary" invariant stops being solely load-bearing (#1525 two-layer philosophy: validate at the boundary AND at the sink).
  - **Platform exemption made true**: `validation_service._notify_operator_on_failure` now creates its notification via a **direct** `db.create_operator_queue_item(...)` instead of writing into the agent file (which would flow through `_sync_agent` and be capped) — this restores exemption-by-construction and fixes the pre-existing latent bug where it `.append`ed to a bare list the sync loop can't parse. **The exemption is scoped by influence, not by caller location (#1677)**: exempt = *platform-only* (volume bound by platform cadence); an *agent-influenceable* direct emitter is **budgeted** — see the next bullet.
  - **Platform-emitter budget (#1677)**: platform direct-DB creates bypass the file-seam caps by construction, and one of them — `task_execution_service._alert_skill_not_found` (#1410) — was agent-influenceable: per-COMMAND dedup + a timestamped id meant each **distinct** unknown slash-command minted a new `priority:"high"` pending row + a notification, uncapped. The unresolved command is a **$0 turn**, so via `agent`/`a2a`/`schedule`/`reminder` triggers the spray runs at dispatch throughput (the loop PoC needs explicit `on_failure="continue", max_consecutive_failures=N` — the default aborts at iteration 1). Fix: such emitters route through `operator_queue_service.create_bounded_alert` — a per-(agent, registered-type) pending-DEPTH cap (`OPERATOR_ALERT_MAX_PENDING_PER_TYPE`, default **5**; DB-measured ⇒ Redis-independent, the #1632 primary-bound mirror; deliberately **no rate cap** — depth is rate-independent, a fast spray only reaches the cap sooner). The budget type is derived from `item["type"]` against the `_BUDGETED_ALERT_TYPES` frozenset registry (today `{skill_not_found}`; registration is a one-line reviewed act) and **every arm fails closed**: an unregistered type, an unreadable count, or a failed create suppresses the ALERT only — the FAILED/`SKILL_NOT_FOUND` execution rows stay the primary observability surface, and the caller gates the paired `db.create_notification` on the same bool so a notification never outlives its queue item. Ordering pinned **dedup → budget → both creates**; the agent-derived command is truncated (~200 chars, marker) on every echo surface (a >16 KiB command used to trip the DB question belt and self-suppress the whole alert). At the cap: ONE cooldown-gated episode alert per `OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS` window per (agent, type), deterministic reserved id `alert-budget-{agent}-{type}-b{bucket}` (the `(agent_name, request_id)` on-conflict target dedups cross-worker; prefix in `_RESERVED_ID_PREFIXES` so an agent can't pre-suppress it), **no `held` count** (untruthful at a first-trip emit) and **no agent-controlled text** (context = `reason`/`alert_type`/`cap`[/`last_triggered_by` platform enum — a triage aid for the cross-agent budget-fill threat: a permitted peer spraying at a victim fills the victim's budget; blast radius one agent × one type, junk rows stay visible, per-row `triggered_by` context identifies the source]). Classification is **CI-forced**: the AST caller-parity guard `tests/unit/test_1677_operator_alert_emitters.py` fails any unclassified `create_operator_queue_item` call site (both facade spellings; `_ALLOWED_CALLERS` keyed (path, qualname) with per-entry justification is the living caller inventory). Scope: the **OSS tree only** — the enterprise submodule is unmounted on public CI and differs per clone, so the private repo owns its own twin (stated residual). A sink-level default bound in `create_item` was rejected on fail-direction grounds: it fails QUIETLY at the load-bearing #1402 poison-park create (park only on success), where the parity test fails LOUDLY at CI (#1890's lesson deliberately inverted). `count_pending_for_agent` gained an optional `item_type` (query-only — no schema change, no migration on either track); `create_item` additionally belts the derived `execution_id` COLUMN (non-str or >512 → None, never truncated — a truncated id feigns validity; `""` unchanged). Closes the platform-emitter residual named on the #1081 pull-mode default-ON gate list.
- **Fail-open policy**: the rate/fleet limiters fail open to the per-worker in-process window; the **DB depth cap** is the Redis-independent hard bound, so fail-open never leaves the channel unbounded (a Redis outage is covered by the depth cap, not by a fail-closed defer that would delay legit escalations). The #1677 budget inverts this for the *alert* surface: its count read fails **closed** (alert suppressed, executions list unaffected) because count degradation is attacker-influenceable and the alert is a secondary surface.
- **Env knobs** (all env-tunable, generous by design — "cap a hostile/runaway agent, not throttle normal use"): `OPERATOR_QUEUE_CREATE_RATE_LIMIT` (60), `OPERATOR_QUEUE_CREATE_RATE_WINDOW` (60), `OPERATOR_QUEUE_FLEET_CREATE_RATE_LIMIT` (300), `OPERATOR_QUEUE_MAX_PENDING_PER_AGENT` (25), `OPERATOR_QUEUE_MAX_SCAN_PER_CYCLE` (500), `OPERATOR_QUEUE_MAX_FILE_BYTES` (2 MiB), `OPERATOR_QUEUE_TITLE_MAX` (300), `OPERATOR_QUEUE_QUESTION_MAX` (4000), `OPERATOR_QUEUE_CONTEXT_MAX_BYTES` (8192), `OPERATOR_QUEUE_OPTIONS_MAX_BYTES` (4096), `OPERATOR_QUEUE_ID_MAX` (256), `OPERATOR_QUEUE_EXECUTION_ID_MAX` (128), `OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS` (300), `OPERATOR_ALERT_MAX_PENDING_PER_TYPE` (5, #1677).
- **Files**: `src/backend/services/operator_queue_service.py`, `src/backend/db/operator_queue.py`, `src/backend/services/validation_service.py`, `src/backend/services/task_execution_service.py` (#1677), `src/backend/database.py`
- **Tests**: `tests/unit/test_1632_operator_queue_caps.py`, `tests/unit/test_1677_operator_alert_budget.py`, `tests/unit/test_1677_operator_alert_emitters.py`

---

## 28. Agent Guardrails (GUARD-001)

### 28.1 Overview
- **Status**: 🚧 In Progress (Phase 1 implemented — #140)
- **Requirement ID**: GUARD-001
- **Priority**: HIGH
- **Description**: Deterministic safety guardrails for autonomous agent execution. Prevents costly mistakes (destructive commands, credential leaks, runaway loops, unauthorized network access) through layered enforcement baked into the base image and agent-server.py — not relying on model compliance alone.
- **Design Principle**: Trinity controls the base image, the agent server, and the deployment pipeline. Guardrails are injected infrastructure-level, not advisory. Agents cannot opt out.

### 28.2 Claude Code Hooks Injection (GUARD-002)
- **Status**: ✅ Implemented (#140)
- **Requirement ID**: GUARD-002
- **Priority**: HIGH
- **Description**: Pre-configure Claude Code hooks in the base image (`/etc/claude-code/managed-settings.json` — root-owned; see 28.2.1) that all agents inherit. Hooks fire deterministically on every tool call — including in `--dangerously-skip-permissions` mode.
- **Key Features**:
  - `PreToolUse` hooks on `Bash` tool: deny-list of destructive patterns (`rm -rf /`, `rm -rf ~`, `chmod 777`, `curl | sh`, `git push --force`, production domain access)
  - `PreToolUse` hooks on `Edit`/`Write` tools: block writes to credential files (`.env`, `.mcp.json`, `~/.ssh/`, `~/.aws/`)
  - `PostToolUse` hooks on `Bash`: scan stdout/stderr for leaked credentials (API key patterns: `sk-`, `ghp_`, `AKIA`, bearer tokens)
  - Hook scripts installed at `/opt/trinity/hooks/` in base image
  - Configurable per-agent overrides via `agent-config.yaml` (operator can relax rules for specific agents that need broader access)
  - All blocked actions logged to Vector pipeline with reason and tool input
- **Architecture**:
  - Base image writes `/etc/claude-code/managed-settings.json` with default hooks during build (ent#345 — was `~/.claude/settings.json`)
  - `startup.sh` merges agent-specific hook overrides from `/config/agent-config.yaml`
  - Hook scripts receive JSON on stdin, return `permissionDecision: deny` to block
  - Exit code 2 = block action, exit code 0 = allow
- **Implementation**:
  - `/opt/trinity/hooks/bash-guardrail.sh` — Deny-list pattern matching on bash commands
  - `/opt/trinity/hooks/file-guardrail.sh` — Block credential file modifications
  - `/opt/trinity/hooks/output-scanner.sh` — Post-execution credential leak detection
  - `/etc/claude-code/managed-settings.json` — Hook registration (baked into Dockerfile, root:root 0444 in a root-owned 0755 dir)

#### 28.2.1 Registration is out of the agent's write reach (ent#345)
- **Status**: ✅ Implemented (2026-08-19)
- **The defect**: the hooks were always root-owned in `/opt/trinity/hooks/`, but their
  REGISTRATION shipped to `/home/developer/.claude/settings.json` — owned by
  `developer`, mode 0644, **inside the git-synced working tree** (HOME is the repo
  root) and read live by Claude Code. The guarded agent could therefore edit the file
  that decides whether it is guarded: one `Edit` disables the bash deny-list, file
  guard, read-only guard and credential output-scanner for every subsequent tool
  call, with no privilege escalation. Observed in the wild — an agent edited this
  file autonomously and committed it through git sync during the #2036
  investigation. Being synced also made it settable from OUTSIDE, by a push to the
  agent's repo. "Agents cannot opt out" (28.1's design principle) was therefore not
  true of the mechanism that enforces it.
- **Fix**: register in `/etc/claude-code/managed-settings.json` — Claude Code's
  admin-controlled managed-settings path, which takes precedence over user and
  project settings and sits outside the synced tree, closing the self-edit and
  inbound-git vectors together. Root-owned `0444` inside a root-owned `0755`
  directory: the file cannot be rewritten and the directory cannot be used to
  replace it or shadow it with a `managed-settings.d` drop-in. No platform-owned
  `settings.json` is shipped into `~/.claude` at all any more, so there is nothing
  there to edit away.
- **Fail-open is the risk this creates, and it is handled**: if the registration is
  missing or writable, Claude Code simply runs no hooks — silently. `startup.sh`
  asserts both properties on every boot and logs `GUARDRAILS: ERROR …` (Vector
  captures it); it reports and continues rather than refusing to boot, so a
  registration problem cannot become a fleet outage. An operator-visible signal on
  `/health` (the `clone_status` pattern, #1439) is the tracked follow-up.
- **Interaction checked**: read-only mode (#887) no longer registers a hook of its
  own — it writes `~/.trinity/read-only-config.json`, which the baked
  `read-only-guard.py` reads — so there is exactly one live registration and the
  managed file cannot clobber a runtime-written one.
  `read_only._remove_legacy_settings_hook` still cleans up pre-#887 leftovers.
- **Both paths stay in the three write-deny lists**
  (`_FILE_WRITE_DENY_PATTERNS` / `guardrails-baseline.json::path_deny` /
  `EDIT_PROTECTED_PATHS`) as defence in depth, not as the primary control. Note
  `bash-guardrail.py` does **not** consult `path_deny`, so before this change the
  Bash route to the registration was open even though the Edit route was denied.
- **Legacy in-tree copy**: `~/.claude/settings.json` sits on the **durable home
  volume**, so rebuilding the image does not remove it from an existing agent —
  leaving a second registration (precedence-dependent) and a live #2036 leak
  candidate. `startup.sh` deletes it **only on an exact `cmp -s` match** against the
  managed copy; an agent-authored or operator-edited settings file differs and is
  left alone. The #2036 ignore rule therefore stays load-bearing, for the legacy and
  agent-authored copies rather than for a platform-baked one — premise restated in
  `test_2036_claude_settings_leak.py`, whose own docstring asked for exactly that
  re-argument if the hooks ever moved out of the synced tree.
- **Tests**: `tests/unit/test_ent345_guardrail_registration.py` (Dockerfile +
  startup assertions; CI does not build the base image, so the shipped artifact is
  what is pinned).

### 28.3 CLI Budget & Scope Controls (GUARD-003)
- **Status**: 🚧 Partially Implemented — `--max-turns` + `--disallowedTools` shipped in #140; chat-mode wall-clock timeout tracked in #313
- **Requirement ID**: GUARD-003
- **Priority**: HIGH
- **Description**: Enforce execution limits on every Claude Code invocation via CLI flags in agent-server.py. Prevents runaway cost, infinite loops, and excessive tool access.
- **Key Features**:
  - `--max-turns` on all executions (configurable per agent, default: 50 for chat, 20 for tasks)
  - `--allowedTools` on task/headless executions (restrict to minimum required tools)
  - `--disallowedTools` for globally banned tools (e.g., block `WebFetch` for agents that shouldn't access the internet)
  - Execution timeout enforced by agent-server.py (kill process after configurable limit, default: 30 minutes)
  - Per-agent configuration via backend API and agent-config.yaml
- **Architecture**:
  - `claude_code.py` reads guardrail config from agent state/config
  - CLI flags injected into every `subprocess.Popen` command array
  - Backend API: `PUT /api/agents/{name}/guardrails` to configure per-agent limits
  - Defaults set in base image, overridable per-agent by operator
- **Configuration Model**:
  ```yaml
  guardrails:
    max_turns_chat: 50
    max_turns_task: 20
    execution_timeout_minutes: 30
    allowed_tools: null          # null = all tools allowed
    disallowed_tools: []         # tools to remove from context
    deny_patterns: []            # additional bash deny patterns
    allow_credential_writes: false
  ```

### 28.4 Credential Isolation (GUARD-004)
- **Status**: ⏳ Not Started
- **Requirement ID**: GUARD-004
- **Priority**: MEDIUM
- **Description**: Prevent agents from reading, logging, or exfiltrating their own credentials. Credentials should be usable (via MCP configs, env vars) but not inspectable.
- **Key Features**:
  - `PreToolUse` hook blocks `Read`/`Bash(cat|head|tail|less|more)` on `.env`, `.mcp.json`, `~/.ssh/*`
  - Credential files mounted read-only with restrictive permissions (already 600, enforce via hook)
  - `PostToolUse` output scanner detects credential values in command output
  - Environment variable values masked if agent tries to `env` or `printenv`
- **Limitation**: Agents need env vars to function (e.g., `ANTHROPIC_API_KEY`). The goal is preventing accidental exposure, not defeating a determined adversary — the Docker isolation boundary is the true security layer.

### 28.5 Guardrails Dashboard & Observability (GUARD-005)
- **Status**: ⏳ Not Started
- **Requirement ID**: GUARD-005
- **Priority**: MEDIUM
- **Description**: Visibility into guardrail enforcement across the fleet. Operators need to see what's being blocked, how often, and whether guardrails are causing legitimate work to fail.
- **Key Features**:
  - Guardrail event log: blocked action, reason, agent, timestamp, tool input
  - Per-agent guardrail configuration display on Agent Detail page
  - Fleet-wide guardrail stats on Operating Room dashboard (blocked/allowed ratio, top blocked patterns)
  - Notifications for high-frequency blocks (may indicate misconfigured agent or attack)
  - Export guardrail events for compliance reporting
- **Architecture**:
  - Hook scripts write structured JSON to `/logs/guardrails.jsonl`
  - Vector pipeline ingests guardrail logs alongside existing container logs
  - Backend API: `GET /api/agents/{name}/guardrail-events`, `GET /api/ops/guardrail-stats`
  - Frontend: Guardrails tab on Agent Detail, summary widget on Operating Room

### 28.6 Network Egress Controls (GUARD-006)
- **Status**: ⏳ Not Started
- **Requirement ID**: GUARD-006
- **Priority**: LOW (Docker network isolation already provides baseline)
- **Description**: Fine-grained control over which external domains/services each agent can reach. Currently agents share the Docker bridge network and can reach any internet host.
- **Key Features**:
  - Per-agent network policy: allowlist of domains the agent can access
  - Default policy: allow all (backward compatible), restrictable per-agent
  - DNS-level filtering via container-specific resolv.conf or iptables rules
  - Log all outbound connections for audit trail
- **Implementation Options**:
  - Docker network policies with iptables rules injected on container creation
  - Sidecar proxy (envoy/nginx) per agent with domain allowlist
  - Claude Code sandbox mode (`sandbox.network.allowedDomains` in settings.json)
- **Note**: This is lower priority because Docker isolation already prevents cross-agent access, and most Trinity agents operate within controlled environments. Prioritize when deploying agents that handle sensitive data or untrusted inputs.

### 28.7 Implementation Phases
1. **Phase 1 — Foundation** (GUARD-002 + GUARD-003): Hook scripts in base image + CLI budget controls in claude_code.py. Immediate protection against the most common failure modes.
2. **Phase 2 — Credential Protection** (GUARD-004): Prevent agents from inspecting their own credentials. Requires hook scripts + output scanning.
3. **Phase 3 — Observability** (GUARD-005): Dashboard and logging for guardrail events. Requires Vector pipeline integration + frontend work.
4. **Phase 4 — Network Controls** (GUARD-006): Per-agent network policies. Requires Docker network configuration changes.

---
