# Channel Adapter Message Routing (SLACK-002)

> **Status**: Complete (updated 2026-07-04 — thread-scoped session + per-speaker attribution + sender-filtered memory, #903)
> **Created**: 2026-03-23
> **Extends**: SLACK-001
> **See also**: [telegram-integration.md](telegram-integration.md) — TGRAM-001 (second adapter implementation)

## Overview

Pluggable channel adapter abstraction for external messaging platforms. Any incoming message follows the same pipeline: Transport → Adapter → Router → Agent → Response. Single Slack App supports multiple agents, each with a dedicated channel. Telegram bots follow the same pipeline with `TelegramAdapter`.

## User Story

As an **agent owner**, I want to connect multiple agents to Slack so each agent gets its own channel and users can chat with the right agent.

As a **Slack user**, I want to @mention the bot in an agent's channel and get a response from that specific agent — and continue the conversation in the thread without needing to @mention again.

As a **platform admin**, I want Slack messages to go through the same execution pipeline as web public chat (audit trail, rate limits, tool restrictions) so public Slack users can't access sensitive agent resources.

## Entry Points

- **Settings UI**: `src/frontend/src/views/Settings.vue` — Slack Integration section (transport + workspace install)
- **Agent UI**: `src/frontend/src/components/SlackChannelPanel.vue` — per-agent channel binding in Sharing tab
- **Legacy UI**: `src/frontend/src/components/PublicLinksPanel.vue:540` — "Connect Slack" button (per public link)
- **API**: `POST /api/settings/slack/connect` — start Socket Mode transport
- **API**: `POST /api/settings/slack/install` — platform-level OAuth (workspace install)
- **API**: `POST /api/agents/{name}/slack/channel` — create channel + bind agent
- **Transport**: `src/backend/adapters/transports/slack_socket.py` — Socket Mode receives events. Runs **N concurrent WebSocket connections** (default 2, configurable via `SLACK_SOCKET_CONNECTION_COUNT` env var, clamped 1–10) per Slack's documented multi-connection guidance; one connection half-closing is absorbed by the others. Each client has an independent watchdog. An envelope-ID dedup ring (cap 1024, FIFO) protects against possible cross-connection duplicate delivery (#244)

## Frontend Layer

### Settings Page (Platform-Level)
- `Settings.vue` — Unified Slack section with:
  - OAuth credentials (Client ID, Client Secret, Signing Secret) + Save
  - Transport connection: Socket Mode status badge, App Token input, Connect button
  - "Install to Workspace" button → triggers platform OAuth → redirects to Slack → callback stores bot token → redirects to Settings with success notification
  - Connected workspaces list with bound agent badges

### Agent Detail — Sharing Tab (Per-Agent)
- `SlackChannelPanel.vue` — Three states:
  - **Bound**: Shows `#channel-name`, workspace name, DM-default badge **or** "Make default" button (with hover tooltip explaining DM routing), and "Unbind" button. The Unbind button is **disabled** when this agent is the DM default *and* the workspace has other bound agents — promoting another agent first via the "Make default" button on its panel is required (#584).
  - **Unbound**: "Create Slack Channel" button → creates channel in Slack + binds to agent
  - **Access denied**: Informational message for non-owner shared users
- `SharingPanel.vue` — Renders `SlackChannelPanel` between Team Sharing and Public Links sections

### Legacy: PublicLinksPanel (Per Public Link)
- `PublicLinksPanel.vue:540-556` — `connectSlack()` method handles two response types:
  - `status: "connected"` — workspace already linked, channel created → show success notification
  - `status: "oauth_required"` — redirect to Slack OAuth URL

### API Calls (Settings)
- `GET /api/settings/slack/status` → `{connected, transport_mode, app_token_configured, workspaces}`
- `POST /api/settings/slack/connect` → `{connected, transport_mode}`
- `POST /api/settings/slack/disconnect` → `{disconnected, was_connected}`
- `POST /api/settings/slack/install` → `{oauth_url}` (browser redirect)

### API Calls (Per-Agent Channel)
- `GET /api/agents/{name}/slack/channel` → `{bound, channel_name, channel_id, workspace_name, is_dm_default, workspace_agent_count}`
- `POST /api/agents/{name}/slack/channel` → `{status, channel_name, channel_id, workspace_name}`
- `DELETE /api/agents/{name}/slack/channel` → `{unbound, workspace_name}` — **409** if the agent is the workspace's DM default and other agents are still bound (#584)
- `PUT /api/agents/{name}/slack/channel/dm-default` → `{status, team_id, workspace_name, previous, new_default}` — owner-only; single-tx clear-then-set on `is_dm_default`; audit-logged via `AGENT_LIFECYCLE/slack_dm_default_changed` (#584)

## Backend Layer

### Message Flow

```
External Platform (Slack)
       │
       ▼
Transport Layer (Socket Mode / Webhook)
       │  Receives raw event, acknowledges immediately
       ▼
ChannelAdapter.parse_message(raw_event)
       │  Returns NormalizedMessage or None (skip)
       ▼
ChannelMessageRouter.handle_message(adapter, message)
       │
       ├─ 1. adapter.get_agent_name(message)       → resolve agent
       ├─ 2. adapter.get_bot_token(message)        → credentials
       ├─ 3. _check_rate_limit(adapter.get_rate_key) → sliding window
       ├─ 4. get_agent_container(agent_name)       → verify running
       ├─ 5. adapter.handle_verification(message)  → sender auth
       ├─ 6. db.get_or_create_public_chat_session  → session
       ├─ 7. db.build_public_chat_context           → history
       ├─ 8. adapter.indicate_processing(message)   → ⏳
       ├─ 9. TaskExecutionService.execute_task       → agent call
       ├─ 10. adapter.indicate_done(message)         → ✅
       ├─ 11. db.add_public_chat_message (x2)        → persist
       ├─ 12. adapter.send_response(channel, resp)   → deliver
       └─ 13. adapter.on_response_sent(msg, agent)   → track thread
```

### Agent Resolution (Slack)

Priority in `SlackAdapter.get_agent_name()`:

1. **Channel binding** — `slack_channel_agents` lookup by `(team_id, channel_id)`
2. **Active thread** — `slack_active_threads` lookup (reply-without-mention)
3. **DM default** — `slack_channel_agents` where `is_dm_default = 1`
4. **Single agent** — Only one connected agent → use it
5. **Legacy fallback** — `slack_link_connections` from SLACK-001

### Endpoints

| Method | Path | Handler | Description |
|--------|------|---------|-------------|
| POST | `/api/public/slack/events` | `routers/slack.py` | Webhook event receiver (fallback mode) |
| GET | `/api/public/slack/oauth/callback` | `routers/slack.py` | OAuth completion redirect |
| POST | `/api/agents/{name}/public-links/{id}/slack/connect` | `routers/slack.py` | Connect agent to Slack (OAuth or channel bind) |
| GET | `/api/agents/{name}/public-links/{id}/slack` | `routers/slack.py` | Connection status |
| DELETE | `/api/agents/{name}/public-links/{id}/slack` | `routers/slack.py` | Disconnect |
| PUT | `/api/agents/{name}/public-links/{id}/slack` | `routers/slack.py` | Update settings (enable/disable) |
| PUT | `/api/agents/{name}/slack/channel/dm-default` | `routers/slack.py` | Make this agent the DM-default for its workspace (#584) |
| DELETE | `/api/agents/{name}/slack/channel` | `routers/slack.py` | Unbind — refuses with 409 if agent is the DM default and others are bound (#584) |

### Business Logic

1. **"Connect Slack" flow**: If workspace not connected → OAuth. If connected → `conversations.create` creates `#agent-name` channel → bind in `slack_channel_agents`
2. **Message routing**: Socket Mode/webhook delivers event → adapter parses → router dispatches to agent via `TaskExecutionService` → adapter formats response via `format_response()` (markdown → Slack mrkdwn) → sends with `chat:write.customize` (agent name/avatar)
3. **Response formatting** (#293, refreshed 2026-05-01): `SlackAdapter.format_response()` converts standard markdown to Slack mrkdwn via `services.slack_mrkdwn.to_slack_mrkdwn` — a stateful `markdown-it-py` AST walker that replaces the third-party `slackify-markdown==0.2.2` library. The library shipped with five compounding bugs that produced "ugly" output: nested lists were flattened, headings were crammed against preceding content (no blank line before), blockquotes only got the `>` prefix on the first line, Markdown tables were passed through verbatim (raw pipes in Slack), and horizontal rules were dropped silently. The renderer fixes all five, plus retains correct inline conversion (`**bold**` → `*bold*`, `*italic*` → `_italic_`, `~~strike~~` → `~strike~`, `[label](url)` → `<url|label>`, code blocks with language fence stripped per Slack mrkdwn spec, images → `<url|alt>` link). Graceful fallback to plain text on conversion failure.
4. **Thread continuity**: First @mention → bot responds in thread → registers in `slack_active_threads` → subsequent replies in that thread don't need @mention

### Database Operations

| Table | Operation | Description |
|-------|-----------|-------------|
| `slack_workspaces` | create/get/delete | Workspace connections with encrypted bot tokens |
| `slack_channel_agents` | bind/unbind/get | Channel-to-agent routing |
| `slack_active_threads` | register/check | Thread tracking for reply-without-mention |
| `public_chat_sessions` | get_or_create | Session persistence (reused from web public chat) |
| `public_chat_messages` | add | Message history persistence |

## Side Effects

- **Slack channel created**: `conversations.create` called when agent connects (visible in workspace)
- **Reaction emoji**: ⏳ added on message receive, replaced with ✅ on completion
- **Execution record**: `schedule_executions` row with `triggered_by="slack"`
- **Activity tracking**: Agent activity recorded via `TaskExecutionService` (visible in Dashboard timeline)
- **Slot management**: Execution slot acquired/released via `SlotService`
- **Active thread**: Registered in `slack_active_threads` after first response

## Error Handling

| Error Case | Response | Notes |
|------------|----------|-------|
| No agent for channel | Silent (no response) | Message in unbound channel |
| Agent not running | "Sorry, I'm not available right now." | Container status != running |
| Rate limited | "You're sending messages too quickly." | 30 msg/min per user |
| Agent at capacity | "I'm busy right now." | All parallel slots in use |
| Billing/credit error | "I'm having trouble processing." | API key or subscription issue |
| Execution error | "Sorry, I encountered an error." | Generic fallback |
| No bot token | Silent (logged as error) | Workspace connection issue |
| Verification pending | Verification prompts sent | Email verification flow |

## Security Considerations

### Authentication
- Slack users are **public users** — no Trinity account needed
- Optional email verification (auto-verify via Slack profile or 6-digit code)
- Socket Mode: token-based auth (handled by `slack_sdk`)
- Webhook Mode: HMAC-SHA256 signature verification

### Tool Restrictions
- Public Slack users restricted to `--allowedTools WebSearch,WebFetch` (configurable)
- No Bash, Read, Write, Edit, NotebookEdit for public users
- **Known limitation**: MCP tools (Trinity API) not restricted by `--allowedTools` — see `docs/research/slack-security-findings.md`

### Data Protection
- Bot tokens encrypted at rest (AES-256-GCM via `credential_encryption.py`)
- Rate limiting: 30 msg/min per Slack user (configurable)
- Execution timeout: 120s (configurable)
- Session identifiers (`SlackAdapter.get_session_identifier`) — no PII:
  - **Channel** (@mention / thread reply): `{team_id}:{channel_id}:{thread_id}` — **thread-scoped** (#903). `sender_id` is dropped so two concurrent threads in a channel stay isolated, a fresh top-level @mention (`thread_id = thread_ts or ts` → a new id) starts with clean history, and a multi-participant thread shares one context. `thread_id` is guaranteed non-None on the channel path.
  - **DM**: `{team_id}:{sender_id}:{channel_id}` — unchanged (one continuous per-user convo; a DM `thread_ts` is ignored).
  - Per-speaker attribution + per-user memory moved off the key onto `public_chat_messages.sender_label` / `sender_email` (see [Per-speaker attribution & memory](#per-speaker-attribution--memory-903)).
  - `get_or_create_public_chat_session` on a brand-new thread key is race-guarded (savepoint + `IntegrityError` re-SELECT) — dropping `sender_id` newly lets two different users race the same key.

### Per-speaker attribution & memory (#903)

Thread-scoping lets several users share one channel session, so who-said-what and whose-memory move onto each message row:

- `public_chat_messages.sender_label` (nullable) — persisted with each channel user turn from the #350 `enrich_message` display name (`"John Smith (@johndoe)"`); the assistant turn is labelled by agent name. `build_public_chat_context` replays history attributed (`Alice: …` / `Bob: …`) instead of a flat `User:`, falling back to the role label when null (DM/web turns and pre-migration rows). The #350 `[Channel: #x]\n[From: …]` prefix stays on the **current** turn only — history attribution now comes from the stored label, so there's no double-prefix.
- `public_chat_messages.sender_email` (nullable) — the verified speaker. `summarize_user_memory_background` reads `get_recent_public_chat_messages(session_id, sender_email=user_email)`, so a shared thread never feeds one user's turns into another user's durable, re-injected MEM-001 memory. Single-participant paths (web public-chat + **any channel DM** — Slack/Telegram/WhatsApp) stamp `sender_email` on **both** the user turn and the assistant reply, so the filter is a no-op there — the summarizer keeps the full user+assistant conversation, as it did pre-#903. Only a multi-participant session — a Slack **channel thread** or a **group chat** — leaves the assistant turn's `sender_email` null so the shared reply is excluded from any single participant's memory. The DM-vs-shared decision (`_assistant_sender_email`) combines each adapter's own signal: Slack sets `is_dm` (never `is_group`), Telegram sets `is_group` (never `is_dm`), WhatsApp (DM-only) sets neither — so keying on `is_dm` alone would regress Telegram/WhatsApp DMs and `not is_group` alone would wrongly stamp Slack channel threads.

Both columns are dual-track migrated (SQLite `public_chat_messages_sender` + Alembic `0013_public_chat_messages_sender`). No migration of pre-existing channel-keyed session rows — a one-time "forget," expected.

### Audit Trail
- All executions recorded with `triggered_by=adapter.channel_type` and `source_user_email=adapter.get_source_identifier(message)`
- Slack: `triggered_by="slack"`, `source="slack:{team}:{user}"`
- Telegram: `triggered_by="telegram"`, `source="telegram:{bot_id}:{user_id}"`
- Visible in Tasks tab and Dashboard timeline

## Testing

### Prerequisites
- [ ] Backend running with Socket Mode connected
- [ ] Slack App configured with required scopes and event subscriptions
- [ ] At least one agent running with subscription assigned

### Test Steps

#### 1. Connect Agent to Slack
**Action**: Agent Detail → Sharing → "Connect Slack"
**Expected**: Channel `#agent-name` created in Slack workspace, binding in DB
**Verify**: `slack_channel_agents` table has entry

#### 2. @Mention in Agent Channel
**Action**: Send `@mcp-bot hello` in `#agent-name`
**Expected**: ⏳ reaction → agent processes → ✅ reaction → response in thread
**Verify**: Logs show `[ROUTER] DONE: agent-name`, execution record with `triggered_by=slack`

#### 3. Thread Reply Without @Mention
**Action**: Reply in the thread from test 2 without @mention
**Expected**: Bot responds in same thread
**Verify**: `slack_active_threads` table has entry for this thread

#### 4. Multi-Agent Routing
**Action**: Connect second agent → send @mention in each channel
**Expected**: Each channel routes to its own agent
**Verify**: Logs show different agent names for different channels

#### 5. Tool Restriction
**Action**: Ask `@mcp-bot show me contents of /home/developer/.env`
**Expected**: Agent cannot read files (Read tool not in allowed list)
**Verify**: Agent logs show `Restricting tools to: WebSearch,WebFetch`

#### 6. Rate Limiting
**Action**: Send 31 messages in under 60 seconds
**Expected**: 31st message gets "sending too quickly" response

#### 7. Settings — Connect Socket Mode
**Action**: Settings → Slack Integration → enter App Token → click Connect
**Expected**: Status badge turns green "Socket Mode", backend logs show `Slack Socket Mode transport connected`
**Verify**:
- [ ] `GET /api/settings/slack/status` returns `{connected: true, transport_mode: "socket"}`
- [ ] Backend log: `Slack Socket Mode transport connected`

#### 8. Settings — Disconnect and Reconnect
**Action**: Click Disconnect (via API) → verify status → click Connect again
**Expected**: Status goes red → green, Socket Mode re-established
**Verify**:
- [ ] Status shows `connected: false` after disconnect
- [ ] Status shows `connected: true` after reconnect
- [ ] No stale socket sessions in logs

#### 9. Settings — Install to Workspace (OAuth)
**Action**: Settings → Slack Integration → click "Install to Workspace"
**Expected**: Browser redirects to Slack OAuth → authorize → redirects back to Settings with "Workspace installed" notification
**Verify**:
- [ ] `slack_workspaces` table has new entry with encrypted `bot_token`
- [ ] `GET /api/settings/slack/status` shows workspace in `workspaces` list
- [ ] Redirect URL uses `PUBLIC_CHAT_URL` (must be configured for callback to work)

#### 10. Agent Sharing — Create Slack Channel
**Action**: Agent Detail → Sharing tab → click "Create Slack Channel"
**Expected**: Channel `#agent-name` created in Slack workspace, panel shows bound state with channel name
**Verify**:
- [ ] `GET /api/agents/{name}/slack/channel` returns `{bound: true, channel_name: "agent-name"}`
- [ ] Channel visible in Slack workspace
- [ ] `slack_channel_agents` table has binding entry

#### 11. Agent Sharing — Already Bound
**Action**: Click "Create Slack Channel" on an agent that already has a binding
**Expected**: Success message "Already bound to #agent-name in workspace-name"
**Verify**:
- [ ] No duplicate channel created
- [ ] `POST /api/agents/{name}/slack/channel` returns `{status: "already_bound"}`

#### 12. Agent Sharing — Unbind Channel
**Action**: Click "Unbind" on a bound agent
**Expected**: Panel switches to unbound state showing "Create Slack Channel" button
**Verify**:
- [ ] `GET /api/agents/{name}/slack/channel` returns `{bound: false}`
- [ ] `slack_channel_agents` table entry removed
- [ ] Slack channel still exists (not deleted) but agent no longer responds to @mentions in it

#### 13. Agent Sharing — No Workspace Connected
**Action**: Click "Create Slack Channel" when no workspace is installed
**Expected**: Error message "No Slack workspace connected. Install a workspace from Settings first."
**Verify**:
- [ ] `POST /api/agents/{name}/slack/channel` returns 400

#### 14. Startup Recovery Supervisor — Network Blip (#708)

The Socket Mode transport's `start()` runs once at backend boot. Pre-#708, if every initial connect attempt timed out (10s ceiling, e.g. transient DNS slowness or Slack edge throttle), the transport gave up silently — backend stayed up but Slack was permanently offline until manual restart. With #708, when all initial attempts fail `start()` spawns a **recovery supervisor** task that retries with the same backoff cadence as the per-client watchdog (60→120→240→300s cap). When at least one client connects, the supervisor appends contexts, spawns per-client watchdogs, and exits — graduating recovery to the watchdog model.

**Manual reproduction (recovery on transient network failure):**

1. With Slack Socket Mode connected, write `extra_hosts` poisoning Slack DNS into a docker-compose override:
   ```yaml
   # docker-compose.smoke.override.yml
   services:
     backend:
       extra_hosts:
         - "slack.com:127.0.0.1"
         - "wss-primary.slack.com:127.0.0.1"
         - "wss-backup.slack.com:127.0.0.1"
         - "wss-mobile.slack.com:127.0.0.1"
   ```
2. Recreate backend with the override applied:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.smoke.override.yml up -d --no-deps --force-recreate backend
   ```
3. **Expected backend logs (within ≤30s)**:
   - `Slack Socket Mode: all initial connection attempts failed. Starting recovery supervisor — will retry in background (first retry in 60s).`
   - `Socket Mode startup supervisor: started (target=N connection(s))`
4. After ~70s the supervisor logs its first retry result:
   - `Socket Mode startup supervisor [c=0]: still failing`
5. Unblock at runtime by stripping the poisoned lines from the running container's `/etc/hosts` (no restart — supervisor task is alive in the same process):
   ```bash
   docker exec trinity-backend python3 -c '
   keep = [l for l in open("/etc/hosts") if not ("127.0.0.1" in l and "slack.com" in l)]
   open("/etc/hosts","w").write("".join(keep))'
   ```
6. **Expected on the supervisor's next retry (≤120s after unblock)**:
   - `Socket Mode startup supervisor: recovered N/N connection(s) after K attempt(s); watchdog(s) started, supervisor exiting.`
   - `Socket Mode watchdog [c=0]: started` (one per recovered client)
7. **Cleanup**: remove the override file and recreate backend to a clean state:
   ```bash
   rm docker-compose.smoke.override.yml
   docker compose up -d --no-deps backend
   ```

**Manual reproduction (no zombie under bad credentials):**

The supervisor will keep retrying forever if the token is permanently invalid (typo, expired, revoked). Verify the backend itself stays healthy throughout:

1. Snapshot the existing `slack_app_token` in `system_settings` (SQLite at `/data/trinity.db` inside the backend container).
2. Replace it with a well-formed but fake token (e.g. `xapp-1-A0000000000-0000000000000-` + 64 hex chars). **Don't forget `updated_at` — the column is `NOT NULL`.**
3. `docker restart trinity-backend`.
4. **Expected**: `start()` passes the format check, `_start_one_client` raises `invalid_auth` from Slack, supervisor spawns and retries.
5. **Probe backend HTTP throughout** — `/health` and `/api/auth/mode` must return 200 during the supervisor's sleep AND across multiple retries. If either degrades, that's the zombie regression. Background-task supervisor must never block FastAPI.
6. **Expected after 3 consecutive failures**: ERROR log `SLACK SOCKET MODE STARTUP UNREACHABLE — N consecutive recovery attempts failed.` — operator-paging signal.
7. **Cleanup**: restore the original token in DB (with `updated_at` set), restart backend.

**Permanent-error early-exit path:**

A token that doesn't start with `xapp-` (operator pasted a `xoxb-` bot token by mistake, or random text) is rejected at the format check at the top of `start()` — no supervisor spawned, no retry loop, no resource use. Backend continues normally. Verified by unit test `TestStartupRecoverySupervisor.test_supervisor_not_spawned_for_invalid_token`.

**Behavior matrix:**

| Scenario | `_running` | Contexts | Supervisor | Backend health |
|---|---|---|---|---|
| All initial connects succeed | True | N | None | Healthy |
| Partial initial succeeds (1 of N) | True | <N | None (degraded mode) | Healthy |
| All initial fail, transient cause | True | 0 → N | Spawned → exits on recovery | Healthy throughout |
| All initial fail, permanent bad creds | True | 0 (forever) | Spawned, retries with backoff | Healthy throughout, ERROR log every 3 failures |
| Token format invalid (no `xapp-` prefix) | False | 0 | None (early return) | Healthy |
| `stop()` called mid-supervisor | False | 0 | Cancelled | Healthy |

**Unit-test coverage:** `tests/unit/test_slack_multi_connection.py::TestStartupRecoverySupervisor` — 9 cases covering happy path, recovery, backoff progression, cancellation by `stop()`, and operator-paging log.

### Edge Cases
- [ ] Bot invited to #general (non-agent channel) → only responds to @mentions
- [ ] Thread started by another user (not via @mention) → bot ignores replies
- [ ] Agent stopped while message in flight → "not available" response
- [ ] Two concurrent threads in one channel → distinct sessions; reply / re-@mention inside a thread → same session (#903, thread isolation)
- [ ] Fresh top-level @mention → brand-new session, empty history (#903)
- [ ] Multi-participant thread (Alice + Bob) → shared context; agent's replay shows `Alice:` / `Bob:`; Bob's MEM-001 memory contains no Alice content (#903)
- [ ] DM continuity: a user's DMs stay on one session across messages/threads (#903)
- [ ] Non-owner views Sharing tab → SlackChannelPanel shows "Only the agent owner can manage Slack channel bindings"
- [ ] `CREDENTIAL_ENCRYPTION_KEY` mismatch → bot token decryption fails → Slack API calls fail (logged, no crash)

**Unit-test coverage (#903):** `tests/unit/test_903_slack_session_thread.py` (parser-driven session-key cases: thread isolation, fresh-mention clean history, DM continuity, F-TEAMID) + `tests/unit/test_903_public_chat_sender.py` (attribution, sender-filtered memory, race guard, schema accessor).

**Last Tested**: 2026-05-08 (#708 startup-recovery + bad-creds zombie smoke verified end-to-end against running backend)
**Tested By**: claude + human (manual + 15 integration tests + 65 unit tests)
**Status**: ✅ Core flow + transport management + per-agent channel binding + startup recovery supervisor (#708) working
**Issues**: MCP tools bypass `--allowedTools` restriction (documented in security findings)

## Related Flows

- [slack-integration.md](slack-integration.md) — SLACK-001: Original Slack DM integration
- [telegram-integration.md](telegram-integration.md) — TGRAM-001: Telegram Bot Integration (second adapter)
- [task-execution-service.md](task-execution-service.md) — EXEC-024: Unified execution lifecycle
- [public-agent-links.md](public-agent-links.md) — Public chat session persistence

## Configurable Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `channel_rate_limit_max` | 30 | Messages per rate limit window |
| `channel_rate_limit_window` | 60 | Window duration in seconds |
| `channel_timeout_seconds` | 120 | Max execution time per message |
| `channel_allowed_tools` | WebSearch,WebFetch | Comma-separated allowed tools for public users |
| `slack_transport_mode` | socket | Transport: `socket` or `webhook` |
| `slack_app_token` | (none) | App-Level Token for Socket Mode |
| `public_chat_url` | (none) | Public URL for webhook mode |
