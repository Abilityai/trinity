# Requirements — Public Access, Payments & Mobile Admin

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 15. Public Access

### 15.1 Public Agent Links
- **Status**: ✅ Implemented (2025-12-22)
- **Description**: Shareable public links for unauthenticated agent access
- **Key Features**: Optional email verification, rate limiting, usage tracking
- **Flow**: `docs/memory/feature-flows/public-agent-links.md`

### 15.1a Public Chat Session Persistence (PUB-005)
- **Status**: ✅ Implemented (2026-02-17)
- **Description**: Multi-turn conversation persistence for public chat links
- **Key Features**: Session management (email-based or anonymous), message history, New Conversation button, page refresh recovery, context injection for continuity
- **Flow**: `docs/memory/feature-flows/public-agent-links.md#public-chat-session-persistence-pub-005`

### 15.1a-2 Per-User Persistent Memory for Public Links (MEM-001)
- **Status**: ✅ Implemented (2026-03-19)
- **Requirement ID**: MEM-001
- **GitHub Issue**: #147
- **Description**: Email-verified public chat sessions maintain persistent per-user memory (text blob) scoped to `(agent_name, user_email)`, injected into every agent call. Memory is updated via background summarization every 5 messages (auto) or explicitly via the `write_user_memory` MCP tool (agent-initiated, #888). The tool resolves the user email server-side from the execution record — agents never handle email addresses directly.
- **Database Tables**: `public_user_memory`
- **API**: `POST /api/agents/{name}/user-memory` (agent-scoped key + execution_id; user-facing triggers only)
- **Flow**: `docs/memory/feature-flows/public-agent-links.md#per-user-persistent-memory-mem-001`

### 15.1a-3 Agent Website Proxy (SITE-001)
- **Status**: ✅ Implemented (2026-05-03)
- **Requirement ID**: SITE-001
- **GitHub Issue**: #633
- **Priority**: P2
- **Description**: Expose an agent's internal web server (e.g., a Next.js or React app running on port 3000) publicly via a `site`-type public link at `/site/{token}/{path}`. All HTTP methods, query strings, and bodies are reverse-proxied; responses are streamed.
- **Key Features**:
  - `site`-type public link token validated by the existing `agent_public_links` table
  - Dual-bucket rate limiting (120 req/min per IP, 300 req/min per token)
  - SSRF defense: agent name validated; upstream always resolves to `http://agent-{name}:3000`
  - Sensitive request headers stripped (`authorization`, `cookie`, `x-internal-secret`)
  - Hop-by-hop and server-banner response headers stripped
  - Audit event `SITE_ACCESS / site_link_visit` on every proxied request (fire-and-forget)
  - 401 invalid token, 410 expired, 429 rate limit, 502 agent unreachable
- **API Endpoints**:
  - `GET /site/{token}` — redirect to `/site/{token}/`
  - `GET/POST/… /site/{token}/{path}` — proxy to agent port 3000
- **Flow**: `docs/memory/feature-flows/public-agent-links.md`

### 15.1b Slack Integration for Public Links (SLACK-001)
- **Status**: ✅ Implemented (2026-02-25)
- **Requirement ID**: SLACK-001
- **Priority**: P1
- **Description**: Enable Slack as a delivery channel for public agent links. Users chat with agents via DMs to a Slack bot.
- **Key Features**:
  - DMs only (no channel @mentions in Phase 1)
  - One Slack workspace = one public link (simple 1:1 mapping)
  - Email verification for Slack users (auto-verify via Slack profile or email code)
  - Session persistence (reuse `public_chat_sessions` with `slack` identifier type)
  - OAuth flow for workspace connection
  - Signature verification for Slack events
- **Database Tables**:
  - `slack_link_connections` - Connects workspace to public link (bot_token AES-256-GCM encrypted, #453)
  - `slack_user_verifications` - Tracks verified Slack users
  - `slack_pending_verifications` - In-progress email verifications
- **API Endpoints**:
  - `POST /api/public/slack/events` - Slack event receiver
  - `GET /api/public/slack/oauth/callback` - OAuth completion
  - `GET/DELETE /api/agents/{name}/public-links/{id}/slack` - Connection status
  - `POST /api/agents/{name}/public-links/{id}/slack/connect` - Initiate OAuth
- **Spec**: `docs/requirements/SLACK_INTEGRATION.md`
- **Flow**: `docs/memory/feature-flows/slack-integration.md`

### 15.1b-ii Channel Adapters + Multi-Agent Slack (SLACK-002)
- **Status**: ✅ Implemented (2026-03-23, updated 2026-07-04)
- **Requirement ID**: SLACK-002
- **Priority**: P1
- **Description**: Pluggable channel adapter abstraction for external messaging platforms. Extends SLACK-001 with multi-agent routing (multiple agents per workspace), @mention support in channels, thread continuity (reply-without-mention), and configurable operational limits.
- **Key Features**:
  - Channel-agnostic adapter pattern (`ChannelAdapter` base class) supporting Slack, future Telegram/Discord
  - `ChannelMessageRouter` — unified message pipeline: resolve agent → rate limit → verify → execute → respond
  - Multi-agent workspace: bind different agents to different Slack channels
  - @mention routing in channels + DM default agent
  - Thread tracking: bot auto-responds to thread replies without requiring @mention
  - **Thread-scoped session context + per-speaker attribution + sender-filtered memory (#903)**: channel chat sessions key on `team:channel:thread` (not `team:sender:channel`), so two concurrent threads in a channel stay isolated, a fresh top-level @mention starts with clean history, and a multi-participant thread shares one context. `sender_id` is dropped from the key; per-speaker attribution and per-user memory move onto each message row via two nullable `public_chat_messages` columns — `sender_label` (history replays as `Alice:`/`Bob:`, role fallback when null) and `sender_email` (the MEM-001 summarizer filters on the current user's own turns so a shared thread never feeds one user's turns into another user's durable memory). DMs keep `team:sender:channel` (one continuous per-user convo). Concurrent `get_or_create_session` on a brand-new thread key is race-guarded (savepoint + `IntegrityError` re-SELECT). No migration of pre-existing channel-keyed rows (one-time "forget", expected).
  - Configurable rate limits, execution timeout, and allowed tools via `settings_service`
  - Periodic pruning of rate-limit buckets to prevent memory leaks
  - **Settings UI**: Socket Mode connect/disconnect, app token management, connection status badge
  - **Platform OAuth**: "Install to Workspace" from Settings page (no agent context needed)
  - **Per-agent channel binding**: Standalone UI in Sharing tab to create/unbind Slack channels per agent
- **Database Tables**:
  - `slack_workspaces` — Workspace connections (team_id, bot_token encrypted)
  - `slack_channel_agents` — Channel-to-agent bindings (multi-agent routing)
  - `slack_active_threads` — Active thread tracking (reply-without-mention)
  - `public_chat_messages.sender_email` / `sender_label` — per-message speaker attribution (#903), both nullable, dual-track migration (SQLite `public_chat_messages_sender` + Alembic `0013_public_chat_messages_sender`)
- **Configurable Settings** (via Settings UI or DB):
  - `channel_rate_limit_max` — Messages per window (default: 30)
  - `channel_rate_limit_window` — Window in seconds (default: 60)
  - `channel_timeout_seconds` — Execution timeout (default: 120)
  - `channel_allowed_tools` — Comma-separated tool list (default: WebSearch,WebFetch)
- **API Endpoints (new in 2026-03-26)**:
  - `GET /api/settings/slack/status` — Transport connection state + workspace info
  - `POST /api/settings/slack/connect` — Save app token + start Socket Mode
  - `POST /api/settings/slack/disconnect` — Stop transport
  - `POST /api/settings/slack/install` — Platform-level OAuth (workspace install)
  - `GET /api/agents/{name}/slack/channel` — Channel binding status
  - `POST /api/agents/{name}/slack/channel` — Create channel + bind agent
  - `DELETE /api/agents/{name}/slack/channel` — Unbind agent
- **Flow**: `docs/memory/feature-flows/slack-channel-routing.md`

### 15.1b-iii Slack Inbound File Sharing (SLACK-FILES)
- **Status**: ✅ Implemented (2026-03-31)
- **Requirement ID**: SLACK-FILES
- **Priority**: P2
- **GitHub Issue**: #222
- **Extends**: SLACK-002
- **Description**: Slack users can upload files that agents process. Images embedded as base64 data URIs for Claude vision. Text files (CSV, JSON, TXT, MD, etc.) copied into per-session container directories via Docker `put_archive` API.
- **Key Features**:
  - `FileAttachment` model + `files` field on `NormalizedMessage` (channel-agnostic)
  - `ChannelAdapter.download_file()` — each channel implements own download auth
  - Images (`image/*`): base64 inline in prompt (Claude vision, 5MB/image, 10MB total)
  - Text files: copied to `/home/developer/uploads/{session_id}/`, `Read` tool added
  - Unsupported formats (PDF, ZIP, video, audio) rejected with user-friendly message
  - Filename sanitization (path traversal, hidden files, special chars)
  - File upload rate limit: 5 files/min per user
  - Per-session upload dirs cleaned after execution
  - `files:read` OAuth scope (requires Slack app reinstall)
  - Max 10 files per message
- **Future**: PDF support (text extraction), agent→Slack outbound file attachments (Phase 2)
- **Flow**: `docs/memory/feature-flows/slack-file-sharing.md`

### 15.1c Telegram Bot Integration (TGRAM-001)
- **Status**: ✅ Complete (2026-04-10)
- **Requirement ID**: TGRAM-001
- **Priority**: P2
- **Description**: Per-agent Telegram bot integration. Each agent gets its own Telegram bot (1:1 via @BotFather), shareable via `t.me/BotUsername` links. Reuses the `ChannelAdapter` abstraction proven by SLACK-002.
- **Key Features**:
  - Frontend UI: `TelegramChannelPanel.vue` in Agent Detail → Sharing tab (connect, verify, disconnect, webhook warning)
  - Per-agent bots (one bot per agent, token encrypted via AES-256-GCM)
  - Bidirectional text chat (users message bot → agent responds via HTML formatting)
  - Photo and document support (download, text extraction for plain text files)
  - Webhook mode (production) — returns 200 immediately, processes async
  - Webhook reconciliation on backend startup (re-registers all active bots)
  - Bot commands: `/start`, `/help`, `/reset` (clear conversation)
  - Message splitting at 4096 char limit (paragraph boundaries)
  - Telegram API 429 retry with `retry_after` backoff
  - No new Python dependencies (httpx only)
  - Router generalization: `ChannelMessageRouter` now uses adapter methods instead of Slack-specific hardcodings
- **Database Tables**:
  - `telegram_bindings` — Maps bots to agents (bot_id UNIQUE, bot_username, webhook_secret, telegram_secret_token, encrypted bot token)
  - `telegram_chat_links` — Maps Telegram users to sessions (binding_id, telegram_user_id, message_count)
- **API Endpoints**:
  - `POST /api/telegram/webhook/{webhook_secret}` — Receive Telegram updates (validated by X-Telegram-Bot-Api-Secret-Token header)
  - `GET /api/agents/{name}/telegram` — Bot binding status
  - `PUT /api/agents/{name}/telegram` — Configure bot token (validates via getMe)
  - `DELETE /api/agents/{name}/telegram` — Remove bot binding + delete webhook
  - `POST /api/agents/{name}/telegram/test` — Test bot connectivity / send test message
- **Security**:
  - Bot tokens AES-256-GCM encrypted at rest (same `CredentialEncryptionService` as Slack)
  - Webhook auth via `X-Telegram-Bot-Api-Secret-Token` header (set during setWebhook)
  - SSRF prevention: media downloads restricted to `api.telegram.org` domain
  - Restricted tools for Telegram users (WebSearch, WebFetch — same as Slack)
  - Update dedup via `last_update_id` tracking
  - Bot token values never logged
- **Flow**: `docs/memory/feature-flows/telegram-integration.md`
- **Future Phases**:
  - Phase 2: Voice transcription (Whisper API), notification forwarding
  - Phase 3: Inline keyboards for approve/reject, deep links with start parameters

### 15.1e Telegram Group Chat Support (TGRAM-GROUP)
- **Status**: ✅ Complete (2026-04-11)
- **Requirement ID**: TGRAM-GROUP
- **Priority**: P2
- **Description**: Agents participate in Telegram group chats. Bots respond to @mentions and direct replies in groups. Per-group configuration for trigger mode and welcome messages.
- **Key Features**:
  - Group message handling: respond to @mentions of bot username in message entities
  - Reply-to-bot detection: respond when user replies to bot's own messages
  - Configurable trigger mode per group: "mention" (default, @mention-only), "all" (all messages), or "observe" (all messages but agent can return `[NO_REPLY]` to skip responding)
  - Sender identity context in group messages: `[Group: title] [From: @username (Name)]` prefix so agent knows who is speaking
  - New member welcome messages: configurable text with {name} placeholder
  - Auto-created group configs on first interaction (no manual setup required)
  - Bot added/removed from group detection via `my_chat_member` update events
  - User join/leave detection via `chat_member` events (requires bot admin in group)
  - Fresh context per group message (no prior session history to prevent context bleed)
  - Silent rate limit drops in groups (no error messages visible to all members)
  - Mention text stripped from agent input for cleaner prompts
  - Commands support @botname suffix in groups (e.g., `/help@mybot`)
  - Uses modern Telegram `reply_parameters` API for threaded replies
- **Database Tables**:
  - `telegram_group_configs` — Per-group settings (binding_id, chat_id, trigger_mode, welcome_enabled, welcome_text, is_active)
- **API Endpoints**:
  - `GET /api/agents/{name}/telegram/groups` — List group configs
  - `PUT /api/agents/{name}/telegram/groups/{id}` — Update group config (trigger mode, welcome settings)
  - `DELETE /api/agents/{name}/telegram/groups/{id}` — Remove group config (deactivates)
  - `POST /api/agents/{name}/telegram/groups/{chat_id}/messages` — Proactive group messaging (rate limited: 10/hr/group, 100/hr/agent)
- **MCP Tools**:
  - `list_channel_groups` — List Telegram groups the agent is connected to
  - `send_group_message` — Send proactive message to a group (supports Telegram HTML, max 4096 chars)
- **Webhook Changes**:
  - `allowed_updates` now includes `["message", "my_chat_member", "chat_member"]`
- **Security**:
  - Same webhook auth (URL secret + header token) — no new attack surface
  - Group membership not re-verified per message (documented limitation)
  - Bot loop prevention inherited from TGRAM-001 (`is_bot` check)
- **Frontend**: TelegramChannelPanel extended with group list, trigger mode radio, welcome message config
- **Flow**: `docs/memory/feature-flows/telegram-integration.md`

### 15.1g Webhook Triggers for Agent Schedules (WEBHOOK-001)
- **Status**: ✅ Implemented (2026-04-24)
- **Requirement ID**: WEBHOOK-001
- **Priority**: P1
- **Issue**: #291
- **Description**: Each agent schedule can optionally expose a public webhook URL so that external systems (CI/CD pipelines, CRMs, monitoring tools) can trigger schedule executions via a simple HTTP POST — no Trinity account or JWT required. Authentication is provided by a 256-bit opaque token embedded in the URL.
- **Key Features**:
  - Public trigger endpoint `POST /api/webhooks/{token}` — no JWT; returns 202 Accepted immediately
  - JWT-auth management endpoints to generate, inspect, and revoke webhook tokens per schedule
  - Token rotation: calling `POST .../webhook` again generates a new token and instantly invalidates the old URL
  - Rate limiting: 10 calls / 60 s per token (Redis-backed, fail-open on Redis unavailability)
  - Optional `context` field (max 4000 chars) appended to schedule message with framing wrapper to reduce prompt injection surface
  - All trigger events audit-logged with `triggered_by="webhook"` in execution records
  - O(1) token lookup via partial unique index on `agent_schedules.webhook_token`
  - **Creation gated on a live owning agent (#1445)**: schedule creation and webhook generation are rejected (fail-loud **404**) when the agent has no live `agent_ownership` row (`deleted_at IS NULL`); non-owners get a uniform **403** regardless of existence (no enumeration oracle). Guarantees a webhook token always resolves to a schedule of a live agent — no orphan schedules whose tokens 404 at trigger time under the #1423 soft-delete-aware token lookup
- **Database Changes**:
  - `agent_schedules.webhook_token TEXT` — nullable 43-char urlsafe token
  - `agent_schedules.webhook_enabled INTEGER DEFAULT 0`
  - `CREATE UNIQUE INDEX idx_schedules_webhook_token ON agent_schedules(webhook_token) WHERE webhook_token IS NOT NULL`
- **API Endpoints**:
  - `POST /api/webhooks/{token}` — public trigger (no auth)
  - `POST /api/agents/{name}/schedules/{id}/webhook` — generate/rotate token
  - `GET /api/agents/{name}/schedules/{id}/webhook` — get status and URL
  - `DELETE /api/agents/{name}/schedules/{id}/webhook` — revoke token
- **Security**:
  - Token: `secrets.token_urlsafe(32)` (256-bit entropy); stored plaintext (it is the credential, not a password)
  - Token format validated by regex before DB lookup to prevent injection
  - Rate limit keyed per token (not per IP) — matches the threat model of a shared trigger URL
  - `Retry-After` header included in 429 responses
  - Context field length-capped and framed as data, not instructions
- **No UI in Phase 1**: webhook URL returned in management endpoint response; no SchedulesPanel widget yet
- **Flow**: `docs/memory/feature-flows/webhook-triggers.md`

### 15.1f WhatsApp via Twilio (WHATSAPP-001)
- **Status**: 🚧 Phase 1 MVP (2026-04-22)
- **Requirement ID**: WHATSAPP-001
- **Priority**: P2
- **Issue**: #299
- **Description**: Per-agent WhatsApp integration via Twilio's Programmable Messaging API. Each agent owner brings their own Twilio account + WhatsApp sender number — no platform-level Twilio account required. Extends the `ChannelAdapter` abstraction from SLACK-002.
- **Phase 1 (shipped — this PR)**:
  - Direct-message chat: inbound via Twilio webhook → agent → outbound via Twilio REST (`POST /Messages.json`)
  - Frontend: `WhatsAppChannelPanel.vue` in Agent Detail → Sharing tab (connect form with AccountSid/AuthToken/from-number, sandbox detection, copy-webhook-URL button, disconnect/verify)
  - Credentials: AuthToken encrypted AES-256-GCM via `CredentialEncryptionService`; AccountSid plaintext (public identifier)
  - Webhook: dual-factor auth — URL `webhook_secret` routes to binding, `X-Twilio-Signature` HMAC-SHA1 validated via `twilio.request_validator.RequestValidator` (handles Twilio's empty-param inclusion gotcha)
  - Dedup by `MessageSid` (2048-entry in-memory ring) to absorb Twilio retries
  - Media inbound: images/audio/PDFs via Twilio-hosted URLs with HTTP Basic auth; SSRF-gated to `*.twilio.com` domain suffix; `follow_redirects=False` with allowlisted single-redirect follow for signed-URL media
  - Message splitting at 1600-char Twilio WhatsApp limit (paragraph → sentence → word boundaries)
  - Sandbox auto-detection from well-known number `whatsapp:+14155238886`
  - Empty TwiML (`<Response/>`) returned to Twilio ack; response delivered asynchronously via REST (no TwiML body response path)
- **Phase 2 (deferred)**:
  - `#311` unified access control: `/login <email>` command flow, verified-email gate, `access_requests` pipeline (schema columns `verified_email`/`verified_at` shipped up-front so Phase 2 is application-only)
- **Phase 3 (partial)**:
  - **Outbound media attachments (shipped — #1315)**: `WhatsAppAdapter.send_response` delivers `ChannelResponse.files` as Twilio `MediaUrl` attachments — one message per file (WhatsApp permits a single media per message), text sent first. Each file is persisted to FILES-001 storage via `create_share_from_bytes` and handed to Twilio as its public `?sig=` URL (Twilio fetches it server-side). Gated on the per-agent `file_sharing_enabled` toggle. Short 1h share TTL (the cleanup-service reaper purges expired shares). Graceful text-link fallback when `public_chat_url` is unset/non-HTTPS, the MIME is unsupported, or the file exceeds WhatsApp caps (image/audio/video ≈5 MB, documents ≈16 MB) — never silently dropped.
- **Phase 3 (deferred)**:
  - SMS on the same Twilio binding (drop `whatsapp:` prefix)
  - Message templates for outbound-first conversations outside the 24h customer-service window (Twilio Content Builder)
  - Interactive buttons and list messages (Twilio Content API)
  - Voice-note transcription (Whisper API)
  - Promoting FILES-001 share URLs already present in `response.text` to `MediaUrl` (#1315 option b)
- **Out of scope**:
  - WhatsApp group chats — not supported by Twilio's WhatsApp API
  - Meta Cloud API direct integration — future alternative; not this PR
- **Database Tables**:
  - `whatsapp_bindings` — One Twilio sender per agent (account_sid, auth_token_encrypted, from_number, messaging_service_sid, is_sandbox, webhook_secret, webhook_url, created_by)
  - `whatsapp_chat_links` — WhatsApp phone → session map (binding_id, wa_user_phone, wa_user_name, verified_email, verified_at, message_count)
- **API Endpoints**:
  - `POST /api/whatsapp/webhook/{webhook_secret}` — Twilio inbound webhook (HMAC-SHA1 validated)
  - `GET /api/agents/{name}/whatsapp` — Binding status (includes computed webhook URL + warning)
  - `PUT /api/agents/{name}/whatsapp` — Configure credentials (validates via Twilio Account fetch)
  - `DELETE /api/agents/{name}/whatsapp` — Remove binding (cascades to chat_links)
  - `POST /api/agents/{name}/whatsapp/test` — Verify credentials / send test message
- **Security**:
  - AuthToken AES-256-GCM encrypted at rest; never logged in full
  - Webhook signature verification via `twilio>=9.10.5` `RequestValidator` (constant-time compare)
  - SSRF allowlist on media downloads: `*.twilio.com` only, no redirects to other hosts
  - Phone number masking in logs: `whatsapp:+141***5309`
  - Unknown webhook secrets return 200 empty TwiML (no binding-existence oracle)
  - Rate limiting via `ChannelMessageRouter` defaults (30 msg/min per sender)
  - Webhook auth requires uvicorn `--proxy-headers --forwarded-allow-ips='*'` so `request.url` reconstructs correctly behind Cloudflare Tunnel + nginx (shipped in this PR's `docker/backend/Dockerfile`)
- **Deployment prerequisite**:
  - Cloudflare Tunnel ingress rules must route `/api/whatsapp/webhook/*` to the frontend service (manual dashboard step — see `docs/requirements/PUBLIC_EXTERNAL_ACCESS_SETUP.md`)
- **Dependencies**:
  - `twilio==9.10.5` — used only for `RequestValidator` (signature verification); outbound send stays in `httpx` matching the Telegram pattern
- **Flow**: `docs/memory/feature-flows/whatsapp-integration.md`

### 15.1d Public Chat Session Memory (PUB-006)
- **Status**: ⏳ Not Started
- **Requirement ID**: PUB-006
- **Priority**: P2
- **Description**: Optional per-session persistent memory for public link users. When enabled, the agent accumulates structured knowledge about a user across the entire session lifetime — beyond simple message history replay — so it can recall facts, preferences, and context without re-reading every prior message.
- **Problem Statement**:
  - Currently, context is injected by concatenating the last N messages (max 10 turns) into the prompt. This breaks for long conversations (context window pressure) and loses facts from early in the session.
  - There is no mechanism for the agent to "remember" key facts it learned (e.g., user's name, goals, constraints) independent of raw message replay.
- **Key Features**:
  - **Opt-in per public link**: Toggle `memory_enabled` on `agent_public_links` (default: `false`). The public link creation/edit UI exposes this setting.
  - **Session metadata store**: Agent can write a structured JSON blob (`session_context`) to the session record after each turn — capturing facts, preferences, and a rolling summary of the conversation.
  - **Smart context injection**: When memory is enabled, the context prompt is built from (a) `session_context` (agent-written facts) + (b) recent messages (last 5 turns), instead of the current flat 10-turn replay. This keeps prompts shorter and more focused.
  - **Agent write-back endpoint**: `POST /api/public/session/{token}/memory` — accepts `session_context` JSON from the agent container after execution completes. The backend calls this automatically by passing the agent's response through a structured extraction step, OR the agent itself calls it via an injected tool.
  - **Session summary**: After every N turns (configurable, default 5), the backend triggers a lightweight summarization call to condense prior messages into `session_context`, freeing up context window space.
  - **No extra infra**: Uses existing `public_chat_sessions` table (add `session_context TEXT` column and `memory_enabled` on links table). No vector DB required in Phase 1.
- **Database Changes**:
  - `agent_public_links`: add `memory_enabled INTEGER DEFAULT 0`
  - `public_chat_sessions`: add `session_context TEXT` (JSON), `summarized_through_message_id TEXT`
- **API Endpoints**:
  - `PUT /api/agents/{name}/public-links/{id}` — extend to include `memory_enabled` toggle (reuse existing link update endpoint)
  - `POST /api/public/session/{token}/memory` — agent or backend writes updated `session_context`; requires valid `session_id` query param; no external auth (link token is sufficient)
  - `GET /api/public/history/{token}` — extend response to include `session_context` when memory is enabled
- **Context Prompt Format (memory enabled)**:
  ```
  ### Session Context (what I know about this user)
  {session_context JSON rendered as bullet list}

  ### Recent conversation (last 5 turns)
  User: ...
  Assistant: ...

  ### Current message
  User: {new_message}
  ```
- **Context Prompt Format (memory disabled)** — unchanged from current behavior (last 10 turns flat replay).
- **Summarization Trigger**: After every 5th user message, if `memory_enabled`, the backend schedules a background task that:
  1. Fetches all messages not yet covered by `summarized_through_message_id`
  2. Calls the agent with a special prompt asking it to extract structured facts and update `session_context`
  3. Saves the updated `session_context` and advances `summarized_through_message_id`
- **Frontend Changes**:
  - Public link settings panel: add "Session Memory" toggle with tooltip explaining behavior
  - No changes to `PublicChat.vue` — transparent to end users
- **Architecture Notes**:
  - Phase 1: Structured JSON blob in SQLite (no vector DB, no embeddings). Sufficient for most agent-user sessions.
  - Phase 2 (future): Embed messages and do semantic retrieval for very long sessions (>50 turns). Out of scope for PUB-006.
  - Memory is scoped to a single `public_chat_session` — there is no cross-session or cross-link memory.
  - Anonymous sessions have the same memory capability as email-verified sessions.

### 15.2 First-Time Setup
- **Status**: ✅ Implemented (2025-12-23; streamlined trinity-enterprise#49, 2026-06-23)
- **Description**: Admin-account wizard on fresh install — a welcoming, animated single-screen first-run page (orbiting fleet constellation)
- **Key Features**: Bcrypt hashing, API key configuration in Settings. **Streamlined (#49)**: the log-copied **setup token was removed** (no token field); **admin email is required** (becomes the sign-in identity) with field order email → password (+confirm) → company → updates opt-in. Security tradeoff of token removal is an explicit operator responsibility (deploy behind a tunnel/VPN until setup completes) — see `docs/DEPLOYMENT.md` → Security Recommendations
- **Flow**: `docs/memory/feature-flows/first-time-setup.md`

### 15.3 Per-Agent API Key Control
- **Status**: ✅ Implemented (2025-12-26)
- **Description**: Agents can use platform API key or user's own Claude subscription
- **Key Features**: Toggle in Terminal tab, container recreation on change

---

## 23. Nevermined Payment Integration (x402 Protocol)

> **Design**: Per-agent monetization via Nevermined x402 payment protocol.
> Spec: `docs/requirements/NEVERMINED_PAYMENT_INTEGRATION.md`
> Flow: `docs/memory/feature-flows/nevermined-payments.md`

### 23.1 Backend Foundation
- **Status**: ✅ Implemented (2026-03-04)
- **Requirement ID**: NVM-001
- **Description**: Database schema, encrypted credential storage, payment logging
- **Key Features**:
  - `nevermined_agent_config` table with AES-256-GCM encrypted NVM_API_KEY
  - `nevermined_payment_log` audit trail for verify/settle/reject actions
  - Migration #23 (idempotent)
  - `NeverminedOperations` DB module following subscription pattern

### 23.2 Payment Service
- **Status**: ✅ Implemented (2026-03-04)
- **Requirement ID**: NVM-001
- **Description**: Verify/settle lifecycle via payments-py SDK
- **Key Features**:
  - `NeverminedPaymentService` with lazy SDK imports (`NEVERMINED_AVAILABLE` flag)
  - `build_402_response()` using SDK's `build_payment_required()` helper
  - `verify_payment()` — 15s timeout, wrapped in `asyncio.to_thread()`
  - `settle_payment()` — 30s timeout, 3 retries with exponential backoff
  - Graceful degradation: 501 if SDK not installed

### 23.3 Paid Chat Endpoint
- **Status**: ✅ Implemented (2026-03-04)
- **Requirement ID**: NVM-001
- **Description**: Public x402 endpoint for external callers
- **Endpoints**:
  - `POST /api/paid/{agent_name}/chat` — 402/403/200 flow; accepts `Idempotency-Key` (#1018)
  - `GET /api/paid/{agent_name}/info` — Public agent info + payment requirements
- **Flow**: No header → 402; invalid token → 403; valid → verify → (idempotency gate) → execute → settle → receipt
- **Settlement-ordering / honest status (#1018)**: `verify_payment` does **not** burn credits —
  only `settle` does. When a settle fails after retries the endpoint keeps HTTP 200 + the delivered
  `response` (deliver-then-reconcile) but the top-level `status` is **`"success_unsettled"`** (NOT the
  old lying `"success"`), with `payment:{settled:false, settle_retry_needed:true, error}`. A concurrent
  settle held by the #1084 effect guard returns `status:"success_unsettled"` +
  `payment:{settled:false, settle_in_progress:true}` — the concurrently-running settle (holding the same
  local `agent_request_id` guard claim) completes it once; the guard, not a provider token, is what
  prevents a double-burn (`agent_request_id` is a Nevermined observability id, not an exactly-once token —
  residual at-least-once retry tracked by #1408). A `failed` execution no longer echoes the response body
  (a `cancelled` turn still does, #679).
- **Idempotency (Invariant #18, #1018)**: the boundary accepts an optional `Idempotency-Key` header
  but always keys on `sha256(payment-signature ∥ message)` (the native client-retry unit) — a client
  re-POST replays the completed work and re-attempts settle rather than re-running the LLM (double cost).
  In-flight duplicate → 409; a completed **settled** snapshot replays verbatim (`X-Idempotent-Replay: true`);
  a completed **unsettled** snapshot re-drives `settle_payment_once` (deduped only against a *concurrent*
  same-id settle via the `payment:{agent_request_id}` guard — not provider-idempotent; #1408) and
  converges the stored snapshot to settled. A divergent
  client header never forks execution; an underivable key (missing token/body) disables dedup (fail-open,
  never a constant collision).

### 23.4 Admin Configuration
- **Status**: ✅ Implemented (2026-03-04)
- **Requirement ID**: NVM-001
- **Description**: Authenticated CRUD for Nevermined config
- **Endpoints**:
  - `POST/GET/DELETE /api/nevermined/agents/{name}/config`
  - `PUT /api/nevermined/agents/{name}/config/toggle`
  - `GET /api/nevermined/agents/{name}/payments`
  - `GET /api/nevermined/settlement-failures` (admin)
  - `POST /api/nevermined/retry-settlement/{log_id}` (admin) — honest **501** (#1018): server-side
    retry is unsupported because the payer access token is not stored (it is a whole-plan-budget bearer
    credential). Previously a misleading 200 "queued" stub that did nothing. The workable path is
    client-driven — re-present the same `payment-signature` to `/api/paid/{agent}/chat` (replays +
    re-settles deterministically). Durable stored-credential server-side retry is a Tier 2 follow-up.

### 23.5 Frontend UI
- **Status**: ✅ Implemented (2026-03-04)
- **Description**: Payments tab in Agent Detail page
- **Component**: `NeverminedPanel.vue`
- **Features**: Config form, enable/disable toggle, paid endpoint URL display, payment log table

### 23.6 MCP Tools
- **Status**: ✅ Implemented (2026-03-04)
- **Description**: 4 MCP tools for Nevermined management
- **Tools**: `configure_nevermined`, `get_nevermined_config`, `toggle_nevermined`, `get_nevermined_payments`

---

## 27. Mobile Admin PWA (MOB-001)

Standalone mobile-friendly admin page for managing agents on the go. Designed as a Progressive Web App (PWA) installable from URL — completely separate from the main UI with no navigation links to it.

### 27.1 Mobile Admin Page
- **Status**: ✅ Implemented (2026-03-14)
- **Requirement ID**: MOB-001
- **Description**: Standalone Vue page at `/m` route, admin-only, not linked from main UI navigation. Self-contained login + management interface optimized for mobile devices.
- **Key Features**:
  - Admin password login (inline, no redirect to main login page)
  - Bottom tab navigation (mobile UX pattern): Agents, Ops, System
  - Dark theme by default (OLED-friendly)
  - Touch-optimized: 16px min font, large tap targets, no hover-dependent interactions
  - iOS safe area support (`viewport-fit=cover`, notch handling)
  - Pull-to-refresh on all tabs
  - Auto-polling with configurable intervals

### 27.2 Agents Tab
- **Status**: ⏳ Not Started
- **Requirement ID**: MOB-001-AGENTS
- **Description**: Simplified agent list with essential management actions
- **Key Features**:
  - Agent cards showing: name, status (running/stopped), type badge
  - Start/stop toggle per agent (inline)
  - Tap agent to expand: logs tail, CPU/memory stats, last activity
  - Search/filter by name
  - System agents hidden by default

### 27.3 Ops Tab
- **Status**: ⏳ Not Started
- **Requirement ID**: MOB-001-OPS
- **Description**: Mobile-optimized Operating Room showing items needing attention
- **Key Features**:
  - Needs Response queue with expandable cards
  - Respond/acknowledge actions inline
  - Notification list with priority badges
  - Badge count on tab icon
  - Cost alerts summary

### 27.4 System Tab
- **Status**: ⏳ Not Started
- **Requirement ID**: MOB-001-SYSTEM
- **Description**: Fleet-level operations and health overview
- **Key Features**:
  - Fleet health summary (running/stopped/error counts)
  - Emergency stop button (with confirmation)
  - Fleet restart action
  - Schedule pause/resume all
  - Cost overview from `/api/ops/costs`

### 27.5 PWA Configuration
- **Status**: ⏳ Not Started
- **Requirement ID**: MOB-001-PWA
- **Description**: Progressive Web App manifest and service worker for "Add to Home Screen" installation
- **Key Features**:
  - Web App Manifest (`mobile-manifest.json`): standalone display, portrait orientation, Trinity branding
  - Service Worker: network-first with cache fallback for static assets, skip API caching
  - iOS PWA meta tags: `apple-mobile-web-app-capable`, status bar style, touch icons
  - Start URL: `/m` (auto-loads mobile admin)
  - Shortcuts: Agents tab, Ops tab
- **Reference**: DGX Sparky PWA implementation (internal DGX project, not in this repo) for patterns

### 27.6 Mobile CSS Optimizations
- **Status**: ⏳ Not Started
- **Requirement ID**: MOB-001-CSS
- **Description**: Mobile-specific CSS for native-feeling interactions
- **Key Features**:
  - `touch-action: manipulation` (remove 300ms tap delay)
  - `-webkit-tap-highlight-color: transparent`
  - 16px minimum input font size (prevents iOS auto-zoom)
  - iOS keyboard handling via `visualViewport` API
  - CSS safe area insets for notched devices
  - Prevent overscroll bounce on iOS

---

## 46. External Client Roster (Access/Sharing redesign — epic #16)

### 46.1 Surface External Channel Clients on the Sharing Tab (#20)

**Description**: The Sharing tab answers "who is actually talking to this agent
through a channel?" with a read-only **client roster** — external users (no
Trinity account) who have messaged the agent via a channel. Activity is already
collected per channel link (`verified_email`, `message_count`, `last_active`)
but was never surfaced. This is the read surface; per-client controls
(block/revoke/approve) are a separate follow-up (#21).

- **FR-1 — Aggregated roster endpoint**: `GET /api/agents/{name}/clients`
  (owner/admin via `OwnedAgentByName`) returns one entry per external client
  across channels: `channel`, `identity` (channel-native handle/phone),
  `display_name`, `verified_email`, `message_count`, `last_active`. Sorted by
  `last_active` descending (never-active rows last).
- **FR-2 — Channel coverage**: roster v1 covers **Telegram**
  (`telegram_bindings` → `telegram_chat_links`) and **WhatsApp**
  (`whatsapp_bindings` → `whatsapp_chat_links`) — the channels that record the
  full `(verified_email, message_count, last_active)` triple per user. Slack
  (verifications carry email but no activity counters) and VoIP (call logs) are
  additive follow-ups; the response model is channel-extensible so they slot in
  without a contract change.
- **FR-3 — Read-only**: no write actions in this slice. The roster renders even
  when the agent container is stopped (DB-sourced). Empty roster renders an
  explicit empty state, not an error.
- **FR-4 — Tenant boundary**: the endpoint is scoped to the path agent; the DB
  join filters by `agent_name` through the channel binding, so a client of
  another agent never appears.

---

## 47. Public & Channel Custom Instructions (#1205)

### 47.1 Per-Agent Public-Facing System-Prompt Fragment (#1205)

**Description**: An agent owner can attach extra system-prompt instructions that
apply **only to public-facing conversations** — public links, channel chats
(Slack/Telegram/WhatsApp), and x402 paid chat — without changing the agent's
behavior in their own authenticated chats, scheduled runs, loops, or
agent-to-agent calls. The text-surface counterpart of `voice_system_prompt`.
Edited from the Sharing tab.

- **FR-1 — Storage**: `agent_ownership.public_channel_system_prompt TEXT`
  (versioned migration, Invariant #3). Unset/empty is the default.
- **FR-2 — API**: owner-only `GET/PUT /api/agents/{name}/public-prompt`
  mirroring the voice-prompt endpoints; PUT enforces a 4000-char cap;
  empty/whitespace clears the value.
- **FR-3 — Injection surfaces**: when set, the fragment is folded into the
  `caller_prompt` passed to `compose_system_prompt` at exactly three sites —
  `adapters/message_router.py` (all channels), `routers/public.py` (public chat
  sync + async), and `routers/paid.py` (x402). It composes with the MEM-001
  per-user memory block (public fragment first, then memory).
- **FR-4 — Scope exclusion**: NOT applied to authenticated web Chat/Session
  tabs, scheduled executions, loops, or agent-to-agent calls — those paths never
  call the folding helper.
- **FR-5 — Strict no-op**: an unset value changes nothing for existing agents;
  a DB lookup failure degrades to the memory block alone (never blocks a chat).
- **FR-6 — Group chats**: applied to group channels too (group surfaces are
  public-facing).
