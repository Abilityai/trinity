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
  - Marks stale activities (`activity_state='started'` > 120 min) as `failed`
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
- **Phase 2 (deferred)**: S-02, S-03, E-01, E-05, E-06, B-01, B-02,
  G-01, R-01 (per the catalog at
  `docs/testing/orchestration-invariant-catalog.md`). Each adds as a new
  file under `src/backend/canary/invariants/` and a registry entry; the
  service and API surface stay unchanged.

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
    "components": { … },
    "runtimes": ["claude-code", "gemini-cli"],
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
