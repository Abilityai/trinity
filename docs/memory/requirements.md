# Trinity - Autonomous Agent Orchestration Platform - Requirements

> **SINGLE SOURCE OF TRUTH** - All development must trace back to this document.
> Update this file BEFORE implementing any new feature.

## Vision

Trinity is autonomous agent orchestration and infrastructure — sovereign infrastructure for deploying, orchestrating, and governing fleets of autonomous AI agents on your own hardware.

| Capability | Description | Implementation Status |
|------------|-------------|----------------------|
| **Hierarchical Delegation** | Orchestrator-Worker with context quarantine | ✅ Agent-to-Agent via MCP |
| **Persistent Memory** | Virtual filesystems, memory folding | ✅ Chat persistence, file browser |
| **Agent Configuration** | Template system with CLAUDE.md and credential injection | ✅ Templates with CLAUDE.md |
| **Autonomous Operations** | Scheduling, monitoring, and fleet management | ✅ Cron scheduling, health monitoring |

## Status Labels
- ⏳ Not Started
- 🚧 In Progress
- ✅ Implemented
- ❌ Removed

---

## 1. Core Agent Management

### 1.1 Agent Creation
- **Status**: ✅ Implemented
- **Description**: Create agents from templates (GitHub or local) or from scratch
- **Key Features**: Web UI, REST API, GitHub templates (`github:Org/repo`), local templates, credential schema auto-detection

### 1.2 Agent Start/Stop Toggle
- **Status**: ✅ Implemented (Updated 2026-01-26)
- **Description**: Start and stop agent containers via unified toggle control
- **Key Features**: Toggle switch shows Running/Stopped state, loading spinner during action, consistent UI across Dashboard, Agents page, and Agent Detail page
- **Components**: `RunningStateToggle.vue` - Reusable toggle component with size variants (sm/md/lg)

### 1.3 Agent Rename (RENAME-001)
- **Status**: ✅ Implemented (2026-03-01)
- **Description**: Rename agents via UI or MCP without deleting and recreating
- **Key Features**: Inline editing with pencil icon, `rename_agent` MCP tool, atomic DB updates, Docker container rename, WebSocket broadcast
- **Restrictions**: System agents cannot be renamed, only owners/admins can rename
- **API**: `PUT /api/agents/{name}/rename` with `{new_name: string}`

### 1.4 Agent Deletion
- **Status**: ✅ Implemented
- **Description**: Delete agents and cleanup resources
- **Key Features**: Container cleanup, network cleanup, cascade delete sharing records

### 1.5 Agent Logs Viewing
- **Status**: ✅ Implemented
- **Description**: View container logs for debugging
- **Key Features**: Logs tab, fixed-height scrollable container, auto-refresh, smart auto-scroll

### 1.6 Agent Live Telemetry
- **Status**: ✅ Implemented
- **Description**: Real-time container metrics in agent header
- **Key Features**: CPU/memory usage, network I/O, uptime display, auto-refresh every 10 seconds

---

## 2. Authentication & Authorization

### 2.1 Email-Based Authentication
- **Status**: ✅ Implemented (2025-12-26, security-hardened 2026-03-26)
- **Description**: Passwordless email login with 6-digit verification codes
- **Key Features**: 2-step verification, admin-managed whitelist, auto-whitelist on agent sharing, rate limiting (IP-based + per-email OTP lockout after 5 failures)
- **Security**: OTP brute-force prevented by dual rate limits — `login_attempts:{ip}` (shared with admin login) and `otp_attempts:{email}` (max 5 failures → 10-min lockout). Both `POST /api/auth/email/verify` and `POST /api/public/verify/confirm` are protected. (pentest 3.1.5 / #176)
- **Flow**: `docs/memory/feature-flows/email-authentication.md`

### 2.2 Admin Password Login
- **Status**: ✅ Implemented
- **Description**: Password-based fallback for admin user
- **Key Features**: Bcrypt hashing, first-time setup wizard

### 2.3 Session Persistence
- **Status**: ✅ Implemented
- **Description**: User profile survives page refresh via localStorage JWT

### 2.4 Agent Sharing
- **Status**: ✅ Implemented
- **Description**: Share agents with team members
- **Key Features**: Share via email, access levels (Owner/Shared/Admin), sharing tab for owners

### 2.5 User Role Model (ROLE-001)
- **Status**: ✅ Implemented (2026-03-20)
- **Requirement ID**: ROLE-001
- **GitHub Issue**: #143
- **Description**: 4-tier role hierarchy (admin > creator > operator > user) with server-side enforcement via `require_role()` dependency factory
- **Key Features**:
  - Role hierarchy: `user` < `operator` < `creator` < `admin`
  - `require_role(min_role)` FastAPI dependency factory for endpoint protection
  - Agent creation restricted to `creator`+ role
  - Admin-only user management: `GET /api/users`, `PUT /api/users/{username}/role`
  - New email users default to `creator` role
  - Settings UI "User Management" section with role dropdowns
- **Database**: `role` column on `users` table (default `"user"`)
- **Flow**: `docs/memory/feature-flows/role-model.md`

### 2.6 Auth0 OAuth
- **Status**: ❌ Removed (2026-01-01)
- **Reason**: Auth0 SDK caused blank pages on HTTP LAN access. Email auth is simpler and works everywhere.

---

## 3. Credential Management

### 3.1 Manual Credential Entry
- **Status**: ✅ Implemented
- **Description**: Add credentials via UI form with name, value, service

### 3.2 OAuth2 Flows
- **Status**: ✅ Implemented
- **Description**: OAuth2 authentication for Google, Slack, GitHub, Notion
- **Key Features**: MCP-compatible credential normalization

### 3.3 Credential Hot-Reload
- **Status**: ✅ Implemented
- **Description**: Update credentials on running agents without restart
- **Key Features**: Hot-reload via UI paste, writes `.env` and regenerates `.mcp.json`

### 3.4 Bulk Credential Import
- **Status**: ✅ Implemented
- **Description**: Paste `.env`-style KEY=VALUE pairs with template selector

### 3.5 Credential Requirements Extraction
- **Status**: ✅ Implemented
- **Description**: Extract from `.mcp.json.template` and show configured vs missing status

---

## 4. Template System

### 4.1 Local Templates
- **Status**: ✅ Implemented
- **Description**: Auto-discovery from `config/agent-templates/`

### 4.2 GitHub Templates
- **Status**: ✅ Implemented
- **Description**: Clone via `github:Org/repo` format with PAT authentication

### 4.2.1 Admin-Configurable GitHub Templates (TMPL-001)
- **Status**: ✅ Implemented
- **Description**: Admin can configure which GitHub repos appear as agent templates via Settings UI. All metadata (display name, description, resources, MCP servers) is fetched from each repo's `template.yaml` via GitHub API (cached 10 min).
- **Key Features**: `config.py` holds default repo list (no metadata), `system_settings` table (`github_templates` key) stores admin overrides, `GET/PUT/DELETE /api/settings/github-templates` endpoints, Settings UI with add/remove/save/reset
- **Behavior**: `None` (key missing) = use defaults, `[]` = no GitHub templates, `[{...}]` = custom list. Admin-provided display_name overrides repo's template.yaml value.

### 4.3 Template Metadata
- **Status**: ✅ Implemented
- **Description**: Read template.yaml for display name, description, resources, credentials

---

## 5. Agent Chat & Terminal

### 5.1 Agent Terminal
- **Status**: ✅ Implemented (2025-12-25)
- **Description**: Browser-based xterm.js terminal with Claude Code TUI
- **Key Features**: PTY forwarding, mode toggle (Claude/Gemini/Bash), resize support
- **Flow**: `docs/memory/feature-flows/agent-terminal.md`

### 5.2 Chat via Backend API
- **Status**: ✅ Implemented
- **Description**: `/api/agents/{name}/chat` endpoint with stream-json output parsing

### 5.3 Conversation History
- **Status**: ✅ Implemented
- **Description**: Persistent chat history per agent stored in database

### 5.4 Context Window Tracking
- **Status**: ✅ Implemented
- **Description**: Token usage display (e.g., "45.5K / 200K") with color-coded progress bar

### 5.5 Session Cost Tracking
- **Status**: ✅ Implemented
- **Description**: Cumulative cost display across conversation

### 5.6 Authenticated Chat Tab
- **Status**: ✅ Implemented (2026-02-19)
- **Description**: Dedicated Chat tab in Agent Detail with simple bubble UI for authenticated users
- **Key Features**: Session selector dropdown, New Chat button, Dashboard activity tracking (uses `/task` endpoint), shared components with PublicChat
- **Spec**: `docs/requirements/AUTHENTICATED_CHAT_TAB.md`
- **Flow**: `docs/memory/feature-flows/authenticated-chat-tab.md`

### 5.7 Dynamic Thinking Status (THINK-001)
- **Status**: ✅ Implemented (2026-03-03, extended 2026-03-04)
- **Description**: Real-time status labels in Chat tab and Public Chat reflecting agent activity (replaces static "Thinking...")
- **Key Features**: SSE stream subscription, tool-name-to-label mapping, 500ms anti-flicker, 10s heartbeat timeout, async_mode task execution with session persistence
- **Scope**: Authenticated Chat tab + Public Chat links (both use async_mode + SSE streaming)
- **Spec**: `docs/requirements/DYNAMIC_THINKING_STATUS.md`
- **Flow**: `docs/memory/feature-flows/authenticated-chat-tab.md`

---

## 6. Activity Monitoring

### 6.1 Unified Activity Panel
- **Status**: ✅ Implemented
- **Description**: Real-time tool execution tracking with `--output-format stream-json --verbose`

### 6.2 Tool Chips with Counts
- **Status**: ✅ Implemented
- **Description**: Visual counts per tool type, sorted by frequency

### 6.3 Expandable Timeline
- **Status**: ✅ Implemented
- **Description**: List of all tool calls with timestamps and durations

### 6.4 Unified Activity Stream
- **Status**: ✅ Implemented (2025-12-02)
- **Description**: Centralized `agent_activities` table for all runtime activities
- **Flow**: `docs/memory/feature-flows/activity-stream.md`

---

## 7. MCP Server

### 7.1 Trinity MCP Server
- **Status**: ✅ Implemented
- **Description**: Agent orchestration via Model Context Protocol
- **Key Features**: FastMCP with Streamable HTTP, 62 tools, API key authentication
- **Flow**: `docs/memory/feature-flows/mcp-orchestration.md`

### 7.2 Per-User API Keys
- **Status**: ✅ Implemented
- **Description**: Generate, revoke, and track usage per key

### 7.3 MCP Execution Query Tools (MCP-007)
- **Status**: ✅ Implemented (2026-03-25)
- **Requirement ID**: MCP-007
- **GitHub Issue**: #19
- **Description**: MCP tools for querying execution history, polling async results, and monitoring agent activity
- **Key Features**: `list_recent_executions`, `get_execution_result`, `get_agent_activity_summary`; enables async polling pattern for agent-to-agent collaboration beyond 60s MCP timeout
- **Spec**: `docs/requirements/MCP_EXECUTION_QUERY_TOOLS.md`

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

---

## 9. Agent Collaboration

### 9.1 Agent-to-Agent Communication
- **Status**: ✅ Implemented (2025-11-29)
- **Description**: Agents communicate via Trinity MCP with agent-scoped API keys
- **Flow**: `docs/memory/feature-flows/agent-to-agent-collaboration.md`

### 9.2 Agent Permissions
- **Status**: ✅ Implemented (2025-12-10, Updated 2026-02-19)
- **Description**: Explicit permission model controlling which agents can call which
- **Key Features**: Permissions tab in UI, restrictive default (no auto-grant), explicit opt-in
- **Flow**: `docs/memory/feature-flows/agent-permissions.md`

### 9.3 Agent Shared Folders
- **Status**: ✅ Implemented (2025-12-13)
- **Description**: File-based collaboration via shared Docker volumes
- **Key Features**: Expose/consume toggles, permission-gated mounting
- **Flow**: `docs/memory/feature-flows/agent-shared-folders.md`

### 9.4 Collaboration Dashboard
- **Status**: ✅ Implemented (2025-12-02)
- **Description**: Real-time visual graph showing agents and animated connections
- **Key Features**: Vue Flow, draggable nodes, context progress bars, replay mode
- **Flow**: `docs/memory/feature-flows/agent-network.md`

### 9.5 Dashboard Timeline View
- **Status**: ✅ Implemented (2026-01-10)
- **Description**: Graph/Timeline mode toggle with execution visualization
- **Key Features**: Execution boxes (color-coded by trigger), collaboration arrows, live streaming
- **Flow**: `docs/memory/feature-flows/dashboard-timeline-view.md`

### 9.6 Replay Timeline Component
- **Status**: ✅ Implemented (2026-01-04)
- **Description**: Waterfall-style timeline visualization of agent activities
- **Key Features**: Zoom controls (50%-2000%), agent rows, activity bars, communication arrows
- **Flow**: `docs/memory/feature-flows/replay-timeline.md`

### 9.7 Task DAG System
- **Status**: ❌ Removed (2025-12-23)
- **Reason**: Individual agent planning deferred to orchestrator-level. Claude Code handles task management internally.

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

### 10.8 Persistent Task Backlog (BACKLOG-001)
- **Status**: ✅ Implemented (2026-04-13)
- **Requirement ID**: BACKLOG-001
- **GitHub Issue**: #260
- **Description**: Async `/task` requests that arrive at full parallel capacity now spill into a durable SQLite-backed FIFO backlog instead of returning HTTP 429. Queued items drain automatically when slots free via a `SlotService` release callback; 60s maintenance task expires stale rows and drains orphans after restart.
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

---

## 11. GitHub Integration

### 11.1 GitHub Sync
- **Status**: ✅ Implemented (2025-11-29, Updated 2026-02-28)
- **Description**: Two sync modes - Source (pull-only, default) and Working Branch (bidirectional)
- **Key Features**: Pull button, sync button, content folder gitignored, branch selection via URL syntax or parameter
- **Branch Selection** (GIT-002): URL syntax `github:owner/repo@branch` or explicit `source_branch` parameter in MCP create_agent tool
- **Flow**: `docs/memory/feature-flows/github-sync.md`

### 11.2 GitHub Repository Initialization
- **Status**: ✅ Implemented
- **Description**: Initialize GitHub sync for existing agents
- **Flow**: `docs/memory/feature-flows/github-repo-initialization.md`

### 11.3 Operator-Readable Conflict Diagnosis (S5)
- **Status**: ✅ Implemented (2026-04-19)
- **Description**: Replace the raw git stderr that previously leaked into `GitConflictModal` with structured classification and per-class operator copy so non-developers can understand what failed and what to do.
- **Key Features**:
  - `ConflictClass` enum (7 members: `AHEAD_ONLY`, `BEHIND_ONLY`, `PARALLEL_HISTORY`, `UNCOMMITTED_LOCAL`, `AUTH_FAILURE`, `WORKING_BRANCH_EXTERNAL_WRITE`, `UNKNOWN`) + pure `classify_conflict()` in both backend (`git_service.py`) and agent-server (`utils/git_conflict.py`) — the two runtimes don't share code.
  - 409 responses now carry `conflict_class` in body and an `X-Conflict-Class` header alongside the legacy `X-Conflict-Type`.
  - Frontend `COPY` lookup in `GitConflictModal.vue` renders per-class title/body/recommendation; raw git stderr lives inside an expandable `<details>` element.
  - Pre-S5 fallback preserved for older agent images that don't emit `conflict_class`.
- **GitHub Issue**: #386 (Epic #381)

### 11.4 Parallel-History Detection (S2)
- **Status**: ✅ Implemented (2026-04-19)
- **Description**: When the agent's working branch and the upstream pull-branch share no recent ancestor and both have diverging commits, render a different conflict modal that offers an "Adopt latest upstream (preserve my state)" recovery instead of the (always-wrong) Pull-First / Force-Push pair.
- **Key Features**:
  - `/api/git/status` returns `common_ancestor_sha`, `common_ancestor_age_days`, and `pull_branch` (label-agnostic copy).
  - Frontend `isParallelHistory` predicate: `(no common ancestor OR ancestor age ≥ 30 days) AND behind > 0`.
  - New sibling modal variant in `GitConflictModal.vue`; existing pull/push variants untouched.
  - Primary recovery button calls the S3 endpoint owned by #384 (deferred dependency).
- **GitHub Issue**: #385 (Epic #381)
- **Flow**: `docs/memory/feature-flows/github-sync.md`

### 11.5 Branch Ownership Enforcement (S7)
- **Status**: ✅ Implemented (2026-04-19)
- **Description**: Prevent silent data loss from two agents binding to the same `(github_repo, working_branch)` pair (2026-04-17 alpaca incident).
- **Key Features**:
  - Layer 0/1: single `reserve_and_generate_instance_id` helper atomically generates a UUID, probes `git ls-remote`, and inserts under the partial UNIQUE; retries 5x on collision.
  - Layer 2: partial UNIQUE `(github_repo, working_branch) WHERE source_mode = 0` on `agent_git_config`; migration refuses to install on existing duplicates so operators rebind first.
  - Layer 3: agent-server pushes with `git push --force-with-lease=<branch>:<expected-sha>`; lease rejection returns 409 + `X-Conflict-Type: branch_ownership_collision` and emits a structured alert into `~/.trinity/operator-queue.json` so the Operating Room surfaces the collision.
- **GitHub Issue**: #382 (Epic #381)
- **Flows**: `docs/memory/feature-flows/github-repo-initialization.md`, `docs/memory/feature-flows/github-sync.md`

### 11.6 Persistent-State Allowlist (S4)
- **Status**: ✅ Implemented (2026-04-19)
- **GitHub Issue**: #383 (primitive); consumer #384 (S3 reset-preserve-state, pending)
- **Description**: Named allowlist of workspace paths that must survive a template-level reset. Materialized to `.trinity/persistent-state.yaml` at agent creation so runtime sync/reset paths don't depend on the 10-minute `template.yaml` cache. Operator-editable per-agent.
- **Key Features**: Default five-pattern list (`workspace/**`, `.trinity/**`, `.mcp.json`, `.claude.json`, `.claude/.credentials.json`); per-template override via `persistent_state:` key; readers on backend and agent-server with default fallback
- **Scope**: Primitive only — the reset-preserve-state operation that consumes it lands in #384
- **Flow**: `docs/memory/feature-flows/persistent-state-allowlist.md`

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

---

## 13. Content & File Management

### 13.1 Per-Agent File Manager
- **Status**: ✅ Implemented (Updated 2026-03-03, Issue #51)
- **Description**: Full-featured file manager in AgentDetail Files tab with two-panel layout (tree + preview)
- **Key Features**: Tree view with search, image/video/audio/PDF/text preview, inline text editing, delete with protected path warnings, show hidden files toggle
- **Components**: Reuses `file-manager/FileTreeNode.vue` and `file-manager/FilePreview.vue`
- **Flow**: `docs/memory/feature-flows/file-browser.md`

### 13.2 File Manager Page (Standalone - Deprecated)
- **Status**: 🗄️ Deprecated (2026-03-03, Issue #51)
- **Description**: Former dedicated `/files` page replaced by per-agent Files tab. Route removed, component preserved.
- **Flow**: `docs/memory/feature-flows/file-manager.md`

### 13.3 Content Folder Convention
- **Status**: ✅ Implemented (2025-12-27)
- **Description**: `content/` directory gitignored by default for large generated assets

### 13.4 Agent Dashboard
- **Status**: ✅ Implemented (2026-01-12, Updated 2026-02-23)
- **Description**: Agent-defined dashboard via `dashboard.yaml` with widget system
- **Key Features**: 11 widget types (metric, status, progress, table, etc.), auto-refresh, historical tracking with sparklines (DASH-001), platform metrics injection
- **DASH-001 Enhancements** (2026-02-23):
  - Historical value tracking in `agent_dashboard_values` table
  - Sparkline charts showing metric trends
  - Trend indicators (up/down/stable with percentage)
  - Auto-injected platform metrics section (Tasks 24h, Success Rate, Cost, Health)
  - Query params: `include_history`, `history_hours`, `include_platform_metrics`
- **Flow**: `docs/memory/feature-flows/agent-dashboard.md`

### 13.5 Tasks Tab
- **Status**: ✅ Implemented
- **Description**: Unified task execution UI in Agent Detail page
- **Key Features**: Trigger manual tasks, monitor queue, view history, stop running tasks, make repeatable
- **Flow**: `docs/memory/feature-flows/tasks-tab.md`

### 13.6 Execution Log Viewer
- **Status**: ✅ Implemented
- **Description**: Modal for viewing Claude Code execution transcripts
- **Flow**: `docs/memory/feature-flows/execution-log-viewer.md`

### 13.7 Execution Detail Page
- **Status**: ✅ Implemented (2026-01-10)
- **Description**: Dedicated page for execution details with metadata, timestamps, transcript
- **Flow**: `docs/memory/feature-flows/execution-detail-page.md`

### 13.8 Live Execution Streaming
- **Status**: ✅ Implemented (2026-01-13), hardened (2026-03-13)
- **Description**: Real-time streaming of Claude Code execution logs to the Execution Detail page
- **Key Features**:
  - SSE streaming from agent server through backend proxy
  - Live log display with auto-scroll
  - "Live" indicator for running executions
  - "Live" button in TasksPanel (green pulsing badge) for running tasks
  - Stop button integration
  - Late joiner support (buffered entries)
  - Polling fallback when stream ends prematurely (race condition recovery)
  - Connect timeout on backend SSE proxy (prevents indefinite hang)
  - User-visible stream error banner with retry button
- **Spec**: `docs/requirements/LIVE_EXECUTION_STREAMING.md`

### 13.9 Continue Execution as Chat (EXEC-023)
- **Status**: ✅ Implemented (2026-02-20)
- **Priority**: MEDIUM
- **Description**: Resume failed or completed executions as interactive chat conversations with full context preservation
- **Key Features**:
  - Store Claude Code `session_id` in execution records
  - "Continue as Chat" button on Execution Detail page
  - Uses `--resume {session_id}` for native Claude Code session continuity
  - Full 150K+ token context available without copying/injection
  - Resume banner in Chat tab showing execution context
- **Spec**: `docs/requirements/CONTINUE_EXECUTION_AS_CHAT.md`

---

## 14. Multi-Runtime Support

### 14.1 Runtime Adapter Architecture
- **Status**: ✅ Implemented (2025-12-28)
- **Description**: Abstract interface for agent execution engines
- **Key Features**: ClaudeCodeRuntime, GeminiRuntime, factory function

### 14.2 Gemini CLI Integration
- **Status**: ✅ Implemented (2025-12-28)
- **Description**: Google's Gemini CLI as alternative runtime
- **Key Features**: Free tier, 1M token context, native Google Search

### 14.3 Runtime Configuration
- **Status**: ✅ Implemented (2025-12-28)
- **Description**: Runtime selection via `template.yaml` `runtime:` field
- **Schema**: `runtime: {type: claude-code|gemini-cli, model: string}`

---

## 15. Public Access

### 15.1 Public Agent Links
- **Status**: ✅ Implemented (2025-12-22)
- **Description**: Shareable public links for unauthenticated agent access
- **Key Features**: Optional email verification, rate limiting, usage tracking
- **Flow**: `docs/memory/feature-flows/public-agent-links.md`

### 15.1a Public Chat Session Persistence (PUB-005)
- **Status**: ✅ Implemented (2026-02-17)
- **Description**: Multi-turn conversation persistence for public chat links
- **Key Features**: Session management (email-based or anonymous), message history, New Conversation button, page refresh recovery, context injection for continuity
- **Flow**: `docs/memory/feature-flows/public-agent-links.md#public-chat-session-persistence-pub-005`

### 15.1a-2 Per-User Persistent Memory for Public Links (MEM-001)
- **Status**: ✅ Implemented (2026-03-19)
- **Requirement ID**: MEM-001
- **GitHub Issue**: #147
- **Description**: Email-verified public chat sessions maintain persistent per-user memory (text blob) scoped to `(agent_name, user_email)`, injected into every agent call and updated via background summarization every 5 messages.
- **Database Tables**: `public_user_memory`
- **Flow**: `docs/memory/feature-flows/public-agent-links.md#per-user-persistent-memory-mem-001`

### 15.1b Slack Integration for Public Links (SLACK-001)
- **Status**: ✅ Implemented (2026-02-25)
- **Requirement ID**: SLACK-001
- **Priority**: P1
- **Description**: Enable Slack as a delivery channel for public agent links. Users chat with agents via DMs to a Slack bot.
- **Key Features**:
  - DMs only (no channel @mentions in Phase 1)
  - One Slack workspace = one public link (simple 1:1 mapping)
  - Email verification for Slack users (auto-verify via Slack profile or email code)
  - Session persistence (reuse `public_chat_sessions` with `slack` identifier type)
  - OAuth flow for workspace connection
  - Signature verification for Slack events
- **Database Tables**:
  - `slack_link_connections` - Connects workspace to public link
  - `slack_user_verifications` - Tracks verified Slack users
  - `slack_pending_verifications` - In-progress email verifications
- **API Endpoints**:
  - `POST /api/public/slack/events` - Slack event receiver
  - `GET /api/public/slack/oauth/callback` - OAuth completion
  - `GET/DELETE /api/agents/{name}/public-links/{id}/slack` - Connection status
  - `POST /api/agents/{name}/public-links/{id}/slack/connect` - Initiate OAuth
- **Spec**: `docs/requirements/SLACK_INTEGRATION.md`
- **Flow**: `docs/memory/feature-flows/slack-integration.md`

### 15.1b-ii Channel Adapters + Multi-Agent Slack (SLACK-002)
- **Status**: ✅ Implemented (2026-03-23, updated 2026-03-26)
- **Requirement ID**: SLACK-002
- **Priority**: P1
- **Description**: Pluggable channel adapter abstraction for external messaging platforms. Extends SLACK-001 with multi-agent routing (multiple agents per workspace), @mention support in channels, thread continuity (reply-without-mention), and configurable operational limits.
- **Key Features**:
  - Channel-agnostic adapter pattern (`ChannelAdapter` base class) supporting Slack, future Telegram/Discord
  - `ChannelMessageRouter` — unified message pipeline: resolve agent → rate limit → verify → execute → respond
  - Multi-agent workspace: bind different agents to different Slack channels
  - @mention routing in channels + DM default agent
  - Thread tracking: bot auto-responds to thread replies without requiring @mention
  - Configurable rate limits, execution timeout, and allowed tools via `settings_service`
  - Periodic pruning of rate-limit buckets to prevent memory leaks
  - **Settings UI**: Socket Mode connect/disconnect, app token management, connection status badge
  - **Platform OAuth**: "Install to Workspace" from Settings page (no agent context needed)
  - **Per-agent channel binding**: Standalone UI in Sharing tab to create/unbind Slack channels per agent
- **Database Tables**:
  - `slack_workspaces` — Workspace connections (team_id, bot_token encrypted)
  - `slack_channel_agents` — Channel-to-agent bindings (multi-agent routing)
  - `slack_active_threads` — Active thread tracking (reply-without-mention)
- **Configurable Settings** (via Settings UI or DB):
  - `channel_rate_limit_max` — Messages per window (default: 30)
  - `channel_rate_limit_window` — Window in seconds (default: 60)
  - `channel_timeout_seconds` — Execution timeout (default: 120)
  - `channel_allowed_tools` — Comma-separated tool list (default: WebSearch,WebFetch)
- **API Endpoints (new in 2026-03-26)**:
  - `GET /api/settings/slack/status` — Transport connection state + workspace info
  - `POST /api/settings/slack/connect` — Save app token + start Socket Mode
  - `POST /api/settings/slack/disconnect` — Stop transport
  - `POST /api/settings/slack/install` — Platform-level OAuth (workspace install)
  - `GET /api/agents/{name}/slack/channel` — Channel binding status
  - `POST /api/agents/{name}/slack/channel` — Create channel + bind agent
  - `DELETE /api/agents/{name}/slack/channel` — Unbind agent
- **Flow**: `docs/memory/feature-flows/slack-channel-routing.md`

### 15.1b-iii Slack Inbound File Sharing (SLACK-FILES)
- **Status**: ✅ Implemented (2026-03-31)
- **Requirement ID**: SLACK-FILES
- **Priority**: P2
- **GitHub Issue**: #222
- **Extends**: SLACK-002
- **Description**: Slack users can upload files that agents process. Images embedded as base64 data URIs for Claude vision. Text files (CSV, JSON, TXT, MD, etc.) copied into per-session container directories via Docker `put_archive` API.
- **Key Features**:
  - `FileAttachment` model + `files` field on `NormalizedMessage` (channel-agnostic)
  - `ChannelAdapter.download_file()` — each channel implements own download auth
  - Images (`image/*`): base64 inline in prompt (Claude vision, 5MB/image, 10MB total)
  - Text files: copied to `/home/developer/uploads/{session_id}/`, `Read` tool added
  - Unsupported formats (PDF, ZIP, video, audio) rejected with user-friendly message
  - Filename sanitization (path traversal, hidden files, special chars)
  - File upload rate limit: 5 files/min per user
  - Per-session upload dirs cleaned after execution
  - `files:read` OAuth scope (requires Slack app reinstall)
  - Max 10 files per message
- **Future**: PDF support (text extraction), agent→Slack outbound file attachments (Phase 2)
- **Flow**: `docs/memory/feature-flows/slack-file-sharing.md`

### 15.1c Telegram Bot Integration (TGRAM-001)
- **Status**: ✅ Complete (2026-04-10)
- **Requirement ID**: TGRAM-001
- **Priority**: P2
- **Description**: Per-agent Telegram bot integration. Each agent gets its own Telegram bot (1:1 via @BotFather), shareable via `t.me/BotUsername` links. Reuses the `ChannelAdapter` abstraction proven by SLACK-002.
- **Key Features**:
  - Frontend UI: `TelegramChannelPanel.vue` in Agent Detail → Sharing tab (connect, verify, disconnect, webhook warning)
  - Per-agent bots (one bot per agent, token encrypted via AES-256-GCM)
  - Bidirectional text chat (users message bot → agent responds via HTML formatting)
  - Photo and document support (download, text extraction for plain text files)
  - Webhook mode (production) — returns 200 immediately, processes async
  - Webhook reconciliation on backend startup (re-registers all active bots)
  - Bot commands: `/start`, `/help`, `/reset` (clear conversation)
  - Message splitting at 4096 char limit (paragraph boundaries)
  - Telegram API 429 retry with `retry_after` backoff
  - No new Python dependencies (httpx only)
  - Router generalization: `ChannelMessageRouter` now uses adapter methods instead of Slack-specific hardcodings
- **Database Tables**:
  - `telegram_bindings` — Maps bots to agents (bot_id UNIQUE, bot_username, webhook_secret, telegram_secret_token, encrypted bot token)
  - `telegram_chat_links` — Maps Telegram users to sessions (binding_id, telegram_user_id, message_count)
- **API Endpoints**:
  - `POST /api/telegram/webhook/{webhook_secret}` — Receive Telegram updates (validated by X-Telegram-Bot-Api-Secret-Token header)
  - `GET /api/agents/{name}/telegram` — Bot binding status
  - `PUT /api/agents/{name}/telegram` — Configure bot token (validates via getMe)
  - `DELETE /api/agents/{name}/telegram` — Remove bot binding + delete webhook
  - `POST /api/agents/{name}/telegram/test` — Test bot connectivity / send test message
- **Security**:
  - Bot tokens AES-256-GCM encrypted at rest (same `CredentialEncryptionService` as Slack)
  - Webhook auth via `X-Telegram-Bot-Api-Secret-Token` header (set during setWebhook)
  - SSRF prevention: media downloads restricted to `api.telegram.org` domain
  - Restricted tools for Telegram users (WebSearch, WebFetch — same as Slack)
  - Update dedup via `last_update_id` tracking
  - Bot token values never logged
- **Flow**: `docs/memory/feature-flows/telegram-integration.md`
- **Future Phases**:
  - Phase 2: Voice transcription (Whisper API), notification forwarding
  - Phase 3: Inline keyboards for approve/reject, deep links with start parameters

### 15.1e Telegram Group Chat Support (TGRAM-GROUP)
- **Status**: ✅ Complete (2026-04-11)
- **Requirement ID**: TGRAM-GROUP
- **Priority**: P2
- **Description**: Agents participate in Telegram group chats. Bots respond to @mentions and direct replies in groups. Per-group configuration for trigger mode and welcome messages.
- **Key Features**:
  - Group message handling: respond to @mentions of bot username in message entities
  - Reply-to-bot detection: respond when user replies to bot's own messages
  - Configurable trigger mode per group: "mention" (default, @mention-only), "all" (all messages), or "observe" (all messages but agent can return `[NO_REPLY]` to skip responding)
  - Sender identity context in group messages: `[Group: title] [From: @username (Name)]` prefix so agent knows who is speaking
  - New member welcome messages: configurable text with {name} placeholder
  - Auto-created group configs on first interaction (no manual setup required)
  - Bot added/removed from group detection via `my_chat_member` update events
  - User join/leave detection via `chat_member` events (requires bot admin in group)
  - Fresh context per group message (no prior session history to prevent context bleed)
  - Silent rate limit drops in groups (no error messages visible to all members)
  - Mention text stripped from agent input for cleaner prompts
  - Commands support @botname suffix in groups (e.g., `/help@mybot`)
  - Uses modern Telegram `reply_parameters` API for threaded replies
- **Database Tables**:
  - `telegram_group_configs` — Per-group settings (binding_id, chat_id, trigger_mode, welcome_enabled, welcome_text, is_active)
- **API Endpoints**:
  - `GET /api/agents/{name}/telegram/groups` — List group configs
  - `PUT /api/agents/{name}/telegram/groups/{id}` — Update group config (trigger mode, welcome settings)
  - `DELETE /api/agents/{name}/telegram/groups/{id}` — Remove group config (deactivates)
  - `POST /api/agents/{name}/telegram/groups/{chat_id}/messages` — Proactive group messaging (rate limited: 10/hr/group, 100/hr/agent)
- **MCP Tools**:
  - `list_channel_groups` — List Telegram groups the agent is connected to
  - `send_group_message` — Send proactive message to a group (supports Telegram HTML, max 4096 chars)
- **Webhook Changes**:
  - `allowed_updates` now includes `["message", "my_chat_member", "chat_member"]`
- **Security**:
  - Same webhook auth (URL secret + header token) — no new attack surface
  - Group membership not re-verified per message (documented limitation)
  - Bot loop prevention inherited from TGRAM-001 (`is_bot` check)
- **Frontend**: TelegramChannelPanel extended with group list, trigger mode radio, welcome message config
- **Flow**: `docs/memory/feature-flows/telegram-integration.md`

### 15.1d Public Chat Session Memory (PUB-006)
- **Status**: ⏳ Not Started
- **Requirement ID**: PUB-006
- **Priority**: P2
- **Description**: Optional per-session persistent memory for public link users. When enabled, the agent accumulates structured knowledge about a user across the entire session lifetime — beyond simple message history replay — so it can recall facts, preferences, and context without re-reading every prior message.
- **Problem Statement**:
  - Currently, context is injected by concatenating the last N messages (max 10 turns) into the prompt. This breaks for long conversations (context window pressure) and loses facts from early in the session.
  - There is no mechanism for the agent to "remember" key facts it learned (e.g., user's name, goals, constraints) independent of raw message replay.
- **Key Features**:
  - **Opt-in per public link**: Toggle `memory_enabled` on `agent_public_links` (default: `false`). The public link creation/edit UI exposes this setting.
  - **Session metadata store**: Agent can write a structured JSON blob (`session_context`) to the session record after each turn — capturing facts, preferences, and a rolling summary of the conversation.
  - **Smart context injection**: When memory is enabled, the context prompt is built from (a) `session_context` (agent-written facts) + (b) recent messages (last 5 turns), instead of the current flat 10-turn replay. This keeps prompts shorter and more focused.
  - **Agent write-back endpoint**: `POST /api/public/session/{token}/memory` — accepts `session_context` JSON from the agent container after execution completes. The backend calls this automatically by passing the agent's response through a structured extraction step, OR the agent itself calls it via an injected tool.
  - **Session summary**: After every N turns (configurable, default 5), the backend triggers a lightweight summarization call to condense prior messages into `session_context`, freeing up context window space.
  - **No extra infra**: Uses existing `public_chat_sessions` table (add `session_context TEXT` column and `memory_enabled` on links table). No vector DB required in Phase 1.
- **Database Changes**:
  - `agent_public_links`: add `memory_enabled INTEGER DEFAULT 0`
  - `public_chat_sessions`: add `session_context TEXT` (JSON), `summarized_through_message_id TEXT`
- **API Endpoints**:
  - `PUT /api/agents/{name}/public-links/{id}` — extend to include `memory_enabled` toggle (reuse existing link update endpoint)
  - `POST /api/public/session/{token}/memory` — agent or backend writes updated `session_context`; requires valid `session_id` query param; no external auth (link token is sufficient)
  - `GET /api/public/history/{token}` — extend response to include `session_context` when memory is enabled
- **Context Prompt Format (memory enabled)**:
  ```
  ### Session Context (what I know about this user)
  {session_context JSON rendered as bullet list}

  ### Recent conversation (last 5 turns)
  User: ...
  Assistant: ...

  ### Current message
  User: {new_message}
  ```
- **Context Prompt Format (memory disabled)** — unchanged from current behavior (last 10 turns flat replay).
- **Summarization Trigger**: After every 5th user message, if `memory_enabled`, the backend schedules a background task that:
  1. Fetches all messages not yet covered by `summarized_through_message_id`
  2. Calls the agent with a special prompt asking it to extract structured facts and update `session_context`
  3. Saves the updated `session_context` and advances `summarized_through_message_id`
- **Frontend Changes**:
  - Public link settings panel: add "Session Memory" toggle with tooltip explaining behavior
  - No changes to `PublicChat.vue` — transparent to end users
- **Architecture Notes**:
  - Phase 1: Structured JSON blob in SQLite (no vector DB, no embeddings). Sufficient for most agent-user sessions.
  - Phase 2 (future): Embed messages and do semantic retrieval for very long sessions (>50 turns). Out of scope for PUB-006.
  - Memory is scoped to a single `public_chat_session` — there is no cross-session or cross-link memory.
  - Anonymous sessions have the same memory capability as email-verified sessions.

### 15.2 First-Time Setup
- **Status**: ✅ Implemented (2025-12-23)
- **Description**: Admin password wizard on fresh install
- **Key Features**: Bcrypt hashing, API key configuration in Settings
- **Flow**: `docs/memory/feature-flows/first-time-setup.md`

### 15.3 Per-Agent API Key Control
- **Status**: ✅ Implemented (2025-12-26)
- **Description**: Agents can use platform API key or user's own Claude subscription
- **Key Features**: Toggle in Terminal tab, container recreation on change

---

## 16. Advanced Features

### 16.1 Agent Resource Allocation
- **Status**: ✅ Implemented (2026-01-02)
- **Description**: Per-agent memory and CPU configuration
- **Flow**: `docs/memory/feature-flows/agent-resource-allocation.md`

### 16.1a Read-Only Mode
- **Status**: ✅ Implemented (2026-02-17)
- **Description**: Per-agent code protection preventing modification of source files
- **Key Features**: Toggle in AgentHeader, PreToolUse hooks intercept Write/Edit/NotebookEdit, blocked patterns (*.py, *.js, etc.), allowed patterns (output/*, content/*)
- **Flow**: `docs/memory/feature-flows/read-only-mode.md`
- **Spec**: `docs/requirements/READ_ONLY_MODE.md`

### 16.2 SSH Access
- **Status**: ✅ Implemented (2026-01-02)
- **Description**: Ephemeral SSH credentials via MCP tool (admin-only)
- **Key Features**: ED25519 keys, configurable TTL, ops setting controlled, admin-only access
- **Flow**: `docs/memory/feature-flows/ssh-access.md`

### 16.3 Agent Info Display
- **Status**: ✅ Implemented
- **Description**: Template metadata display in Info tab
- **Flow**: `docs/memory/feature-flows/agent-info-display.md`

### 16.4 Parallel Headless Execution
- **Status**: ✅ Implemented (2025-12-22)
- **Description**: Stateless parallel task execution via `POST /task` endpoint
- **Key Features**: Bypasses queue, enables orchestrator-worker patterns
- **Flow**: `docs/memory/feature-flows/parallel-headless-execution.md`

### 16.5 System Manifest Deployment
- **Status**: ✅ Implemented (2025-12-18)
- **Description**: Recipe-based multi-agent deployment via YAML manifest
- **Key Features**: Permission presets, shared folders, schedules, auto-start
- **Flow**: `docs/memory/feature-flows/system-manifest.md`

### 16.6 Local Agent Deployment via MCP
- **Status**: ✅ Implemented
- **Description**: Deploy local agents via MCP tool
- **Flow**: `docs/memory/feature-flows/local-agent-deploy.md`

### 16.7 Agents Page UI
- **Status**: ✅ Implemented (2026-01-09)
- **Description**: Grid layout with Dashboard parity for Agents list page
- **Key Features**: 3-column grid, autonomy toggle, execution stats, context bar
- **Flow**: `docs/memory/feature-flows/agents-page-ui-improvements.md`

### 16.8 Dark Mode / Theme Switching
- **Status**: ✅ Implemented (2025-12-14)
- **Description**: Client-side theme system with Light/Dark/System modes
- **Key Features**: localStorage persistence, Tailwind class strategy
- **Flow**: `docs/memory/feature-flows/dark-mode-theme.md`

### 16.9 Events Page UI
- **Status**: ✅ Implemented (2026-02-20)
- **Description**: Dedicated page for viewing and managing agent notifications
- **Key Features**: Filter controls (status, priority, agent, type), stats cards, notification cards with actions, bulk selection, real-time WebSocket updates, navigation badge
- **Spec**: `docs/requirements/EVENTS_PAGE_UI.md`
- **Flow**: `docs/memory/feature-flows/events-page.md`

---

## 17. Planned Features

### 17.1 Horizontal Agent Scalability
- **Status**: ⏳ Not Started
- **Priority**: High
- **Description**: Agent pools with N instances for parallel workloads
- **Key Concepts**: Pool configuration, load balancing, auto-scaling triggers

### 17.2 Agent Event Subscriptions (EVT-001)
- **Status**: ✅ Implemented (2026-03-26)
- **Priority**: High (P1)
- **Description**: Lightweight SQLite-backed pub/sub for inter-agent event pipelines
- **Key Features**:
  - MCP tool `emit_event(event_type, payload)` — agents emit named events with structured data
  - CRUD API for event subscriptions (source agent, event type, message template)
  - Subscription trigger: matching event → async task to subscriber with `{{payload.field}}` interpolation
  - Permission-gated: uses existing `agent_permissions` — subscriber must be permitted to call source
  - Events persisted to `agent_events` table, subscriptions to `agent_event_subscriptions`
  - WebSocket broadcast for real-time event visibility
  - MCP tools: `emit_event`, `subscribe_to_event`, `list_event_subscriptions`, `delete_event_subscription`
- **GitHub Issue**: #169
- **Relationship to 17.2 (Redis Streams)**: This is a pragmatic first step. If Redis Streams (#22) lands later, subscriptions can migrate.

### 17.3 Event Handlers & Reactions
- **Status**: ✅ Partially Implemented via EVT-001 (2026-03-26)
- **Priority**: High
- **Description**: Configure automatic agent reactions to events
- **Key Concepts**: Event matching with filters, debouncing/throttling
- **Note**: Basic event → task triggering implemented in EVT-001. Advanced filtering, debouncing, and throttling are future enhancements.

### 17.4 Async MCP Chat Commands
- **Status**: ✅ Implemented (2026-01-30)
- **Priority**: High
- **Description**: Non-blocking MCP `chat_with_agent` for parallel multi-agent orchestration
- **Key Features**: `async=true` parameter (requires `parallel=true`), returns `execution_id` immediately, poll `GET /api/agents/{name}/executions/{id}` for results
- **Use Case**: Orchestrator sends tasks to 5 worker agents simultaneously, collects results as they complete

### 17.5 Fan-Out Parallel Self-Invocation (FANOUT-001)
- **Status**: ✅ Implemented
- **Description**: Dispatch N independent tasks to an agent in parallel, collect results with per-task status
- **Key Features**: `POST /api/agents/{name}/fan-out` endpoint, `fan_out` MCP tool, configurable `max_concurrency` (1-10, default 3), overall deadline with per-task timeout, best-effort policy (partial results on failure), dedicated fan-out concurrency (doesn't starve normal operations)
- **Use Case**: Agent self-invocation for batch predictions, parallel analysis, ensemble methods — each subtask gets a fresh context window
- **Execution Tracking**: All subtasks follow standard `TaskExecutionService` path — visible on dashboard with full observability (cost, tokens, logs), linked by `fan_out_id`
- **Limits**: Max 50 tasks per fan-out, max 10 concurrency, timeout 10-3600s, task IDs must be unique alphanumeric (max 64 chars)
- **Flow**: `docs/memory/feature-flows/fan-out.md`

### 17.6 Automated Git Sync
- **Status**: ⏳ Not Started
- **Priority**: Medium
- **Description**: Sync modes - Manual / Scheduled / On Stop

### 17.7 Automated Secret Rotation
- **Status**: ⏳ Not Started
- **Priority**: Medium
- **Description**: Automatic credential rotation with notifications

### 17.8 Kubernetes Deployment
- **Status**: ⏳ Not Started
- **Priority**: Low
- **Description**: Helm charts, StatefulSet for agents

---

## 18. Process Engine (Business Process Orchestration)

> **Design Documents**: `docs/PROCESS_DRIVEN_PLATFORM/`
> **Feature Flows**: `docs/memory/feature-flows/process-engine/`

### 18.1 Process Definition & Storage
- **Status**: ✅ Implemented (2026-01-16)
- **Description**: YAML-based process definitions with validation and versioning
- **Key Features**: JSON schema validation, semantic validation, version management
- **Flow**: `docs/memory/feature-flows/process-engine/process-definition.md`

### 18.2 Process Execution Engine
- **Status**: ✅ Implemented (2026-01-16)
- **Description**: Core engine orchestrating step execution with state machine
- **Key Features**: Step handlers, dependency resolution, parallel branches, retry logic
- **Flow**: `docs/memory/feature-flows/process-engine/process-execution.md`

### 18.3 Process Monitoring
- **Status**: ✅ Implemented (2026-01-16)
- **Description**: Real-time execution monitoring via WebSocket events
- **Key Features**: Live step progress, execution timeline, breadcrumb navigation
- **Flow**: `docs/memory/feature-flows/process-engine/process-monitoring.md`

### 18.4 Human Approval Gates
- **Status**: ✅ Implemented (2026-01-16)
- **Description**: Human-in-the-loop approval steps within processes
- **Key Features**: Approval inbox, timeout handling, decision tracking
- **Flow**: `docs/memory/feature-flows/process-engine/human-approval.md`

### 18.5 Process Scheduling
- **Status**: ✅ Implemented (2026-01-16)
- **Description**: Cron-based schedule triggers and timer steps
- **Key Features**: Cron presets, timezone support, timer delays
- **Flow**: `docs/memory/feature-flows/process-engine/process-scheduling.md`

### 18.6 Process Analytics & Cost Tracking
- **Status**: ✅ Implemented (2026-01-16)
- **Description**: Metrics, trends, and cost threshold alerts
- **Key Features**: Success rates, duration trends, cost aggregation, alerts
- **Flow**: `docs/memory/feature-flows/process-engine/process-analytics.md`

### 18.7 Sub-Processes
- **Status**: ✅ Implemented (2026-01-16)
- **Description**: Calling other processes as steps with parent-child linking
- **Key Features**: Input mapping, output capture, breadcrumb navigation
- **Flow**: `docs/memory/feature-flows/process-engine/sub-processes.md`

### 18.8 Agent Roles (EMI Pattern)
- **Status**: ✅ Implemented (2026-01-16)
- **Description**: Executor/Monitor/Informed role assignments for steps
- **Key Features**: Role matrix UI, InformedAgentNotifier, NDJSON event persistence
- **Flow**: `docs/memory/feature-flows/process-engine/agent-roles-emi.md`

### 18.9 Process Templates
- **Status**: ✅ Implemented (2026-01-16)
- **Description**: Bundled and user-created process templates
- **Key Features**: Template selector, category filtering, user templates
- **Flow**: `docs/memory/feature-flows/process-engine/process-templates.md`

### 18.10 Step Types
The Process Engine supports six step types:

| Step Type | Description | Handler |
|-----------|-------------|---------|
| `agent_task` | Execute task via AI agent | `AgentTaskHandler` |
| `human_approval` | Pause for human decision | `HumanApprovalHandler` |
| `gateway` | Conditional branching | `GatewayHandler` |
| `timer` | Delay execution | `TimerHandler` |
| `notification` | Send notifications | `NotificationHandler` |
| `sub_process` | Call another process | `SubProcessHandler` |

---

## 19. Future Vision

### 19.1 Human-in-the-Loop Improvement
- **Status**: ⏳ Concept Phase
- **Description**: Feedback collection and continuous improvement of agent behavior

### 19.2 Compliance-Ready Methodology
- **Status**: ⏳ Concept Phase
- **Description**: SOC-2 and ISO 27001-compatible development practices
- **Location**: `dev-methodology-template/`

### 19.3 Process Designer UI
- **Status**: ⏳ Concept Phase
- **Description**: Visual drag-and-drop process builder
- **Note**: Currently using YAML editor with live preview

---

## 20. Security & Compliance

### 20.1 Audit Trail System (SEC-001)
- **Status**: 🚧 Phase 1 implemented (2026-04-14, Issue #20). Phases 2–4 pending.
- **Requirement ID**: SEC-001
- **Priority**: HIGH
- **Description**: Comprehensive audit logging for all user and agent actions with full actor attribution. Enables investigation, compliance reporting, and accountability.
- **Key Features**:
  - Append-only `audit_log` table with immutability triggers (UPDATE blocked unconditionally; DELETE blocked within 365-day retention)
  - Full actor attribution (user, agent, MCP client, system)
  - MCP API key tracking per tool call
  - Hash chain for tamper evidence (Phase 4 — code present but dormant in Phase 1)
  - Query API with filters, pagination, and stats aggregation
  - Distinct from Process Engine audit (`audit_entries`) — coexist intentionally
- **Phase 1 Delivery (#20, this PR)**:
  - `audit_log` table + indexes + immutability triggers (`db/schema.py`, migration #31)
  - `PlatformAuditOperations` (`db/audit.py`)
  - `PlatformAuditService` with global instance (`services/platform_audit_service.py`)
  - Admin query API: `GET /api/audit-log`, `GET /api/audit-log/stats`, `GET /api/audit-log/{event_id}`
  - 29 unit tests (schema, query, filters, pagination, immutability, service actor resolution, error handling, lifecycle integration shape)
- **Phase 2a Delivery (#20, same PR — agent lifecycle smoke test)**:
  - `routers/agents.py` emits audit rows after successful create / start / stop / delete
  - 5 integration-shape tests asserting the exact field layout produced by the handlers
  - End-to-end verifiable: UI agent action → row appears in `/api/audit-log`
- **Event Categories**:
  - `AGENT_LIFECYCLE`: create, start, stop, delete, recreate
  - `EXECUTION`: task_triggered, chat_started, schedule_triggered
  - `AUTHENTICATION`: login_success, login_failed, logout
  - `AUTHORIZATION`: permission_grant, permission_revoke, share, unshare
  - `CONFIGURATION`: settings_change, resource_limits
  - `CREDENTIALS`: create, delete, reload
  - `MCP_OPERATION`: tool_call, key_create, key_revoke
  - `GIT_OPERATION`: sync, pull, init, commit
  - `SYSTEM`: startup, shutdown, emergency_stop
- **Architecture**: `docs/requirements/AUDIT_TRAIL_ARCHITECTURE.md`
- **Flow**: `docs/memory/feature-flows/audit-trail.md`
- **Implementation Phases**:
  1. ✅ Core infrastructure (table, service, db ops, API, tests) — landed in this PR
  2a. ✅ Agent lifecycle integration (create / start / stop / delete) — landed in this PR as smoke test
  2b. ⏳ Remaining backend integration (auth, sharing, settings, credentials, rename, request_id middleware)
  3. ⏳ MCP integration (TypeScript audit logging for tool calls)
  4. ⏳ Advanced features (hash chain verification, CSV/JSON export, retention automation, admin UI)

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

---

## Non-Functional Requirements

### Performance
- Agent creation: < 30 seconds
- Chat response: < 5 seconds for simple queries
- UI responsiveness: < 100ms for interactions
- WebSocket latency: < 500ms for status updates
- **Task list loading: < 1 second** (PERF-001)

### PERF-001: Task List Performance Optimization
- **Status**: ✅ Implemented (2026-02-21)
- **Requirement ID**: PERF-001
- **Priority**: MEDIUM
- **Description**: Optimize task loading on Agent Detail Tasks tab. Current implementation transfers full `execution_log` (100KB+ per execution) for list views that don't display it.
- **Key Features**:
  - Lightweight `ExecutionSummary` response model excluding heavy fields
  - Optimized SQL query selecting only needed columns
  - Composite index on `(agent_name, started_at DESC)`
  - On-demand detail loading when user expands a task
- **Achieved Impact**: 50-100x reduction in data transfer (10MB → 100KB)
- **Spec**: `docs/requirements/TASK_LIST_PERFORMANCE.md`

### Security
- All credentials encrypted at rest (Redis)
- HTTPS in production (Let's Encrypt)
- Container isolation (network, filesystem)
- Comprehensive audit logging via audit_log table (SEC-001)
- **Encryption key endpoint admin-only** (C-001, 2026-03-09)
- **WebSocket authentication required before accept** (C-002, 2026-03-09; hardened #178, 2026-03-27 — reject before `websocket.accept()`, removed first-message auth)
- **Internal API shared-secret auth** (C-003, 2026-03-09)
- **Agent access control on chat/credential endpoints** (M-004/M-006, 2026-03-09)
- **DOMPurify XSS protection on all v-html** (H-005, 2026-03-09)

### Scalability
- Support 50+ concurrent agents
- Multiple backend workers (uvicorn)
- Stateless backend (Docker as truth)

### Reliability
- Agent state survives backend restart
- SQLite persists across container recreation
- Redis AOF for secret durability

### Testing
- Feature flows include Testing sections
- Manual testing before marking complete
- Automated tests for critical paths only

---

## 21. Skills Management (GitHub-Based)

> **Simplified Design**: GitHub repository as the single source of truth for skills.
> No custom version control, no Docker volumes, no complex infrastructure.
> Spec: `docs/requirements/SKILLS_MANAGEMENT.md`

### 21.1 GitHub Skills Library
- **Status**: ⏳ Not Started
- **Description**: Platform syncs skills from a GitHub repository
- **Key Features**:
  - Configure library URL in Settings (admin)
  - `git clone/pull` to local `/data/skills-library/`
  - Auto-sync options: on-demand, on agent start, hourly, daily
  - Uses existing GitHub PAT for private repos

### 21.2 Skill Types (by Convention)
- **Status**: ⏳ Not Started
- **Description**: Three skill types via naming convention
- **Types**:
  - `policy-*` — Always active rules (e.g., `policy-code-review`)
  - `procedure-*` — Step-by-step instructions (e.g., `procedure-incident-response`)
  - (no prefix) — Methodologies/guidance (e.g., `verification`, `tdd`)

### 21.3 Agent Skill Assignment
- **Status**: ⏳ Not Started
- **Description**: Assign specific skills to individual agents
- **Key Features**:
  - Checkbox list in Agent Detail → Skills tab
  - Database stores assignments only (`agent_skills` table)
  - Bulk save via PUT `/api/agents/{name}/skills`

### 21.4 Skill Injection (Copy-Based)
- **Status**: ⏳ Not Started
- **Description**: Copy assigned skills to agent's `~/.claude/skills/` directory
- **Key Features**:
  - Copy on agent start (not symlinks)
  - Manual "Inject to Agent" button for running agents
  - "Sync All Agents" admin action after library update

### 21.5 MCP Tools (Simplified)
- **Status**: ⏳ Not Started
- **Description**: 4 essential MCP tools (reduced from 10)
- **Tools**:
  - `list_skills` — List available skills from library
  - `get_skill` — Get skill content
  - `assign_skill_to_agent` — Assign skill
  - `sync_agent_skills` — Re-inject skills to running agent
- **Removed**: create/update/delete (use GitHub), execute_procedure (use scheduling)

---

## 22. Playbooks Tab (Agent Local Skills)

> **Design**: Browse and invoke agent's local skills directly from UI.
> Spec: `docs/requirements/PLAYBOOKS_TAB.md`
> Flow: `docs/memory/feature-flows/playbooks-tab.md`

### 22.1 Playbooks Tab
- **Status**: ✅ Implemented (2026-02-27)
- **Requirement ID**: PLAYBOOK-001
- **Description**: UI tab to view and invoke agent's local `.claude/skills/` directory
- **Key Features**:
  - Grid display of skills parsed from SKILL.md YAML frontmatter
  - One-click run (sends `/{skill-name}` to `/task` endpoint)
  - Run with instructions (prefills Tasks tab input)
  - Search/filter by name or description
  - Automation badge (autonomous/gated/manual)
- **Agent Endpoint**: `GET /api/skills` - Lists skills from `.claude/skills/`
- **Backend Proxy**: `GET /api/agents/{name}/playbooks`
- **Frontend**: `PlaybooksPanel.vue` component

### 22.2 Skills Tab (Platform Library) - Hidden
- **Status**: ✅ Implemented (hidden)
- **Description**: Platform-level skill library assignment (existing feature)
- **Change**: Tab hidden from visibleTabs but component preserved for potential admin-only access

---

## 23. Nevermined Payment Integration (x402 Protocol)

> **Design**: Per-agent monetization via Nevermined x402 payment protocol.
> Spec: `docs/requirements/NEVERMINED_PAYMENT_INTEGRATION.md`
> Flow: `docs/memory/feature-flows/nevermined-payments.md`

### 23.1 Backend Foundation
- **Status**: ✅ Implemented (2026-03-04)
- **Requirement ID**: NVM-001
- **Description**: Database schema, encrypted credential storage, payment logging
- **Key Features**:
  - `nevermined_agent_config` table with AES-256-GCM encrypted NVM_API_KEY
  - `nevermined_payment_log` audit trail for verify/settle/reject actions
  - Migration #23 (idempotent)
  - `NeverminedOperations` DB module following subscription pattern

### 23.2 Payment Service
- **Status**: ✅ Implemented (2026-03-04)
- **Requirement ID**: NVM-001
- **Description**: Verify/settle lifecycle via payments-py SDK
- **Key Features**:
  - `NeverminedPaymentService` with lazy SDK imports (`NEVERMINED_AVAILABLE` flag)
  - `build_402_response()` using SDK's `build_payment_required()` helper
  - `verify_payment()` — 15s timeout, wrapped in `asyncio.to_thread()`
  - `settle_payment()` — 30s timeout, 3 retries with exponential backoff
  - Graceful degradation: 501 if SDK not installed

### 23.3 Paid Chat Endpoint
- **Status**: ✅ Implemented (2026-03-04)
- **Requirement ID**: NVM-001
- **Description**: Public x402 endpoint for external callers
- **Endpoints**:
  - `POST /api/paid/{agent_name}/chat` — 402/403/200 flow
  - `GET /api/paid/{agent_name}/info` — Public agent info + payment requirements
- **Flow**: No header → 402; invalid token → 403; valid → verify → execute → settle → receipt

### 23.4 Admin Configuration
- **Status**: ✅ Implemented (2026-03-04)
- **Requirement ID**: NVM-001
- **Description**: Authenticated CRUD for Nevermined config
- **Endpoints**:
  - `POST/GET/DELETE /api/nevermined/agents/{name}/config`
  - `PUT /api/nevermined/agents/{name}/config/toggle`
  - `GET /api/nevermined/agents/{name}/payments`
  - `GET /api/nevermined/settlement-failures` (admin)
  - `POST /api/nevermined/retry-settlement/{log_id}` (admin)

### 23.5 Frontend UI
- **Status**: ✅ Implemented (2026-03-04)
- **Description**: Payments tab in Agent Detail page
- **Component**: `NeverminedPanel.vue`
- **Features**: Config form, enable/disable toggle, paid endpoint URL display, payment log table

### 23.6 MCP Tools
- **Status**: ✅ Implemented (2026-03-04)
- **Description**: 4 MCP tools for Nevermined management
- **Tools**: `configure_nevermined`, `get_nevermined_config`, `toggle_nevermined`, `get_nevermined_payments`

---

## 24. Platform Image Generation (IMG-001)

> **Design**: Platform-level image generation service using Gemini. Two-step pipeline:
> prompt refinement (Gemini 2.5 Flash text) + image generation (Gemini 2.5 Flash Image).

### 24.1 Image Generation Service
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: IMG-001
- **Description**: Core service for generating images from text prompts
- **Key Features**:
  - Two-step pipeline: prompt refinement → image generation
  - Use-case-specific best practices (general, thumbnail, diagram, social)
  - Configurable aspect ratios (1:1, 16:9, 9:16, 4:3, 3:4, 3:2, 2:3)
  - Optional prompt refinement bypass
  - Singleton pattern, httpx async client
- **Config**: `GEMINI_API_KEY` environment variable

### 24.2 REST Endpoints
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: IMG-001
- **Description**: REST API for image generation
- **Endpoints**:
  - `POST /api/images/generate` — Generate image from prompt (JWT required)
  - `GET /api/images/models` — List available models and options (JWT required)

### 24.3 Future: MCP Tools
- **Status**: ⏳ Not Started
- **Description**: MCP tools for agents to generate images

### 24.4 Future: Frontend UI
- **Status**: ⏳ Not Started
- **Description**: UI for image generation in agent detail or standalone page

---

## 25. AI-Generated Agent Avatars (AVATAR-001)

> **Design**: AI-generated circular avatars for agents using the existing Gemini image generation service.
> Users provide an identity prompt, the platform generates a consistent avatar cached on disk.

### 25.1 Avatar Generation
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: AVATAR-001
- **Description**: Generate agent avatars from identity prompts using Gemini image service
- **Key Features**:
  - Identity prompt stored in DB (avatar_identity_prompt column)
  - Avatar use case in image generation prompts (optimized for circular crop, bold colors, digital illustration)
  - PNG cached at /data/avatars/{agent_name}.png
  - Cache-busting via avatar_updated_at timestamp

### 25.2 Avatar REST API
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: AVATAR-001
- **Endpoints**:
  - `GET /api/agents/{name}/avatar` — Serve cached PNG (JWT, access check)
  - `GET /api/agents/{name}/avatar/identity` — Get identity prompt + metadata (JWT, access check)
  - `POST /api/agents/{name}/avatar/generate` — Generate avatar (JWT, owner only)
  - `DELETE /api/agents/{name}/avatar` — Remove avatar (JWT, owner only)

### 25.3 Avatar UI Components
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: AVATAR-001
- **Description**: Reusable avatar component with fallback, shown across all agent surfaces
- **Components**:
  - `AgentAvatar.vue` — Circular avatar with gradient+initials fallback (sm/md/lg/xl sizes)
  - `AvatarGenerateModal.vue` — Modal for generating/removing avatars
- **Integration**: AgentHeader, AgentNode (dashboard), Agents list (3 layouts)

### 25.4 Avatar Lifecycle
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: AVATAR-001
- **Description**: Avatar files cleaned up on agent delete, renamed on agent rename

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

## 27. Mobile Admin PWA (MOB-001)

Standalone mobile-friendly admin page for managing agents on the go. Designed as a Progressive Web App (PWA) installable from URL — completely separate from the main UI with no navigation links to it.

### 27.1 Mobile Admin Page
- **Status**: ✅ Implemented (2026-03-14)
- **Requirement ID**: MOB-001
- **Description**: Standalone Vue page at `/m` route, admin-only, not linked from main UI navigation. Self-contained login + management interface optimized for mobile devices.
- **Key Features**:
  - Admin password login (inline, no redirect to main login page)
  - Bottom tab navigation (mobile UX pattern): Agents, Ops, System
  - Dark theme by default (OLED-friendly)
  - Touch-optimized: 16px min font, large tap targets, no hover-dependent interactions
  - iOS safe area support (`viewport-fit=cover`, notch handling)
  - Pull-to-refresh on all tabs
  - Auto-polling with configurable intervals

### 27.2 Agents Tab
- **Status**: ⏳ Not Started
- **Requirement ID**: MOB-001-AGENTS
- **Description**: Simplified agent list with essential management actions
- **Key Features**:
  - Agent cards showing: name, status (running/stopped), type badge
  - Start/stop toggle per agent (inline)
  - Tap agent to expand: logs tail, CPU/memory stats, last activity
  - Search/filter by name
  - System agents hidden by default

### 27.3 Ops Tab
- **Status**: ⏳ Not Started
- **Requirement ID**: MOB-001-OPS
- **Description**: Mobile-optimized Operating Room showing items needing attention
- **Key Features**:
  - Needs Response queue with expandable cards
  - Respond/acknowledge actions inline
  - Notification list with priority badges
  - Badge count on tab icon
  - Cost alerts summary

### 27.4 System Tab
- **Status**: ⏳ Not Started
- **Requirement ID**: MOB-001-SYSTEM
- **Description**: Fleet-level operations and health overview
- **Key Features**:
  - Fleet health summary (running/stopped/error counts)
  - Emergency stop button (with confirmation)
  - Fleet restart action
  - Schedule pause/resume all
  - Cost overview from `/api/ops/costs`

### 27.5 PWA Configuration
- **Status**: ⏳ Not Started
- **Requirement ID**: MOB-001-PWA
- **Description**: Progressive Web App manifest and service worker for "Add to Home Screen" installation
- **Key Features**:
  - Web App Manifest (`mobile-manifest.json`): standalone display, portrait orientation, Trinity branding
  - Service Worker: network-first with cache fallback for static assets, skip API caching
  - iOS PWA meta tags: `apple-mobile-web-app-capable`, status bar style, touch icons
  - Start URL: `/m` (auto-loads mobile admin)
  - Shortcuts: Agents tab, Ops tab
- **Reference**: DGX Sparky PWA implementation (`~/Dropbox/Agents/dgx/sparky-ui/`) for patterns

### 27.6 Mobile CSS Optimizations
- **Status**: ⏳ Not Started
- **Requirement ID**: MOB-001-CSS
- **Description**: Mobile-specific CSS for native-feeling interactions
- **Key Features**:
  - `touch-action: manipulation` (remove 300ms tap delay)
  - `-webkit-tap-highlight-color: transparent`
  - 16px minimum input font size (prevents iOS auto-zoom)
  - iOS keyboard handling via `visualViewport` API
  - CSS safe area insets for notched devices
  - Prevent overscroll bounce on iOS

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

## 29. Voice Chat (VOICE-001)

### 29.1 Voice Session Initialization (VOICE-001)
- **Status**: 🚧 In Progress (Phase 1 MVP)
- **Description**: Real-time voice conversations with agents via Gemini 2.5 Flash Native Audio
- **Key Features**: `POST /api/agents/{name}/voice/start` loads voice prompt + summarizes prior chat, opens Gemini Live API WebSocket connection
- **Architecture**: Browser (mic) → WebSocket → Backend (proxy) → Gemini Live API → Backend → WebSocket → Browser (speaker)

### 29.2 Audio Streaming Bridge (VOICE-002)
- **Status**: 🚧 In Progress
- **Description**: WebSocket proxy: browser ↔ backend ↔ Gemini, <100ms added latency
- **Key Features**: PCM 16kHz mono input, 24kHz mono output, base64 frame encoding

### 29.3 Transcript Persistence (VOICE-003)
- **Status**: 🚧 In Progress
- **Description**: Voice transcripts saved as ChatMessage rows with `source="voice"`, inline in existing chat sessions
- **Key Features**: Automatic transcript extraction from Gemini, `source` column on chat_messages table

### 29.4 Frontend Voice UI (VOICE-004)
- **Status**: 🚧 In Progress
- **Description**: Mic button next to chat input, voice overlay with status/mute/end controls
- **Key Features**: VoiceOverlay component, pulsing status indicators, live transcript display, mute toggle

### 29.5 Voice System Prompt (VOICE-005)
- **Status**: 🚧 In Progress
- **Description**: Per-agent `voice_system_prompt` field for voice personality
- **Key Features**: Stored on agent_ownership table, fallback to auto-generated prompt from agent name

### 29.6 Context Summary (VOICE-006)
- **Status**: 🚧 In Progress
- **Description**: On voice start, summarize prior messages and inject into Gemini system prompt
- **Key Features**: Last 20 messages truncated to ~750 tokens, injected as conversation context

### Phase Roadmap
1. **Phase 1 (MVP)**: Authenticated chat only, basic overlay, transcript on session end, manual voice prompt
2. **Phase 2 (Polish)**: Real-time waveform, incremental transcript, auto-generate voice prompt from CLAUDE.md
3. **Phase 3 (Advanced)**: Function calling, multi-language auto-detection, custom voice per agent

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

## Out of Scope

- Multi-tenant deployment (single org only)
- ~~Mobile application~~ → See Section 27 (MOB-001) for PWA-based mobile admin
- Fiat/Stripe payment integration (Nevermined handles crypto payments)
- Agent marketplace
