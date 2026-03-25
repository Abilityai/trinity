# Trinity Deep Agent Orchestration Platform - Architecture

> **Purpose**: Documents the CURRENT system design. Update only when implementing changes.

## System Overview

**Trinity** is a **Deep Agent Orchestration Platform** — sovereign infrastructure for deploying, orchestrating, and governing autonomous AI systems ("System 2" AI).

### The Four Pillars of Deep Agency

| Pillar | Trinity Implementation |
|--------|----------------------|
| **I. Explicit Planning** | Scheduling, activity timeline, runtime platform prompt injection (Task DAGs removed 2025-12-23) |
| **II. Hierarchical Delegation** | Agent-to-Agent via MCP, access control, collaboration dashboard |
| **III. Persistent Memory** | SQLite chat persistence, virtual filesystems, file browser |
| **IV. Extreme Context Engineering** | Template system with CLAUDE.md, credential injection, Trinity commands |

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
| Let's Encrypt | SSL/TLS certificates |
| GCP | Cloud hosting |

---

## Component Details

### Backend (`src/backend/`)

**Modular Architecture (refactored 2025-11-29):**

| Module | Purpose |
|--------|---------|
| `main.py` | FastAPI app initialization, WebSocket manager, router mounting (182 lines) |
| `config.py` | Centralized configuration constants |
| `models.py` | All Pydantic request/response models |
| `dependencies.py` | FastAPI dependencies (auth, token validation, agent access control) |
| `database.py` | SQLite persistence (users, agent ownership, MCP API keys) |
| ~~`credentials.py`~~ | **REMOVED (2026-02-05)** - CRED-002 replaced with `routers/credentials.py` file injection system |

**Routers (`routers/`)** — 45 router modules:

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
- `slack.py` - Slack integration (OAuth, events, DM handling) (SLACK-001)

*Subscriptions & Skills:*
- `subscriptions.py` - Subscription management (SUB-002)
- `skills.py` - Skill CRUD and assignment
- `settings.py` - Platform admin settings

*Process Engine:*
- `processes.py` - Process definition CRUD, execution control
- `process_templates.py` - Process template listing and retrieval
- `approvals.py` - Human approval inbox
- `triggers.py` - Process triggers

*Content & Files:*
- `image_generation.py` - Image generation REST endpoints (IMG-001)
- `avatar.py` - Agent avatar generation and serving (AVATAR-001)
- `docs.py` - Documentation endpoints

*System:*
- `system_agent.py` - System agent management

**Services (`services/`)** — 22 service modules + Process Engine:

*Core:*
- `docker_service.py` - Docker container management
- `docker_utils.py` - Docker utility helpers
- `template_service.py` - GitHub template cloning and processing
- `agent_client.py` - HTTP client for agent container communication (chat, session, injection)
- `settings_service.py` - Centralized settings retrieval (API keys, ops config)

*Execution & Scheduling:*
- `task_execution_service.py` - Unified task execution lifecycle (slot mgmt, activity tracking, sanitization) (EXEC-024)
- `slot_service.py` - Parallel execution slot management with dynamic TTL (CAPACITY-001)
- `execution_queue.py` - Redis-based execution queueing
- `scheduler_service.py` - APScheduler-based scheduling service
- `cleanup_service.py` - Background recovery of stale executions, activities, and slots (CLEANUP-001)

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
- `git_service.py` - Git sync operations for GitHub-native agents
- `github_service.py` - GitHub API client (repo creation, validation, org detection)

*Integrations:*
- `slack_service.py` - Slack API client (OAuth, messaging, verification) (SLACK-001)
- `nevermined_payment_service.py` - x402 payment verification and settlement (NVM-001)

*Content & Media:*
- `image_generation_service.py` - Platform image generation via Gemini (prompt refinement + image gen) (IMG-001)
- `image_generation_prompts.py` - Best practices prompts for image generation use cases (IMG-001)

*Skills & System:*
- `skill_service.py` - Skill CRUD and injection
- `system_agent_service.py` - System agent lifecycle management
- `system_service.py` - System manifest operations
- `log_archive_service.py` - Log archival
- `archive_storage.py` - Archive storage backend

*Process Engine:*
- `process_engine/` - BPMN-inspired workflow orchestration (see Process Engine section below)

**Logging (`logging_config.py`):**
- Structured JSON logging for production
- Captured by Vector via Docker stdout/stderr

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

**62 Tools** across 13 tool modules (`src/tools/`):

| Module | Tools | Description |
|--------|-------|-------------|
| `agents.ts` (17) | `list_agents`, `get_agent`, `get_agent_info`, `create_agent`, `rename_agent`, `delete_agent`, `start_agent`, `stop_agent`, `list_templates`, `get_credential_status`, `inject_credentials`, `export_credentials`, `import_credentials`, `get_credential_encryption_key`, `get_agent_ssh_access`, `deploy_local_agent`, `initialize_github_sync` | Agent lifecycle, credentials, SSH, local deploy, GitHub sync |
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
| `docs.ts` (1) | `get_agent_requirements` | Agent documentation |

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
├── content/               # Generated assets (gitignored)
└── [template files...]    # Any other files from template
```

### Process Engine (`src/backend/services/process_engine/`)

**NEW: 2026-01-16** — BPMN-inspired workflow orchestration for multi-agent processes.

**Architecture:** Domain-Driven Design (DDD) with clean layer separation.

```
services/process_engine/
├── domain/              # Core domain model
│   ├── aggregates.py    # ProcessDefinition, ProcessExecution
│   ├── entities.py      # StepDefinition, StepRoles, StepExecution
│   ├── value_objects.py # ProcessId, ExecutionId, Version, StepId
│   ├── enums.py         # ExecutionStatus, StepType, AgentRole
│   ├── events.py        # Domain events (ProcessStarted, StepCompleted, etc.)
│   └── step_configs.py  # Type-specific step configurations
├── engine/              # Execution engine
│   ├── execution_engine.py  # Main orchestrator
│   ├── step_handler.py      # Base handler interface
│   ├── dependency_resolver.py # Parallel execution planning
│   └── handlers/            # Step type handlers
│       ├── agent_task.py    # AI agent execution
│       ├── human_approval.py # Human-in-the-loop
│       ├── gateway.py       # Conditional branching
│       ├── timer.py         # Delay/scheduling
│       ├── notification.py  # Send notifications
│       └── sub_process.py   # Nested process calls
├── repositories/        # Persistence layer
│   ├── process_definition_repository.py
│   └── process_execution_repository.py
├── services/            # Application services
│   ├── validator.py     # YAML + semantic validation
│   ├── analytics.py     # Metrics and trends
│   ├── alerts.py        # Cost threshold alerts
│   ├── informed_notifier.py # EMI pattern notifications
│   └── templates.py     # Process template management
├── events/              # Event infrastructure
│   └── websocket_publisher.py # Real-time UI updates
└── schemas/             # JSON Schema for validation
    └── process-definition.schema.json
```

**Step Types:**

| Type | Handler | Description |
|------|---------|-------------|
| `agent_task` | `AgentTaskHandler` | Execute task via AI agent |
| `human_approval` | `HumanApprovalHandler` | Pause for human decision |
| `gateway` | `GatewayHandler` | Conditional branching |
| `timer` | `TimerHandler` | Delay execution |
| `notification` | `NotificationHandler` | Send notifications |
| `sub_process` | `SubProcessHandler` | Call another process |

**Execution State Machine:**

```
PENDING → RUNNING → COMPLETED
                 ↘ FAILED
                 ↘ CANCELLED
          ↗ PAUSED (approval) → RUNNING
```

**EMI Role Pattern:**
- **Executor**: Agent that performs the work (required)
- **Monitor**: Agents that can intervene (optional)
- **Informed**: Agents notified of events via NDJSON files (optional)

**Feature Flows:** `docs/memory/feature-flows/process-engine/`

### Background Services

Services that run continuously in the backend process:

| Service | Module | Description |
|---------|--------|-------------|
| **Cleanup Service** | `cleanup_service.py` | Recovers stale executions stuck in 'running' status, expires abandoned activities, releases orphaned slots. Runs on configurable interval (default 120 min). (CLEANUP-001) |
| **Operator Queue Sync** | `operator_queue_service.py` | Polls running agents every 5s, reads `~/.trinity/operator-queue.json`, syncs to DB, writes responses back. (OPS-001) |
| **Monitoring Service** | `monitoring_service.py` | Fleet-wide health checks on configurable interval. (MON-001) |
| **Scheduler Service** | `scheduler_service.py` | APScheduler-based cron job execution. Async fire-and-forget with DB polling for status. |

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
| POST | `/api/agents/{name}/ssh-access` | Generate ephemeral SSH credentials (NEW: 2026-01-02) |
| GET | `/api/agents/{name}/read-only` | Get read-only mode status and config (NEW: 2026-02-17) |
| PUT | `/api/agents/{name}/read-only` | Enable/disable read-only mode (blocks source file writes) |
| GET | `/api/agents/{name}/timeout` | Get execution timeout setting (NEW: 2026-03-12) |
| PUT | `/api/agents/{name}/timeout` | Set execution timeout (60-7200s, default 900s = 15min) |

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

### Sharing (3 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agents/{name}/share` | Share agent |
| DELETE | `/api/agents/{name}/share/{email}` | Remove share |
| GET | `/api/agents/{name}/shares` | List shares |

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

### Auth & MCP (12 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/auth/mode` | Get auth mode config - unauthenticated |
| POST | `/api/token` | Admin login (username/password) |
| POST | `/api/auth/email/request` | Request email verification code |
| POST | `/api/auth/email/verify` | Verify email code and login |
| GET | `/api/auth/validate` | Validate JWT (for nginx auth_request) |
| GET | `/api/users/me` | Current user |
| GET | `/api/mcp/info` | MCP server info |
| POST | `/api/mcp/keys` | Create API key |
| GET | `/api/mcp/keys` | List API keys |
| DELETE | `/api/mcp/keys/{id}` | Delete API key |
| GET | `/oauth/{provider}/authorize` | Start OAuth |
| GET | `/oauth/{provider}/callback` | OAuth callback |
| GET | `/api/health` | Health check |

### Process Engine (NEW: 2026-01-16)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/processes` | List all process definitions |
| POST | `/api/processes` | Create process from YAML |
| GET | `/api/processes/{id}` | Get process definition |
| PUT | `/api/processes/{id}` | Update process definition |
| DELETE | `/api/processes/{id}` | Delete process |
| POST | `/api/processes/{id}/publish` | Publish process (make executable) |
| POST | `/api/processes/{id}/execute` | Start process execution |
| GET | `/api/executions` | List all executions |
| GET | `/api/executions/{id}` | Get execution details |
| POST | `/api/executions/{id}/cancel` | Cancel running execution |
| GET | `/api/approvals` | List pending approvals |
| GET | `/api/approvals/{id}` | Get approval details |
| POST | `/api/approvals/{id}/decide` | Submit approval decision |
| GET | `/api/process-templates` | List process templates |
| GET | `/api/process-templates/{id}` | Get template with definition |
| POST | `/api/process-templates` | Create user template |
| GET | `/api/processes/{id}/analytics` | Get process metrics |
| GET | `/api/processes/{id}/trends` | Get execution trends |

**WebSocket Events (Process Engine):**
- `process_started` - Execution began
- `step_started` - Step execution began
- `step_completed` - Step finished successfully
- `step_failed` - Step failed
- `process_completed` - Execution finished
- `process_failed` - Execution failed
- `approval_required` - Human approval needed

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

---

## Database Schema

### SQLite (`/data/trinity.db`)

**users:**
```sql
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT,
    picture TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login TIMESTAMP
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
    FOREIGN KEY (owner_id) REFERENCES users(id)
);
```

**agent_sharing:**
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
    FOREIGN KEY (schedule_id) REFERENCES agent_schedules(id)
);
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

**process_definitions:** (Phase 14 - Process Engine, NEW: 2026-01-16)
```sql
CREATE TABLE process_definitions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    yaml_content TEXT NOT NULL,
    version INTEGER DEFAULT 1,
    status TEXT DEFAULT 'draft',       -- draft, published, archived
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(name, version),
    FOREIGN KEY (created_by) REFERENCES users(id)
);
CREATE INDEX idx_process_definitions_name ON process_definitions(name);
CREATE INDEX idx_process_definitions_status ON process_definitions(status);
```

**process_executions:** (Phase 14 - Process Engine, NEW: 2026-01-16)
```sql
CREATE TABLE process_executions (
    id TEXT PRIMARY KEY,
    process_id TEXT NOT NULL,
    process_name TEXT NOT NULL,
    process_version INTEGER NOT NULL,
    status TEXT NOT NULL,              -- pending, running, completed, failed, cancelled, paused
    triggered_by TEXT NOT NULL,        -- manual, schedule, sub_process, api
    input_data TEXT,                   -- JSON
    output_data TEXT,                  -- JSON
    total_cost REAL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    parent_execution_id TEXT,          -- For sub-processes
    parent_step_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (process_id) REFERENCES process_definitions(id),
    FOREIGN KEY (parent_execution_id) REFERENCES process_executions(id)
);
CREATE INDEX idx_process_executions_process ON process_executions(process_id);
CREATE INDEX idx_process_executions_status ON process_executions(status);
CREATE INDEX idx_process_executions_parent ON process_executions(parent_execution_id);
```

**process_step_executions:** (Phase 14 - Process Engine, NEW: 2026-01-16)
```sql
CREATE TABLE process_step_executions (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    step_type TEXT NOT NULL,           -- agent_task, human_approval, gateway, timer, notification, sub_process
    status TEXT NOT NULL,              -- pending, running, completed, failed, skipped, waiting
    agent_name TEXT,
    input_data TEXT,                   -- JSON
    output_data TEXT,                  -- JSON
    cost REAL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    retry_count INTEGER DEFAULT 0,
    error TEXT,
    FOREIGN KEY (execution_id) REFERENCES process_executions(id)
);
CREATE INDEX idx_step_executions_execution ON process_step_executions(execution_id);
CREATE INDEX idx_step_executions_status ON process_step_executions(status);
```

**process_approvals:** (Phase 14 - Process Engine, NEW: 2026-01-16)
```sql
CREATE TABLE process_approvals (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL,              -- pending, approved, rejected, timed_out
    title TEXT NOT NULL,
    description TEXT,
    approvers TEXT,                    -- JSON array
    timeout_at TEXT,
    decided_by TEXT,
    decision TEXT,
    comment TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (execution_id) REFERENCES process_executions(id)
);
CREATE INDEX idx_process_approvals_status ON process_approvals(status);
CREATE INDEX idx_process_approvals_execution ON process_approvals(execution_id);
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

**Process Engine Features:**
- YAML-based process definitions with JSON Schema validation
- Six step types: agent_task, human_approval, gateway, timer, notification, sub_process
- Parallel execution based on dependency graph
- EMI role pattern (Executor/Monitor/Informed) per step
- Real-time monitoring via WebSocket events
- Cost tracking and threshold alerts
- Sub-process support with parent-child linking

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

| Scope | Enforcement |
|-------|-------------|
| **`list_agents`** | Returns only permitted agents + self |
| **`chat_with_agent`** | Blocks calls to non-permitted targets |

**Permission Rules**:

| Source | Target | Access |
|--------|--------|--------|
| Agent (any) | Self | ✅ Always allowed |
| Agent (same owner) | Same owner agents | ✅ Default on creation |
| Agent (different owner) | Shared agent | ✅ If `is_shared=true` |
| Agent (different owner) | Private agent | ❌ Denied |
| System agent | Any agent | ✅ Bypasses all checks |

**Configuration**: Permissions tab in Agent Detail UI (`PUT /api/agents/{name}/permissions`)

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

| Scope | Description | Permission Enforcement |
|-------|-------------|----------------------|
| `user` | Human user via Claude Code client | Owner/admin/shared checks |
| `agent` | Regular agent calling other agents | Explicit permission list |
| `system` | System agent only | **Bypasses all checks** |

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

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `src/backend/main.py` | Main FastAPI app |
| `src/backend/database.py` | SQLite persistence |
| `src/backend/routers/credentials.py` | Credential injection (CRED-002) |
| `src/frontend/src/views/AgentDetail.vue` | Agent detail page |
| `src/frontend/src/stores/agents.js` | Agent state |
| `src/frontend/src/stores/auth.js` | Auth state |
| `docker/base-image/agent-server.py` | Agent internal server |
| `docker/base-image/Dockerfile` | Agent base image |
| `docker-compose.yml` | Local orchestration |
| `docker-compose.prod.yml` | Production config |

### Process Engine Files (NEW: 2026-01-16)

| File | Purpose |
|------|---------|
| `src/backend/services/process_engine/` | Process Engine service root |
| `src/backend/services/process_engine/engine/execution_engine.py` | Core orchestration engine |
| `src/backend/services/process_engine/domain/aggregates.py` | ProcessDefinition, ProcessExecution |
| `src/backend/services/process_engine/domain/entities.py` | StepDefinition, StepRoles |
| `src/backend/services/process_engine/services/validator.py` | YAML + semantic validation |
| `src/backend/services/process_engine/services/analytics.py` | Metrics and trends |
| `src/backend/routers/processes.py` | Process API endpoints |
| `src/frontend/src/views/ProcessList.vue` | Process list page |
| `src/frontend/src/views/ProcessEditor.vue` | YAML editor with preview |
| `src/frontend/src/views/ProcessExecutionDetail.vue` | Execution monitoring |
| `src/frontend/src/views/Approvals.vue` | Human approval inbox |
| `config/process-templates/` | Bundled process templates |
| `docs/PROCESS_DRIVEN_PLATFORM/` | Design documents |
| `docs/memory/feature-flows/process-engine/` | Feature flow documentation |
