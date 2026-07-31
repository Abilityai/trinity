# Feature Flow: Internal System Agent

> **Phase 11.1 & 11.2** - Platform operations manager with privileged access
>
> **Updated 2026-01-13**: UI Consolidation - System agent now uses standard `AgentDetail.vue` instead of dedicated `SystemAgent.vue`. Accessible via `/agents/trinity-system` with full tab access including Schedules. Admin-only visibility on Agents page with purple ring and "SYSTEM" badge.
>
> **Updated 2026-01-09**: Added Schedule & Execution Management slash commands and `GET /api/ops/schedules` endpoint
>
> **Updated 2025-12-31**: Added AgentClient service pattern for HTTP communication

## Overview

The Internal System Agent (`trinity-system`) is an auto-deployed, deletion-protected agent that serves as the **platform operations manager**. It focuses exclusively on infrastructure concerns: agent health, lifecycle management, resource governance, and schedule control.

> **Guiding Principle**: "The system agent manages the orchestra, not the music."

## Operational Scope

### In Scope (Operations)

| Area | Responsibilities |
|------|------------------|
| **Health Monitoring** | Detect stuck agents, high context usage, container failures |
| **Lifecycle Management** | Start, stop, restart agents based on health |
| **Resource Governance** | Monitor cost, context thresholds, memory bounds |
| **Schedule Control** | Enable/disable schedules, trigger manual runs, pause automation |
| **Validation** | Pre-flight checks before agent operations |
| **Alerting** | Notify on anomalies, failures, threshold breaches |
| **Cleanup** | Reset stuck sessions, clear stale context |
| **Reporting** | Fleet status, cost summaries, health reports |

### Out of Scope (Not Ops)

| NOT Responsible For | Why |
|---------------------|-----|
| Task orchestration | Agents coordinate themselves via MCP |
| Content/output review | Domain-specific, not infrastructure |
| Workflow design | User/developer responsibility |
| Agent-to-agent messaging | Agents handle directly |

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Trinity Platform Startup                        │
├─────────────────────────────────────────────────────────────────────┤
│  1. Backend starts                                                  │
│  2. Database migrations run (is_system column)                      │
│  3. system_agent_service.ensure_deployed() called                   │
│  4. If trinity-system doesn't exist → create from template          │
│  5. If exists but stopped → start_agent_internal() (#1816)          │
│       → the shared lifecycle: config predicates, the #1809 image-   │
│         drift cold-start gate, #1560 clear-before-recreate → ADOPTS │
│  6. If running → READ-ONLY probe (#1816)                            │
│       check_base_image_state() → base_image_state in the result     │
│       stale   → WARNING + edge-triggered operator-queue alarm       │
│       unknown → WARNING only, never alarms (fail-open probe)        │
│       NEVER recreates — see the AC2 invariant below                 │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                    System Agent Container                            │
├─────────────────────────────────────────────────────────────────────┤
│  Name: agent-trinity-system                                         │
│  Template: local:trinity-system                                      │
│  Resources: 4 CPU, 8GB RAM                                          │
│  MCP Key: scope="system" (bypasses all permissions)                 │
│  Labels: trinity.is-system=true                                      │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  CLAUDE.md                                                    │   │
│  │  - Operations-only scope                                      │   │
│  │  - Available MCP tools                                        │   │
│  │  - Health monitoring guidelines                               │   │
│  │  - Alerting and reporting                                     │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Slash Commands (/ops/*)                                      │   │
│  │  Fleet Operations:                                            │   │
│  │  - /ops/status - Fleet status report                         │   │
│  │  - /ops/health - Health check all agents                     │   │
│  │  - /ops/restart <agent> - Restart specific agent             │   │
│  │  - /ops/restart-all - Restart entire fleet                   │   │
│  │  - /ops/stop <agent> - Stop specific agent                   │   │
│  │  - /ops/costs - Cost report from OTel                        │   │
│  │                                                               │   │
│  │  Schedule Management:                                         │   │
│  │  - /ops/schedules - Quick schedule overview                  │   │
│  │  - /ops/schedules/list - Detailed listing with history       │   │
│  │  - /ops/schedules/pause [agent] - Pause schedules            │   │
│  │  - /ops/schedules/resume [agent] - Resume schedules          │   │
│  │                                                               │   │
│  │  Execution Management:                                        │   │
│  │  - /ops/executions/list [agent] - List recent executions     │   │
│  │  - /ops/executions/status <id> - Execution details           │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

## Base-image adoption (#1816)

`trinity-system` runs `restart_policy: unless-stopped` and is never manually cycled, so before #1816 it
was the **most-stale agent in every fleet**: `ensure_deployed` returned `action: none` the moment the
container reported `running`, and no drift predicate ever ran. After `build-base-image.sh` the
orchestrator kept the old image indefinitely, across every backend restart.

### The convergence invariant (test-pinned)

> For `trinity-system`, the container produced by `_create_system_agent` **and** the container produced by
> the shared recreate path (`recreate_container_with_updated_config`) must both leave **all eight** config
> predicates `True`. Any predicate that can be permanently false is an AC2 hole *by construction* — a
> config-drift recreate resolves the image from the container's own `Config.Image` **tag**
> (`trinity-agent-base:latest`), not the pinned id, so **every** config recreate is also an image adoption.

Two predicates were permanently false for a freshly created system agent, and both are now converged in
`_create_system_agent`:

| Predicate | Was | Now |
|---|---|---|
| `check_agent_auth_token_env_matches` (#1159) | creation never wrote `TRINITY_AGENT_AUTH_TOKEN` | env carries `derive_agent_token(SYSTEM_AGENT_NAME)` |
| `check_full_capabilities_match` | `cap_add=FULL_CAPABILITIES` but no `trinity.full-capabilities` label (missing → `false`) | label pinned `'true'`, **and** the predicate is system-aware (the system agent's capabilities are contractual, not fleet-derived — pinning the label alone would never converge on an `agent_full_capabilities=false` install) |

`tests/unit/test_1816_system_agent_convergence.py` AST-extracts creation's actual `env_vars` / `labels`
dicts and asserts all eight predicates against a container built **from them** — a hand-built fixture
would assert only that a correct container is correct.

### The three boundaries

| Boundary | Behaviour |
|---|---|
| Backend boot, container **running** | **Read-only.** `check_base_image_state()` → `result["base_image_state"]` ∈ `stale \| current \| unknown`; `action: "none"`. WARNING names the remedy; an edge-triggered `base-image-stale-` operator-queue alarm fires on `stale` **only** (a fail-open probe must never manufacture an alert, so `unknown` never alarms). **No recreate call exists in this branch** — source-pinned. The alarm's cooldown cursor is **per-process**, and `ensure_deployed` runs once per worker's lifespan, so a stale boot files one item **per worker** (`--workers 2` ⇒ 2). Deliberate: a cross-worker cursor would put Redis or a DB read on the boot path for an advisory alarm, and the un-guessable timestamped id — which is what stops an agent pre-creating and silencing it — cannot be deduped by `on_conflict_do_nothing` anyway. |
| Backend boot, container **stopped** | Delegates to `start_agent_internal(SYSTEM_AGENT_NAME)` instead of a bare `container_start`, inheriting the #1809 cold-start image gate, #1560 clear-before-recreate ordering, the 409/NotFound concurrent-recreate hardening and the post-recreate handle re-lookup. `recreated` / `recreate_reason` are surfaced into the result. **This is where adoption happens.** |
| `POST /api/system-agent/restart` | Explicit stop, then `start_agent_internal` — same adoption path. The response re-fetches the container so it reports the replacement's status, not the removed handle's. |

### AC2 — the structural gate

`POST /api/agents/trinity-system/start` was the **one** path that could replace a *running*
`trinity-system` (every other caller — subscription assign/remove, auto-switch, `restart_system`,
`/restart`, `/reinitialize` — stops the container first; delete, rename, ephemeral discard, the cleanup
orphan sweep and `restart_fleet` all refuse or skip system agents). `start_agent_internal` now carries a
structural gate: **system agent AND `was_already_running` ⇒ skip the recreate entirely** and return
`recreate_deferred: "system_agent_running"` (surfaced through `routers/agents.py`'s whitelisted response
dict, not just the internal one).

This is deliberately independent of predicate *count* — a ninth config predicate added next quarter cannot
reopen AC2. The accepted cost: an admin who changes a running system agent's config and clicks Start gets
no recreate; the response names the remedy (stop it, then start). This is a **deliberate divergence** from
regular-agent semantics.

### Consequences worth knowing

- **Adoption destroys the container writable layer.** Everything installed outside `/home/developer` —
  apt packages, system pip, `/usr/local`, `/etc` — is gone. `trinity-system` is the one agent that runs
  `FULL_CAPABILITIES` explicitly for package installation, so this is the most consequential side effect.
- **Adoption re-reads `ANTHROPIC_API_KEY`** from `get_anthropic_api_key()` and rewrites `trinity.cpu` /
  `trinity.memory` from `db.get_resource_limits` — creation reads them from `template.yaml`, so the two
  can legitimately disagree.
- **On the canonical upgrade path adoption is operator-triggered** (alarm → Restart), not automatic:
  `build-base-image.sh` only builds and tags, `start.sh`/`stop.sh` never touch agent containers, and
  `unless-stopped` means the system agent is *running* after every canonical upgrade.
- **`/reinitialize` deliberately does not adopt.** It is already an explicit stop and carries zero
  incremental AC coverage.
- **The system agent gets no `TRINITY_BACKEND_URL`.** The recreate `setdefault`s it for regular agents,
  but that env var is the agent-side heartbeat loop's gate and `heartbeat_service.authorize_heartbeat`
  accepts only `scope == "agent"` keys — the system agent's key is `scope == "system"`, so arming it would
  yield a permanent 5-second 403 loop. Test-pinned.
- **The generic recovery rebuild refuses `trinity-system`** (409). `start_agent_internal` falls through to
  `recreate_missing_container` when the container lookup returns `None`, and #1816 newly reaches that
  from the boot path and from `/restart` — `ensure_deployed` runs in **every** uvicorn worker with no
  leader lock, so a concurrent recreate can null the lookup mid-flight. That path reconstructs a *regular*
  agent: it deactivates the existing **system-scoped** MCP key and mints an *agent-scoped* one (plaintext
  unrecoverable ⇒ the orchestrator irreversibly loses its permission bypass), omits `trinity.is-system`
  and the read-only `/template` bind, drops `unless-stopped`, arms `TRINITY_BACKEND_URL`, and follows the
  fleet capabilities setting. Failing closed is self-healing — `ensure_deployed`'s create branch rebuilds
  it correctly on the next boot. The per-agent start lock (#1817) is the fix for the race itself.
- **The boot path blocks the lifespan.** The STOPPED branch runs recreate + `wait_for_agent_ready` (60s)
  + credential retries (3 × 2s) + skill injection + read-only sync, so the backend serves nothing for
  ~20–90s. Absorbed by the prod healthcheck (`start_period` 60s + 5 × 30s) and rare in practice, since
  `unless-stopped` means the container is normally *running* and takes the read-only branch.
- **`/restart` and `/reinitialize` are human-only.** `assert_admin` rejects *connector* principals but not
  *agent* ones, and `get_current_user` hands an agent-scoped MCP key its owner's role — so on a default
  admin-owned install any non-ephemeral agent's `TRINITY_MCP_API_KEY` passed it. Harmless when `/restart`
  was a stop+start; not once it replaces the container. Both now call `reject_agent_principal` first
  (trinity-ops-agent#232 precedent). No-op for JWT / user-scoped / system-scoped callers.

## Key Files

| File | Purpose |
|------|---------|
| `config/agent-templates/trinity-system/template.yaml` | Template configuration |
| `config/agent-templates/trinity-system/CLAUDE.md` | Operations instructions |
| `config/agent-templates/trinity-system/.claude/commands/ops/*.md` | Slash commands |
| `src/backend/services/system_agent_service.py` | Auto-deployment logic |
| `src/backend/routers/system_agent.py` | Admin management endpoints |
| `src/backend/routers/ops.py` | Fleet operations endpoints |
| `src/backend/services/agent_client.py` | Centralized agent HTTP client |
| `src/backend/routers/settings.py` | Ops settings (OPS_SETTINGS_DEFAULTS) |
| `src/backend/db/agents.py` | SYSTEM_AGENT_NAME constant, is_system checks |
| `src/frontend/src/views/AgentDetail.vue` | **System Agent UI** - uses standard agent detail with tab filtering (lines 414-448) |
| `src/frontend/src/views/Agents.vue` | System agent display on Agents page (admin-only, purple ring, SYSTEM badge, lines 46-75) |
| `src/frontend/src/stores/agents.js` | `systemAgent` getter (line 25-27), `sortedAgentsWithSystem` getter (line 39-41) |
| `src/frontend/src/router/index.js` | `/system-agent` redirects to `/agents/trinity-system` (lines 77-81) |

## Frontend UI

### Route (Updated 2026-01-13)
- **Path**: `/agents/trinity-system` (legacy `/system-agent` redirects here)
- **Access**: Admin-only visibility on Agents page; full access on AgentDetail.vue
- **NavBar**: "System" link removed - access via Agents page or direct URL

### UI Consolidation (2026-01-13)

The dedicated `SystemAgent.vue` has been removed. The system agent now uses the standard `AgentDetail.vue` with tab filtering:

**Agents Page Display** (`src/frontend/src/views/Agents.vue`):
- Lines 271-304: Admin check on mount (`GET /api/users/me`)
- Lines 274-279: `displayAgents` computed - includes system agent for admins via `sortedAgentsWithSystem`
- Lines 46-57: System agent card styling - purple ring (`ring-2 ring-purple-500/50`), purple border
- Lines 69-75: "SYSTEM" badge next to agent name

**Store Changes** (`src/frontend/src/stores/agents.js`):
- Lines 24-27: `systemAgent` getter - finds agent with `is_system: true`
- Lines 38-41: `sortedAgentsWithSystem` getter - pins system agent at top of list
- Lines 43-76: `_getSortedAgents()` helper - conditionally includes system agent

**AgentDetail Tab Filtering** (`src/frontend/src/views/AgentDetail.vue`):
- Lines 414-448: `visibleTabs` computed property
- Lines 427-430: Hides Sharing/Permissions tabs for system agent (not applicable)
- Line 433: Schedules tab always visible (including for system agent)
- Lines 442-445: Hides Folders/Public Links tabs for system agent

**Available Tabs for System Agent**:
| Tab | Visible | Purpose |
|-----|---------|---------|
| Info | Yes | Agent metadata and template info |
| Tasks | Yes | Manual task execution and history |
| Dashboard | Yes | Agent-defined dashboard (if configured) |
| Terminal | Yes | **Primary ops interface** - Claude Code TUI for `/ops/` commands |
| Logs | Yes | Container logs |
| Credentials | Yes | MCP API key and credentials |
| Schedules | Yes | System agent scheduled tasks |
| Git | Yes (if configured) | Git sync status |
| Files | Yes | File browser |
| Sharing | **No** | Not applicable (system agent) |
| Permissions | **No** | Not applicable (system agent has full access) |
| Folders | **No** | Not applicable (system agent) |
| Public Links | **No** | Not applicable (system agent) |

**Router Redirect** (`src/frontend/src/router/index.js`):
- Lines 77-81: `/system-agent` redirects to `/agents/trinity-system`

### Deleted Files (2026-01-13)
| File | Lines | Reason |
|------|-------|--------|
| `src/frontend/src/views/SystemAgent.vue` | 782 | Replaced by AgentDetail.vue with tab filtering |
| `src/frontend/src/components/SystemAgentTerminal.vue` | ~350 | Terminal functionality now uses standard TerminalPanelContent.vue |

### Design Rationale

1. **Unified Experience**: System agent uses same UI patterns as regular agents
2. **Full Feature Access**: Schedules tab now available (was missing in old UI)
3. **Reduced Maintenance**: One component instead of two parallel implementations
4. **Tab Filtering**: Irrelevant tabs (Sharing, Permissions, Folders) hidden via computed property
5. **Admin-Only Visibility**: System agent only appears on Agents page for admin users
6. **Visual Distinction**: Purple ring and "SYSTEM" badge clearly identify the system agent

## API Endpoints

### System Agent Management

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/system-agent/status` | GET | Get system agent status. **#1816:** adds `base_image_state` ∈ `stale \| current \| unknown` when the container is running — an **enum only**, never image ids or digests (mirrors the `/health clone_status` contract, #1439); admin-gated |
| `/api/system-agent/restart` | POST | Restart system agent (admin **+ human-only**, `reject_agent_principal`). **#1816:** stop, then `start_agent_internal` — adopts a rebuilt base image; the response reads a freshly fetched container |
| `/api/system-agent/reinitialize` | POST | Reset to clean state (admin **+ human-only**). Deliberately **not** an adoption path (#1816) |

### Fleet Operations

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/ops/fleet/status` | GET | All agents with status, context, activity |
| `/api/ops/fleet/health` | GET | Health summary with critical/warning issues |
| `/api/ops/fleet/restart` | POST | Restart all/filtered running agents through the canonical stop→`start_agent_internal` path — applies pending config drift and adopts a rebuilt base image (#1860). Skips system + ephemeral agents; agent-scoped keys 403 (`reject_agent_principal` — system-scoped keys unaffected); 409 while another fleet restart is in flight; outcome durably recorded in the audit log (`fleet_restart`) even if the client times out |
| `/api/ops/fleet/stop` | POST | Stop all/filtered agents |

### Schedule Control

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/ops/schedules` | GET | List all schedules with filters and execution history |
| `/api/ops/schedules/pause` | POST | Pause all schedules (optionally per-agent) |
| `/api/ops/schedules/resume` | POST | Resume all schedules (optionally per-agent) |
| `/api/ops/emergency-stop` | POST | Halt all executions immediately. Optional `?system_prefix=` to filter by agent name prefix |

**Query Parameters for `GET /api/ops/schedules`**:
- `agent_name` - Filter by specific agent
- `enabled_only` - Only return enabled schedules (default: false)

### Cost & Observability (OTel Integration)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/ops/costs` | GET | Cost report with OTel metrics, alerts, and thresholds |

The `/api/ops/costs` endpoint provides an ops-focused view of OpenTelemetry metrics:

**Response Structure**:
```json
{
  "enabled": true,
  "available": true,
  "timestamp": "2025-12-20T...",
  "summary": {
    "total_cost": 0.0234,
    "total_tokens": 48093,
    "daily_limit": 50.0,
    "cost_percent_of_limit": 0.05
  },
  "alerts": [
    {
      "severity": "warning",
      "type": "cost_limit_approaching",
      "message": "...",
      "recommendation": "..."
    }
  ],
  "cost_by_model": [...],
  "tokens_by_type": {...},
  "productivity": {
    "sessions": 5,
    "active_time_formatted": "1h",
    "commits": 3,
    "pull_requests": 1,
    "lines_added": 42,
    "lines_removed": 15
  }
}
```

**Alert Types**:
- `cost_limit_exceeded` (critical) - Daily cost >= limit
- `cost_limit_approaching` (warning) - Daily cost >= 80% of limit

**Integration with OTel**:
- Fetches raw metrics from OTel Collector's Prometheus endpoint (`:8889/metrics`)
- Reuses parsing from `observability.py` (decoupled architecture)
- Adds ops-specific analysis: threshold checks, alerts, formatted output
- System agent interprets the data via `/ops/costs` slash command

### Ops Settings

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/settings/ops/config` | GET | Get all ops settings with defaults |
| `/api/settings/ops/config` | PUT | Update multiple ops settings |
| `/api/settings/ops/reset` | POST | Reset all ops settings to defaults |

## Ops Settings

Stored in `system_settings` table with these defaults:

| Key | Default | Description |
|-----|---------|-------------|
| `ops_context_warning_threshold` | 75 | Context % to trigger warning |
| `ops_context_critical_threshold` | 90 | Context % to trigger critical |
| `ops_idle_timeout_minutes` | 30 | Minutes before stuck detection |
| `ops_cost_limit_daily_usd` | 50.0 | Daily cost limit (0 = unlimited) |
| `ops_max_execution_minutes` | 10 | Max chat execution time |
| `ops_alert_suppression_minutes` | 15 | Suppress duplicate alerts |
| `ops_log_retention_days` | 7 | Days to keep container logs |
| `ops_health_check_interval` | 60 | Seconds between health checks |

## MCP Scope Types

| Scope | Description | Permission Check |
|-------|-------------|------------------|
| `user` | Human user via Claude Code client | Owner/admin/shared |
| `agent` | Regular agent | Explicit permission list |
| `system` | System agent | **Bypasses all checks** |

## Slash Commands

Commands are located in `config/agent-templates/trinity-system/.claude/commands/ops/`:

### Fleet Operations

| Command | File | Description |
|---------|------|-------------|
| `/ops/status` | `status.md` | Fleet status with context usage |
| `/ops/health` | `health.md` | Health check with issues and recommendations |
| `/ops/restart <agent>` | `restart.md` | Restart specific agent |
| `/ops/restart-all` | `restart-all.md` | Restart entire fleet |
| `/ops/stop <agent>` | `stop.md` | Stop specific agent |
| `/ops/costs` | `costs.md` | Cost report from OTel metrics |

### Schedule Management

| Command | File | Description |
|---------|------|-------------|
| `/ops/schedules` | `schedules.md` | Quick schedule overview |
| `/ops/schedules/list` | `schedules/list.md` | Detailed schedule listing with execution history |
| `/ops/schedules/pause [agent]` | `schedules/pause.md` | Pause schedules (all or per-agent) |
| `/ops/schedules/resume [agent]` | `schedules/resume.md` | Resume paused schedules |

### Execution Management

| Command | File | Description |
|---------|------|-------------|
| `/ops/executions/list [agent]` | `executions/list.md` | List recent task executions |
| `/ops/executions/status <id>` | `executions/status.md` | Get detailed execution status |

## Data Flow

### 1. Auto-Deployment on Startup

```
Backend Startup
       │
       ▼
┌──────────────────┐
│ lifespan()       │
│ handler          │
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────────┐
│ system_agent_service.            │
│ ensure_deployed()                │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────┐    Yes    ┌──────────────────┐
│ Container        ├──────────►│ Already running? │
│ exists?          │           │ → No action      │
└────────┬─────────┘           └──────────────────┘
         │ No
         ▼
┌──────────────────────────────────┐
│ _create_system_agent()           │
│ 1. Load template                 │
│ 2. Create MCP key (scope=system) │
│ 3. Start container with mounts:  │
│    - /template (ro)              │
│    - /trinity-meta-prompt (ro)   │
│ 4. Register ownership (is_system)│
└──────────────────────────────────┘
```

### 2. Fleet Health Check Flow

```
User: /ops/health
       │
       ▼
┌──────────────────────────────────┐
│ System Agent receives command    │
│ Reads health.md instructions     │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ mcp__trinity__list_agents        │
│ Get all agents                   │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ For each running agent:          │
│ - Check container status         │
│ - Check context usage            │
│ - Check last activity            │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Classify health:                 │
│ - Critical: >90% context, down   │
│ - Warning: >75% context, idle    │
│ - Healthy: Normal operation      │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Generate health report           │
│ with recommendations             │
└──────────────────────────────────┘
```

### 2a. Fleet Status/Health Context Polling (AgentClient Pattern)

Both fleet status and health endpoints use `AgentClient` to poll context stats from running agents:

```
GET /api/ops/fleet/status
       │
       ▼
┌──────────────────────────────────────┐
│ routers/ops.py:40-100                │
│ get_fleet_status()                   │
└────────┬─────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────┐
│ list_all_agents_fast()  (line 54)    │
│ Get agents from labels only (~50ms)  │
└────────┬─────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────┐
│ For each running agent (lines 60-73):                        │
│                                                              │
│   client = get_agent_client(agent_name)                      │
│   session_info = await client.get_session()                  │
│                                                              │
│   context_stats[agent_name] = {                              │
│       "context_tokens": session_info.context_tokens,         │
│       "context_window": session_info.context_window,         │
│       "context_percent": session_info.context_percent        │
│   }                                                          │
└────────┬─────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────┐
│ Build fleet_status response          │
│ with agent context stats             │
└──────────────────────────────────────┘
```

**AgentClient.get_session()** (lines 247-272 in `services/agent_client.py`):
- Calls `GET http://agent-{name}:8000/api/chat/session`
- Timeout: 5 seconds (SESSION_TIMEOUT)
- Returns `AgentSessionInfo` dataclass:

```python
@dataclass
class AgentSessionInfo:
    context_tokens: int       # Current token count in context
    context_window: int       # Max context window size
    context_percent: float    # Usage percentage (0-100)
    total_cost_usd: Optional[float] = None  # Session cost
```

**Key Implementation Details**:
- Uses centralized `AgentClient` instead of raw `httpx` calls
- Silent error handling - if agent not responding, context stats are omitted
- Factory function: `get_agent_client(agent_name)` returns client instance
- URL pattern: `http://agent-{agent_name}:8000` (Docker network naming)

**Fleet Health Endpoint** (`GET /api/ops/fleet/health`, lines 119-218):
- Same pattern as fleet status but with health classification
- Uses `client.get_session()` at lines 167-169
- Classifies context usage: critical (>90%), warning (>75%), healthy

### 3. Emergency Stop Flow (Parallel Execution - 2026-01-14, Prefix Filter - 2026-01-27)

**Performance Improvement**: Emergency stop now uses `ThreadPoolExecutor(max_workers=10)` to stop agents in parallel instead of sequentially. This significantly reduces the time to halt a large fleet during emergencies.

**Prefix Filtering (2026-01-27)**: Added optional `system_prefix` query parameter to target specific agents by name prefix. This allows stopping only agents matching a pattern (e.g., `?system_prefix=prod-` to stop only production agents).

```
POST /api/ops/emergency-stop?system_prefix={optional-prefix}
       │
       ▼
┌──────────────────────────────────────────────────────┐
│ 1. Pause enabled schedules (with prefix filter)      │
│    - If prefix set: only schedules where             │
│      agent_name.startswith(prefix)                   │
│    - Set enabled=false in DB                         │
│    - Remove from APScheduler                         │
└────────┬─────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────┐
│ 2. Stop non-system agents IN PARALLEL (with filter)  │
│    - If prefix set: only agents where                │
│      agent_name.startswith(prefix)                   │
│    - Filter to running non-system agents             │
│    - ThreadPoolExecutor(max_workers=10)              │
│    - _stop_agent_container() helper for each         │
│    - container.stop(timeout=10) per agent            │
│    - 60-second timeout for all futures               │
└────────┬─────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ 3. Audit log with results        │
│    - schedules_paused: N         │
│    - agents_stopped: N           │
│    - errors: [...]               │
└──────────────────────────────────┘
```

**API Signature** (`src/backend/routers/ops.py:607-696`):

```python
@router.post("/emergency-stop")
async def emergency_stop(
    request: Request,
    current_user: User = Depends(get_current_user),
    system_prefix: Optional[str] = Query(
        None,
        description="Optional: Only stop agents whose names start with this prefix"
    )
):
```

**Prefix Filter Logic** (lines 638-639 for schedules, 658-659 for agents):

```python
# Schedule filtering (line 638-639)
if system_prefix and not schedule.agent_name.startswith(system_prefix):
    continue

# Agent filtering (line 658-659)
if system_prefix and not agent_name.startswith(system_prefix):
    continue
```

**Implementation** (`src/backend/routers/ops.py:591-696`):

```python
def _stop_agent_container(agent_name: str, timeout: int = 10) -> dict:
    """Stop a single agent container. Used for parallel stopping."""
    try:
        container = get_agent_container(agent_name)
        if container:
            container.stop(timeout=timeout)
            return {"agent": agent_name, "result": "stopped"}
        return {"agent": agent_name, "result": "not_found"}
    except Exception as e:
        return {"agent": agent_name, "result": "error", "error": str(e)}

# In emergency_stop():
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = {
        executor.submit(_stop_agent_container, agent_name, 10): agent_name
        for agent_name in agents_to_stop
    }
    for future in concurrent.futures.as_completed(futures, timeout=60):
        # Process results
```

**Why Parallel**: Docker SDK `container.stop()` is synchronous and waits for the container to stop (up to timeout). With 20 agents at 10-second timeout each, sequential stopping would take 200+ seconds. Parallel stopping with 10 workers reduces this to ~20-30 seconds.

**Use Cases for Prefix Filter**:
- Stop all agents in a namespace: `?system_prefix=prod-`
- Stop agents from a specific project: `?system_prefix=project-x-`
- Testing without side effects: `?system_prefix=nonexistent-prefix` (stops nothing)

### 4. Cost Monitoring Flow (OTel Access)

The system agent accesses OpenTelemetry metrics via the REST API using its MCP API key:

```
User: /ops/costs (or "show me platform costs")
       │
       ▼
┌──────────────────────────────────┐
│ System Agent reads costs.md      │
│ (slash command instructions)     │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ Execute Bash curl command:                               │
│ curl -s http://backend:8000/api/ops/costs \              │
│   -H "Authorization: Bearer $TRINITY_MCP_API_KEY"        │
└────────┬─────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Backend /api/ops/costs endpoint  │
│ 1. Validate MCP API key (system) │
│ 2. Check OTEL_ENABLED env var    │
│ 3. Fetch from OTel Collector     │
│    (http://collector:8889/metrics)│
│ 4. Parse Prometheus format       │
│ 5. Add alerts & thresholds       │
│ 6. Return JSON response          │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ System Agent parses response:    │
│ - enabled: true/false            │
│ - available: true/false          │
│ - summary: cost, tokens, limit   │
│ - alerts: warnings/critical      │
│ - cost_by_model: breakdown       │
│ - productivity: commits, PRs     │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Generate formatted report        │
│ based on response state          │
└──────────────────────────────────┘
```

**Key Implementation Details (2025-12-21 Fix):**

| Issue | Root Cause | Fix |
|-------|------------|-----|
| Agent couldn't access OTel | Slash command used `$TRINITY_API_KEY` but agent has `$TRINITY_MCP_API_KEY` | Updated `costs.md` to use correct env var |
| Agent said "I don't have access" | No explicit documentation in CLAUDE.md | Added Cost Monitoring section with curl example |
| MCP key works for REST API | `dependencies.py` already supports MCP key auth | No code change needed (discovery) |

**Environment Variable:**
- `$TRINITY_MCP_API_KEY` - Auto-injected when system agent is created
- Used for both MCP tool calls AND REST API authentication
- System scope bypasses permission checks

**Files Modified:**
- `config/agent-templates/trinity-system/.claude/commands/ops/costs.md:11` - Fixed env var
- `config/agent-templates/trinity-system/CLAUDE.md:123-147` - Added Cost Monitoring section

### 5. Schedule Management Flow (2026-01-09)

The system agent manages schedules across all agents via REST API calls:

```
User: /ops/schedules/list
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│ System Agent reads schedules/list.md instructions        │
│ Executes curl command:                                   │
│ curl -s "http://backend:8000/api/ops/schedules" \        │
│   -H "Authorization: Bearer $TRINITY_MCP_API_KEY"        │
└────────┬─────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ Backend /api/ops/schedules endpoint (ops.py:427-495)     │
│ 1. db.list_all_schedules()                               │
│ 2. Apply filters (agent_name, enabled_only)              │
│ 3. For each schedule, get recent executions              │
│ 4. Build response with summary + schedule list           │
└────────┬─────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ Response JSON:                                           │
│ {                                                        │
│   "summary": {total, enabled, disabled, agents_with_...},│
│   "schedules": [{                                        │
│     "id", "agent_name", "name", "cron_expression",       │
│     "enabled", "next_run_at", "last_run_at",             │
│     "last_execution": {status, duration_ms, error}       │
│   }, ...]                                                │
│ }                                                        │
└────────┬─────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ System Agent formats report:                             │
│ - Summary stats                                          │
│ - Schedules grouped by agent                             │
│ - Warnings for failing schedules                         │
└──────────────────────────────────────────────────────────┘
```

**Pause/Resume Flow:**

```
User: /ops/schedules/pause my-agent
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│ curl -X POST "http://backend:8000/api/ops/schedules/     │
│   pause?agent_name=my-agent" \                           │
│   -H "Authorization: Bearer $TRINITY_MCP_API_KEY"        │
└────────┬─────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ Backend (ops.py:498-516)                                 │
│ 1. Get all enabled schedules for agent                   │
│ 2. For each: db.set_schedule_enabled(id, False)          │
│ 3. Remove from APScheduler                               │
└────────┬─────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ Response: {"paused_count": 3, "agent_filter": "my-agent"}│
└──────────────────────────────────────────────────────────┘
```

**Database Layer (db/schedules.py):**

| Method | Line | Description |
|--------|------|-------------|
| `list_all_schedules()` | 185-193 | List all schedules across all agents |
| `list_all_enabled_schedules()` | 165-173 | List enabled schedules |
| `list_all_disabled_schedules()` | 175-183 | List disabled schedules |
| `set_schedule_enabled()` | 250-258 | Enable/disable a schedule |

### 6. Reinitialize Flow (5-Step Process)

The reinitialize endpoint resets the system agent to a clean state without losing database records:

```
POST /api/system-agent/reinitialize (admin-only)
       │
       ▼
┌──────────────────────────────────┐
│ Step 1: STOPPED                  │
│ - container.stop(timeout=30)     │
│ - Wait for clean shutdown        │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ Step 2: FILES_CLEARED                                    │
│ - container.start()                                      │
│ - exec: rm -rf /home/developer/.claude                   │
│         /home/developer/.trinity /home/developer/content │
│         /home/developer/plans                            │
└────────┬─────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Step 3: STARTED                  │
│ - Container already running      │
│   from step 2                    │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│ Step 4: TEMPLATE_COPIED (2025-12-21 Fix)                 │
│ - exec: cp -r /template/.claude /home/developer/         │
│ - exec: cp /template/CLAUDE.md /home/developer/          │
│ - Restores slash commands deleted in step 2              │
└────────┬─────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Step 5: TRINITY_INJECTED         │
│ - inject_trinity_meta_prompt()   │
│ - Copies /trinity-meta-prompt/*  │
│   to /home/developer/            │
└────────┬─────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Return result:                   │
│ - success: true                  │
│ - steps_completed: [...]         │
│ - warnings: [any errors]         │
└──────────────────────────────────┘
```

**Why Step 4 Was Added (2025-12-21):**

The previous reinitialize flow had 4 steps but was missing template re-copy:
1. Step 2 deleted `/home/developer/.claude` (including `/ops/` slash commands)
2. Step 5 only injected Trinity meta-prompt (not template content)
3. Result: System agent lost its `/ops/` slash commands after reinitialize

Fix: Added explicit template copy step between workspace clear and Trinity injection.

**File:** `src/backend/routers/system_agent.py:167-178`

```python
# Step 4: Re-copy template files (.claude and CLAUDE.md)
copy_result = container.exec_run(
    "bash -c 'cp -r /template/.claude /home/developer/ 2>/dev/null; "
    "cp /template/CLAUDE.md /home/developer/ 2>/dev/null; true'",
    user="developer"
)
```

## Testing

### 1. Auto-Deployment
```bash
# Restart backend
docker-compose restart backend

# Check logs for system agent deployment
docker-compose logs backend | grep "System agent"
# Expected: "System agent: created - System agent created and started"
```

### 2. Deletion Protection
```bash
# Try to delete via API
curl -X DELETE http://localhost:8000/api/agents/trinity-system \
  -H "Authorization: Bearer $TOKEN"

# Expected: 403 with message about system agents
```

### 3. Fleet Status API
```bash
curl http://localhost:8000/api/ops/fleet/status \
  -H "Authorization: Bearer $TOKEN"

# Expected: List of all agents with status, context, activity
```

### 4. Health Check API
```bash
curl http://localhost:8000/api/ops/fleet/health \
  -H "Authorization: Bearer $TOKEN"

# Expected: Health summary with critical_issues, warnings, healthy_count
```

### 5. Emergency Stop
```bash
# Stop ALL non-system agents
curl -X POST http://localhost:8000/api/ops/emergency-stop \
  -H "Authorization: Bearer $TOKEN"

# Expected: All schedules paused, all non-system agents stopped

# Stop only agents with specific prefix
curl -X POST "http://localhost:8000/api/ops/emergency-stop?system_prefix=prod-" \
  -H "Authorization: Bearer $TOKEN"

# Expected: Only schedules/agents where name starts with "prod-" are affected

# Safe test (no side effects)
curl -X POST "http://localhost:8000/api/ops/emergency-stop?system_prefix=nonexistent-prefix" \
  -H "Authorization: Bearer $TOKEN"

# Expected: {"success": true, "schedules_paused": 0, "agents_stopped": 0}
```

### 6. Ops Commands (via Chat)
```bash
# Connect to system agent and run:
# /ops/status
# /ops/health
# /ops/restart <agent-name>
```

### 7. Ops Settings
```bash
# Get current settings
curl http://localhost:8000/api/settings/ops/config \
  -H "Authorization: Bearer $TOKEN"

# Update settings
curl -X PUT http://localhost:8000/api/settings/ops/config \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"settings": {"ops_context_warning_threshold": "80"}}'
```

## Troubleshooting

### System Agent Not Deploying
1. Check admin user exists in database
2. Verify template exists at `config/agent-templates/trinity-system/`
3. Check backend logs for deployment errors
4. Verify Docker is accessible

### System Agent Can't Access Other Agents
1. Verify MCP key was created with `scope: "system"`
2. Check MCP server logs for authentication
3. Verify key is passed correctly to agent container

### Re-initialization Fails
1. Verify user is admin
2. Check container is accessible
3. Review workspace cleanup errors in response

### Ops Commands Not Working
1. Verify slash commands exist at `config/agent-templates/trinity-system/.claude/commands/ops/`
2. Check agent has been re-initialized after template update
3. Verify agent can access MCP tools

### System Agent Can't Access OTel Metrics (2025-12-21 Fix)

**Symptom**: Agent responds "I don't have direct access to view OpenTelemetry metrics" when asked about costs.

**Root Causes and Fixes**:

1. **Wrong environment variable in slash command**
   - Problem: `costs.md` used `$TRINITY_API_KEY`
   - Fix: Change to `$TRINITY_MCP_API_KEY` (what the agent actually has)
   - File: `config/agent-templates/trinity-system/.claude/commands/ops/costs.md`

2. **Missing /trinity-meta-prompt mount on creation**
   - Problem: System agent created without Trinity meta-prompt mount
   - Fix: Added mount in `_create_system_agent()` (lines 215-221)
   - File: `src/backend/services/system_agent_service.py`

3. **Reinitialize deleted slash commands without restoring them**
   - Problem: Step 2 deleted `.claude/` dir, step 5 only injected Trinity prompt
   - Fix: Added step 4 to re-copy template files before Trinity injection
   - File: `src/backend/routers/system_agent.py`

4. **No explicit documentation in CLAUDE.md**
   - Problem: Agent didn't know it could call REST API with its MCP key
   - Fix: Added "Cost Monitoring" section with curl example
   - File: `config/agent-templates/trinity-system/CLAUDE.md`

**Verification**:
```bash
# SSH into system agent and verify environment
echo $TRINITY_MCP_API_KEY  # Should show the key

# Test the API call directly
curl -s http://backend:8000/api/ops/costs \
  -H "Authorization: Bearer $TRINITY_MCP_API_KEY"
```

## Report Storage

All ops reports are saved to organized directories in the system agent's workspace:

```
~/reports/
├── fleet/              # /ops/status reports
├── health/             # /ops/health reports
├── costs/              # /ops/costs reports
├── compliance/         # /ops/compatibility-audit reports
├── service-checks/     # /ops/service-check reports
├── schedules/          # /ops/schedules/list reports
└── executions/         # /ops/executions/list reports
```

**Naming Convention**: `YYYY-MM-DD_HHMM.md` (e.g., `2026-01-13_1430.md`)

**Workflow**:
1. Command generates report following instructions
2. Creates directory if needed: `mkdir -p ~/reports/{type}`
3. Saves with timestamp: `~/reports/{type}/YYYY-MM-DD_HHMM.md`
4. Outputs to chat for immediate viewing
5. Confirms save location

**Finding Latest Report**:
```bash
ls -1 ~/reports/fleet/ | tail -1
```

**gitignored**: The `reports/` directory is excluded from Git sync.

## Revision History

| Date | Changes |
|------|---------|
| 2026-07-28 | **#1816 base-image adoption**: `ensure_deployed` is read-only when the container is running (3-state `check_base_image_state` → `base_image_state`, WARNING + an edge-triggered `base-image-stale-` operator alarm on `stale` only) and delegates to `start_agent_internal` when stopped, so the cold boundary adopts a rebuilt base image through the shared lifecycle. Creation converges on the recreate path's contract (`TRINITY_AGENT_AUTH_TOKEN`, `trinity.full-capabilities`) — the convergence invariant. AC2 is a **structural** gate in `start_agent_internal` (`is_system AND was_already_running` ⇒ `recreate_deferred="system_agent_running"`), independent of predicate count. `/restart` delegates; `/status` gains `base_image_state`. |
| 2026-02-11 | Fixed reinitialize flow diagram - cleanup command now shows actual paths (`/home/developer/.claude`, `.trinity`, `content`, `plans`) instead of obsolete workspace reference |
| 2026-01-27 | **Emergency Stop Prefix Filter**: Added `system_prefix` query parameter to `POST /api/ops/emergency-stop` (`routers/ops.py:607-696`). Allows targeting specific agents/schedules by name prefix. Schedule pausing respects prefix (line 638-639), agent stopping respects prefix (line 658-659). Enables safe testing with nonexistent prefix. |
| 2026-01-14 | **Emergency Stop Parallel Execution (LOW)**: Implemented parallel agent stopping with `ThreadPoolExecutor(max_workers=10)` in `routers/ops.py:591-696`. Added `_stop_agent_container()` helper function for thread pool execution. Reduces emergency stop time from 200+ seconds (sequential) to 20-30 seconds for a 20-agent fleet. Uses 60-second timeout for all futures with `concurrent.futures.as_completed()`. |
| 2026-01-13 | **Report Storage**: Added organized report storage in `~/reports/` with subdirectories per report type. All slash commands now save reports with timestamped filenames. Added `.gitignore` to exclude reports from sync. |
| 2026-01-13 | **UI Consolidation**: Removed dedicated `SystemAgent.vue` (782 lines) and `SystemAgentTerminal.vue` (~350 lines). System agent now uses standard `AgentDetail.vue` with tab filtering (`visibleTabs` computed, lines 414-448). Added `systemAgent` and `sortedAgentsWithSystem` getters to `agents.js` store (lines 25-27, 39-41). System agent visible on Agents page for admins only with purple ring and "SYSTEM" badge (Agents.vue lines 46-75). `/system-agent` route redirects to `/agents/trinity-system`. Schedules tab now available for system agent. |
| 2026-01-12 | **Docker Stats Optimization**: Updated fleet status flow - `list_all_agents_fast()` now used at ops.py:54 instead of `list_all_agents()`. Avoids slow Docker API calls for better dashboard performance (~50ms vs 2-3s). |
| 2026-01-09 | Added Schedule & Execution Management slash commands and `GET /api/ops/schedules` endpoint |
| 2025-12-31 | Added AgentClient service pattern for HTTP communication |

## Related Documents

- `docs/drafts/SYSTEM_AGENT_OPS_REQUIREMENTS.md` - Full ops requirements
- `docs/memory/requirements.md` - Requirements 11.1 and 11.2
- `docs/memory/feature-flows/agent-lifecycle.md` - Agent lifecycle operations
