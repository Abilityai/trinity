# Trinity - Autonomous Agent Orchestration Platform - Architecture

> **Purpose**: Documents the CURRENT system design. Update only when implementing changes.

## System Overview

**Trinity** is an **autonomous agent orchestration and infrastructure platform** — sovereign infrastructure for deploying, orchestrating, and governing fleets of autonomous AI agents on your own hardware.

Each agent runs as an isolated Docker container with standardized interfaces for credentials, tools, and MCP server integrations.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Trinity Agent Platform                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │   Frontend   │  │   Backend    │  │  MCP Server  │  │    Vector    │    │
│  │   (Vue.js)   │  │  (FastAPI)   │  │  (FastMCP)   │  │   (Logs)     │    │
│  │   :80        │  │   :8000      │  │   :8080      │  │   :8686      │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
│         │                 │                 │                 │             │
│         └─────────────────┼─────────────────┼─────────────────┘             │
│                           │                 │                               │
│                    ┌──────┴──────┐   ┌──────┴──────┐                       │
│                    │    Redis    │   │   Docker    │                       │
│                    │    :6379    │   │   Engine    │                       │
│                    └─────────────┘   └──────┬──────┘                       │
│                                             │                               │
│         ┌───────────────────────────────────┼───────────────────────────┐  │
│         │                                   │                           │  │
│    ┌────┴────┐    ┌─────────┐    ┌─────────┴┐    ┌─────────┐           │  │
│    │ Agent 1 │    │ Agent 2 │    │ Agent 3  │    │ Agent N │           │  │
│    │ :8000   │    │ :8000   │    │ :8000    │    │ :8000   │           │  │
│    └─────────┘    └─────────┘    └──────────┘    └─────────┘           │  │
│         Agent Network (172.28.0.0/16)                                   │  │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Technology Stack

### Frontend
| Technology | Version | Purpose |
|------------|---------|---------|
| Vue.js | 3.x | UI framework (Composition API) |
| Vue Flow | 1.48.0 | Node-based graph visualization |
| Tailwind CSS | 3.x | Styling |
| Pinia | 2.x | State management |
| Vite | 5.x | Build system |

### Backend
| Technology | Version | Purpose |
|------------|---------|---------|
| FastAPI | 0.100+ | REST API framework |
| Python | 3.11 | Runtime |
| Docker SDK | 7.x | Container management |
| SQLite | 3.x | Relational data persistence |
| Redis | 7.x | Secrets/cache storage |
| httpx | 0.24+ | Async HTTP client |

### Agent Runtime
| Technology | Version | Purpose |
|------------|---------|---------|
| Python | 3.11 | Primary runtime |
| Node.js | 20 | JavaScript runtime |
| Go | 1.21 | Go runtime |
| Claude Code | Latest | AI agent |

### Infrastructure
| Technology | Purpose |
|------------|---------|
| Docker | Container orchestration |
| nginx | Reverse proxy (production) |
| Cloudflare Tunnel | Public endpoint access (webhooks, public chat) |
| Tailscale | Private VPN access |
| GCP | Cloud hosting |
| Vertex AI Search | Documentation Q&A (public endpoint) |

---

## Component Details

### Backend (`src/backend/`)

**Modular Architecture (refactored 2025-11-29):**

| Module | Purpose |
|--------|---------|
| `main.py` | FastAPI app initialization, WebSocket manager, router mounting |
| `config.py` | Centralized configuration constants |
| `models.py` | All Pydantic request/response models |
| `dependencies.py` | FastAPI dependencies (auth, token validation, role hierarchy, agent access control) |
| `database.py` | SQLite persistence facade — orchestrates 27 domain operation classes from `db/` (users, ownership, MCP keys, schedules, executions, chat, activities, subscriptions, monitoring, audit log, Slack/Telegram, payments, operator queue, skills, tags, …) |
| ~~`credentials.py`~~ | **REMOVED (2026-02-05)** - CRED-002 replaced with `routers/credentials.py` file injection system |

**Routers (`routers/`)** — 53 router modules:

*Core Agent:*
- `agents.py` - Core CRUD, start/stop, logs, stats, queue, activities, terminal (642 lines)
- `agent_config.py` - Per-agent settings: autonomy, read-only, resources, capabilities, capacity, timeout, api-key
- `agent_files.py` - Files, info, playbooks, permissions, metrics, shared folders
- `agent_rename.py` - Rename endpoint (RENAME-001)
- `agent_ssh.py` - SSH access endpoint
- `credentials.py` - Credential injection/export/import (CRED-002 simplified system)
- `chat.py` - Agent chat/activity monitoring
- `chat/` - Chat sub-router directory
- `internal.py` - Internal endpoints for agent startup, scheduler task execution (no auth)
- `templates.py` - Template listing and GitHub repo fetching
- `sharing.py` - Agent sharing between users
- `git.py` - Git sync endpoints (status, sync, log, pull)

*Auth & Security:*
- `auth.py` - Authentication endpoints (admin login, email auth, token validation)
- `users.py` - User management (list users, update roles) (ROLE-001)
- `mcp_keys.py` - MCP API key management
- `setup.py` - First-time setup wizard

*Scheduling & Execution:*
- `schedules.py` - Agent scheduling CRUD and control
- `executions.py` - Execution list and details

*Organization & Tags:*
- `tags.py` - Agent tagging
- `system_views.py` - Saved system views
- `systems.py` - System manifest deployment

*Monitoring & Operations:*
- `monitoring.py` - Fleet health monitoring (MON-001)
- `telemetry.py` - Host telemetry (CPU/memory/disk)
- `activities.py` - Activity timeline endpoints
- `agent_dashboard.py` - Agent-defined dashboard (dashboard.yaml)
- `alerts.py` - Cost threshold alerts
- `notifications.py` - Agent notifications
- `operator_queue.py` - Operating Room queue (OPS-001)
- `ops.py` - Operating Room sync service
- `logs.py` - Container log endpoints
- `observability.py` - Observability data
- `audit.py` - Audit trail

*Public Access & Monetization:*
- `public_links.py` - Public agent link management
- `public.py` - Public chat endpoints
- `paid.py` - x402 payment-gated chat (NVM-001)
- `nevermined.py` - Nevermined payment config management
- `slack.py` - Slack integration (OAuth, events, multi-agent channel routing, per-agent channel binding) (SLACK-001/002)
- `messages.py` - Proactive agent-to-user messaging (#321)

*Subscriptions & Skills:*
- `subscriptions.py` - Subscription management (SUB-002)
- `skills.py` - Skill CRUD and assignment
- `settings.py` - Platform admin settings (includes Slack transport management: connect/disconnect/install)

*Content & Files:*
- `image_generation.py` - Image generation REST endpoints (IMG-001)
- `avatar.py` - Agent avatar generation and serving (AVATAR-001)
- `docs.py` - Documentation endpoints

*System:*
- `system_agent.py` - System agent management

**Services (`services/`)** — 37 service modules:

*Core:*
- `docker_service.py` - Docker container management
- `docker_utils.py` - Docker utility helpers
- `template_service.py` - GitHub template cloning and processing
- `agent_client.py` - HTTP client for agent container communication (chat, session, injection)
- `settings_service.py` - Centralized settings retrieval (API keys, ops config, agent quotas)

*Execution & Scheduling:*
- `task_execution_service.py` - Unified task execution lifecycle (slot mgmt, activity tracking, sanitization) (EXEC-024)
- `slot_service.py` - Parallel execution slot management with dynamic TTL (CAPACITY-001)
- `backlog_service.py` - Persistent SQLite-backed FIFO backlog for async tasks at capacity (BACKLOG-001)
- `execution_queue.py` - Redis-based execution queueing
- `scheduler_service.py` - APScheduler-based scheduling service
- `cleanup_service.py` - Active watchdog reconciliation + passive stale recovery for executions, activities, and slots (CLEANUP-001, #129)

*Real-time delivery:*
- `event_bus.py` - Redis Streams transport for WebSocket delivery (`EventBus` publisher + `StreamDispatcher` consumer, reconnect replay via `last-event-id`, 3-failure client eviction, MAXLEN-trimmed stream) (RELIABILITY-003, #306)

*Monitoring & Activities:*
- `activity_service.py` - Activity tracking and timeline
- `monitoring_service.py` - Fleet-wide health monitoring (MON-001)
- `monitoring_alerts.py` - Alert threshold configuration
- `operator_queue_service.py` - Operating Room sync with agent containers (OPS-001)

*Auth & Credentials:*
- `credential_encryption.py` - AES-256-GCM encryption for .credentials.enc files (CRED-002)
- `subscription_service.py` - Subscription management (SUB-002)
- `ssh_service.py` - Ephemeral SSH credential generation
- `email_service.py` - Email sending for verification codes

*Git & GitHub:*
- `git_service.py` - Git sync operations for GitHub-native agents; persistent-state allowlist primitive (S4, #383)
- `github_service.py` - GitHub API client (repo creation, validation, org detection)

*Integrations:*
- `slack_service.py` - Slack API client (OAuth, messaging, verification) (SLACK-001)
- `nevermined_payment_service.py` - x402 payment verification and settlement (NVM-001)
- `proactive_message_service.py` - Agent-to-user proactive messaging with rate limiting and audit (#321)

**Channel Adapters (`adapters/`)** — Pluggable external messaging (SLACK-002):

*Core:*
- `base.py` - `ChannelAdapter` ABC, `NormalizedMessage`, `ChannelResponse` models
- `message_router.py` - `ChannelMessageRouter`: rate limiting, agent resolution, execution pipeline

*Slack:*
- `slack_adapter.py` - Slack adapter: DMs, @mentions, thread replies, agent identity via `chat:write.customize`
- `transports/slack_socket.py` - Socket Mode transport (WebSocket, auto-reconnect, default)
- `transports/slack_webhook.py` - HTTP webhook transport (fallback for production)

*Database:*
- `db/slack_channels.py` - Workspace connections (encrypted bot tokens), channel-agent bindings, active threads

*Content & Media:*
- `image_generation_service.py` - Platform image generation via Gemini (prompt refinement + image gen) (IMG-001)
- `image_generation_prompts.py` - Best practices prompts for image generation use cases (IMG-001)

*Skills & System:*
- `skill_service.py` - Skill CRUD and injection
- `system_agent_service.py` - System agent lifecycle management
- `system_service.py` - System manifest operations
- `log_archive_service.py` - Log archival
- `archive_storage.py` - Archive storage backend

**Logging (`logging_config.py`):**
- Structured JSON logging for production
- Captured by Vector via Docker stdout/stderr
- OpenTelemetry trace ID included in log entries for log-trace correlation (RELIABILITY-002)

**OpenTelemetry Tracing (`main.py`):**
- Auto-instrumentation for FastAPI, httpx, and Redis (RELIABILITY-002)
- `traceparent` header propagated through inter-agent calls
- Traces exported to OTel Collector via OTLP/gRPC (`trinity-otel-collector:4317`)
- Configurable sampling via `OTEL_SAMPLE_RATE` (default 10%)
- Enabled via `OTEL_ENABLED=1` environment variable

**Utilities (`utils/`):**
- `helpers.py` - Shared helper functions

**Docker Integration:**
- Uses `docker-py` SDK
- Containers labeled with `trinity.*` prefix
- Docker is the source of truth (no in-memory registry)

### Frontend (`src/frontend/`)

**Key Directories:**
- `src/views/` - Page components (Dashboard, Agents, Templates, ApiKeys, AgentCollaboration)
- `src/stores/` - Pinia state (agents.js, auth.js, collaborations.js)
- `src/components/` - Reusable UI components (NavBar, CredentialsPanel, AgentNode)
- `src/utils/` - WebSocket client, helpers

**State Management:**
- `stores/agents.js` - Agent CRUD, chat, activity
- `stores/auth.js` - Email/admin authentication + JWT
- `stores/collaborations.js` - Collaboration graph state, WebSocket integration

**Real-time:**
- WebSocket client at `utils/websocket.js`
- Auto-reconnect on disconnect
- Status update broadcasts
- Tracks `_eid` (Redis stream id) on every incoming message; reconnect URL appends `&last-event-id=<id>` so brief disconnects replay missed events. On `{type: "resync_required"}` the cursor is cleared and authoritative state is refetched via REST (RELIABILITY-003, #306)

**Collaboration Dashboard:**
- Vue Flow for node-based graph visualization
- Real-time collaboration event display
- Animated edges for agent-to-agent communication
- localStorage persistence for node positions

### MCP Server (`src/mcp-server/`)

**Technology:** FastMCP with Streamable HTTP transport

**Port:** 8080 (internal and production)

**Authentication:**
- API key-based authentication via `Authorization: Bearer` header
- FastMCP `authenticate` callback validates keys against backend
- Returns `McpAuthContext` stored in session for tool execution:
  ```typescript
  {
    userId: string,
    userEmail: string,
    keyName: string,
    agentName?: string,  // Set for agent-scoped keys
    scope: "user" | "agent",
    mcpApiKey: string
  }
  ```
- Tools access auth context via `context.session` parameter
- Agent-to-agent collaboration uses agent-scoped keys for access control

**Tools** across 16 tool modules (`src/tools/`):

| Module | Tools | Description |
|--------|-------|-------------|
| `agents.ts` (19) | `list_agents`, `get_agent`, `get_agent_info`, `create_agent`, `rename_agent`, `delete_agent`, `start_agent`, `stop_agent`, `list_templates`, `get_credential_status`, `inject_credentials`, `export_credentials`, `import_credentials`, `get_credential_encryption_key`, `get_agent_ssh_access`, `deploy_local_agent`, `initialize_github_sync`, `get_agent_github_pat_status`, `set_agent_github_pat` | Agent lifecycle, credentials, SSH, local deploy, GitHub sync, per-agent PAT (#347) |
| `chat.ts` (3) | `chat_with_agent`, `get_chat_history`, `get_agent_logs` | Chat (enforces sharing rules), history, logs |
| `schedules.ts` (8) | `list_agent_schedules`, `create_agent_schedule`, `get_agent_schedule`, `update_agent_schedule`, `delete_agent_schedule`, `toggle_agent_schedule`, `trigger_agent_schedule`, `get_schedule_executions` | Schedule CRUD and execution history |
| `executions.ts` (3) | `list_recent_executions`, `get_execution_result`, `get_agent_activity_summary` | Execution queries, async result polling, activity monitoring (MCP-007) |
| `skills.ts` (7) | `list_skills`, `get_skill`, `get_skills_library_status`, `assign_skill_to_agent`, `set_agent_skills`, `sync_agent_skills`, `get_agent_skills` | Skill management and assignment |
| `tags.ts` (5) | `list_tags`, `get_agent_tags`, `tag_agent`, `untag_agent`, `set_agent_tags` | Agent tagging |
| `systems.ts` (4) | `deploy_system`, `list_systems`, `restart_system`, `get_system_manifest` | System manifest deployment |
| `subscriptions.ts` (6) | `register_subscription`, `list_subscriptions`, `assign_subscription`, `clear_agent_subscription`, `get_agent_auth`, `delete_subscription` | Subscription management |
| `monitoring.ts` (3) | `get_fleet_health`, `get_agent_health`, `trigger_health_check` | Fleet health monitoring |
| `nevermined.ts` (4) | `configure_nevermined`, `get_nevermined_config`, `toggle_nevermined`, `get_nevermined_payments` | x402 payment configuration |
| `notifications.ts` (1) | `send_notification` | Agent-to-platform notifications |
| `events.ts` (4) | `emit_event`, `subscribe_to_event`, `list_event_subscriptions`, `delete_event_subscription` | Agent event pub/sub (EVT-001) |
| `docs.ts` (1) | `get_agent_requirements` | Agent documentation |
| `channels.ts` (2) | `list_channel_groups`, `send_group_message` | Channel group discovery and proactive group messaging (#349) |
| `messages.ts` (1) | `send_message` | Proactive user messaging by verified email (#321) |

### Vector Log Aggregator (`config/vector.yaml`)

**Technology:** Vector 0.43.1 (timberio/vector:0.43.1-alpine)

**Features:**
- Captures ALL container stdout/stderr via Docker socket
- Routes platform logs to `/data/logs/platform.json`
- Routes agent logs to `/data/logs/agents.json`
- Enriches with container metadata (name, labels)
- Parses JSON logs for structured querying

**Health Check:** `http://localhost:8686/health`

**Query Logs:**
```bash
# Platform logs
docker exec trinity-vector sh -c "tail -50 /data/logs/platform.json" | jq .

# Agent logs
docker exec trinity-vector sh -c "tail -50 /data/logs/agents.json" | jq .
```

### Agent Containers

**Base Image:** `trinity-agent-base:latest`

**Pre-installed:**
- Python 3.11, Node.js 20, Go 1.21
- Claude Code (latest version)
- Common Python packages (requests, aiohttp)

**Internal Server:** `agent-server.py`
- FastAPI app on port 8000
- `/api/chat` - Claude Code execution (messages persisted to database)
- `/api/health` - Health check
- `/api/credentials/update` - Hot-reload credentials
- `/api/chat/session` - Context window stats
- `/api/files` - List workspace files (recursive tree structure)
- `/api/files/download` - Download file content (100MB limit)

**Persistent Chat:**
- All chat messages automatically saved to SQLite (`chat_sessions`, `chat_messages`)
- Sessions survive container restarts/deletions
- Includes full observability: costs, context usage, tool calls, execution time
- Access control: users see only their own messages (admins see all)

**File Structure:**
```
/home/developer/           # Agent home directory (WORKDIR, all files live here)
├── CLAUDE.md              # Agent instructions (from template)
├── template.yaml          # Agent metadata
├── .env                   # Credentials (KEY=VALUE)
├── .mcp.json              # Generated MCP config
├── .mcp.json.template     # Template with ${VAR} placeholders
├── .claude/               # Claude Code config
├── .trinity/              # Trinity-specific files
│   └── persistent-state.yaml  # S4 allowlist (#383): paths surviving reset
├── content/               # Generated assets (gitignored)
└── [template files...]    # Any other files from template
```

### Process Engine

> **OUT OF SCOPE**: The Process Engine (`src/backend/services/process_engine/`) is not currently being developed. Code exists but is dormant. See `docs/memory/feature-flows/process-engine/` for historical design docs.

### Background Services

Services that run continuously in the backend process:

| Service | Module | Description |
|---------|--------|-------------|
| **Cleanup Service** | `cleanup_service.py` | Active watchdog reconciliation against agent process registries (orphan recovery, auto-terminate timeouts) + passive stale recovery. Runs every 5 min. (CLEANUP-001, #129) |
| **Operator Queue Sync** | `operator_queue_service.py` | Polls running agents every 5s, reads `~/.trinity/operator-queue.json`, syncs to DB, writes responses back. (OPS-001) |
| **Sync Health Service** | `sync_health_service.py` | Polls git-enabled agents every 60s, upserts `agent_sync_state`, emits `sync_failing` operator-queue entries when consecutive_failures ≥ 3. (#389 S1) |
| **Monitoring Service** | `monitoring_service.py` | Fleet-wide health checks on configurable interval. (MON-001) |
| **Scheduler Service** | `scheduler_service.py` | APScheduler-based cron job execution. Async fire-and-forget with DB polling for status. |
| **Backlog Maintenance** | `backlog_service.py` | Expires stale queued tasks (>24h) and drains orphans after restart. Runs every 60s. (BACKLOG-001) |

The **agent server** also runs a 15-min `auto_sync` heartbeat loop (gated
by `GIT_SYNC_AUTO` env var; default-on for non-source-mode GitHub-template
agents) that stages/commits/pushes in-container changes and writes the
outcome to `.trinity/sync-state.json` — which the Sync Health Service
picks up on its next poll. (#389 S1a)

---

## Collaboration Dashboard

**Purpose**: Real-time visualization of agent-to-agent communication

**Features**:
- Draggable agent nodes with status-based colors
- Animated edges during agent-to-agent chats
- WebSocket-driven real-time updates
- Node position persistence (localStorage)
- Collaboration statistics and history panel
- **Replay Mode** - Historical playback of collaboration events with time range filtering
- **Activity Timeline Integration** - Database-backed persistent collaboration history
- Collapsible history panel with live feed and historical sections

**Components**:
- `AgentCollaboration.vue` - Main dashboard view
- `AgentNode.vue` - Custom node component
- `collaborations.js` - Pinia store for graph state

**WebSocket Events**:

1. **agent_collaboration** - Agent-to-agent communication:
```json
{
  "type": "agent_collaboration",
  "source_agent": "agent-a",
  "target_agent": "agent-b",
  "action": "chat",
  "timestamp": "2025-12-01T..."
}
```

2. **agent_activity** - Activity state changes:
```json
{
  "type": "agent_activity",
  "agent_name": "research-agent",
  "activity_id": "uuid",
  "activity_type": "agent_collaboration|chat_start|tool_call|schedule_start|schedule_end",
  "activity_state": "started|completed|failed",
  "action": "Human-readable description",
  "timestamp": "2025-12-01T...",
  "details": {},
  "error": null
}
```

**Detection Mechanism**:
- Backend chat endpoint accepts `X-Source-Agent` header
- If present, broadcasts `agent_collaboration` event via WebSocket
- Activity service broadcasts `agent_activity` events on state changes
- Frontend animates edge between nodes for 3 seconds (collaboration)
- Dashboard displays real-time activity feed (all activity types)

---

## API Endpoints

### Agents (32 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents` | List all agents |
| GET | `/api/agents/context-stats` | Get context & activity state for all agents (NEW: 2025-12-02) |
| GET | `/api/agents/autonomy-status` | Get autonomy status for all accessible agents (NEW: 2026-01-01) |
| GET | `/api/agents/sync-health` | Per-agent git sync health for dashboard dots (NEW: 2026-04-19, #389) |
| POST | `/api/agents` | Create agent |
| GET | `/api/agents/{name}` | Get agent details |
| DELETE | `/api/agents/{name}` | Delete agent |
| POST | `/api/agents/{name}/start` | Start agent |
| POST | `/api/agents/{name}/stop` | Stop agent |
| POST | `/api/agents/{name}/chat` | Send chat message |
| GET | `/api/agents/{name}/chat/history` | Get in-memory chat history (container) |
| GET | `/api/agents/{name}/chat/history/persistent` | Get persistent chat history (database) |
| GET | `/api/agents/{name}/chat/sessions` | List all chat sessions for agent |
| GET | `/api/agents/{name}/chat/sessions/{id}` | Get session details with messages |
| POST | `/api/agents/{name}/chat/sessions/{id}/close` | Close chat session |
| DELETE | `/api/agents/{name}/chat/history` | Reset session |
| GET | `/api/agents/{name}/logs` | Get container logs |
| GET | `/api/agents/{name}/stats` | Get live telemetry |
| GET | `/api/agents/{name}/activity` | Get activity summary |
| GET | `/api/agents/{name}/info` | Get template metadata |
| GET | `/api/agents/{name}/files` | List workspace files (tree structure) |
| GET | `/api/agents/{name}/files/download` | Download file |
| GET | `/api/agents/{name}/folders` | Get shared folder config (NEW: 2025-12-13) |
| PUT | `/api/agents/{name}/folders` | Update shared folder config |
| GET | `/api/agents/{name}/folders/available` | List mountable folders from permitted agents |
| GET | `/api/agents/{name}/folders/consumers` | List agents that will mount this folder |
| GET | `/api/agents/{name}/autonomy` | Get autonomy status with schedule counts (NEW: 2026-01-01) |
| PUT | `/api/agents/{name}/autonomy` | Enable/disable autonomy (toggles all schedules) |
| POST | `/api/agents/{name}/ssh-access` | Generate ephemeral SSH credentials (admin-only) |
| GET | `/api/agents/{name}/read-only` | Get read-only mode status and config (NEW: 2026-02-17) |
| PUT | `/api/agents/{name}/read-only` | Enable/disable read-only mode (blocks source file writes) |
| GET | `/api/agents/{name}/timeout` | Get execution timeout setting (NEW: 2026-03-12) |
| PUT | `/api/agents/{name}/timeout` | Set execution timeout (60-7200s, default 900s = 15min) |
| GET | `/api/agents/{name}/guardrails` | Get per-agent guardrails config (NEW: 2026-04-15) |
| PUT | `/api/agents/{name}/guardrails` | Set per-agent guardrails overrides (GUARD-001) |

**Note**: Route ordering is critical. `/context-stats` and `/autonomy-status` must be defined BEFORE `/{name}` catch-all route to avoid 404 errors.

### Activities (1 endpoint)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/activities/timeline` | Cross-agent activity timeline with filtering |

**Query Parameters:**
- `start_time` - ISO 8601 timestamp (e.g., "2025-12-01T00:00:00Z")
- `end_time` - ISO 8601 timestamp
- `activity_types` - Comma-separated types (e.g., "agent_collaboration,chat_start")
- `limit` - Max results (default 100)

**Access Control:** Only returns activities for agents the user can access (owner, shared, or admin).

### Credentials (4 endpoints - CRED-002 Simplified)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents/{name}/credentials/status` | Check credential files in agent |
| POST | `/api/agents/{name}/credentials/inject` | Inject files directly to agent (NEW) |
| POST | `/api/agents/{name}/credentials/export` | Export to .credentials.enc (NEW) |
| POST | `/api/agents/{name}/credentials/import` | Import from encrypted file (NEW) |

### GitHub PAT (3 endpoints - #347)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents/{name}/github-pat` | Get PAT config status (agent vs global) |
| PUT | `/api/agents/{name}/github-pat` | Set per-agent GitHub PAT (validated, encrypted) |
| DELETE | `/api/agents/{name}/github-pat` | Clear per-agent PAT (revert to global) |
| GET | `/api/agents/{name}/git/auto-sync` | Read per-agent auto-sync flag (NEW: 2026-04-19, #389) |
| PUT | `/api/agents/{name}/git/auto-sync` | Toggle 15-min auto-sync heartbeat |
| GET | `/api/agents/{name}/git/freeze-schedules-if-failing` | Read freeze-on-sync-failure flag |
| PUT | `/api/agents/{name}/git/freeze-schedules-if-failing` | Toggle freeze-on-sync-failure flag |
| GET | `/api/agents/{name}/git/sync-state` | Persisted sync-state row (#389) |

### Git Recovery (1 endpoint - S3, #384)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agents/{name}/git/reset-to-main-preserve-state` | Adopt `origin/main`, snapshot persistent-state allowlist (S4) first, overlay back, force-with-lease push. Safe recovery for parallel-history deadlock (P2/P3). 409 with `X-Conflict-Type: agent_busy \| no_git_config \| no_remote_main`. |

### Internal (1 endpoint - no auth)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/internal/decrypt-and-inject` | Auto-import on agent startup (NEW) |

### Templates (4 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/templates` | List templates |
| GET | `/api/templates/{id}` | Get template details |
| GET | `/api/templates/env-template` | Get env template |
| POST | `/api/templates/refresh` | Refresh cache |

### Sharing & Access Control (6 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agents/{name}/share` | Share agent |
| DELETE | `/api/agents/{name}/share/{email}` | Remove share |
| GET | `/api/agents/{name}/shares` | List shares |
| GET | `/api/agents/{name}/access-policy` | Get cross-channel access policy (#311) |
| PUT | `/api/agents/{name}/access-policy` | Set `require_email` / `open_access` flags |
| GET | `/api/agents/{name}/access-requests` | List pending access requests |
| POST | `/api/agents/{name}/access-requests/{id}/decide` | Approve (auto-shares) or reject |

### Schedules (9 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents/{name}/schedules` | List schedules |
| POST | `/api/agents/{name}/schedules` | Create schedule |
| GET | `/api/agents/{name}/schedules/{id}` | Get schedule |
| PUT | `/api/agents/{name}/schedules/{id}` | Update schedule |
| DELETE | `/api/agents/{name}/schedules/{id}` | Delete schedule |
| POST | `/api/agents/{name}/schedules/{id}/enable` | Enable schedule |
| POST | `/api/agents/{name}/schedules/{id}/disable` | Disable schedule |
| POST | `/api/agents/{name}/schedules/{id}/trigger` | Manual trigger |
| GET | `/api/agents/{name}/schedules/{id}/executions` | Execution history |

### Auth, Users & MCP (15 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/auth/mode` | Get auth mode config - unauthenticated |
| POST | `/api/token` | Admin login (username/password) |
| POST | `/api/auth/email/request` | Request email verification code |
| POST | `/api/auth/email/verify` | Verify email code and login |
| GET | `/api/auth/validate` | Validate JWT (for nginx auth_request) |
| GET | `/api/users/me` | Current user |
| GET | `/api/users` | List all users with roles (admin-only, ROLE-001) |
| PUT | `/api/users/{username}/role` | Update user role (admin-only, ROLE-001) |
| GET | `/api/mcp/info` | MCP server info |
| POST | `/api/mcp/keys` | Create API key |
| GET | `/api/mcp/keys` | List API keys |
| DELETE | `/api/mcp/keys/{id}` | Delete API key |
| GET | `/oauth/{provider}/authorize` | Start OAuth |
| GET | `/oauth/{provider}/callback` | OAuth callback |
| GET | `/api/health` | Health check |

### Fleet Sync Audit (#390 / S6)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/fleet/sync-audit` | Aggregate per-agent sync state + `duplicate_binding` flag. Admins see all; non-admins see accessible agents. |

The `duplicate_binding` field flags agents whose
`(github_repo, working_branch)` pair is shared with another non-source-mode
agent — detects the §P5 silent-clobber setup at fleet level.


### Operator Queue (OPS-001)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/operator-queue` | List queue items (filters: status, type, priority, agent_name, since) |
| GET | `/api/operator-queue/stats` | Queue statistics (counts by status/type/priority/agent) |
| GET | `/api/operator-queue/{id}` | Get single queue item |
| POST | `/api/operator-queue/{id}/respond` | Submit operator response |
| POST | `/api/operator-queue/{id}/cancel` | Cancel pending item |
| GET | `/api/operator-queue/agents/{name}` | Items for specific agent |

**WebSocket Events (Operator Queue):**
- `operator_queue_new` — New items synced from agent
- `operator_queue_responded` — Operator responded to item
- `operator_queue_acknowledged` — Agent acknowledged response

**Background Service:** `OperatorQueueSyncService` polls running agents every 5s, reads `~/.trinity/operator-queue.json`, syncs to DB, writes responses back.

### Platform Audit Log (SEC-001 — Phase 1, NEW: 2026-04-14)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/audit-log` | Admin | List entries (filters: event_type, actor_type, actor_id, target_type, target_id, source, start_time, end_time, limit, offset) |
| GET | `/api/audit-log/stats` | Admin | Aggregate counts by event_type and actor_type |
| GET | `/api/audit-log/{event_id}` | Admin | Single entry by UUID |

**Storage**: append-only `audit_log` table in main SQLite DB. SQLite triggers block UPDATE unconditionally and DELETE within the 365-day retention window.

**Phase 1 + agent lifecycle smoke test.** Phase 1 ships infrastructure; Phase 2a ships agent lifecycle audit. Remaining integrations (auth, sharing, settings, credentials — Phase 2b), MCP TypeScript audit (Phase 3), and hash-chain verification (Phase 4) follow as separate PRs.

### Nevermined Payments (NVM-001)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/paid/{agent_name}/chat` | x402 | Paid chat (402/403/200) |
| GET | `/api/paid/{agent_name}/info` | None | Payment requirements |
| POST | `/api/nevermined/agents/{name}/config` | JWT | Configure payments |
| GET | `/api/nevermined/agents/{name}/config` | JWT | Get config |
| DELETE | `/api/nevermined/agents/{name}/config` | JWT | Remove config |
| PUT | `/api/nevermined/agents/{name}/config/toggle` | JWT | Enable/disable |
| GET | `/api/nevermined/agents/{name}/payments` | JWT | Payment history |
| GET | `/api/nevermined/settlement-failures` | Admin | Failed settlements |
| POST | `/api/nevermined/retry-settlement/{log_id}` | Admin | Retry settlement |

### Platform Settings (3 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/settings/mcp-url` | Get configured MCP server URL (any auth user) |
| PUT | `/api/settings/mcp-url` | Set MCP server URL (admin-only) |
| DELETE | `/api/settings/mcp-url` | Reset to auto-detect (admin-only) |

---

## Architectural Invariants

These are structural patterns that must be preserved. Breaking them causes cascading issues.

1. **Three-Layer Backend: Router → Service → DB** — Every feature follows `routers/X.py` → `services/X_service.py` → `db/X.py`. Routers hold no business logic, services hold no SQL, db modules hold no HTTP concerns.

2. **DB Layer: Class-per-domain with Mixin Composition** — Each `db/` file defines an `XOperations` class. Agent-specific settings use mixins (`db/agent_settings/`) composed into `AgentOperations`. New agent settings → new mixin, not a bigger class.

3. **Schema in `db/schema.py`, Migrations in `db/migrations.py`** — All table DDL lives in `schema.py`. Schema changes require a versioned migration in `migrations.py`. Never create tables ad-hoc in service code.

4. **Router Registration Order Matters** — In `main.py`, static routes like `/api/agents/context-stats` must come before `/{name}` catch-all. New collection-level agent endpoints must be registered before parameterized routes.

5. **Agent Server Mirrors Backend (Subset)** — `docker/base-image/agent_server/routers/` has routers that mirror a subset of backend routers (chat, credentials, files, git, skills, dashboard). The backend proxies to the agent server. Changes to agent-internal APIs must update both sides.

6. **Frontend: Store = Domain, View = Page** — Pinia stores (`stores/agents.js`) are domain-scoped, not view-scoped. Views compose from multiple stores. Composables (`composables/use*.js`) extract reusable logic. API calls go through stores, not views directly.

7. **Single API Client (`api.js`)** — One Axios instance with auth interceptor. Stores call `api.get()`/`api.post()`. No raw `fetch()` or duplicate Axios instances.

8. **Auth Pattern: `Depends(get_current_user)` + `AuthorizedAgent`** — Every authenticated endpoint uses FastAPI `Depends()` for auth. Agent-scoped endpoints use `AuthorizedAgent` or `OwnedAgentByName` for access control. Role-gated endpoints use `require_role("creator")` or `require_admin` (ROLE-001). `internal.py` is the only exception (no auth, for agent-to-backend calls).

9. **Channel Adapter ABC** — External messaging (Slack, Telegram) follows `adapters/base.py` → `ChannelAdapter` ABC with `NormalizedMessage` and `ChannelResponse`. New channels must implement this interface.

10. **WebSocket Events for Real-Time** — All real-time updates go through WebSocket broadcast (`agent_activity`, `agent_collaboration`). Frontend subscribes via `utils/websocket.js`. Don't poll for state that should be pushed. Transport is the Redis Streams event bus in `services/event_bus.py` (RELIABILITY-003, #306) — `ConnectionManager` / `FilteredWebSocketManager` are thin shims that `XADD` to `trinity:events`; the `StreamDispatcher` runs one `XREAD BLOCK` per backend process and fans out to registered clients. New broadcast sites should continue calling the existing `manager.broadcast(...)` / `filtered_manager.broadcast_filtered(...)` API — do not bypass it to publish directly.

11. **Docker as Source of Truth** — Agent container state comes from Docker labels (`trinity.*`), not from an in-memory registry. `docker_service.py` is the single point of Docker interaction.

12. **Credentials: File Injection, Never Stored in DB** — Credentials use `.env` files injected into containers (CRED-002). Encrypted exports use AES-256-GCM (`.credentials.enc`). Redis holds transient secrets. Never persist credential values in SQLite.

13. **MCP Server = Third Surface in Sync** — The MCP server (`src/mcp-server/src/tools/*.ts`) is a TypeScript proxy over the backend API. When adding a backend endpoint for external access, the MCP tool module needs updating too. Three surfaces must stay in sync: backend router, agent server (if internal), MCP tool (if external).

14. **Pydantic Models Centralized in `models.py`** — Request/response models live in `models.py`, not scattered across routers. Keeps the API contract in one place.

15. **API URL Nesting Convention** — Agent-scoped resources nest under `/api/agents/{name}/...`. Platform-wide resources get top-level prefixes (`/api/executions`, `/api/operator-queue`).

---

## Database Schema

### SQLite (`/data/trinity.db`)

**users:**
```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    role TEXT NOT NULL DEFAULT 'user',  -- ROLE-001: admin, creator, operator, user
    auth0_sub TEXT UNIQUE,
    name TEXT,
    picture TEXT,
    email TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login TEXT
);
```

**agent_ownership:**
```sql
CREATE TABLE agent_ownership (
    agent_name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    max_parallel_tasks INTEGER DEFAULT 3,          -- CAPACITY-001
    execution_timeout_seconds INTEGER DEFAULT 900, -- TIMEOUT-001 (15 min)
    require_email INTEGER DEFAULT 0,               -- #311: gate channels on verified email
    open_access INTEGER DEFAULT 0,                 -- #311: anyone with verified email can chat
    FOREIGN KEY (owner_id) REFERENCES users(id)
);
```

**agent_sharing:** (cross-channel allow-list — same email admits the user on web, Telegram, and Slack)
```sql
CREATE TABLE agent_sharing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    shared_with_email TEXT NOT NULL,
    shared_by_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(agent_name, shared_with_email),
    FOREIGN KEY (shared_by_id) REFERENCES users(id)
);
```

**access_requests:** (#311 — Unified Channel Access Control)
```sql
CREATE TABLE access_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    email TEXT NOT NULL,                  -- verified email of requester
    channel TEXT NOT NULL,                -- 'web' | 'telegram' | 'slack'
    status TEXT NOT NULL DEFAULT 'pending', -- pending, approved, rejected
    decided_by TEXT,                      -- user_id of approver
    decided_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(agent_name, email)
);
```

**telegram_chat_links:** (#311 — verified-email binding for Telegram identities)
```sql
-- New columns added by access_control migration:
ALTER TABLE telegram_chat_links ADD COLUMN verified_email TEXT;
ALTER TABLE telegram_chat_links ADD COLUMN verified_at TEXT;
```

**Access Control Flow:**
- `ChannelAdapter.resolve_verified_email()` translates native channel identity → verified email.
- `message_router` runs a single gate: owner/admin/`agent_sharing` → `open_access` → upsert pending `access_requests` row.
- Approving a request inserts into `agent_sharing` and (if email auth is enabled) whitelists the email.
- Group chats bypass the gate; agents with both policy flags off retain legacy permissive behavior (backward compatibility).

**mcp_api_keys:**
```sql
CREATE TABLE mcp_api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    key_hash TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used TIMESTAMP,
    use_count INTEGER DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

**agent_schedules:**
```sql
CREATE TABLE agent_schedules (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    name TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    message TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    timezone TEXT DEFAULT 'UTC',
    description TEXT,
    owner_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_at TEXT,
    next_run_at TEXT,
    model TEXT,                                  -- MODEL-001: Model override (NULL = agent default)
    FOREIGN KEY (owner_id) REFERENCES users(id)
);
```

**schedule_executions:**
```sql
CREATE TABLE schedule_executions (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    message TEXT NOT NULL,
    response TEXT,
    error TEXT,
    triggered_by TEXT NOT NULL,
    model_used TEXT,                             -- MODEL-001: Which model was used
    queued_at TEXT,                              -- BACKLOG-001: When task entered backlog
    backlog_metadata TEXT,                       -- BACKLOG-001: JSON identity/request for drain replay
    FOREIGN KEY (schedule_id) REFERENCES agent_schedules(id)
);

-- BACKLOG-001: Partial index for cheap atomic FIFO claim
CREATE INDEX idx_executions_queued ON schedule_executions(agent_name, queued_at)
    WHERE status = 'queued';
```

**agent_activities:** (Phase 9.7 - Unified Activity Stream)
```sql
CREATE TABLE agent_activities (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    activity_type TEXT NOT NULL,            -- chat_start, chat_end, tool_call, schedule_start, schedule_end, agent_collaboration
    activity_state TEXT NOT NULL,           -- started, completed, failed
    parent_activity_id TEXT,                -- Link to parent activity (tool → chat)
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    user_id INTEGER,
    triggered_by TEXT NOT NULL,             -- user, schedule, agent, system
    related_chat_message_id TEXT,           -- FK to chat_messages (observability link)
    related_execution_id TEXT,              -- FK to schedule_executions (observability link)
    details TEXT,                           -- JSON: tool_name, target_agent, etc.
    error TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (parent_activity_id) REFERENCES agent_activities(id),
    FOREIGN KEY (related_chat_message_id) REFERENCES chat_messages(id),
    FOREIGN KEY (related_execution_id) REFERENCES schedule_executions(id)
);

-- Indexes for agent_activities (optimized for dashboard queries)
CREATE INDEX idx_activities_agent ON agent_activities(agent_name, created_at DESC);
CREATE INDEX idx_activities_type ON agent_activities(activity_type);
CREATE INDEX idx_activities_state ON agent_activities(activity_state);
CREATE INDEX idx_activities_user ON agent_activities(user_id);
CREATE INDEX idx_activities_parent ON agent_activities(parent_activity_id);
CREATE INDEX idx_activities_chat_msg ON agent_activities(related_chat_message_id);
CREATE INDEX idx_activities_execution ON agent_activities(related_execution_id);
```

**Data Strategy:**
- `chat_messages.tool_calls` - Aggregated JSON summary (backward compatible)
- `agent_activities` - Granular tool tracking (one row per tool call)
- Observability fields (cost, context) stored in chat_messages/schedule_executions only
- Activity queries use JOINs to fetch observability data when needed

**chat_sessions:** (Phase 9.5 - Persistent Chat Tracking)
```sql
CREATE TABLE chat_sessions (
    id TEXT PRIMARY KEY,                  -- Unique session ID (urlsafe token)
    agent_name TEXT NOT NULL,             -- Agent name
    user_id INTEGER NOT NULL,             -- User ID (FK to users table)
    user_email TEXT NOT NULL,             -- User email for quick lookup
    started_at TEXT NOT NULL,             -- ISO timestamp of first message
    last_message_at TEXT NOT NULL,        -- ISO timestamp of most recent message
    message_count INTEGER DEFAULT 0,      -- Total messages (user + assistant)
    total_cost REAL DEFAULT 0.0,          -- Cumulative cost in USD
    total_context_used INTEGER DEFAULT 0, -- Latest context tokens used
    total_context_max INTEGER DEFAULT 200000, -- Latest context window size
    status TEXT DEFAULT 'active',         -- 'active' or 'closed'
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Indexes for chat_sessions
CREATE INDEX idx_chat_sessions_agent ON chat_sessions(agent_name);
CREATE INDEX idx_chat_sessions_user ON chat_sessions(user_id);
CREATE INDEX idx_chat_sessions_status ON chat_sessions(status);
```

**chat_messages:** (Phase 9.5 - Persistent Chat Tracking)
```sql
CREATE TABLE chat_messages (
    id TEXT PRIMARY KEY,                  -- Unique message ID (urlsafe token)
    session_id TEXT NOT NULL,             -- FK to chat_sessions
    agent_name TEXT NOT NULL,             -- Agent name (denormalized for queries)
    user_id INTEGER NOT NULL,             -- User ID (denormalized)
    user_email TEXT NOT NULL,             -- User email (denormalized)
    role TEXT NOT NULL,                   -- 'user' or 'assistant'
    content TEXT NOT NULL,                -- Message content
    timestamp TEXT NOT NULL,              -- ISO timestamp
    cost REAL,                            -- Cost for assistant messages (NULL for user)
    context_used INTEGER,                 -- Tokens used (assistant only)
    context_max INTEGER,                  -- Context window size (assistant only)
    tool_calls TEXT,                      -- JSON array of tool executions (assistant only)
    execution_time_ms INTEGER,            -- Execution duration (assistant only)
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Indexes for chat_messages
CREATE INDEX idx_chat_messages_session ON chat_messages(session_id);
CREATE INDEX idx_chat_messages_agent ON chat_messages(agent_name);
CREATE INDEX idx_chat_messages_user ON chat_messages(user_id);
CREATE INDEX idx_chat_messages_timestamp ON chat_messages(timestamp);
```

**Persistent Chat Features:**
- Chat sessions survive agent restarts and container deletions
- Auto-created per user+agent combination
- Tracks cumulative costs and context usage
- Full observability metadata stored per message
- Access control: users see only their own messages (admins see all)

**agent_permissions:** (Phase 9.10 - Agent Permissions)
```sql
CREATE TABLE agent_permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_agent TEXT NOT NULL,           -- Agent making calls
    target_agent TEXT NOT NULL,           -- Agent being called
    granted_by TEXT NOT NULL,             -- User ID who granted permission
    created_at TEXT NOT NULL,
    UNIQUE(source_agent, target_agent),
    FOREIGN KEY (granted_by) REFERENCES users(id)
);
CREATE INDEX idx_agent_permissions_source ON agent_permissions(source_agent);
CREATE INDEX idx_agent_permissions_target ON agent_permissions(target_agent);
```

**agent_shared_folder_config:** (Phase 9.11 - Agent Shared Folders)
```sql
CREATE TABLE agent_shared_folder_config (
    agent_name TEXT PRIMARY KEY,
    expose_enabled INTEGER DEFAULT 0,     -- 1 = expose /home/developer/shared-out
    consume_enabled INTEGER DEFAULT 0,    -- 1 = mount permitted agents' folders
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_shared_folders_expose ON agent_shared_folder_config(expose_enabled);
CREATE INDEX idx_shared_folders_consume ON agent_shared_folder_config(consume_enabled);
```

**Shared Folders Features:**
- Agents expose a folder via Docker volume at `/home/developer/shared-out`
- Consuming agents mount permitted agents' volumes at `/home/developer/shared-in/{agent}`
- Permission-gated: only agents with permissions (via `agent_permissions`) can mount
- Container recreation on restart when mount config changes
- Volume ownership automatically fixed to UID 1000

**agent_event_subscriptions:** (EVT-001 - Agent Event Pub/Sub)
```sql
CREATE TABLE agent_event_subscriptions (
    id TEXT PRIMARY KEY,
    subscriber_agent TEXT NOT NULL,       -- Agent receiving events
    source_agent TEXT NOT NULL,           -- Agent emitting events
    event_type TEXT NOT NULL,             -- Namespaced event type
    target_message TEXT NOT NULL,         -- Message template with {{payload.field}}
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(subscriber_agent, source_agent, event_type)
);
CREATE TABLE agent_events (
    id TEXT PRIMARY KEY,
    source_agent TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT,                         -- JSON
    subscriptions_triggered INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
```

**slack_workspaces:** (SLACK-002 - Channel Adapters)
```sql
CREATE TABLE slack_workspaces (
    id TEXT PRIMARY KEY,
    team_id TEXT UNIQUE NOT NULL,          -- Slack workspace team ID
    team_name TEXT,                        -- Workspace display name
    bot_token TEXT NOT NULL,               -- Bot OAuth token
    connected_by TEXT,                     -- User who connected
    connected_at TEXT NOT NULL,
    enabled INTEGER DEFAULT 1
);
```

**slack_channel_agents:** (SLACK-002 - Channel Adapters)
```sql
CREATE TABLE slack_channel_agents (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,                 -- FK to slack_workspaces.team_id
    slack_channel_id TEXT NOT NULL,        -- Slack channel/DM ID
    slack_channel_name TEXT,               -- Channel display name
    agent_name TEXT NOT NULL,              -- Trinity agent name
    is_dm_default INTEGER DEFAULT 0,      -- 1 = default agent for DMs
    created_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(team_id, slack_channel_id)
);
```

**slack_active_threads:** (SLACK-002 - Channel Adapters)
```sql
CREATE TABLE slack_active_threads (
    team_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,               -- Slack thread timestamp
    agent_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(team_id, channel_id, thread_ts)
);
```

**operator_queue:** (OPS-001 - Operating Room, NEW: 2026-03-07)
```sql
CREATE TABLE operator_queue (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    type TEXT NOT NULL,                -- approval, question, alert
    status TEXT NOT NULL DEFAULT 'pending', -- pending, responded, acknowledged, expired, cancelled
    priority TEXT NOT NULL DEFAULT 'medium', -- critical, high, medium, low
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    options TEXT,                       -- JSON array (approval choices)
    context TEXT,                       -- JSON metadata from agent
    execution_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    response TEXT,
    response_text TEXT,
    responded_by_id TEXT,
    responded_by_email TEXT,
    responded_at TEXT,
    acknowledged_at TEXT,
    FOREIGN KEY (responded_by_id) REFERENCES users(id)
);
CREATE INDEX idx_opqueue_status ON operator_queue(status);
CREATE INDEX idx_opqueue_agent ON operator_queue(agent_name);
CREATE INDEX idx_opqueue_priority ON operator_queue(priority);
CREATE INDEX idx_opqueue_created ON operator_queue(created_at);
CREATE INDEX idx_opqueue_agent_status ON operator_queue(agent_name, status);
```

**agent_sync_state:** (Issue #389 — Sync health observability, NEW: 2026-04-19)
```sql
CREATE TABLE agent_sync_state (
    agent_name TEXT PRIMARY KEY,
    last_sync_at TEXT,
    last_sync_status TEXT,                 -- 'success' | 'failed' | 'never'
    consecutive_failures INTEGER DEFAULT 0,
    last_error_summary TEXT,
    last_remote_sha_main TEXT,
    last_remote_sha_working TEXT,
    ahead_main INTEGER DEFAULT 0,
    behind_main INTEGER DEFAULT 0,
    ahead_working INTEGER DEFAULT 0,       -- #389 P6: working-branch divergence
    behind_working INTEGER DEFAULT 0,
    last_check_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
);
CREATE INDEX idx_sync_state_status
    ON agent_sync_state(last_sync_status, consecutive_failures);

-- Also adds to agent_git_config:
--   auto_sync_enabled INTEGER DEFAULT 0
--   freeze_schedules_if_sync_failing INTEGER DEFAULT 0
```

**agent_sync_state Features:**
- One row per agent; upserted by `SyncHealthService` every 60s.
- `consecutive_failures` incremented on `failed`, reset on `success`.
- `ahead_working`/`behind_working` fix P6 (external writes to the working
  branch now visible in `GET /api/git/status`).
- Powers the dashboard sync-health dot + `sync_failing` operator-queue
  alerts + `/api/fleet/sync-audit` aggregator.


**audit_log:** (SEC-001 / Issue #20 — Phase 1, NEW: 2026-04-14)
```sql
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,         -- UUID, generated by service layer
    event_type TEXT NOT NULL,              -- AuditEventType (agent_lifecycle, authentication, ...)
    event_action TEXT NOT NULL,            -- specific action ("create", "login_success", etc.)
    actor_type TEXT NOT NULL,              -- user | agent | mcp_client | system
    actor_id TEXT,                         -- user.id, agent_name, or mcp key id
    actor_email TEXT,
    actor_ip TEXT,
    mcp_key_id TEXT,
    mcp_key_name TEXT,
    mcp_scope TEXT,                        -- user | agent | system
    target_type TEXT,
    target_id TEXT,
    timestamp TEXT NOT NULL,               -- ISO 8601 UTC
    details TEXT,                          -- JSON payload, event-specific
    request_id TEXT,                       -- request correlation id
    source TEXT NOT NULL,                  -- api | mcp | scheduler | system
    endpoint TEXT,                         -- request path
    previous_hash TEXT,                    -- Phase 4 (hash chain — dormant)
    entry_hash TEXT,                       -- Phase 4
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_audit_log_timestamp ON audit_log(timestamp DESC);
CREATE INDEX idx_audit_log_event_type ON audit_log(event_type, timestamp DESC);
CREATE INDEX idx_audit_log_actor ON audit_log(actor_type, actor_id, timestamp DESC);
CREATE INDEX idx_audit_log_target ON audit_log(target_type, target_id, timestamp DESC);
CREATE INDEX idx_audit_log_mcp_key ON audit_log(mcp_key_id, timestamp DESC);
CREATE INDEX idx_audit_log_request ON audit_log(request_id);

-- Append-only enforcement at the database layer
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'Audit log entries cannot be modified'); END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
WHEN OLD.timestamp > datetime('now', '-365 days')
BEGIN SELECT RAISE(ABORT, 'Audit log entries cannot be deleted within retention period'); END;
```

**audit_log Features:**
- Append-only via SQLite triggers (UPDATE blocked unconditionally, DELETE blocked within 365-day retention)
- Cross-cutting platform audit for lifecycle, auth, MCP, credentials events
- Phase 1 ships infrastructure only; write integration into routers happens in Phase 2

### Redis

**Credential Storage (DEPRECATED - CRED-002):**
> Note: Credential storage moved to encrypted files in git. Redis storage kept for backward compatibility.

```
credentials:{id}:metadata → HASH { id, name, service, type, user_id, ... }
credentials:{id}:secret → STRING (JSON blob of secret values)
user:{user_id}:credentials → SET of credential IDs
agent:{name}:credentials → SET of assigned credential IDs (deprecated)
```

**New Credential Storage (CRED-002):**
Credentials are now stored as files in agent workspaces:
- `.env` - Source of truth for KEY=VALUE credentials
- `.credentials.enc` - Encrypted backup (AES-256-GCM, safe for git)

**OAuth State:**
```
oauth_state:{state} → {
    "provider": "google",
    "redirect_uri": "...",
    "user_id": "..."
}
```

---

## Authentication & Authorization Architecture

Trinity has multiple authentication layers for different component interactions:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    Authentication & Authorization Flow                            │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                   │
│   [Human User]                                                                    │
│        │                                                                          │
│        │ (1) User Auth: JWT via Email verification or Admin login                  │
│        ▼                                                                          │
│   ┌─────────┐    JWT Token    ┌─────────────┐                                    │
│   │ Browser │───────────────►│   Backend   │                                    │
│   └────┬────┘                 │   FastAPI   │                                    │
│        │                      └──────┬──────┘                                    │
│        │                             │                                            │
│   [Claude Code Client]               │                                            │
│        │                             │                                            │
│        │ (2) MCP API Key             │                                            │
│        ▼                             │                                            │
│   ┌───────────┐  Validates Key  ┌────┴────┐                                      │
│   │ MCP Server│◄───────────────►│ Backend │                                      │
│   │  FastMCP  │                 └────┬────┘                                      │
│   └─────┬─────┘                      │                                            │
│         │                            │                                            │
│         │ (3) Agent MCP Key          │                                            │
│         ▼                            │                                            │
│   ┌─────────────┐  (4) Permissions   │                                            │
│   │ Agent A     │◄──────────────────►│                                            │
│   │ Container   │    Database        │                                            │
│   └──────┬──────┘                    │                                            │
│          │                           │                                            │
│          │ (5) External Credentials  │                                            │
│          ▼                           │                                            │
│   ┌─────────────┐   (6) Hot-reload   │                                            │
│   │ External    │◄──────────────────►│                                            │
│   │ Services    │    via Redis       │                                            │
│   └─────────────┘                    │                                            │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 1. User Authentication (Human → Platform)

Users authenticate to the Trinity web UI and API.

| Mode | Flow | Token |
|------|------|-------|
| **Email** (primary) | Email → 6-digit code → `POST /api/auth/email/verify` | JWT with `mode: "email"` |
| **Admin** (secondary) | Password → `POST /api/token` | JWT with `mode: "admin"` |

- Email whitelist controls who can login via email
- Admin login always available for 'admin' user
- **4-tier role hierarchy** (ROLE-001): `user` < `operator` < `creator` < `admin`. Agent creation requires `creator` or above. Enforced via `require_role()` dependency factory in `dependencies.py`.
- **Whitelist-driven role on first login** (#314): New email users inherit the `default_role` recorded on their `email_whitelist` row (fallback `user` if no row or NULL). Callsites pass explicit intent — `/share` and access-request approvals → `user` (chat-only grant); public `/api/access/request` self-signup → `user`; admin whitelist UI → caller-specified, defaults to `user`. Owners promote collaborators to `creator` explicitly via `PUT /api/users/{username}/role`. This closes a privilege-escalation where any access grant silently promoted the recipient to `creator` on first web login.

### 2. MCP API Keys (User → MCP Server)

External Claude Code clients authenticate to Trinity MCP Server using MCP API Keys.

| Component | Details |
|-----------|---------|
| **Creation** | User creates via UI `/api-keys` page |
| **Format** | `trinity_mcp_{random}` (44 chars) |
| **Storage** | SHA-256 hash in SQLite |
| **Transport** | `Authorization: Bearer trinity_mcp_...` header |
| **Validation** | MCP Server calls `POST /api/mcp/validate` |

**Client Configuration** (`.mcp.json`):
```json
{
  "mcpServers": {
    "trinity": {
      "type": "http",
      "url": "http://localhost:8080/mcp",
      "headers": { "Authorization": "Bearer trinity_mcp_..." }
    }
  }
}
```

### 3. MCP Server → Backend (Key Passthrough)

The MCP server authenticates backend API calls using the user's MCP API key.

| Step | Action |
|------|--------|
| 1 | MCP Server receives request with user's MCP API key |
| 2 | FastMCP `authenticate` callback validates key via backend |
| 3 | Returns `McpAuthContext` with userId, email, scope |
| 4 | MCP tools use user's key for backend API calls |
| 5 | Backend `get_current_user()` validates JWT OR MCP API key |

**Key Point**: In production (`MCP_REQUIRE_API_KEY=true`), MCP server has NO admin credentials. All API calls use the user's MCP key.

### 4. Agent MCP Keys (Agent → Trinity MCP)

Each agent gets an auto-generated MCP API key for agent-to-agent collaboration.

| Property | Value |
|----------|-------|
| **Scope** | `agent` (vs `user` for human users) |
| **Agent Name** | Stored with key for permission checks |
| **Injection** | Auto-added to agent's `.mcp.json` on creation |
| **Environment** | `TRINITY_MCP_API_KEY` env var in container |
| **MCP URL** | Internal: `http://mcp-server:8080/mcp` |

**Agent .mcp.json** (auto-generated):
```json
{
  "mcpServers": {
    "trinity": {
      "type": "http",
      "url": "http://mcp-server:8080/mcp",
      "headers": { "Authorization": "Bearer ${TRINITY_MCP_API_KEY}" }
    }
  }
}
```

### 5. Agent-to-Agent Permissions

Fine-grained control over which agents can communicate with each other.

**Enforcement layer**: Agent-to-agent permissions are enforced at the **MCP server layer** (`src/mcp-server/src/tools/`), not the backend REST API. The backend resolves agent-scoped keys to their owner user and applies standard ownership/sharing checks. The `current_user.agent_name` field is set for agent-scoped keys but is only used by notifications and event subscriptions, not for permission gating on chat/list.

| MCP Tool | Enforcement |
|----------|-------------|
| **`list_agents`** | Returns only permitted agents + self |
| **`chat_with_agent`** | Blocks calls to non-permitted targets |

**Permission Rules** (MCP layer):

| Source | Target | Access |
|--------|--------|--------|
| Agent (any) | Self | ✅ Always allowed |
| Agent (any) | Other agents | ❌ Denied unless explicitly granted |
| System agent | Any agent | ✅ Bypasses all checks |

**Restrictive default**: New agents start with zero permissions. All agent-to-agent access must be explicitly configured via the Permissions tab in Agent Detail UI (`PUT /api/agents/{name}/permissions`).

### 6. System Agent (Privileged Access)

The internal system agent (`trinity-system`) has special privileges.

| Property | Value |
|----------|-------|
| **Scope** | `system` (not `user` or `agent`) |
| **Permission Check** | Bypassed entirely |
| **Access** | Can call any agent, any tool |
| **Protection** | Cannot be deleted via API |
| **Purpose** | Platform operations (health, costs, fleet management) |

### 7. External Credentials (Agent → External Services)

Credentials for external APIs (OpenAI, HeyGen, etc.) injected into agent containers.

> **Refactored 2026-02-05 (CRED-002)**: Simplified from Redis-based assignment system to direct file injection with encrypted git storage.

| Storage | Files in agent workspace (`.env`, `.credentials.enc`) |
|---------|-------------------------------------------------------|
| **Injection** | Direct file write via inject endpoint |
| **Files** | `.env` (KEY=VALUE) + `.mcp.json` (edited directly) |
| **Backup** | `.credentials.enc` (AES-256-GCM encrypted, safe for git) |
| **Auto-import** | On startup if `.credentials.enc` exists without `.env` |

**Flow**:
```
User pastes credentials → Quick Inject → .env written to agent
        OR
User clicks Export → Read files → Encrypt → Write .credentials.enc
        OR
Agent starts → If .credentials.enc exists → Decrypt → Write files
```

**New Endpoints**:
- `POST /api/agents/{name}/credentials/inject` - Write files to agent
- `POST /api/agents/{name}/credentials/export` - Export to encrypted file
- `POST /api/agents/{name}/credentials/import` - Import from encrypted file

### MCP Scope Summary

| Scope | Description | MCP Enforcement | Backend Enforcement |
|-------|-------------|-----------------|---------------------|
| `user` | Human user via Claude Code client | Owner/admin/shared checks | Owner/admin/shared checks |
| `agent` | Regular agent calling other agents | Explicit permission list (`agent_permissions` table) | Resolves to owner user; ownership/sharing checks only |
| `system` | System agent only | **Bypasses all checks** | Resolves to owner user (system agent owner) |

**Note**: Agent-to-agent permission enforcement (`agent_permissions`) only occurs at the MCP layer. The backend treats agent-scoped keys as "act on behalf of the key's owner." In practice this is not a bypass risk because agents communicate via MCP, not direct REST calls.

---

## Container Security

- Non-root execution (`developer:1000`)
- `CAP_DROP: ALL` + `CAP_ADD: NET_BIND_SERVICE`
- `security_opt: no-new-privileges:true`
- tmpfs `/tmp` with `noexec,nosuid`
- Isolated network (`172.28.0.0/16`)
- No external UI port exposure

### Internal API Security (C-003)

Internal endpoints (`/api/internal/`) used by the scheduler and agent containers require shared-secret authentication via `X-Internal-Secret` header. Falls back to `SECRET_KEY` if `INTERNAL_API_SECRET` env var is not set.

### WebSocket Security (C-002)

The `/ws` endpoint requires JWT authentication. Token provided via `?token=` query parameter or as first message (`Bearer <token>`, 5s timeout). Unauthenticated connections are rejected. The `/ws/events` endpoint requires MCP API key authentication (unchanged).

**Reconnect replay (RELIABILITY-003, #306):** Both `/ws` and `/ws/events` accept an optional `?last-event-id=<stream_id>` query param. The value is regex-gated (`^\d+-\d+$`) by `validate_last_event_id()` in `services/event_bus.py` before reaching `XRANGE`; malformed input is ignored (no catchup). Catchup is capped at `REPLAY_GAP_LIMIT=5000` entries — a larger gap returns `{"type": "resync_required", "reason": "gap_too_large"}` instead of an unbounded `XRANGE`. Authorization (`accessible_agents` for `/ws/events`) is re-applied on replay, not just on live fan-out.

### Frontend XSS Protection (H-005)

All markdown rendering in Vue components uses `DOMPurify` sanitization via `utils/markdown.js`. No direct `v-html` with unsanitized content.

---

## External Integrations

### User Authentication
Email-based authentication with verification codes (primary) and admin password login (secondary).
Auth0 OAuth was removed in 2026-01-01 - see [email-authentication.md](feature-flows/email-authentication.md).

### OAuth Providers (Agent Credentials)
- Google (Workspace access)
- Slack (Bot/User tokens)
- GitHub (PAT for repos)
- Notion (API access)

### MCP Servers (in agents)
- google-workspace
- slack
- notion
- github
- n8n-mcp (535 nodes)

---

## Development Environment

### URLs (Local & Production)

Local and production use the same ports for consistency:

| Service | Local | Production |
|---------|-------|------------|
| Frontend | http://localhost | https://your-domain.com |
| Backend API | http://localhost:8000/docs | https://your-domain.com/api/ |
| MCP Server | http://localhost:8080/mcp | http://your-server:8080/mcp |
| Vector (logs) | http://localhost:8686/health | http://your-server:8686/health |
| Redis | localhost:6379 (internal) | internal only |

### Port Allocation
| Port | Service |
|------|---------|
| 80 | Frontend (nginx/Vite) |
| 8000 | Backend (FastAPI) |
| 8080 | MCP Server |
| 2222-2262 | Agent SSH |

---

## Data Persistence

### Bind Mount (survives `docker-compose down -v`)
- `~/trinity-data/` → `/data` in container
- Contains: `trinity.db` (SQLite)

### Docker Volumes
- `redis-data` - Redis AOF persistence
- `agent-configs` - Agent configurations
- `audit-data` - Audit database
- `audit-logs` - Audit log files

