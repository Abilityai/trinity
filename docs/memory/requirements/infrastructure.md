# Requirements — Infrastructure, Platform Operations, CLI, Canary, Enterprise, Build Info

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 8. Infrastructure

### 8.1 Docker as Source of Truth
- **Status**: ✅ Implemented
- **Description**: No in-memory registry; query Docker directly with container labels

### 8.2 SQLite Data Persistence
- **Status**: ✅ Implemented
- **Description**: Users, ownership, API keys, chat sessions via bind mount

### 8.3 Redis for Secrets
- **Status**: ✅ Implemented
- **Description**: Credential storage, OAuth state with AOF persistence

### 8.4 Audit Logging
- **Status**: ✅ Implemented
- **Description**: Security event tracking via Vector log aggregation

### 8.5 Container Security
- **Status**: ✅ Implemented (Updated 2026-03-26)
- **Description**: Non-root execution, CAP_DROP ALL, isolated network, base image allowlist
- **Key Features**: Optional full capabilities mode for containers needing system access, base image allowlist validation (SEC-172)
- **Base Image Allowlist** (SEC-172): Agent creation validates `base_image` against configurable allowlist (`base_image_allowlist` system setting, default `["trinity-agent-base:*"]`). Blocks arbitrary Docker image pulls that could access internal network services. Returns HTTP 403 for disallowed images.

### 8.5b Base-Image Adoption Semantics (#1809, #1816)
- **Status**: ✅ Implemented (2026-07-28)
- **GitHub Issues**: #1809 (regular agents), #1816 (`trinity-system`)
- **Description**: A rebuilt `trinity-agent-base:latest` must be adopted by existing agent containers, which stay pinned to the image **id** they were created from. Adoption happens only at a **cold boundary**.
- **Requirements**:
  - **ADOPT-001**: An agent container whose own `Config.Image` tag no longer resolves to the image id it runs is recreated on its next **cold** start (`check_base_image_matches`, the lazy ninth predicate). Fail-open: any unreadable state skips the evaluation and logs a WARNING.
  - **ADOPT-002**: A **running** agent is never image-recreated. A start of a running agent is a load-bearing idempotent no-op (MCP ensure-running, the SUB-003 auto-switch restart, `restart_system`); image drift is armed fleet-wide by any `build-base-image.sh` run and must never turn it into a container kill. Ephemeral ghosts are excluded outright (volume-less by design).
  - **ADOPT-003** (`trinity-system`): the platform orchestrator adopts at the same cold boundary — backend boot with the container **stopped**, or an explicit `POST /api/system-agent/restart`. `ensure_deployed`'s running branch is **read-only**: it reports `base_image_state` ∈ `stale | current | unknown` and raises an edge-triggered operator-queue alarm on `stale` only (never on `unknown` — a fail-open probe must not manufacture an alert).
  - **ADOPT-004** (AC2, structural): **no** code path may replace the container of a *running* `trinity-system` without an explicit operator stop. Enforced in `start_agent_internal` as an `is_system AND was_already_running` gate over the whole `needs_recreation` block — deliberately independent of predicate count — returning `recreate_deferred: "system_agent_running"` rather than silently doing nothing.
  - **ADOPT-005** (convergence invariant): the container produced by `_create_system_agent` and the container produced by `recreate_container_with_updated_config` must both leave **all eight** config predicates `True`. A permanently-false predicate is an ADOPT-004 hole by construction, because a config-drift recreate resolves the image from a *tag* and is therefore also an image adoption.
  - **ADOPT-006** (rebuild fences): `recreate_missing_container` — the #1559 soft-delete recovery rebuild — **refuses** `trinity-system` with a 409. It reconstructs a regular agent and would irreversibly downgrade the orchestrator (deactivates the **system-scoped** MCP key and mints an agent-scoped replacement, drops `trinity.is-system` / the `/template` bind / `unless-stopped`, arms the scope-403 `TRINITY_BACKEND_URL`). `ensure_deployed`'s create branch is the only supported rebuild. Reachable because `ensure_deployed` runs per uvicorn worker with no leader lock — the race itself is #1817.
  - **ADOPT-007** (operator remedy is human-only): `POST /api/system-agent/restart` and `/reinitialize` require an admin **and** a human principal (`reject_agent_principal`). `assert_admin` alone lets an agent-scoped key through on a default admin-owned install, and #1816 turns `/restart` into a container replacement.
- **Tests**: `tests/unit/test_1809_image_drift_recreate.py`, `tests/unit/test_1816_system_agent_convergence.py`, `tests/unit/test_1816_system_agent_adoption.py`
- **Docs**: [internal-system-agent.md](../feature-flows/internal-system-agent.md) → Base-image adoption; [agent-lifecycle.md](../feature-flows/agent-lifecycle.md)

### 8.5a SSRF Prevention — Skills Library URL Validation (SEC-179)
- **Status**: ✅ Implemented (2026-03-27)
- **GitHub Issue**: #179
- **Description**: Skills library URL validated against strict github.com allowlist to prevent SSRF leading to DoS (pentest finding 3.2.2, CVSS 6.7)
- **Key Features**: Hostname must be exactly `github.com`, HTTPS enforced, DNS resolution checked against private/internal IP ranges, validation at both write time (`PUT /api/settings/skills_library_url`) and sync time (`POST /api/skills/library/sync`)
- **Tests**: `tests/unit/test_ssrf_skills_library.py` — 28 tests

### 8.6 GCP Production Deployment
- **Status**: ✅ Implemented
- **Description**: SSL/TLS via Let's Encrypt, nginx reverse proxy

### 8.7 Vector Log Aggregation
- **Status**: ✅ Implemented (2025-12-31)
- **Description**: Centralized log aggregation via Vector replacing audit-logger
- **Key Features**: Docker socket capture, VRL transforms, platform.json/agents.json output
- **Flow**: `docs/memory/feature-flows/vector-logging.md`

### 8.8 Frontend E2E Test Infrastructure
- **Status**: ✅ Implemented (2026-04-29)
- **Description**: Playwright-based smoke test harness for the Trinity frontend, gated on the `ui` PR label in CI (#556)
- **Key Features**: Chromium-only smoke suite (dashboard, agents, operating room, templates), storage-state auth pattern (login once, reuse session), label-gated CI workflow (~5 min, opt-in), on-failure artifact upload (screenshots, videos, Trinity logs)

---

## 12. Platform Operations

### 12.1 Internal System Agent
- **Status**: ✅ Implemented (2025-12-20)
- **Description**: Auto-deployed platform orchestrator (`trinity-system`)
- **Key Features**: Deletion-protected, system-scoped MCP key, permission bypass, ops commands
- **Flow**: `docs/memory/feature-flows/internal-system-agent.md`

### 12.2 System Agent Operations Scope
- **Status**: ✅ Implemented (2025-12-20)
- **Description**: Fleet ops, health monitoring, schedule control, emergency stop
- **Key Features**: `/ops/*` slash commands, configurable thresholds
- **Guiding Principle**: "The system agent manages the orchestra, not the music."

### 12.3 Web Terminal for System Agent
- **Status**: ✅ Implemented (2025-12-25)
- **Description**: Admin-only browser terminal for System Agent
- **Flow**: `docs/memory/feature-flows/web-terminal.md`

### 12.4 System Agent UI Page
- **Status**: ✅ Implemented (2025-12-20)
- **Description**: Admin-only `/system-agent` page with fleet overview and operations console
- **Key Features**: Fleet cards, Emergency Stop, Restart All, Pause/Resume Schedules
- **Flow**: `docs/memory/feature-flows/system-agent-ui.md`

### 12.5 OpenTelemetry Integration
- **Status**: ✅ Implemented (2025-12-20, extended 2026-04-14)
- **Description**: OTel metrics export from Claude Code agents + backend distributed tracing
- **Key Features**: Cost, tokens, productivity metrics in Dashboard; trace_id in logs for multi-agent request correlation (RELIABILITY-002)
- **Flow**: `docs/memory/feature-flows/opentelemetry-integration.md`

### 12.6 System-Wide Trinity Prompt
- **Status**: ✅ Implemented (2025-12-14, refactored 2026-03-15 Issue #136)
- **Description**: Admin-configurable prompt injected at runtime via `--append-system-prompt` on every Claude Code invocation
- **Flow**: `docs/memory/feature-flows/system-wide-trinity-prompt.md`

### 12.6.1 Execution Context Injection (#171)
- **Status**: ✅ Implemented (2026-04-14)
- **Description**: Dynamic per-invocation `## Execution Context` block appended to every agent system prompt so agents can self-calibrate. Carries mode (chat vs autonomous task), trigger source, model, timeout budget, own name, permitted collaborators, schedule metadata, and timestamp.
- **Key Features**:
  - Single composition seam (`platform_prompt_service.compose_system_prompt`) for all invocation paths (chat / task / schedule / mcp / agent-to-agent / fan-out / paid / public)
  - Behavioral guidance per mode: chat mode permits clarifying questions; task mode enforces execute-to-completion
  - User-controlled metadata (schedule name, MCP key name) sanitized before rendering — strips control chars, backticks, and markdown heading markers, caps length — to prevent prompt-injection via metadata fields
  - Builder failures never fail a request: always falls back to the base platform prompt
  - Operator kill-switch via `trinity_execution_context_enabled` setting (default enabled)
- **Flow**: `docs/memory/feature-flows/execution-context-injection.md`

### 12.7 Vector Memory
- **Status**: ❌ Removed (2025-12-24)
- **Reason**: Templates should define their own memory. Platform should not inject agent capabilities.

### 12.8 Agent Monitoring Service (MON-001)
- **Status**: ✅ Implemented (2026-02-23)
- **Requirement ID**: MON-001
- **Description**: Multi-layer health monitoring for agent fleet with real-time alerts
- **Key Features**:
  - Docker layer: Container status, CPU/memory, restart count, OOM detection
  - Network layer: Agent HTTP reachability with latency tracking
  - Business layer: Runtime availability, context usage, error rates
  - Real-time WebSocket updates for health state changes
  - Alert cooldowns to prevent notification spam
  - Fleet dashboard with health summary (admin-only)
  - 3 MCP tools: `get_fleet_health`, `get_agent_health`, `trigger_health_check`
- **Status Levels**: healthy → degraded → unhealthy → critical → unknown
- **Flow**: `docs/memory/feature-flows/agent-monitoring.md`

### 12.8a Richer Agent `/health` Signal (#1020)
- **Status**: ✅ Implemented (2026-06-02)
- **GitHub Issue**: #1020
- **Description**: Promote the agent container's `/health` from `{status}` + ad-hoc diagnostics to a named, contractual signal the platform acts on — an incremental step toward `TARGET_ARCHITECTURE.md` §Agent Runtime.
- **Key Features**:
  - New top-level fields: `active_tasks` (concurrent executions across `/api/chat` + `/api/task`), `last_task_at` (ISO), `consecutive_failures` (reset on success, incremented on failure).
  - Counters tracked in `agent_server/state.py` (`record_task_start`/`record_task_finish`), wired at both execution chokepoints in `agent_server/routers/chat.py`. Thread-safe (concurrent tasks).
  - `consecutive_failures` is the signal the dispatch circuit breaker (#526) consumes; `last_task_at` powers liveness; both feed the heartbeat push (#307).
  - Backend `monitoring_service.py` reads `consecutive_failures`/`last_task_at` into `BusinessHealthCheck` (graceful `None` default for pre-#1020 agent images).
  - `mailbox_depth` intentionally NOT emitted — no agent-side mailbox until the actor model (#945); backend derives queue depth from `CapacityManager`.
  - Back-compat: existing `/health` keys unchanged; new keys additive.

### 12.9 Cleanup Service for Stuck Resources
- **Status**: ✅ Implemented (Updated 2026-03-25, Issue #129)
- **Requirement ID**: CLEANUP-001
- **GitHub Issue**: #94, #129
- **Description**: Background service that automatically recovers stuck intermediate states via active watchdog reconciliation and passive stale detection
- **Key Features**:
  - **Active watchdog** (Issue #129): Reconciles DB execution state against agent process registries every 5 minutes
  - Orphan recovery: Executions marked "running" in DB but not found on agent are marked failed with descriptive error
  - Auto-terminate: Executions confirmed running on agent but exceeding `timeout_seconds` are terminated via agent API
  - Race-condition guard: Conditional DB update (`WHERE status='running'`) prevents overwriting normal completions
  - Capacity/queue release: Slots and queue state released on recovery; atomic Lua-script queue release prevents TOCTOU
  - WebSocket broadcast: Frontend notified of watchdog recovery actions
  - Dispatch grace period: 60s grace for newly created executions before orphan detection
  - Systemic failure detection: Warns if >50% of recovery attempts fail in a single cycle
  - **Passive stale cleanup**: Marks stale executions (`status='running'` > 120 min) as `failed`
  - Marks stale activities (`activity_state='started'` > 120 min) as `failed` — a **backstop for
    the unclaimed only** (#1804): every writer that wins a terminal CAS now closes the paired
    dispatch activity itself (§10.15 in `scheduling.md`), so a row reaching this sweep means a
    producer is unowned. Runs **after** `_sweep_stale_slots` in the cycle (it used to run one line
    before the stale-slot reaper, so within a single cycle the 120-minute duration fabricator could
    beat a legitimate closer).
  - Recovery paths (watchdog `_recover_execution`, startup recovery, the two bulk sweeps via
    `_close_bulk_swept_activities`, the lease reaper, both backend-shutdown `CancelledError`
    handlers) close their execution's activity on the CAS-won branch — counted in
    `CleanupReport.activities_closed_on_recovery` (#1804)
  - Cleans up stale Redis slots (entries older than TTL)
  - One-shot startup sweep on backend restart
  - Periodic cleanup every 5 minutes
  - Admin-only status endpoint: `GET /api/monitoring/cleanup-status`
  - Admin-only trigger endpoint: `POST /api/monitoring/cleanup-trigger`
- **Constants**: Interval 300s, execution timeout 120min, activity timeout 120min, watchdog HTTP timeout 5s, dispatch grace 60s

### 12.10 Execution & Health-Check Retention (Issue #772)
- **Status**: ✅ Implemented (2026-05-11, Issue #772)
- **Requirement ID**: RETENTION-001
- **GitHub Issue**: #772
- **Description**: Bounded growth for `schedule_executions` (driven by per-run JSONL transcripts in `execution_log`, ~150–190 KB/row) and `agent_health_checks` so active fleets don't hit disk pressure within weeks. Production observation pre-fix: ~3.3 GB / ~9k rows on `schedule_executions` and ~200 MB / ~750k rows on `agent_health_checks`.
- **Key Features**:
  - **Two-stage retention on `schedule_executions`**: nulling `execution_log` past `execution_log_retention_days` preserves row + metadata (agent, status, cost, duration) for audit; full row DELETE past `execution_row_retention_days` for deeper retention.
  - **Per-cycle row budget**: each sweep caps at 5000 rows per 5-min cleanup tick so the first post-deploy backfill spans hours rather than holding a multi-minute write lock.
  - **Chunked SQL**: prune methods iterate `SELECT id ... LIMIT N` → `DELETE/UPDATE id IN (...)`, committing per chunk (avoids `SQLITE_ENABLE_UPDATE_DELETE_LIMIT` dependency).
  - **`iso_cutoff()` cutoffs**: time-window comparisons against ISO-Z TEXT columns use the helper from `utils/helpers.py`, per Architectural Invariant #16.
  - **Partial index** `idx_executions_completed_terminal ON schedule_executions(completed_at) WHERE status IN ('completed','failed','terminated')` drives both sweeps via index range scan.
  - **WAL checkpoint** after each cycle that reclaims rows (`PRAGMA wal_checkpoint(TRUNCATE)`).
  - **Daily VACUUM** via `db_vacuum_service.py` (APScheduler, 04:30 UTC, autocommit connection) for last-mile page reclaim.
  - **Admin-configurable** via `GET/PUT /api/settings/ops/config` using new ops keys: `execution_log_retention_days` (default 30), `execution_row_retention_days` (default 90), `health_check_retention_days` (default 7). `0` disables that sweep.
  - **Backward-compatible**: existing `cleanup_old_records()` (agent_health_checks) is reused with added `chunk_size` parameter; previously orphaned (not invoked from any tick), now wired into the cleanup service.
- **Constants**: Cleanup tick 300s, per-cycle row budget 5000, vacuum cron 04:30 UTC.

### 12.11 Terminal `backlog_metadata` PII Scrub (Issue #1449)
- **Status**: ✅ Implemented (2026-07-17, Issue #1449)
- **Requirement ID**: RETENTION-002
- **GitHub Issue**: #1449
- **Description**: `services/backlog_service.py::enqueue` `json.dumps`es the full drain-replay request — including `user_message`, `user_email`, and `system_prompt` — into `schedule_executions.backlog_metadata` so a queued task can be reconstructed at drain. That blob is read **only while `status='queued'`** (the backlog drain claims only queued rows; the #1083/#1081 result callbacks read the POST payload, not the row's metadata; canary E-04/G-04 are queued-scoped). On a **terminal** row it is stale PII sitting in the DB indefinitely, bounded only by the 90-day `execution_row_retention_days` DELETE. The scrub NULLs it as soon as the row reaches an authoritative terminal.
- **Key Features**:
  - **`db.scrub_terminal_backlog_metadata(chunk_size)`** — chunked `SELECT id ... LIMIT N` → `UPDATE ... SET backlog_metadata=NULL WHERE id IN (...)`, each chunk its own transaction (short write lock), mirroring `prune_execution_logs`.
  - **Authoritative terminals only** — `status IN ('success','cancelled','skipped')` (the `_AUTHORITATIVE_TERMINALS` set). **FAILED is deliberately EXCLUDED**: a FAILED row is resurrectable to SUCCESS via a late token-gated CAS (`park_expired_lease` keeps its `claim_token`), so its drain-replay intent must survive; FAILED PII stays bounded by the 90-day `prune_execution_rows`.
  - **Not age-gated, not operator-configurable** — the scrub is a **security invariant**, not a retention window. It runs unconditionally every cleanup tick (even when every #772 window is `0`) and has **no ops-settings key** — a fixed default sidesteps the #1638 floor-by-seed trap.
  - **Count-only logging** — the scrubbed count feeds the sweep report + the `_maybe_wal_checkpoint` sum (a scrub-only cycle still truncates the WAL); the `backlog_metadata` blob itself is **never** logged (it carries PII).
- **Location**: `services/cleanup_service.py::_sweep_retention_772` (sub-sweep), `db/schedules.py::scrub_terminal_backlog_metadata`.
- **No schema change, no migration, no new service.**
- **Deferred sibling (not in this change)**: callback/pull-path chat-session persistence (the other #1444 carve-out) is deferred to the pull single-applier work (#1081) — it must land WITH the FAILED-exclusion already shipped here.

---

## 30. CLI Tool (CLI-001)

### 30.1 CLI Package
- **Status**: 🚧 In Progress
- **Description**: Python Click CLI (`trinity`) that provides shell-level access to the platform
- **Key Features**: `pip install -e src/cli/`, mirrors core MCP tools as shell commands, JSON and table output
- **Location**: `src/cli/`

### 30.2 CLI Authentication (CLI-002)
- **Status**: ✅ Implemented
- **Description**: Email-based login flow for CLI users
- **Key Features**: `trinity init` (onboarding), `trinity login` (email + code), `trinity logout`, `trinity status`, config stored in `~/.trinity/config.json`
- **API**: `POST /api/access/request` (auto-approve whitelist), reuses `/api/auth/email/request` + `/api/auth/email/verify`

### 30.3 CLI Agent Operations (CLI-003)
- **Status**: ✅ Implemented
- **Description**: Core agent management commands
- **Key Features**: `trinity agents list|get|create|delete|start|stop|rename`, `trinity chat`, `trinity logs`, `trinity health`, `trinity skills`, `trinity schedules`, `trinity tags`

### 30.4 CLI Output Formatting (CLI-004)
- **Status**: ✅ Implemented
- **Description**: `--format json` (default, for scripting) and `--format table` (human-readable via Rich)

### 30.5 CLI Multi-Instance Profiles (CLI-005)
- **Status**: 🚧 In Progress
- **Description**: Named profiles for managing multiple Trinity instances (local, staging, production) from a single CLI installation
- **Key Features**: `trinity profile list|use|remove`, `--profile` global flag, `TRINITY_PROFILE` env var, legacy flat config auto-migration to `default` profile
- **Location**: `src/cli/trinity_cli/config.py`, `src/cli/trinity_cli/commands/profiles.py`

### 30.6 CLI Deploy Command (CLI-006)
- **Status**: ✅ Implemented
- **Description**: Deploy local agent directories to Trinity with `trinity deploy .`
- **Key Features**: Tar+base64 archive, POST to `/api/agents/deploy-local`, `.trinity-remote.yaml` tracking for idempotent redeploys, `--name` override, `--repo` for GitHub-based deploy, `.gitignore`-aware archiving, instance mismatch warning on redeploy
- **Location**: `src/cli/trinity_cli/commands/deploy.py`
- **Tracking file**: `.trinity-remote.yaml` (auto-added to `.gitignore`)

### 30.7 CLI MCP Key Auto-Provisioning (CLI-007)
- **Status**: ✅ Implemented
- **Description**: After `trinity init` or `trinity login`, automatically provision an MCP API key and store it in the profile
- **Key Features**: Calls `POST /api/mcp/keys/ensure-default`, stores `mcp_api_key` in profile, `trinity init` also writes `.mcp.json` with Trinity MCP server config
- **Location**: `src/cli/trinity_cli/commands/auth.py`

### 30.8 Agent Quota Enforcement (QUOTA-001)
- **Status**: ✅ Implemented
- **Description**: Per-role agent creation limits with admin exemption. Configurable per role via Settings UI.
- **Key Features**: Admin users exempt (unlimited), per-role defaults (creator=10, operator=3, user=1), configurable via `GET/PUT /api/settings/agent-quotas`, legacy `max_agents_per_user` fallback, system agents excluded from count, redeploys bypass quota, 429 response includes current/limit counts
- **Location**: `src/backend/services/settings_service.py` (`get_agent_quota_for_role`), `src/backend/services/agent_service/crud.py`, `src/backend/services/agent_service/deploy.py`, `src/backend/routers/settings.py`, `src/frontend/src/views/Settings.vue`

---

## 31. Canary Invariant Harness (CANARY-001)

### 31.1 Continuous Orchestration-Invariant Watcher (CANARY-001 — Phase 1)
- **Implements**: Issue #411 — first three invariants (S-01, E-02, L-03)
- **Description**: Background watcher service that runs deterministic
  orchestration-invariant checks against live platform state every 5
  minutes. Persists violations to a queryable table and classifies
  green→red transitions for an external alert sink. Catches the bug
  class behind PRs #378, #403, #129, #226 — race conditions and
  cross-component state drift that unit tests miss.
- **Architecture**: deterministic Python library (`src/backend/canary/`)
  shared between the watcher service (`services/canary_service.py`) and
  the on-demand admin endpoint (`POST /api/canary/run-cycle`). Library
  reads state but writes nothing; service writes violations and
  classifies transitions.
- **Phase 1 invariants**:
  - **S-01** Slot–row bijection (Redis ZRANGE vs SQL running rows, drain
    sentinels filtered)
  - **E-02** No phantom reversal (terminal executions stay terminal,
    detected via Redis-backed state comparison)
  - **L-03** Delete cascades (no orphan rows referencing removed agents
    in any cross-cutting table; no orphan Redis slot keys)
- **Storage**: `canary_violations` table; observed_state JSON column.
- **Activation**: gated by `CANARY_ENABLED=1` env var; disabled by
  default. Production deployment is staging/dev — the harness watches
  there, not in user-facing prod.
- **Fleet**: `config/canary-fleet.yaml` deploys two synthetic agents
  (`canary-fleet-burst` minute-cron, `canary-fleet-long` 5-min cron) via
  the existing `/api/systems/deploy` endpoint. Without the fleet, the
  watcher reports trivially-green cycles with no signal.
- **Alert sink**: Slack via incoming webhook URL configured by the
  `CANARY_SLACK_WEBHOOK_URL` env var (admin-side, no Settings UI — the
  audience is operators with shell access on staging/dev). Each
  green→red transition fires exactly one webhook POST with a Block Kit
  payload (severity emoji header, rendered violation summary, context
  line with snapshot_time + violation count + "last red Xm ago"
  badge). Unset = silent sink: cycles still run, violations still
  persist to `canary_violations`, only the outbound POST is skipped.
  Continuing-red invariants don't re-post. The dashboard-notifications
  path (writing `agent_notifications` rows via `db.create_notification`)
  was rejected on the product call.
- **Determinism**: invariant checks are pure functions
  `check(snapshot) → list[ViolationReport]`. Same snapshot input always
  yields the same output. No LLM reasoning anywhere in the canary path.
- **Phase 2 / 3 (shipped, #882)**: S-02, E-01, E-05, B-01 (Phase 2) and
  S-03, B-02, R-01 (Phase 3). E-06 shipped separately (#1472).
- **Phase 4 (shipped, #1077)**: four pure single-table predicates over
  `schedule_executions`, no new source types. E-03 (completed rows populated —
  `completed_at IS NOT NULL`, `completed_at`-only predicate) and G-03 (clock
  sanity — `started_at ≤ completed_at`, ~1s tolerance, UTC-aware parse) ride a
  shared terminal-row collector (`_collect_terminal_rows`, windowed on
  `started_at`, `LIMIT 5000`). E-04 (queued-row metadata integrity —
  `queued_at NOT NULL` AND `backlog_metadata` non-NULL + JSON-parseable) and
  G-04 (no raw credentials in `backlog_metadata` — secret-prefix regex scan)
  ride the queued-row metadata `_collect_executions` captures, scoped strictly
  to `status='queued'` rows (so #1449's deferred terminal-row NULL-out can't
  false-fire). E-04/G-04 are stacked on #1450's queued-read rework and land
  after it. **Credential safety:** E-04/G-04 violations persist to
  `canary_violations`, so neither ever echoes the raw `backlog_metadata` — E-04
  reports the failed-predicate reason code, G-04 the matched pattern name only.
- **Phase 5 (shipped, #1813)**: **H-01 collector blindness** — the harness's
  first *self*-check, and the reason the `H-` (harness health) id family exists:
  every other invariant means "the system is broken", H-01 means "the observer
  is blind", and an H-01 violation invalidates every other green in that cycle.
  #1540 repointed the SQL-tier collectors onto the configured engine but left
  the failure *shape* untouched — a collector reading an empty or unreachable
  source returns zero rows, which is indistinguishable from a genuinely clean
  fleet, so both report green. H-01 fires when the roster read
  (`_collect_known_agents`) returns zero rows or raises **while an independent,
  non-SQL source proves the fleet is alive**: Docker container presence
  (`docker_agent_names`, read from the container list *before* any `exec_run`,
  since `zombie_counts` is keyed by exec success and thins on a degraded
  container) ∪ Redis slot keys (`orphan_redis_slots`, corroborating only — slot
  keys exist solely while an execution holds a slot). Reason codes
  (stable — trinity-enterprise#202 scores on them): `roster_read_failed` /
  `roster_empty_contradicted` (critical) / `roster_empty_unverifiable` (major,
  the evidence source was itself unreachable). **Two-cycle confirmation**
  (`canary:h01:suspect_since`, E-02's cross-cycle-state precedent) so the
  last-agent delete race — DB row gone, container still tearing down — cannot
  false-fire; an unreadable marker fires *unconfirmed* rather than skipping,
  because a guard that cannot self-check must say so. Scoped to the roster read
  ONLY: on a live-but-quiet fleet `terminal_rows`/`enabled_schedules`/
  `orphan_refs`/`terminal_exec_statuses` are all legitimately empty, so a
  general "any SQL collector reads zero" rule would false-alarm on every idle
  install. Dual-track by construction (a pure function over the `Snapshot`; it
  issues no SQL). **Residual:** an entirely *stopped* fleet has no containers
  and no slots, so no evidence exists and H-01 can only reach
  `roster_empty_unverifiable`; partial blindness (roster returns 1 of 20) is out
  of scope, since a count comparison would false-fire on create/stop races.
- **Registration**: each new invariant is a new file under
  `src/backend/canary/invariants/` + a registry entry (per the catalog at
  `docs/testing/orchestration-invariant-catalog.md`); the service and API
  surface stay unchanged. An invariant whose alert must be *actionable* also
  needs a `_INVARIANT_NAMES` + `_INVARIANT_RUNBOOKS` entry (and, when it carries
  no `agent_name`, a `_render_message` branch) in `services/canary_alerts.py` —
  otherwise the Slack line degrades to the opaque `"<ID> fired N violation(s)"`
  fallback. *Known gap: E-03/E-04/E-06/G-03/G-04 (Phase 4) were never added to
  those dicts and still render the fallback — pre-existing, tracked separately.*

---

## 35. Enterprise Edition Architecture (#847)

### 35.1 Open-Core Seam — Private Submodule Integration (#847)
- **Status**: ✅ Implemented (2026-05-21)
- **GitHub Issue**: #847 (design + paid-module catalog tracked privately in `trinity-enterprise`)
- **Description**: A generic extension seam in the public backend for loading
  closed-source modules from a private git submodule at
  `src/backend/enterprise/`. The seam is feature-agnostic — it carries **no
  enumeration of which capabilities are paid**; that catalog and the
  per-module designs live only in the private `trinity-enterprise` repo.
- **Key mechanism (public)**:
  - `EntitlementService` (`src/backend/services/entitlement_service.py`) — a
    registry. `register_module(feature_id)` populates a set; `is_entitled()` /
    `list_entitled_features()` read from it. OSS builds never call
    `register_module` → empty set → deny everything. `TRINITY_OSS_ONLY=1` is a
    hard override (denies even when modules ARE registered).
  - `requires_entitlement(feature_id)` (`src/backend/dependencies.py`) — a
    FastAPI dependency factory mirroring `require_role`; HTTP 403 when not
    entitled.
  - Conditional loader in `src/backend/main.py` —
    `try: from enterprise.backend import register_enterprise; register_enterprise(app) except ImportError: pass`.
    OSS-only builds (no submodule) silently no-op.
  - `/api/settings/feature-flags` exposes `enterprise_features: list[str]` —
    empty in OSS mode, populated when the private submodule is mounted; the OSS
    frontend reads it to decide which gated surfaces to render (same pattern as
    `session_tab_enabled` / `voice_available`).
  - Enterprise Vue components ship in the OSS bundle (no algorithmic IP — the
    moat is the private backend logic); they are gated purely by the
    server-driven `enterprise_features` list.
- **Tunables (env)**: `TRINITY_OSS_ONLY` (`0`/`1`, default `0`) — force
  OSS-only mode regardless of submodule presence.
- **Private (not in this repo)**: the specific module catalog, their routers and
  private schema, the licensing/entitlement enforcement design, and the
  commercial rationale are documented privately in `trinity-enterprise`.

### 35.2 Seam DX — Optional Submodules, Public Install Doc, Edition Surface (#1443)
- **Status**: ✅ Implemented (2026-07-04)
- **GitHub Issue**: #1443 (epic #1258)
- **Description**: Make the open-core seam discoverable and friction-free.
  Both private submodules (`.claude`, `src/backend/enterprise`) are marked
  `update = none` in `.gitmodules`, so a fresh public clone +
  `git submodule update --init --recursive` completes **without credentials**
  (git skips them, exit 0). Mounting is an explicit per-clone opt-in.
- **Opt-in mechanics** (empirically verified): under `update = none`, a plain
  `--init <path>` is *also* skipped, and a one-shot `--init --checkout` copies
  `none` into local config (future plain updates skip again). The durable
  opt-in is config-first: `git config submodule.<path>.update checkout`, then
  `git submodule update --init <path>`. Existing clones initialized while
  `.gitmodules` had `update = checkout` (i.e. `.claude` post-init) carry a
  protective local override; enterprise clones do NOT and need the one-time
  config line (documented in `docs/ENTERPRISE.md`; `deploy-dev.yml` sets it
  and judges init success by the populated marker file, since skip == exit 0).
- **Public install doc**: `docs/ENTERPRISE.md` — generic seam only (mount
  commands, HTTPS-PAT URL override, rebuild, verification via boot line /
  feature-flags / `edition`); guard-compliant per
  `.github/workflows/enterprise-docs-guard.yml`.
- **Edition surface**: `GET /api/version` returns
  `edition: "oss" | "enterprise"` + `enterprise_features: list[str]`, both
  derived from `entitlement_service.list_entitled_features()` (the same
  source as feature-flags — surfaces can't diverge). Semantics: *effective*
  runtime entitlement, not submodule-on-disk; `TRINITY_OSS_ONLY=1` or a
  fully-failed registration → `"oss"`; partial registration → `"enterprise"`
  with the surviving modules listed. Handler imports the service
  function-locally (test-stub compatibility); `_build_version_payload` stays
  stdlib-pure with `edition`/`enterprise_features` threaded as parameters.

---

## 36. Build Info Surface (#926)

### 36.1 Version Chip + Git Commit Detail (#926)
- **Status**: 🚧 In Progress
- **Implements**: Issue #926
- **Description**: Operators need an in-app way to confirm which commit
  is actually deployed. Pre-#926, only the `VERSION` file (semver
  string) plus an optional `BUILD_DATE` env var were exposed via
  `GET /api/version`. Operators had to SSH or `docker inspect` to
  resolve "is my fix deployed?" — a recurring friction point during
  hotfixes and incident response. This surfaces git commit + branch
  metadata baked in at backend image build time.
- **Backend (`GET /api/version`)** — extended payload:
  ```json
  {
    "version": "0.9.0",
    "platform": "trinity",
    "edition": "oss",
    "enterprise_features": [],
    "components": { … },
    "runtimes": ["claude-code", "gemini-cli", "codex"],
    "build_date": "2026-05-25T14:00:00Z",
    "git_commit": "f1ba610fab…full sha…",
    "git_commit_short": "f1ba610f",
    "git_commit_subject": "review(#929): drop dead accessor…",
    "git_commit_timestamp": "2026-05-25T11:45:00+00:00",
    "git_branch": "dev",
    "voice_enabled": false
  }
  ```
  All new fields default to `"unknown"` when the build args are
  absent (local dev / volume-mount workflows). Endpoint stays
  JWT-authenticated (SEC-180).
- **Build wiring**:
  - `docker/backend/Dockerfile` accepts `GIT_COMMIT`,
    `GIT_COMMIT_SUBJECT`, `GIT_COMMIT_TIMESTAMP`, `GIT_BRANCH`,
    `BUILD_DATE` as `ARG`s and re-exports each as `ENV` so the
    runtime reads them via `os.getenv()`.
  - `docker-compose.yml` `backend.build.args` block forwards the
    `${GIT_COMMIT}` etc. shell vars from the environment so
    `docker compose build` picks them up automatically.
  - `scripts/deploy/start.sh` exports the args from the local repo
    before the build: `git rev-parse HEAD`, `git rev-parse --abbrev-ref HEAD`,
    `git log -1 --pretty=%s`, `git log -1 --pretty=%cI`, and
    `date -u +%Y-%m-%dT%H:%M:%SZ`.
- **Frontend**:
  - `NavBar.vue` renders a small muted version chip (e.g. `v0.9.0`).
    Click opens a modal with the full build-info block.
  - `Settings.vue` adds a "Build Info" subsection showing version,
    commit short SHA + full SHA, commit subject + ISO timestamp,
    branch, build date.
  - One-shot fetch on app mount via a `useBuildInfo()` composable
    that caches the response — build metadata never changes at runtime.
- **Out of scope**: per-component version drift (frontend vs
  backend), MCP server version surface (the MCP TypeScript
  package has its own `package.json` version), agent base-image
  commit metadata. Follow-ups if useful.

---
