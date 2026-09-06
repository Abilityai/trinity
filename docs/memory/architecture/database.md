# Trinity Architecture — Schema, migrations, Redis keyspaces

> Part of the Trinity architecture set. Core map, invariants and topology: [architecture.md](../architecture.md). This file is **not** auto-loaded.
>
> **Owns**: `src/backend/db/schema.py`, `src/backend/db/migrations.py`, `src/backend/db/tables.py`, `src/backend/migrations/versions/**`, `src/backend/db/alembic_runner.py`
>
> **Read this before changing the paths above**: Every schema change needs BOTH tracks: a versioned SQLite entry and a new Alembic revision (Invariant #9). Two revisions sharing a `down_revision` are two heads, and `upgrade head` resolves its target before applying anything, so it applies ZERO revisions while git reports no conflict (#2068).
>
> **Write path**: changes to this area land here, not in the core (core editorial rule 4). Keep the core's map row in step if the owned paths change.

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
    suspended_at TEXT,                  -- #995: NULL = active; set = deactivated
    github_pat_encrypted TEXT           -- ent#162: per-user GitHub PAT (AES-256-GCM envelope; NULL = none)
);
```

`github_pat_encrypted` (ent#162) is a per-user GitHub credential: a non-admin stores one token in their own settings (`GET`/`PUT`/`DELETE /api/users/me/github-pat`, self-service, token never echoed on read) and agent creation resolves **per-agent → owner's per-user (live) → global** via `services/settings_service.resolve_github_pat(agent_name, owner_id)`, so a user is no longer confined to the admin PAT's repo scope. The resolved value is persisted as the agent's per-agent PAT (#347) **only** when it came from the per-user or fork tier — never the global fallback (a global-fallback agent keeps this NULL so `github_pat_propagation_service` still reaches it on admin rotation). The recreate/restart PAT ladder (`settings_service.get_github_pat_for_agent`) stays 2-tier (per-agent → global) and never re-derives the per-user tier, so adding a personal token in Settings cannot force-recreate a running agent. OSS-core.

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
    public_channel_model TEXT,                     -- #894: per-agent model for public channels (NULL → platform default)
    public_channel_system_prompt TEXT,             -- #1205: public/channel-only custom-instructions fragment
    guardrails_config TEXT,
    file_sharing_enabled INTEGER DEFAULT 0,        -- FILES-001
    circuit_breaker_enabled INTEGER DEFAULT 0,     -- RELIABILITY-007 (#526): dispatch-breaker opt-in
    mcp_exposed INTEGER DEFAULT 0,                 -- #846: dedicated chat_with_<slug> MCP tool opt-in
    operator_resume_enabled INTEGER DEFAULT 0,     -- ent#329: an operator answer re-triggers the agent (owner opt-in; each answer costs a turn)
    a2a_exposed INTEGER DEFAULT 0,                 -- ent#157: A2A inbound-server exposure opt-in (default OFF)
    tts_voice_replies_enabled INTEGER DEFAULT 0,   -- epic #24/#25: outbound voice replies (shared agent-level)
    tts_voice_id TEXT,                             -- epic #24/#25: ElevenLabs voice id for spoken replies
    tts_voice_telegram_enabled INTEGER DEFAULT 1,  -- ent#117: per-channel voice-allowed flag
    tts_voice_slack_enabled INTEGER DEFAULT 1,     -- ent#117: per-channel voice-allowed flag
    tts_voice_whatsapp_enabled INTEGER DEFAULT 1,  -- ent#117: per-channel voice-allowed flag
    deleted_at TEXT,                               -- #834: NULL = live; set = soft-deleted
    is_ephemeral INTEGER DEFAULT 0,                -- trinity-enterprise#69: 1 = ghost (budgeted, hard-discarded)
    ephemeral_max_executions INTEGER,              -- trinity-enterprise#69: NULL = no exec budget
    ephemeral_expires_at TEXT,                     -- trinity-enterprise#69: ALWAYS set for ghosts; doubles as discard-intent marker
    spawned_by_agent TEXT,                         -- trinity-enterprise#69 Part 2: parent agent name (provenance)
    spawned_by_key_id TEXT,                        -- trinity-enterprise#69 Part 2: parent MCP key id (stable identity)
    volume_base_name TEXT,                         -- #1664: base name of the agent's data volumes (NULL = agent_name);
                                                   -- pinned at rename (volumes keep the old name), frozen across re-renames
    FOREIGN KEY (owner_id) REFERENCES users(id),
    FOREIGN KEY (subscription_id) REFERENCES subscription_credentials(id)
);

-- #834: partial index keeps the retention sweep cheap as the live agent count grows
CREATE INDEX idx_agent_ownership_deleted_at
    ON agent_ownership(deleted_at) WHERE deleted_at IS NOT NULL;
```

Soft-delete semantics: see [Soft Delete & Retention](reliability.md#soft-delete-retention--recovery-834-772).

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
    scope TEXT DEFAULT 'user',       -- user | agent | system | connector | portal_delegate
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
    webhook_secret_encrypted TEXT,               -- ent#77: AES-256-GCM HMAC signing secret (Invariant #12), nullable
    webhook_auth_enabled INTEGER DEFAULT 0,      -- ent#77: gate signature verification in the public trigger
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
    claim_token TEXT,                            -- #1081 Phase 0 (dark): pull-worker lease CAS token (NULL on push/#1083 rows)
    lease_expires_at TEXT,                       -- #1081 Phase 0 (dark): ISO-Z lease deadline; non-NULL ⇒ owned by the lease-reaper
    claimed_by_worker TEXT,                      -- #1081 Phase 0 (dark): opaque pull-worker identity that holds the lease
    redelivery_count INTEGER DEFAULT 0,          -- #1081 Phase 3 (#429/#1402): lease-reaper re-delivery count (distinct from retry_count)
    source_channel TEXT,                         -- ent#117: originating channel (telegram|slack|whatsapp) for voice-reply delivery;
                                                 -- also 'portal' (#2157) — a surface stamp, NOT a delivery leg (no chat id)
    source_channel_chat_id TEXT,                 -- ent#117: channel destination (chat/channel id)
    source_channel_thread TEXT,                  -- ent#117: channel thread id (nullable)
    source_channel_agent TEXT,                   -- ent#265: binding-agent for channel report-back (NULL = executing agent)
    turn_integrity TEXT,                         -- #2467: JSON turn-integrity flags (background_tasks_killed records +
                                                 -- background_tasks_pending_at_exit), derived backend-side at terminal write;
                                                 -- NULL = no evidence, never "verified healthy"
    FOREIGN KEY (schedule_id) REFERENCES agent_schedules(id)
);

-- BACKLOG-001: partial index for cheap atomic FIFO claim
CREATE INDEX idx_executions_queued ON schedule_executions(agent_name, queued_at)
    WHERE status = 'queued';
-- #740: partial index for joining executions back to their parent loop
CREATE INDEX idx_executions_loop ON schedule_executions(loop_id)
    WHERE loop_id IS NOT NULL;
```

**agent_loops + agent_loop_runs** (#740 — see [Sequential Agent Loops](execution.md#sequential-agent-loops-740-ui-1106)):
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
    max_cost_usd REAL,                           -- #1155: NULL = no cost budget (gt=0 when set)
    no_progress_threshold INTEGER,               -- #1157: NULL = disabled (legacy); 0 = off; ≥2 = stop after K identical responses
    on_failure TEXT NOT NULL DEFAULT 'abort',    -- #1167: abort (fail-fast) | continue (tolerate failed iterations)
    max_consecutive_failures INTEGER NOT NULL DEFAULT 3,  -- #1167: continue-mode cutoff (1–100)
    model TEXT,
    allowed_tools TEXT,                          -- JSON array
    status TEXT NOT NULL,                        -- queued | running | completed | completed_with_errors | stopped | failed | interrupted
    runs_completed INTEGER NOT NULL DEFAULT 0,
    failed_runs INTEGER NOT NULL DEFAULT 0,      -- #1167: tolerated-failure count (continue mode)
    stop_reason TEXT,                            -- max_runs_reached | stop_signal_matched | user_stopped | deadline_exceeded | budget_exhausted | no_progress | max_consecutive_failures | error | interrupted
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

**agent_reminders** (#1296 — see [Agent Self-Reminders](execution.md#agent-self-reminders-1296)). Durable
one-shot deferred self-trigger; the standalone scheduler arms a `DateTrigger` per pending row.
Dual-track migration (SQLite `agent_reminders_table` + Alembic `0028_agent_reminders`); `AGENT_REFS`
CASCADE (rename cascade + purge). Status: `pending → firing → fired` / `firing → pending|failed` /
`pending → cancelled`:
```sql
CREATE TABLE agent_reminders (
    id TEXT PRIMARY KEY,                        -- 'rem_<hex>'
    agent_name TEXT NOT NULL,                   -- target == source (self-reminder)
    message TEXT NOT NULL,
    fire_at TEXT NOT NULL,                      -- ISO-Z absolute (relative delay resolved at create)
    status TEXT NOT NULL DEFAULT 'pending',     -- pending | firing | fired | cancelled | failed
    model TEXT,                                 -- optional override
    timeout_seconds INTEGER,                    -- optional; clamped to agent cap at create
    allowed_tools TEXT,                         -- optional JSON array
    owner_id INTEGER,                           -- resolved owner (provenance)
    created_by_email TEXT,                      -- denormalized owner email (provenance)
    source_agent_name TEXT,                     -- the agent that set it (provenance)
    source_mcp_key_id TEXT,                     -- MCP key id that set it (provenance)
    execution_id TEXT,                          -- latest fire attempt's execution row
    fire_attempts INTEGER NOT NULL DEFAULT 0,   -- at-least-once retry counter (≤ MAX_REMINDER_FIRE_ATTEMPTS)
    firing_at TEXT,                             -- in-flight fire start (stale-firing reclaim threshold)
    error TEXT,                                 -- last-attempt failure detail
    created_at TEXT NOT NULL,
    fired_at TEXT,
    cancelled_at TEXT
);
CREATE INDEX idx_agent_reminders_agent ON agent_reminders(agent_name);
-- Partial index covers BOTH the pending-scan and the stale-firing reclaim.
CREATE INDEX idx_agent_reminders_active ON agent_reminders(fire_at)
    WHERE status IN ('pending', 'firing');
```

**agent_activities** (unified activity stream):
```sql
CREATE TABLE agent_activities (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    activity_type TEXT NOT NULL,            -- chat_start, chat_end, tool_call, schedule_start, schedule_end, agent_collaboration
    activity_state TEXT NOT NULL,           -- started, completed, failed, cancelled (#1332)
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

**chat_sessions / chat_messages** (persistent chat — survives container restarts/deletions; auto-created per user+agent; access control: own messages only, admins all). The authenticated Chat tab's `/task` writer (`chat_persistence_service.py::persist_chat_session`, extracted from `routers/chat.py` by #1483; shared by the sync + async branches; guarded on a SUCCESS terminal) is **fail-loud** (#1444): a persistence error logs at ERROR with a stack trace (message carries agent + execution_id + exc-type only, and the SQLAlchemy engine sets `hide_parameters=True` in `db/engine.py` so a DB-error traceback can't leak bound values either — no user content in message or trace) and never re-raises past a completed, billed turn — the sync branch surfaces a `chat_persist_failed` marker on the response. A caller-supplied `chat_session_id` is **owner-checked** (`session.user_id == caller`) before appending (closes an IDOR); on mismatch the write falls through to the caller's own session. The in-process path is the only persister — the #1083 fire-and-forget callback path is structurally unreachable by a manual `/task` (`ASYNC_DISPATCH_ELIGIBLE_TRIGGERS` = `{schedule, webhook}`), so callback-path persistence is deferred to the pull-mode epic. Schema:
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

**agent_sessions / agent_session_messages** (per-platform-user resumable sessions — see [Resumable Turns](execution.md#resumable-turns)):
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

**agent_shared_files** (FILES-001 — see [Outbound File Sharing](integrations.md#outbound-file-sharing-files-001)):
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
    event_type TEXT NOT NULL,             -- agent-emitted, OR backend-emitted 'agent.task.completed'/'agent.task.failed' (#1578)
    payload TEXT,                         -- JSON
    subscriptions_triggered INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
```

`agent_events` rows are written by **agent-emitted** `emit_event` (EVT-001) AND by the **system-emitted** task-completion emitter (#1578), which persists a row ONLY when a matching subscription exists — see [Task Completion Events](execution.md#task-completion-events-1578).

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
    id TEXT PRIMARY KEY,               -- #1631: platform-minted uuid4().hex (global row handle)
    agent_name TEXT NOT NULL,
    request_id TEXT,                   -- #1631: agent-authored correlation string; UNIQUE(agent_name, request_id)
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
    cleared_at TEXT,                    -- #1017: NULL = visible; set = hidden by Clear All (rows deleted by the #1142 retention sweep past operator_queue_retention_days)
    addressed_to_email TEXT,            -- ent#364: the human this ask is for; NULL = operator ask. Validated at ingestion against the agent's roster, never trusted from the payload
    FOREIGN KEY (responded_by_id) REFERENCES users(id)
);
CREATE INDEX idx_operator_queue_agent ON operator_queue(agent_name);
CREATE INDEX idx_operator_queue_status ON operator_queue(status);
CREATE INDEX idx_operator_queue_priority ON operator_queue(priority);
CREATE INDEX idx_operator_queue_type ON operator_queue(type);
CREATE INDEX idx_operator_queue_created ON operator_queue(created_at DESC);
-- #1631: per-agent uniqueness on the agent-authored correlation string
CREATE UNIQUE INDEX idx_operator_queue_agent_request ON operator_queue(agent_name, request_id);
```

**agent_sync_state** (#389 — see [Git Sync Health](agent-lifecycle.md#git-sync-health-389390)):
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
    git_dir_bytes INTEGER,                 -- #1596: agent .git on-disk size (bloat curve)
    pack_count INTEGER,                    -- #1595: packs from `git count-objects -v`
    loose_objects INTEGER,                 -- #1595: loose objects (gc-health signal)
    maintenance_failures INTEGER DEFAULT 0, -- #1595: consecutive failed maintenance attempts
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
    mcp_key_id TEXT,                       -- #2323: populated for EVERY key-authenticated
                                           -- call, not just MCP tool calls (see below)
    mcp_key_name TEXT,
    mcp_scope TEXT,                        -- user | agent | system | connector | portal_delegate | ops
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

**idempotency_keys** (RELIABILITY-006 — see [Idempotency](execution.md#idempotency-reliability-006-525) and Invariant #18):
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

**agent_compatibility_results** (#668 — see [Agent Compatibility Validation](agent-lifecycle.md#agent-compatibility-validation-668)). Latest-snapshot-per-agent (one row, upserted by `agent_name`); STATIC recomputes live, persisted AI verdicts merge in. Dual-track migration (SQLite `db/migrations.py` + Alembic `migrations/versions/0003_*`); cascade/rename via `AGENT_REFS`:
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

**agent_reports** (#918 — see [Agent Reports](observability.md#agent-reports-918)). Dual-track migration
(SQLite `agent_reports_table` + Alembic `0006_agent_reports`). `user_id` = the MCP-key/JWT
owner who authored the report (not necessarily the agent owner):
```sql
CREATE TABLE agent_reports (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    user_id INTEGER,                     -- author = MCP-key owner (current_user.id)
    report_type TEXT NOT NULL,           -- namespaced, e.g. 'recon.weekly_summary'
    title TEXT NOT NULL,
    payload TEXT NOT NULL,               -- arbitrary JSON, ≤5 MiB (413 over cap, #1537)
    display_hint TEXT,                   -- table|kpi|markdown|timeline|json|NULL
    schema_version INTEGER DEFAULT 1,
    period_start TEXT,
    period_end TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX idx_agent_reports_agent   ON agent_reports(agent_name, created_at DESC);
CREATE INDEX idx_agent_reports_type    ON agent_reports(report_type, created_at DESC);
CREATE INDEX idx_agent_reports_created ON agent_reports(created_at);  -- retention sweep
```

**subscription_headroom_history** (ent#433 — the durable half of #471's live headroom).
One row per probe, so utilization trends survive a Redis snapshot that overwrites itself on
every probe. Written only by `_probe_and_store` (off-loop, after the Redis write, fail-open);
read as a bounded `last`-per-bucket series. Dual-track migration (SQLite
`subscription_headroom_history_table` + Alembic `0043_subscription_headroom_history`).
Subscription-keyed, so the `AGENT_REFS` agent cascade does not apply; the subscription cascade
is performed **explicitly** inside `delete_subscription`'s transaction, because the DDL's
`ON DELETE CASCADE` is decorative (`PRAGMA foreign_keys` is off platform-wide and
`_PG_TABLE_SUBS` strips every FK clause before the DDL reaches PostgreSQL — hence the Alembic
revision deliberately declares **no** constraint, which would otherwise be the platform's first
enforced FK and would diverge the backends on the in-flight-probe delete race):
```sql
CREATE TABLE subscription_headroom_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- SERIAL on PostgreSQL
    subscription_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,               -- ISO-Z; stamped at probe START (pre-HTTP)
    status TEXT NOT NULL,                   -- ok | rate_limited | invalid_token | error | no_windows
    five_hour_utilization_pct REAL,         -- NULL-able INDEPENDENTLY of status: a 429 reports
    five_hour_resets_at TEXT,               --   a window status with no figure, so a reader that
    five_hour_status TEXT,                  --   coerces NULL to 0 inverts its most important sample
    seven_day_utilization_pct REAL,
    seven_day_resets_at TEXT,
    seven_day_status TEXT,
    representative_claim TEXT,
    overage_status TEXT,
    unified_status TEXT,
    FOREIGN KEY (subscription_id) REFERENCES subscription_credentials(id) ON DELETE CASCADE
);
CREATE INDEX idx_headroom_history_sub_fetched
    ON subscription_headroom_history(subscription_id, fetched_at DESC);  -- the windowed read
CREATE INDEX idx_headroom_history_fetched
    ON subscription_headroom_history(fetched_at);                        -- the retention sweep
```
The series is **`last`-per-bucket, never `max`** — probes are demand-driven so a max is biased by
how often anyone looked (`E[max of n]` rises with `n`, and the unattended overnight burn gets the
fewest samples); the 5h and 7d windows peak at different instants so a two-column max has no
single owning row; and a max over `utilization_pct` drops rate-limited samples outright. Selection
is `ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY fetched_at DESC)`, never a bare
non-aggregated column beside an aggregate (a SQLite-only extension that raises `GroupingError` on
PostgreSQL). Each bucket carries **both** its logical `bucket_start` and the real `fetched_at`:
emitting only non-empty buckets with real timestamps alone cannot support gap detection, because
sample jitter and a true gap are indistinguishable from timestamp deltas.

**agent_evaluations** (ent#206 — see [Agent Evaluations](observability.md#agent-evaluations--the-referee-surface-ent206)).
The referee surface: written by the platform/evaluator, **never** by the graded agent
(`require_admin` + `reject_agent_principal` on the single write route). `quality` is
nullable — null means "not graded yet", not zero. Dual-track migration (SQLite
`agent_evaluations_table` + Alembic `0033_agent_evaluations`); `AGENT_REFS` CASCADE:
```sql
CREATE TABLE agent_evaluations (
    id TEXT PRIMARY KEY,                 -- 'eval_<hex>'
    agent_name TEXT NOT NULL,
    execution_id TEXT,                   -- nullable: may grade the agent, not one run
    archetype TEXT,                      -- what "good" means here (per-archetype rubric)
    completion INTEGER,                  -- mirror of the clean-exit axis
    quality REAL,                        -- the graded axis (nullable, independent)
    checks_json TEXT,                    -- Tier-0 deterministic check results
    judge_json TEXT,                     -- Tier-1 judge output (enterprise layer)
    evaluator TEXT NOT NULL,             -- 'tier0' | judge id | admin username
    created_at TEXT NOT NULL,
    FOREIGN KEY (execution_id) REFERENCES schedule_executions(id)
);
CREATE INDEX idx_agent_evaluations_agent ON agent_evaluations(agent_name, created_at DESC);
CREATE INDEX idx_agent_evaluations_execution ON agent_evaluations(execution_id);
```

**skill_sources** (ent#237 — the multi-source skills library). **Replaces** the single
`skills_library_url` system setting: the platform syncs from a bundled public community
catalog plus any number of admin-added custom repos, each with its own checkout at
`/data/skills-library/<source_id>/`. Resolution across sources is `priority` ASC then
`created_at` ASC — custom sources default to 100 and the community source to 1000, so
**custom wins** a name clash and names stay bare. Dual-track migration (SQLite
`skill_sources_table` + Alembic `0034_skill_sources`):
```sql
CREATE TABLE skill_sources (
    id TEXT PRIMARY KEY,                 -- 'src_<hex8>' (server-minted; also the checkout dir name)
    name TEXT NOT NULL,
    url TEXT NOT NULL,                   -- github.com only (SSRF allowlist, SEC-179), validated on write AND at sync
    ref TEXT NOT NULL,                   -- branch name or tag name
    ref_type TEXT NOT NULL,              -- 'branch' | 'tag'
    is_default INTEGER DEFAULT 0,        -- the bundled community source
    enabled INTEGER DEFAULT 1,
    priority INTEGER NOT NULL,           -- resolution order; lower wins
    last_sync_at TEXT,
    last_sync_status TEXT,               -- 'never' | 'success' | 'failed'
    last_commit_sha TEXT,                -- the pin comparison's durable side (tag sources)
    last_error TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT
);
CREATE UNIQUE INDEX idx_skill_sources_url_ref ON skill_sources(url, ref);
```

`agent_skills.source_id` records which source an assignment resolved from — recorded, not
keyed (the UNIQUE stays `(agent_name, skill_name)`, since two sources' copies cannot
coexist on disk). Deleting a source does **not** cascade to assignments.

**Tag pinning is the supply-chain control (AC#5).** Skills carry executable `scripts/`
that the ent#139 runner executes and ent#236 re-injects fleet-wide unattended, so the
community source — which takes public PRs — pins to a **tag we bump**, never a branch
head; custom sources, whose write access the operator controls, track a branch. A pinned
tag that resolves to a different commit than the last sync is **refused**
(`moved_tag`), never adopted. The refusal covers **both** materialization paths, which is
the whole of it: the update path via a `fetch` without `--force` plus an explicit SHA
comparison — against the tag **peeled** (`^{commit}`), since the recorded SHA is the commit
and a bare rev-parse of an *annotated* tag is the tag object, which read an unmoved tag as
moved on every sync after the first (#2550) — and the **clone** path via
`_refuse_moved_pin_after_clone`, which also deletes
the checkout (a failed sync that leaves the tree on disk would still serve the moved tag's
content to `list_skills` and to injection). Enforcing only the update path leaves the pin
bypassed exactly when the checkout was lost — a quarantine rename, a restored `/data`
backup, a recreated volume — and a moved tag would then reach every running agent behind a
successful-looking sync. Revocation = cut a new tag without the offending skill.

### Redis

- **OAuth state**: `oauth_state:{state}` → `{provider, redirect_uri, user_id}`.
- **Heartbeat keys**: see [Heartbeat Liveness](reliability.md#heartbeat-liveness-reliability-004-307). All heartbeat ops are within the backend Redis ACL (`-@dangerous`) and follow the `agent:*` naming convention.
- **Capacity/breaker keys**: `agent:slots:{name}` (ZSET) + `agent:slot:{name}:{eid}` (HASH — whose `timeout_seconds` field is the floor canary S-03 reads, ent#336), `agent:circuit:{name}`, `agent:dispatch:{name}`, `agent:canary_zombie:{name}` (HASH, canary R-01's per-pid zombie-dwell marker with its own 24h TTL — ent#337), `canary:drain_tick_at`, `canary:leader` (the single-cycling-worker lease, SET NX + TTL floor 900s, mirror `monitoring:leader` — #1881; global rather than `agent:`-keyed, so like `canary:e02:*` it is legitimately unregistered: clearing it on an agent lifecycle event would drop leadership for the whole fleet), `canary:alert_pending` (HASH, field = invariant id — the #1897 undelivered-alert retry store, **no TTL**: it is bounded by the invariant registry (≤16 fields) and reaped by the success/give-up `HDEL`, whereas a TTL would silently discard every pending alert after one quiet hour, which is the silent loss the key exists to prevent. Keyed by invariant id, a fixed code registry that no user can recycle, so it is global rather than `agent:`-keyed and — like `canary:leader` and `canary:e02:*` — legitimately absent from `CLEARED_KEYSPACES`: clearing it on an agent lifecycle event would drop a pending fleet-level alert) — see the respective subsystem blocks. Every **name-keyed** per-agent keyspace is enumerated once in `services/agent_runtime_state.py` (`CLEARED_KEYSPACES` / `EXEMPT_KEYSPACES`) and cleared across the agent lifecycle, so a recycled name never inherits its predecessor's state (#1560); a parity test fails CI on an unregistered `agent:*` key.
- **Resumable-turn keys**: `session_lock:*`, `session_inflight:*` — shared by both conversation surfaces; see [Resumable Turns](execution.md#resumable-turns).
- **In-flight dispatch keys** (#2433): `execution:inflight:{execution_id}` (JSON `{agent, phase, since, pid}`, 60s TTL, refreshed every 15s by ONE refresher task per backend process — liveness, not state) and `execution:cancel:{execution_id}` (set by a terminate that finds the row parked in another worker; checked at semaphore grant). Execution-keyed and self-expiring, so deliberately outside `agent:*` (the `session_inflight:` precedent, documented in `agent_runtime_state.py`) — clearing either on an agent lifecycle event would strand or un-cancel a live call. Both fail open; the refresher negative-caches a Redis failure for 30s. **Neither key is eviction-proof:** prod and hosted run Redis with `--maxmemory 256mb --maxmemory-policy allkeys-lru`, so under memory pressure a marker can be dropped before its TTL and its row reads `absent` — a false orphan, exactly the exposure the slot ZSETs already carry. The blast radius is one row, and the agent-side `pending_ids`/`recently_completed_ids` half is unaffected. See [In-Flight Dispatch Proof-of-Life](execution.md#in-flight-dispatch-proof-of-life-2433).
- **Skills library sync lock**: `skills:library:sync` (SET NX + TTL, fail-open, check-and-delete release) serialises clone/pull across workers (ent#236). ent#237 keeps it covering the **whole sweep** rather than one lock per source: per-source locking would let two workers interleave and each publish a merged listing built from a half-updated set of checkouts, and the listing, the cache invalidation and the durable status are all library-wide. Contention returns `busy` (409), never a failure — a contended manual click must not overwrite the panel with "Last sync failed". Deliberately outside `agent:*` (the `compat_fix` precedent), so the #1560 name-keyed registry doesn't apply.
- **JWT / portal-session revocation**: `jwt:revoked:{jti}` (per-token blacklist, TTL = the token's remaining life, #187) and `portal:revoked_before:{email}` (a single per-email *cutoff* timestamp; `decode_portal_session` rejects a portal token whose `iat` is at or before it, TTL = the max portal-session lifetime). The cutoff form exists because `jti` is random per token and nothing indexes email → issued jtis, so "revoke every session this address holds" is O(1) instead of an index maintained at every mint — and it therefore cannot be forgotten on a new mint path. Both fail open on Redis; an edition-agnostic primitive (`dependencies.revoke_portal_sessions_for_email`), with the policy for *when* to call it owned by the entitled module that mints portal sessions.
- **Repo-binding locks** (ent#109): `agent:bind_op:{name}` (SET NX + TTL, double-submit guard) and `agent:bind_dest:{sha256(lower(destination_repo))}` — the latter keyed by **destination, not agent name**, because the collision is between two *different* agents targeting one repo. Both **fail closed** (503 + `Retry-After`), unlike `agent:data_op:`. Registered in `agent_runtime_state.EXEMPT_KEYSPACES` with reasons (clearing either mid-operation would unserialize the very operation that asked for the recreate).
- **Compatibility fix lock**: `compat_fix:{name}` (SET NX, 30s TTL) serialises the per-agent gitignore auto-fix read-modify-write (#668); ownership-checked via the shared `SingleFlightLock` (#1920 — was a constant-"1" + unconditional-delete twin of system_seed's bug).
- **Skill injection / removal lock**: `skill_inject:{name}` (SET NX + TTL, fail-open) serialises injection against removal — both mutate `~/.claude/skills/` and read-modify-write CLAUDE.md (ent#183 / ent#236). Deliberately outside `agent:*` (the `compat_fix` precedent), so the #1560 name-keyed registry doesn't apply. **Skills auto-sync leader**: `skills:sync:leader` (SET NX, TTL 3× interval, own-lease refresh, fail-open) — one worker per cycle (ent#236).
- **Operator-queue sync keys** (#1632): `opqueue:leader` (SET NX, TTL `max(3×poll-interval, 30s)` floor — the single-syncing-worker lease, mirror `monitoring:leader`) and the create rate-limit windows `ratelimit:operator_queue_create:{agent}` + `ratelimit:operator_queue_create:_fleet` (ZSET, via `rate_limiter.check`, fail-open). Not `agent:*`-named, so the #1560 name-keyed registry doesn't apply; both fail open.
- **Subscription-headroom keys** (#471): `subscription:headroom:{id}` (JSON provider snapshot, 7d TTL, best-effort DEL on subscription delete) + `subscription:headroom_probe:{sid}` (probe `SingleFlightLock` #1920). Subscription-id-keyed, deliberately not `agent:*` (the #1560 name-keyed registry doesn't apply); ambient probing is fail-CLOSED on an unanswered Redis read — see `subscription_headroom_service`.
- **Ephemeral-agent keys** (trinity-enterprise#69): `ephemeral:quota:{owner_id}` (owner-keyed atomic ghost-quota counter — deliberately not `agent:*`, so the #1560 name-keyed registry doesn't apply) and `ephemeral:discard:{name}` (SETNX+TTL discard lock, acquired/released through `SingleFlightLock` #1920). Both fail-open.
- **System-seed provisioning lock** (trinity-enterprise#124, hardened #1920): `system_seed:provision` (SETNX + 600s TTL) — one first-run seeding pass at a time; acquired + released through `SingleFlightLock` so the release is ownership-checked (unique per-acquire token + compare-and-delete), no longer a tokenless `delete` that could remove a successor's lock. Not `agent:*`-named; fail-open; the reserved-name existence backstop is the real duplicate guard.
- **Single-flight lock consolidation (#1920).** The SETNX single-flight sites — `ops:fleet_restart`, `ephemeral:discard:{name}`, `skill_inject:{name}`, `skills:library:sync`, `system_seed:provision`, `cornelius:provision`, `compat_fix:{name}`, and the later `agent:deploy_op:{base_name}` (#2060) — now share ONE ownership-checked primitive, `redis_breaker_util.SingleFlightLock` (mint-per-acquire token, GET-then-DELETE compare-and-delete). This unifies the **single-flight family only**: **four lock idioms remain** after this change — the shared `SingleFlightLock`, the async Lua-CAD `ResumeLock` (`session_turn_service.py`), the two verbatim leader leases (`monitoring:leader` / `opqueue:leader`, stable cross-cycle worker id), and canary's Lua-CAD leader lease (`canary:leader`, #1881) — kept separate by design. A static guard (`tests/unit/test_1920_no_hand_rolled_single_flight.py`) fails CI when a new hand-rolled `set(..., nx=True, ex=...)` single-flight lock appears outside `SingleFlightLock` with no allowlist entry.
- **Fleet-restart lock** (#1860, hardened #1919): `ops:fleet_restart` — SETNX + 2100s TTL single-flight guard for `POST /api/ops/fleet/restart` (409 on contention); TTL is sized above the slowest single agent (skill injection alone is bounded by `skill_service._INJECT_LOCK_TTL_SECONDS`=1800 — constants comment-linked). The live loop's per-agent refresh is an **ownership-checked pre-action gate** (GET-compare → EXPIRE, never a bare EXPIRE): a **foreign** token (concurrent caller took the lock after a lapse) stops the run — partial results stand, `summary`/audit carry `stopped_early="lease_lost_foreign"` + `processed` vs `total`; an **absent** token (lapsed, unclaimed) is re-acquired via SETNX and the run continues (`lease_reacquired` audited); acquire sits inside the `try/finally` so nothing can leak the lock. Release is compare-and-delete (shared `redis_breaker_util.lock_token_matches`), attempted even after detected loss (foreign-safe by construction). Not `agent:*`-named (fleet-scoped, #1560 registry doesn't apply); fail-open when Redis is down (a refresh Redis error is throttle-logged, never treated as loss).
- **SSH-port reservations + first-run seed pass lock** (#2215): `port_alloc:{port}` — transient per-port reservation bridging the allocator's check and `containers.run` (SETNX + TTL 600s at allocation; SET no-NX by `reserve_port_for_recreate` across the recreate gap). **No release path by design**: once the container exists its `trinity.ssh-port` label is the durable truth (Invariant #11), so the key self-expires; deliberately not `agent:*` (port-keyed, and no lifecycle event should clear it — clearing on an agent event would un-reserve a port mid-create; the `ephemeral:quota:` precedent). `first_run_seed:provision` — pass-level lock serialising BOTH first-run seeders across workers (SETNX, uuid token, TTL 900s, `lock_token_matches` compare-and-delete release, fail-open; loser skips the whole pass). Both fail open; neither is in the #1560 registry.

---

