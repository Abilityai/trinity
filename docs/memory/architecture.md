# Trinity Deep Agent Orchestration Platform - Architecture

> **Purpose**: Documents the CURRENT system design. Update only when implementing changes.

## System Overview

**Trinity** is a **Deep Agent Orchestration Platform** — sovereign infrastructure for deploying, orchestrating, and governing autonomous AI systems ("System 2" AI).

### The Four Pillars of Deep Agency

| Pillar | Trinity Implementation |
|--------|----------------------|
| **I. Explicit Planning** | Task DAG engine, plan persistence, Trinity meta-prompt injection, scheduling, activity timeline |
| **II. Hierarchical Delegation** | Agent-to-Agent via MCP, access control, collaboration dashboard |
| **III. Persistent Memory** | SQLite chat persistence, virtual filesystems, file browser |
| **IV. Extreme Context Engineering** | Template system with CLAUDE.md, credential injection, Trinity commands |

Each agent runs as an isolated Docker container with standardized interfaces for credentials, tools, and MCP server integrations.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Trinity Agent Platform                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │   Frontend   │  │   Backend    │  │  MCP Server  │  │ Audit Logger │    │
│  │   (Vue.js)   │  │  (FastAPI)   │  │  (FastMCP)   │  │  (FastAPI)   │    │
│  │   :3000      │  │   :8000      │  │   :8080      │  │   :8001      │    │
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
| Auth0 Vue | 2.x | Authentication |

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
| `dependencies.py` | FastAPI dependencies (auth, token validation) |
| `database.py` | SQLite persistence (users, agent ownership, MCP API keys) |
| `credentials.py` | Redis-backed credential manager with OAuth2 flows |

**Routers (`routers/`):**
- `auth.py` - Authentication endpoints (login, Auth0 exchange, token validation)
- `agents.py` - Agent CRUD, start/stop, logs, stats
- `credentials.py` - Credential management (env files, MCP configs)
- `templates.py` - Template listing and GitHub repo fetching
- `sharing.py` - Agent sharing between users
- `mcp_keys.py` - MCP API key management
- `chat.py` - Agent chat/activity monitoring
- `schedules.py` - Agent scheduling CRUD and control
- `git.py` - Git sync endpoints (status, sync, log, pull)

**Services (`services/`):**
- `audit_service.py` - Async audit event logging
- `docker_service.py` - Docker container management
- `template_service.py` - GitHub template cloning and processing
- `scheduler_service.py` - APScheduler-based scheduling service
- `git_service.py` - Git sync operations for GitHub-native agents

**Utilities (`utils/`):**
- `helpers.py` - Shared helper functions

**Docker Integration:**
- Uses `docker-py` SDK
- Containers labeled with `trinity.*` prefix
- Docker is the source of truth (no in-memory registry)

### Frontend (`src/frontend/`)

**Key Directories:**
- `src/views/` - Page components (Dashboard, Agents, Credentials, Templates, ApiKeys, AgentCollaboration)
- `src/stores/` - Pinia state (agents.js, auth.js, collaborations.js)
- `src/components/` - Reusable UI components (NavBar, AgentNode)
- `src/utils/` - WebSocket client, helpers

**State Management:**
- `stores/agents.js` - Agent CRUD, chat, activity
- `stores/auth.js` - Auth0 + JWT authentication
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

**Port:** 8080 (internal), 8007 (production)

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

**12 Tools:**
1. `list_agents` - List all agents
2. `get_agent` - Get agent details
3. `create_agent` - Create new agent
4. `delete_agent` - Delete agent
5. `start_agent` - Start agent
6. `stop_agent` - Stop agent
7. `list_templates` - List templates
8. `chat_with_agent` - Send message to agent (enforces sharing rules)
9. `get_chat_history` - Get conversation history
10. `get_agent_logs` - Get container logs
11. `reload_credentials` - Hot-reload credentials
12. `get_credential_status` - Check credential files

### Audit Logger (`src/audit-logger/`)

**File:** `audit_logger.py` (174 lines)

**Event Types:**
- `authentication` - Login/logout events
- `agent_management` - Create/delete/start/stop
- `agent_access` - Chat, logs access
- `credential_access` - Credential operations

**Storage:** SQLite database with query API

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
/home/developer/
├── workspace/           # Agent workspace (from template)
├── .env                 # Credentials (KEY=VALUE)
├── .mcp.json           # Generated MCP config
├── .mcp.json.template  # Template with ${VAR} placeholders
└── .claude/            # Claude Code config
```

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

### Agents (26 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents` | List all agents |
| GET | `/api/agents/context-stats` | Get context & activity state for all agents (NEW: 2025-12-02) |
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

**Note**: Route ordering is critical. `/context-stats` must be defined BEFORE `/{name}` catch-all route to avoid 404 errors.

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

### Credentials (8 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/credentials` | List credentials |
| POST | `/api/credentials` | Add credential |
| DELETE | `/api/credentials/{id}` | Delete credential |
| POST | `/api/credentials/bulk` | Bulk import |
| GET | `/api/agents/{name}/credentials` | Agent requirements |
| POST | `/api/agents/{name}/credentials/reload` | Reload from store |
| POST | `/api/agents/{name}/credentials/hot-reload` | Hot-reload (paste) |
| GET | `/api/agents/{name}/credentials/status` | Check files |

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

### Plans - Task DAG System (8 endpoints) - NEW: 2025-12-05
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents/plans/aggregate` | Cross-agent plan summary (must come before `/{name}`) |
| GET | `/api/agents/{name}/plans` | List agent's plans |
| POST | `/api/agents/{name}/plans` | Create new plan |
| GET | `/api/agents/{name}/plans/summary` | Aggregate plan stats for agent |
| GET | `/api/agents/{name}/plans/{plan_id}` | Get specific plan with tasks |
| PUT | `/api/agents/{name}/plans/{plan_id}` | Update plan metadata |
| DELETE | `/api/agents/{name}/plans/{plan_id}` | Delete plan |
| PUT | `/api/agents/{name}/plans/{plan_id}/tasks/{task_id}` | Update task status |

**Note**: `/plans/aggregate` route must be defined BEFORE `/{name}` routes to avoid FastAPI matching "plans" as an agent name.

**Implementation**: Backend proxies to agent container's `/api/plans` endpoints. Plans stored as YAML files in `plans/active/` and `plans/archive/`.

### Auth & MCP (12 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/auth/mode` | Get auth mode config (dev/prod) - unauthenticated |
| POST | `/api/token` | Dev mode login (gated by DEV_MODE_ENABLED) |
| POST | `/api/auth/exchange` | Exchange Auth0 token for JWT |
| GET | `/api/auth/validate` | Validate JWT (for nginx auth_request) |
| GET | `/api/users/me` | Current user |
| GET | `/api/mcp/info` | MCP server info |
| POST | `/api/mcp/keys` | Create API key |
| GET | `/api/mcp/keys` | List API keys |
| DELETE | `/api/mcp/keys/{id}` | Delete API key |
| GET | `/oauth/{provider}/authorize` | Start OAuth |
| GET | `/oauth/{provider}/callback` | OAuth callback |
| GET | `/api/health` | Health check |

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

### Redis

**Credential Storage:**
```
credentials:{id} → {
    "id": "uuid",
    "name": "VARIABLE_NAME",
    "value": "secret_value",
    "service": "google",
    "owner_id": "user_id",
    "type": "api_key|oauth_token",
    "created_at": "timestamp"
}
```

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
│        │ (1) User Auth: JWT via Auth0/OAuth or Dev credentials                    │
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
| **Dev** (`DEV_MODE_ENABLED=true`) | Username/password → `POST /api/token` | JWT with `mode: "dev"` |
| **Prod** (`DEV_MODE_ENABLED=false`) | Google OAuth via Auth0 → `POST /api/auth/exchange` | JWT with `mode: "prod"` |

- Runtime mode detection via `GET /api/auth/mode` (no rebuild to switch)
- Domain restriction (e.g., `@ability.ai`) enforced server-side
- Token mode claim prevents dev tokens in prod mode

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

| Storage | Redis with separate metadata and secrets |
|---------|------------------------------------------|
| **Injection** | At agent creation + hot-reload at runtime |
| **Files** | `.env` (KEY=VALUE) + `.mcp.json` (substituted) |
| **Template** | `.mcp.json.template` with `${VAR_NAME}` placeholders |
| **Hot-reload** | `POST /api/agents/{name}/credentials/hot-reload` |

**Flow**:
```
User uploads credentials → Redis storage → Agent creation OR hot-reload
                                              ↓
                                    .env written to container
                                              ↓
                                    .mcp.json generated from template
                                              ↓
                                    MCP servers pick up credentials
```

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

---

## External Integrations

### Auth0 (User Authentication)
| Setting | Environment Variable |
|---------|---------------------|
| Domain | `AUTH0_DOMAIN` |
| Client ID | `VITE_AUTH0_CLIENT_ID` |
| Allowed Domain | `AUTH0_ALLOWED_DOMAIN` (optional) |

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

### Local URLs
| Service | URL |
|---------|-----|
| Frontend | http://localhost:3000 |
| Backend API | http://localhost:8000/docs |
| MCP Server | http://localhost:8080/mcp |
| Audit Logger | http://localhost:8001/docs |
| Redis | localhost:6379 |

### Production URLs
| Service | URL |
|---------|-----|
| Frontend | `https://your-domain.com` |
| Backend API | `https://your-domain.com/api/` |
| MCP Server | `http://your-server:8007/mcp` |
| Audit Logger | `http://your-server:8006/docs` |

### Landing Page (Optional)
Landing page is a separate project that can be deployed on Vercel or any static hosting.

### Port Allocation (Production)
| Port | Service |
|------|---------|
| 3005 | Frontend (nginx) |
| 8005 | Backend (FastAPI) |
| 8006 | Audit Logger |
| 8007 | MCP Server |
| 2224-2242 | Agent SSH |

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
| `src/backend/credentials.py` | Credential manager |
| `src/frontend/src/views/AgentDetail.vue` | Agent detail page |
| `src/frontend/src/stores/agents.js` | Agent state |
| `src/frontend/src/stores/auth.js` | Auth state |
| `docker/base-image/agent-server.py` | Agent internal server |
| `docker/base-image/Dockerfile` | Agent base image |
| `docker-compose.yml` | Local orchestration |
| `docker-compose.prod.yml` | Production config |
