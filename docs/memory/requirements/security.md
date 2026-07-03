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
  - Agent-server endpoint `POST /api/credentials/reload-token` (`{token, remove_api_key}`) — mutates `os.environ` + persists the token to the writable-layer override; does **not** rewrite `.env`/`.mcp.json` or re-inject Trinity MCP.
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
- **Description**: Pre-configure Claude Code hooks in the base image (`~/.claude/settings.json`) that all agents inherit. Hooks fire deterministically on every tool call — including in `--dangerously-skip-permissions` mode.
- **Key Features**:
  - `PreToolUse` hooks on `Bash` tool: deny-list of destructive patterns (`rm -rf /`, `rm -rf ~`, `chmod 777`, `curl | sh`, `git push --force`, production domain access)
  - `PreToolUse` hooks on `Edit`/`Write` tools: block writes to credential files (`.env`, `.mcp.json`, `~/.ssh/`, `~/.aws/`)
  - `PostToolUse` hooks on `Bash`: scan stdout/stderr for leaked credentials (API key patterns: `sk-`, `ghp_`, `AKIA`, bearer tokens)
  - Hook scripts installed at `/opt/trinity/hooks/` in base image
  - Configurable per-agent overrides via `agent-config.yaml` (operator can relax rules for specific agents that need broader access)
  - All blocked actions logged to Vector pipeline with reason and tool input
- **Architecture**:
  - Base image writes `~/.claude/settings.json` with default hooks during build
  - `startup.sh` merges agent-specific hook overrides from `/config/agent-config.yaml`
  - Hook scripts receive JSON on stdin, return `permissionDecision: deny` to block
  - Exit code 2 = block action, exit code 0 = allow
- **Implementation**:
  - `/opt/trinity/hooks/bash-guardrail.sh` — Deny-list pattern matching on bash commands
  - `/opt/trinity/hooks/file-guardrail.sh` — Block credential file modifications
  - `/opt/trinity/hooks/output-scanner.sh` — Post-execution credential leak detection
  - `~/.claude/settings.json` — Hook registration (baked into Dockerfile)

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
