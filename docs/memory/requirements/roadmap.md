# Requirements — Roadmap — Advanced, Planned, Process Engine, Future Vision

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 16. Advanced Features

### 16.1 Agent Resource Allocation
- **Status**: ✅ Implemented (2026-01-02; RES-001 system defaults 2026-04-30)
- **Description**: Per-agent memory and CPU configuration; system-wide admin defaults as fleet-level ceiling
- **Key Features**: 3-tier fallback (per-agent DB override → system default → hardcoded safe value); admin `GET/PUT /api/settings/agent-defaults/resources`; CPU as whole processors (1/2/4/8/16); memory as Docker-native strings (1g–32g)
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
- **Status**: ✅ Implemented (2025-12-18; resilient deploy 2026-07-23, trinity-enterprise#125)
- **Description**: Recipe-based multi-agent deployment via YAML manifest
- **Key Features**: Permission presets, shared folders, schedules, auto-start
- **Resilient deploy (trinity-enterprise#125)**: deploy is **best-effort by default** — a per-agent create failure is collected into `failed: [{name, short_name, template, reason, status_code}]` and the remaining agents still deploy; post-create configuration (folders/permissions/schedules/tags/view) is scoped to the created agents and each phase degrades to `warnings` instead of aborting. Response `status` is tri-state: `deployed` (all created) / `partial` (some failed, HTTP 200) / `failed` (none created, HTTP 500 with the full report as the body) — plus `valid` for dry-run. `strict: true` on the request restores abort-on-first-error, preserving the failing agent's original status code. Failure reasons are credential-sanitized + URL-userinfo-redacted + truncated at the exit point. A partial response warns that re-deploying the same manifest creates `_N`-suffixed duplicates (converge/`on_conflict` semantics remain deferred under the fresh-install epic trinity-enterprise#122 — the first-run seeder §16.5.1 sidesteps them by never re-deploying: flag latch + existence backstop). The global `trinity_prompt` write only happens when at least one agent was created. Prerequisite for the fresh-install seed (§16.5.1, trinity-enterprise#124) and UI manifest install (trinity-enterprise#126).
- **Deploy orchestration home (trinity-enterprise#124)**: the full deploy pipeline (parse → validate → resolve → create loop → prompt → config phases → start) lives in `services/system_service.py::deploy_manifest(manifest_yaml, current_user, request=None, *, dry_run, strict, create_agent_fn=None)` — `routers/systems.py::deploy_system` is a thin HTTP wrapper (maps `status="failed"` → HTTP 500 JSONResponse; everything else passes through). `create_agent_fn=None` resolves lazily to the `routers/agents.create_agent_internal` **facade** (which injects `ws_manager`), so `agent_created` WS broadcasts are preserved on both the HTTP and seed paths.
- **Flow**: `docs/memory/feature-flows/system-manifest.md`

#### 16.5.1 First-Run Default System Seed (trinity-enterprise#124)
- **Status**: ✅ Implemented (2026-07-24)
- **Description**: On a genuinely fresh install, auto-deploy a bundled default system manifest so a new instance comes up with a running starter fleet — the multi-agent generalization of the Cornelius seeder (ent#107, §core-agent).
- **Seeder**: `services/system_seed_service.py`. `ensure_first_run_seeded()` is the single first-run pass both call sites use (`routers/setup.py` setup-completion background task; `main.py` lifespan safety-net gated on `setup_completed`, held via a module-level strong task reference): it resolves the **persisted first-run verdict** then runs the Cornelius seeder and the fleet seeder under that one decision.
- **Persisted freshness verdict (`first_run_fresh` system-setting)**: computed ONCE — `count_non_system_agents() == 0`, forced false when `cornelius_seeded=="true"` (an established ent#107-era install, including one whose agents were later deleted) — and stored durably. Both seeders consume it (`ensure_seeded(fresh=...)`; Cornelius falls back to its legacy internal count when `fresh=None`). Without this, the first seeder's own agents poison every later pass's count: a failed fleet deploy could never retry, and a failed Cornelius would self-mark seeded after the fleet lands.
- **Idempotency & safety**: durable `default_system_seeded` flag; Redis SETNX lock `system_seed:provision` (TTL 600s, fail-open); **existence backstop** inside the locked section — if any final `{system}-{short}` agent name is already reserved (`is_agent_name_reserved`, covers soft-deleted), the seeder converges the flag WITHOUT deploying (the deploy path has no 409 backstop: name collisions become `_N` suffixes, so a fail-open lock race would otherwise double-seed). Flag policy: `deployed`/`partial` → set (a partial fleet must never be re-deployed — that suffixes duplicates); `failed` (0 created) / exception / unreadable manifest → NOT set (retry next pass; nothing to duplicate). `default_system_seed_info` (JSON: manifest name, sha256, status, created/failed counts, seeded_at) is stored beside the flag for diagnostics and future curated-fleet upgrades (ent#137).
- **Manifest resolution**: `TRINITY_DEFAULT_SYSTEM_MANIFEST` env (read at call time, `strip()`ed; empty ⇒ unset) — a path to an operator manifest, or a disable sentinel (`disabled`/`none`/`off`/`0`/`false`); otherwise the bundled `config/manifests/default-system.yaml` (baked into the backend image AND bind-mounted via the `config/manifests` directory in both compose files — the dev compose `./src/backend:/app` mount shadows image COPYs, so the mount is load-bearing locally). An explicitly-set-but-unreadable override fails loudly (ERROR log + operator-queue alert) and does not fall back to the bundled manifest.
- **Bundled starter fleet**: the in-tree acme trio (`local:scout`/`local:sage`/`local:scribe`) deployed as the coherent team its CLAUDE.md content assumes — system name `acme`, shared folders, `permissions: preset: full-mesh`; **no schedules** (zero-credential installs must not accumulate failing cron executions), **no `prompt:`** (never mutates the platform-wide `trinity_prompt`). Content is data: ent#137's curated public fleet replaces the manifest without touching the mechanism. Footprint: a fresh install runs `trinity-system` + Cornelius + 3 starters (limits, not reservations); operators opt out via the disable sentinel.
- **Honest status**: partial/failed seeds, seed-path errors (e.g. a parse-broken override), unreadable overrides, and crash-interrupted partial fleets detected by the converge backstop all emit a platform-path operator-queue alert (direct DB create on `trinity-system`, deterministic `system-seed-*` id — a #1632 reserved prefix so an agent cannot pre-create-and-silence it; best-effort) in addition to logs.
- **Fail-open**: the seeder never raises and never blocks boot or setup completion.
- **Flow**: `docs/memory/feature-flows/system-manifest.md` (seed section)

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

### 17.2a System-Emitted Task Completion Events (#1578)
- **Status**: ✅ Implemented (2026-07-17)
- **Priority**: Medium (P2) · `theme-reliability`
- **Description**: The **backend** deterministically emits `agent.task.completed` / `agent.task.failed` at **every CAS-won execution terminal**, delivered over the existing EVT-001 subscription-dispatch machinery, so a subscribed caller/orchestrator is **woken** when a long async task finishes instead of polling `get_execution_result`. Implements the missing half of `TARGET_ARCHITECTURE.md` §Async-First Communication and is a down-payment on Epic #1045 → #1081 (pull coordination).
- **System- vs agent-emitted**: EVT-001 (17.2) carries only **agent-emitted** events (an agent's LLM calls `emit_event`). These are the first **system-emitted** events — synthesized by the deterministic backend chokepoint with no LLM in the loop, `source_agent` = the executing agent, in the reserved `agent.task.*` namespace.
- **Key Features**:
  - Emitted from a single shared helper (`services/event_dispatch_service.py::emit_task_terminal_event`) fanned across **every** CAS-won terminal writer — `apply_result` (success + failure), `_write_terminal_and_gate` (timeout/budget/crash), the #1083 lease-reaper (`cleanup_service`), and the pull sink (`pull_coordination_service`, dark until a pilot). Bulk watchdog sweeps are a documented residual (no per-row context).
  - **Matching-subscription gated**: `find_matching_event_subscriptions` runs FIRST — zero matching subscriptions ⇒ **no** `agent_events` row and **no** dispatch (inert by default, no fleet-wide spam).
  - **CAS-won exactly-once**: emit runs strictly inside the `won` branch, so a replayed/late #1083 callback or a lease-expiry race fires nothing (no double-wake).
  - **Payload** `{execution_id, status, triggered_by, summary_or_error, duration_ms, cost, fan_out_id, loop_id}` — flat + `{{payload.field}}`-interpolable; `status` is the string value (`success`/`failed`/`cancelled`). `fan_out_id`/`loop_id` carried for the future pull fan-out join envelope.
  - **Reserved namespace + loop safety (3 layers)**: agents cannot `emit_event` into `agent.task.*` (400); self-subscription to `agent.task.*` is blocked at create + update (400); and a **recursion-break** — a task spawned by an `agent.task.*` dispatch is persisted with `triggered_by="event"` and the emit helper suppresses re-emission, breaking self / A↔B / A→B→C→A auto-emit cycles at the root.
  - **Delivery** = EVT-001 subscription dispatch (loopback async `/task`). Best-effort: wakes a **running** (incl. #1402 parked-but-running) subscriber; a stopped subscriber's 503 is swallowed (the `agent_events` row persists, the wake does not). Durable "lands in the caller's queue" is the pull migration's future queue.
  - **Fail-open**: the entire emit body is try/except-swallowed — a broken/slow emit never affects the billed terminal.
- **Additive & inert**: reuses `agent_events` / `agent_event_subscriptions` and the existing `triggered_by` TEXT column — no schema change, no migration, no feature flag, no new config.
- **GitHub Issue**: #1578 (Epic #1045 → #1081)

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

> **Status**: ❌ DELETED (2026-04-24, issue #430, PR #482)
> **Archive**: `archive/process-engine` git branch preserves full history
> **Reason**: The `agent_task` step handler bypassed `TaskExecutionService`, violating all orchestration invariants (slot accounting, activity tracking, backlog). Option B (delete) was chosen over Option A (fold through TES) to keep the execution stack clean.
> **What replaced it**: Agent scheduling + `TaskExecutionService` is the standard execution primitive. Human-approval use cases can be served by the Operating Room operator queue.

All subsections 18.1–18.10 were deleted with the code. Flow docs archived at `docs/memory/feature-flows/archive/process-dashboard.md`.

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
