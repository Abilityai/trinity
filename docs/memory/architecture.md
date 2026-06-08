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
- `agent_files.py` - Files, info, playbooks, permissions, metrics, shared folders, file-sharing toggle + list/revoke (FILES-001)
- `loops.py` - Sequential agent loops: start/get/stop + agent-scoped list (#740)
- `files.py` - Public download endpoint for outbound agent file sharing (FILES-001)
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
- `telegram.py` - Telegram bot integration (webhook receiver, bot binding, group config) (TELEGRAM-001/TGRAM-GROUP)
- `whatsapp.py` - WhatsApp via Twilio (webhook receiver, binding CRUD + test) (WHATSAPP-001)
- `voip.py` - VoIP telephony: per-agent Twilio-voice binding CRUD (owner-only), outbound call trigger (idempotent, rate-limited), and the Media Streams WebSocket entrypoint. Feature-flag gated (`voip_available`, default OFF) (VOIP-001, #1056)
- `webhooks.py` - Public webhook trigger endpoint + JWT-auth webhook management (WEBHOOK-001, #291)
- `messages.py` - Proactive agent-to-user messaging (#321)
- `public_memory.py` - Per-user memory write endpoint for channel sessions (MEM-001, #888)

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
- `agent_client.py` - HTTP client for agent container communication (chat, session, injection); Redis-backed **transport** circuit breaker (`CircuitState`, `agent:circuit:{name}`) with exponential backoff + dormant state (#631); only TCP/connection failures count toward the circuit — HTTP 4xx/5xx and 502/503/504 are treated as application errors and skip the failure counter (#474). Shared Redis plumbing (fail-open client, Lua `ScriptCache`, decode helpers) was extracted to the top-level `redis_breaker_util.py` so the dispatch breaker (#526) reuses it without duplication (RELIABILITY-007).
- `settings_service.py` - Centralized settings retrieval (API keys, ops config, agent quotas)

*Execution & Scheduling:*
- `task_execution_service.py` - Unified task execution lifecycle (slot mgmt, activity tracking, sanitization) (EXEC-024). On reader-race empty results (502 dict body with `num_turns < 5`, `raw_message_count == 0`, `parse_failure_count == 0`), fires one in-line auto-retry with the same `execution_id` capped at 300s, persisting `retry_count` and rolling previous-attempt cost into the terminal write (#678). **Dispatch breaker outcome recording (#526):** the single execution path, so it records every outcome to `DispatchBreaker` (gated on the combined global+per-agent flag) — `record_outcome(None)` at the success terminal (resets), `record_outcome(AUTH)` gated on `error_code == AUTH` at the HTTP-error terminal (counts). On the `→open` transition it backgrounds `_fail_backlog_and_audit` via `_spawn_bg` (holds a strong task ref so the fire-and-forget drain can't be GC'd mid-flight) — `db.fail_queued_for_agent` → FAILED + clear in-memory queue + audit; if that task is still lost or its DB write throws, the 60s breaker-aware `run_maintenance` sweep re-fails the queued backlog for any still-open breaker (~60s worst case, not the 24h generic expiry). Catches `CircuitOpen` from `acquire` → `TaskExecutionResult(CIRCUIT_OPEN)` + FAILED row; the 3b pre-dispatch check also fast-fails on a non-probe-consuming dispatch `state == "open"` read, but ONLY on the backlog-drain path (`slot_already_held and not dispatch_gate_checked`) so it never blocks a probe an upstream `acquire` gate already admitted.
- `capacity_manager.py` - **Unified capacity facade (#428, CAPACITY-CONSOLIDATE).** Single public API for admit/release/status across `/chat` (`max_concurrent=max_parallel_tasks`, `queue_in_memory` policy) and `/task` (`queue_persistent` policy). Composes `slot_service.py` and `backlog_service.py` internally; owns the in-memory overflow store (Redis LIST, depth 3). Replaces the prior three-class pyramid (`SlotService` + `ExecutionQueue` + `BacklogService`); `ExecutionQueue` deleted, the other two are now private internals. `acquire(... breaker_enabled=False)` gates on the dispatch breaker at the TOP of `acquire` (before the overflow branch) when both the per-agent flag and global `DISPATCH_BREAKER_ENABLED` are on. A `deny` (open within cooldown, or a sibling holds the probe) raises `CircuitOpen` before any slot/overflow work, so a doomed task is never enqueued (the no-enqueue invariant, #526 D2). When the breaker is open and the call holds the half-open **probe**, the probe is admitted ONLY into a free slot — if slots are full the probe fast-fails (`CircuitOpen`) rather than enqueuing, so the no-enqueue invariant extends across the half-open window and the probe always leads to a recorded dispatch instead of a verdict-less backlog row that would stall the breaker's backoff (#526 F1).
- `slot_service.py` - Internal: atomic N-ary capacity counter (Redis ZSET) with dynamic per-agent TTL (CAPACITY-001). Used only by `CapacityManager`.
- `backlog_service.py` - Internal: persistent SQLite-backed FIFO overflow store with drain-on-release (BACKLOG-001). Used only by `CapacityManager`.
- `dispatch_breaker.py` - **Per-agent dispatch circuit breaker (RELIABILITY-007, #526).** Producer-side breaker fed *only* by execution outcomes in `task_execution_service` — counts **AUTH only** (`error_code == AUTH`, agent answers HTTP 503), NOT TIMEOUT/AGENT_ERROR (D10). Consecutive-failure machine (`closed → open → half-open(probe) → closed`, default threshold 3, base cooldown 30s, exp backoff) in Redis `agent:dispatch:{name}` reusing the proven `CircuitState` Lua pattern (D9). Separate namespace + separate Lua from the transport breaker, so the two never contaminate each other's counter. `record_outcome(error_code)` returns the `(prior,new)` transition; the **caller** backgrounds the drain on `→open` (no `capacity`/`db` import here → no circular dep, D3). Fail-open on Redis down; never raises. Exposes `record_failure("missed_heartbeat")` as the #307 heartbeat seam. `record_success` is a no-op write (Lua early-return) when the breaker is already closed with zero failures, so a healthy breaker-enabled agent doesn't churn Redis on every successful execution.
- `scheduler_service.py` - APScheduler-based scheduling service
- `cleanup_service.py` - Active watchdog reconciliation + passive stale recovery for executions, activities, and slots (CLEANUP-001, #129)

*Real-time delivery:*
- `event_bus.py` - Redis Streams transport for WebSocket delivery (`EventBus` publisher + `StreamDispatcher` consumer, reconnect replay via `last-event-id`, 3-failure client eviction, MAXLEN-trimmed stream) (RELIABILITY-003, #306)

*Monitoring & Activities:*
- `activity_service.py` - Activity tracking and timeline
- `monitoring_service.py` - Fleet-wide health monitoring (MON-001)
- `monitoring_alerts.py` - Alert threshold configuration
- `heartbeat_service.py` - Agent push-heartbeat liveness layer (RELIABILITY-004, #307). Owns all Redis heartbeat keys; `record_heartbeat` (SETEX 15s + persistent `seen` marker), `read_heartbeat`, `heartbeat_status`/`heartbeat_status_bulk` (one pipelined round-trip, D4), `authorize_heartbeat` (Option B — only the agent's own agent-scoped MCP key), and `run_heartbeat_watch_loop`/`process_watch_tick` (5s loop, 3-miss guard, fires a cooldown-debounced operator alert via `monitoring_alerts` on the alive→stale transition; writes no health row). Additive to the 30s `monitoring_service.py`, which stays authoritative.
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
- `agent_shared_files_service.py` - Outbound file sharing: path validation, MIME blocklist, quota, Docker `get_archive` extraction, URL building (FILES-001)
- `loop_service.py` - Sequential agent loops: in-process `asyncio.Task` runner, cooperative stop, template substitution, WS events (`loop_run_completed`, `loop_completed`) (#740)
- `voip_service.py` - VoIP outbound-call orchestration (VOIP-001, #1056): gate checks (flag/binding) + abuse controls (rate-limit per owner+destination, durable per-agent daily cap), stages a Gemini session intent in Redis keyed by a `call_id` (distinct from the `vs_` VoiceSession id), mints a call-bound WSS ticket, calls Twilio `calls.create(<Connect><Stream>)`, and dispatches the post-call transcript to the **main agent** via `task_execution_service.execute_task(triggered_by="voip")` (default ON). Never calls `connect_and_stream` (cross-worker safety — the WS handler does).

**Channel Adapters (`adapters/`)** — Pluggable external messaging (SLACK-002):

*Core:*
- `base.py` - `ChannelAdapter` ABC, `NormalizedMessage`, `ChannelResponse` models
- `message_router.py` - `ChannelMessageRouter`: rate limiting, agent resolution, execution pipeline; injects MEM-001 per-user memory into `execute_task(system_prompt=…)` gated on `verified_email and not is_group` (#895)

*Slack:*
- `slack_adapter.py` - Slack adapter: DMs, @mentions, thread replies, agent identity via `chat:write.customize`
- `transports/slack_socket.py` - Socket Mode transport: N concurrent WebSockets per `SLACK_SOCKET_CONNECTION_COUNT` env var (default 2, range 1–10), per-client watchdog, envelope-ID dedup ring against possible cross-connection duplicate delivery (#244)
- `transports/slack_webhook.py` - HTTP webhook transport (fallback for production)

*Telegram:*
- `telegram_adapter.py` - Telegram adapter: DMs, group chats (@mention/observe modes), voice transcription, /login flow
- `transports/telegram_webhook.py` - Telegram Bot API webhook (inbound POST + setWebhook registration)

*WhatsApp (via Twilio):*
- `whatsapp_adapter.py` - WhatsApp adapter: DMs via Twilio (WHATSAPP-001); media with SSRF-gated downloads; `/login`/`/logout`/`/whoami` command handlers + markdown→WhatsApp syntax conversion (#467)
- `transports/twilio_webhook.py` - Twilio webhook transport: HMAC-SHA1 signature (via `twilio.request_validator`), MessageSid dedup, form-encoded body

*VoIP Telephony (via Twilio Media Streams) — a voice transport, NOT a text `ChannelAdapter` (VOIP-001, #1056):*
- `transports/twilio_media_stream.py` - Media Streams WS bridge (`handle_media_stream`): `accept()`-then-authenticate — Twilio does NOT forward the `<Stream url>` query string, so the call-bound ticket arrives as a `<Parameter>` in the first `start` frame (`start.customParameters.ticket`), read only after the handshake completes (#1073); a query-string `?ticket=` is still honored as a fallback for non-Twilio/diagnostic clients. Then scope check, `GETDEL` staged intent (consume-once), creates the Gemini `VoiceSession` on the connecting worker, runs the unmodified `connect_and_stream`. Per-connection `_CallBridge`: inbound μ-law→PCM resample, outbound queue + paced 20ms 160-byte μ-law sender, `clear`-on-barge-in, `streamSid` capture, teardown ties Gemini-end→Twilio-close + SETNX-guarded single transcript save (`source="voice"`) + post-call processing dispatch.
- `transports/voip_audio.py` - Pure stdlib-`audioop` codec helpers (`ulaw8k_to_pcm16k`, `pcm24k_to_ulaw8k` direct 3:1, `pop_frames`). Carries per-direction `ratecv` state across chunks (anti-click). `audioop-lts` pinned for Python ≥ 3.13.

*Database:*
- `db/slack_channels.py` - Workspace connections (encrypted bot tokens), channel-agent bindings, active threads
- `db/telegram_channels.py` - Telegram bindings (encrypted bot tokens), group configs, chat links
- `db/whatsapp_channels.py` - WhatsApp (Twilio) bindings (encrypted AuthToken), chat links, verified-email read/write/by-email lookup (#467 Phase 2)
- `db/voip.py` - VoIP `voip_bindings` (encrypted Twilio-voice AuthToken, `from_number`, `inbound_number` [Phase 2], `daily_call_cap`) + `voip_call_logs` lifecycle + durable daily-cap window count (VOIP-001)

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
- `src/views/` - Page components (Dashboard, Agents, Templates, Settings, AgentCollaboration)
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

**Tools** across 17 tool modules (`src/tools/`):

| Module | Tools | Description |
|--------|-------|-------------|
| `agents.ts` (19) | `list_agents`, `get_agent`, `get_agent_info`, `create_agent`, `rename_agent`, `delete_agent`, `start_agent`, `stop_agent`, `list_templates`, `get_credential_status`, `inject_credentials`, `export_credentials`, `import_credentials`, `get_credential_encryption_key`, `get_agent_ssh_access`, `deploy_local_agent`, `initialize_github_sync`, `get_agent_github_pat_status`, `set_agent_github_pat` | Agent lifecycle, credentials, SSH, local deploy, GitHub sync, per-agent PAT (#347) |
| `chat.ts` (3) | `chat_with_agent`, `get_chat_history`, `get_agent_logs` | Chat (enforces sharing rules), history, logs. `chat_with_agent` sync mode applies `MCP_CHAT_TIMEOUT_MS` (default 25000) — on abort the client queries `/api/agents/{name}/executions`, matches the in-flight MCP row, and returns `{status:"queued_timeout", execution_id, message}` so callers poll instead of duplicate-queueing (#914). |
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
| `files.ts` (1) | `share_file` | Outbound file sharing — publish file from `/home/developer/public/` and return download URL (FILES-001) |
| `loops.ts` (3) | `run_agent_loop`, `get_loop_status`, `stop_loop` | Sequential bounded task execution (#740) |
| `memory.ts` (1) | `write_user_memory` | Write per-user memory blob in isolated store; resolves user email server-side from execution_id (MEM-001, #888) |

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
- `/health` - Health check. Beyond `{status}`, returns a richer signal (#1020): `active_tasks` (concurrent executions across `/api/chat` + `/api/task`), `last_task_at` (ISO), `consecutive_failures` (reset on success, incremented on failure — consumed by the dispatch circuit breaker #526 and fleet-health #307), plus the #333 `diagnostics` gauges. `mailbox_depth` is intentionally NOT emitted — there is no agent-side mailbox until the actor model (#945); the backend derives queue depth from `CapacityManager`. Counters live in `agent_server/state.py` (`record_task_start`/`record_task_finish`); the backend reads `consecutive_failures`/`last_task_at` in `monitoring_service.py` (graceful default for pre-#1020 agent images).
- `/api/credentials/update` - Hot-reload credentials
- `/api/chat/session` - Context window stats
- `/api/files` - List workspace files (recursive tree structure)
- `/api/files/download` - Download file content (100MB limit)
- `/api/files/mkdir` - Create a directory (workspace-confined, edit-protected paths rejected) (#37)

**Template-supplied pre-check** (optional, SCHED-COND-001): if the template ships an executable `~/.trinity/pre-check` file, the backend's internal endpoint `POST /api/internal/agents/{name}/pre-check` runs it via `docker exec` before the scheduler fires a cron-triggered chat. The hook is **language-agnostic** — interpreter is selected by the file's shebang line (Python, bash, node, compiled binary, …); Trinity does not invoke `python3` for it. The hook's stdout becomes the chat message; empty stdout + exit 0 records a skipped execution. No HTTP endpoint is exposed on the agent-server for this — the primitive is the same `execute_command_in_container` already used by `services/git_service.py` (persistent-state allowlist), `ssh_service.py`, and the agent terminal.

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


### Background Services

Services that run continuously in the backend process:

| Service | Module | Description |
|---------|--------|-------------|
| **Cleanup Service** | `cleanup_service.py` | Active watchdog reconciliation against agent process registries (orphan recovery, auto-terminate timeouts) + passive stale recovery. Runs every 5 min. Also runs the #772 retention sweeps: nulls `schedule_executions.execution_log` past `execution_log_retention_days` (default 30), DELETEs terminal `schedule_executions` rows past `execution_row_retention_days` (default 90), and DELETEs `agent_health_checks` rows past `health_check_retention_days` (default 7). Also runs the #834 Phase 1a soft-deleted-agent purge: hard-deletes `agent_ownership` rows whose `deleted_at` is older than `agent_soft_delete_retention_days` (default 180, `0` = disabled), cascading child tables via the #816 `purge_agent_ownership`/`cascade_delete` primitive. Also runs the #834 Phase 1b soft-deleted-schedule purge: hard-deletes `agent_schedules` rows whose `deleted_at` is older than `schedule_soft_delete_retention_days` (default 30, `0` = disabled) via `purge_schedule()`, which cascades the row's `schedule_executions` (no #816 chain — schedules have no #816-registered children). Each sweep is capped at 5000 rows/cycle so the first post-deploy backfill spans hours, not minutes; `0` disables a sweep. Triggers `PRAGMA wal_checkpoint(TRUNCATE)` when any sweep reclaims rows. **Startup hook (#740):** one-shot `mark_orphan_loops_interrupted()` flips any `agent_loops` row left in `queued`/`running` after a restart to `interrupted` (`stop_reason="interrupted"`); loops do not auto-resume. (CLEANUP-001, #129, #772, #834, #740) |
| **Operator Queue Sync** | `operator_queue_service.py` | Polls running agents every 5s, reads `~/.trinity/operator-queue.json`, syncs to DB, writes responses back. (OPS-001) |
| **Sync Health Service** | `sync_health_service.py` | Polls git-enabled agents every 60s, upserts `agent_sync_state`, emits `sync_failing` operator-queue entries when consecutive_failures ≥ 3. (#389 S1) |
| **Monitoring Service** | `monitoring_service.py` | Fleet-wide health checks on configurable interval. (MON-001) |
| **Heartbeat Watch Loop** | `heartbeat_service.py` | 5s loop (staggered +10s) that fires a soft, cooldown-debounced operator alert (via the existing `monitoring_alerts` notification path) after 3 consecutive missed agent push-heartbeats — and a recovery notification when beats resume. Reads `seen`-marked agents via a batched Redis pipeline; per-tick miss counters live in Redis. Alerts fire **only on the alive→stale transition** (and recovery only after a prior downgrade), so the operator gets one alert per loss episode. It writes **no** health-check row — the 30s monitoring loop stays authoritative for aggregate status. Soft severity + 3-miss guard keep the silent-fail heartbeat's false positives recoverable. Old-image agents (no `seen` marker) resolve to `unsupported` and are ignored. (RELIABILITY-004, #307) |
| **Scheduler Service** | `scheduler_service.py` | APScheduler-based cron job execution. Async fire-and-forget with DB polling for status. On each cron-triggered fire, optionally invokes the agent's executable `~/.trinity/pre-check` (interpreter chosen by shebang) via the backend's `POST /api/internal/agents/{name}/pre-check` (which `docker exec`s into the agent container). Empty stdout + exit 0 records a skipped execution and does not invoke Claude (SCHED-COND-001, #454). |
| **Capacity Maintenance** | `capacity_manager.py` | Calls `CapacityManager.run_maintenance()` every 60s — expires stale queued tasks (>24h) and drains orphans after restart. Also runs the #526 breaker-aware backstop (`_backstop_open_breaker_backlog`): re-fails the queued backlog for any agent whose dispatch breaker is still open, so a lost inline drain recovers in ~60s rather than waiting out the 24h generic expiry (gated on `DISPATCH_BREAKER_ENABLED`; bounded to agents with queued rows). On each successful sweep, writes a unix-timestamp heartbeat to Redis key `canary:drain_tick_at` (read by canary B-02 to distinguish stuck drains from "drain just hasn't run yet"). (BACKLOG-001 / CAPACITY-CONSOLIDATE #428; B-02 heartbeat #882; #526 backstop) |
| **Audit Retention** | `audit_retention_service.py` | Daily APScheduler job at 04:15 UTC that DELETEs `audit_log` rows past the retention window. Configured via `AUDIT_LOG_RETENTION_DAYS` (default 365, floored at 365 — the `audit_log_no_delete` trigger refuses younger rows). Pruning ages out hash-chain history past the cutoff by design. (#552) |
| **DB Vacuum** | `db_vacuum_service.py` | Daily APScheduler job at 04:30 UTC that runs `VACUUM` on `/data/trinity.db` to reclaim pages freed by the cleanup-service retention sweeps. Configurable via `DB_VACUUM_ENABLED` / `DB_VACUUM_HOUR` / `DB_VACUUM_MINUTE`. Opens an autocommit (`isolation_level=None`) connection because VACUUM cannot run inside a transaction; accepts the rare BUSY outcome rather than retrying. (#772) |
| **Session Cleanup** | `session_cleanup_service.py` | Periodic JSONL reaper for the Session tab. Default 6h cycle (`poll_interval_seconds`); each cycle diffs every running agent's `~/.claude/projects/-home-developer/<uuid>.jsonl` set against `agent_sessions.cached_claude_session_id` and deletes JSONLs not in the keep set whose mtime is older than `min_age_seconds` (default 1h race guard). Synchronous best-effort `reap_jsonl()` is also called by the session router on user-initiated reset/delete so the disk reclaim is immediate. Uses `execute_command_in_container` (no agent-server endpoint required). Also reaps headless-task JSONLs created by long-running headless tasks (timeout > 600s) which auto-enable JSONL persistence so the stdout-race recovery code in `agent_server/services/jsonl_recovery.py` can fire — those UUIDs aren't in `agent_sessions`, so they fall out of the keep set automatically and the existing 1h age guard + 6h sweep removes them. (SESSION_TAB Phase 4.2; #678 JSONL persistence Option B) |
| **Canary Watcher** | `canary_service.py` | Continuous orchestration-invariant harness (CANARY-001 / Issue #411). Every 5 min: `collect_snapshot()` over Redis × SQLite × agent registries, runs deterministic invariant library (S-01, E-02, L-03 in Phase 1), persists violations to `canary_violations`, classifies green→red transitions and fires one Slack webhook POST per transition (`CANARY_SLACK_WEBHOOK_URL` env var; unset = silent sink). Disabled by default; enable on staging/dev with `CANARY_ENABLED=1`. |

The **agent server** also runs a 15-min `auto_sync` heartbeat loop (gated
by `GIT_SYNC_AUTO` env var; default-on for non-source-mode GitHub-template
agents) that stages/commits/pushes in-container changes and writes the
outcome to `.trinity/sync-state.json` — which the Sync Health Service
picks up on its next poll. (#389 S1a)

The agent server additionally runs a 5s **liveness heartbeat loop**
(`agent_server/heartbeat.py`, gated on both `TRINITY_BACKEND_URL` and
`TRINITY_MCP_API_KEY` being present) that POSTs `{memory_mb,
active_executions, uptime_s}` to `POST /api/agents/{name}/heartbeat`,
authenticated with the agent's own `TRINITY_MCP_API_KEY` (Option B,
least-privilege — no master secret injected). `memory_mb` is read from
`/proc/self/status` `VmRSS` (no psutil dep). The loop sleeps-first and
swallows **all** exceptions — a failed beat is silent by design; the
backend's watch loop is what acts on the absence. (RELIABILITY-004, #307)

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

### Agents (33 endpoints)
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
| GET | `/api/agents/{name}/a2a/agent-card` | A2A v1.0 Agent Card for external orchestrator discovery (#737) |
| GET | `/api/agents/{name}/files` | List workspace files (tree structure) |
| GET | `/api/agents/{name}/files/download` | Download file |
| POST | `/api/agents/{name}/files/mkdir` | Create a directory in the workspace (NEW: 2026-05-19, #37) |
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
| PUT | `/api/agents/{name}/timeout` | Set execution timeout (60-7200s, default 3600s = 60min, #665). 400 with `error=agent_timeout_below_active_schedules` if the new cap would drop below any non-deleted schedule's `timeout_seconds` (#929). |
| GET | `/api/agents/{name}/guardrails` | Get per-agent guardrails config (NEW: 2026-04-15) |
| PUT | `/api/agents/{name}/guardrails` | Set per-agent guardrails overrides (GUARD-001) |
| GET | `/api/agents/{name}/file-sharing` | Get outbound file-sharing status + quota (NEW: 2026-04-24, FILES-001) |
| PUT | `/api/agents/{name}/file-sharing` | Enable/disable outbound file sharing (owner-only; returns `restart_required`) |
| POST | `/api/agents/{name}/shared-files` | Mint a download URL for a file in the publish dir (owner/admin or agent-scoped key; used by `share_file` MCP tool) |
| GET | `/api/agents/{name}/shared-files` | List active (non-revoked, non-expired) shared files with download counts |
| DELETE | `/api/agents/{name}/shared-files/{file_id}` | Revoke a shared file (owner-only; idempotent) |
| POST | `/api/agents/{name}/user-memory` | Write per-user memory blob; resolves user email from execution_id server-side (MEM-001, #888) |
| POST | `/api/agents/{name}/heartbeat` | Agent liveness heartbeat (RELIABILITY-004, #307). Auth = the agent's own agent-scoped `TRINITY_MCP_API_KEY` (Option B); 403 unless the key is agent-scoped and its `agent_name` matches the path (user/system/null keys rejected). Validated with `track_usage=False` so a 5s beat doesn't amplify `usage_count`. Best-effort `record_heartbeat`; returns `{ok, stored}`. The five `heartbeat_*` fields surface on `GET /api/monitoring/status` via a single batched Redis read. |
| GET | `/api/agents/{name}/circuit-breaker` | Unified breaker state: `{dispatch:{state,failure_count,retry_after_seconds}, transport:{...}, open:bool, config:{enabled,global_enabled}}` (NEW: 2026-05-30, #526) |
| PUT | `/api/agents/{name}/circuit-breaker` | Enable/disable the per-agent dispatch breaker (owner-only); body `{enabled:bool}`. Global `DISPATCH_BREAKER_ENABLED` must also be on to engage (#526) |
| POST | `/api/agents/{name}/circuit-breaker/reset` | Admin-only; resets BOTH the transport (`agent:circuit:{name}`) and dispatch (`agent:dispatch:{name}`) breakers to closed (#921, extended #526) |

**Note**: Route ordering is critical. `/context-stats` and `/autonomy-status` must be defined BEFORE `/{name}` catch-all route to avoid 404 errors.

### Voice (5 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agents/{name}/voice/start` | Start Gemini Live voice session; accepts `workspace_mode` to enable panel tools |
| POST | `/api/agents/{name}/voice/stop` | Stop active voice session |
| GET | `/api/agents/{name}/voice/prompt` | Get per-agent voice system prompt |
| PUT | `/api/agents/{name}/voice/prompt` | Set per-agent voice system prompt |
| GET | `/api/agents/{name}/voice/{session_id}/panel` | Canvas panel state for workspace mode (ownership-gated; returns empty state when session gone, #699) |

### VoIP Telephony (VOIP-001, #1056 — Phase 1, feature-flag gated default OFF)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/agents/{name}/voip` | Owner | Twilio-voice binding status. 404 when `voip_available` off. |
| PUT | `/api/agents/{name}/voip` | Owner | Configure Twilio voice creds (validated via Twilio Account fetch; AuthToken AES-256-GCM encrypted). |
| DELETE | `/api/agents/{name}/voip` | Owner | Remove the voice binding. |
| POST | `/api/agents/{name}/voip/call` | JWT/MCP (`AuthorizedAgent`) | Place an outbound call. Rate-limited per `(owner, destination)` + durable per-agent daily cap; optional `Idempotency-Key` (Invariant #18). Returns `{call_id, status:"ringing", twilio_call_sid}`. |
| WS | `/api/voip/voice/{call_id}` | Call-bound ticket (`<Parameter>`; `?ticket=` fallback) | Twilio Media Streams audio bridge (no JWT — Twilio can't send one). Ticket arrives via the `start` frame's `customParameters` (Twilio drops the query string — #1073); `scope="voip:{call_id}"`; staged intent consumed once via Redis `GETDEL`. |

MCP tool: `call_user` (`src/mcp-server/src/tools/voip.ts`). The call transcript
is persisted to `chat_messages` (`source="voice"`) and, by default, dispatched
to the main agent for processing via `task_execution_service.execute_task(triggered_by="voip")`.

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
| POST | `/api/agents/{name}/access-requests/{id}/decide` | Approve (auto-shares + fires fire-and-forget approval notification back on the requester's originating channel for telegram/slack/whatsapp, #951) or reject |

### Schedules (13 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents/{name}/schedules` | List schedules |
| POST | `/api/agents/{name}/schedules` | Create schedule. 400 with `error=schedule_timeout_exceeds_agent_cap` if `body.timeout_seconds > agent.execution_timeout_seconds` (#929). |
| GET | `/api/agents/{name}/schedules/{id}` | Get schedule |
| PUT | `/api/agents/{name}/schedules/{id}` | Update schedule. Same 400 when the update touches `timeout_seconds` and the new value exceeds the agent cap (#929). |
| DELETE | `/api/agents/{name}/schedules/{id}` | Delete schedule |
| POST | `/api/agents/{name}/schedules/{id}/enable` | Enable schedule |
| POST | `/api/agents/{name}/schedules/{id}/disable` | Disable schedule |
| POST | `/api/agents/{name}/schedules/{id}/trigger` | Manual trigger |
| GET | `/api/agents/{name}/schedules/{id}/executions` | Execution history |
| GET | `/api/agents/{name}/schedules/{id}/analytics` | Per-schedule analytics — counts, success rate, duration p50/p95/p99, cost total, tool-call top-5 by total duration, daily timeline. `?window_hours=` ∈ {24, 168, 720}, default 168 (#868) |
| POST | `/api/agents/{name}/schedules/{id}/webhook` | Generate/rotate webhook token (WEBHOOK-001) |
| GET | `/api/agents/{name}/schedules/{id}/webhook` | Get webhook status and URL (WEBHOOK-001) |
| DELETE | `/api/agents/{name}/schedules/{id}/webhook` | Revoke webhook token (WEBHOOK-001) |

**Analytics endpoint (#868):** Percentiles computed Python-side via `statistics.quantiles` over the newest 5,000 success rows (`sampled: true, sample_size: 5000` reported back when cap is hit); counts and the daily timeline use the full unsampled rowset. UTC day buckets via `substr(started_at, 1, 10)` then Python gap-fill so chart x-axis is continuous. Tenant boundary lives in the DB layer (`db.schedules.get_schedule_analytics(schedule_id, hours, agent_name=name)`) — `AuthorizedAgent` only validates the path-param agent name, not that `schedule_id` belongs to it. Soft-deleted schedules return 404; the audit/billing surface (cross-trigger per-agent rollup with soft-deleted schedules included) is the deferred #18 endpoint.

### Webhook Triggers (WEBHOOK-001)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/webhooks/{webhook_token}` | Token (URL-embedded) | Trigger schedule execution — no JWT required; rate-limited 10 calls/60s per token via the shared sliding-window limiter `services/rate_limiter.py` (#1023); returns 202 Accepted |

**Token lifecycle:** `POST .../webhook` generates a `secrets.token_urlsafe(32)` token stored in `agent_schedules.webhook_token` (partial unique index for O(1) lookup). Calling `POST .../webhook` again rotates the token, instantly invalidating the old URL. `DELETE .../webhook` nulls the token; subsequent trigger calls return 404.

**Context injection:** Optional `{"context": "..."}` body (max 4000 chars) is appended to the schedule message wrapped in a framing header to reduce prompt injection surface. All triggers are audit-logged with `triggered_by="webhook"`.

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
| GET | `/health` | Health check (unauthenticated, top-level — no `/api/` prefix) |
| GET | `/api/version` | Platform version + build-time git provenance: `git_commit`, `git_commit_short`, `git_commit_subject`, `git_commit_timestamp`, `git_branch`, `build_date`. Sourced from Dockerfile `ARG`/`ENV` wired through `docker-compose.yml` `backend.build.args` + `scripts/deploy/start.sh`. All git fields default to `"unknown"` when build args are absent (#926). |

### Soft-Delete Admin Recovery (#834 Phase 1c)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/admin/soft-deleted/agents` | Admin | List soft-deleted agents (newest first); each row has computed `purge_eta` (null if `agent_soft_delete_retention_days=0`). `limit` capped at 500. |
| POST | `/api/admin/soft-deleted/agents/{name}/recover` | Admin | Clear `agent_ownership.deleted_at`. 404 if not soft-deleted. Metadata-only — container NOT recreated (`needs_container_recreate=true`); operator runs `POST /api/agents/{name}/start`. Audit `agent_lifecycle:recover`. |
| GET | `/api/admin/soft-deleted/schedules` | Admin | List soft-deleted schedules (optional `?agent_name=`); `purge_eta` from `schedule_soft_delete_retention_days`. `limit` capped at 500. |
| POST | `/api/admin/soft-deleted/schedules/{id}/recover` | Admin | Clear `agent_schedules.deleted_at`. 404 if not soft-deleted. Rejoins the scheduler firing list next poll if enabled. Audit `agent_lifecycle:schedule_recover`. |

Recovery is metadata-only (`deleted_at → NULL`); preserved child rows
make the entity immediately usable via the regular
`deleted_at`-filtered read paths. Response models `SoftDeletedAgent` /
`SoftDeletedSchedule` are in `models.py` (Invariant #14).

### Fleet Sync Audit (#390 / S6)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/fleet/sync-audit` | Aggregate per-agent sync state + `duplicate_binding` flag. Admins see all; non-admins see accessible agents. |

The `duplicate_binding` field flags agents whose
`(github_repo, working_branch)` pair is shared with another non-source-mode
agent — detects the §P5 silent-clobber setup at fleet level.


### Executions (EXEC-022, NEW: 2026-05-15)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/executions/stats` | Aggregate stats for the fleet stat cards. `total`, `success_count`, `failed_count`, `total_cost` are windowed by `hours` (0 = all-time); `running_count` and `queued_count` are always live. Optional `agent` filter narrows to one agent. |
| GET | `/api/executions` | Paginated fleet execution list. Filters: `status`, `triggered_by`, `hours`, `agent`, `search`. `limit` (max 200, default 50), `offset`. Returns `FleetExecutionSummary` rows ordered by `started_at DESC`. |

**Access control:** Admin users see all agents. Non-admins see only agents they own or have been shared. Both endpoints share the `accessible_agent_names()` helper from `services/agent_service/helpers.py`.

**Implementation:** `routers/executions.py` → `db/schedules.py:ScheduleOperations.get_fleet_executions` / `get_fleet_execution_stats`. Stats use single-pass conditional aggregation (`CASE WHEN time_cond THEN ...`) so windowed and live counts come from one SQL query. Router registers `/stats` before `""` to prevent FastAPI routing the literal string `"stats"` as an execution ID.

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
| GET | `/api/audit-log/heatmap` | Admin | Day-of-week × hour-of-day activity heatmap (sparse 7×24 grid). Honors `start_time`/`end_time` and optional `event_type`/`actor_type` filters (#941 v3) |
| GET | `/api/audit-log/calendar` | Admin | GitHub-style per-day calendar heatmap (sparse `[{date, count}]`). Same filters as `/heatmap`; complement view — *when* in calendar time vs. the *weekly pattern* from `/heatmap` (#941 v3.1) |
| GET | `/api/audit-log/{event_id}` | Admin | Single entry by UUID |
| GET | `/api/audit-log/distinct/event-types` | Admin | Sorted unique `event_type` values — populates dashboard filter dropdown (#941) |
| GET | `/api/audit-log/distinct/actor-types` | Admin | Sorted unique `actor_type` values — dashboard filter dropdown (#941) |
| GET | `/api/audit-log/export` | Admin | Export time-range entries as `json` or `csv` (Phase 4) |
| POST | `/api/audit-log/verify` | Admin | Verify SHA-256 hash chain over `start_id..end_id` (Phase 4) |
| POST | `/api/audit-log/hash-chain/enable` | Admin | Toggle hash chain computation for new entries (Phase 4) |
| POST | `/api/internal/audit` | Internal secret | Fire-and-forget write path for MCP server tool-call audit (Phase 3) |

**Storage**: append-only `audit_log` table in main SQLite DB. SQLite triggers block UPDATE unconditionally and DELETE within the 365-day retention window.

**Note on `/api/audit`**: the old `/api/audit` router was part of the Process Engine (removed 2026-04-24, #430). The platform audit log at `/api/audit-log` is the only audit surface going forward.

**All phases complete.** Phase 1: infrastructure. Phase 2a: agent lifecycle
audit. Phase 2b: auth, sharing, credentials, settings, rename, request-ID
middleware. Phase 3: MCP tool call audit via transparent wrapper (all 66+
tools, zero per-tool code). Phase 4: hash chain verification, CSV/JSON
export, enable/disable toggle. Issue #20 can be closed.

### Canary Invariant Harness (CANARY-001 — Phase 1, NEW: 2026-05-04)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/canary/violations` | Admin | List violations (filters: invariant_id, severity, tier, start_time, end_time, limit, offset) |
| GET | `/api/canary/violations/stats` | Admin | Aggregate counts by invariant_id and severity |
| GET | `/api/canary/violations/{id}` | Admin | Single violation by row id |
| POST | `/api/canary/run-cycle` | Admin | Run one cycle on demand (delegates to the same `CanaryService.run_cycle()` invoked by the 5-min background loop). Optional body filters which invariants to run. Returns `{snapshot_time, cycle_duration_ms, checks_run, sources_unavailable, violations[], transitions[]}`. Returns 409 with `detail="cycle in progress"` when a background or sibling on-demand cycle is mid-run — empty payload is never silently returned. |

**Storage**: `canary_violations` table in main SQLite DB. JSON-encoded
`observed_state` column carries invariant-specific payload.

**Phase 1 invariants** (#653 — S-01, E-02, L-03):
- **S-01 — Slot–row bijection**: per agent, set of execution_ids in
  `agent:slots:{name}` (Redis ZSET, drain sentinels filtered) equals set
  of execution_ids in `schedule_executions WHERE status='running'`.
  Severity: critical. Catches PR #378/#403 bug class.
- **E-02 — No phantom reversal**: an execution row that was in a
  terminal status in the previous cycle must not appear non-terminal in
  this snapshot. Phase 1 uses Redis-backed state comparison (key
  `canary:e02:terminal_seen`) instead of Vector log diff for simplicity.
  Severity: critical.
- **L-03 — Delete cascades**: no live row in any cross-cutting table
  (agent_sharing, agent_schedules, schedule_executions [non-terminal],
  agent_skills, agent_tags, agent_shared_files, agent_public_links,
  pending operator_queue, pending access_requests, agent-scoped
  mcp_api_keys, active chat_sessions) may reference an `agent_name` not
  in `agent_ownership`; no Redis `agent:slots:{name}` for missing agent.
  Severity: critical for orphaned `schedule_executions` or Redis slots,
  major otherwise. Catches Issue #129 bug class.

**Phase 2 invariants** (#882 — S-02, E-01, E-05, B-01):
- **S-02 — No overbooking**: per agent, `ZCARD(agent:slots:{name})`
  (drain sentinels filtered) ≤ `agent_ownership.max_parallel_tasks`.
  Severity: critical. Catches `acquire_slot` concurrency-bypass
  regressions — distinct from S-01 because the violation can be self-
  consistent (Redis and SQL agree on N+1 running tasks against a cap of N).
- **E-01 — Terminal-state closure**: no `status='running'` row whose
  `started_at` is older than the agent's `execution_timeout_seconds + 300s`
  (matches `SLOT_TTL_BUFFER` so the check fires *after* cleanup has had
  its window to act). Severity: critical. Tier B.
- **E-05 — Dispatched rows have session**: no `status='running'` row
  older than 60s with `claude_session_id IS NULL`. Severity: major.
  Tier B. Guards Issue #106.
- **B-01 — Queue-status coherence**: per agent, `db.get_queued_count`
  (the accessor `BacklogService` calls) agrees with the snapshot's
  independently-collected `len(queued_exec_ids)`. Severity: critical.
  Tier A. Trivially-green today after the #428 consolidation; exists as
  a regression guard against a future cache layer or status-filter drift
  on the production accessor.

**Phase 3 invariants** (#882, same PR — S-03, B-02, R-01):
- **S-03 — Slot TTL ≥ execution timeout**: for every member of
  `agent:slots:{name}`, the companion `agent:slot:{name}:{eid}` HASH
  must have been created with at least
  `execution_timeout_seconds + 300s` of TTL. Three failure kinds
  surfaced explicitly: `missing` (-2, metadata HASH expired ahead of
  the ZSET — the #226 class), `no_expiry` (-1, `expire()` never set),
  `below_floor` (initial TTL under the configured floor). Severity:
  critical. Tier A. **Decay-invariance (#913):** the raw `TTL` of a
  long-lived slot decays linearly from `EXPIRE`, so once #913 makes the
  initial TTL equal the floor exactly, a raw `ttl < floor` check would
  fire by ~1s within the cycle. The Phase 3.1 implementation
  reconstructs the *initial* TTL as `ttl + age` where
  `age = snapshot_time - slot_score` (the ZSET member's score is the
  unix epoch recorded by `SlotService` at ZADD time) and compares
  against `floor - 1` (1s tolerance for Redis float→int wire rounding).
  A real #226-class bug (initial TTL set below the floor) still
  surfaces; natural decay does not.
- **B-02 — No queued without slots-full**: if any agent has
  `len(queued_exec_ids) > 0`, then either `slot_count == max_parallel`
  OR a drain tick fired in the last 60s. Severity: critical. Tier B.
  Heartbeat written by `CapacityManager.run_maintenance()` to
  `canary:drain_tick_at` at the END of each successful sweep, so a
  mid-sweep crash leaves the cursor stale and lets the check catch the
  breakage.
- **R-01 — No zombie Claude processes**: for every running
  `trinity.platform=agent` container,
  `ps -eo stat,comm | grep '^Z.*claude' | wc -l == 0`. Severity:
  critical. Tier A. Guards PR #407. New source type for the canary
  (docker exec); per-container failures recorded in
  `sources_unavailable` so a single unhealthy container doesn't kill
  the cycle. The regex is anchored at `^Z` rather than the catalog's
  ` Z` (leading-space) — procps-ng on the agent base image emits STAT
  left-aligned without padding.

**Fleet**: `config/canary-fleet.yaml` — synthetic load generators
(`canary-fleet-burst`, `canary-fleet-long`) deployed via the existing
systems-deploy API. Without traffic the harness produces trivially-green
checks; the fleet is what gives the watcher something to watch on
staging/dev.

**Architecture**: deterministic library (`src/backend/canary/`) shared
between the 5-min watcher service and the on-demand admin endpoint.
Library reads state but writes nothing; service writes violations and
classifies green→red transitions. **Alert sink**: Slack via incoming
webhook URL configured by `CANARY_SLACK_WEBHOOK_URL` env var (admin-side,
no Settings UI — the canary is staging/dev-only and the operator already
has shell access). Unset = silent sink (cycles still run, violations
still persist). Each transition fires exactly one webhook POST with a
Block Kit payload (header + body + context with "last red Xm ago"
badge). Continuing-red invariants don't re-post. No LLM reasoning
anywhere — the canary's value depends on determinism.

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

### Outbound File Sharing (FILES-001, NEW: 2026-04-24)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/files/{file_id}` | Token (`?sig=`) | Public download. 401 on bad/missing sig, 404 on unknown id, 410 on revoked/expired, `Content-Disposition: attachment; filename="..."`, `X-Content-Type-Options: nosniff`, rate-limited per IP, audit event `file_share_download` |
| POST | `/api/internal/agent-files/share` | `X-Internal-Secret` | Agent-server path — mint a download URL (used by agent-server direct calls, not the MCP tool) |

Storage: `/data/agent-files/{file_id}` under the existing `trinity-data` volume (no compose changes). Agent writes to `/home/developer/public/` (Docker volume `agent-{name}-public`); backend uses Docker SDK `get_archive` to extract the named file on demand — never mounts the agent workspace.

### Sequential Agent Loops (#740, NEW: 2026-05-20)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/agents/{name}/loops` | JWT/MCP | Start a loop; returns `{loop_id, status, agent_name, max_runs}` immediately (202). Body: `message` (template, supports `{{run}}` + `{{previous_response}}`), `max_runs` (1–100, required), `stop_signal`, `delay_seconds`, `timeout_per_run`, `model`, `allowed_tools`. |
| GET | `/api/agents/{name}/loops` | JWT/MCP | List loops for the agent, optional `?status=`, `?limit=` (1–200, default 50). |
| GET | `/api/loops/{loop_id}` | JWT/MCP | Status + per-run summaries + last full response. 404 if unknown; 403 if caller is neither initiator nor agent-accessor. |
| POST | `/api/loops/{loop_id}/stop` | JWT/MCP | Graceful stop. Returns `{status: "stopping" \| "already_done"}`. |

MCP tools: `run_agent_loop`, `get_loop_status`, `stop_loop` (`src/mcp-server/src/tools/loops.ts`). Loop runner lives in `services/loop_service.py`; each iteration dispatches through `task_execution_service.execute_task()` with `triggered_by="loop"` and the parent `loop_id` carried on the resulting `schedule_executions` row.

### Platform Settings (5 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/settings/mcp-url` | Get configured MCP server URL (any auth user) |
| PUT | `/api/settings/mcp-url` | Set MCP server URL (admin-only) |
| DELETE | `/api/settings/mcp-url` | Reset to auto-detect (admin-only) |
| GET | `/api/settings/feature-flags` | Public-safe feature flags for UI gating (any auth user). Exposes `session_tab_enabled` (SESSION_TAB Phase 3), `voice_available` (`VOICE_ENABLED && bool(GEMINI_API_KEY)`, #699), `workspace_available` (`voice_available AND WORKSPACE_ENABLED`, #860 — opt-in, default False), `voip_available` (`VOIP_ENABLED && bool(GEMINI_API_KEY)`, #1056 — opt-in, default False; also requires a per-agent `voip_bindings` row to function), and `enterprise_features` — the list of registered enterprise modules (`EntitlementService.list_entitled_features()`; empty in OSS-only builds or under `TRINITY_OSS_ONLY=1`), which gates every enterprise UI surface in the OSS bundle (#847). |
| GET | `/api/settings/agent-defaults/resources` | Get fleet-wide default CPU/memory for new containers (admin-only, RES-001) |
| PUT | `/api/settings/agent-defaults/resources` | Set fleet-wide default CPU/memory; valid CPU: 1/2/4/8/16; valid memory: 1g–32g (admin-only, RES-001) |

### Session Tab (SESSION_TAB_2026-04, NEW: 2026-05-01)

`--resume`-default chat surface that lives alongside the existing Chat tab. Each turn reattaches to the same Claude Code session via `claude --print --resume <uuid>`, preserving tool-result memory, mid-skill state, and reasoning state across turns.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/agents/{name}/session` | JWT | Create a new session row for the current user. First turn against it is a cold turn (no cached UUID) but writes a JSONL so turn 2 can resume. |
| GET | `/api/agents/{name}/sessions` | JWT | List the caller's sessions on this agent (per-user scoped — owners cannot see other users' sessions, E6). Optional `?status=active`. |
| GET | `/api/agents/{name}/sessions/{id}` | JWT | Session row + most-recent `?limit=N` (default 100, max 500) messages. |
| POST | `/api/agents/{name}/sessions/{id}/message` | JWT | The turn endpoint. Body: `{message, model?, timeout_seconds?}`. Synchronous — returns the assistant message + refreshed session row. Always passes `persist_session=True` to the agent. Resume-failure fallback: if a cached UUID's JSONL is missing, clear the cache, mark the failure, retry once cold. Two Redis primitives gate the turn: (1) per-`(agent, claude_uuid)` resume lock `session_lock:{agent}:{uuid}` (async wait, 30s ceiling, 429 on contention) serialises concurrent `--resume` calls to prevent JSONL corruption (Anthropic #20992) — keyed per-session (`session_lock:cold:{session_id}`) for cold turns (#779); (2) per-session in-flight sentinel `session_inflight:{session_id}` SET for the duration of any turn (cold + warm) drives the `turn_in_progress` field on the GET endpoint so the UI can reattach on KeepAlive activation (#759). Both keys use a dynamic TTL = `db.get_execution_timeout(agent_name) + 30s`, capped at 7230s. |
| POST | `/api/agents/{name}/sessions/{id}/reset` | JWT | Clear `cached_claude_session_id` (next turn cold). Best-effort synchronous JSONL reap. |
| DELETE | `/api/agents/{name}/sessions/{id}` | JWT | Delete the session row + `agent_session_messages`. Best-effort synchronous JSONL reap. |

All endpoints return 404 when `is_session_tab_enabled()` is false. The flag at `system_settings.session_tab_enabled` (or `SESSION_TAB_ENABLED` env) is **default ON since GA 2026-05-04**; settable to false to disable platform-wide. All endpoints enforce per-user ownership and return 404 (not 403) on mismatch to avoid leaking session-id existence.

### Enterprise Modules (#847 seam)

Open-core: enterprise backend code lives in the private `trinity-enterprise`
submodule mounted at `src/backend/enterprise/`. `main.py` conditionally
`register_enterprise(app)` (no-op `ImportError` in OSS-only builds). Each
module calls `entitlement_service.register_module("<id>")`; the registry
drives `GET /api/settings/feature-flags` → `enterprise_features`, which the
OSS Vue bundle reads to show/hide every enterprise surface. `requires_entitlement("<id>")`
(in `dependencies.py`) gates each enterprise endpoint (403 when unentitled;
404 when the submodule is absent and the router was never mounted).
`TRINITY_OSS_ONLY=1` hard-empties the registry.

| Feature id | Module | Surface |
|------------|--------|---------|
| `audit` | (#941) | Entitlement only — flips the OSS audit-log dashboard route visible; `/api/audit-log/*` stay OSS. |
| `user_management` | `enterprise/backend/user_management/` (#995) | Org lifecycle: **invite** (whitelists the email + sends an `EmailService` invite), **deactivate/reactivate** (over the OSS `users.suspended_at` primitive), per-user **activity** view (reads OSS `audit_log`). Endpoints under `/api/enterprise/user-management/*`; UI integrated into Settings → User Management (gated). |
| `siem` | `enterprise/backend/siem/` (#997) | **SIEM log export** — ships OSS `audit_log` to a customer SIEM over an HTTP/JSON webhook. Private `enterprise_siem_config` (destination + AES-encrypted token + export cursor); background daemon pusher (Redis-lock-serialised across workers); at-least-once (cursor advances only on a successful POST). Endpoints under `/api/enterprise/siem/*`. No OSS/UI surface. |

Enterprise tables migrate via the two-track runner (Invariant #3): one file
per migration in each module's `migrations/` package, tracked in
`enterprise_schema_migrations`.

---

## Architectural Invariants

These are structural patterns that must be preserved. Breaking them causes cascading issues.

1. **Three-Layer Backend: Router → Service → DB** — Every feature follows `routers/X.py` → `services/X_service.py` → `db/X.py`. Routers hold no business logic, services hold no SQL, db modules hold no HTTP concerns.

2. **DB Layer: Class-per-domain with Mixin Composition** — Each `db/` file defines an `XOperations` class. Agent-specific settings use mixins (`db/agent_settings/`) composed into `AgentOperations`. New agent settings → new mixin, not a bigger class.

3. **Schema in `db/schema.py`, Migrations in `db/migrations.py`** — All OSS table DDL lives in `schema.py`. Schema changes require a versioned migration in `migrations.py` (tracked in the `schema_migrations` table). Never create tables ad-hoc in service code. **Two-track migrations (open-core):** enterprise modules own only `enterprise_*` tables and migrate them through a **separate** runner (`enterprise/backend/_migrations.py`) tracked in `enterprise_schema_migrations` — never the OSS `schema_migrations`, so the two version-lines can't collide. Enterprise authors one file per migration in the module's `migrations/` package (`NNNN_slug.py` with `NAME` + `upgrade(cursor, conn)`, auto-discovered in filename order). Enterprise migrations may FK-into OSS tables but must **never ALTER** an OSS table — anything OSS must enforce goes through an OSS migration as an edition-agnostic primitive (e.g. `users.suspended_at`, #995). The enterprise runner is invoked from `register_enterprise` *after* OSS `init_database`, so OSS tables already exist.

4. **Router Registration Order Matters** — In `main.py`, static routes like `/api/agents/context-stats` must come before `/{name}` catch-all. New collection-level agent endpoints must be registered before parameterized routes.

5. **Agent Server Mirrors Backend (Subset)** — `docker/base-image/agent_server/routers/` has routers that mirror a subset of backend routers (chat, credentials, files, git, skills, dashboard). The backend proxies to the agent server. Changes to agent-internal APIs must update both sides.

6. **Frontend: Store = Domain, View = Page** — Pinia stores (`stores/agents.js`) are domain-scoped, not view-scoped. Views compose from multiple stores. Composables (`composables/use*.js`) extract reusable logic. API calls go through stores, not views directly.

7. **Single API Client (`api.js`)** — One Axios instance with auth interceptor. Stores call `api.get()`/`api.post()`. No raw `fetch()` or duplicate Axios instances.

8. **Auth Pattern: `Depends(get_current_user)` + `AuthorizedAgent`** — Every authenticated endpoint uses FastAPI `Depends()` for auth. Agent-scoped endpoints use `AuthorizedAgent` or `OwnedAgentByName` for access control. Role-gated endpoints use `require_role("creator")` or `require_admin` (ROLE-001). `internal.py` is the only exception (no auth, for agent-to-backend calls).

9. **Channel Adapter ABC** — External messaging (Slack, Telegram, WhatsApp/Twilio) follows `adapters/base.py` → `ChannelAdapter` ABC with `NormalizedMessage` and `ChannelResponse`. New channels must implement this interface.

10. **WebSocket Events for Real-Time** — All real-time updates go through WebSocket broadcast (`agent_activity`, `agent_collaboration`). Frontend subscribes via `utils/websocket.js`. Don't poll for state that should be pushed. Transport is the Redis Streams event bus in `services/event_bus.py` (RELIABILITY-003, #306) — `ConnectionManager` / `FilteredWebSocketManager` are thin shims that `XADD` to `trinity:events`; the `StreamDispatcher` runs one `XREAD BLOCK` per backend process and fans out to registered clients. New broadcast sites should continue calling the existing `manager.broadcast(...)` / `filtered_manager.broadcast_filtered(...)` API — do not bypass it to publish directly.

11. **Docker as Source of Truth** — Agent container state comes from Docker labels (`trinity.*`), not from an in-memory registry. `docker_service.py` is the single point of Docker interaction.

12. **Credentials: File Injection, Never Stored in DB as Plaintext** — Credentials use `.env` files injected into containers (CRED-002). Encrypted exports use AES-256-GCM (`.credentials.enc`). Redis holds transient secrets. **Exception with mandatory encryption**: channel bot/auth tokens (Slack, Telegram, WhatsApp) and subscription/Nevermined OAuth tokens are persisted in SQLite because they drive long-lived background processes (webhook receivers, scheduled bots) that can't depend on container env vars. These MUST be wrapped in AES-256-GCM JSON envelopes via `services/credential_encryption.py` — plaintext persistence is forbidden. Tables under this rule: `subscription_credentials.encrypted_credentials`, `nevermined_agent_config.encrypted_credentials`, `telegram_bindings.bot_token_encrypted`, `whatsapp_bindings.auth_token_encrypted`, `agent_git_config.github_pat_encrypted`, `slack_workspaces.bot_token` (TEXT column, JSON-envelope content), `slack_link_connections.slack_bot_token` (TEXT column, JSON-envelope content — encrypted by #453, 2026-05-05).

13. **MCP Server = Third Surface in Sync** — The MCP server (`src/mcp-server/src/tools/*.ts`) is a TypeScript proxy over the backend API. When adding a backend endpoint for external access, the MCP tool module needs updating too. Three surfaces must stay in sync: backend router, agent server (if internal), MCP tool (if external).

14. **Pydantic Models Centralized in `models.py`** — Request/response models live in `models.py`, not scattered across routers. Keeps the API contract in one place.

15. **API URL Nesting Convention** — Agent-scoped resources nest under `/api/agents/{name}/...`. Platform-wide resources get top-level prefixes (`/api/executions`, `/api/operator-queue`).

16. **Time-Window SQL uses `iso_cutoff()`, not `datetime('now', ...)`** — Columns written via `utc_now_iso()` are ISO-Z strings (`T` separator, `Z` suffix); SQLite's `datetime('now', ...)` emits a different format (space separator, no suffix), making lexicographic comparison silently incorrect (#476). For rolling-window filters on ISO-Z TEXT columns, compute the cutoff in Python via `iso_cutoff(hours)` from `utils/helpers.py` and pass it as a bound parameter.

17. **Non-root containers** — every Trinity-built image MUST end with a `USER` directive switching to a non-root user. Backend additionally requires `group_add: ${DOCKER_GID:-999}` in compose for Docker socket access on Linux. New service Dockerfiles failing this invariant are rejected at review. Established by #874. CI guards in `.github/workflows/container-security.yml` (path-filtered, runs unconditionally on `docker/**`, `docker-compose*.yml`, `scripts/deploy/start.sh`, `src/mcp-server/Dockerfile` changes — independent of the `ui`-label-gated e2e workflow so backend infra PRs can't silently skip them): `verify-non-root` execs the running backend/scheduler/mcp-server containers (those hold the credentials and the `docker.sock` mount), asserts UID 1000, and proves `group_add` is wired through on Linux by running `docker.from_env().ping()` from inside the backend (NOT a `/api/agents` HTTP probe — `list_all_agents_fast` swallows Docker exceptions and returns `[]`, which made the original gate a false positive); `verify-prod-frontend-uid` builds the prod frontend image out-of-band (start.sh boots the Vite-dev image) and asserts its UID is 101 (`nginxinc/nginx-unprivileged`). Dev-only images (`docker/frontend/Dockerfile`) are intentionally exempt — they have no production attack surface. Existing deployments upgrading through this change must re-own their data path and `agent-configs` volume per [docs/migrations/NON_ROOT_CONTAINERS_2026-05.md](../migrations/NON_ROOT_CONTAINERS_2026-05.md).

18. **Trigger boundaries accept `Idempotency-Key`** (RELIABILITY-006, #525) — every producer boundary that creates an execution accepts an optional `Idempotency-Key` header and routes it through `services/idempotency_service.py` (`begin`/`complete`/`fail`) backed by the `idempotency_keys` table. The same `(scope, key)` within 24h yields one execution; duplicates short-circuit with the original result + `X-Idempotent-Replay: true` (in-flight duplicate → 409). Enforcement lives at the **router** layer, not solely in `TaskExecutionService`, because sync `/chat` runs an inline path and `/api/webhooks/{token}` creates no execution. Wired boundaries: `/chat`, `/task`, `/api/internal/execute-task`, `/api/webhooks/{token}` (auto-derives `(token, body_hash)`), `/api/agents/{name}/fan-out`, and the scheduler (`Idempotency-Key: sched:{execution_id}`) + MCP `chat_with_agent`/`fan_out` (deterministic key over call args). **Any new trigger type must accept an idempotency key before merge** — the dedup layer is fail-open (a key never blocks a real execution), so the cost of adding it is one `begin/complete/fail` triple.

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
    last_login TEXT,
    suspended_at TEXT                   -- #995: NULL = active; set = deactivated
);
```

**User deactivation primitive (#995):** `suspended_at` is an
edition-agnostic primitive. OSS owns the column **and** its enforcement —
`dependencies.get_current_user` rejects any user with `suspended_at` set
on both the JWT and MCP-key paths, so setting it blocks new logins **and**
invalidates live tokens on the next request. `/api/users` exposes it
(read-only). Only the **enterprise** `user_management` module exposes a
way to set/clear it (core-primitive + enterprise-knob, same shape as
#834). OSS-only builds ship the column + enforcement but no setter.

**agent_ownership:**
```sql
CREATE TABLE agent_ownership (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT UNIQUE NOT NULL,
    owner_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    is_system INTEGER DEFAULT 0,
    use_platform_api_key INTEGER DEFAULT 1,
    autonomy_enabled INTEGER DEFAULT 0,
    memory_limit TEXT,
    cpu_limit TEXT,
    full_capabilities INTEGER DEFAULT 0,
    read_only_mode INTEGER DEFAULT 0,
    read_only_config TEXT,
    subscription_id TEXT,
    max_parallel_tasks INTEGER DEFAULT 3,          -- CAPACITY-001
    execution_timeout_seconds INTEGER DEFAULT 3600, -- TIMEOUT-001 (60 min, #665)
    avatar_identity_prompt TEXT,
    avatar_updated_at TEXT,
    is_default_avatar INTEGER DEFAULT 0,
    require_email INTEGER DEFAULT 0,               -- #311
    open_access INTEGER DEFAULT 0,                 -- #311
    max_backlog_depth INTEGER DEFAULT 50,          -- BACKLOG-001
    group_auth_mode TEXT DEFAULT 'none',
    voice_system_prompt TEXT,
    guardrails_config TEXT,
    file_sharing_enabled INTEGER DEFAULT 0,        -- FILES-001
    circuit_breaker_enabled INTEGER DEFAULT 0,     -- RELIABILITY-007 (#526): per-agent dispatch-breaker opt-in (default OFF)
    deleted_at TEXT,                               -- #834 Phase 1a: NULL = live; set = soft-deleted
    FOREIGN KEY (owner_id) REFERENCES users(id),
    FOREIGN KEY (subscription_id) REFERENCES subscription_credentials(id)
);

-- #834 Phase 1a: partial index narrows the retention-sweep scan to
-- actually-deleted rows so it stays cheap as the live agent count grows.
CREATE INDEX idx_agent_ownership_deleted_at
    ON agent_ownership(deleted_at) WHERE deleted_at IS NOT NULL;
```

**Soft-delete (#834 Phase 1a)**: `DELETE /api/agents/{name}` marks
`agent_ownership.deleted_at = NOW` instead of hard-deleting; child rows
are preserved (recoverable until purge). The Cleanup Service hard-purges
rows past `agent_soft_delete_retention_days` (default 180, `0` =
disabled), running the #816 `cascade_delete` primitive at that point to
wipe every per-agent child table. Name reservation
(`is_agent_name_reserved()`) sees soft-deleted rows so a soft-deleted
name cannot be reused before purge. The scheduler's
`list_all_enabled_schedules()` joins `agent_ownership` and filters
`deleted_at IS NULL` so a soft-deleted agent's schedules stop firing
immediately. **Phase 1b** (schedule soft-delete) and **Phase 1c** (admin
recovery endpoints) build on this.

**agent_sharing:** (cross-channel allow-list — same email admits the user on web, Telegram, and Slack)
```sql
CREATE TABLE agent_sharing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    shared_with_email TEXT NOT NULL,
    shared_by_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    allow_proactive INTEGER DEFAULT 0,
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
    channel TEXT NOT NULL,                -- 'web' | 'telegram' | 'slack' | 'whatsapp'
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
- Approval also fires a fire-and-forget proactive notification back to the requester on the originating channel via `proactive_message_service.send_access_grant_notification` — only for `telegram | slack | whatsapp` (web users see the change via the existing `agent_shared` WebSocket event). Notification bypasses the `allow_proactive` opt-in (the user explicitly initiated the request) and the per-recipient rate limit (one-shot, not a campaign). Outcome (`delivered` / `recipient_not_found` / channel error) is audit-logged via `AuditEventType.PROACTIVE_MESSAGE`. Failure to deliver does not roll back the approval. (#951)
- Group chats bypass the gate; agents with both policy flags off retain legacy permissive behavior (backward compatibility).

**mcp_api_keys:**
```sql
CREATE TABLE mcp_api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    key_prefix TEXT NOT NULL,
    key_hash TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    usage_count INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    user_id INTEGER NOT NULL,
    agent_name TEXT,                 -- non-null for agent-scoped keys
    scope TEXT DEFAULT 'user',       -- user | agent | system
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
    timeout_seconds INTEGER,                     -- #913: NULL = inherit agent_ownership.execution_timeout_seconds
    webhook_token TEXT,                          -- WEBHOOK-001: opaque 43-char urlsafe token, nullable
    webhook_enabled INTEGER DEFAULT 0,           -- WEBHOOK-001: 0 = disabled, 1 = active
    deleted_at TEXT,                             -- #834 Phase 1b: NULL = live; set = soft-deleted
    FOREIGN KEY (owner_id) REFERENCES users(id)
);

-- #834 Phase 1b: partial index narrows the schedule retention sweep to
-- soft-deleted rows.
CREATE INDEX idx_agent_schedules_deleted_at
    ON agent_schedules(deleted_at) WHERE deleted_at IS NOT NULL;
```

**Schedule soft-delete (#834 Phase 1b)**: `DELETE
/api/agents/{name}/schedules/{id}` marks `agent_schedules.deleted_at =
NOW` instead of hard-deleting; the row and its `schedule_executions`
are preserved for the retention window. All schedule read paths —
including the cron-firing `list_all_enabled_schedules()` in both the
backend and the standalone scheduler process — filter `deleted_at IS
NULL`, so a soft-deleted schedule stops firing immediately. The Cleanup
Service hard-purges rows past `schedule_soft_delete_retention_days`
(default 30, `0` = disabled); `purge_schedule()` cascades the
`schedule_executions` delete alongside the row (consistent with the
prior hard-delete behavior and with agent-purge `cascade_delete`).
`delete_schedule()` is idempotent on an already-soft-deleted row.

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
    retry_count INTEGER DEFAULT 0,               -- #678: in-line auto-retry count for reader-race recovery
    fan_out_id TEXT,                             -- FANOUT-001: Parent fan-out operation ID
    loop_id TEXT,                                -- #740: Parent agent_loops.id for sequential-loop iterations
    FOREIGN KEY (schedule_id) REFERENCES agent_schedules(id)
);

-- BACKLOG-001: Partial index for cheap atomic FIFO claim
CREATE INDEX idx_executions_queued ON schedule_executions(agent_name, queued_at)
    WHERE status = 'queued';
-- #740: Partial index for joining executions back to their parent loop
CREATE INDEX idx_executions_loop ON schedule_executions(loop_id)
    WHERE loop_id IS NOT NULL;
```

**agent_loops + agent_loop_runs:** (#740 — Sequential agent loops)
```sql
CREATE TABLE agent_loops (
    id TEXT PRIMARY KEY,                         -- 'loop_<urlsafe>'
    agent_name TEXT NOT NULL,
    message_template TEXT NOT NULL,              -- Supports {{run}} and {{previous_response}}
    max_runs INTEGER NOT NULL,                   -- 1–100 hard cap
    stop_signal TEXT,                            -- NULL = fixed mode; set = until mode
    delay_seconds INTEGER NOT NULL DEFAULT 0,
    timeout_per_run INTEGER,                     -- NULL = agent's execution_timeout_seconds
    model TEXT,
    allowed_tools TEXT,                          -- JSON array
    status TEXT NOT NULL,                        -- queued | running | completed | stopped | failed | interrupted
    runs_completed INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,                            -- max_runs_reached | stop_signal_matched | user_stopped | error | interrupted
    last_response TEXT,
    error TEXT,
    started_by_user_id INTEGER,
    started_by_user_email TEXT,
    source_agent_name TEXT,
    source_mcp_key_id TEXT,
    source_mcp_key_name TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX idx_loops_agent ON agent_loops(agent_name);
CREATE INDEX idx_loops_status ON agent_loops(status);
CREATE INDEX idx_loops_user ON agent_loops(started_by_user_id);

CREATE TABLE agent_loop_runs (
    id TEXT PRIMARY KEY,                         -- 'lr_<urlsafe>'
    loop_id TEXT NOT NULL,
    run_number INTEGER NOT NULL,                 -- 1-indexed
    execution_id TEXT,                           -- joins back to schedule_executions
    status TEXT NOT NULL,                        -- running | completed | failed
    response TEXT,                               -- Full response for this iteration
    error TEXT,
    cost REAL,
    duration_ms INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (loop_id) REFERENCES agent_loops(id)
);
CREATE INDEX idx_loop_runs_loop ON agent_loop_runs(loop_id, run_number);
```

**Sequential Agent Loops Features:**
- Loop runner lives in-process as an `asyncio.Task` spawned by
  `services/loop_service.py`. Each iteration calls
  `task_execution_service.execute_task()` with
  `triggered_by="loop"` and the parent `loop_id`; the iteration goes
  through the standard `capacity_manager` admit/slot path so loops
  share the agent's `max_parallel_tasks` budget with other traffic.
- Stop semantics: cooperative. `POST /api/loops/{id}/stop` flips an
  in-process `should_stop` flag; the current iteration finishes
  (sequential, fire-and-disconnect) and the runner exits with
  `stop_reason="user_stopped"`.
- Restart recovery: `cleanup_service` runs `mark_orphan_loops_interrupted()`
  on startup — any leftover `queued`/`running` rows flip to
  `interrupted` with `stop_reason="interrupted"`. Loops do not
  auto-resume.
- Timeline integration: iterations appear as normal `schedule_executions`
  rows tagged with `loop_id` — no dedicated dashboard surface in
  Phase 1.

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

**agent_sessions / agent_session_messages:** (SESSION_TAB_2026-04 — `--resume`-default Session tab, NEW: 2026-05-01)
```sql
CREATE TABLE agent_sessions (
    id TEXT PRIMARY KEY,                           -- urlsafe token
    agent_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_email TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    total_context_used INTEGER DEFAULT 0,
    total_context_max INTEGER DEFAULT 200000,
    status TEXT DEFAULT 'active',                  -- active | archived | reset
    subscription_id TEXT,
    cached_claude_session_id TEXT,                 -- THE primitive — Claude Code UUID for --resume
    last_resume_at TEXT,
    consecutive_resume_failures INTEGER DEFAULT 0, -- drives the resume-fallback path
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX idx_agent_sessions_agent_user ON agent_sessions(agent_name, user_id);
CREATE INDEX idx_agent_sessions_status ON agent_sessions(status);

CREATE TABLE agent_session_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_email TEXT NOT NULL,
    role TEXT NOT NULL,                            -- user | assistant
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    cost REAL,
    context_used INTEGER,
    context_max INTEGER,
    cache_read_tokens INTEGER,                     -- prompt-cache hit observability
    tool_calls TEXT,                               -- JSON
    execution_time_ms INTEGER,
    claude_session_id TEXT,                        -- per-message UUID Claude actually ran under (audit)
    FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX idx_agent_session_messages_session ON agent_session_messages(session_id);
CREATE INDEX idx_agent_session_messages_user ON agent_session_messages(user_id);
```

**Session Tab Features:**
- Strictly parallel to `chat_sessions` / `chat_messages` — no FK between them, no shared state, separate router (`routers/sessions.py`), separate Pinia store (`stores/sessions.js`), separate Vue component (`SessionPanel.vue`).
- `cached_claude_session_id` is the load-bearing field: each turn calls `claude --print --resume <uuid>` so working memory persists.
- `consecutive_resume_failures` drives the fallback path — when a cached UUID's JSONL is missing (Anthropic #39667 / #53417), the router clears the cache, increments the counter, and retries cold once. Reset on the next successful turn.
- `cache_read_tokens` per message: observability for whether Anthropic's prompt cache is engaging across resume turns.
- `claude_session_id` per message: audit history of which Claude UUID each turn ran under (changes on fallback or reset).
- ON DELETE CASCADE on `agent_session_messages` is aspirational (PRAGMA foreign_keys is off platform-wide); `delete_session()` deletes child rows explicitly.
- JSONL files in agent containers (`~/.claude/projects/-home-developer/<uuid>.jsonl`) are reaped by `session_cleanup_service.py` — synchronous best-effort on user-initiated reset/delete, plus a 6h periodic sweep with a 1h race guard.

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

**agent_shared_files:** (FILES-001 — Outbound File Sharing, NEW: 2026-04-24)
```sql
CREATE TABLE agent_shared_files (
    id TEXT PRIMARY KEY,                  -- UUID
    agent_name TEXT NOT NULL,
    filename TEXT NOT NULL,               -- Display name in download
    stored_filename TEXT NOT NULL,        -- UUID filename under /data/agent-files/
    size_bytes INTEGER NOT NULL,
    mime_type TEXT,                       -- python-magic detected
    download_token TEXT UNIQUE NOT NULL,  -- secrets.token_urlsafe(32), 192-bit
    created_by TEXT NOT NULL,             -- Agent name (or user for admin-created)
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,             -- Default 7d
    revoked_at TEXT,                      -- Set when manually revoked
    one_time INTEGER DEFAULT 0,           -- Deferred: one-time link mode (column retained for future)
    consumed_at TEXT,                     -- Deferred
    download_count INTEGER DEFAULT 0,
    last_downloaded_at TEXT,
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
        ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE INDEX idx_agent_files_agent ON agent_shared_files(agent_name);
CREATE INDEX idx_agent_files_token ON agent_shared_files(download_token);
CREATE INDEX idx_agent_files_expires ON agent_shared_files(expires_at) WHERE revoked_at IS NULL;
-- Also: agent_ownership.file_sharing_enabled INTEGER DEFAULT 0
```

**Outbound File Sharing Features:**
- Per-agent opt-in via `agent_ownership.file_sharing_enabled`
- Publish dir is a Docker volume `agent-{name}-public` mounted at `/home/developer/public/` inside the agent
- Backend stores extracted bytes at `/data/agent-files/{file_id}` (under existing `trinity-data` volume — no compose changes)
- Agent extracts via Docker SDK `get_archive` on demand — backend never mounts the agent workspace (filesystem-isolated blast radius)
- Query param is `?sig={token}` (NOT `?download_token=`) to avoid the credential sanitizer's `.*TOKEN.*` pattern redacting it in agent transcripts
- URL format: `{public_chat_url}/api/files/{file_id}?sig={token}` — uses existing `/api/*` proxy rules on Vite dev + prod nginx
- FK has `ON UPDATE CASCADE` + `ON DELETE CASCADE` (aspirational — platform doesn't `PRAGMA foreign_keys=ON`; the agent delete handler + `rename_agent()` manually cascade as is the platform convention)
- Manually cascaded in: `routers/agents.py` delete handler (rows + on-disk files + volume), `db/agent_settings/metadata.py:rename_agent` (updates `agent_name` in 17 tables)

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
    bot_token TEXT NOT NULL,               -- AES-256-GCM JSON envelope of OAuth token
    connected_by TEXT,                     -- User who connected
    connected_at TEXT NOT NULL,
    enabled INTEGER DEFAULT 1
);
```
**Note**: `bot_token` column type is `TEXT` but its contents are an AES-256-GCM JSON envelope (`{"version": 1, "algorithm": "AES-256-GCM", "nonce": "...", "ciphertext": "..."}`). The column was not renamed to `bot_token_encrypted` for backward compatibility with existing rows; the read path in `db/slack_channels.py:_decrypt_token` handles both encrypted and legacy plaintext (`xoxb-*`) values. Plaintext rows are re-encrypted on the next backend restart by the `slack_bot_token_encryption` migration (#453).

**slack_link_connections:** (SLACK-001 - Public Link Slack Integration)
```sql
CREATE TABLE slack_link_connections (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL UNIQUE,          -- FK to agent_public_links
    slack_team_id TEXT NOT NULL UNIQUE,    -- Slack workspace ID
    slack_team_name TEXT,                  -- Workspace display name
    slack_bot_token TEXT NOT NULL,         -- AES-256-GCM JSON envelope of OAuth token
    connected_by TEXT NOT NULL,            -- User who connected
    connected_at TEXT NOT NULL,
    enabled INTEGER DEFAULT 1
);
```
**Note**: One Slack workspace = one public link = one agent (the SLACK-001 model). Coexists with `slack_workspaces` (SLACK-002 multi-agent routing) — different products, different OAuth installations possible. `slack_bot_token` follows the same encrypted-JSON-envelope-in-TEXT pattern as `slack_workspaces.bot_token` (encrypted by #453, 2026-05-05).

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

**whatsapp_bindings:** (WHATSAPP-001 — Twilio WhatsApp integration, NEW: 2026-04-22)
```sql
CREATE TABLE whatsapp_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL UNIQUE,
    account_sid TEXT NOT NULL,                 -- Twilio AccountSid (public)
    auth_token_encrypted TEXT NOT NULL,        -- AES-256-GCM
    from_number TEXT NOT NULL,                 -- 'whatsapp:+E164'
    messaging_service_sid TEXT,                -- optional; preferred over from_number
    display_name TEXT,                         -- friendly_name from Twilio Account fetch
    is_sandbox INTEGER DEFAULT 0,              -- auto-detected from from_number
    webhook_secret TEXT NOT NULL UNIQUE,       -- 32-byte token_urlsafe
    webhook_url TEXT,                          -- computed from public_chat_url
    enabled INTEGER DEFAULT 1,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX idx_whatsapp_bindings_agent ON whatsapp_bindings(agent_name);
CREATE INDEX idx_whatsapp_bindings_webhook ON whatsapp_bindings(webhook_secret);

CREATE TABLE whatsapp_chat_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_id INTEGER NOT NULL REFERENCES whatsapp_bindings(id),
    wa_user_phone TEXT NOT NULL,               -- 'whatsapp:+E164'
    wa_user_name TEXT,                         -- Twilio ProfileName
    session_id TEXT,
    verified_email TEXT,                       -- #311 Phase 2 (shipped up-front)
    verified_at TEXT,
    message_count INTEGER DEFAULT 0,
    last_active TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(binding_id, wa_user_phone)
);
CREATE INDEX idx_whatsapp_chat_links_binding ON whatsapp_chat_links(binding_id);
```

**whatsapp_bindings Features:**
- One Twilio sender per agent; each agent owner brings their own Twilio account (no platform-level Twilio account required)
- AuthToken encrypted at rest via `CredentialEncryptionService` (same pattern as Slack/Telegram)
- Webhook verification: dual-factor (URL `webhook_secret` + HMAC-SHA1 via `twilio.request_validator.RequestValidator`)
- Twilio Sandbox auto-detected from well-known sender `whatsapp:+14155238886`
- Media downloads SSRF-gated to `*.twilio.com` domain suffix
- Phase 1 is DMs only (Twilio's WhatsApp API does not support groups); access control wiring columns (`verified_email`, `verified_at`) shipped up-front so Phase 2 (#311) is additive application-only code

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

**canary_violations:** (CANARY-001 / Issue #411 — Phase 1, NEW: 2026-05-04)
```sql
CREATE TABLE canary_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invariant_id TEXT NOT NULL,            -- 'S-01', 'E-02', 'L-03', ...
    tier TEXT NOT NULL,                    -- 'A' | 'B'
    severity TEXT NOT NULL,                -- 'critical' | 'major' | 'minor'
    snapshot_time TEXT NOT NULL,           -- ISO 8601 UTC
    observed_state TEXT NOT NULL,          -- JSON, invariant-specific
    signal_query TEXT,                     -- the check that fired (debugging aid)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_canary_violations_invariant
    ON canary_violations(invariant_id, snapshot_time DESC);
CREATE INDEX idx_canary_violations_severity
    ON canary_violations(severity, snapshot_time DESC);
CREATE INDEX idx_canary_violations_snapshot
    ON canary_violations(snapshot_time DESC);
```

**canary_violations Features:**
- Append-only in practice (no UPDATE / DELETE in the read API surface).
- One row per fired check per cycle. `observed_state` carries
  invariant-specific JSON (slot diffs, ghost agent names, terminal-status
  reversals).
- Read via `GET /api/canary/violations`; `GET /api/canary/violations/stats`
  drives the dashboard tiles.
- Populated by `services/canary_service.py` on a 5-min loop or on-demand
  via `POST /api/canary/run-cycle`.

**idempotency_keys:** (RELIABILITY-006 / Issue #525 — NEW: 2026-06-02)
```sql
CREATE TABLE idempotency_keys (
    scope TEXT NOT NULL,              -- tenant isolation: "agent:{name}" | "webhook:{token}"
    idempotency_key TEXT NOT NULL,    -- caller-supplied or derived
    execution_id TEXT,               -- nullable (webhook short-circuit has none)
    status TEXT NOT NULL,            -- 'in_flight' | 'completed'
    response_snapshot TEXT,          -- JSON of the original response, for replay
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, idempotency_key)
);
CREATE INDEX idx_idempotency_created ON idempotency_keys(created_at);
```

**idempotency_keys Features:**
- `PRIMARY KEY (scope, idempotency_key)` IS the atomic claim — `claim()` INSERTs
  an `in_flight` row; the loser of a concurrent race catches `IntegrityError`
  and reads the surviving row (cross-process safe across uvicorn workers + the
  standalone scheduler, which share one SQLite file).
- Lifecycle: `claim` → (`attach_execution`) → `complete` (status→`completed`,
  stores `response_snapshot`) or `release` (delete in_flight so a failed first
  attempt can retry; never deletes a `completed` row).
- A row older than the 24h TTL is treated as expired and re-claimed as new.
- Purged on the 24h window by the cleanup service
  (`db.idempotency_purge_expired`, report field `idempotency_keys_purged`).
- DB layer `db/idempotency.py`; orchestration `services/idempotency_service.py`
  (key derivation + `begin`/`complete`/`fail`). See Invariant #18.

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

**Agent Heartbeat (RELIABILITY-004, #307):**
```
agent:heartbeat:{name}        → STRING, 15s TTL (SETEX). JSON {ts, memory_mb, active_executions, uptime_s}
agent:heartbeat:seen:{name}   → STRING "1", no TTL. Backward-compat hinge: absent ⇒ unsupported (never marked dead), present+TTL-key ⇒ alive, present+TTL-gone ⇒ stale
agent:heartbeat:misses:{name} → STRING(int), ~60s TTL. Consecutive-miss counter (watch loop INCR/EXPIRE/DEL); never persisted to SQLite
```
All three keys are deleted by `heartbeat_service.clear_heartbeat(name)`, called best-effort from the agent **delete** handler and from **rename** (old name) — the `seen` marker has no TTL, so without this it would leak one permanent key per agent ever created and orphan the old name on rename. The `hb`/`misses` keys self-expire but are cleared too.

All ops (`SETEX`/`SET`/`GET`/`GETs via pipeline`/`INCR`/`EXPIRE`/`DEL`) are within the backend Redis ACL (`-@dangerous`) and follow the `agent:*` naming convention.

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
| **Creation** | User creates via UI `/settings?tab=mcp-keys` |
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

## Network Topology (Issue #589)

Two Docker bridge networks, by design — agents physically cannot route to Redis.

| Network | Subnet | Members |
|---------|--------|---------|
| `trinity-platform-network` | 172.29.0.0/16 | redis, scheduler, vector |
| `trinity-agent-network` | 172.28.0.0/16 | agents, frontend |

Bridges (members of **both** networks):

- `backend` — primary HTTP API; talks to Redis on platform side, to agents on agent side
- `mcp-server` — agents call `http://mcp-server:8080/mcp` via Docker DNS on the agent network; backend reaches it on platform network
- `otel-collector` — agents push metrics to it
- `cloudflared` (prod only) — proxies to backend (platform) and public agents (agent)

**Rule:** agents are *never* on `trinity-platform-network`. Adding any new
service that mounts the agent network must NOT connect to Redis — full stop.

The agent-creation sites in `services/agent_service/crud.py:583`,
`services/agent_service/lifecycle.py:495`, and
`services/system_agent_service.py:238` hard-code the network name
`trinity-agent-network` — that name is preserved across the split, so no
code changes are required.

**Redis ACL users:**

| User | Auth | Purpose |
|------|------|---------|
| `default` | `REDIS_PASSWORD` | Admin / recovery / ad-hoc ops; `+@all` |
| `backend` | `REDIS_BACKEND_PASSWORD` | Backend container runtime; data ops only, `-@dangerous` |
| `scheduler` | `REDIS_BACKEND_PASSWORD` | Scheduler container runtime; same access pattern as `backend` |

`backend` and `scheduler` cannot run `FLUSHALL`, `CONFIG`, `SHUTDOWN`,
`DEBUG`, `MIGRATE`, `REPLICAOF`, `MONITOR`, or other categories under
`@dangerous`. Both passwords are mandatory in `.env`; `docker compose`
refuses to render without them, and `src/backend/config.py` /
`src/scheduler/config.py` raise on import if `REDIS_URL` lacks
credentials. See `docs/migrations/REDIS_AUTH.md` for the upgrade path.

---

## Container Security

- **Non-root execution** — every Trinity-built container runs as a non-root user:
  backend and scheduler as `trinity` (UID 1000), MCP server as `node` (UID 1000),
  frontend as `nginx` (UID 101), agents as `developer` (UID 1000). Established by
  issue #874. Backend additionally requires `group_add: ${DOCKER_GID:-999}` in
  compose for Docker socket access on Linux hosts.
- `CAP_DROP: ALL` + `CAP_ADD: NET_BIND_SERVICE`
- `security_opt: no-new-privileges:true`
- tmpfs `/tmp` with `noexec,nosuid`
- Isolated network (`172.28.0.0/16` — agents only; Redis lives on the platform network, see "Network Topology" above)
- No external UI port exposure

### Internal API Security (C-003)

Internal endpoints (`/api/internal/`) used by the scheduler and agent containers require shared-secret authentication via `X-Internal-Secret` header. Falls back to `SECRET_KEY` if `INTERNAL_API_SECRET` env var is not set.

### WebSocket Security (C-002, #550)

The `/ws` endpoint uses **single-use opaque tickets** instead of a JWT in the URL. Browser flow:

1. Authenticated client `POST /api/ws/ticket` (JWT in `Authorization` header) → backend mints a 32-byte urlsafe ticket, stores it in Redis with a 30s TTL, and returns it.
2. Client connects to `/ws?ticket=<opaque>`. Backend atomically `GETDEL`s the Redis key (Redis 6.2+) — single-use — resolves it to the authenticated subject, and only then accepts the WebSocket.
3. Reconnects re-mint a fresh ticket; the JWT never enters the WebSocket URL.

This closes the JWT-leak surface flagged by the April 2026 remediation pentest (finding 3.2.1): nginx access logs, browser history, and upstream proxies no longer see the JWT. CSWSH is mitigated because the ticket endpoint requires the JWT in an `Authorization` header — a malicious page can't mint a ticket on the victim's behalf without an explicit cross-origin request, which CORS rejects. Implementation lives in `services/ws_ticket_service.py` + `routers/ws_tickets.py`.

The `/ws/events` endpoint still uses `?token=trinity_mcp_xxx` (MCP API key) for compatibility with documented external scripts (`websocat`, `wscat`); MCP keys are scoped, named, and revocable so the leak surface is bounded relative to a JWT.

`mint_ticket` takes an optional `ttl_seconds` (default 30s, ceiling 600s) used by the VoIP Media Streams socket (VOIP-001, #1056): Twilio cannot send a JWT and a PSTN call's dial+ring exceeds the 30s browser TTL, so the call trigger mints a **call-bound** ticket (`scope="voip:{call_id}"`, 180s) that the WS handler verifies against the URL's `call_id` — binding a single-use ticket to exactly one call.

**Reconnect replay (RELIABILITY-003, #306):** Both `/ws` and `/ws/events` accept an optional `?last-event-id=<stream_id>` query param. The value is regex-gated (`^\d+-\d+$`) by `validate_last_event_id()` in `services/event_bus.py` before reaching `XRANGE`; malformed input is ignored (no catchup). Catchup is capped at `REPLAY_GAP_LIMIT=5000` entries — a larger gap returns `{"type": "resync_required", "reason": "gap_too_large"}` instead of an unbounded `XRANGE`. Authorization (`accessible_agents` for `/ws/events`) is re-applied on replay, not just on live fan-out.

### Frontend XSS Protection (H-005)

All markdown rendering in Vue components uses `DOMPurify` sanitization via `utils/markdown.js`. No direct `v-html` with unsanitized content.

### Rate limiting (#1023)

Request-rate limits use a single shared **sliding-window** limiter,
`services/rate_limiter.py` — a Redis sorted-set rolling window (no fixed-window
boundary burst), fail-open with a bounded per-worker in-process fallback,
cached Redis client. `enforce(key, limit, window)` raises 429 + `Retry-After`.
The webhook trigger (`/api/webhooks/{token}`) is the first adopter; new
request-rate limits should reuse this primitive, not hand-roll Redis counters.

**Not** unified under it: the auth login/OTP limiters (`routers/auth.py`) are
**failure-counters** (increment on failure, reset on success) — a different
pattern, intentionally separate. A global ASGI middleware applying limits to
every route (with a route→policy table) is a tracked follow-up; today the
limiter is applied per-endpoint via `enforce()`.

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

