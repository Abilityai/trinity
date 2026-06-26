# Trinity - Autonomous Agent Orchestration Platform - Architecture

> **Purpose**: Documents the CURRENT system design. Update only when implementing changes.
>
> **Editorial rules**: (1) **One home per feature** — each cross-cutting subsystem is described exactly once, in [Cross-Cutting Subsystems](#cross-cutting-subsystems); every other mention is a pointer. (2) **Catalogs are catalogs** — router/service/endpoint entries are ≤2 lines; deeper behavior lives in the subsystem block or `docs/memory/feature-flows/`. (3) No changelog narration — git history records what was replaced and when; issue tags (`#526`) are kept as lookup keys.

## System Overview

**Trinity** is an **autonomous agent orchestration and infrastructure platform** — sovereign infrastructure for deploying, orchestrating, and governing fleets of autonomous AI agents on your own hardware. Each agent runs as an isolated Docker container with standardized interfaces for credentials, tools, and MCP server integrations.

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

| Layer | Technologies |
|-------|-------------|
| Frontend | Vue.js 3 (Composition API), Vue Flow 1.48.0 (graph visualization), Tailwind CSS 3, Pinia 2, Vite 5 |
| Backend | FastAPI 0.100+, Python 3.11, Docker SDK 7.x, SQLite 3, Redis 7, httpx 0.24+ |
| Agent runtime | Python 3.11, Node.js 20, Go 1.21, Claude Code (latest) |
| Infrastructure | Docker, nginx (prod reverse proxy), Cloudflare Tunnel (public endpoints), Tailscale (private VPN), GCP, Vertex AI Search (docs Q&A) |

---

## Component Details

### Backend (`src/backend/`)

**Core modules:**

| Module | Purpose |
|--------|---------|
| `main.py` | FastAPI app initialization, WebSocket manager, router mounting |
| `config.py` | Centralized configuration constants |
| `models.py` | All Pydantic request/response models (Invariant #14) |
| `dependencies.py` | FastAPI dependencies (auth, token validation, role hierarchy, agent access control) |
| `database.py` | SQLite persistence facade — orchestrates 27 domain operation classes from `db/` |
| `logging_config.py` | Structured JSON logging (captured by Vector); OTel trace ID in log entries for log-trace correlation (RELIABILITY-002) |
| `redis_breaker_util.py` | Shared Redis plumbing (fail-open client, Lua `ScriptCache`, decode helpers) used by both circuit breakers |

**OpenTelemetry tracing** (RELIABILITY-002): auto-instrumentation for FastAPI/httpx/Redis; `traceparent` propagated through inter-agent calls; OTLP/gRPC export to `trinity-otel-collector:4317`; `OTEL_ENABLED=1`, `OTEL_SAMPLE_RATE` (default 10%).

**Routers (`routers/`)** — 63 router modules:

*Core Agent:*
- `agents.py` - Core CRUD, start/stop, logs, stats, queue, activities, terminal (1054 lines)
- `agent_config.py` - Per-agent settings: autonomy, read-only, resources, capabilities, capacity, timeout, api-key
- `agent_files.py` - Files, info, playbooks, permissions, metrics, shared folders, file-sharing toggle + list/revoke (FILES-001)
- `agent_data.py` - Runtime-data export/import (`data_paths`) over the durable home volume (#1169)
- `loops.py` - Sequential agent loops: start/get/stop + agent-scoped list (#740)
- `files.py` - Public download endpoint for outbound agent file sharing (FILES-001)
- `agent_rename.py` - Rename endpoint (RENAME-001)
- `agent_ssh.py` - SSH access endpoint
- `credentials.py` - Credential injection/export/import (CRED-002)
- `chat.py` / `chat/` - Agent chat/activity monitoring
- `internal.py` - Internal endpoints for agent startup, scheduler task execution (no auth; see Container Security)
- `templates.py` - Template listing and GitHub repo fetching
- `sharing.py` - Agent sharing between users
- `git.py` - Git sync endpoints (status, sync, log, pull)

*Auth & Security:*
- `auth.py` - Admin login (username OR registered email + password, #82), email auth, token validation
- `users.py` - User management, roles (ROLE-001); `PUT /me/email` self-service sign-in email (#82 transition)
- `mcp_keys.py` - MCP API key management
- `setup.py` - First-time setup wizard; **required** admin email (sign-in identity) + opt-in hosted intake (trinity-enterprise#38, #82). Setup token removed — no token, no Redis dependency for setup; the unauthenticated first-run window is an operator responsibility (deploy behind a tunnel/VPN until setup completes) (trinity-enterprise#49)

*Scheduling & Execution:*
- `schedules.py` - Schedule CRUD and control
- `executions.py` - Fleet execution list/stats (EXEC-022)
- `analytics.py` - Agent-scoped Overview analytics (#1107)
- `compatibility.py` - Agent compatibility validation: report + auto-fix (#668) — see [Agent Compatibility Validation](#agent-compatibility-validation-668)

*Organization & Tags:*
- `tags.py` - Agent tagging
- `system_views.py` - Saved system views
- `systems.py` - System manifest deployment

*Monitoring & Operations:*
- `monitoring.py` - Fleet health monitoring (MON-001)
- `telemetry.py` - Host telemetry (CPU/memory/disk)
- `activities.py` - Activity timeline
- `agent_dashboard.py` - Agent-defined dashboard (dashboard.yaml)
- `alerts.py` - Cost threshold alerts
- `notifications.py` - Agent notifications
- `operator_queue.py` - Operating Room queue (OPS-001)
- `ops.py` - Operating Room sync service
- `logs.py` - Container log endpoints
- `observability.py` - Observability data
- `audit.py` - Platform audit log (SEC-001)

*Public Access & Monetization:*
- `public_links.py` - Public agent link management
- `public.py` - Public chat endpoints
- `paid.py` - x402 payment-gated chat (NVM-001)
- `nevermined.py` - Nevermined payment config (NVM-001)
- `slack.py` - Slack integration: OAuth, events, multi-agent channel routing, per-agent binding (SLACK-001/002)
- `telegram.py` - Telegram bot integration: webhook receiver, bot binding, group config (TELEGRAM-001)
- `whatsapp.py` - WhatsApp via Twilio: webhook receiver, binding CRUD + test (WHATSAPP-001)
- `voip.py` - VoIP telephony: binding CRUD, outbound call trigger, Media Streams WS entrypoint — see [VoIP](#voip-telephony-voip-001-1056)
- `webhooks.py` - Public webhook trigger endpoint + JWT-auth webhook management (WEBHOOK-001)
- `messages.py` - Proactive agent-to-user messaging (#321)
- `public_memory.py` - Per-user memory write endpoint for channel sessions (MEM-001, #888)

*Subscriptions & Skills:*
- `subscriptions.py` - Subscription management (SUB-002)
- `skills.py` - Skill CRUD and assignment
- `settings.py` - Platform admin settings (incl. Slack transport connect/disconnect/install)

*Content & Files:*
- `image_generation.py` - Image generation REST endpoints (IMG-001)
- `avatar.py` - Agent avatar generation and serving (AVATAR-001)
- `docs.py` - Documentation endpoints

*System:*
- `system_agent.py` - System agent management
- `sessions.py` - Session tab endpoints — see [Session Tab](#session-tab)

**Services (`services/`)** — 66 service modules:

*Core:*
- `docker_service.py` - Docker container management (single point of Docker interaction, Invariant #11)
- `docker_utils.py` - Docker utility helpers
- `template_service.py` - GitHub template cloning and processing
- `agent_client.py` - HTTP client for agent container communication (chat, session, injection); hosts the transport circuit breaker — see [Circuit Breakers](#circuit-breakers-transport--dispatch-526)
- `settings_service.py` - Centralized settings retrieval (API keys, ops config, agent quotas)
- `operator_intake_service.py` - Fire-and-forget, once-per-install opt-in operator intake POST at first-run; owns `installation_id` (trinity-enterprise#38)

*Execution & Scheduling:*
- `task_execution_service.py` - Unified task execution lifecycle: slot mgmt, activity tracking, sanitization (EXEC-024); #678 reader-race auto-retry (see [task-execution-service.md](feature-flows/task-execution-service.md)); records dispatch-breaker outcomes (see [Circuit Breakers](#circuit-breakers-transport--dispatch-526)); hosts `apply_result` + the 202 dispatch path (see [Fire-and-Forget Dispatch](#fire-and-forget-dispatch-1083))
- `capacity_manager.py` - Unified capacity facade for admit/release/status — see [Capacity & Backlog](#capacity--backlog-428)
- `slot_service.py` - Internal to `CapacityManager`: atomic N-ary capacity counter (Redis ZSET, dynamic per-agent TTL) (CAPACITY-001)
- `backlog_service.py` - Internal to `CapacityManager`: persistent SQLite FIFO overflow store with drain-on-release (BACKLOG-001)
- `dispatch_breaker.py` - Per-agent dispatch circuit breaker (RELIABILITY-007, #526) — see [Circuit Breakers](#circuit-breakers-transport--dispatch-526)
- `scheduler_service.py` - APScheduler-based scheduling
- `cleanup_service.py` - Watchdog reconciliation + retention sweeps — see [Soft Delete & Retention](#soft-delete-retention--recovery-834-772)
- `idempotency_service.py` - Trigger-boundary dedup (`begin`/`complete`/`fail`) (RELIABILITY-006, #525; Invariant #18)
- `rate_limiter.py` - Shared sliding-window request limiter (#1023; see Container Security)

*Real-time delivery:*
- `event_bus.py` - Redis Streams transport for WebSocket delivery — see [Real-time Delivery](#real-time-delivery-reliability-003-306)
- `ws_ticket_service.py` - Single-use WebSocket auth tickets (C-002, #550) — see [Real-time Delivery](#real-time-delivery-reliability-003-306)

*Monitoring & Activities:*
- `activity_service.py` - Activity tracking and timeline
- `monitoring_service.py` - Fleet-wide health monitoring, 30s loop — authoritative for aggregate status; lifespan-resumed from persisted `monitoring_config`, default OFF (MON-001, #1121)
- `monitoring_alerts.py` - Alert threshold configuration
- `heartbeat_service.py` - Agent push-heartbeat liveness layer — see [Heartbeat Liveness](#heartbeat-liveness-reliability-004-307)
- `operator_queue_service.py` - Operating Room sync with agent containers (OPS-001)
- `sync_health_service.py` - Git sync health polling — see [Git Sync Health](#git-sync-health-389390)
- `canary_service.py` - Orchestration-invariant watcher — see [Canary Harness](#canary-invariant-harness-canary-001-411)
- `compatibility/` - Agent compatibility validation package (spec/collector/static_checks/ai_checks/fixes) — see [Agent Compatibility Validation](#agent-compatibility-validation-668)

*Auth & Credentials:*
- `credential_encryption.py` - AES-256-GCM encryption for `.credentials.enc` and DB-persisted tokens (CRED-002, Invariant #12)
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
- `agent_shared_files_service.py` - Outbound file sharing — see [Outbound File Sharing](#outbound-file-sharing-files-001)
- `loop_service.py` - Sequential agent loop runner — see [Sequential Agent Loops](#sequential-agent-loops-740-ui-1106)
- `voip_service.py` - VoIP outbound-call orchestration — see [VoIP](#voip-telephony-voip-001-1056)

*Content & Media:*
- `image_generation_service.py` / `image_generation_prompts.py` - Platform image generation via Gemini (IMG-001)

*Skills & System:*
- `skill_service.py` - Skill CRUD and injection
- `system_agent_service.py` - System agent lifecycle
- `system_service.py` - System manifest operations
- `log_archive_service.py` / `archive_storage.py` - Log archival + storage backend
- `session_cleanup_service.py` - Session JSONL reaper — see [Session Tab](#session-tab)
- `db_vacuum_service.py` / `audit_retention_service.py` - Daily maintenance jobs (see Background Services)

**Channel Adapters (`adapters/`)** — pluggable external messaging (SLACK-002, Invariant #9):

- `base.py` - `ChannelAdapter` ABC, `NormalizedMessage`, `ChannelResponse` models
- `message_router.py` - `ChannelMessageRouter`: rate limiting, agent resolution, execution pipeline; injects MEM-001 per-user memory into `execute_task(system_prompt=…)` gated on `verified_email and not is_group` (#895)
- `slack_adapter.py` - DMs, @mentions, thread replies, agent identity via `chat:write.customize`
- `transports/slack_socket.py` - Socket Mode: N concurrent WebSockets per `SLACK_SOCKET_CONNECTION_COUNT` (default 2, range 1–10), per-client watchdog, envelope-ID dedup ring (#244)
- `transports/slack_webhook.py` - HTTP webhook transport (production fallback)
- `telegram_adapter.py` - DMs, group chats (@mention/observe modes), voice transcription, /login flow
- `transports/telegram_webhook.py` - Telegram Bot API webhook (inbound POST + setWebhook registration)
- `whatsapp_adapter.py` - DMs via Twilio (WHATSAPP-001); media downloads SSRF-gated to the `*.twilio.com` domain suffix; `/login`/`/logout`/`/whoami` commands + markdown→WhatsApp syntax conversion (#467)
- `transports/twilio_webhook.py` - Twilio webhook: HMAC-SHA1 signature validation, MessageSid dedup, form-encoded body
- `transports/twilio_media_stream.py` + `transports/voip_audio.py` - VoIP Media Streams bridge (a voice transport, NOT a text `ChannelAdapter`) — see [VoIP](#voip-telephony-voip-001-1056)

Channel DB modules: `db/slack_channels.py` (workspace connections, channel-agent bindings, active threads), `db/telegram_channels.py` (bindings, group configs, chat links), `db/whatsapp_channels.py` (bindings, chat links, verified-email lookup), `db/voip.py` (voice bindings, call logs, daily-cap window). All persisted tokens AES-256-GCM encrypted (Invariant #12).

### Frontend (`src/frontend/`)

**Key directories:** `src/views/` (page components), `src/stores/` (Pinia state), `src/components/` (reusable UI), `src/utils/` (WebSocket client, helpers, `markdown.js` with DOMPurify).

**Stores (domain-scoped, Invariant #6):**
- `stores/agents.js` - Agent CRUD, chat, activity
- `stores/auth.js` - Email/admin authentication + JWT
- `stores/collaborations.js` - Collaboration graph state, WebSocket integration
- `stores/loops.js` - Sequential agent loops UI state, agent-scoped, WebSocket-driven (#1106)
- `stores/executions.js` - Fleet execution list/stats + agent Overview analytics (`fetchAgentAnalytics`, cached per `${name}:${window}`, never polled) (#1107) + per-schedule performance rollups (`fetchSchedulesSummary`, same `${name}:${window}` cache; one fetch shared by the Overview "Schedules performance" section and the Schedules-tab inline stats) (#1115)
- `stores/sessions.js` - Session tab state

**Real-time:** WebSocket client at `utils/websocket.js` with auto-reconnect; tracks `_eid` and replays via `last-event-id` — see [Real-time Delivery](#real-time-delivery-reliability-003-306).

**Top-nav IA — Operations (#1109):** former Health/Ops/Executions nav entries are one **Operations** entry (`views/Operations.vue`, `/operations`) — a `?tab=`-driven view: Needs Response · Notifications · Health · Executions · Resolved. Tab content in embeddable `components/MonitoringPanel.vue` / `ExecutionsPanel.vue`; tabs toggle by `v-if` so store-owned polling tears down on leave. Health tab admin-gated. NavBar carries one unified badge (pending operator-queue + notifications, critical-pulse). Legacy `/monitoring`, `/executions`, `/operating-room`, `/events` redirect (query-preserving) to the matching tab.

**Tab overflow — `components/OverflowTabs.vue` (#1114):** reusable "priority+" tab strip for Agent Detail: a hidden mirror row measures each tab's width plus a worst-case "More" button; the visible row renders what fits and collapses the rest into a "More ▾" menu. Re-measures on resize + `document.fonts.ready`; all-inline before first measure (no first-paint snap). The trigger reflects an overflowed active tab. Keyboard/touch accessible, dark-mode aware; `v-model` over `activeTab` so `?tab=` deep-linking is unaffected.

**Agent Detail Overview tab (#1107):** `components/OverviewPanel.vue` is the default landing tab — owns "trend over the last few days" while the persistent `AgentHeader` owns "now + cost" (no duplicate live gauges). Sections: About lead, needs-attention count + Operations link (hidden at zero), trend charts, health panel (uptime/latency clamped ≤7d by `agent_health_checks` retention), recent-activity drill-in, footprint chips. Charts: `StackedBarChart.vue` (CSS/flexbox) for executions-by-type; `TrendLineChart.vue` (uPlot) for line series. `InfoPanel.vue` leads with About + "What You Can Ask", `template.yaml` metadata behind a `<details>`.

**Collaboration Dashboard** (`views/AgentCollaboration.vue`, `components/AgentNode.vue`, `stores/collaborations.js`): Vue Flow node graph of agent-to-agent communication — draggable status-colored nodes, edges animated 3s on collaboration, real-time activity feed, replay with time-range filtering, localStorage node positions. Detection: the backend chat endpoint accepts `X-Source-Agent` and broadcasts `agent_collaboration` WS events; `activity_service` broadcasts `agent_activity` (`activity_type`: chat_start/chat_end/tool_call/schedule_start/schedule_end/agent_collaboration; `activity_state`: started/completed/failed).

### MCP Server (`src/mcp-server/`)

FastMCP, Streamable HTTP transport, port 8080. API-key auth via `Authorization: Bearer` header; FastMCP `authenticate` callback validates keys against the backend and stores an `McpAuthContext` in session: `{userId, userEmail, keyName, agentName?, scope: "user"|"agent", mcpApiKey}`. Agent-to-agent collaboration uses agent-scoped keys for access control.

**Tools** across 21 tool modules (`src/tools/`):

| Module | Tools | Description |
|--------|-------|-------------|
| `agents.ts` (22) | `list_agents`, `get_agent`, `get_agent_info`, `get_agent_compatibility_report`, `create_agent`, `rename_agent`, `delete_agent`, `start_agent`, `stop_agent`, `list_templates`, `get_credential_status`, `inject_credentials`, `export_credentials`, `import_credentials`, `get_credential_encryption_key`, `get_agent_ssh_access`, `deploy_local_agent`, `initialize_github_sync`, `get_agent_github_pat_status`, `set_agent_github_pat`, `export_agent_data`, `import_agent_data` | Agent lifecycle, credentials, SSH, local deploy, GitHub sync, per-agent PAT (#347), runtime-data export/import (#1169), compatibility report (#668) |
| `chat.ts` (3) | `chat_with_agent`, `get_chat_history`, `get_agent_logs` | Chat (enforces sharing rules), history, logs. Sync mode applies `MCP_CHAT_TIMEOUT_MS` (default 25000); on abort the client queries `/api/agents/{name}/executions`, matches the in-flight MCP row, and returns `{status:"queued_timeout", execution_id, message}` so callers poll instead of duplicate-queueing (#914) |
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
| `files.ts` (1) | `share_file` | Publish file from `/home/developer/public/`, return download URL (FILES-001) |
| `pipelines.ts` (2) | `list_agent_pipelines`, `get_agent_pipeline_state` | Read-only introspection of an agent's self-published pipelines (`~/.trinity/pipelines/*.yaml` + `~/.trinity/pipeline-state/<id>/<instance>.json`) over the **existing** `agent_files` surface — no backend/DB change (Invariant #8). Strict `^[A-Za-z0-9._-]+$` id validation (path-traversal guard), hardened YAML parse (size cap + dup-key + alias guard), latest-instance by mtime, only-404→empty (#919) |
| `loops.ts` (3) | `run_agent_loop`, `get_loop_status`, `stop_loop` | Sequential bounded task execution (#740) |
| `memory.ts` (1) | `write_user_memory` | Per-user memory blob; user email resolved server-side from execution_id (MEM-001, #888) |
| `voip.ts` (1) | `call_user` | Outbound phone call via Twilio Media Streams; server-gated + rate-limited (VOIP-001, #1056) |
| `operator_queue.ts` (3) | `list_operator_queue`, `get_operator_queue_item`, `respond_to_operator_queue` | Read the Operating Room queue (broad or `agent_name`-scoped) and **resolve** a pending item — answer / approve / deny via `POST /{id}/respond`. The respond tool resolves the item's `agent_name`, then applies the same MCP-layer gate before writing (non-`pending` → structured error). Agent-scoped keys gated to `{self} ∪ permitted`. `cancel` deferred. (OPS-001, #1101 read / #1104 respond) |

### Vector Log Aggregator (`config/vector.yaml`)

Vector 0.43.1 (`timberio/vector:0.43.1-alpine`). Captures all container stdout/stderr via Docker socket; routes platform logs to `/data/logs/platform.json` and agent logs to `/data/logs/agents.json`; enriches with container metadata; parses JSON logs. Health: `http://localhost:8686/health`. Query: `docker exec trinity-vector sh -c "tail -50 /data/logs/platform.json" | jq .` (same for `agents.json`).

### Agent Containers

**Base image** `trinity-agent-base:latest`: Python 3.11, Node.js 20, Go 1.21, Claude Code (latest), common Python packages.

**Internal server** `agent-server.py` (FastAPI, port 8000):
- `/api/chat` - Claude Code execution (messages persisted to database)
- `/health` - Health check. Returns `{status}` plus `active_tasks` (concurrent executions across `/api/chat` + `/api/task`), `last_task_at`, `consecutive_failures` (reset on success — consumed by the dispatch breaker #526 and fleet health #307) and the #333 `diagnostics` gauges (#1020). `mailbox_depth` intentionally NOT emitted — no agent-side mailbox until the actor model (#945); the backend derives queue depth from `CapacityManager`. Counters live in `agent_server/state.py`; backend reads them in `monitoring_service.py` with graceful defaults for older images.
- `/api/credentials/update` - Hot-reload credentials (rewrites `.env`/`.mcp.json`)
- `/api/credentials/reload-token` - Surgical subscription-token hot-reload (#1089): mutates the agent-server process `os.environ["CLAUDE_CODE_OAUTH_TOKEN"]` so the NEXT claude subprocess uses the rotated token while in-flight subprocesses keep theirs; persists to the writable-layer override `/var/lib/trinity/oauth-token` (0600). Does NOT touch `.env`/`.mcp.json`. See [Subscription Token Rotation](#subscription-token-rotation-via-hot-reload-1089)
- `/api/chat/session` - Context window stats
- `/api/files`, `/api/files/download` (100MB limit), `/api/files/mkdir` (workspace-confined, #37)

The agent server also runs two loops: the 15-min git `auto_sync` heartbeat (see [Git Sync Health](#git-sync-health-389390)) and the 5s liveness heartbeat (see [Heartbeat Liveness](#heartbeat-liveness-reliability-004-307)).

**Durable subscription-token override (#1089):** `startup.sh` exports `CLAUDE_CODE_OAUTH_TOKEN` from `/var/lib/trinity/oauth-token` (when present, non-empty) **before** launching the agent server, so a token rotated via hot-reload survives a plain stop+start (a fleet restart via `routers/ops.py` does a raw `container_stop`+`container_start` that bypasses `start_agent_internal` and would otherwise revert to the baked `Config.Env` token). The path is deliberately on the writable layer, **not** under the persisted `/home/developer` volume: it survives `stop`→`start` (same container) but is wiped on recreate (fresh layer), so a DB-driven recreate cleanly re-bakes `Config.Env` from the DB and the stale override is gone — self-reconciling, no marker logic. Dir created+chowned to UID 1000 in the base-image Dockerfile.

**Template-supplied pre-check** (SCHED-COND-001, #454): if the template ships an executable `~/.trinity/pre-check`, the backend's internal endpoint `POST /api/internal/agents/{name}/pre-check` runs it via `docker exec` before a cron-triggered chat. Language-agnostic — interpreter selected by shebang. The hook's stdout becomes the chat message; empty stdout + exit 0 records a skipped execution (Claude never invoked). Uses the same `execute_command_in_container` primitive as `git_service.py`, `ssh_service.py`, and the agent terminal — no agent-server HTTP endpoint.

**Persistent chat:** all chat messages auto-saved to SQLite (`chat_sessions`, `chat_messages`) with full observability (costs, context, tool calls, execution time); sessions survive container restarts/deletions; users see only their own messages (admins see all).

**File structure:**
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
| **Cleanup Service** | `cleanup_service.py` | Every 5 min: active watchdog reconciliation against agent process registries (orphan recovery, auto-terminate timeouts) + passive stale recovery (CLEANUP-001, #129). Also runs retention + soft-delete purge sweeps and the #740 startup orphan-loop hook — see [Soft Delete & Retention](#soft-delete-retention--recovery-834-772) |
| **Operator Queue Sync** | `operator_queue_service.py` | Polls running agents every 5s, reads `~/.trinity/operator-queue.json`, syncs to DB, writes responses back (OPS-001) |
| **Sync Health Service** | `sync_health_service.py` | Polls git-enabled agents every 60s — see [Git Sync Health](#git-sync-health-389390) |
| **Monitoring Service** | `monitoring_service.py` | Fleet-wide health checks on configurable interval (30s default); authoritative for aggregate status. **Lifespan-resumed (#1121):** boot reads the persisted `monitoring_config` (staggered +12s) and starts the loop only when `enabled` — the flag is the single source of truth, **defaults OFF**, persisted by `enable`/`disable`/`PUT /config` (which also reconcile the running loop) so the choice survives restarts; `*_check_interval` rejects non-positive values (422), loop clamps sleep ≥1s (MON-001) |
| **Heartbeat Watch Loop** | `heartbeat_service.py` | 5s loop acting on missed agent heartbeats — see [Heartbeat Liveness](#heartbeat-liveness-reliability-004-307) |
| **Scheduler Service** | `scheduler_service.py` | APScheduler cron execution; async fire-and-forget with DB polling for status. On each cron fire, optionally invokes the agent's `~/.trinity/pre-check` (see Agent Containers) |
| **Capacity Maintenance** | `capacity_manager.py` | `run_maintenance()` every 60s — see [Capacity & Backlog](#capacity--backlog-428) |
| **Audit Retention** | `audit_retention_service.py` | Daily 04:15 UTC: DELETEs `audit_log` rows past retention. `AUDIT_LOG_RETENTION_DAYS` (default 365, floored at 365 — the `audit_log_no_delete` trigger refuses younger rows). Pruning ages out hash-chain history past the cutoff by design (#552) |
| **DB Vacuum** | `db_vacuum_service.py` | Daily 04:30 UTC: `VACUUM` on `/data/trinity.db` to reclaim pages freed by retention sweeps. `DB_VACUUM_ENABLED`/`DB_VACUUM_HOUR`/`DB_VACUUM_MINUTE`. Autocommit connection (VACUUM can't run in a transaction); accepts rare BUSY rather than retrying (#772) |
| **Session Cleanup** | `session_cleanup_service.py` | Periodic JSONL reaper — see [Session Tab](#session-tab) |
| **Canary Watcher** | `canary_service.py` | 5-min invariant harness cycle — see [Canary Harness](#canary-invariant-harness-canary-001-411) |

---

## Cross-Cutting Subsystems

Canonical home for each multi-component feature. Endpoint signatures live in [API Endpoints](#api-endpoints); table DDL in [Database Schema](#database-schema).

### Agent Runtimes — multi-runtime / "harness == runtime" (#1187)

A Trinity **harness IS an `AgentRuntime`** — the pluggable execution engine inside the agent container. Three ship today: **Claude Code** (default), **Gemini CLI**, and **OpenAI Codex** (#1187). `AGENT_RUNTIME` (container env, set from `template.yaml runtime:` via `crud.py`; also a `trinity.agent-runtime` label) selects one; `runtime_adapter.get_runtime()` is the factory — it **validates** the value against `KNOWN_RUNTIMES` and raises on an unknown one rather than silently defaulting to Claude.

**ABC** (`agent_server/services/runtime_adapter.py`): `execute` (chat), `execute_headless` (stateless task), `configure_mcp`, `is_available`, `get_default_model`, `get_context_window`, plus a non-abstract `capabilities()` returning a `RuntimeCapabilities` dataclass (`chat_continuity`, `session_tab_resume`, `mcp_support`, `cost_reporting: "native"|"estimated"`) — conservative by default (an un-overridden runtime is least-capable). Each runtime is a singleton (`get_<name>_runtime()`).

**Codex** (`codex_runtime.py`, built independently on the per-runtime primitives — NOT a shared helper, so it never inherits Gemini's blanket `kill_cgroup_orphans()`): `codex exec --json` → JSONL events (`thread.started`→session id, `turn.completed.usage`→tokens with `reasoning_output_tokens` ⊂ `output_tokens`, `item.completed`→activity, `turn.failed`/`error`); `-o/--output-last-message` is the authoritative result (read-then-delete in `finally`); `codex exec resume <thread_id>` for continuity; cost estimated via `CODEX_PRICING`. Concurrency-safe orphan cleanup via `_drain_bounded` (`kill_cgroup_orphans(extra_pids=…)` preserves siblings). Error→HTTP: auth→503, rate→429, runtime-unavailable→**500** (avoids the AUTH collision), pipe-drop→**502** (SUB-003 guard).

**Parity surface** — every runtime must wire these (Codex specifics in [codex-runtime.md](feature-flows/codex-runtime.md); contract in the [Harness Authoring Guide](harness-authoring-guide.md)): platform **system prompt**, **sandbox** (`_resolve_sandbox_mode`: normal → `--sandbox danger-full-access` since Codex's bubblewrap can't namespace inside the hardened container; read-only → `--sandbox read-only` from `~/.trinity/read-only-config.json` — fail-closed Codex read-only is a fast-follow), **guardrails** (`_load_guardrails()`; unmapped Claude tool-names logged, not dropped), **credential sanitization** (`utils/credential_sanitizer`). The **Session tab** is gated off for runtimes lacking `session_tab_resume` (backend constant `RUNTIMES_WITHOUT_SESSION_TAB_RESUME` in `sessions.py` → stateless turn; frontend hides the tab). The platform prompt is **runtime-aware** (`platform_prompt_service.get_platform_system_prompt(runtime=…)`/`compose_system_prompt(runtime=…)`, threaded from `routers/chat.py` + `task_execution_service.py` via the `trinity.agent-runtime` label): for Codex it strips the Claude-only `mcp__trinity__` prefix (else `unknown MCP server`) and uses bare `trinity` tool names. Backend reads nothing runtime-specific in MVP (infers AUTH from HTTP 503; `ExecutionMetadata.status`/`error_code` unused — fast-follow). Codex agents skip Claude-subscription auto-assign (`is_claude_runtime`).

### Capacity & Backlog (#428)

`CapacityManager` (CAPACITY-CONSOLIDATE) is the single public API for admit/release/status across `/chat` (`max_concurrent=max_parallel_tasks`, `queue_in_memory`) and `/task` (`queue_persistent`). It composes two private internals — `slot_service.py` (atomic N-ary counter, Redis ZSET `agent:slots:{name}`, dynamic per-agent TTL) and `backlog_service.py` (SQLite FIFO over `schedule_executions.status='queued'`, drain-on-release) — and owns the in-memory overflow store (Redis LIST, depth 3). See [capacity-management.md](feature-flows/capacity-management.md).

`run_maintenance()` every 60s: expires stale queued tasks (>24h), drains orphans after restart, runs the #526 breaker-aware backstop, and on each successful sweep writes a unix-timestamp heartbeat to Redis `canary:drain_tick_at` (read by canary B-02; written at sweep END so a mid-sweep crash leaves the cursor stale and trips the check).

**Status-as-projection (#1082):** `schedule_executions.status` is a CAS-guarded *projection* of an execution's terminal event — the agent process registry is the runtime authority for "is running"; no backend reader treats `status='running'` as standalone authority (cleanup-watchdog readers use it as a candidate filter, then confirm against the registry/Redis before any destructive write). In `db/schedules.py` every `update(schedule_executions)` writing `status` carries a status precondition in its `WHERE` (incl. `update_execution_to_queued`'s `AND status == RUNNING` guard, closing the E-02 phantom-reversal gap); kept by `tests/unit/test_schedule_status_observability.py`. **Not yet covered (#1082 follow-up):** the standalone scheduler (`src/scheduler/`) writes the same DB with raw-SQL, non-CAS status writers — a late backend `SUCCESS` can still be clobbered on the retry-failure path. See [status-as-projection.md](feature-flows/status-as-projection.md).

### Circuit Breakers (transport + dispatch, #526)

Two independent per-agent breakers, separate Redis namespaces and separate Lua, so they never contaminate each other's counters. Both reuse the `CircuitState` Lua pattern and the shared `redis_breaker_util.py` plumbing; both fail open on Redis down.

**Transport breaker** (`agent_client.py`, key `agent:circuit:{name}`, #631): exponential backoff + dormant state. Only TCP/connection failures count — HTTP 4xx/5xx (incl. 502/503/504) are application errors and skip the failure counter (#474).

**Dispatch breaker** (`dispatch_breaker.py`, key `agent:dispatch:{name}`, RELIABILITY-007): producer-side, fed *only* by execution outcomes in `task_execution_service` — counts **AUTH only** (`error_code == AUTH`, agent answers HTTP 503), NOT TIMEOUT/AGENT_ERROR (D10). Consecutive-failure machine `closed → open → half-open(probe) → closed`; default threshold 3, base cooldown 30s, exponential backoff (D9). `record_outcome(error_code)` returns the `(prior, new)` transition; the **caller** backgrounds the drain on `→open` (no `capacity`/`db` import in the breaker → no circular dep, D3). Never raises. `record_failure("missed_heartbeat")` is the #307 seam. `record_success` is a no-op write (Lua early-return) when already closed with zero failures, so healthy agents don't churn Redis. Gating: per-agent `circuit_breaker_enabled` (default OFF) AND global `DISPATCH_BREAKER_ENABLED` must both be on.

**Execution-path flow** (details in [dispatch-circuit-breaker.md](feature-flows/dispatch-circuit-breaker.md)):
- `CapacityManager.acquire(...)` gates the breaker at the TOP (before overflow). A deny raises `CircuitOpen` before any slot/overflow work — a doomed task is never enqueued (**no-enqueue invariant**, D2). A half-open **probe** is admitted ONLY into a free slot (full → fast-fail, never a verdict-less backlog row that stalls backoff, F1).
- `task_execution_service` (single execution path) records every outcome: `record_outcome(None)` at success (resets), `record_outcome(AUTH)` at the HTTP-error terminal (counts). On `→open` it backgrounds `_fail_backlog_and_audit` via `_spawn_bg` (`db.fail_queued_for_agent` → FAILED + clear queue + audit); catches `CircuitOpen` → `TaskExecutionResult(CIRCUIT_OPEN)` + FAILED row. The step-3b pre-dispatch check fast-fails on `state == "open"` only on the backlog-drain path (`slot_already_held and not dispatch_gate_checked`), never blocking an already-admitted probe.
- **Backstop**: if the inline drain is lost, the 60s `run_maintenance` sweep (`_backstop_open_breaker_backlog`) re-fails queued backlog for any still-open breaker (~60s, not the 24h generic expiry).

API: `GET`/`PUT /api/agents/{name}/circuit-breaker` (owner-only toggle), `POST .../circuit-breaker/reset` (admin-only; resets BOTH breakers) — see API Endpoints.

### Fire-and-Forget Dispatch (#1083)

Removes backend-thread pinning for autonomous turns by construction: an eligible turn is dispatched with a **202 ACK** and runs in the agent's background, then POSTs its terminal back to a callback that finalizes the row. `execute_task` returns right after the ACK, so a wedged turn holds **zero** backend coroutine and the slot becomes a **lease** (released by the callback or reclaimed by the existing TTL reaper). Flag-gated `DISPATCH_ASYNC` (default OFF) AND Claude-runtime only (decision: the typed terminal envelope is Claude-specific); non-202 responses (old image / non-Claude / flag off) fall through to today's **synchronous** handling — the safe mixed-fleet fallback. v1 eligible triggers: **`{schedule, webhook}`** only (the triggers reaching `execute_task` with no synchronous `result.response` consumer; `loop`/`fan_out` stay sync, `event` bypasses `execute_task`).

- **Terminal applier** (`task_execution_service.apply_result`): the single point that finalizes an execution — shared by the inline sync path and the callback. Derives every persisted field from a normalized `TerminalEnvelope` and **gates ALL side-effects on the CAS bool** (`db.update_execution_status` → bool): a CAS-lost write (replay / late callback) completes no activity, records no breaker outcome, and releases no slot. `slot_service.release_slot` also gates the BACKLOG-001 drain on the ZREM result, so a replayed release can't admit past `max_parallel_tasks`.
- **Durable async marker**: `mark_execution_dispatched(async_dispatch=True)` writes `claude_session_id='dispatched_async'` (both sentinels non-NULL, so the no-session sweep / E-05 treat them identically). The callback finalizes **only** RUNNING rows carrying it (fail-closed cross-path guard — never terminal-writes a sync/interactive execution mid-await).
- **Callback endpoint** (`routers/agents.py`, `POST /api/agents/{name}/executions/{id}/result`): agent's own MCP key (mirrors `authorize_heartbeat`) + ownership (404) + marker gate (409) + idempotent replay (an **authoritative** terminal — SUCCESS/CANCELLED/SKIPPED — short-circuits `{replayed:true}`; a FAILED row falls through so a late SUCCESS can still overwrite a reaper `LEASE_EXPIRED` via the CAS, Codex #2) + body-size 413 caps. On accept → `apply_result(..., release_slot=True)`, closing the activity via the filtered `get_open_activity_id_for_execution` (chat/schedule_start + `started`, never a shared-eid tool_call row).
- **Agent side** (`agent_server/services/result_callback.py`): `try_spawn_async` gates on async + Claude + execution_id + callback creds; `_run_and_report` runs the headless turn, builds the typed envelope (success → `completed`; HTTPException → status-mapped `error_code`/`terminal_reason`, metadata salvaged from the structured 502 body), **persists** it to `~/.trinity/pending-results/<eid>.json`, and delivers with capped backoff up to the lease deadline (dispatch + `timeout + SLOT_TTL_BUFFER`), deleting on a 2xx / permanent 4xx. A strong-ref `_inflight` set defeats the asyncio GC footgun; a **startup sweep** (`main.py`) re-sends leftover envelopes so a crash/restart mid-callback doesn't lose completed work (a late SUCCESS still overwrites a reaper `LEASE_EXPIRED` via CAS).
- **Lease reaper** (`cleanup_service`): an expired lease (no callback before the slot TTL) FAILs the row with the `lease_expired` tag (`TaskExecutionErrorCode.LEASE_EXPIRED`) and closes its open activity. The stale-execution sweep uses each agent's `timeout + SLOT_TTL_BUFFER` window (not the flat 120-min default) so a legitimately-running max-timeout async turn isn't failed early.

**v1 boundaries**: lease-expiry = FAIL (not re-queue); async empty-result = FAIL (the #678 inline auto-retry stays sync-only); SUB-003 auto-switch stays inline-only (the breaker still protects the fleet); 504/503 async failures write a null-cost row until `execute_headless_task` exposes `ctx.metadata` on those paths (T8 / #1201 fast-follow).

### Heartbeat Liveness (RELIABILITY-004, #307)

Additive push-heartbeat layer; the 30s `monitoring_service` loop (lifespan-resumed, default-off, #1121) stays authoritative for aggregate status when enabled.

**Agent side** (`agent_server/heartbeat.py`): 5s loop, gated on `TRINITY_BACKEND_URL` + `TRINITY_MCP_API_KEY`. POSTs `{memory_mb, active_executions, uptime_s}` to `POST /api/agents/{name}/heartbeat`, authenticated with the agent's own agent-scoped MCP key (least privilege, no master secret). `memory_mb` from `/proc/self/status` VmRSS (no psutil). Sleeps-first and swallows **all** exceptions — a failed beat is silent by design; the backend watch loop acts on absence.

**Backend side** (`heartbeat_service.py`): owns all Redis heartbeat keys — `record_heartbeat` (SETEX 15s + persistent `seen` marker), `read_heartbeat`, `heartbeat_status`/`heartbeat_status_bulk` (one pipelined round-trip, D4), `authorize_heartbeat` (403 unless the key is agent-scoped and its `agent_name` matches the path; user/system/null rejected; validated `track_usage=False`). Keys:

```
agent:heartbeat:{name}        → STRING, 15s TTL. JSON {ts, memory_mb, active_executions, uptime_s}
agent:heartbeat:seen:{name}   → STRING "1", no TTL. Absent ⇒ unsupported (old image, never marked dead);
                                present + TTL-key alive ⇒ alive; present + TTL-key gone ⇒ stale
agent:heartbeat:misses:{name} → STRING(int), ~60s TTL. Consecutive-miss counter; never persisted to SQLite
```

**Watch loop**: 5s (staggered +10s), batched Redis pipeline over `seen`-marked agents, 3-miss guard. Fires a soft, cooldown-debounced operator alert (`monitoring_alerts` path) **only on the alive→stale transition**, plus a recovery notification when beats resume after a prior downgrade — one alert per loss episode. Writes no health-check rows. `clear_heartbeat(name)` deletes all three keys, best-effort on agent delete and rename (old name) — `seen` has no TTL, so otherwise it leaks one permanent key per agent. The five `heartbeat_*` fields surface on `GET /api/monitoring/status` via one batched Redis read.

### Idempotency (RELIABILITY-006, #525)

Trigger-boundary dedup — policy in Architectural Invariant #18, table DDL under `idempotency_keys`, details in [idempotency-keys.md](feature-flows/idempotency-keys.md). `services/idempotency_service.py` (`begin`/`complete`/`fail`) over `db/idempotency.py`. The `(scope, key)` PRIMARY KEY is the atomic claim: `claim()` INSERTs an `in_flight` row; a concurrent loser catches `IntegrityError` and reads the surviving row (cross-process safe over the shared SQLite file). Lifecycle: `claim` → (`attach_execution`) → `complete` (stores `response_snapshot` for replay) or `release` (deletes the in_flight row so retry is possible; never deletes a `completed` row). Rows >24h expire and re-claim (cleanup purges via `idempotency_purge_expired`). Duplicates within 24h short-circuit with the original result + `X-Idempotent-Replay: true`; an in-flight duplicate returns 409. Fail-open.

**Effect-scoped extension (#1084):** trigger-boundary dedup stops a re-POSTed `/chat`/webhook from creating a *second execution*; it does NOT reach an agent's individual outbound tool calls. So a re-delivered turn (the at-least-once semantics pull-mode / work-stealing will introduce, Epic #1045/#1081) re-emits the same side effect (re-sends a message, re-charges a payment). The same `idempotency_service` adds a per-sink guard — `effect_guard(effect_type, identifying_args, *, execution_id, agent_name, dedup_label, payment_request_id)` — enforced at the SINK, per resolved action identity. Scopes: `effect:{execution_id}` for messages/voip/share_file (after `resolve_and_validate_execution` confirms the execution belongs to the agent — generalizing MEM-001), `payment:{agent_request_id}` for Nevermined settles (its native exactly-once token). Key = `{effect_type}:sha256(execution_id ∥ effect_type ∥ resolved_identifying_args ∥ dedup_label)` on **resolved, immutable** identity only (recipient/channel/account) — **never the LLM-generated body** (non-deterministic across a re-run → would defeat dedup); `dedup_label` lets an agent intentionally repeat an effect to the same target in one turn. `in_flight ≠ completed`: a completed replay returns the stored sanitized snapshot (no re-emit); an in-flight replay raises `EffectInProgressError` (router → 409, never a silent skip-and-succeed). Reuses the 24h default TTL (already exceeds the lease window, so a completed row outlives a late re-delivery — no new TTL plumbing). Wired sinks: `proactive_message_service.send_message`, `voip_service.place_outbound_call`, `agent_shared_files_service.create_share`, `nevermined_payment_service.settle_payment_once`; agents pass `execution_id`+`dedup_label` as MCP tool args (`messages.ts`/`voip.ts`/`files.ts`), **fail-open when absent** (safe today — pull-mode re-delivery is OFF). Trusted runtime injection of `execution_id` + fail-closed-when-absent is a **BLOCKING prerequisite** on Epic #1045/#1081 before pull-mode default-ON for side-effect agents (git push is idempotent-by-construction and needs no key). See [effect-idempotency.md](feature-flows/effect-idempotency.md).

### Subscription Token Rotation via Hot-Reload (#1089)

Rotating an agent's subscription token used to recreate the container, making "rotate a credential" and "kill every in-flight turn" the same operation (#1037). Rotation now hot-reloads the running container; recreate is reserved for image/template/auth-**mode** changes. The agent server authenticates Claude purely from `CLAUDE_CODE_OAUTH_TOKEN` and is a single uvicorn worker, so mutating its process env makes the **next** subprocess use the new token while in-flight ones finish on the old.

Backend orchestration in `services/subscription_auto_switch.py`: `_hot_reload_subscription_token(agent_name)` POSTs the DB token to the agent-server `POST /api/credentials/reload-token`, falling back to `_restart_agent` on 404/transport failure/missing token. Three producer paths converted, all under the #799 `agent_switch_lock`: **auto-switch** (`_perform_auto_switch`, SUB-003), **manual sub→sub reassignment** (`PUT /api/subscriptions/agents/{name}`; auth-mode changes still recreate), and **key rollover** (`reload_subscription_for_all_agents(sub_id)` fans a best-effort reload across running agents). Durable override (`/var/lib/trinity/oauth-token`) + `startup.sh` read make a rotation survive a plain restart. Agent-server mirroring follows Invariant #5.

### Real-time Delivery (RELIABILITY-003, #306)

**Transport** (`event_bus.py`, details in [websocket-event-bus.md](feature-flows/websocket-event-bus.md)): Redis Streams. `ConnectionManager`/`FilteredWebSocketManager` are thin shims that `XADD` to the MAXLEN-trimmed `trinity:events` stream; one `StreamDispatcher` per backend process runs `XREAD BLOCK` and fans out, evicting a client after 3 consecutive delivery failures. New broadcast sites keep calling `manager.broadcast(...)` / `filtered_manager.broadcast_filtered(...)` — never publish to the stream directly (Invariant #10).

**Reconnect replay**: `/ws` and `/ws/events` accept `?last-event-id=<stream_id>`, regex-gated (`^\d+-\d+$`) by `validate_last_event_id()` before `XRANGE`. Catchup capped at `REPLAY_GAP_LIMIT=5000` — larger gaps return `{"type": "resync_required", "reason": "gap_too_large"}`. Authorization (`accessible_agents` for `/ws/events`) is re-applied on replay. The frontend tracks `_eid` per message, appends `&last-event-id=` on reconnect, and on `resync_required` clears the cursor and refetches via REST.

**WebSocket auth** (C-002, #550): `/ws` uses single-use opaque tickets, not a JWT in the URL: `POST /api/ws/ticket` mints a 32-byte urlsafe ticket (Redis, 30s TTL); client connects `/ws?ticket=...`; backend atomically `GETDEL`s then accepts. Closes the JWT-leak surface (nginx logs, history, proxies); CSWSH mitigated because minting needs the JWT in an `Authorization` header (CORS-blocked cross-origin). `/ws/events` still accepts `?token=trinity_mcp_*` for external scripts (scoped, revocable). `mint_ticket` optional `ttl_seconds` (default 30s, ceiling 600s); VoIP mints call-bound tickets (`scope="voip:{call_id}"`, 180s) since PSTN dial+ring exceeds 30s. Impl: `services/ws_ticket_service.py` + `routers/ws_tickets.py`.

### Soft Delete, Retention & Recovery (#834, #772)

**Agent soft-delete (Phase 1a):** `DELETE /api/agents/{name}` sets `agent_ownership.deleted_at` (child rows preserved, recoverable until purge). `is_agent_name_reserved()` sees soft-deleted rows, so the name can't be reused before purge. The scheduler's `list_all_enabled_schedules()` filters `deleted_at IS NULL`, so schedules stop firing immediately.

**Schedule soft-delete (Phase 1b):** `DELETE .../schedules/{id}` sets `agent_schedules.deleted_at` (row + `schedule_executions` preserved). All read paths — incl. cron firing in backend and standalone scheduler — filter `deleted_at IS NULL`. `delete_schedule()` is idempotent on an already-soft-deleted row.

**Admin recovery (Phase 1c):** metadata-only (`deleted_at → NULL`) via `/api/admin/soft-deleted/*`. Agent recovery does NOT recreate the container (`needs_container_recreate=true`; operator runs `POST /api/agents/{name}/start`); schedule recovery rejoins the firing list next poll if enabled. Audit `agent_lifecycle:recover` / `schedule_recover`. Models `SoftDeletedAgent`/`SoftDeletedSchedule`.

**Cleanup Service sweeps** (every 5 min): #772 retention — nulls `schedule_executions.execution_log` past `execution_log_retention_days` (default 30), DELETEs terminal `schedule_executions` past `execution_row_retention_days` (default 90), DELETEs `agent_health_checks` past `health_check_retention_days` (default 7). #834 purges — hard-deletes `agent_ownership` rows soft-deleted past `agent_soft_delete_retention_days` (default 180, `0`=off), cascading children via the #816 `purge_agent_ownership`/`cascade_delete` primitive; hard-deletes `agent_schedules` past `schedule_soft_delete_retention_days` (default 30, `0`=off) via `purge_schedule()`, cascading its `schedule_executions`. Each sweep capped at 5000 rows/cycle; `0` disables; `PRAGMA wal_checkpoint(TRUNCATE)` when any sweep reclaims rows. Also purges expired `idempotency_keys`. **Startup hook (#740):** one-shot `mark_orphan_loops_interrupted()` flips `agent_loops` rows left `queued`/`running` after a restart to `interrupted` (`stop_reason="interrupted"`); no auto-resume.

### Sequential Agent Loops (#740, UI #1106)

Bounded sequential task execution against one agent. Runner is an in-process `asyncio.Task` spawned by `loop_service.py`; each iteration dispatches through `task_execution_service.execute_task()` with `triggered_by="loop"` and the parent `loop_id` carried on the resulting `schedule_executions` row — iterations go through the standard `capacity_manager` admit/slot path, sharing the agent's `max_parallel_tasks` budget. Message template supports `{{run}}` and `{{previous_response}}`; `max_runs` 1–100 hard cap; optional `stop_signal` (until-mode), `delay_seconds`, `timeout_per_run`, `max_duration_seconds`, `model`, `allowed_tools`. Stop is cooperative: `POST /api/loops/{id}/stop` flips an in-process `should_stop` flag; the current iteration finishes and the runner exits with `stop_reason="user_stopped"`. **Wall-clock deadline (#1156):** optional `max_duration_seconds` (≤7 days) measured from `started_at`, checked only at iteration boundaries (before the next run and before/after the inter-run delay, which is capped to the remaining budget) — an in-flight run is never killed mid-turn, so overshoot is bounded by one `timeout_per_run`; expiry stops the loop with `stop_reason="deadline_exceeded"`. Rejected at create (400) when smaller than the effective per-run timeout (`timeout_per_run`, else the agent's `execution_timeout_seconds`). `GET /api/loops/{id}` returns `max_duration_seconds` + computed `elapsed_seconds`. Restart recovery via the cleanup-service startup hook (above); no auto-resume. WS events `loop_run_completed`/`loop_completed`.

**Web UI (#1106):** a **Loops** tab on Agent Detail (`components/LoopsPanel.vue` + agent-scoped `stores/loops.js`; `setAgent(name)` on mount, `clear()` on unmount). The global WS handler routes the fleet-wide loop events to the store, which filters by mounted agent and targeted-refreshes only the affected loop; a 12s backstop poll runs while any loop is `queued`/`running` to recover a missed terminal event. Last full response rendered via `utils/markdown.js` (DOMPurify).

### Session Tab

`--resume`-default chat surface: each turn reattaches via `claude --print --resume <uuid>`, preserving tool-result memory, mid-skill state, and reasoning state across turns. Strictly parallel to `chat_sessions`/`chat_messages` — no FK, no shared state; separate router (`routers/sessions.py`), store (`stores/sessions.js`), component (`SessionPanel.vue`). `cached_claude_session_id` is the load-bearing field.

**Unified Chat tab (#1112):** Agent Detail shows a single **Chat** tab carrying a **Session-mode toggle**, default ON. ON → `SessionPanel.vue` (`--resume` continuity); OFF → legacy stateless `ChatPanel.vue`. The toggle swaps in-place (`v-if` on `effectiveChatMode`); choice persists per-user in `localStorage['trinity.chatMode']`. Session mode available only when `sessionsStore.sessionTabEnabled` AND the runtime has `--resume` (not Codex) — else the toggle hides and the tab falls back to legacy (never zero chat surfaces). Routing: legacy `?tab=session` aliases to `chat` (`TAB_ALIASES`) and hints session mode; ExecutionDetail "continue as chat" (`?tab=chat&resumeSessionId=…`) forces legacy via a transient `routeForcedMode` without rewriting the saved preference.

**Turn semantics** (`POST .../sessions/{id}/message`, synchronous; details in [session-tab.md](feature-flows/session-tab.md)): always passes `persist_session=True`. Resume-failure fallback: missing cached-UUID JSONL → clear cache, increment `consecutive_resume_failures`, retry once cold (reset on next success). Two Redis gates, dynamic TTL = `get_execution_timeout(agent) + 30s` capped 7230s: (1) resume lock `session_lock:{agent}:{uuid}` (`session_lock:cold:{session_id}` for cold, #779) serialises `--resume` to prevent JSONL corruption (429 on contention); (2) in-flight sentinel `session_inflight:{session_id}` drives `turn_in_progress` for UI reattach (#759).

**Access & gating:** all endpoints per-user scoped (owners cannot see other users' sessions) and return 404 — not 403 — on mismatch to avoid leaking session-id existence. All return 404 when `is_session_tab_enabled()` is false; flag `system_settings.session_tab_enabled` (or `SESSION_TAB_ENABLED` env), default ON.

**JSONL reaping** (`session_cleanup_service.py`): default 6h cycle diffs each running agent's `~/.claude/projects/-home-developer/<uuid>.jsonl` set against `agent_sessions.cached_claude_session_id`, deleting JSONLs outside the keep set with mtime older than `min_age_seconds` (default 1h race guard). Synchronous `reap_jsonl()` also fires on reset/delete. Uses `execute_command_in_container` (no agent-server endpoint). Headless-task JSONLs (timeout > 600s auto-enables persistence for the #678 stdout-race recovery in `agent_server/services/jsonl_recovery.py`) aren't in `agent_sessions`, so they fall out of the keep set and the same sweep removes them.

### Outbound File Sharing (FILES-001)

Per-agent opt-in (`agent_ownership.file_sharing_enabled`). The agent writes to `/home/developer/public/` (Docker volume `agent-{name}-public`); on share, the backend extracts the named file via Docker SDK `get_archive` (never mounts the workspace — isolated blast radius) and stores bytes at `/data/agent-files/{file_id}`. `agent_shared_files_service.py` handles path validation, MIME blocklist, quota, extraction, URL building.

Download URL: `{public_chat_url}/api/files/{file_id}?sig={token}` — `?sig=` (NOT `?download_token=`) so the credential sanitizer's `.*TOKEN.*` pattern doesn't redact it in transcripts. Cascades manual per platform convention: agent delete removes rows + files + volume; `rename_agent()` updates `agent_name` across 17 tables. MCP tool `share_file`.

### Agent Runtime Data — `data_paths` + Snapshot/Export (#1169)

Declared runtime data (SQLite DBs, datasets) over the **existing durable home volume** — **no separate volume, no platform schema change** (snapshots are filesystem artifacts; audit rides `audit_log`). The agent home (`/home/developer`) is already a persistent named Docker volume (`agent-{name}-workspace`) that survives recreate/upgrade/template-repull/sub-switch, so data under `/home/developer/data` is already durable; this feature adds only the **declaration + export/import** surface.

**Declaration:** a template's `template.yaml data_paths:` (globs under `data/`) is surfaced by `template_service` and materialized at creation by `crud.py` → `git_service.materialize_data_paths()`: writes `~/.trinity/data-paths.yaml` (quoted-heredoc) AND appends `data/` + each path to the agent's **own** `.gitignore` (idempotent `grep -qxF`, never the fleet-wide `_GITIGNORE_PATTERNS`). Opt-in — empty list is a no-op. Shares one primitive with S4 persistent-state (`materialize_trinity_yaml_list`/`_read_trinity_yaml_list`, heredoc delimiter parameterized).

**Export** (`routers/agent_data.py`, `POST /api/agents/{name}/data/export`, owner/admin): streams `container_get_archive("/home/developer/data")` → temp file under `/data/agent-data-tmp` → `StreamingResponse` (temp removed via `BackgroundTask`); `AGENT_DATA_EXPORT_MAX_BYTES` (default 5 GiB) → 413; the tar embeds a self-describing `manifest.json`. Missing `data/` → manifest-only tar, not 500. `?format=base64` returns the tar inline as JSON up to `AGENT_DATA_INLINE_MAX_BYTES` (default 10 MiB) for MCP. Naturally-idempotent read (accepts `Idempotency-Key`; creates no execution).

**Import** (`POST /api/agents/{name}/data/import`, owner/admin): proxies the uploaded tar to the agent-server `POST /api/agent-server/restore` primitive (`restore_from_tar` enforces the `data/**` allowlist, rejects absolute/`..`); deduped via `Idempotency-Key`. Both endpoints serialized per agent by a cross-worker Redis op lock (`agent:data_op:{name}`, SETNX+TTL, fail-open, 409 on contention). MCP tools `export_agent_data`/`import_agent_data` carry the base64 tar — "move an agent" = template URL + `.credentials.enc` + data tar. System agents out of scope. **PR2 (deferred):** scheduled snapshots + `~/.trinity/pre-snapshot` quiesce hook + retention + rename/purge cascade.

### Git Sync Health (#389/#390)

**Agent side:** 15-min `auto_sync` heartbeat loop in the agent server (gated by `GIT_SYNC_AUTO`; default-on for non-source-mode GitHub-template agents) stages/commits/pushes in-container changes and writes the outcome to `.trinity/sync-state.json` (S1a).

**Backend side** (details in [git-sync-health.md](feature-flows/git-sync-health.md)): `SyncHealthService` polls git-enabled agents every 60s, upserts `agent_sync_state` (`consecutive_failures` ++ on fail / reset on success; `ahead_working`/`behind_working` expose working-branch divergence, P6), emits `sync_failing` operator-queue entries at ≥3 failures (S1). Powers the dashboard sync dot, `GET /api/agents/sync-health`, and `GET /api/fleet/sync-audit` — whose `duplicate_binding` flag marks agents sharing a `(github_repo, working_branch)` pair (§P5 silent-clobber setup) (S6, #390).

**Recovery (S3, #384):** `POST /api/agents/{name}/git/reset-to-main-preserve-state` adopts `origin/main`, snapshots the S4 persistent-state allowlist first, overlays it back, force-with-lease pushes — safe recovery for parallel-history deadlock (P2/P3). 409 with `X-Conflict-Type: agent_busy | no_git_config | no_remote_main`. Per-agent toggles: auto-sync flag and freeze-schedules-if-sync-failing flag (see API Endpoints).

### VoIP Telephony (VOIP-001, #1056)

Outbound phone calls from agents via Twilio Media Streams + Gemini Live (details in [voip-telephony.md](feature-flows/voip-telephony.md)). Feature-flag gated: `voip_available = VOIP_ENABLED && bool(GEMINI_API_KEY)`, default OFF; also requires a per-agent `voip_bindings` row (Twilio-voice creds, validated via Twilio Account fetch, AuthToken AES-256-GCM encrypted). A voice transport, NOT a text `ChannelAdapter`.

**Call flow:** MCP tool `call_user` → `POST /api/agents/{name}/voip/call` → `voip_service.py`: gate checks (flag/binding) + abuse controls (rate limit per `(owner, destination)`, durable per-agent daily cap), stages a Gemini session intent in Redis keyed by `call_id` (distinct from the `vs_` VoiceSession id), mints a call-bound WSS ticket, calls Twilio `calls.create(<Connect><Stream>)`. Never calls `connect_and_stream` itself (cross-worker safety — the WS handler does). Optional `Idempotency-Key` honored (Invariant #18).

**Media bridge** (`transports/twilio_media_stream.py`, WS `/api/voip/voice/{call_id}`): `accept()`-then-authenticate — Twilio does NOT forward the `<Stream url>` query string, so the call-bound ticket arrives as `start.customParameters.ticket` in the first `start` frame, read after handshake (#1073); `?ticket=` fallback for non-Twilio clients. Then scope check (`voip:{call_id}`), `GETDEL` staged intent (consume-once), create the Gemini `VoiceSession` on the connecting worker, run the unmodified `connect_and_stream`. Per-connection `_CallBridge`: inbound μ-law→PCM resample, outbound paced 20ms 160-byte μ-law sender, `clear`-on-barge-in, `streamSid` capture; teardown ties Gemini-end→Twilio-close + SETNX-guarded single transcript save + post-call dispatch. Codec helpers in `transports/voip_audio.py` (stdlib `audioop`, per-direction `ratecv` state for anti-click; `audioop-lts` pinned for Python ≥ 3.13).

**Post-call:** transcript persisted to `chat_messages` (`source="voice"`) and dispatched to the main agent via `task_execution_service.execute_task(triggered_by="voip")` (default ON). Phase 2 column `inbound_number` reserved in `voip_bindings`.

### Canary Invariant Harness (CANARY-001, #411)

Continuous orchestration-invariant watcher. Deterministic library (`src/backend/canary/`) shared between the 5-min watcher service and the on-demand admin endpoint — the library reads state (Redis × SQLite × agent registries) but writes nothing; the service persists violations to `canary_violations` and classifies green→red transitions. No LLM reasoning anywhere — the canary's value depends on determinism. Disabled by default; `CANARY_ENABLED=1` on staging/dev. **Alert sink:** one Slack Block Kit webhook POST per green→red transition (`CANARY_SLACK_WEBHOOK_URL` env; unset = silent sink — cycles still run, violations persist; continuing-red doesn't re-post). **Fleet:** `config/canary-fleet.yaml` deploys synthetic load generators (`canary-fleet-burst`, `canary-fleet-long`) via the systems-deploy API — without traffic the checks are trivially green.

Lookup keys: S-01/E-02/L-03 shipped via #653; S-02/E-01/E-05/B-01 (Phase 2) and S-03/B-02/R-01 (Phase 3) via #882.

| ID | Tier | Severity | Invariant (bug class guarded) |
|----|------|----------|-------------------------------|
| S-01 | A | major | Slot–row bijection: per agent, execution_ids in `agent:slots:{name}` (drain sentinels filtered) ≡ execution_ids of `running` rows (PR #378/#403 class). Severity `critical`→`major` (#1082): redundant under #1082 single-owner status — the slot ZSET is no longer a competing authority — and retires with the slot ZSET in #1081 Phase 5 |
| S-02 | A | critical | No overbooking: `ZCARD(agent:slots:{name})` ≤ `max_parallel_tasks` — distinct from S-01 because Redis and SQL can agree on N+1 (`acquire_slot` concurrency bypass) |
| S-03 | A | critical | Slot TTL floor: every `agent:slot:{name}:{eid}` HASH created with ≥ `execution_timeout_seconds + 300s` TTL. Kinds: `missing` (-2, HASH expired ahead of ZSET — #226 class), `no_expiry` (-1), `below_floor`. Decay-invariant (#913): reconstructs *initial* TTL as `ttl + age` (age = snapshot − ZSET score, the ZADD epoch) vs `floor − 1` (1s wire-rounding tolerance), so natural decay never fires but a real below-floor set still does |
| E-01 | B | critical | Terminal-state closure: no `running` row older than `execution_timeout_seconds + 300s` (matches `SLOT_TTL_BUFFER`, so it fires after cleanup's window) |
| E-02 | A | critical | No phantom reversal: a row terminal in the previous cycle never reappears non-terminal (Redis state key `canary:e02:terminal_seen`) |
| E-05 | B | major | Dispatched rows have session: no `running` row >60s with `claude_session_id IS NULL` (#106) |
| B-01 | A | critical | Queue-status coherence: `db.get_queued_count` ≡ independently-collected `len(queued_exec_ids)` — regression guard against a future cache layer or status-filter drift |
| B-02 | B | critical | No queued without slots-full: queued > 0 ⇒ slots full OR a drain tick fired <60s ago (`canary:drain_tick_at` heartbeat) |
| L-03 | A | crit/major | Delete cascades: no live row in any cross-cutting table (sharing, schedules, non-terminal executions, skills, tags, shared files, public links, pending operator queue/access requests, agent-scoped MCP keys, active chat sessions) referencing an `agent_name` absent from `agent_ownership`; no orphan `agent:slots:{name}` (critical for orphaned executions/slots, major otherwise; #129 class) |
| R-01 | A | critical | No zombie Claude processes: per running agent container, `ps -eo stat,comm` shows zero `^Z.*claude` (anchored `^Z` — procps-ng emits STAT left-aligned; guards PR #407). Docker-exec source; per-container failures land in `sources_unavailable` so one unhealthy container doesn't kill the cycle |

### Agent Compatibility Validation (#668)

Advisory, non-blocking server-side validation of a **running** agent's workspace
against ~100 best-practice checks (11 categories) — surfaced in the Agent Detail
Overview tab (`components/CompatibilityPanel.vue`, reusing the "needs attention"
idiom), via `GET /api/agents/{name}/compatibility`, and the MCP tool
`get_agent_compatibility_report`. The canonical check list is
`docs/agent-validation-spec.md` (single source of truth, sync-tested against
`spec.py`).

Package `services/compatibility/` mirrors the deterministic `canary/` library (`spec.py` catalog, `collector.py`, `static_checks.py`, `ai_checks.py`, `fixes.py`, `__init__.py` → `build_report`/`apply_fix`). Details in [agent-compatibility-validation.md](feature-flows/agent-compatibility-validation.md).

- **Collector**: ONE `docker exec` runs a base64-injected `python3` script walking a FIXED path allowlist → ONE JSON snapshot (per-file `{exists,size,binary,truncated,content}`, 256 KB/file + 2 MB/total caps); secret-bearing files (`.env`, `.mcp.json`) are **existence-only**. Backend `json.loads` once → `unavailable` on any failure (never 500); a stopped container → degraded report from the last persisted result.
- **Checks**: pure `(snapshot)→[Check]` functions. `[STATIC]` deterministic (always, free); `[AI]` LLM-judged (Claude Haiku, batched by category, tool-use structured output, **iterate-expected**, fail-open on no-key/error). **AI severity capped at SOFT** — HARD reserved for STATIC. Claude-only checks skipped for non-Claude runtimes (#1187). Secret values never echoed; AI payloads redacted.
- **Persistence** (`agent_compatibility_results`, latest-snapshot-per-agent, upsert): STATIC recomputes live; persisted AI verdicts merge in so findings show on every Overview load without re-spending tokens (`?include_ai=true` / "Re-run" forces fresh AI; requirements §41). Cascade/rename via `AGENT_REFS`.
- **Auto-fix** (`POST .../compatibility/fix`, owner/admin): the 10 gitignore checks; reuses `git_service._GITIGNORE_PATTERNS`; per-agent Redis lock (`compat_fix:{name}`); atomic base64 write-back; G-001 removes a blanket `.claude/` line by exact-line match. **No auto-commit** — uncommitted until next git sync. Creates no execution.

---

## API Endpoints

### Agents (33 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents` | List all agents |
| GET | `/api/agents/context-stats` | Context & activity state for all agents |
| GET | `/api/agents/autonomy-status` | Autonomy status for all accessible agents |
| GET | `/api/agents/sync-health` | Per-agent git sync health for dashboard dots (#389) |
| POST | `/api/agents` | Create agent |
| GET | `/api/agents/{name}` | Get agent details |
| DELETE | `/api/agents/{name}` | Soft-delete agent (see [Soft Delete](#soft-delete-retention--recovery-834-772)) |
| POST | `/api/agents/{name}/start` | Start agent |
| POST | `/api/agents/{name}/stop` | Stop agent |
| POST | `/api/agents/{name}/chat` | Send chat message |
| GET | `/api/agents/{name}/chat/history` | In-memory chat history (container) |
| GET | `/api/agents/{name}/chat/history/persistent` | Persistent chat history (database) |
| GET | `/api/agents/{name}/chat/sessions` | List chat sessions |
| GET | `/api/agents/{name}/chat/sessions/{id}` | Session details with messages |
| POST | `/api/agents/{name}/chat/sessions/{id}/close` | Close chat session |
| DELETE | `/api/agents/{name}/chat/history` | Reset session |
| GET | `/api/agents/{name}/logs` | Container logs |
| GET | `/api/agents/{name}/stats` | Live telemetry |
| GET | `/api/agents/{name}/activity` | Activity summary |
| GET | `/api/agents/{name}/info` | Template metadata |
| GET | `/api/agents/{name}/a2a/agent-card` | A2A v1.0 Agent Card for external orchestrator discovery (#737) |
| GET | `/api/agents/{name}/files` | List workspace files (tree) |
| GET | `/api/agents/{name}/files/download` | Download file |
| POST | `/api/agents/{name}/files/mkdir` | Create workspace directory (#37) |
| GET/PUT | `/api/agents/{name}/folders` | Get/update shared folder config |
| GET | `/api/agents/{name}/folders/available` | Mountable folders from permitted agents |
| GET | `/api/agents/{name}/folders/consumers` | Agents that will mount this folder |
| GET/PUT | `/api/agents/{name}/autonomy` | Get / enable-disable autonomy (toggles all schedules) |
| POST | `/api/agents/{name}/ssh-access` | Ephemeral SSH credentials (admin-only) |
| GET/PUT | `/api/agents/{name}/read-only` | Read-only mode status / toggle (blocks source file writes) |
| GET/PUT | `/api/agents/{name}/timeout` | Execution timeout (60–7200s, default 3600s, #665). PUT 400 `agent_timeout_below_active_schedules` if the new cap drops below any non-deleted schedule's `timeout_seconds` (#929) |
| GET/PUT | `/api/agents/{name}/guardrails` | Per-agent guardrails config / overrides (GUARD-001) |
| GET/PUT | `/api/agents/{name}/file-sharing` | Outbound file-sharing status + quota / owner-only toggle (returns `restart_required`) (FILES-001) |
| POST | `/api/agents/{name}/shared-files` | Mint a download URL for a file in the publish dir (owner/admin or agent-scoped key) |
| GET | `/api/agents/{name}/shared-files` | List active shared files with download counts |
| DELETE | `/api/agents/{name}/shared-files/{file_id}` | Revoke a shared file (owner-only; idempotent) |
| POST | `/api/agents/{name}/user-memory` | Write per-user memory blob; email resolved from execution_id server-side (MEM-001, #888) |
| POST | `/api/agents/{name}/data/export` | Export agent `data/` as a tar (owner/admin; `?format=stream`\|`base64`; 413 over cap; per-agent op lock). See [Agent Runtime Data](#agent-runtime-data--data_paths--snapshotexport-1169) (#1169) |
| POST | `/api/agents/{name}/data/import` | Restore an uploaded tar into agent `data/` via the agent-server restore primitive (owner/admin; `data/**` allowlist + traversal guard; `Idempotency-Key`; op lock) (#1169) |
| POST | `/api/agents/{name}/heartbeat` | Agent liveness heartbeat — auth and semantics in [Heartbeat Liveness](#heartbeat-liveness-reliability-004-307) |
| POST | `/api/agents/{name}/executions/{execution_id}/result` | Fire-and-forget terminal callback — agent's own MCP key + ownership + durable async-marker gate; finalizes via `apply_result`. See [Fire-and-Forget Dispatch](#fire-and-forget-dispatch-1083) (#1083) |
| GET | `/api/agents/{name}/circuit-breaker` | Unified breaker state: `{dispatch:{state,failure_count,retry_after_seconds}, transport:{...}, open:bool, config:{enabled,global_enabled}}` (#526) |
| PUT | `/api/agents/{name}/circuit-breaker` | Enable/disable per-agent dispatch breaker (owner-only); engages only with global `DISPATCH_BREAKER_ENABLED` (#526) |
| POST | `/api/agents/{name}/circuit-breaker/reset` | Admin-only; resets BOTH transport and dispatch breakers to closed (#921, #526) |
| GET | `/api/agents/{name}/compatibility` | Compatibility report (`?include_ai=` forces fresh AI; STATIC live + persisted AI). Non-blocking; `unavailable` when stopped. See [Agent Compatibility Validation](#agent-compatibility-validation-668) (#668) |
| POST | `/api/agents/{name}/compatibility/fix` | Owner/admin; apply a gitignore auto-fix (`{check_id}`). 400 non-fixable, 409 concurrent fix. Uncommitted until next git sync (#668) |

**Note**: Route ordering is critical — static routes (`/context-stats`, `/autonomy-status`) must be defined BEFORE the `/{name}` catch-all (Invariant #4).

### Voice (6 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agents/{name}/voice/start` | Start Gemini Live voice session; `workspace_mode` enables panel tools. Resolves voice as request override → persisted `voice_name` → `Kore` (#28) |
| POST | `/api/agents/{name}/voice/stop` | Stop active voice session |
| GET/PUT | `/api/agents/{name}/voice/prompt` | Get/set per-agent voice system prompt |
| GET/PUT | `/api/agents/{name}/voice/name` | Get (any accessor; returns `available_voices`/`default_voice`) / set (owner-only; 400 on a voice outside `GEMINI_VOICE_NAMES`) the persisted per-agent Gemini voice. Applies to both the voice overlay/workspace and outbound VoIP calls (#28) |
| GET | `/api/agents/{name}/voice/{session_id}/panel` | Canvas panel state for workspace mode (ownership-gated; empty state when session gone, #699) |

### VoIP Telephony (VOIP-001, #1056 — flag-gated, default OFF)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET/PUT/DELETE | `/api/agents/{name}/voip` | Owner | Twilio-voice binding status / configure / remove. 404 when `voip_available` off. Re-PUT preserves the `enabled` flag (#28) |
| PUT | `/api/agents/{name}/voip/enabled` | Owner | Enable/disable the binding without re-entering credentials (`{enabled: bool}`); 404 when no binding exists. Disabled ⇒ outbound calls refused (#28) |
| POST | `/api/agents/{name}/voip/call` | JWT/MCP (`AuthorizedAgent`) | Place outbound call; rate-limited + daily-capped; optional `Idempotency-Key`. Returns `{call_id, status:"ringing", twilio_call_sid}` |
| WS | `/api/voip/voice/{call_id}` | Call-bound ticket | Twilio Media Streams audio bridge — see [VoIP](#voip-telephony-voip-001-1056) |

The per-agent VoIP config + voice-picker UI lives in the agent Settings/Sharing tab (`components/VoipChannelPanel.vue`), shown only when the platform `voip_available` flag is true (frontend reads it via `stores/sessions.js`); the underlying CRUD/voice endpoints are OSS and ungated (#28).

### Activities (1 endpoint)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/activities/timeline` | Cross-agent activity timeline. Params: `start_time`/`end_time` (ISO 8601), `activity_types` (comma-separated), `limit` (default 100). Returns only agents the user can access (owner, shared, or admin) |

### Credentials (CRED-002)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents/{name}/credentials/status` | Check credential files in agent |
| POST | `/api/agents/{name}/credentials/inject` | Write credential files directly to agent (`files` text + `files_b64` binary) |
| POST | `/api/agents/{name}/credentials/export` | Export to `.credentials.enc` (AES-256-GCM) |
| POST | `/api/agents/{name}/credentials/import` | Import from encrypted file |
| POST | `/api/internal/decrypt-and-inject` | Auto-import on agent startup (internal, no auth) |

**Credential-path policy (#11):** injection accepts a **curated set of credential file types**, not a fixed 3-path list — the policy lives in `services/credential_paths.py` (`is_allowed_credential_path`), vendored **byte-identically** into `docker/base-image/agent_server/credential_paths.py` for the agent-server second layer (Invariant #5; parity test). Allows `.env`/`.credentials.enc`/`.mcp.json` (the last still content-validated, #598) + `.config/gcloud/**`, `.kube/config`, `*.pem`/`*.key`/`*.crt`/`*.cert`/`*.p12`/`*.pfx`, `.ssh/id_*`; deny-list (precedence) blocks anything executed/sourced at startup (shell rc, `CLAUDE.md`/`AGENTS.md`/`.claude/**`, `.mcp.json.template`, `.ssh/authorized_keys`/`config`, `.git*`, `bin/**`) plus `..`/absolute traversal. Binary creds round-trip as base64 (`files_b64`); the `.credentials.enc` archive is a v2 `{files, files_b64}` envelope (legacy flat archives still decrypt) and export captures the **full** injected set via the agent `GET /api/credentials/list`.

### GitHub PAT & Git (#347, #389, #384)
| Method | Path | Description |
|--------|------|-------------|
| GET/PUT/DELETE | `/api/agents/{name}/github-pat` | PAT config status / set per-agent PAT (validated, encrypted) / clear (revert to global) |
| GET/PUT | `/api/agents/{name}/git/auto-sync` | Per-agent 15-min auto-sync heartbeat flag |
| GET/PUT | `/api/agents/{name}/git/freeze-schedules-if-failing` | Freeze-on-sync-failure flag |
| GET | `/api/agents/{name}/git/sync-state` | Persisted sync-state row |
| POST | `/api/agents/{name}/git/reset-to-main-preserve-state` | Recovery reset — see [Git Sync Health](#git-sync-health-389390) |
| GET | `/api/fleet/sync-audit` | Aggregate per-agent sync state + `duplicate_binding` flag (admins all; others accessible agents) |

### Templates (4 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/templates` / `/api/templates/{id}` | List templates / template details |
| GET | `/api/templates/env-template` | Env template |
| POST | `/api/templates/refresh` | Refresh cache |

### Sharing & Access Control (#311, #951)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agents/{name}/share` | Share agent |
| DELETE | `/api/agents/{name}/share/{email}` | Remove share |
| GET | `/api/agents/{name}/shares` | List shares |
| GET | `/api/agents/{name}/access` | Operator (Trinity-user) access roster for the **Access tab** (trinity-enterprise#17). Resolves each `agent_sharing` allow-list email against `users`: resolved → **active** operator (`username`/`role`/`last_active`), unresolved → **pending** invite. Read-only typed view over `agent_sharing`; add/remove reuse `/share` + `/share/{email}`. Drawing the operator-vs-client line on the read path is this endpoint's job (strict client roster is the Sharing redesign #18/#20) |
| GET/PUT | `/api/agents/{name}/access-policy` | Cross-channel access policy: `require_email` / `open_access` flags |
| GET | `/api/agents/{name}/access-requests` | Pending access requests |
| POST | `/api/agents/{name}/access-requests/{id}/decide` | Approve (auto-shares + fire-and-forget approval notification on the requester's originating channel for telegram/slack/whatsapp, #951) or reject |

### Schedules (13 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET/POST | `/api/agents/{name}/schedules` | List / create. POST 400 `schedule_timeout_exceeds_agent_cap` if `timeout_seconds` > agent cap (#929) |
| GET/PUT/DELETE | `/api/agents/{name}/schedules/{id}` | Get / update (same 400 on timeout) / soft-delete |
| POST | `/api/agents/{name}/schedules/{id}/enable` · `/disable` · `/trigger` | Enable / disable / manual trigger |
| GET | `/api/agents/{name}/schedules/{id}/executions` | Execution history |
| GET | `/api/agents/{name}/schedules/analytics-summary` | **Per-schedule performance rollups for the whole agent** in one call (#1115). `?window=` ∈ {7d,14d,30d}→168/336/720h (422 else). One row per **non-deleted** schedule (zero-run included): terminal `success_rate` (`None`→`—`), `avg_duration_ms` (NULL-skip), `cost_total`, `context_avg`, `tool_call_total`, last-run outcome. Backs both the Overview "Schedules performance" section and the Schedules-tab inline stats from one fetch. **Declared before `/{id}`** so `analytics-summary` isn't captured as a `schedule_id` (Invariant #4). DB `get_agent_schedules_summary`; tool-call totals over the newest 5,000 rows (`tool_calls_sampled`) |
| GET | `/api/agents/{name}/schedules/{id}/analytics` | Per-schedule analytics: counts, success rate, duration p50/p95/p99, cost, tool-call top-5, daily timeline. `?window_hours=` ∈ {24,168,720}, default 168 (#868). Percentiles Python-side over the newest 5,000 success rows (`sampled:true` when capped); counts + timeline full-set; UTC buckets gap-filled. Tenant boundary in the DB layer (`agent_name` passed through) — `AuthorizedAgent` validates only the path agent name, not that `schedule_id` belongs to it. Soft-deleted schedules 404 |
| POST/GET/DELETE | `/api/agents/{name}/schedules/{id}/webhook` | Generate/rotate token · status + URL · revoke (WEBHOOK-001) |

### Webhook Triggers (WEBHOOK-001)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/webhooks/{webhook_token}` | Token (URL-embedded) | Trigger schedule execution; rate-limited 10/60s per token via `rate_limiter.py` (#1023); returns 202 |

Token lifecycle: `secrets.token_urlsafe(32)` stored in `agent_schedules.webhook_token` (partial unique index, O(1) lookup); re-POST rotates (old URL instantly invalid); DELETE nulls (subsequent triggers 404). Optional `{"context": "..."}` body (max 4000 chars) appended to the schedule message wrapped in a framing header to reduce prompt-injection surface. All triggers audit-logged with `triggered_by="webhook"`; auto-derives idempotency key `(token, body_hash)` (Invariant #18).

### Auth, Users & MCP (15 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/auth/mode` | Auth mode config (unauthenticated) |
| POST | `/api/token` | Admin login — `username` accepts `admin` OR the admin's registered email + password (#82); form-encoded |
| POST | `/api/auth/email/request` / `/verify` | Request email code / verify and login |
| GET | `/api/auth/validate` | Validate JWT (for nginx auth_request) |
| GET | `/api/users/me` | Current user |
| PUT | `/api/users/me/email` | Bind a sign-in email to the caller's own account (#82 transition; 409 if taken). No verification email sent |
| GET | `/api/users` | List users with roles (admin-only; exposes `suspended_at` read-only) (ROLE-001) |
| PUT | `/api/users/{username}/role` | Update user role (admin-only) |
| GET | `/api/mcp/info` | MCP server info |
| POST/GET/DELETE | `/api/mcp/keys` (`/{id}`) | Create / list / delete API keys |
| GET | `/oauth/{provider}/authorize` / `/callback` | OAuth start / callback |
| GET | `/health` | Health check (unauthenticated, top-level — no `/api/` prefix) |
| GET | `/api/version` | Platform version + build-time git provenance (`git_commit`, `git_commit_short`, `git_commit_subject`, `git_commit_timestamp`, `git_branch`, `build_date`) from Dockerfile ARG/ENV wired through compose build args + `start.sh`; all default `"unknown"` when absent (#926) |

### Soft-Delete Admin Recovery (#834 Phase 1c)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/admin/soft-deleted/agents` | Admin | List soft-deleted agents (newest first) with computed `purge_eta` (null if retention `0`); `limit` ≤ 500 |
| POST | `/api/admin/soft-deleted/agents/{name}/recover` | Admin | Clear `deleted_at`; 404 if not soft-deleted; container NOT recreated. Audit `agent_lifecycle:recover` |
| GET | `/api/admin/soft-deleted/schedules` | Admin | List soft-deleted schedules (optional `?agent_name=`); `purge_eta`; `limit` ≤ 500 |
| POST | `/api/admin/soft-deleted/schedules/{id}/recover` | Admin | Clear `deleted_at`; rejoins scheduler next poll if enabled. Audit `agent_lifecycle:schedule_recover` |

### Executions (EXEC-022)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/executions/stats` | Fleet stat cards: `total`/`success_count`/`failed_count`/`total_cost` windowed by `hours` (0 = all-time); `running_count`/`queued_count` always live. Optional `agent` filter |
| GET | `/api/executions` | Paginated fleet execution list. Filters: `status`, `triggered_by`, `hours`, `agent`, `search`; `limit` (max 200, default 50), `offset`; ordered `started_at DESC` |

Access: admins see all; non-admins only owned/shared agents (`accessible_agent_names()` helper). Stats use single-pass conditional aggregation (one SQL query). `/stats` registered before `""` so the literal `"stats"` never routes as an execution ID.

### Agent Overview Analytics (#1107)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/agents/{name}/analytics` | `AuthorizedAgent` | Multi-day execution analytics for the Overview tab. `?window=` ∈ {`7d`,`14d`,`30d`} (422 otherwise). Returns per-day counts stacked by type bucket, per-day + headline terminal success rate, duration avg (full-set) + p95 (sampled), avg context use, per-bucket totals, gap-filled UTC-day timeline |

Generalises #868 to agent scope (`db/schedules.py:get_agent_analytics`); read-only, DB-sourced (renders when the agent is stopped). Data-source discipline (locked by /autoplan review): all per-day series and headline `avg`/`context_avg` are **full-set** aggregates — never the capped pool (a sampled avg would be silently wrong on high-traffic agents); only headline p95 uses the newest 5,000 success rows (`sampled=true` when capped). `triggered_by` grouped in Python via `_TRIGGER_BUCKETS` (Chat/Tasks, MCP, Channels, Public, Scheduled, Loops, Agent-to-agent, Voice) with an explicit `Other` catch-all so a new trigger never silently vanishes (`manual` → Chat/Tasks; `loop` → Loops, #1150). `success_rate` is terminal-based; zero-terminal days report `null` so charts render a gap, not a false 0%; `context_avg` uses NULL-skipping AVG.

### Operator Queue (OPS-001)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/operator-queue` | List queue items (filters: status, type, priority, agent_name, since) |
| GET | `/api/operator-queue/stats` | Counts by status/type/priority/agent |
| POST | `/api/operator-queue/bulk-cancel` | Cancel listed pending items (`{ids: [...]}`, 1–500, ids-scoped so a sync race can't cancel unseen items); returns `{cancelled, skipped}`; audit-logged (#1017) |
| POST | `/api/operator-queue/clear-resolved` | Hide terminal rows (acknowledged/cancelled/expired) by setting `cleared_at` — NOT a DELETE (the 5s sync loop would resurrect items whose agent-file entry still says `pending`); `responded` kept visible until delivered; actual deletion deferred to the retention sweep (#1142); returns `{cleared}`; audit-logged (#1017) |
| GET | `/api/operator-queue/{id}` | Single item |
| POST | `/api/operator-queue/{id}/respond` / `/cancel` | Submit operator response / cancel pending item. Respond returns **409** if the item left `pending` under the caller (race vs bulk-cancel, #1017) |
| GET | `/api/operator-queue/agents/{name}` | Items for one agent |

Bulk ops scope writes to the caller's accessible agents (tri-state: admin = no filter, empty set = no-op). The sync service write-back also propagates `cancelled`/`expired` status into agent queue files (in-place flips of still-`pending` entries only) so agents stop waiting on cleared items and stale file entries can't resurrect purged rows (#1017). The Operations UI exposes these as a per-tab **Clear All** button (`notifications` tab uses `POST /api/notifications/dismiss-all` — bulk pending+acknowledged → dismissed, same accessible-set scoping).

WebSocket events: `operator_queue_new`, `operator_queue_responded`, `operator_queue_acknowledged`, `operator_queue_cleared` (one per bulk op, #1017; `notifications_cleared` for the notifications variant). Backed by the 5s Operator Queue Sync background service.

### Platform Audit Log (SEC-001)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/audit-log` | Admin | List entries (filters: event_type, actor_type, actor_id, target_type, target_id, source, start/end_time, limit, offset) |
| GET | `/api/audit-log/stats` | Admin | Counts by event_type and actor_type |
| GET | `/api/audit-log/heatmap` | Admin | Day-of-week × hour-of-day sparse 7×24 grid; honors time + event/actor filters (#941) |
| GET | `/api/audit-log/calendar` | Admin | Per-day calendar heatmap (sparse `[{date, count}]`); same filters — *when* in calendar time vs the weekly pattern from `/heatmap` (#941) |
| GET | `/api/audit-log/{event_id}` | Admin | Single entry by UUID |
| GET | `/api/audit-log/distinct/event-types` / `/actor-types` | Admin | Distinct values for dashboard filter dropdowns (#941) |
| GET | `/api/audit-log/export` | Admin | Export time range as `json` or `csv` |
| POST | `/api/audit-log/verify` | Admin | Verify SHA-256 hash chain over `start_id..end_id` |
| POST | `/api/audit-log/hash-chain/enable` | Admin | Toggle hash-chain computation for new entries |
| POST | `/api/internal/audit` | Internal secret | Fire-and-forget write path for MCP tool-call audit |

Coverage: agent lifecycle, auth, sharing, credentials, settings, rename; request-ID middleware; MCP tool-call audit via a transparent wrapper (all 66+ tools, zero per-tool code). Storage: append-only `audit_log` table (see schema). `/api/audit-log` is the only audit surface (the old `/api/audit` Process Engine router is gone).

### Canary (CANARY-001)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/canary/violations` | Admin | List violations (filters: invariant_id, severity, tier, start/end_time, limit, offset) |
| GET | `/api/canary/violations/stats` | Admin | Counts by invariant_id and severity |
| GET | `/api/canary/violations/{id}` | Admin | Single violation |
| POST | `/api/canary/run-cycle` | Admin | Run one cycle on demand (same `CanaryService.run_cycle()` as the 5-min loop; optional invariant filter in body). Returns snapshot + violations + transitions; 409 `"cycle in progress"` when another cycle is mid-run — empty payload never silently returned |

### Nevermined Payments (NVM-001)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/paid/{agent_name}/chat` | x402 | Paid chat (402/403/200) |
| GET | `/api/paid/{agent_name}/info` | None | Payment requirements |
| POST/GET/DELETE | `/api/nevermined/agents/{name}/config` | JWT | Configure / get / remove payments |
| PUT | `/api/nevermined/agents/{name}/config/toggle` | JWT | Enable/disable |
| GET | `/api/nevermined/agents/{name}/payments` | JWT | Payment history |
| GET | `/api/nevermined/settlement-failures` | Admin | Failed settlements |
| POST | `/api/nevermined/retry-settlement/{log_id}` | Admin | Retry settlement |

### Outbound File Sharing (FILES-001)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/files/{file_id}` | Token (`?sig=`) | Public download: 401 bad/missing sig, 404 unknown id, 410 revoked/expired; `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`; per-IP rate limit; audit `file_share_download` |
| POST | `/api/internal/agent-files/share` | `X-Internal-Secret` | Agent-server path to mint a download URL |

### Sequential Agent Loops (#740)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/agents/{name}/loops` | JWT/MCP | Start loop; 202 with `{loop_id, status, agent_name, max_runs}`. Body: `message` (template), `max_runs` (1–100, required), `stop_signal`, `delay_seconds`, `timeout_per_run`, `model`, `allowed_tools` |
| GET | `/api/agents/{name}/loops` | JWT/MCP | List loops (`?status=`, `?limit=` 1–200 default 50) |
| GET | `/api/loops/{loop_id}` | JWT/MCP | Status + per-run summaries + last full response; 404 unknown, 403 if caller neither initiator nor agent-accessor |
| POST | `/api/loops/{loop_id}/stop` | JWT/MCP | Graceful stop → `{status: "stopping" \| "already_done"}` |

### Platform Settings
| Method | Path | Description |
|--------|------|-------------|
| GET/PUT/DELETE | `/api/settings/mcp-url` | Get (any auth user) / set / reset-to-auto-detect (admin-only) MCP server URL |
| GET | `/api/settings/feature-flags` | Public-safe UI gating flags (any auth user): `session_tab_enabled`, `voice_available` (`VOICE_ENABLED && GEMINI_API_KEY`), `workspace_available` (voice AND `WORKSPACE_ENABLED`, #860), `voip_available` (#1056), `mcp_agent_chat_pull_enabled` (#946 observability-only; routing gate is the MCP server's own `MCP_AGENT_CHAT_PULL_ENABLED`, default OFF), `enterprise_features` (registered enterprise modules; empty in OSS-only or `TRINITY_OSS_ONLY=1`) (#847) |
| GET/PUT | `/api/settings/agent-defaults/resources` | Fleet-wide default CPU/memory for new containers (admin-only; CPU 1/2/4/8/16, memory 1g–32g) (RES-001) |
| GET/PUT | `/api/settings/agent-defaults/access-policy` | Fleet-wide default `require_email` for new agents (admin-only, #1129). Stored in `system_settings`, **secure-by-default ON** (code fallback when unset — no migration); seeds `agent_ownership.require_email` at creation (`register_agent_owner`) for **new** agents only, never rewrites existing rows; owners still override per agent via `PUT /api/agents/{name}/access-policy` |

### Session Tab
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agents/{name}/session` | Create session row (first turn cold; writes JSONL so turn 2 resumes) |
| GET | `/api/agents/{name}/sessions` | List caller's sessions (per-user scoped; `?status=active`) |
| GET | `/api/agents/{name}/sessions/{id}` | Session row + most-recent `?limit=N` (default 100, max 500) messages |
| POST | `/api/agents/{name}/sessions/{id}/message` | The turn endpoint (`{message, model?, timeout_seconds?}`) — semantics in [Session Tab](#session-tab) |
| POST | `/api/agents/{name}/sessions/{id}/reset` | Clear cached UUID (next turn cold); best-effort JSONL reap |
| DELETE | `/api/agents/{name}/sessions/{id}` | Delete session + messages; best-effort JSONL reap |

### Enterprise Modules (#847)

Open-core seam (generic mechanism only). The public backend exposes an extension point: `main.py` conditionally `register_enterprise(app)` (no-op `ImportError` in OSS-only builds); each registered module calls `entitlement_service.register_module("<id>")`, and the registry drives `feature-flags → enterprise_features`, which the OSS Vue bundle reads to show/hide gated surfaces. `requires_entitlement("<id>")` in `dependencies.py` gates an entitled endpoint (403 unentitled; 404 when the submodule is absent). `TRINITY_OSS_ONLY=1` hard-empties the registry. Private enterprise tables migrate via the separate two-track runner (Invariant #3).

> The catalog of specific enterprise modules, their private schema, and the commercial rationale are intentionally **not** documented in this public repo — they live in the private `trinity-enterprise` repository (see `docs/memory/ENTERPRISE_DOCS.md` there). Public docs describe the generic seam only.

## Architectural Invariants

These are structural patterns that must be preserved. Breaking them causes cascading issues.

1. **Three-Layer Backend: Router → Service → DB** — Every feature follows `routers/X.py` → `services/X_service.py` → `db/X.py`. Routers hold no business logic, services hold no SQL, db modules hold no HTTP concerns.

2. **DB Layer: Class-per-domain with Mixin Composition** — Each `db/` file defines an `XOperations` class. Agent-specific settings use mixins (`db/agent_settings/`) composed into `AgentOperations`. New agent settings → new mixin, not a bigger class.

3. **Schema in `db/schema.py`, Migrations in `db/migrations.py`** — All OSS table DDL lives in `schema.py`. Schema changes require a versioned migration in `migrations.py` (tracked in the `schema_migrations` table). Never create tables ad-hoc in service code. **Runner safety (#1160):** `init_database()` wraps both migration passes + `init_schema` in a cross-process `flock` (`db/migration_lock.py`) so workers + scheduler can't race; table-rebuild migrations use `_atomic_rebuild` (rename-swap inside `BEGIN`/`COMMIT`) so a crash mid-rebuild rolls back; a failed migration is named via `add_note` and surfaced as `first_pending` in the `/health` 503. **Backend split (#1183):** the `db/migrations.py` runner (PRAGMA + `INSERT OR IGNORE`) is **SQLite-only**; PostgreSQL is owned by **Alembic** — `init_database()`'s non-SQLite branch calls `db/alembic_runner.upgrade_to_head()` (`src/backend/migrations/` + `alembic.ini`; `env.py` targets `db/tables.py` MetaData). Fresh PG built by the `0001_baseline` revision (reuses `init_schema_postgres` DDL); pre-Alembic PG stamped at baseline. Both coexist during transition, so a schema change lands in **both** `migrations.py` (SQLite) and a new Alembic revision (Postgres) — both tracks CI-guarded by the `schema-parity` job (SQLite parity pytest + the `scripts/ci/check_alembic_parity.py` cross-track guard that fails a DDL change missing its Alembic revision, #1342) — until SQLite **end-of-support September 1, 2026** (#1278; guide `docs/migrations/SQLITE_TO_POSTGRES.md`) — after which the goal (#746) is `tables.py` MetaData as single source with autogenerated revisions. **Two-track (open-core):** enterprise owns only `enterprise_*` tables via a **separate** runner (`enterprise/backend/_migrations.py`, tracked in `enterprise_schema_migrations`, never OSS `schema_migrations`); one file per migration (`NNNN_slug.py` with `NAME` + `upgrade(cursor, conn)`, filename order). Enterprise migrations may FK-into OSS tables but must **never ALTER** one — OSS enforcement goes through an OSS migration as an edition-agnostic primitive (e.g. `users.suspended_at`, #995). The enterprise runner runs from `register_enterprise` *after* OSS `init_database`.

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

14. **Pydantic Models Centralized in `models.py`** — Request/response models live in `models.py`, not scattered across routers (#654). Keeps the API contract in one place. **Scope:** this invariant governs **router** models — a `class X(BaseModel)` must not be defined under `routers/` (enforced by the static guard `tests/unit/test_models_centralized.py`). Two model homes are **intentionally separate** and out of scope: `db_models.py` (DB-row / persistence models — a distinct layer) and `adapters/base.py` (the ChannelAdapter ABC's `NormalizedMessage`/`ChannelResponse`). One documented exception, allowlisted in the guard: `routers/canary.py::RunCycleRequest` evaluates `INVARIANTS` (from the `canary` library) in a `Field(description=…)` at class-definition time, and the `canary` library imports `TaskExecutionStatus` back from `models` — relocating it would force `models.py` to `from canary import …`, inverting the dependency direction of a module meant to be a low-level leaf everything imports *from*.

15. **API URL Nesting Convention** — Agent-scoped resources nest under `/api/agents/{name}/...`. Platform-wide resources get top-level prefixes (`/api/executions`, `/api/operator-queue`).

16. **Time-Window SQL uses `iso_cutoff()`, not `datetime('now', ...)`** — Columns written via `utc_now_iso()` are ISO-Z strings (`T` separator, `Z` suffix); SQLite's `datetime('now', ...)` emits a different format (space separator, no suffix), making lexicographic comparison silently incorrect (#476). For rolling-window filters on ISO-Z TEXT columns, compute the cutoff in Python via `iso_cutoff(hours)` from `utils/helpers.py` and pass it as a bound parameter.

17. **Non-root containers** — every Trinity-built image MUST end with a `USER` directive switching to a non-root user; the backend additionally requires `group_add: ${DOCKER_GID:-999}` in compose for Docker socket access on Linux. New Dockerfiles failing this are rejected at review (#874). CI guards in `.github/workflows/container-security.yml` (path-filtered on `docker/**`, `docker-compose*.yml`, `scripts/deploy/start.sh`, `src/mcp-server/Dockerfile`, independent of the `ui`-gated e2e workflow): `verify-non-root` execs the backend/scheduler/mcp-server containers, asserts UID 1000, and proves `group_add` works by running `docker.from_env().ping()` from inside the backend (not a `/api/agents` probe — `list_all_agents_fast` swallows Docker errors, a false-positive trap); `verify-prod-frontend-uid` builds the prod frontend out-of-band and asserts UID 101 (`nginxinc/nginx-unprivileged`). Dev-only `docker/frontend/Dockerfile` is exempt. Upgrading deployments must re-own their data path and `agent-configs` volume per [docs/migrations/NON_ROOT_CONTAINERS_2026-05.md](../migrations/NON_ROOT_CONTAINERS_2026-05.md).

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

`suspended_at` (#995) is an edition-agnostic primitive: OSS owns the column AND its enforcement — `dependencies.get_current_user` rejects suspended users on both JWT and MCP-key paths, so setting it blocks new logins and invalidates live tokens on the next request. Only the enterprise `user_management` module exposes a setter (core-primitive + enterprise-knob pattern); OSS builds ship column + enforcement but no setter.

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
    voice_name TEXT,                               -- #28: persisted Gemini voice (NULL → 'Kore')
    guardrails_config TEXT,
    file_sharing_enabled INTEGER DEFAULT 0,        -- FILES-001
    circuit_breaker_enabled INTEGER DEFAULT 0,     -- RELIABILITY-007 (#526): dispatch-breaker opt-in
    deleted_at TEXT,                               -- #834: NULL = live; set = soft-deleted
    FOREIGN KEY (owner_id) REFERENCES users(id),
    FOREIGN KEY (subscription_id) REFERENCES subscription_credentials(id)
);

-- #834: partial index keeps the retention sweep cheap as the live agent count grows
CREATE INDEX idx_agent_ownership_deleted_at
    ON agent_ownership(deleted_at) WHERE deleted_at IS NOT NULL;
```

Soft-delete semantics: see [Soft Delete & Retention](#soft-delete-retention--recovery-834-772).

**agent_sharing** (cross-channel allow-list — same email admits the user on web, Telegram, and Slack):
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

**access_requests** (#311 — unified channel access control):
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

-- access_control migration also adds to telegram_chat_links:
--   verified_email TEXT, verified_at TEXT
```

Access-control flow (#311): `ChannelAdapter.resolve_verified_email()` maps native channel identity → verified email; `message_router` runs a single gate — owner/admin/`agent_sharing` → `open_access` → upsert pending `access_requests` row. Approval inserts into `agent_sharing`, whitelists the email (if email auth on), and fires a fire-and-forget notification on the requester's originating channel (telegram/slack/whatsapp only; bypasses `allow_proactive` and per-recipient rate limit — the user initiated the request; outcome audit-logged; delivery failure never rolls back the approval) (#951). Group chats bypass the gate; agents with both policy flags off retain legacy permissive behavior.

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
    model TEXT,                                  -- MODEL-001: override (NULL = agent default)
    timeout_seconds INTEGER,                     -- #913: NULL = inherit agent cap
    webhook_token TEXT,                          -- WEBHOOK-001: 43-char urlsafe token, nullable
    webhook_enabled INTEGER DEFAULT 0,           -- WEBHOOK-001
    deleted_at TEXT,                             -- #834: NULL = live; set = soft-deleted
    FOREIGN KEY (owner_id) REFERENCES users(id)
);

CREATE INDEX idx_agent_schedules_deleted_at
    ON agent_schedules(deleted_at) WHERE deleted_at IS NOT NULL;
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
    model_used TEXT,                             -- MODEL-001
    queued_at TEXT,                              -- BACKLOG-001: when task entered backlog
    backlog_metadata TEXT,                       -- BACKLOG-001: JSON identity/request for drain replay
    retry_count INTEGER DEFAULT 0,               -- #678: in-line auto-retry count (reader-race recovery)
    fan_out_id TEXT,                             -- FANOUT-001: parent fan-out operation ID
    loop_id TEXT,                                -- #740: parent agent_loops.id
    FOREIGN KEY (schedule_id) REFERENCES agent_schedules(id)
);

-- BACKLOG-001: partial index for cheap atomic FIFO claim
CREATE INDEX idx_executions_queued ON schedule_executions(agent_name, queued_at)
    WHERE status = 'queued';
-- #740: partial index for joining executions back to their parent loop
CREATE INDEX idx_executions_loop ON schedule_executions(loop_id)
    WHERE loop_id IS NOT NULL;
```

**agent_loops + agent_loop_runs** (#740 — see [Sequential Agent Loops](#sequential-agent-loops-740-ui-1106)):
```sql
CREATE TABLE agent_loops (
    id TEXT PRIMARY KEY,                         -- 'loop_<urlsafe>'
    agent_name TEXT NOT NULL,
    message_template TEXT NOT NULL,              -- supports {{run}} and {{previous_response}}
    max_runs INTEGER NOT NULL,                   -- 1–100 hard cap
    stop_signal TEXT,                            -- NULL = fixed mode; set = until mode
    delay_seconds INTEGER NOT NULL DEFAULT 0,
    timeout_per_run INTEGER,                     -- NULL = agent's execution_timeout_seconds
    max_duration_seconds INTEGER,                -- #1156: NULL = no wall-clock deadline (≤7d when set)
    model TEXT,
    allowed_tools TEXT,                          -- JSON array
    status TEXT NOT NULL,                        -- queued | running | completed | stopped | failed | interrupted
    runs_completed INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,                            -- max_runs_reached | stop_signal_matched | user_stopped | deadline_exceeded | error | interrupted
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
    response TEXT,                               -- full response for this iteration
    error TEXT,
    cost REAL,
    duration_ms INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (loop_id) REFERENCES agent_loops(id)
);
CREATE INDEX idx_loop_runs_loop ON agent_loop_runs(loop_id, run_number);
```

**agent_activities** (unified activity stream):
```sql
CREATE TABLE agent_activities (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    activity_type TEXT NOT NULL,            -- chat_start, chat_end, tool_call, schedule_start, schedule_end, agent_collaboration
    activity_state TEXT NOT NULL,           -- started, completed, failed
    parent_activity_id TEXT,                -- link to parent activity (tool → chat)
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

CREATE INDEX idx_activities_agent ON agent_activities(agent_name, created_at DESC);
CREATE INDEX idx_activities_type ON agent_activities(activity_type);
CREATE INDEX idx_activities_state ON agent_activities(activity_state);
CREATE INDEX idx_activities_user ON agent_activities(user_id);
CREATE INDEX idx_activities_parent ON agent_activities(parent_activity_id);
CREATE INDEX idx_activities_chat_msg ON agent_activities(related_chat_message_id);
CREATE INDEX idx_activities_execution ON agent_activities(related_execution_id);
```

Data strategy: `chat_messages.tool_calls` holds the aggregated JSON summary; `agent_activities` holds granular per-tool rows; observability fields (cost, context) live in `chat_messages`/`schedule_executions` only — activity queries JOIN for them.

**chat_sessions / chat_messages** (persistent chat — survives container restarts/deletions; auto-created per user+agent; access control: own messages only, admins all):
```sql
CREATE TABLE chat_sessions (
    id TEXT PRIMARY KEY,                  -- urlsafe token
    agent_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_email TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,      -- user + assistant
    total_cost REAL DEFAULT 0.0,
    total_context_used INTEGER DEFAULT 0,
    total_context_max INTEGER DEFAULT 200000,
    status TEXT DEFAULT 'active',         -- 'active' or 'closed'
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_chat_sessions_agent ON chat_sessions(agent_name);
CREATE INDEX idx_chat_sessions_user ON chat_sessions(user_id);
CREATE INDEX idx_chat_sessions_status ON chat_sessions(status);

CREATE TABLE chat_messages (
    id TEXT PRIMARY KEY,                  -- urlsafe token
    session_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,             -- denormalized for queries
    user_id INTEGER NOT NULL,
    user_email TEXT NOT NULL,
    role TEXT NOT NULL,                   -- 'user' or 'assistant'
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    cost REAL,                            -- assistant only (NULL for user)
    context_used INTEGER,
    context_max INTEGER,
    tool_calls TEXT,                      -- JSON array (assistant only)
    execution_time_ms INTEGER,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_chat_messages_session ON chat_messages(session_id);
CREATE INDEX idx_chat_messages_agent ON chat_messages(agent_name);
CREATE INDEX idx_chat_messages_user ON chat_messages(user_id);
CREATE INDEX idx_chat_messages_timestamp ON chat_messages(timestamp);
```

**agent_sessions / agent_session_messages** (Session tab — see [Session Tab](#session-tab)):
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
    cache_read_tokens INTEGER,                     -- prompt-cache hit observability across resume turns
    tool_calls TEXT,                               -- JSON
    execution_time_ms INTEGER,
    claude_session_id TEXT,                        -- per-message UUID Claude actually ran under (audit; changes on fallback/reset)
    FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX idx_agent_session_messages_session ON agent_session_messages(session_id);
CREATE INDEX idx_agent_session_messages_user ON agent_session_messages(user_id);
```

ON DELETE CASCADE is aspirational (`PRAGMA foreign_keys` is off platform-wide); `delete_session()` deletes child rows explicitly.

**agent_permissions** (agent-to-agent access — enforced at the MCP layer, see Auth section):
```sql
CREATE TABLE agent_permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_agent TEXT NOT NULL,           -- agent making calls
    target_agent TEXT NOT NULL,           -- agent being called
    granted_by TEXT NOT NULL,             -- user ID who granted permission
    created_at TEXT NOT NULL,
    UNIQUE(source_agent, target_agent),
    FOREIGN KEY (granted_by) REFERENCES users(id)
);
CREATE INDEX idx_agent_permissions_source ON agent_permissions(source_agent);
CREATE INDEX idx_agent_permissions_target ON agent_permissions(target_agent);
```

**agent_shared_folder_config** (shared folders — exposing agents publish a Docker volume at `/home/developer/shared-out`; consumers with `agent_permissions` mount it at `/home/developer/shared-in/{agent}`; container recreated on restart when mount config changes; volume ownership fixed to UID 1000):
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

**agent_shared_files** (FILES-001 — see [Outbound File Sharing](#outbound-file-sharing-files-001)):
```sql
CREATE TABLE agent_shared_files (
    id TEXT PRIMARY KEY,                  -- UUID
    agent_name TEXT NOT NULL,
    filename TEXT NOT NULL,               -- display name in download
    stored_filename TEXT NOT NULL,        -- UUID filename under /data/agent-files/
    size_bytes INTEGER NOT NULL,
    mime_type TEXT,                       -- python-magic detected
    download_token TEXT UNIQUE NOT NULL,  -- secrets.token_urlsafe(32), 192-bit
    created_by TEXT NOT NULL,             -- agent name (or user for admin-created)
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,             -- default 7d
    revoked_at TEXT,
    one_time INTEGER DEFAULT 0,           -- deferred: one-time link mode (column reserved)
    consumed_at TEXT,                     -- deferred
    download_count INTEGER DEFAULT 0,
    last_downloaded_at TEXT,
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
        ON DELETE CASCADE ON UPDATE CASCADE   -- aspirational; manual cascade per platform convention
);
CREATE INDEX idx_agent_files_agent ON agent_shared_files(agent_name);
CREATE INDEX idx_agent_files_token ON agent_shared_files(download_token);
CREATE INDEX idx_agent_files_expires ON agent_shared_files(expires_at) WHERE revoked_at IS NULL;
```

**agent_event_subscriptions / agent_events** (EVT-001 — agent event pub/sub):
```sql
CREATE TABLE agent_event_subscriptions (
    id TEXT PRIMARY KEY,
    subscriber_agent TEXT NOT NULL,       -- agent receiving events
    source_agent TEXT NOT NULL,           -- agent emitting events
    event_type TEXT NOT NULL,             -- namespaced event type
    target_message TEXT NOT NULL,         -- message template with {{payload.field}}
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

**slack_workspaces** (SLACK-002):
```sql
CREATE TABLE slack_workspaces (
    id TEXT PRIMARY KEY,
    team_id TEXT UNIQUE NOT NULL,          -- Slack workspace team ID
    team_name TEXT,
    bot_token TEXT NOT NULL,               -- AES-256-GCM JSON envelope of OAuth token
    connected_by TEXT,
    connected_at TEXT NOT NULL,
    enabled INTEGER DEFAULT 1
);
```

`bot_token` is a TEXT column whose contents are an AES-256-GCM JSON envelope (not renamed for backward compatibility); the read path in `db/slack_channels.py:_decrypt_token` handles both encrypted and legacy plaintext (`xoxb-*`) values, and plaintext rows are re-encrypted on next backend restart by the `slack_bot_token_encryption` migration (#453).

**slack_link_connections** (SLACK-001 — one Slack workspace = one public link = one agent; coexists with `slack_workspaces` (SLACK-002 multi-agent routing) — different products, different OAuth installations possible):
```sql
CREATE TABLE slack_link_connections (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL UNIQUE,          -- FK to agent_public_links
    slack_team_id TEXT NOT NULL UNIQUE,
    slack_team_name TEXT,
    slack_bot_token TEXT NOT NULL,         -- AES-256-GCM JSON envelope (same pattern as slack_workspaces.bot_token)
    connected_by TEXT NOT NULL,
    connected_at TEXT NOT NULL,
    enabled INTEGER DEFAULT 1
);
```

**slack_channel_agents / slack_active_threads** (SLACK-002):
```sql
CREATE TABLE slack_channel_agents (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,                 -- FK to slack_workspaces.team_id
    slack_channel_id TEXT NOT NULL,
    slack_channel_name TEXT,
    agent_name TEXT NOT NULL,
    is_dm_default INTEGER DEFAULT 0,       -- 1 = default agent for DMs
    created_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(team_id, slack_channel_id)
);

CREATE TABLE slack_active_threads (
    team_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,               -- Slack thread timestamp
    agent_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(team_id, channel_id, thread_ts)
);
```

**whatsapp_bindings / whatsapp_chat_links** (WHATSAPP-001 — one Twilio sender per agent, owner brings their own Twilio account; webhook verification dual-factor: URL `webhook_secret` + HMAC-SHA1; Sandbox auto-detected from well-known sender `whatsapp:+14155238886`; DMs only — Twilio's WhatsApp API has no groups; `verified_email`/`verified_at` shipped up-front so #311 Phase 2 is additive):
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
    verified_email TEXT,                       -- #311 Phase 2
    verified_at TEXT,
    message_count INTEGER DEFAULT 0,
    last_active TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(binding_id, wa_user_phone)
);
CREATE INDEX idx_whatsapp_chat_links_binding ON whatsapp_chat_links(binding_id);
```

**operator_queue** (OPS-001):
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
    cleared_at TEXT,                    -- #1017: NULL = visible; set = hidden by Clear All (deletion deferred to retention sweep #1142)
    FOREIGN KEY (responded_by_id) REFERENCES users(id)
);
CREATE INDEX idx_operator_queue_agent ON operator_queue(agent_name);
CREATE INDEX idx_operator_queue_status ON operator_queue(status);
CREATE INDEX idx_operator_queue_priority ON operator_queue(priority);
CREATE INDEX idx_operator_queue_type ON operator_queue(type);
CREATE INDEX idx_operator_queue_created ON operator_queue(created_at DESC);
```

**agent_sync_state** (#389 — see [Git Sync Health](#git-sync-health-389390)):
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

**audit_log** (SEC-001 — append-only at the database layer):
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
    previous_hash TEXT,                    -- hash chain
    entry_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_audit_log_timestamp ON audit_log(timestamp DESC);
CREATE INDEX idx_audit_log_event_type ON audit_log(event_type, timestamp DESC);
CREATE INDEX idx_audit_log_actor ON audit_log(actor_type, actor_id, timestamp DESC);
CREATE INDEX idx_audit_log_target ON audit_log(target_type, target_id, timestamp DESC);
CREATE INDEX idx_audit_log_mcp_key ON audit_log(mcp_key_id, timestamp DESC);
CREATE INDEX idx_audit_log_request ON audit_log(request_id);

-- Append-only enforcement
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'Audit log entries cannot be modified'); END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
WHEN OLD.timestamp > datetime('now', '-365 days')
BEGIN SELECT RAISE(ABORT, 'Audit log entries cannot be deleted within retention period'); END;
```

**canary_violations** (CANARY-001 — one row per fired check per cycle; `observed_state` carries invariant-specific JSON; append-only in practice — no UPDATE/DELETE in the read API):
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

**idempotency_keys** (RELIABILITY-006 — see [Idempotency](#idempotency-reliability-006-525) and Invariant #18):
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

**agent_compatibility_results** (#668 — see [Agent Compatibility Validation](#agent-compatibility-validation-668)). Latest-snapshot-per-agent (one row, upserted by `agent_name`); STATIC recomputes live, persisted AI verdicts merge in. Dual-track migration (SQLite `db/migrations.py` + Alembic `migrations/versions/0003_*`); cascade/rename via `AGENT_REFS`:
```sql
CREATE TABLE agent_compatibility_results (
    agent_name TEXT PRIMARY KEY,
    overall_status TEXT NOT NULL,        -- compatible | issues | unavailable
    checks_json TEXT NOT NULL,           -- full last report's check list (JSON)
    hard_count INTEGER NOT NULL DEFAULT 0,
    soft_count INTEGER NOT NULL DEFAULT 0,
    info_count INTEGER NOT NULL DEFAULT 0,
    container_running INTEGER NOT NULL DEFAULT 0,
    ai_ran_at TEXT,                      -- last AI evaluation (NULL = never)
    static_ran_at TEXT,
    updated_at TEXT NOT NULL
);
```

### Redis

- **Credential storage (DEPRECATED — CRED-002)**: credentials moved to encrypted files in agent workspaces (`.env` + `.credentials.enc`); legacy keys (`credentials:{id}:*`, `user:{id}:credentials`, `agent:{name}:credentials`) kept for backward compatibility only.
- **OAuth state**: `oauth_state:{state}` → `{provider, redirect_uri, user_id}`.
- **Heartbeat keys**: see [Heartbeat Liveness](#heartbeat-liveness-reliability-004-307). All heartbeat ops are within the backend Redis ACL (`-@dangerous`) and follow the `agent:*` naming convention.
- **Capacity/breaker keys**: `agent:slots:{name}` (ZSET) + `agent:slot:{name}:{eid}` (HASH), `agent:circuit:{name}`, `agent:dispatch:{name}`, `canary:drain_tick_at` — see the respective subsystem blocks.
- **Session tab keys**: `session_lock:*`, `session_inflight:*` — see [Session Tab](#session-tab).
- **Compatibility fix lock**: `compat_fix:{name}` (SET NX, 30s TTL) serialises the per-agent gitignore auto-fix read-modify-write (#668).

---

## Authentication & Authorization Architecture

### 1. User Authentication (Human → Platform)

| Mode | Flow | Token |
|------|------|-------|
| **Email** (primary) | Email → 6-digit code → `POST /api/auth/email/verify` | JWT with `mode: "email"` |
| **Admin** (secondary) | Password → `POST /api/token` | JWT with `mode: "admin"` |

- Email whitelist controls who can login via email; admin login always available for 'admin'.
- **4-tier role hierarchy** (ROLE-001): `user` < `operator` < `creator` < `admin`. Agent creation requires `creator`+. Enforced via `require_role()` in `dependencies.py`.
- **Whitelist-driven role on first login** (#314): new email users inherit the `default_role` on their `email_whitelist` row (fallback `user`). Callsites pass explicit intent — `/share` and access-request approvals → `user` (chat-only grant); public `/api/access/request` self-signup → `user`; admin whitelist UI → caller-specified. Owners promote collaborators explicitly via `PUT /api/users/{username}/role`. Closes a privilege escalation where any access grant silently promoted the recipient to `creator`.
- **Public self-signup is default-OFF** (trinity-enterprise#10): the unauthenticated `POST /api/access/request` returns **403** unless an operator opts in via `PUBLIC_ACCESS_REQUESTS_ENABLED` (env) or the `public_access_requests_enabled` system setting. When off it never auto-whitelists, so the email whitelist stays authoritative against self-enrollment. Login-code requests for already-whitelisted emails are unaffected.

### 2. MCP API Keys (User → MCP Server)

Created via UI `/settings?tab=mcp-keys`; format `trinity_mcp_{random}` (44 chars); SHA-256 hash stored in SQLite; sent as `Authorization: Bearer trinity_mcp_...`; MCP server validates via `POST /api/mcp/validate`.

Client config (`.mcp.json`):
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

The FastMCP `authenticate` callback validates the user's key via the backend and returns the `McpAuthContext`; MCP tools then call the backend API with the user's own key — the backend's `get_current_user()` accepts JWT OR MCP API key. In production (`MCP_REQUIRE_API_KEY=true`) the MCP server holds NO admin credentials.

### 4. Agent MCP Keys (Agent → Trinity MCP)

Each agent gets an auto-generated agent-scoped key (`scope='agent'`, `agent_name` stored for permission checks), injected as `TRINITY_MCP_API_KEY` env var and auto-added to the agent's `.mcp.json` pointing at the internal URL `http://mcp-server:8080/mcp`.

### 5. Agent-to-Agent Permissions

Enforced at the **MCP server layer** (`src/mcp-server/src/tools/`), not the backend REST API: `list_agents` returns only permitted agents + self; `chat_with_agent` blocks non-permitted targets. The backend resolves agent-scoped keys to the owner user and applies standard ownership/sharing checks (`current_user.agent_name` is used only by notifications and event subscriptions). **Restrictive default**: new agents start with zero permissions; grants are explicit via the Permissions tab (`agent_permissions` table). Not a bypass risk in practice because agents communicate via MCP, not direct REST.

### 6. System Agent

`trinity-system` has `scope='system'`: bypasses all permission checks, can call any agent/tool, cannot be deleted via API. Purpose: platform operations (health, costs, fleet management).

| Scope | MCP Enforcement | Backend Enforcement |
|-------|-----------------|---------------------|
| `user` | Owner/admin/shared checks | Owner/admin/shared checks |
| `agent` | Explicit permission list (`agent_permissions`) | Resolves to owner user; ownership/sharing checks only |
| `system` | **Bypasses all checks** | Resolves to owner user (system agent owner) |

### 7. External Credentials (Agent → External Services)

CRED-002 file-injection model (Invariant #12): `.env` (KEY=VALUE source of truth) + `.mcp.json` edited directly; encrypted backup `.credentials.enc` (AES-256-GCM, safe for git); auto-import on startup if `.credentials.enc` exists without `.env`. Flow: Quick Inject writes `.env` → Export encrypts to `.credentials.enc` → agent start decrypts and writes files. OAuth providers for agent credentials: Google, Slack, GitHub (PAT), Notion. Common MCP servers inside agents: google-workspace, slack, notion, github, n8n-mcp.

---

## Network Topology (#589)

Two Docker bridge networks, by design — agents physically cannot route to Redis.

| Network | Subnet | Members |
|---------|--------|---------|
| `trinity-platform-network` | 172.29.0.0/16 | redis, scheduler, vector |
| `trinity-agent-network` | 172.28.0.0/16 | agents, frontend |

Bridges (members of **both** networks): `backend` (primary HTTP API — Redis on platform side, agents on agent side), `mcp-server` (agents reach `http://mcp-server:8080/mcp` via Docker DNS), `otel-collector` (agents push metrics), `cloudflared` (prod only — proxies to backend and public agents).

**Rule:** agents are *never* on `trinity-platform-network`. Any new service that mounts the agent network must NOT connect to Redis — full stop. The agent-creation sites (`services/agent_service/crud.py:583`, `services/agent_service/lifecycle.py:495`, `services/system_agent_service.py:238`) hard-code the network name `trinity-agent-network`.

**Redis ACL users:**

| User | Auth | Purpose |
|------|------|---------|
| `default` | `REDIS_PASSWORD` | Admin / recovery / ad-hoc ops; `+@all` |
| `backend` | `REDIS_BACKEND_PASSWORD` | Backend runtime; data ops only, `-@dangerous` |
| `scheduler` | `REDIS_BACKEND_PASSWORD` | Scheduler runtime; same access pattern |

`backend`/`scheduler` cannot run `FLUSHALL`, `CONFIG`, `SHUTDOWN`, `DEBUG`, `MIGRATE`, `REPLICAOF`, `MONITOR`, or other `@dangerous` categories. Both passwords are mandatory in `.env`; compose refuses to render without them, and `src/backend/config.py` / `src/scheduler/config.py` raise on import if `REDIS_URL` lacks credentials. Upgrade path: `docs/migrations/REDIS_AUTH.md`.

---

## Container Security

- **Non-root execution** (Invariant #17, #874): backend and scheduler as `trinity` (UID 1000), MCP server as `node` (UID 1000), frontend as `nginx` (UID 101), agents as `developer` (UID 1000). Backend needs `group_add: ${DOCKER_GID:-999}` for Docker socket access on Linux.
- `CAP_DROP: ALL` + `CAP_ADD: NET_BIND_SERVICE`; `security_opt: no-new-privileges:true`; tmpfs `/tmp` with `noexec,nosuid` (RAM-backed, default 512 MB — operator-tunable via `AGENT_TMP_SIZE` on the backend service, validated `^\d+[mg]$` with invalid→default; `noexec,nosuid` stay fixed; counts against the agent memory cgroup; creation-time, so existing agents pick up a change on recreate not restart, #1231. Heavy scratch like pip/npm/ML wheels is redirected via a default `TMPDIR=/home/developer/.tmp` on the disk-backed home volume, created at start by `startup.sh`; mount spec + TMPDIR default live in `services/agent_service/capabilities.py` so create/recreate/system-agent can't drift, #1098); no external UI port exposure; network isolation per Network Topology above.
- **Internal API security (C-003)**: `/api/internal/` endpoints (scheduler, agent containers) require the `X-Internal-Secret` header; falls back to `SECRET_KEY` if `INTERNAL_API_SECRET` unset.
- **Agent-server inbound auth (#1159)** (details in [agent-server-authentication.md](feature-flows/agent-server-authentication.md)): every backend→agent call carries a per-agent `X-Trinity-Agent-Token` = `HMAC-SHA256(AGENT_AUTH_SECRET, "trinity-agent-auth:v1:"+name)` — *derived*, not stored; the master lives only in backend env, so a compromised agent can't compute a sibling's token. A **pure-ASGI** middleware (`docker/base-image/agent_server/middleware/auth.py`) enforces it on **all** HTTP **and** WS routes via constant-time compare, exempting only exact `/health` (+ `OPTIONS`) — pure-ASGI (not `BaseHTTPMiddleware`) so it gates WS scopes too and never buffers SSE. The dead unauthenticated `/ws/chat` route (ran arbitrary Claude) was removed; CORS dropped (internal-only). Grace path: empty `TRINITY_AGENT_AUTH_TOKEN` → allow (old-image); `check_agent_auth_token_env_matches` forces a one-pass recreate so a missing/stale token re-injects. Backend fail-closed (`derive_agent_token` raises on empty secret; `start.sh` auto-generates the hex32 master, both compose files forward it). Callers route through `services/agent_auth.py`; a static guard (`tests/unit/test_agent_auth_header_guard.py`) fails any raw `agent-{name}:8000` caller that skips them.
- **WebSocket security (C-002, #550)**: single-use ticket auth — see [Real-time Delivery](#real-time-delivery-reliability-003-306).
- **Frontend XSS (H-005)**: all markdown rendering uses DOMPurify via `utils/markdown.js`; no direct `v-html` with unsanitized content.
- **Rate limiting (#1023)**: shared sliding-window limiter `services/rate_limiter.py` — Redis sorted-set rolling window (no fixed-window boundary burst), fail-open with bounded per-worker in-process fallback; `enforce(key, limit, window)` raises 429 + `Retry-After`. New request-rate limits reuse this primitive — don't hand-roll Redis counters. Intentionally NOT unified under it: the auth login/OTP limiters in `routers/auth.py` are failure-counters (increment on failure, reset on success) — a different pattern. A global ASGI middleware with a route→policy table is a tracked follow-up.

---

## Development Environment

Local and production use the same ports. Local URLs, auth, and admin credentials: see `CLAUDE.md` / `CLAUDE.local.md`.

| Port | Service |
|------|---------|
| 80 | Frontend (nginx/Vite) — prod: `https://your-domain.com` |
| 8000 | Backend (FastAPI) — `/docs` for OpenAPI |
| 8080 | MCP Server (`/mcp`) |
| 8686 | Vector health |
| 2222–2262 | Agent SSH |

---

## Data Persistence

- **Bind mount** (survives `docker-compose down -v`): `~/trinity-data/` → `/data` — contains `trinity.db` (SQLite), `agent-files/` (FILES-001), and `agent-data-tmp/` (transient export staging, #1169).
- **Docker volumes**: `redis-data` (Redis AOF), `agent-configs`, `audit-data`, `audit-logs`, per-agent `agent-{name}-workspace` (the durable home volume — declared `data_paths` runtime data lives under `/home/developer/data` here, #1169), `agent-{name}-public` (FILES-001), and shared-folder volumes.
