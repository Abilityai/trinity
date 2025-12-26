# Trinity Deep Agent Orchestration Platform - Requirements

> **SINGLE SOURCE OF TRUTH** - All development must trace back to this document.
> Update this file BEFORE implementing any new feature.

## Vision: The Four Pillars of Deep Agency

Trinity implements infrastructure for "System 2" AI — Deep Agents that plan, reason, and execute autonomously.

| Pillar | Description | Implementation Status |
|--------|-------------|----------------------|
| **I. Explicit Planning** | Task DAGs persisting outside context window | ❌ REMOVED (2025-12-23) - Individual agent planning deferred to orchestrator-level |
| **II. Hierarchical Delegation** | Orchestrator-Worker with context quarantine | ✅ Agent-to-Agent via MCP |
| **III. Persistent Memory** | Virtual filesystems, memory folding | ✅ Chat persistence, file browser |
| **IV. Extreme Context Engineering** | High-Order Prompts defining reasoning | ✅ Templates with CLAUDE.md |

## Status Labels
- ⏳ Not Started
- 🚧 In Progress
- ✅ Implemented

---

## Functional Requirements

### 1. Core Agent Management

#### 1.1 Agent Creation
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Create agents from templates (GitHub or local) or from scratch via UI and API
- **Acceptance Criteria**:
  - [x] Create agents via Web UI
  - [x] Create agents via REST API
  - [x] Support GitHub templates (`github:Org/repo`)
  - [x] Support local templates
  - [x] Template metadata extraction (template.yaml)
  - [x] Credential schema auto-detection from `.mcp.json.template`

#### 1.2 Agent Start/Stop
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Start and stop agent containers with visual feedback
- **Acceptance Criteria**:
  - [x] Start agent container
  - [x] Stop agent container
  - [x] Loading spinners during operations
  - [x] Toast notifications for success/failure
  - [x] WebSocket status broadcasts

#### 1.3 Agent Deletion
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Delete agents and cleanup resources
- **Acceptance Criteria**:
  - [x] Delete agent via UI
  - [x] Delete agent via API
  - [x] Container cleanup
  - [x] Network cleanup
  - [x] Cascade delete sharing records

#### 1.4 Agent Logs Viewing
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: View container logs for debugging
- **Acceptance Criteria**:
  - [x] Logs tab in agent detail page
  - [x] Fixed-height scrollable container
  - [x] Auto-refresh every 10 seconds (optional)
  - [x] Smart auto-scroll (stays at bottom unless user scrolls up)

#### 1.5 Agent Live Telemetry
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: Real-time container metrics in agent header
- **Acceptance Criteria**:
  - [x] CPU usage percentage with color-coded bar
  - [x] Memory usage (e.g., "847 MB / 4 GB")
  - [x] Network I/O (RX/TX bytes)
  - [x] Uptime display (e.g., "2h 15m")
  - [x] Auto-refresh every 5 seconds

---

### 2. Authentication & Authorization

#### 2.1 Auth0 + Google OAuth
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: SSO via Auth0 with Google OAuth provider
- **Acceptance Criteria**:
  - [x] Auth0 integration
  - [x] Google OAuth single sign-on
  - [x] JWT token management
  - [x] User profile display with avatar

#### 2.2 Domain Restriction
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Restrict access to @ability.ai domain
- **Acceptance Criteria**:
  - [x] Only @ability.ai emails allowed
  - [x] Non-ability.ai emails rejected with "Access Denied"

#### 2.3 Development Mode Bypass
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: Skip Auth0 for local development
- **Acceptance Criteria**:
  - [x] `VITE_DEV_MODE=true` bypasses Auth0
  - [x] Uses backend admin credentials

#### 2.4 Session Persistence
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: User profile survives page refresh
- **Acceptance Criteria**:
  - [x] Token stored in localStorage
  - [x] Profile restored on page load

#### 2.5 Agent Sharing
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Share agents with team members
- **Acceptance Criteria**:
  - [x] Share via email address
  - [x] Access levels: Owner, Shared, Admin
  - [x] Sharing tab in agent detail (owners only)
  - [x] "Shared by X" badge on shared agents
  - [x] Owner: full control
  - [x] Shared: can use (chat, start/stop) but cannot delete/share
  - [x] Admin: full control over all agents

---

### 3. Credential Management

#### 3.1 Manual Credential Entry
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Add credentials via UI form
- **Acceptance Criteria**:
  - [x] Add credential with name, value, service
  - [x] List credentials
  - [x] Delete credentials

#### 3.2 OAuth2 Flows
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: OAuth2 authentication for external services
- **Acceptance Criteria**:
  - [x] Google OAuth2 flow
  - [x] Slack OAuth2 flow
  - [x] GitHub OAuth2 flow
  - [x] Notion OAuth2 flow

#### 3.2.1 OAuth Credential Normalization for MCP Compatibility
- **Status**: ✅ Implemented (2025-12-01)
- **Priority**: High
- **Description**: Normalize OAuth credentials with MCP-compatible naming conventions for seamless integration
- **Acceptance Criteria**:
  - [x] Include client_id and client_secret in OAuth token response
  - [x] Store both raw OAuth tokens and normalized MCP-compatible names in Redis
  - [x] Google Drive MCP integration with GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
  - [x] Normalize credentials for all OAuth providers (Google, Slack, GitHub, Notion)
  - [x] Backward compatible with existing credential extraction logic
  - [x] Template variable replacement works with normalized names
  - [x] Test template created (google-drive-test)

#### 3.3 Credential Hot-Reload
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Update credentials on running agents without restart
- **Acceptance Criteria**:
  - [x] Hot-reload via UI paste (KEY=VALUE format)
  - [x] Hot-reload from Redis store
  - [x] Writes `.env` and regenerates `.mcp.json`
  - [x] MCP tools: `reload_credentials`, `get_credential_status`

#### 3.4 Bulk Credential Import
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: Import multiple credentials at once
- **Acceptance Criteria**:
  - [x] Paste `.env`-style KEY=VALUE pairs
  - [x] Template selector for required credentials
  - [x] Auto-detect service name from variable names

#### 3.5 Credential Requirements Extraction
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: Show required credentials per agent
- **Acceptance Criteria**:
  - [x] Extract from `.mcp.json.template` (${VAR} patterns)
  - [x] Extract from `template.yaml`
  - [x] Show configured vs missing status
  - [x] Progress bar for completeness

---

### 4. Template System

#### 4.1 Local Templates
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Templates from `config/agent-templates/`
- **Acceptance Criteria**:
  - [x] Auto-discovery of local templates
  - [x] Metadata extraction
  - [x] Workspace initialization

#### 4.2 GitHub Templates
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Templates from GitHub repositories
- **Acceptance Criteria**:
  - [x] Clone via `github:Org/repo` format
  - [x] GitHub PAT authentication
  - [x] Private repo support

#### 4.3 Template Metadata Extraction
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: Read template.yaml for metadata
- **Acceptance Criteria**:
  - [x] Display name and description
  - [x] Resource requirements
  - [x] Credential schema

#### 4.4 Automatic Credential Schema Detection
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: Parse `.mcp.json.template` for requirements
- **Acceptance Criteria**:
  - [x] Extract ${VAR} placeholders
  - [x] Map to credential requirements
  - [x] Show in UI

---

### 5. Agent Chat

#### 5.1 Chat via Backend API
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Chat with agents through authenticated API
- **Acceptance Criteria**:
  - [x] `/api/agents/{name}/chat` endpoint
  - [x] Real Claude Code execution
  - [x] Stream-json output parsing

#### 5.2 Conversation History
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Persist chat history per agent
- **Acceptance Criteria**:
  - [x] History stored in container
  - [x] Retrieve via API
  - [x] Display in UI

#### 5.3 Context Window Tracking
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: Show token usage in chat header
- **Acceptance Criteria**:
  - [x] Tokens used vs max context (e.g., "45.5K / 200K")
  - [x] Color-coded progress bar
  - [x] Parse `modelUsage.contextWindow` from output

#### 5.4 Session Cost Tracking
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: Track cumulative cost across conversation
- **Acceptance Criteria**:
  - [x] Display session cost
  - [x] Cumulative across messages

#### 5.5 New Session Reset
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: Reset conversation context
- **Acceptance Criteria**:
  - [x] "New Session" button
  - [x] Clear context to zero
  - [x] Start fresh conversation

#### 5.6 Persistent Chat History
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Database persistence for all chat interactions with full audit trail
- **Acceptance Criteria**:
  - [x] Database tables: `chat_sessions` and `chat_messages`
  - [x] Automatic session creation on first message
  - [x] Track all user and assistant messages
  - [x] Persist cost, context usage, tool calls, execution time per message
  - [x] Survives container restarts and deletions
  - [x] API endpoint: `GET /api/agents/{name}/chat/history/persistent`
  - [x] API endpoint: `GET /api/agents/{name}/chat/sessions`
  - [x] API endpoint: `GET /api/agents/{name}/chat/sessions/{id}`
  - [x] API endpoint: `POST /api/agents/{name}/chat/sessions/{id}/close`
  - [x] User-based access control (users see own, admins/owners see all)
  - [x] Filter by user, status, time range
  - [x] Full observability: costs, tokens, tool usage per session

---

### 6. Activity Monitoring

#### 6.1 Unified Activity Panel
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Real-time visibility into agent execution
- **Acceptance Criteria**:
  - [x] Single unified component (UnifiedActivityPanel.vue)
  - [x] Real-time tool call capture
  - [x] Uses `--output-format stream-json --verbose`

#### 6.2 Real-time Tool Call Display
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Show tools as they execute
- **Acceptance Criteria**:
  - [x] Current tool with pulsing indicator
  - [x] Updates during execution
  - [x] ThreadPoolExecutor + subprocess streaming

#### 6.3 Tool Chips with Counts
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: Visual count of tool usage
- **Acceptance Criteria**:
  - [x] Counts per tool type
  - [x] Sorted by frequency
  - [x] e.g., "Read ×12", "Bash ×5"

#### 6.4 Expandable Timeline
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: List of all tool calls
- **Acceptance Criteria**:
  - [x] Timestamps
  - [x] Durations
  - [x] Expandable entries

#### 6.5 Drill-down Modal
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: Full tool call details on click
- **Acceptance Criteria**:
  - [x] Full input/output display
  - [x] Modal interface

---

### 7. MCP Server

#### 7.1 Trinity MCP Server
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Agent orchestration via Model Context Protocol
- **Acceptance Criteria**:
  - [x] FastMCP with Streamable HTTP transport
  - [x] Port 8080 internal, 8007 production
  - [x] 12 tools implemented

#### 7.2 MCP API Key Authentication
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Secure MCP access
- **Acceptance Criteria**:
  - [x] Per-user API keys
  - [x] Usage statistics tracking
  - [x] API key management UI at `/api-keys`
  - [x] SHA-256 hashed storage

#### 7.3 Per-User API Keys
- **Status**: ✅ Implemented
- **Priority**: Medium
- **Description**: Individual keys per user
- **Acceptance Criteria**:
  - [x] Generate unique keys
  - [x] Revoke keys
  - [x] Track usage per key

#### 7.4 Usage Statistics
- **Status**: ✅ Implemented
- **Priority**: Low
- **Description**: Track MCP server usage
- **Acceptance Criteria**:
  - [x] Call counts
  - [x] Per-tool statistics

---

### 8. Infrastructure

#### 8.1 Docker as Source of Truth
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: No in-memory agent registry
- **Acceptance Criteria**:
  - [x] Query Docker directly
  - [x] Container labels for metadata
  - [x] Survives backend restarts
  - [x] Works with multiple workers

#### 8.2 SQLite Data Persistence
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Relational data persistence
- **Acceptance Criteria**:
  - [x] Users table
  - [x] Agent ownership
  - [x] MCP API keys
  - [x] Bind mount (survives `docker-compose down -v`)

#### 8.3 Redis for Secrets/Cache
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Secret storage and caching
- **Acceptance Criteria**:
  - [x] Credential storage
  - [x] OAuth state
  - [x] AOF persistence

#### 8.4 Audit Logging
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Security event tracking
- **Acceptance Criteria**:
  - [x] Authentication events
  - [x] Agent management events
  - [x] Credential access events
  - [x] Query API with filtering

#### 8.5 Container Security
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Secure agent containers
- **Acceptance Criteria**:
  - [x] Non-root execution (developer:1000)
  - [x] CAP_DROP: ALL
  - [x] no-new-privileges
  - [x] tmpfs with noexec,nosuid
  - [x] Isolated bridge network

#### 8.6 GCP Production Deployment
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Production deployment on GCP
- **Acceptance Criteria**:
  - [x] your-vm-name VM @ your-server-ip
  - [x] https://your-domain.com
  - [x] SSL/TLS via Let's Encrypt
  - [x] nginx reverse proxy

---

### 9. In Progress Features

#### 9.1 Agent Scheduling & Autonomy
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Schedule automatic agent executions for autonomous operation
- **Acceptance Criteria**:
  - [x] Platform-managed scheduler (APScheduler with AsyncIO)
  - [x] Configurable schedules on agent detail page (Schedules tab)
  - [x] Cron-style scheduling support (5-field cron expressions)
  - [x] View scheduled executions list with status
  - [x] Execution logs per scheduled run
  - [x] Enable/disable scheduling per agent
  - [x] Manual trigger for scheduled tasks
  - [x] Timezone support (UTC, EST, PST, etc.)
  - [x] Schedule CRUD via REST API
  - [x] WebSocket broadcasts for execution events

#### 9.2 GitHub-Native Agents (Bidirectional Sync)
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Agents created from GitHub repos can sync state back to GitHub
- **Architecture**:
  - Branch per instance: `trinity/{agent-name}/{instance-id}` (not main)
  - Main branch = template source of truth (read-only)
  - Working branch = agent instance state (platform-controlled)
  - Platform controls all commits (agent has no say)
  - Force push on conflicts (platform always wins)
- **Acceptance Criteria**:
  - [x] Architecture planned (docs/GITHUB_NATIVE_AGENTS.md)
  - [x] GitHub PAT configured (reuse existing for clone/push)
  - [x] Clone on agent creation (existing template system)
  - [x] Create working branch on agent creation from GitHub template
  - [x] Store branch name and repo URL in database
  - [x] Database schema: `agent_git_config` table
  - [x] "Sync to GitHub" button in agent detail UI
  - [x] POST `/api/agents/{name}/git/sync` endpoint
  - [x] Git operations: stage configured paths, commit, force push
  - [x] Auto-generated commit messages with timestamp
  - [x] Track last commit SHA and push timestamp
  - [x] "Git" tab in agent detail page
  - [x] Show: repo, branch, last sync time, commit history
  - [x] Sync status indicator (synced/pending changes)

#### 9.3 Agent Info Display
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Display template metadata in agent detail UI for user understanding
- **Acceptance Criteria**:
  - [x] Expose template metadata via agent detail API (`/api/agents/{name}/info`)
  - [x] "Info" tab in agent detail page (first tab position)
  - [x] Display: display_name, description, version, author
  - [x] Show capabilities list with visual chips
  - [x] Show sub-agents list (if defined)
  - [x] Show slash commands list (if defined)
  - [x] Show supported platforms (if defined)
  - [x] Show resource allocation (CPU, memory)
  - [x] Graceful fallback for agents without templates

#### 9.4 Agent-to-Agent Collaboration
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Enable agents to communicate with each other via Trinity MCP
- **Architecture**:
  - Agent-scoped MCP API keys (scope: "agent" vs "user")
  - Trinity MCP auto-injected into agent .mcp.json on startup
  - Access control based on ownership and sharing rules
  - Audit logging for inter-agent communication
- **Acceptance Criteria**:
  - [x] Database schema: agent_name, scope fields in mcp_api_keys
  - [x] Agent-scoped MCP key generation on agent creation
  - [x] Auto-inject Trinity MCP into agent .mcp.json
  - [x] MCP validate endpoint returns agent context (agent_name, scope)
  - [x] Access control in chat_with_agent tool:
    - Same owner: allowed
    - Shared agent: allowed
    - Admin bypass: allowed
    - Otherwise: denied with reason
  - [x] Audit logging for collaboration events
  - [x] Delete agent MCP key on agent deletion
- **Flow**:
  1. Agent A created → auto-generate agent-scoped MCP key
  2. Agent A starts → Trinity MCP injected into .mcp.json
  3. Agent A calls `mcp__trinity__chat_with_agent(target="B")`
  4. MCP server validates key, extracts agent_name/scope
  5. Access control checks ownership/sharing rules
  6. If allowed: proxy chat to Agent B
  7. Audit log: agent_collaboration event

#### 9.5 File Browser for Agent Workspaces
- **Status**: ✅ Implemented
- **Priority**: High
- **Description**: Browse and download files from agent workspaces via web UI
- **Acceptance Criteria**:
  - [x] Agent server endpoints: `GET /api/files` (list), `GET /api/files/download`
  - [x] Backend proxied endpoints: `GET /api/agents/{name}/files`, `/files/download`
  - [x] Security: workspace-only access (`/home/developer/workspace`)
  - [x] Security: 100MB file size limit
  - [x] Security: skip hidden files (.env, .git, etc.)
  - [x] "Files" tab in agent detail page
  - [x] Real-time search/filter by filename or path
  - [x] File metadata display: name, path, size, modified date
  - [x] Download button per file (browser download)
  - [x] Refresh button to reload file list
  - [x] Auto-load files when tab is activated
  - [x] Audit logging: `file_access` event type with `file_list`/`file_download` actions
  - [x] User attribution for all file operations
  - [x] Agent must be running to browse files
- **Flow**: User → Files tab → loadFiles() → GET /api/agents/{name}/files → agent-server /api/files → recursive workspace walk → file list → download → audit log

#### 9.4.1 Agent Collaboration Execution Tracking
- **Status**: ⏳ Not Started
- **Priority**: Medium
- **Description**: Track agent-to-agent calls in schedule_executions table
- **Acceptance Criteria**:
  - [ ] Extend schedule_executions with `triggered_by: "agent_collaboration"`
  - [ ] Add `caller_agent` field to track which agent initiated the call
  - [ ] Log full execution details (message, response, duration, cost, context)
  - [ ] Track access denied events with denial reason
  - [ ] Queryable via existing executions API
  - [ ] Visible in UI execution history

#### 9.6 Agent Collaboration Dashboard
- **Status**: ✅ Implemented (2025-12-01, Enhanced 2025-12-02)
- **Priority**: High
- **Description**: Real-time visual dashboard showing agents as draggable nodes with animated connections, context usage, and activity state
- **Acceptance Criteria**:
  - [x] Vue Flow integration (latest versions: @vue-flow/core 1.48.0, background 1.3.2, controls 1.1.3, minimap 1.5.0)
  - [x] Dashboard accessible at `/collaboration` route
  - [x] Custom AgentNode.vue component with clean white card design
  - [x] Draggable agent nodes with position persistence (localStorage)
  - [x] WebSocket real-time updates for collaboration events
  - [x] Animated edges when agents communicate (blue flowing dots)
  - [x] Edge fade-out after 3 seconds (gray inactive state)
  - [x] Zoom, pan, and minimap controls
  - [x] Agent status sync (running/stopped/starting colors)
  - [x] Collaboration stats (agent count, active collaborations, last event time)
  - [x] Click-through to agent detail page from nodes
  - [x] Connection status indicator
  - [x] Reset layout button
  - [x] Backend WebSocket broadcasts for agent-to-agent chats
  - [x] **NEW (2025-12-02)**: Real-time context window progress bars (0-100%)
  - [x] **NEW (2025-12-02)**: Color-coded progress: Green (<50%), Yellow (50-75%), Orange (75-90%), Red (>90%)
  - [x] **NEW (2025-12-02)**: Three activity states: Active (executing), Idle (running), Offline (stopped)
  - [x] **NEW (2025-12-02)**: Backend endpoint `/api/agents/context-stats` for context polling
  - [x] **NEW (2025-12-02)**: 5-second polling interval for context updates
  - [x] **NEW (2025-12-02)**: Activity state based on agent_activities table (< 60s = active)
- **Architecture**:
  - Frontend: Vue 3 + Vue Flow + Tailwind CSS
  - Backend: WebSocket event broadcasting via `broadcast_collaboration_event()`
  - Detection: `X-Source-Agent` header indicates agent-to-agent communication
  - Event format: `{type: "agent_collaboration", source_agent, target_agent, action, timestamp}`
  - Context polling: GET `/api/agents/context-stats` → Fetches from agent `/api/chat/session`
  - Activity detection: Recent activity in `agent_activities` table (60-second window)
- **UI/UX Design**:
  - Clean white cards with subtle shadows (matches mockup)
  - Agent name in bold, activity state label below
  - Context progress bar with percentage label
  - Status dot (green for active/idle, gray for offline, pulses when active)
  - Progress bar only visible for running agents (not offline)
- **Future Enhancements**:
  - Auto-layout algorithms (force-directed, hierarchical)
  - Edge click to show message history
  - Export graph as PNG/SVG
  - Adjustable polling interval (user preference)

#### 9.6.1 Collaboration Dashboard Replay Feature
- **Status**: ✅ Implemented (2025-12-02)
- **Priority**: High
- **Description**: Replay historical agent collaborations with time compression, allowing users to visualize past interaction patterns
- **Acceptance Criteria**:
  - [x] Mode toggle (Live/Replay) in dashboard header
  - [x] Replay controls (Play/Pause/Stop) with visual feedback
  - [x] Speed selector (1x, 2x, 5x, 10x, 20x, 50x)
  - [x] Timeline scrubber with event markers
  - [x] Draggable playback position marker
  - [x] Click timeline to jump to specific time
  - [x] Progress statistics (Event X/Y, elapsed time, remaining time)
  - [x] Time compression logic (real-time delta / speed multiplier)
  - [x] Minimum 100ms delay between events to prevent too fast playback
  - [x] Edge animation during replay (reuses existing animateEdge logic)
  - [x] Reset all edges to inactive on stop/jump
  - [x] Playback state persistence in localStorage
  - [x] WebSocket disconnection during replay mode
  - [x] Context polling stopped during replay mode
  - [x] Smooth transitions and hover effects
- **Architecture**:
  - Frontend-only feature (no backend changes required)
  - Uses existing historicalCollaborations from agent_activities table
  - Replay state managed in collaborations.js Pinia store
  - Timeline position calculated from event timestamps
  - Playback scheduling via setTimeout with dynamic delays
- **UI/UX**:
  - Mode toggle styled as pill-shaped button group
  - Replay controls only visible when in Replay mode
  - Timeline shows all events as gray dots
  - Playback marker is blue with handle for dragging
  - Event markers show tooltip on hover with source/target/time
  - Progress stats in dashboard header update in real-time
- **Performance**:
  - Timeline limited to 500 events from fetchHistoricalCollaborations
  - Event position calculation cached for smooth rendering
  - Playback timeout cleared on pause/stop to prevent memory leaks

#### 9.7 Unified Activity Stream
- **Status**: ✅ Implemented (2025-12-02)
- **Priority**: High
- **Description**: Unified activity tracking system using agent_activities table for real-time state monitoring and tool-level observability
- **Architecture**:
  - Single source of truth: `agent_activities` table for all runtime activities
  - Activity types: chat_start, chat_end, tool_call, schedule_start, schedule_end, agent_collaboration
  - Parent-child relationships: tool_call → chat_start linkage
  - Relationships: Links to chat_messages and schedule_executions via foreign keys
  - Observability: Cost/context stored in existing tables, JOINed when needed
- **Acceptance Criteria**:
  - [x] Database schema: agent_activities table with indexes
  - [x] Activity types enum: CHAT_START, CHAT_END, TOOL_CALL, SCHEDULE_START, SCHEDULE_END, AGENT_COLLABORATION
  - [x] Activity state enum: STARTED, COMPLETED, FAILED
  - [x] Centralized ActivityService for tracking and broadcasting
  - [x] Chat integration: track chat_start, tool_call (granular), chat_end
  - [x] Schedule integration: track schedule_start, schedule_end
  - [x] WebSocket real-time broadcasting of activity events
  - [x] Query API endpoints: GET /api/agents/{name}/activities, GET /api/activities/timeline
  - [x] Parent-child relationships (tool calls linked to parent chat)
  - [x] Foreign key links to chat_messages and schedule_executions
  - [x] Tool call storage: Both chat_messages.tool_calls JSON AND agent_activities rows
  - [x] Dashboard consumption: idle/active state visualization
  - [x] Performance: Indexed queries <50ms for timeline
  - [x] Backward compatibility: No changes to existing tables
- **Data Strategy**:
  - Keep existing chat_messages table unchanged (conversation content + summary)
  - Keep existing schedule_executions table for detailed results
  - Add agent_activities for real-time state + granular tool tracking
  - Tool calls stored in BOTH places (chat_messages JSON + agent_activities rows)
  - Cost/context stored in chat_messages/schedule_executions only (JOIN pattern)

#### 9.8 Task DAG System (Pillar I - Explicit Planning)
- **Status**: ❌ REMOVED (2025-12-23)
- **Reason**: Individual agent-level task planning removed. Task management at agent level is handled by Claude Code itself. System-level task management will be implemented via orchestrator agents in future phases.
- **What was removed**:
  - Workplan tab and WorkplanPanel.vue from AgentDetail
  - Task progress display from AgentNode.vue and Dashboard
  - Plan API endpoints from backend and agent-server
  - Workplan command files from trinity-meta-prompt
  - Plan-related state from stores (agents.js, network.js)
- **What remains**:
  - Trinity injection for agent collaboration (MCP tools, vector memory)
  - Basic Trinity meta-prompt (simplified, no workplan instructions)

#### 9.9 Agent Custom Metrics
- **Status**: 🚧 In Progress
- **Priority**: High
- **Description**: Agent-defined custom metrics displayed in Trinity UI
- **Architecture**:
  - Agents define metrics in `template.yaml` under `metrics:` field
  - Agents write current values to `metrics.json` in workspace
  - Agent server reads definitions + values via `GET /api/metrics`
  - Backend proxies to agent via `GET /api/agents/{name}/metrics`
  - Frontend MetricsPanel.vue displays metrics with type-specific rendering
- **Metric Types**:
  - `counter`: Monotonically increasing value (large number with label)
  - `gauge`: Current value that can go up/down (number with trend indicator)
  - `percentage`: 0-100 value (progress bar with percentage)
  - `status`: Enum/state value (colored badge)
  - `duration`: Time in seconds (formatted as "2h 15m")
  - `bytes`: Size in bytes (formatted as "1.2 MB")
- **Acceptance Criteria**:
  - [x] Metrics schema in template.yaml (AGENT_TEMPLATE_SPEC.md)
  - [ ] Agent server endpoint `GET /api/metrics`
  - [ ] Backend proxy endpoint `GET /api/agents/{name}/metrics`
  - [ ] MetricsPanel.vue component with type-specific rendering
  - [ ] Metrics tab in AgentDetail.vue
  - [ ] Auto-refresh every 30 seconds when tab is active
  - [ ] Test agent template.yaml files with metrics definitions
- **Flow**:
  1. Template defines metrics in `template.yaml`
  2. Agent writes values to `workspace/metrics.json`
  3. User opens Metrics tab
  4. Frontend calls `GET /api/agents/{name}/metrics`
  5. Backend proxies to agent `GET /api/metrics`
  6. Agent server reads template.yaml + metrics.json
  7. Frontend renders metrics with type-specific visualizations

#### 9.10 Agent-to-Agent Permissions
- **Status**: ✅ Implemented (2025-12-10)
- **Priority**: High
- **Description**: Explicit permission model controlling which agents can call which other agents via MCP
- **Architecture**:
  - Database table: `agent_permissions(source_agent, target_agent, created_at, created_by)`
  - Permission check in MCP server before forwarding agent calls
  - UI: Permissions tab in AgentDetail.vue for owners
  - Default grant: Same-owner agents get mutual permissions on creation
- **Acceptance Criteria**:
  - [x] Database schema for agent_permissions table
  - [x] Permission check in chat_with_agent MCP tool
  - [x] Permission denied returns error (not silent failure)
  - [x] Permissions tab UI with toggle switches
  - [x] Default permissions granted on agent creation
  - [x] Cascade delete on agent deletion

#### 9.11 Agent Shared Folders
- **Status**: ✅ Implemented (2025-12-13)
- **Priority**: High
- **Description**: File-based collaboration between agents via shared Docker volumes
- **Architecture**:
  - Database table: `agent_shared_folder_config(agent_name, expose_enabled, consume_enabled)`
  - Expose: Creates volume `agent-{name}-shared` mounted at `/home/developer/shared-out`
  - Consume: Mounts permitted agents' volumes at `/home/developer/shared-in/{agent}`
  - Permission-gated: Only permitted agents can mount shared folders
- **Acceptance Criteria**:
  - [x] Database schema for agent_shared_folder_config table
  - [x] Volume creation/mounting in agent creation flow
  - [x] Volume cleanup on agent deletion
  - [x] API endpoints: GET/PUT /api/agents/{name}/folders
  - [x] API endpoints: GET /api/agents/{name}/folders/available
  - [x] API endpoints: GET /api/agents/{name}/folders/consumers
  - [x] Shared Folders tab in AgentDetail.vue
  - [x] FoldersPanel.vue component with toggle switches
  - [x] Restart required indicator when config changes
  - [x] Frontend store actions for folder management
- **Flow**:
  1. Agent A enables "Expose Shared Folder" (creates volume, mounts at `/home/developer/shared-out`)
  2. Agent B granted permission to communicate with Agent A
  3. Agent B enables "Mount Shared Folders" (will mount Agent A's volume at `/home/developer/shared-in/agent-a`)
  4. Both agents restart to apply volume mounts
  5. Agent A writes files to `shared-out/`, Agent B reads from `shared-in/agent-a/`
- **Testing**:
  1. Create two agents (agent-a, agent-b)
  2. Enable expose on agent-a, enable consume on agent-b
  3. Grant agent-b permission to call agent-a
  4. Restart both agents
  5. Write file in agent-a's shared-out folder
  6. Verify file visible in agent-b's shared-in/agent-a folder

---

### 10. Planned Features

#### 10.1 Automated Git Sync
- **Status**: ⏳ Not Started
- **Priority**: Medium
- **Description**: Automatic scheduled sync to GitHub (extends 9.2)
- **Acceptance Criteria**:
  - [ ] Sync mode selector: Manual / Scheduled / On Stop
  - [ ] Scheduled sync via existing scheduler infrastructure
  - [ ] Auto-sync on agent stop
  - [ ] Sync on "New Session" in chat

#### 10.2 Automated Secret Rotation
- **Status**: ⏳ Not Started
- **Priority**: Medium
- **Description**: Automatic credential rotation
- **Acceptance Criteria**:
  - [ ] Rotation schedule
  - [ ] Auto-refresh tokens
  - [ ] Notification on rotation

#### 10.3 Kubernetes Deployment
- **Status**: ⏳ Not Started
- **Priority**: Low
- **Description**: K8s deployment scripts
- **Acceptance Criteria**:
  - [ ] Helm charts
  - [ ] StatefulSet for agents
  - [ ] Horizontal scaling

#### 10.4 Agent Vector Memory (Chroma)
- **Status**: ❌ REMOVED (2025-12-24)
- **Reason**: Development workflow parity - agents should be self-contained. Templates that need vector memory should include it themselves.
- **What was removed**:
  - chromadb, sentence-transformers, chroma-mcp from base image (~800MB savings)
  - Vector store directory creation during Trinity injection
  - Chroma MCP config injection into .mcp.json
  - vector-memory.md documentation file
  - Vector memory section in injected CLAUDE.md
- **Alternative**: Templates can include vector memory dependencies and configuration. See reference templates for examples.

#### 10.5 Chroma MCP Server Integration
- **Status**: ❌ REMOVED (2025-12-24)
- **Reason**: Removed along with 10.4 - platform should not inject agent capabilities

#### 10.6 System-Wide Trinity Prompt
- **Status**: ✅ Implemented (2025-12-14)
- **Priority**: Medium
- **Description**: Admin-configurable system prompt that gets injected into all agents' CLAUDE.md at startup
- **Architecture**:
  - System setting stored in SQLite `system_settings` table (key: `trinity_prompt`)
  - Retrieved during agent start via `inject_trinity_meta_prompt()`
  - Passed to agent-server injection API
  - Injected as "## Custom Instructions" section in CLAUDE.md after Trinity section
- **Acceptance Criteria**:
  - [x] Database table: `system_settings(key, value, updated_at)`
  - [x] Backend API: GET/PUT/DELETE `/api/settings/{key}` (admin-only)
  - [x] Agent-server accepts `custom_prompt` in injection request
  - [x] Custom prompt injected into CLAUDE.md during Trinity injection
  - [x] Settings page in frontend (admin-only access)
  - [x] NavBar shows Settings link for admin users only
  - [x] Changes apply on agent start/restart
- **Testing**:
  1. Login as admin, navigate to Settings
  2. Enter custom Trinity prompt, save
  3. Start/restart any agent
  4. Verify agent's CLAUDE.md contains "## Custom Instructions" with the prompt
  5. Clear prompt, restart agent, verify section removed
  6. Non-admin users should not see Settings link in nav

#### 10.7 System Manifest (Multi-Agent Deployment)
- **Status**: ✅ Complete (2025-12-18)
- **Priority**: High
- **Description**: Recipe-based deployment of multi-agent systems via YAML manifest
- **Design Doc**: `docs/drafts/SYSTEM_MANIFEST_SIMPLIFIED.md`
- **Implementation**: `docs/drafts/SYSTEM_MANIFEST_PHASE1.md`
- **Approach**: Recipe (one-shot deployment, not declarative/binding)
- **Key Decisions**:
  - Agent naming: `{system}-{agent}` format
  - Re-deploy creates new agents with `_N` suffix (e.g., `ruby_2`, `ruby_3`)
  - Updates global `trinity_prompt` setting (reuses existing 10.6)
  - No `systems` table - agents are independent after creation
- **YAML Format**:
  ```yaml
  name: content-production
  description: Autonomous content pipeline

  prompt: |  # Updates global trinity_prompt
    System-wide instructions here...

  agents:
    orchestrator:
      template: github:YourOrg/repo
      resources: {cpu: "2", memory: "4g"}
      folders: {expose: true, consume: true}
      schedules:
        - name: daily-task
          cron: "0 9 * * *"
          message: "Do the thing"

    worker:
      template: local:business-assistant
      folders: {expose: true, consume: true}

  permissions:
    preset: full-mesh  # or: orchestrator-workers, none, explicit
  ```
- **Acceptance Criteria**:
  - [x] YAML parsing with pydantic validation *(Phase 1 - 2025-12-17)*
  - [x] `POST /api/systems/deploy` endpoint *(Phase 1 - 2025-12-17)*
  - [x] Dry run mode for validation (`dry_run: true` in body) *(Phase 1 - 2025-12-17)*
  - [x] Agent creation with `{system}-{agent}` naming *(Phase 1 - 2025-12-17)*
  - [x] Incremental suffix for conflicts (`_2`, `_3`) *(Phase 1 - 2025-12-17)*
  - [x] Global `trinity_prompt` update from manifest *(Phase 1 - 2025-12-17)*
  - [x] Dry run returns validation + warnings *(Phase 1 - 2025-12-17)*
  - [x] All agents started after configuration *(Phase 1 - 2025-12-17)*
  - [x] Permission presets: `full-mesh`, `orchestrator-workers`, `none` *(Phase 2 - 2025-12-18)*
  - [x] Explicit permission matrix support *(Phase 2 - 2025-12-18)*
  - [x] Shared folder configuration per agent *(Phase 2 - 2025-12-18)*
  - [x] Schedule creation per agent *(Phase 2 - 2025-12-18)*
  - [x] MCP tools for system deployment (`deploy_system`, `list_systems`, `restart_system`, `get_system_manifest`) *(Phase 3 - 2025-12-18)*
  - [x] Backend API endpoints for system management (`GET /systems`, `GET /systems/{name}`, `POST /systems/{name}/restart`, `GET /systems/{name}/manifest`) *(Phase 3 - 2025-12-18)*
  - [x] Export manifest function to generate YAML from deployed systems *(Phase 3 - 2025-12-18)*
- **Maps to Existing APIs**:
  - `prompt` → `PUT /api/settings/trinity_prompt`
  - `agents.*` → `POST /api/agents`
  - `agents.*.folders` → `PUT /api/agents/{name}/folders`
  - `agents.*.schedules[]` → `POST /api/agents/{name}/schedules`
  - `permissions` → `PUT /api/agents/{name}/permissions`
- **Testing**:
  1. Create `system.yaml` with 2+ agents
  2. `POST /api/systems/deploy` with YAML
  3. Verify agents created with correct names
  4. Verify permissions configured per preset
  5. Verify shared folders enabled
  6. Verify schedules created
  7. Re-deploy same YAML, verify `_2` suffix agents created
  8. Dry run returns warnings for existing agents

#### 10.8 OpenTelemetry UI Integration
- **Status**: ✅ Implemented (2025-12-20)
- **Priority**: High
- **Depends On**: OpenTelemetry Integration (implemented 2025-12-20)
- **Description**: Display OTel metrics in Trinity UI - system-wide cost, token usage, and productivity metrics from Claude Code agents
- **Feature Flow**: `docs/memory/feature-flows/opentelemetry-integration.md`
- **Architecture**:
  - Backend API queries Prometheus endpoint at `:8889/metrics`
  - Frontend displays in Dashboard header (summary) and panel (detail)
  - Opt-in visibility: only shows when `OTEL_ENABLED=1` and collector reachable
  - Flexible layout to accommodate new metrics over time
- **Metrics Available**:
  - `cost_usage_USD` - Cost per model (counter)
  - `token_usage_tokens` - Tokens per model/type (input, output, cacheCreation, cacheRead)
  - `lines_of_code_count` - Lines added/removed (counter)
  - `session_count` - Session count (counter)
  - `active_time_total_seconds` - Active usage duration (counter)
  - `pull_request_count` - PRs created (counter)
  - `commit_count` - Commits created (counter)
- **UI Placement**:
  | Location | What to Show |
  |----------|--------------|
  | Dashboard Header | Total cost, total tokens, OTel status indicator |
  | Dashboard Panel | Full breakdown by model/type in "Observability" tab |
  | AgentDetail | *Future* - Per-agent cost (requires agent_name label) |
- **Acceptance Criteria**:
  - [x] **Phase 1: Backend API**
    - [x] `GET /api/observability/metrics` endpoint
    - [x] Returns `{enabled: false}` when `OTEL_ENABLED=0`
    - [x] Queries Prometheus at `:8889/metrics` when enabled
    - [x] Parses and returns structured JSON with all metrics
    - [x] Graceful error handling when collector unreachable
  - [x] **Phase 1: Dashboard Header**
    - [x] OTel summary stats in header (cost, tokens, status indicator)
    - [x] Only visible when OTel enabled and has data
    - [x] Follows existing header stats pattern
  - [x] **Phase 1: Dashboard Panel**
    - [x] "Observability" tab in collapsible panel
    - [x] Cost breakdown by model (table)
    - [x] Token usage by type (table)
    - [x] Productivity metrics (lines, sessions, active time, commits, PRs)
    - [x] Auto-refresh every 60 seconds
  - [ ] **Phase 2: Charts (Future)**
    - [ ] Dedicated `/observability` page
    - [ ] Time series charts for cost/tokens over time
    - [ ] Model comparison visualization
  - [ ] **Phase 3: Per-Agent Attribution (Future)**
    - [ ] Add `agent_name` label to OTel metrics
    - [ ] Show per-agent cost in AgentDetail
- **Testing**:
  1. With `OTEL_ENABLED=0`: Verify no OTel UI elements shown
  2. With `OTEL_ENABLED=1` but collector down: Verify graceful "unavailable" state
  3. With OTel fully working: Verify header shows cost/tokens
  4. Generate chat activity, wait 60s, verify metrics update
  5. Check panel shows full breakdown by model/type

---

### 11. Ecosystem & Enterprise (Phase 11)

#### 11.1 Internal System Agent
- **Status**: ✅ Implemented (2025-12-20)
- **Priority**: High
- **Description**: Auto-deployed platform orchestrator that can manage and coordinate all other agents
- **Feature Flow**: `docs/memory/feature-flows/internal-system-agent.md`
- **Architecture**:
  - **Name**: `trinity-system` (fixed, cannot be changed)
  - **Template**: `local:trinity-system` in `config/agent-templates/trinity-system/`
  - **Auto-deployment**: Deploys on backend startup, creates container if missing
  - **Deletion-protected**: Cannot be deleted, only re-initialized
  - **System MCP scope**: `scope: "system"` bypasses all permission checks
- **Components**:
  | Component | Purpose |
  |-----------|---------|
  | `template.yaml` | Agent configuration and metrics |
  | `CLAUDE.md` | System agent instructions for orchestration |
  | `is_system` column | Database flag for protection |
  | `system_agent_service.py` | Auto-deployment service |
  | `system_agent` router | Status, restart, reinitialize endpoints |
- **API Endpoints**:
  | Endpoint | Method | Description |
  |----------|--------|-------------|
  | `/api/system-agent/status` | GET | Get system agent status |
  | `/api/system-agent/restart` | POST | Restart system agent (admin) |
  | `/api/system-agent/reinitialize` | POST | Reset to clean state (admin) |
- **MCP Permissions**:
  - System-scoped keys (`scope: "system"`) bypass all permission checks
  - System agent can communicate with any agent regardless of owner
  - System agent can list all agents without filtering
  - System agent cannot delete itself
- **Acceptance Criteria**:
  - [x] Template created at `config/agent-templates/trinity-system/`
  - [x] `is_system` column in `agent_ownership` table
  - [x] Delete protection in backend (403 for system agents)
  - [x] Auto-deploy on backend startup
  - [x] System-scoped MCP key with permission bypass
  - [x] System badge in UI (agent nodes and detail page)
  - [x] Admin-only reinitialize endpoint
- **Testing**:
  1. Restart backend, verify trinity-system is deployed
  2. Attempt to delete via API, verify 403 response
  3. Verify system agent can chat with any agent
  4. Verify SYSTEM badge appears in UI
  5. Admin reinitialize resets to clean state

#### 11.2 System Agent Operations Scope
- **Status**: ✅ Implemented (2025-12-20)
- **Priority**: High
- **Description**: The system agent focuses exclusively on platform operations (health, lifecycle, resource governance) rather than workflow orchestration
- **Design Doc**: `docs/drafts/SYSTEM_AGENT_OPS_REQUIREMENTS.md`
- **Guiding Principle**: "The system agent manages the orchestra, not the music."
- **Operational Scope**:
  | Area | Responsibilities |
  |------|------------------|
  | **Health Monitoring** | Detect stuck agents, high context usage, container failures |
  | **Lifecycle Management** | Start, stop, restart agents based on health |
  | **Resource Governance** | Monitor cost, context thresholds, memory bounds |
  | **Schedule Control** | Enable/disable schedules, trigger manual runs, pause automation |
  | **Validation** | Pre-flight checks before agent operations |
  | **Alerting** | Notify on anomalies, failures, threshold breaches |
  | **Cleanup** | Reset stuck sessions, archive old plans |
  | **Reporting** | Fleet status, cost summaries, health reports |
- **Out of Scope**:
  - Task orchestration (agents coordinate themselves via MCP)
  - Content/output review (domain-specific)
  - Workflow design (user/developer responsibility)
  - Agent-to-agent messaging (agents handle directly)
- **Slash Commands**:
  | Command | Description |
  |---------|-------------|
  | `/ops/status` | Fleet status report |
  | `/ops/health` | Health check all agents |
  | `/ops/restart <agent>` | Restart specific agent |
  | `/ops/restart-all` | Restart entire fleet |
  | `/ops/stop <agent>` | Stop specific agent |
  | `/ops/schedules` | Schedule overview |
  | `/ops/costs` | Cost report from OTel |
- **API Endpoints**:
  | Endpoint | Method | Description |
  |----------|--------|-------------|
  | `/api/ops/fleet/status` | GET | All agents status with context |
  | `/api/ops/fleet/health` | GET | Health summary with issues |
  | `/api/ops/fleet/restart` | POST | Restart all/filtered agents |
  | `/api/ops/fleet/stop` | POST | Stop all/filtered agents |
  | `/api/ops/schedules/pause` | POST | Pause all schedules |
  | `/api/ops/schedules/resume` | POST | Resume all schedules |
  | `/api/ops/emergency-stop` | POST | Halt all executions |
  | `/api/settings/ops/config` | GET | Get ops settings |
  | `/api/settings/ops/config` | PUT | Update ops settings |
- **Ops Settings** (stored in `system_settings` table):
  | Key | Type | Default | Description |
  |-----|------|---------|-------------|
  | `ops_context_warning_threshold` | int | 75 | Context % to trigger warning |
  | `ops_context_critical_threshold` | int | 90 | Context % to trigger critical |
  | `ops_idle_timeout_minutes` | int | 30 | Minutes before stuck detection |
  | `ops_cost_limit_daily_usd` | float | 50.0 | Daily cost limit (0 = unlimited) |
  | `ops_max_execution_minutes` | int | 10 | Max chat execution time |
  | `ops_alert_suppression_minutes` | int | 15 | Suppress duplicate alerts |
  | `ops_log_retention_days` | int | 7 | Days to keep container logs |
  | `ops_health_check_interval` | int | 60 | Seconds between health checks |
- **Acceptance Criteria**:
  - [x] Updated CLAUDE.md with ops-only scope
  - [x] Created `/ops/*` slash commands
  - [x] Fleet operations API endpoints
  - [x] Schedule control endpoints
  - [x] Emergency stop functionality
  - [x] Ops settings with defaults
  - [x] Settings API for ops configuration
- **Testing**:
  1. Chat with system agent, run `/ops/status`
  2. Verify fleet status report generated
  3. Run `/ops/health`, verify health issues identified
  4. Test emergency stop pauses schedules and stops agents
  5. Verify ops settings have defaults and can be modified

#### 11.3 Public Agent Links (Phase 12.2)
- **Status**: ✅ Implemented (2025-12-22)
- **Priority**: Medium
- **Description**: Generate shareable public links that allow unauthenticated users to chat with agents
- **Design Doc**: `docs/drafts/PUBLIC_AGENT_LINKS.md`
- **Features**:
  - Agent owners can create public links with optional email verification
  - Public links have unique tokens (URL-safe, 24 characters)
  - Optional expiration dates for links
  - Rate limiting per IP (30 messages/minute)
  - Email verification uses 6-digit codes (10-minute expiry)
  - Session tokens for verified users (24-hour validity)
  - Usage tracking (message counts, unique users, last used)
- **Database Tables**:
  | Table | Purpose |
  |-------|---------|
  | `agent_public_links` | Link metadata (token, agent, settings) |
  | `public_link_verifications` | Email verification codes and sessions |
  | `public_link_usage` | Usage tracking (messages, IPs, users) |
- **API Endpoints (Owner)**:
  | Endpoint | Method | Description |
  |----------|--------|-------------|
  | `/api/agents/{name}/public-links` | GET | List all public links |
  | `/api/agents/{name}/public-links` | POST | Create new public link |
  | `/api/agents/{name}/public-links/{id}` | GET | Get link details |
  | `/api/agents/{name}/public-links/{id}` | PUT | Update link settings |
  | `/api/agents/{name}/public-links/{id}` | DELETE | Delete/revoke link |
- **API Endpoints (Public)**:
  | Endpoint | Method | Description |
  |----------|--------|-------------|
  | `/api/public/link/{token}` | GET | Get link info (valid, requires email) |
  | `/api/public/verify/request` | POST | Request verification code |
  | `/api/public/verify/confirm` | POST | Confirm code, get session |
  | `/api/public/chat/{token}` | POST | Send chat message |
- **Frontend Components**:
  | Component | Purpose |
  |-----------|---------|
  | `PublicChat.vue` | Public-facing chat interface |
  | `PublicLinksPanel.vue` | Owner management in AgentDetail |
- **Email Providers**: Console (dev), SMTP, SendGrid
- **Acceptance Criteria**:
  - [x] Create/edit/delete public links via UI
  - [x] Copy link URL to clipboard
  - [x] Enable/disable links
  - [x] Email verification flow (when enabled)
  - [x] Public chat interface without login
  - [x] Usage statistics displayed to owner
  - [x] Rate limiting prevents abuse
  - [x] Audit logging for all public access
- **Testing**:
  1. Create public link without email requirement
  2. Access `/chat/{token}`, verify chat works
  3. Create link with email requirement
  4. Verify email flow, receive code, confirm
  5. Test rate limiting (>30 messages/min)
  6. Disable link, verify chat blocked
  7. Delete link, verify 404 response

#### 11.4 First-Time Setup (Phase 12.3)
- **Status**: ✅ Implemented (2025-12-23)
- **Priority**: High
- **Description**: First-time setup wizard for admin password and API key configuration
- **Features**:
  - On first launch, redirect to `/setup` to set admin password
  - Password must be at least 8 characters
  - Uses bcrypt hashing for secure password storage
  - Login blocked until setup complete
  - Settings page includes API Keys section for Anthropic key management
  - API key can be tested before saving
  - API key from settings takes precedence over environment variable
- **Backend Changes**:
  | Change | Description |
  |--------|-------------|
  | `dependencies.py` | Added `hash_password()`, updated `verify_password()` for bcrypt |
  | `db/users.py` | Added `update_user_password()` method |
  | `routers/setup.py` | New router with `/api/setup/status` and `/api/setup/admin-password` |
  | `routers/auth.py` | Added `is_setup_completed()` check, blocks login if setup incomplete |
  | `routers/settings.py` | Added API key endpoints: GET/PUT/DELETE `/api/settings/api-keys/anthropic`, POST `/api/settings/api-keys/anthropic/test` |
  | Agent creation | Uses `get_anthropic_api_key()` helper (settings > env fallback) |
- **Frontend Changes**:
  | Component | Description |
  |-----------|-------------|
  | `SetupPassword.vue` | New view for initial password setup |
  | `router/index.js` | Added `/setup` route with setup status check guard |
  | `Settings.vue` | Added API Keys section with test/save functionality |
- **API Endpoints**:
  | Endpoint | Method | Description |
  |----------|--------|-------------|
  | `/api/setup/status` | GET | Check if setup is complete (no auth) |
  | `/api/setup/admin-password` | POST | Set initial admin password (no auth, once only) |
  | `/api/settings/api-keys` | GET | Get API keys status (admin) |
  | `/api/settings/api-keys/anthropic` | PUT | Set Anthropic API key (admin) |
  | `/api/settings/api-keys/anthropic` | DELETE | Delete Anthropic API key (admin) |
  | `/api/settings/api-keys/anthropic/test` | POST | Test API key validity (admin) |
- **Acceptance Criteria**:
  - [x] First launch shows /setup page
  - [x] Password form validates length and match
  - [x] Password strength indicator
  - [x] Bcrypt hashing for secure storage
  - [x] Login blocked until setup complete
  - [x] API key section in Settings
  - [x] Test button validates key with Anthropic API
  - [x] Save button stores key in database
  - [x] Agent creation uses stored key with env fallback
- **Testing**:
  1. Fresh install: Delete `setup_completed` setting from database
  2. Navigate to any page, verify redirect to /setup
  3. Set password, verify redirect to /login
  4. Login with new password
  5. Navigate to Settings, add Anthropic API key
  6. Test key, verify validation works
  7. Save key, create agent, verify agent uses stored key

#### 11.5 Web Terminal for System Agent
- **Status**: ✅ Implemented (2025-12-25)
- **Priority**: High
- **Description**: Browser-based interactive terminal for System Agent with full Claude Code TUI support
- **Design Doc**: `docs/requirements/WEB_TERMINAL_SYSTEM_AGENT.md`
- **Architecture**:
  - Frontend: xterm.js v5.5.0 terminal emulator in browser
  - Backend: WebSocket endpoint with PTY forwarding via Docker exec
  - Transport: Bidirectional binary WebSocket frames
  - Security: Admin-only, JWT auth, no SSH port exposure
- **Key Features**:
  - Full Claude Code TUI preserved (colors, prompts, shortcuts, streaming)
  - Terminal resize support (SIGWINCH forwarding)
  - Mode toggle: Claude Code or Bash shell
  - Session audit logging (start/end with duration)
  - Session limit (1 terminal per user)
- **Acceptance Criteria**:
  - [x] "Open Terminal" button on System Agent page
  - [x] Terminal opens in modal/panel with xterm.js
  - [x] Full Claude Code functionality (test `/help`, Ctrl+C, colors)
  - [x] Admin-only access (non-admins see disabled/hidden button)
  - [x] Terminal resize follows browser window
  - [x] Audit log entries for session start/end
  - [x] Clean PTY termination on disconnect
  - [x] Mode toggle (Claude Code / Bash)
- **Frontend Components**:
  - `SystemAgentTerminal.vue` - xterm.js terminal with WebSocket connection
  - Modal integration in `SystemAgent.vue` with Teleport
- **Backend Components**:
  - WebSocket endpoint: `WS /api/system-agent/terminal?mode=claude|bash`
  - PTY allocation via Docker SDK `exec_create(tty=True)`
  - Bidirectional socket forwarding with `select()`
  - `decode_token()` helper for WebSocket auth
- **Frontend Dependencies**:
  - `@xterm/xterm` ^5.5.0
  - `@xterm/addon-fit` ^0.10.0
  - `@xterm/addon-web-links` ^0.11.0
- **Backend**: Uses existing `docker==7.1.0` with `exec_create(tty=True)` and `exec_start(socket=True)`
- **Testing**:
  1. Login as admin, navigate to System Agent page
  2. Click "Terminal" button, verify terminal opens
  3. Type commands, verify output displays correctly
  4. Test Claude Code mode (`/help`, Ctrl+C, colors)
  5. Test Bash mode (basic shell commands)
  6. Resize browser window, verify terminal resizes
  7. Close modal, verify session terminates cleanly
  8. Non-admin users should not see Terminal button

#### 11.6 Per-Agent API Key Control

- **Status**: Complete (2025-12-26)
- **Priority**: High
- **Description**: Allow agents to use either platform API key or user's own Claude subscription via terminal authentication
- **Acceptance Criteria**:
  - [x] Database: `use_platform_api_key` column in agent_ownership (default: true)
  - [x] API endpoints: GET/PUT `/api/agents/{name}/api-key-setting`
  - [x] Container recreation on start if setting changed (env var add/remove)
  - [x] UI toggle in Terminal tab when agent is stopped
  - [x] User can select "Authenticate in Terminal" to use own subscription
  - [x] Agent starts without ANTHROPIC_API_KEY when using terminal auth
  - [x] User runs `claude login` in terminal to authenticate
- **Files**:
  | File | Purpose |
  |------|---------|
  | `database.py` | Migration for use_platform_api_key column |
  | `db/agents.py` | get_use_platform_api_key, set_use_platform_api_key methods |
  | `routers/agents.py` | API endpoints, container recreation logic |
  | `agents.js` | Store methods for API key setting |
  | `AgentDetail.vue` | Radio toggle UI in Terminal tab |
- **Testing**:
  1. Create new agent, verify uses platform key by default
  2. Go to Terminal tab when stopped
  3. Select "Authenticate in Terminal", verify toast shows restart required
  4. Start agent, open terminal
  5. Run `claude login` or verify Claude prompts for auth
  6. Restart agent, verify auth persists (stored in persistent volume)
  7. Switch back to "Use Platform API Key", restart, verify platform key used

---

## Non-Functional Requirements

### Performance
- Agent creation: < 30 seconds
- Chat response: < 5 seconds for simple queries
- UI responsiveness: < 100ms for interactions
- WebSocket latency: < 500ms for status updates

### Security
- All credentials encrypted at rest (Redis)
- HTTPS in production (Let's Encrypt)
- Container isolation (network, filesystem)
- Audit logging for compliance
- Domain restriction (@ability.ai only)

### Scalability
- Support 50+ concurrent agents
- Multiple backend workers (uvicorn)
- Stateless backend (Docker as truth)

### Reliability
- Agent state survives backend restart
- SQLite persists across container recreation
- Redis AOF for secret durability

### Testing
- Every feature flow includes Testing section with step-by-step instructions
- Manual testing by following documented steps before marking features complete
- Automated tests only for repeated failures or critical paths
- Feature verification at UI, API, Docker, and database levels

---

## Out of Scope

- Multi-tenant deployment (single org only)
- Custom model providers (Claude only)
- Mobile application
- Billing/payment integration
- Agent marketplace
