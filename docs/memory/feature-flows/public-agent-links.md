# Feature Flow: Public Agent Links (12.2)

**Last Updated**: 2026-05-03

## Overview

Public Agent Links allow agent owners to generate shareable URLs that enable unauthenticated users to chat with their agents. This feature supports usage tracking and rate limiting, and honors the agent-level **Channel Access Policy** (`require_email`, `open_access`) which is shared with Slack/Telegram — see [unified-channel-access-control.md](unified-channel-access-control.md).

> **Refactor (2026-04-13)**: The per-link `require_email` flag has been removed from the API/UI. Public web chat now uses the agent-level policy (`agent_ownership.require_email` / `agent_ownership.open_access`) as the single source of truth. The `agent_public_links.require_email` column still exists in the schema (unused by public chat; retained for the legacy `slack_link_connections` join in `db/slack.py`). Existing email-required links were preserved by ORing the flag into `agent_ownership.require_email` via migration `public_link_require_email_unified`.

## User Stories

1. **As an agent owner**, I want to create public links so prospects can demo my agent without logging in.
2. **As an agent owner**, I want to require email verification to track who uses my agent.
3. **As an agent owner**, I want to see usage statistics for each public link.
4. **As a public user**, I want to chat with an agent using just a URL.
5. **As a public user**, I want a simple verification flow if required.

## Architecture

```
+-------------------------------------------------------------------+
|                         Frontend                                   |
+-------------------------------------------------------------------+
|  PublicLinksPanel.vue          PublicChat.vue                     |
|  (Owner management)            (Public chat interface)            |
|                                                                    |
|  - Create/edit/delete links    - Email verification flow          |
|  - Copy link URL               - Chat interface (async mode)      |
|  - View usage stats            - Session management               |
|  - Enable/disable              - SSE status streaming (THINK-001) |
|                                - Error handling                   |
+-------------------------------------------------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
|                         Backend API                                |
+-------------------------------------------------------------------+
|  routers/public_links.py       routers/public.py                  |
|  (Authenticated)               (Unauthenticated + JWT optional)   |
|                                                                    |
|  - CRUD endpoints              - Link validation                  |
|  - Owner verification          - Email verification               |
|  - Usage stats                 - Public chat (async mode)         |
|                                - SSE stream proxy (THINK-001)     |
|                                - Execution status polling         |
|                                - Session list (JWT, #587)         |
|                                - Session detail (JWT, #587)       |
+-------------------------------------------------------------------+
                                  |
         +------------------------+------------------------+
         v                        v                        v
+------------------+  +----------------------+  +------------------+
|   SQLite DB      |  |   Email Service      |  |TaskExecutionSvc  |
+------------------+  +----------------------+  +------------------+
| agent_public_    |  | Console (dev)        |  | execute_task()   |
|   links          |  | SMTP                 |  |   -> slot mgmt   |
| public_link_     |  | SendGrid             |  |   -> exec record |
|   verifications  |  |                      |  |   -> activity    |
| public_link_     |  |                      |  |   -> agent POST  |
|   usage          |  |                      |  |   -> sanitize    |
| schedule_        |  |                      |  |                  |
|   executions     |  |                      |  |                  |
| public_user_     |  |                      |  |                  |
|   memory (MEM-001)|  |                      |  |                  |
+------------------+  +----------------------+  +------------------+
```

## Entry Points

### Owner Interface
- **UI**: `src/frontend/src/views/AgentDetail.vue` - "Sharing" tab (contains both Team Sharing and Public Links)
- **Component**: `src/frontend/src/components/SharingPanel.vue:82-83` - Embeds PublicLinksPanel
- **Component**: `src/frontend/src/components/PublicLinksPanel.vue` - Full management panel

> **Note (2026-02-18)**: The "Public Links" tab was consolidated into the "Sharing" tab. PublicLinksPanel is now rendered within SharingPanel.vue, separated by a divider from the Team Sharing section.

### Public Interface
- **UI**: `src/frontend/src/views/PublicChat.vue` - Public chat page
- **Route**: `/chat/:token` defined in `src/frontend/src/router/index.js:18-22`

## Data Flow

### 1. Create Public Link (Owner)

```
Owner (AgentDetail) -> POST /api/agents/{name}/public-links
                    -> Backend validates ownership
                    -> Generate unique token (secrets.token_urlsafe(24))
                    -> Insert into agent_public_links
                    -> Return link with URL
```

### 2. Public Chat (No Email Required)

> **Note (2026-04-13)**: `require_email` in the `PublicLinkInfo` response now reflects the agent-level policy (`agent_ownership.require_email`) via helper `_agent_requires_email(agent_name)` in `routers/public.py`. It is no longer a per-link flag.

```
Public User -> GET /api/public/link/{token}
            -> Backend returns {valid: true, require_email: false}

Public User -> POST /api/public/chat/{token}  {async_mode: true}
            -> Backend validates token
            -> Check rate limit (30/min per IP)
            -> Record usage
            -> Create execution record early (for SSE)
            -> Spawn _execute_public_chat_background()
            -> Return {status: "accepted", execution_id, agent_name}

Frontend -> GET /api/public/executions/{token}/{execution_id}/stream
         -> SSE proxy: agent status events (Reading file, Running command, etc.)
         -> Frontend maps events to loading labels via getStatusFromStreamEvent()

Frontend -> GET /api/public/executions/{token}/{execution_id}/status (polling, 5s)
         -> Returns {status, response, error}
         -> Frontend displays assistant response on success

Background -> TaskExecutionService.execute_task(triggered_by="public")
           -> Acquire capacity slot (429 if at capacity)
           -> Track activity start
           -> POST agent /api/task (with retry)
           -> Sanitize credentials from response
           -> Track activity completion
           -> Release slot
           -> Store assistant response in public_chat_messages
```

> **EXEC-024 (2026-03-04)**: Public chat now routes through `TaskExecutionService` instead of a raw `httpx` call. This means public executions create `schedule_executions` records, appear in the Tasks tab and Dashboard timeline, count toward agent capacity slots, and have credential-sanitized logs.

> **THINK-001 (2026-03-04)**: Public chat uses async mode with SSE streaming for real-time status labels. The frontend submits with `async_mode: true`, subscribes to the SSE stream for status updates (same `execution-status.js` utilities as authenticated chat), and polls for completion.

### 3. Public Chat (Email Required — Agent Policy)

> **Refactor (2026-04-13)**: The trigger for the email verification flow is now the **agent-level policy** (`agent_ownership.require_email`), not a per-link flag. After email verification succeeds, `public_chat` runs the same unified access gate used by Slack/Telegram (see [unified-channel-access-control.md](unified-channel-access-control.md)).

```
Public User -> GET /api/public/link/{token}
            -> _agent_requires_email(agent_name) reads agent_ownership.require_email
            -> Backend returns {valid: true, require_email: true}

Public User -> POST /api/public/verify/request
            -> Backend generates 6-digit code
            -> Email service sends code (subject/body now include agent name — #890)
            -> Return {expires_in_seconds: 600}

Public User -> POST /api/public/verify/confirm
            -> Backend validates code
            -> Generate session_token (24h validity)
            -> Return {session_token: "..."}

Public User -> POST /api/public/chat/{token}
              {message: "...", session_token: "..."}
            -> Backend validates session -> verified email
            -> Unified access gate (routers/public.py, same rules as message_router):
                 1. db.email_has_agent_access(agent_name, email)  -> allow
                 2. else agent_ownership.open_access              -> allow
                 3. else db.upsert_access_request(agent, email, "web")
                          -> 403 "pending approval"
            -> Chat proceeds as normal
```

**Gate helpers** (`routers/public.py`, near top of module):
- `_agent_requires_email(agent_name)` — reads `agent_ownership.require_email`
- `_agent_allows_open_access(agent_name)` — reads `agent_ownership.open_access`

**Owner/admin/shared** users in `agent_sharing` are admitted by step (1). Everyone else falls through to `open_access` (public-by-default) or creates a pending row in `access_requests` which the owner can approve from the Access Requests UI.

## Frontend Layer

### Components

| File | Line | Description |
|------|------|-------------|
| `src/frontend/src/components/SharingPanel.vue` | 82-83 | Embeds PublicLinksPanel within Sharing tab |
| `src/frontend/src/components/SharingPanel.vue` | 92 | Import statement for PublicLinksPanel |
| `src/frontend/src/components/PublicLinksPanel.vue` | 1-503 | Owner management panel |
| `src/frontend/src/views/PublicChat.vue` | 1-786 | Public chat interface (PUB-003 intro, PUB-004 header metadata, PUB-005 session persistence, PUB-006 bottom-aligned messages, THINK-001 dynamic status) |

> **Note (2026-02-18)**: AgentDetail.vue no longer directly renders PublicLinksPanel. The component is now embedded within SharingPanel.vue.

> **Note (2026-04-13)**: `PublicLinksPanel.vue` no longer shows the "Require email verification" checkbox in the create/edit modal and no longer renders the "Email Required" badge on link rows. A small pointer directs owners to the agent-level **Channel Access Policy** section for those settings. See [unified-channel-access-control.md](unified-channel-access-control.md).

### PublicLinksPanel.vue Methods

| Method | Line | Description |
|--------|------|-------------|
| `loadLinks()` | 345 | Fetch all links for agent |
| `saveLink()` | 360 | Create or update link |
| `toggleLink()` | 400 | Enable/disable link |
| `editLink()` | 417 | Open edit modal |
| `deleteLink()` | 433 | Delete with confirmation |
| `copyLink()` | 452 | Copy URL to clipboard |

### PublicChat.vue Methods

| Method | Line | Description |
|--------|------|-------------|
| `loadLinkInfo()` | 302 | Validate link token, receives agent metadata |
| `requestCode()` | 332 | Request verification email |
| `verifyCode()` | 355 | Confirm 6-digit code, calls `fetchIntro()` |
| `loadHistory()` | 402 | Load chat history from server (PUB-005) |
| `fetchIntro()` | 441 | Fetch agent introduction (PUB-003) |
| `confirmNewConversation()` | 476 | Clear session and restart (PUB-005) |
| `updateLoadingText()` | 525 | THINK-001: Update loading text with anti-flicker timing |
| `resetHeartbeat()` | 546 | THINK-001: Reset heartbeat timer (fallback to "Working...") |
| `closeSSE()` | 556 | THINK-001: Close SSE connection and cleanup timers |
| `subscribeToStream()` | 568 | THINK-001: Subscribe to public execution SSE stream |
| `pollExecution()` | 635 | THINK-001: Poll public execution status until complete |
| `sendMessage()` | 661 | Send chat message (async mode with SSE streaming) |

### Shared Chat Components (CHAT-001 Refactor)

PublicChat.vue now uses shared components from `src/frontend/src/components/chat/`:

| Component | File | Description |
|-----------|------|-------------|
| `ChatMessages` | `ChatMessages.vue` (87 lines) | Message list with bottom-aligned layout, auto-scroll |
| `ChatInput` | `ChatInput.vue` (83 lines) | Auto-resize textarea with send button |
| `ChatBubble` | `ChatBubble.vue` (56 lines) | Message bubble with markdown rendering (assistant) |
| `ChatLoadingIndicator` | `ChatLoadingIndicator.vue` (36 lines) | Bouncing dots with dynamic status text (THINK-001) |

### Shared Utilities (THINK-001)

PublicChat.vue uses execution status utilities from `src/frontend/src/utils/execution-status.js`:

| Export | Description |
|--------|-------------|
| `getStatusFromStreamEvent(data)` | Maps Claude Code stream-json events to labels (Read → "Reading file...", Bash → "Running command...", etc.) |
| `MIN_LABEL_DISPLAY_MS` (500ms) | Minimum display time per label to prevent flicker |
| `HEARTBEAT_TIMEOUT_MS` (10000ms) | Timeout before falling back to "Working..." |

**Imports** (lines 258-259):
```javascript
import { ChatMessages, ChatInput } from '../components/chat'
import { getStatusFromStreamEvent, MIN_LABEL_DISPLAY_MS, HEARTBEAT_TIMEOUT_MS } from '../utils/execution-status'
```

**Usage** (lines 200-248):
- `ChatMessages` renders messages with custom empty slot for intro/welcome states
- `ChatInput` handles message input with v-model and submit event

### Router Configuration

```javascript
// src/frontend/src/router/index.js:18-22
{
  path: '/chat/:token',
  name: 'PublicChat',
  component: () => import('../views/PublicChat.vue'),
  meta: { requiresAuth: false }
}
```

## Backend Layer

### Owner Endpoints (Authenticated)

| Endpoint | File:Line | Handler |
|----------|-----------|---------|
| `POST /api/agents/{name}/public-links` | `public_links.py:64` | `create_public_link()` |
| `GET /api/agents/{name}/public-links` | `public_links.py:100` | `list_public_links()` |
| `GET /api/agents/{name}/public-links/{id}` | `public_links.py:115` | `get_public_link()` |
| `PUT /api/agents/{name}/public-links/{id}` | `public_links.py:134` | `update_public_link()` |
| `DELETE /api/agents/{name}/public-links/{id}` | `public_links.py:175` | `delete_public_link()` |

### Public Endpoints (Unauthenticated)

| Endpoint | File:Line | Handler |
|----------|-----------|---------|
| `GET /api/public/link/{token}` | `public.py:43` | `get_public_link_info()` |
| `POST /api/public/verify/request` | `public.py:73` | `request_verification_code()` |
| `POST /api/public/verify/confirm` | `public.py:130` | `confirm_verification_code()` |
| `POST /api/public/chat/{token}` | `public.py:215` | `public_chat()` (supports `async_mode` for THINK-001) |
| `GET /api/public/intro/{token}` | `public.py:374` | `get_agent_intro()` |
| `GET /api/public/executions/{token}/{execution_id}/stream` | `public.py:676` | `public_stream_execution()` (THINK-001 SSE proxy) |
| `GET /api/public/executions/{token}/{execution_id}/status` | `public.py:735` | `public_execution_status()` (THINK-001 polling) |
| `GET /api/public/sessions/{token}` | `public.py` | `list_public_sessions()` — JWT required; returns caller's last 20 sessions for this agent link with `preview` field (#587) |
| `GET /api/public/sessions/{token}/{session_id}` | `public.py` | `get_public_session()` — JWT required; returns session detail with messages; validates session belongs to caller and correct agent (#587) |

### Database Operations

| Method | File:Line | Description |
|--------|-----------|-------------|
| `create_public_link()` | `db/public_links.py:28` | Create new link with token |
| `get_public_link()` | `db/public_links.py:52` | Get link by ID |
| `get_public_link_by_token()` | `db/public_links.py:69` | Get link by URL token |
| `list_agent_public_links()` | `db/public_links.py:86` | List all links for agent |
| `update_public_link()` | `db/public_links.py:101` | Update link properties |
| `delete_public_link()` | `db/public_links.py:142` | Delete link and related data |
| `delete_agent_public_links()` | `db/public_links.py:156` | Cascade delete for agent |
| `is_link_valid()` | `db/public_links.py:176` | Validate token (enabled, not expired) |
| `create_verification()` | `db/public_links.py:200` | Create 6-digit verification code |
| `verify_code()` | `db/public_links.py:235` | Verify code, create session |
| `validate_session()` | `db/public_links.py:281` | Validate session token |
| `count_recent_verification_requests()` | `db/public_links.py:305` | Verification rate limit |
| `record_usage()` | `db/public_links.py:324` | Record chat message usage |
| `get_link_usage_stats()` | `db/public_links.py:363` | Get usage statistics |
| `count_recent_messages_by_ip()` | `db/public_links.py:407` | Rate limit check |

### Pydantic Models

| Model | File:Line | Description |
|-------|-----------|-------------|
| `PublicLinkCreate` | `db_models.py:301` | Create link request |
| `PublicLinkUpdate` | `db_models.py:308` | Update link request |
| `PublicLink` | `db_models.py:316` | Base link model |
| `PublicLinkWithUrl` | `db_models.py:329` | Link with generated URL |
| `PublicLinkInfo` | `db_models.py:336` | Public-facing link info (includes agent metadata) |
| `VerificationRequest` | `db_models.py:343` | Request verification code |
| `VerificationConfirm` | `db_models.py:349` | Confirm verification code |
| `VerificationResponse` | `db_models.py:356` | Verification result |
| `PublicChatRequest` | `db_models.py:364` | Chat message request (includes `async_mode` for THINK-001) |
| `PublicChatResponse` | `db_models.py:370` | Chat message response |

> **Note (2026-04-13)**: `PublicLinkCreate`, `PublicLinkUpdate`, `PublicLink`, and `PublicLinkWithUrl` no longer carry a `require_email` field. The owner UI sets email-required-ness via the agent-level Channel Access Policy (see [unified-channel-access-control.md](unified-channel-access-control.md)), not per-link. `PublicLinkInfo.require_email` remains but is populated from `agent_ownership.require_email`.

> **Note (SITE-001, 2026-05-03)**: `PublicLinkCreate` now accepts an optional `link_type: str = "chat"` field (`"chat"` or `"site"`). `PublicLink` and `PublicLinkWithUrl` carry `link_type: str`. `_build_public_url()` and `_build_external_url()` accept `link_type` and emit `/site/{token}/` (with trailing slash) for site links instead of `/chat/{token}`.

## Database Schema

### agent_public_links

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | Unique link ID |
| agent_name | TEXT FK | Target agent |
| token | TEXT UNIQUE | URL-safe token for link |
| created_by | TEXT FK | Owner user ID |
| created_at | TEXT | ISO timestamp |
| expires_at | TEXT | Optional expiration |
| enabled | INTEGER | 1=active, 0=disabled |
| name | TEXT | Optional friendly name |
| require_email | INTEGER | **DEPRECATED (2026-04-13)** — retained for the legacy `slack_link_connections` join in `db/slack.py`; no longer read by public web chat. Source of truth is `agent_ownership.require_email`. Migration `public_link_require_email_unified` ORed any legacy `1` values into the agent-level flag. |
| type | TEXT | `'chat'` (default) or `'site'` (SITE-001). Added by migration `public_links_type`. |

### public_link_verifications

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | Verification ID |
| link_id | TEXT FK | Parent link |
| email | TEXT | User's email |
| code | TEXT | 6-digit code |
| created_at | TEXT | Request time |
| expires_at | TEXT | Code expiration |
| verified | INTEGER | 0=pending, 1=verified, -1=invalidated |
| session_token | TEXT | Session after verification |
| session_expires_at | TEXT | Session expiration |

### public_link_usage

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | Usage record ID |
| link_id | TEXT FK | Parent link |
| email | TEXT | Verified email (if any) |
| ip_address | TEXT | Client IP |
| message_count | INTEGER | Messages sent |
| created_at | TEXT | First message time |
| last_used_at | TEXT | Most recent message |

## Side Effects

### WebSocket Broadcasts

```javascript
// Link created (note: require_email removed from payload as of 2026-04-13 — agent-level policy is the source of truth)
{ "event": "public_link_created", "data": { "agent_name": "...", "link_id": "..." } }

// Link updated
{ "event": "public_link_updated", "data": { "agent_name": "...", "link_id": "...", "enabled": true/false } }

// Link deleted
{ "event": "public_link_deleted", "data": { "agent_name": "...", "link_id": "..." } }
```

### Execution Tracking (EXEC-024)

Each public chat message now creates a full execution record via `TaskExecutionService`:

| Side Effect | Description |
|-------------|-------------|
| **Execution record** | `schedule_executions` row with `triggered_by="public"`, `source_user_email` set to verified email or `"anonymous ({ip})"` |
| **Activity stream** | `CHAT_START` / `completed` activity pair visible in Dashboard timeline |
| **Slot management** | Execution occupies a capacity slot; 429 returned if all slots busy |
| **Credential sanitization** | Response text and execution logs are sanitized via `sanitize_response()` / `sanitize_execution_log()` |
| **Tasks tab visibility** | Public executions appear alongside manual/scheduled executions in the Tasks tab |

### Audit Logging

| Event Type | Action | Trigger |
|------------|--------|---------|
| `public_link` | `create` | Owner creates link |
| `public_link` | `update` | Owner modifies link |
| `public_link` | `delete` | Owner deletes link |
| `public_verification` | `request_code` | User requests email code |
| `public_verification` | `confirm_code` | User confirms code |
| `public_chat` | `message` | User sends chat message |

## Security Considerations

1. **Rate Limiting**: 30 messages/minute per IP prevents abuse
2. **Email Verification**: Optional but recommended for tracking
3. **Session Tokens**: 24-hour validity, tied to link+email
4. **Verification Codes**: 10-minute expiry, 3 requests/10 min per email
5. **Audit Logging**: All public access logged
6. **No Sensitive Data**: Public endpoints don't expose link IDs or agent details
7. **Owner-Only Management**: Only agent owners with `can_share` can create/manage links

## Configuration

### Environment Variables

```bash
# Email Provider
EMAIL_PROVIDER=console  # console, smtp, sendgrid

# SMTP Settings
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=user@example.com
SMTP_PASSWORD=secret
SMTP_FROM=noreply@example.com

# SendGrid
SENDGRID_API_KEY=SG.xxx

# Frontend URL (for internal link generation)
# Default: http://localhost (port 80, Docker deployment)
# For Vite dev server: http://localhost:5173
FRONTEND_URL=http://localhost

# External URL for public chat links (PUB-002)
# Set when you want to share links with users outside VPN
# Examples: Tailscale Funnel, GCP Load Balancer, Cloudflare Tunnel
# When set, enables "Copy External Link" button in PublicLinksPanel
PUBLIC_CHAT_URL=
```

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Link not found | 200* | `{valid: false, reason: "not_found"}` |
| Link disabled | 200* | `{valid: false, reason: "disabled"}` |
| Link expired | 200* | `{valid: false, reason: "expired"}` |
| Agent not found | 404 | Agent not found |
| Permission denied | 403 | Only the agent owner can... |
| Session required | 401 | Session token required for this link |
| Session expired | 401 | Invalid or expired session |
| Access not granted (pending approval) | 403 | Creates/updates a pending `access_requests` row for `(agent, email, "web")`; owner must approve before chat is allowed. Enforced by the unified gate in `routers/public.py` (2026-04-13). |
| Rate limited | 429 | Too many requests |
| Agent at capacity | 429 | Agent is busy (all parallel task slots occupied) |
| Agent unavailable | 503 | Agent is not available |
| Agent timeout | 504 | Request timed out |
| Agent error | 502 | Failed to process your request |

*Note: Public link info endpoint returns 200 with valid=false for invalid links (not 404)

## Files

### Backend

| File | Description |
|------|-------------|
| `src/backend/db/public_links.py` | Database operations class (includes MEM-001 memory methods + #895 split-storage helpers `_parse_memory_blob`/`_encode_memory_blob` and `update_user_memory_agent_notes`/`update_user_memory_conversation_summary`) |
| `src/backend/db/schema.py` | Table definitions including `public_user_memory` and lookup index — DDL unchanged by #895; `memory_text` now holds JSON |
| `src/backend/db/migrations.py` | Migration #28 `_migrate_public_user_memory_table`; `_migrate_public_link_require_email_unified` (2026-04-13) — ORs legacy per-link `require_email=1` into `agent_ownership.require_email=1` |
| `src/backend/services/platform_prompt_service.py` | `format_user_memory_block(dict)` helper + `summarize_user_memory_background()` (extracted from `routers/public.py` in #895; called by both web and channel paths) |
| `src/backend/routers/public_links.py` | Owner CRUD endpoints; `_build_public_url()` / `_build_external_url()` now accept `link_type` and emit `/site/{token}/` for site links |
| `src/backend/routers/public_links.py:30-39` | URL builders: `_build_public_url(token, link_type)`, `_build_external_url(token, link_type)` |
| `src/backend/routers/public_links.py:42-61` | `_link_to_response()` - converts DB dict to API model |
| `src/backend/routers/public.py` | Public endpoints (764 lines, includes THINK-001 async mode, SSE proxy, status polling) |
| `src/backend/routers/site.py` | **SITE-001**: reverse-proxy router for `type='site'` links (see section below) |
| `src/backend/services/task_execution_service.py` | Unified task execution lifecycle (EXEC-024) |
| `src/backend/services/email_service.py` | Email sending service |
| `src/backend/db_models.py:301-374` | Pydantic models |
| `src/backend/db_models.py:329-333` | `PublicLinkWithUrl` with `external_url` field |
| `src/backend/database.py` | Schema, imports, delegation |
| `src/backend/config.py:43-45` | `PUBLIC_CHAT_URL` environment variable |
| `src/backend/config.py:86-88` | Auto-add `PUBLIC_CHAT_URL` to CORS origins |
| `src/backend/main.py:44-45,112,292-293` | Router registration |

### Frontend

| File | Description |
|------|-------------|
| `src/frontend/src/views/PublicChat.vue` | Public chat page (786 lines, THINK-001 SSE streaming + shared components) |
| `src/frontend/src/components/chat/index.js` | Shared component exports (ChatMessages, ChatInput, ChatBubble, ChatLoadingIndicator) |
| `src/frontend/src/utils/execution-status.js` | Shared status mapping utilities (THINK-001) |
| `src/frontend/src/components/PublicLinksPanel.vue` | Owner panel — now includes Chat/Website type selector in create modal, "Website" badge on site links |
| `src/frontend/src/components/SharingPanel.vue` | Embeds PublicLinksPanel (lines 82-83, 92) |
| `src/frontend/src/router/index.js:18-22` | Route definition |
| `src/frontend/nginx.conf` | **SITE-001**: `location /site/` block added to proxy site link requests to backend |

### Tests

| File | Description |
|------|-------------|
| `tests/test_public_links.py` | Comprehensive test suite |

## Testing

### Prerequisites

- Backend running on `localhost:8000`
- Frontend running on `localhost:3000`
- At least one running agent with the updated base image (must have `/api/task` endpoint)

**Note**: Agents created before the Parallel Headless Execution feature (Phase 12.1) need to be recreated with the updated base image for public chat to work. The `/api/task` endpoint is required for stateless parallel execution.

### Test File

`tests/test_public_links.py` - Comprehensive test suite

### Test Classes

| Class | Description |
|-------|-------------|
| `TestPublicLinkDatabase` | Verify database tables exist |
| `TestPublicEndpoints` | Test unauthenticated endpoints |
| `TestOwnerEndpointsNoAuth` | Verify auth is required |
| `TestOwnerEndpointsWithAuth` | Test authenticated CRUD |
| `TestPublicLinkLifecycle` | Full create/update/delete cycle |
| `TestEmailVerification` | Email verification flow |
| `TestPublicChat` | Public chat functionality |

### Test Results (2025-12-22)

| Test | Status | Notes |
|------|--------|-------|
| Database tables exist | Pass | 3 tables created |
| Public endpoint - invalid link | Pass | Returns `{valid: false, reason: "not_found"}` |
| Public endpoint - verification request (invalid) | Pass | Returns 404 |
| Public endpoint - verification confirm (invalid) | Pass | Returns 404 |
| Public endpoint - chat (invalid link) | Pass | Returns 404 |
| Owner endpoints require auth | Pass | Returns 401 without token |
| Create public link | Pass | Returns link with ID, token, URL |
| List owner links | Pass | Returns array with usage stats |
| Update link | Pass | Name and enabled state updated |
| Disable link - public sees disabled | Pass | Returns `{valid: false, reason: "disabled"}` |
| Re-enable link | Pass | Link becomes valid again |
| Delete link | Pass | Link removed from database |
| Public chat (no email required) | Skip | Requires agent with updated base image |
| Email verification flow | Pass | Codes sent (console mode) |

### How to Run Tests

```bash
# API tests via pytest
pytest tests/test_public_links.py -v

# Inside docker container
docker-compose exec backend python -m pytest tests/test_public_links.py -v
```

## Related Flows

- **Upstream**: Agent Lifecycle (agent must exist and be running), Agent Sharing (now hosts PublicLinksPanel in same tab)
- **Downstream**: Agent Chat (shares `TaskExecutionService.execute_task()` unified execution path)
- **Related**: [Authenticated Chat Tab](authenticated-chat-tab.md) - shares chat components (ChatMessages, ChatInput, ChatBubble, ChatLoadingIndicator)
- **Related**: [Dynamic Thinking Status](dynamic-thinking-status.md) (THINK-001) - shares `execution-status.js` utilities and SSE streaming pattern
- **Related**: Agent Sharing (manages `can_share` permission, embeds PublicLinksPanel via SharingPanel.vue)
- **Related**: [Slack Integration](slack-integration.md) (SLACK-001) - Slack as delivery channel for public links

## External Public URL (PUB-002)

**Status**: Implemented (2026-02-16)
**Spec**: `docs/requirements/EXTERNAL_PUBLIC_URL.md`

Support for an external (internet-accessible) URL for public links, enabling sharing with users outside the Tailscale VPN.

### Data Flow

```
+-----------------------------+     +----------------------------------+
|  PublicLinksPanel.vue       |     |  Backend API                     |
+-----------------------------+     +----------------------------------+
|                             |     |                                  |
|  1. User clicks copy button |     |  GET /api/agents/{name}/public-links
|     - Internal (clipboard)  | <-- |  Returns: {url, external_url, ...}
|     - External (globe icon) |     |                                  |
|                             |     +----------------------------------+
|  2. copyLink(link, external)|                    |
|     - external=false: link.url                   |
|     - external=true: link.external_url           v
|                             |     +----------------------------------+
|  3. Notification:           |     |  config.py                       |
|     - "Internal link copied!"|     +----------------------------------+
|     - "External link copied!"|     |  PUBLIC_CHAT_URL env var        |
+-----------------------------+     |  (loaded at startup)             |
                                    +----------------------------------+
                                                   |
                                                   v
                                    +----------------------------------+
                                    |  public_links.py                 |
                                    +----------------------------------+
                                    |  _build_external_url(token)      |
                                    |    if PUBLIC_CHAT_URL:           |
                                    |      return f"{URL}/chat/{token}"|
                                    |    return None                   |
                                    +----------------------------------+
```

### Configuration

Set `PUBLIC_CHAT_URL` environment variable to your external URL:

```bash
# .env or docker-compose.yml
PUBLIC_CHAT_URL=https://public.example.com
```

When set:
1. The API returns `external_url` field in all public link responses
2. The UI shows a globe icon button to copy the external link
3. CORS is automatically configured to allow the external origin

### Backend Implementation

**Config (`src/backend/config.py:43-45`)**:
```python
# External URL for public chat links (Tailscale Funnel, Cloudflare Tunnel, etc.)
# When set, enables "Copy External Link" button in PublicLinksPanel
PUBLIC_CHAT_URL = os.getenv("PUBLIC_CHAT_URL", "")
```

**CORS Auto-Configuration (`src/backend/config.py:86-88`)**:
```python
# Automatically add PUBLIC_CHAT_URL to CORS if set
if PUBLIC_CHAT_URL:
    _extra_origins.append(PUBLIC_CHAT_URL)
```

**URL Builder (`src/backend/routers/public_links.py:35-39`)**:
```python
def _build_external_url(token: str) -> str | None:
    """Build the external public URL for a link token (if configured)."""
    if PUBLIC_CHAT_URL:
        return f"{PUBLIC_CHAT_URL}/chat/{token}"
    return None
```

**Response Assembly (`src/backend/routers/public_links.py:42-61`)**:
```python
def _link_to_response(link: dict, include_usage: bool = True) -> PublicLinkWithUrl:
    # ...
    return PublicLinkWithUrl(
        # ... other fields ...
        url=_build_public_url(link["token"]),        # Line 58: Internal URL
        external_url=_build_external_url(link["token"]),  # Line 59: External URL
        usage_stats=usage_stats
    )
```

**Model (`src/backend/db_models.py:329-333`)**:
```python
class PublicLinkWithUrl(PublicLink):
    """Public link with generated URL."""
    url: str  # Internal URL (VPN/tailnet)
    external_url: Optional[str] = None  # External URL (public internet via Funnel/Tunnel)
    usage_stats: Optional[dict] = None
```

### Frontend Implementation

**URL Preview (`src/frontend/src/components/PublicLinksPanel.vue:79-102`)**:
- Line 81: Displays `external_url` if available, otherwise `url`
- Lines 83-90: Internal copy button (clipboard icon)
- Lines 92-101: External copy button (globe icon, conditionally rendered)

```vue
<!-- URL preview - shows external URL if available -->
<code class="...">{{ link.external_url || link.url }}</code>

<!-- Internal link copy -->
<button @click="copyLink(link, false)" title="Copy internal link">
  <!-- clipboard icon -->
</button>

<!-- External link copy (only if external_url exists) -->
<button v-if="link.external_url" @click="copyLink(link, true)"
        title="Copy external link (public internet)">
  <!-- globe icon -->
</button>
```

**Copy Method (`src/frontend/src/components/PublicLinksPanel.vue:461-473`)**:
```javascript
const copyLink = async (link, external = false) => {
  try {
    const url = external && link.external_url ? link.external_url : link.url
    await navigator.clipboard.writeText(url)
    copyNotification.value = external ? 'external' : 'internal'
    setTimeout(() => {
      copyNotification.value = false
    }, 2000)
  } catch (err) {
    console.error('Failed to copy link:', err)
  }
}
```

**Notification (`src/frontend/src/components/PublicLinksPanel.vue:311-317`)**:
```vue
<div v-if="copyNotification" class="...">
  {{ copyNotification === 'external' ? 'External link copied!' : 'Internal link copied!' }}
</div>
```

### UI Behavior

| Scenario | Copy Icon | Globe Icon | URL Displayed |
|----------|-----------|------------|---------------|
| `PUBLIC_CHAT_URL` not set | Visible | Hidden | Internal URL (`FRONTEND_URL/chat/{token}`) |
| `PUBLIC_CHAT_URL` set | Visible | Visible | External URL (`PUBLIC_CHAT_URL/chat/{token}`) |

### Example API Response

```json
{
  "id": "link_abc123",
  "agent_name": "my-agent",
  "token": "xyz789...",
  "url": "http://localhost/chat/xyz789...",
  "external_url": "https://public.example.com/chat/xyz789...",
  "enabled": true,
  "usage_stats": { "total_messages": 42, "unique_users": 5 }
}
```

> **Note (2026-04-13)**: `PublicLinkWithUrl` no longer carries `require_email`. To see whether email verification is required for an agent, call `GET /api/public/link/{token}` (which returns agent-level policy) or use the owner-facing access policy endpoints described in [unified-channel-access-control.md](unified-channel-access-control.md).

### Infrastructure Requirements

The external URL requires separate infrastructure setup. Options:
- **Tailscale Funnel**: `tailscale funnel 80` (simplest)
- **GCP Load Balancer**: External IP with SSL termination
- **Cloudflare Tunnel**: `cloudflared tunnel`
- **ngrok**: `ngrok http 80`

See `docs/requirements/PUBLIC_EXTERNAL_ACCESS_SETUP.md` for setup guides.

---

## Agent Introduction (PUB-003)

**Status**: Implemented (2026-02-17)

When users access a public chat link, the agent automatically introduces itself before they start chatting.

### Data Flow

```
User opens /chat/{token}
        ↓
loadLinkInfo() - validate link
        ↓
Email verification (if required)
        ↓
fetchIntro() called
        ↓
GET /api/public/intro/{token}?session_token=...
        ↓
Backend sends INTRO_PROMPT to agent via /api/task
        ↓
Agent responds with 2-paragraph introduction
        ↓
Response displayed as first assistant message
```

### Backend Implementation

**Intro Prompt** (`public.py:366-372`):
```python
INTRO_PROMPT = """Provide a brief 2-paragraph introduction of yourself.

First paragraph: Who you are and what you do.
Second paragraph: Your purpose and how you can help the user.

Be concise, welcoming, and conversational. Do not use headers, bullet points, or markdown formatting."""
```

**Endpoint** (`public.py:374-455`):
- Validates link token and session (if email required)
- Sends intro prompt to agent via direct `httpx` call to `/api/task` (not routed through TaskExecutionService -- intros are lightweight and do not need execution tracking)
- Returns `{"intro": "..."}`
- 120-second timeout for generation

### Frontend Implementation

**State** (`PublicChat.vue:270-273`):
```javascript
const introLoading = ref(false)
const introError = ref(null)
const introFetched = ref(false)
```

**fetchIntro()** (`PublicChat.vue:376-408`):
- Builds URL with session token if needed
- Adds response as first assistant message
- Sets `introFetched = true` on success or error

**Trigger Points**:
1. After email verification succeeds (`verifyCode()`)
2. On mount if no email needed or already verified (`onMounted()`)

**UI States**:
- Loading: "Getting ready..." with spinner
- Success: Intro message displayed in chat
- Error: Falls back to generic "Start a Conversation"

### User Experience

| Before | After |
|--------|-------|
| Generic "Start a Conversation" | Personalized agent introduction |
| User must ask what agent does | Agent proactively explains purpose |
| Cold start | Warm, welcoming interaction |

---

## Public Chat Header Metadata (PUB-004)

**Status**: Implemented (2026-02-17)

The public chat page header now displays agent metadata to provide users with context about who they are chatting with.

### Data Flow

```
User opens /chat/{token}
        |
        v
GET /api/public/link/{token}
        |
        v
Backend validates link token
        |
        v
Backend fetches agent metadata:
  1. AgentsDB.get_autonomy_enabled(agent_name) -> is_autonomous
  2. AgentsDB.get_read_only_mode(agent_name) -> is_read_only
  3. GET http://agent-{name}:8000/api/template/info -> display_name, description
        |
        v
Returns PublicLinkInfo with metadata fields
        |
        v
PublicChat.vue header displays:
  - Agent display name (or fallback to agent name)
  - Agent description (if available)
  - AUTO badge (amber) if is_autonomous
  - READ-ONLY badge (rose with lock icon) if is_read_only
```

### Backend Implementation

**Endpoint** (`public.py:44-103`):
```python
@router.get("/link/{token}", response_model=PublicLinkInfo)
async def get_public_link_info(token: str, request: Request):
    # ... validation ...

    # Get agent metadata from database
    agents_db = AgentsDB()
    is_autonomous = agents_db.get_autonomy_enabled(agent_name)
    read_only_data = agents_db.get_read_only_mode(agent_name)
    is_read_only = read_only_data.get("enabled", False)

    # Get display name and description from template.yaml (if agent running)
    if agent_available:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"http://agent-{agent_name}:8000/api/template/info")
            if response.status_code == 200:
                info = response.json()
                agent_display_name = info.get("name") or info.get("display_name") or agent_name
                agent_description = info.get("description")

    return PublicLinkInfo(
        valid=True,
        # ... other fields ...
        agent_display_name=agent_display_name,
        agent_description=agent_description,
        is_autonomous=is_autonomous,
        is_read_only=is_read_only
    )
```

**Model** (`db_models.py:336-346`):
```python
class PublicLinkInfo(BaseModel):
    """Public-facing link information (no sensitive data)."""
    valid: bool
    require_email: bool = False  # 2026-04-13: now sourced from agent_ownership.require_email, not the link
    agent_available: bool = True
    reason: Optional[str] = None
    # Agent metadata (only populated when valid)
    agent_display_name: Optional[str] = None
    agent_description: Optional[str] = None
    is_autonomous: bool = False
    is_read_only: bool = False
```

### Frontend Implementation

**Header Template** (`PublicChat.vue:4-46`):
```vue
<header class="bg-white dark:bg-gray-800 shadow-sm py-3 px-4">
  <div class="max-w-3xl mx-auto">
    <div v-if="linkInfo && linkInfo.valid" class="space-y-1">
      <!-- Top row: Status dot, name, badges -->
      <div class="flex items-center justify-between">
        <div class="flex items-center space-x-2">
          <div class="w-2 h-2 rounded-full bg-green-500 animate-pulse"></div>
          <span class="font-semibold text-gray-900 dark:text-white">
            {{ linkInfo.agent_display_name || 'Agent' }}
          </span>
        </div>
        <!-- Status badges -->
        <div class="flex items-center space-x-2">
          <span v-if="linkInfo.is_autonomous"
            class="... bg-amber-100 text-amber-800 ...">
            AUTO
          </span>
          <span v-if="linkInfo.is_read_only"
            class="... bg-rose-100 text-rose-800 ...">
            <svg><!-- lock icon --></svg>
            READ-ONLY
          </span>
        </div>
      </div>
      <!-- Description row -->
      <p v-if="linkInfo.agent_description"
        class="text-sm text-gray-500 dark:text-gray-400 line-clamp-2">
        {{ linkInfo.agent_description }}
      </p>
    </div>
  </div>
</header>
```

### API Response Example

```json
{
  "valid": true,
  "require_email": false,
  "agent_available": true,
  "reason": null,
  "agent_display_name": "Sales Assistant",
  "agent_description": "I help answer questions about our products and services.",
  "is_autonomous": true,
  "is_read_only": false
}
```

### UI Behavior

| Field | Display |
|-------|---------|
| `agent_display_name` | Header title (fallback: "Agent") |
| `agent_description` | Subtitle below name (truncated to 2 lines) |
| `is_autonomous` | Amber "AUTO" badge |
| `is_read_only` | Rose "READ-ONLY" badge with lock icon |

### Status Badges

| Badge | Color | Icon | Meaning |
|-------|-------|------|---------|
| AUTO | Amber (`bg-amber-100 text-amber-800`) | None | Agent runs autonomous scheduled executions |
| READ-ONLY | Rose (`bg-rose-100 text-rose-800`) | Lock | Agent cannot modify filesystem outside `.trinity` |

### Metadata Sources

| Field | Primary Source | Fallback |
|-------|---------------|----------|
| `agent_display_name` | `template.yaml` via `/api/template/info` | Container label `trinity.agent-type`, then agent name |
| `agent_description` | `template.yaml` via `/api/template/info` | None |
| `is_autonomous` | `agents` table `autonomy_enabled` column | `false` |
| `is_read_only` | `agents` table `read_only_enabled` column | `false` |

---

## Public Chat Session Persistence (PUB-005)

**Status**: Implemented (2026-02-17)
**Spec**: `docs/requirements/PUBLIC_CHAT_SESSIONS.md`

Multi-turn conversation persistence for public chat, enabling context across page refreshes and return visits.

### Data Flow

```
User sends message via /chat/{token}
        |
        v
POST /api/public/chat/{token}
        |
        v
Determine session identifier:
  - Email links: verified email from session_token
  - Anonymous links: session_id from request or generate new
        |
        v
db.get_or_create_public_chat_session(link_id, identifier, type)
        |
        v
db.build_public_chat_context(session_id, message, max_turns=10)
  -> "Previous conversation:\nUser: ...\nAssistant: ...\n\nCurrent message:\nUser: ..."
  NOTE: context is built BEFORE the user message is stored (#539 fix).
        |
        v
db.add_public_chat_message(session_id, "user", message)
        |
        v
TaskExecutionService.execute_task(triggered_by="public")
  -> execution record, slot, activity, agent POST, sanitize, release
        |
        v
db.add_public_chat_message(session_id, "assistant", response, cost=result.cost)
        |
        v
Return {response, session_id (for anonymous), message_count}
```

### Database Schema

#### public_chat_sessions

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | Session UUID (`secrets.token_urlsafe(16)`) |
| link_id | TEXT FK | Parent public link |
| session_identifier | TEXT | Email (lowercase) or anonymous token |
| identifier_type | TEXT | 'email' or 'anonymous' |
| created_at | TEXT | ISO timestamp |
| last_message_at | TEXT | ISO timestamp |
| message_count | INTEGER | Total messages in session |
| total_cost | REAL | Accumulated cost |

**Unique Constraint**: `(link_id, session_identifier)`
**Schema Location**: `database.py:662-674`

#### public_chat_messages

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | Message UUID (`secrets.token_urlsafe(16)`) |
| session_id | TEXT FK | Parent session (CASCADE DELETE) |
| role | TEXT | 'user' or 'assistant' |
| content | TEXT | Message text |
| timestamp | TEXT | ISO timestamp |
| cost | REAL | Cost (assistant only, calculated from usage) |

**Schema Location**: `database.py:678-686`

**Indexes** (`database.py:781-783`):
- `idx_public_chat_sessions_link` on `link_id`
- `idx_public_chat_sessions_identifier` on `session_identifier`
- `idx_public_chat_messages_session` on `session_id`

### API Endpoints

| Endpoint | Method | File:Line | Description |
|----------|--------|-----------|-------------|
| `/api/public/chat/{token}` | POST | `public.py:214` | Chat with persistence (updated) |
| `/api/public/history/{token}` | GET | `public.py:465` | Get chat history |
| `/api/public/session/{token}` | DELETE | `public.py:541` | Clear session (New Conversation) |

### Request/Response Models

**PublicChatRequest** (`db_models.py:370-376`):
```python
class PublicChatRequest(BaseModel):
    message: str
    session_token: Optional[str] = None  # Email links
    session_id: Optional[str] = None     # Anonymous links (from localStorage)
    async_mode: bool = False             # THINK-001: return execution_id for SSE streaming
```

**PublicChatResponse** (`db_models.py:377-382`):
```python
class PublicChatResponse(BaseModel):
    response: str
    session_id: Optional[str] = None      # Returned for anonymous links
    message_count: Optional[int] = None   # Total messages in session
    usage: Optional[dict] = None          # {input_tokens, output_tokens}
```

**PublicChatHistoryResponse** (`public.py:28-32`):
```python
class PublicChatHistoryResponse(BaseModel):
    messages: List[dict]  # [{role, content, timestamp}]
    session_id: str
    message_count: int
```

**ClearSessionResponse** (`public.py:35-38`):
```python
class ClearSessionResponse(BaseModel):
    cleared: bool
    new_session_id: Optional[str] = None  # For anonymous links
```

**PublicChatSession** (`db_models.py:389-398`):
```python
class PublicChatSession(BaseModel):
    id: str
    link_id: str
    session_identifier: str
    identifier_type: str  # 'email' or 'anonymous'
    created_at: datetime
    last_message_at: datetime
    message_count: int = 0
    total_cost: float = 0.0
```

**PublicChatMessage** (`db_models.py:401-408`):
```python
class PublicChatMessage(BaseModel):
    id: str
    session_id: str
    role: str  # 'user' or 'assistant'
    content: str
    timestamp: datetime
    cost: Optional[float] = None
```

### Backend Implementation

**Database Operations** (`db/public_chat.py` - 303 lines):

| Method | Line | Description |
|--------|------|-------------|
| `get_or_create_session()` | 47 | Get existing or create new session |
| `get_session_by_identifier()` | 99 | Lookup by link_id + identifier |
| `get_session()` | 115 | Get session by ID |
| `add_message()` | 123 | Add message and update session stats |
| `get_session_messages()` | 172 | Get messages oldest-first (all) |
| `get_recent_messages()` | 198 | Get most recent N messages oldest-first |
| `clear_session()` | 221 | Delete session and messages |
| `build_context_prompt()` | 242 | Build prompt with history |
| `delete_link_sessions()` | 278 | Cascade delete on link deletion |

**Delegation Methods** (`database.py:1404-1432`):
- `get_or_create_public_chat_session()`
- `get_public_chat_session_by_identifier()`
- `get_public_chat_session()`
- `add_public_chat_message()`
- `get_public_chat_messages()`
- `get_recent_public_chat_messages()`
- `clear_public_chat_session()`
- `build_public_chat_context()`
- `delete_public_link_sessions()`

**Chat Endpoint** (`routers/public.py`):
1. Validate link token
2. Determine session identifier
   - Email links: validate session_token, extract email
   - Anonymous links: use provided session_id or generate new
3. Rate limit check
4. Check agent availability
5. Get or create session
6. **Build context-enriched prompt** (#539: must happen BEFORE storing user message to avoid duplication)
7. Store user message
8. Record usage
9. **Execute via `TaskExecutionService.execute_task(triggered_by="public")`**
   - Creates `schedule_executions` record
   - Acquires capacity slot (returns 429 if at capacity)
   - Tracks activity start (Dashboard timeline)
   - POSTs to agent `/api/task` with retry
   - Sanitizes credentials from response
   - Tracks activity completion
   - Releases slot
10. Check result status; raise 429/504/502 on failure (lines 324-341)
11. Store assistant response with cost from result (lines 346-351)
12. Get updated message count (lines 353-355)
13. Return with session_id for anonymous links (lines 357-362)

**History Endpoint** (`routers/public.py:465-538`):
1. Validate link token (line 480)
2. Determine session identifier based on link type (lines 484-509)
3. Look up session (lines 512-515)
4. Return empty array if no session (lines 517-522)
5. Get messages (oldest first, limit 100) (line 525)
6. Return formatted response (lines 527-538)

**Clear Session Endpoint** (`routers/public.py:541-602`):
1. Validate link token (line 556)
2. Determine session identifier (lines 560-583)
3. Look up and delete session (lines 586-592)
4. Generate new session_id for anonymous links (lines 594-597)
5. Return response (lines 599-602)

### Frontend Implementation

**State** (`PublicChat.vue:305-306`):
```javascript
const chatSessionId = ref(localStorage.getItem(`public_chat_session_id_${token.value}`) || '')
const historyLoading = ref(false)
```

**Methods**:

| Method | Line | Description |
|--------|------|-------------|
| `loadHistory()` | 429-465 | Fetch previous messages on mount |
| `sendMessage()` | 544-606 | Send message, store session_id |
| `confirmNewConversation()` | 503-541 | Clear session, update session_id |

**loadHistory()** (`PublicChat.vue:429-465`):
- Builds URL with session_token (email) or session_id (anonymous)
- Returns `true` if history loaded, `false` if no history
- Called before `fetchIntro()` on mount

**sendMessage()** (`PublicChat.vue:544-606`):
- Includes session_id in payload for anonymous links (line 573)
- Stores returned session_id in localStorage (lines 579-581)

**confirmNewConversation()** (`PublicChat.vue:503-541`):
- Shows confirmation dialog (line 504)
- Calls DELETE endpoint with credentials (lines 509-523)
- Clears local messages (line 526)
- Updates chatSessionId from response (lines 530-532)
- Fetches fresh intro (line 536)

**New Conversation Button** (`PublicChat.vue:17-27`):
- Visible when `messages.length > 0 && isVerified`
- Disabled during `chatLoading`
- Refresh icon with "New" label

**Initialization** (`onMounted`, `PublicChat.vue:624-644`):
```javascript
onMounted(async () => {
  await loadLinkInfo()
  if (linkInfo.value?.valid && linkInfo.value?.agent_available) {
    if (!needsEmail || hasSession) {
      const hasHistory = await loadHistory()
      if (!hasHistory) {
        await fetchIntro()
      } else {
        introFetched.value = true
      }
    }
  }
})
```

### Session Identifier Strategy

| Link Type | Identifier | Storage | Generation |
|-----------|------------|---------|------------|
| Email Required | `email.lower()` | Server (via session_token validation) | N/A |
| Anonymous | `secrets.token_urlsafe(16)` | localStorage `public_chat_session_id_{token}` | Backend generates, frontend stores |

### Context Injection

**Method**: `build_context_prompt()` (`db/public_chat.py:245-280`)

Last 10 exchanges (20 messages) formatted with public mode header (PUB-006):

```
### Trinity: Public Link Access Mode

Previous conversation:
User: [message 1]
Assistant: [response 1]
User: [message 2]
Assistant: [response 2]
...

Current message:
User: [new message]
```

If no history exists, still includes the public mode header:

```
### Trinity: Public Link Access Mode

Current message:
User: [new message]
```

The `PUBLIC_LINK_MODE_HEADER` constant is defined at `db/public_chat.py:17-18`. See [PUB-006 Public Client Mode Awareness](#public-client-mode-awareness-pub-006) for details.

### Cost Tracking

Assistant message cost is now sourced from `TaskExecutionResult.cost` (calculated by the agent server and returned in metadata). The `TaskExecutionService` persists cost in the `schedule_executions` record; the public chat endpoint passes it through to `add_public_chat_message()`:
```python
# public.py:346-351
db.add_public_chat_message(
    session_id=chat_session.id,
    role="assistant",
    content=assistant_response,
    cost=result.cost  # From TaskExecutionResult
)
```

### Files

| File | Lines | Changes |
|------|-------|---------|
| `src/backend/db/public_chat.py` | 307 | PublicChatOperations class, `PUBLIC_LINK_MODE_HEADER` constant (line 18), `build_context_prompt()` (245-280) |
| `src/backend/database.py` | 1437 | Tables (660-687), indexes (781-783), delegation (1404-1432) |
| `src/backend/db_models.py` | 483 | PublicChatSession (389-398), PublicChatMessage (401-408), updated Request/Response (370-382) |
| `src/backend/routers/public.py` | 764 | Updated chat (215-362, async mode at 320-348), THINK-001 background task (636-673), SSE proxy (676-732), status poll (735-764), history (457-530), session (533-594) |
| `src/backend/services/task_execution_service.py` | 391 | Unified execution lifecycle: slot mgmt, execution records, activity tracking, credential sanitization |
| `src/frontend/src/views/PublicChat.vue` | 786 | State (278-294), loadHistory (402-465), sendMessage (661-744, async mode), THINK-001 SSE (525-632), pollExecution (635-658), confirmNewConversation (476-522), New button (13-23), bottom-aligned layout (191-193) |

### Error Handling

| Error Case | HTTP Status | Handler |
|------------|-------------|---------|
| Invalid link token | 404 | `public.py:232-233` |
| Session token required (email link) | 401 | `public.py:242-246` |
| Invalid/expired session | 401 | `public.py:249-253` |
| Rate limited | 429 | `public.py:267-271` |
| Agent at capacity | 429 | `public.py:326-330` (via TaskExecutionService slot check) |
| Agent unavailable | 503 | `public.py:275-279` |
| Agent timeout | 504 | `public.py:331-335` |
| Agent error | 502 | `public.py:336-341` |
| Missing session_id for clear | 400 | `public.py:570-574` |

### Testing

Prerequisites:
- Running agent with `/api/task` endpoint
- Backend and frontend running

Test scenarios:
1. **Anonymous flow**: Open link, send messages, refresh page, verify history loads
2. **Email flow**: Verify email, send messages, refresh, verify history loads
3. **New Conversation**: Click button, confirm, verify history cleared, new session ID
4. **Context injection**: Send follow-up question, verify agent responds with conversation awareness
5. **Session isolation**: Different users on same link should have separate sessions
6. **Cost tracking**: Verify assistant messages have cost field populated

---

## Public Client Mode Awareness (PUB-006)

**Status**: Implemented (2026-02-17)

Agents now know when they are serving public users via public links, enabling them to adjust their behavior accordingly.

### Data Flow

```
User sends message via /chat/{token}
        |
        v
POST /api/public/chat/{token}
        |
        v
db.build_context_prompt(session_id, message)
        |
        v
Prepends PUBLIC_LINK_MODE_HEADER to prompt:
  "### Trinity: Public Link Access Mode\n\n..."
        |
        v
Agent receives context-aware prompt
        |
        v
Agent can detect public mode and adjust behavior
```

### Backend Implementation

**Constant** (`db/public_chat.py:17-18`):
```python
# Header injected into every public chat request so agents know they're serving a public user
PUBLIC_LINK_MODE_HEADER = "### Trinity: Public Link Access Mode"
```

**Context Building** (`db/public_chat.py:245-280`):
```python
def build_context_prompt(
    self,
    session_id: str,
    new_message: str,
    max_turns: int = 10
) -> str:
    """Build a prompt with conversation history for context injection."""
    messages = self.get_recent_messages(session_id, limit=max_turns * 2)

    # Start with public link mode header
    parts = [PUBLIC_LINK_MODE_HEADER, ""]

    if messages:
        parts.append("Previous conversation:")
        for msg in messages:
            role_label = "User" if msg.role == "user" else "Assistant"
            parts.append(f"{role_label}: {msg.content}")
        parts.append("")

    parts.append("Current message:")
    parts.append(f"User: {new_message}")

    return "\n".join(parts)
```

### Context Injection Format

Every public chat message is formatted as:

```
### Trinity: Public Link Access Mode

Previous conversation:
User: [message 1]
Assistant: [response 1]
...

Current message:
User: [new message]
```

For first messages (no history):

```
### Trinity: Public Link Access Mode

Current message:
User: [new message]
```

### Agent Use Cases

Agents can detect the `### Trinity: Public Link Access Mode` header to:
1. Use more formal/professional language
2. Avoid revealing internal implementation details
3. Limit responses to public-appropriate information
4. Skip internal tool usage that requires authentication
5. Provide external documentation links instead of internal resources

---

## Bottom-Aligned Chat Messages (UI)

**Status**: Implemented (2026-02-17)

The public chat interface now displays messages aligned to the bottom of the chat area, similar to iMessage, Slack, and other modern chat applications.

### UI Behavior

| Before | After |
|--------|-------|
| Messages start at top, scroll down | Messages appear near input field |
| Empty space at bottom | Empty space at top |
| Traditional forum-style layout | Modern messaging app layout |

### Implementation

**HTML Structure** (`PublicChat.vue:190-259`):
```vue
<!-- Messages area - flex container that pushes content to bottom -->
<div class="flex-1 overflow-y-auto flex flex-col" ref="messagesContainer">
  <!-- Spacer that pushes content to bottom -->
  <div class="flex-1"></div>

  <!-- Messages content wrapper -->
  <div class="space-y-4 pb-4">
    <!-- Loading intro, welcome message, message list, etc. -->
  </div>
</div>
```

**Scroll Handling** (`PublicChat.vue:334,642-647`):
```javascript
const messagesContainer = ref(null)

const scrollToBottom = () => {
  if (messagesContainer.value) {
    messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
  }
}
```

### Flexbox Layout Strategy

1. **Container**: `flex-1 overflow-y-auto flex flex-col` - Takes available space, allows scrolling, arranges children vertically
2. **Spacer**: `flex-1` empty div - Expands to fill available space, pushing content down
3. **Content**: Messages wrapper with fixed content - Stays at bottom of container

---

## Agent Website Proxy (SITE-001 / Issue #633)

**Status**: RETIRED — removed in #865 (2026-05-17)

> **Design decision (2026-05-17):** The port-3000 reverse-proxy approach was retired after design review. Three problems: (1) agents must manage a live web server process — fragile, no standardization; (2) arbitrary HTML/JS served from the same origin as the Trinity frontend creates XSS risk; (3) `file_sharing_enabled` permission gate was not enforced.
>
> The replacement design uses `dashboard.yaml` — Trinity owns the renderer, agents push structured data, no arbitrary HTML reaches the browser. See companion issue for SITE-002.
>
> **What was removed**: `src/backend/routers/site.py`, `SITE_PORT` constant, router mount in `main.py`, `location /site/` nginx block, "Website" link-type option in `PublicLinksPanel.vue`.
>
> **What was kept for SITE-002 reuse**: `agent_public_links.type` DB column and schema, `AuditEventType.SITE_ACCESS`, Docker volume infrastructure (`agent-{name}-public`, `/home/developer/public/`), `file_sharing_enabled` flag on `agent_ownership`, rate-limiting helpers pattern, token validation pattern.

### Related Flows

- [public-agent-links.md](public-agent-links.md) — chat links (type='chat') share the same `agent_public_links` table and owner management UI
- [audit-trail.md](audit-trail.md) — `SITE_ACCESS` event type added to `AuditEventType`

### Scroll Behavior

- `scrollToBottom()` called after user sends message (`sendMessage()` lines 585, 631)
- `scrollToBottom()` called after message history loads
- Container automatically scrolls to show new messages
- User can scroll up to see older messages

### Files Changed

| File | Lines | Changes |
|------|-------|---------|
| `src/frontend/src/views/PublicChat.vue` | 684 | Added `messagesContainer` ref (334), spacer div (193), flexbox layout (191-192), updated scroll handling (642-647) |

---

---

## Slack Integration (SLACK-001)

**Status**: Implemented (2026-02-25)
**Spec**: `docs/requirements/SLACK_INTEGRATION.md`
**Feature Flow**: [slack-integration.md](slack-integration.md)

Slack can be connected to a public link as an additional delivery channel. Users DM the Slack bot to chat with the agent, using the same session persistence infrastructure.

### Key Points

- **1:1 mapping**: One Slack workspace connects to one public link
- **Session reuse**: `public_chat_sessions` with `identifier_type='slack'`, `session_identifier='{team_id}:{user_id}'`
- **Email verification**: Follows the agent-level `agent_ownership.require_email` policy (same as web and Telegram). As of 2026-04-13, this is no longer a per-link flag — see [unified-channel-access-control.md](unified-channel-access-control.md).
- **UI location**: Slack section in PublicLinksPanel.vue (lines 125-181)

### New Database Tables

| Table | Purpose |
|-------|---------|
| `slack_link_connections` | Maps Slack workspace to public link |
| `slack_user_verifications` | Tracks verified Slack users |
| `slack_pending_verifications` | In-progress verification state |

### New API Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /api/public/slack/events` | Receive Slack DM events |
| `GET /api/public/slack/oauth/callback` | OAuth completion |
| `GET/PUT/DELETE /api/agents/{name}/public-links/{id}/slack` | Connection management |
| `POST .../slack/connect` | Initiate OAuth |

See [slack-integration.md](slack-integration.md) for complete implementation details.

---

## Dynamic Thinking Status (THINK-001)

**Status**: Implemented (2026-03-04)
**Feature Flow**: [dynamic-thinking-status.md](dynamic-thinking-status.md)

Real-time status labels during public chat execution, replacing the static "Thinking..." indicator with dynamic labels like "Reading file...", "Running command...", "Searching code...", etc.

### Architecture

```
PublicChat.vue                         Backend (public.py)              Agent Container
     |                                      |                               |
     |  POST /api/public/chat/{token}       |                               |
     |  {message, async_mode: true}         |                               |
     |------------------------------------->|                               |
     |                                      | create execution record       |
     |  {status: "accepted",               | spawn background task         |
     |   execution_id: "..."}              |                               |
     |<-------------------------------------|                               |
     |                                      |                               |
     |  GET .../executions/{token}/{id}/stream                              |
     |------------------------------------->|  GET /api/executions/{id}/stream
     |                                      |------------------------------>|
     |  SSE: {type: "stream-json",         |  SSE: stream-json events      |
     |   tool: "Read", ...}                |<------------------------------|
     |<-------------------------------------|                               |
     |  getStatusFromStreamEvent()          |                               |
     |  -> "Reading file..."               |  execute_task()               |
     |                                      |------------------------------>|
     |  GET .../executions/{token}/{id}/status (polling every 5s)          |
     |------------------------------------->|                               |
     |  {status: "running"}                |                               |
     |<-------------------------------------|                               |
     |                                      |  task complete                |
     |  {status: "success",               |<------------------------------|
     |   response: "..."}                  |                               |
     |<-------------------------------------|                               |
```

### Backend Implementation

**Async mode in `public_chat()`** (`public.py:320-348`):
- When `async_mode=True`, creates execution record early via `db.create_task_execution()`
- Spawns `_execute_public_chat_background()` via `asyncio.create_task()`
- Returns `{status: "accepted", execution_id, agent_name, session_id, async_mode: true}` immediately

**`_execute_public_chat_background()`** (`public.py:636-673`):
- Runs task via `TaskExecutionService.execute_task()` with pre-created `execution_id`
- Stores assistant response in `public_chat_messages` on success
- Logs errors without raising (background task)

**SSE stream proxy** (`public.py:676-732`):
- `GET /api/public/executions/{token}/{execution_id}/stream`
- Validates public link token (no JWT required)
- Verifies execution belongs to the agent associated with the link
- Proxies SSE stream from `http://agent-{name}:8000/api/executions/{id}/stream`
- Returns `StreamingResponse` with `text/event-stream` media type

**Execution status polling** (`public.py:735-764`):
- `GET /api/public/executions/{token}/{execution_id}/status`
- Validates public link token
- Returns `{execution_id, status, response, error}`
- `response` only populated when status is `success` or `failed`

### Frontend Implementation

**State** (`PublicChat.vue:288-294`):
```javascript
const loadingText = ref('Thinking...')    // Dynamic status text
let heartbeatTimer = null                  // Fallback timer
let labelTimer = null                      // Anti-flicker timer
let lastLabelTime = 0                      // Last label update timestamp
let streamReader = null                    // SSE reader reference
```

**`sendMessage()`** (`PublicChat.vue:661-744`):
1. Submits with `async_mode: true` (line 681)
2. Receives `execution_id` from response (line 695)
3. Calls `subscribeToStream(executionId)` for SSE status labels (line 707)
4. Calls `pollExecution(executionId)` for completion (line 710)
5. On completion: closes SSE, displays assistant response

**`subscribeToStream()`** (`PublicChat.vue:568-632`):
- Uses `fetch()` (not `EventSource`) since no auth headers needed for public endpoints
- Reads SSE stream via `response.body.getReader()`
- Parses `data:` lines as JSON, calls `getStatusFromStreamEvent()` to map to labels
- Calls `updateLoadingText()` with the result

**`updateLoadingText()`** (`PublicChat.vue:525-543`):
- Enforces minimum 500ms display per label (`MIN_LABEL_DISPLAY_MS`)
- Defers updates if within the minimum window
- Calls `resetHeartbeat()` on each update

**`resetHeartbeat()`** (`PublicChat.vue:546-553`):
- Sets 10-second timer (`HEARTBEAT_TIMEOUT_MS`)
- Falls back to "Working..." if no SSE events received

**`pollExecution()`** (`PublicChat.vue:635-658`):
- Polls `GET /api/public/executions/{token}/{execution_id}/status` every 5 seconds
- Returns execution data when status is `success`, `failed`, or `cancelled`
- Maximum 360 attempts (30 minutes)

**`closeSSE()`** (`PublicChat.vue:556-565`):
- Cancels `streamReader`, clears timers
- Called on completion, error, and `onUnmounted`

**Template** (`PublicChat.vue:204`):
```vue
<ChatLoadingIndicator :loading-text="loadingText" />
```
(Changed from static `loading-text="Thinking..."` to dynamic `:loading-text="loadingText"`)

### Security

- SSE stream and status endpoints validate the public link token instead of JWT
- Execution ownership verified: the execution must belong to the agent associated with the link
- No sensitive execution data exposed (only status, response text, and error message)

### Related

- **Authenticated chat**: `ChatPanel.vue` uses the same `execution-status.js` utilities with JWT-authenticated endpoints
- **Feature flow**: [dynamic-thinking-status.md](dynamic-thinking-status.md) documents the core THINK-001 implementation

---

## Per-User Persistent Memory (MEM-001)

**Status**: Implemented 2026-03-19 (#147). Split storage + channel injection: 2026-05-18 (#895).
**Issues**: #147 (initial), #888 (`write_user_memory` MCP tool), #895 (split storage + channel injection)

When a public link requires email verification, the agent accumulates a persistent memory of facts about each verified user. This memory is injected into the system prompt on every request so the agent can personalize responses across sessions.

Anonymous (unverified) sessions are unaffected — no memory is read or written.

**#895 — Split storage**: The `memory_text` column now holds a JSON object with two named sections so the agent-deliberate writer (`write_user_memory` MCP tool, #888) and the every-5-message background summarizer can update independent sections without clobbering each other.

**#895 — Channel injection**: Slack / Telegram / WhatsApp DMs now inject memory the same way the web path does, gated on `verified_email and not is_group`. Group chats are excluded because `verified_email` there is the *unlocker's* email (set once per group), so injecting their memory into replies addressed to other group members would leak PII across users.

### Storage Shape (#895)

The `public_user_memory.memory_text` TEXT column holds a JSON object:

```json
{
  "agent_notes": "<written only by the write_user_memory MCP tool>",
  "conversation_summary": "<written only by the background summarizer>"
}
```

- The schema is unchanged — no migration. The column still has type `TEXT NOT NULL DEFAULT ''`.
- **Legacy plaintext rows** (written before #895) are transparently surfaced as `conversation_summary` on read via `db/public_links._parse_memory_blob`. The next section-targeted write upgrades the row to JSON shape in place.
- Malformed JSON (anything that isn't a JSON object) is also treated as plaintext → `conversation_summary`.

### Database Table: `public_user_memory`

`src/backend/db/schema.py` — table and unique index (DDL unchanged by #895).

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | Unique record ID |
| agent_name | TEXT | Target agent |
| user_email | TEXT | Verified user email (stored lowercase) |
| memory_text | TEXT | JSON `{agent_notes, conversation_summary}` since #895 (default `''`) |
| message_count | INTEGER | Cumulative messages sent by this user |
| created_at | TEXT | ISO timestamp of first message |
| updated_at | TEXT | ISO timestamp of last write |

UNIQUE constraint on `(agent_name, user_email)` — one memory blob per user per agent, shared across web and channel sessions.

Index: `idx_public_user_memory_lookup ON public_user_memory(agent_name, user_email)`.

Migration: `src/backend/db/migrations.py` — migration #28 `_migrate_public_user_memory_table` (#147; no new migration for #895).

### Database Operations (#895)

Methods on `PublicLinkOperations` (`src/backend/db/public_links.py`):

| Method | Description |
|--------|-------------|
| `get_or_create_user_memory(agent_name, user_email)` | Fetch existing record or `INSERT` a blank row; returns the parsed dict with `agent_notes` and `conversation_summary` keys (plus id/agent_name/user_email/message_count/created_at/updated_at). |
| `increment_message_count(agent_name, user_email)` | Atomic `UPDATE message_count + 1`; returns the new count. |
| `update_user_memory_agent_notes(agent_name, user_email, agent_notes)` | Section write: read-modify-write that replaces only the `agent_notes` key. Used by the `write_user_memory` MCP tool (#888). |
| `update_user_memory_conversation_summary(agent_name, user_email, summary)` | Section write: replaces only `conversation_summary`. Used by `summarize_user_memory_background`. |

Module-level helpers in the same file:
- `_parse_memory_blob(text)` — handles JSON, legacy plaintext, malformed input. Returns `{agent_notes, conversation_summary}` dict.
- `_encode_memory_blob(agent_notes, conversation_summary)` — JSON-encodes for storage with `ensure_ascii=False`.

Delegation methods on `db` in `src/backend/database.py`:
- `get_or_create_public_user_memory(agent_name, user_email)` → `PublicLinkOperations.get_or_create_user_memory()`
- `increment_public_user_memory_count(agent_name, user_email)` → `PublicLinkOperations.increment_message_count()`
- `update_public_user_memory_agent_notes(agent_name, user_email, agent_notes)` → `PublicLinkOperations.update_user_memory_agent_notes()` (#895)
- `update_public_user_memory_conversation_summary(agent_name, user_email, summary)` → `PublicLinkOperations.update_user_memory_conversation_summary()` (#895)

### Read Flow (Memory Injection)

Both the **web** path and the **channel-adapter** path follow the same pattern:

```
1. record = db.get_or_create_public_user_memory(agent_name, verified_email)
2. memory_system_prompt = format_user_memory_block(record)
3. execute_task(..., system_prompt=memory_system_prompt)
```

`format_user_memory_block(memory_record)` (`src/backend/services/platform_prompt_service.py`) accepts the parsed dict and emits a multi-section markdown block — or returns `None` when both sections are empty so callers can skip the `--append-system-prompt` injection entirely.

```
## What you know about this user

### Agent notes

{agent_notes}

### Conversation summary

{conversation_summary}

---
```

Either section is omitted when empty. Agent notes render first (higher signal than the auto-summary).

**Web path** — `routers/public.py` (sync + async background paths):

| Path | Gate |
|------|------|
| `public_chat()` (sync) | `identifier_type == "email" and verified_email` |
| `_execute_public_chat_background()` (async) | same |

**Channel-adapter path** — `adapters/message_router.py:_handle_message_inner` (#895):

| Path | Gate |
|------|------|
| All channels (Slack DM, Telegram DM, WhatsApp DM) | `verified_email and not is_group` |
| Group chats (any channel) | excluded — see PII rationale above |

The channel-side fetch is wrapped in try/except with a warning log; a memory-fetch failure never breaks the channel response.

### Write Flow (Background Summarization)

After execution completes, the count is incremented and the summarizer fires every 5 messages — same logic on web and channel paths:

```
1. new_count = db.increment_public_user_memory_count(agent_name, verified_email)
2. If new_count % 5 == 0:
     asyncio.create_task(summarize_user_memory_background(
         agent_name=agent_name,
         user_email=verified_email,
         session_id=session_id,
     ))
```

`summarize_user_memory_background(agent_name, user_email, session_id)` lives in `services/platform_prompt_service.py` (extracted from `routers/public.py` in #895). It is fire-and-forget:

1. Reads `ANTHROPIC_API_KEY` via `get_anthropic_api_key()`; skips silently if absent.
2. Calls `db.get_or_create_public_user_memory()` and reads the current `conversation_summary` (not `agent_notes` — the summarizer must not see deliberate writes).
3. Calls `db.get_recent_public_chat_messages(session_id, limit=20)` for the last 20 messages.
4. Formats `_SUMMARIZATION_PROMPT` with `existing_memory` and `conversation` fields.
5. POSTs to `https://api.anthropic.com/v1/messages` using model `claude-haiku-4-5-20251001`, `max_tokens=512`, 30-second timeout.
6. On success, writes the returned text to `db.update_public_user_memory_conversation_summary()` — touches only the summary section.
7. All failures are logged (`[MemSummarize]` prefix) and never surfaced to the user.

Summarization is triggered every 5th message per `(agent_name, user_email)` pair — not per session, per link token, or per channel.

### Channel-Side Hook (#895)

Wired into the same step in `_handle_message_inner` (`src/backend/adapters/message_router.py`):
- Memory fetch: between source-email resolution and `execute_task` call, gated on `verified_email and not is_group`.
- Increment + summarizer trigger: after `db.add_public_chat_message(session_id, "assistant", ...)`, same gate, also wrapped in try/except.

### Scope

| Session type | Memory read | Memory written |
|---|---|---|
| Email-verified web | Yes (if non-empty) | Yes (every 5th message) |
| Email-verified Slack/Telegram/WhatsApp DM | Yes — added in #895 | Yes — added in #895 |
| Group chat (any channel) | No — PII guard | No |
| Anonymous (no verified email, any channel) | No | No |

### Known Limitations

- **Race window in section writes (#895)**: `update_user_memory_*` does a read-modify-write inside a single connection. Two writers (e.g., write_user_memory + summarizer firing within ~10ms) can interleave and one write may be lost. The window is much smaller than the pre-#895 deterministic clobber but not zero. A follow-up using atomic SQLite JSON1 `json_set` would close it.
- **Summarizer cost ceiling**: no per-(agent, user) cooldown — applies to every 5th message. Pre-existing concern.
- **Memory-text safety filter**: the system-prompt block is rendered raw, leaving a prompt-injection surface from agent-curated content. Pre-existing concern.

### Files

| File | Description |
|------|-------------|
| `src/backend/db/schema.py` | `public_user_memory` table + lookup index (unchanged by #895) |
| `src/backend/db/migrations.py` | Migration #28 `_migrate_public_user_memory_table` (#147) |
| `src/backend/db/public_links.py` | `_parse_memory_blob`, `_encode_memory_blob`, `PublicLinkOperations` memory methods (#895 split storage) |
| `src/backend/database.py` | `db` facade — `get_or_create_public_user_memory`, `increment_public_user_memory_count`, `update_public_user_memory_agent_notes`, `update_public_user_memory_conversation_summary` |
| `src/backend/services/platform_prompt_service.py` | `format_user_memory_block(dict)`, `summarize_user_memory_background()` (extracted in #895) |
| `src/backend/routers/public.py` | Memory injection + counter + summarizer trigger on the web path (sync + async background) |
| `src/backend/routers/public_memory.py` | `POST /api/agents/{name}/user-memory` (calls `update_public_user_memory_agent_notes` since #895) |
| `src/backend/adapters/message_router.py` | Memory injection + counter + summarizer trigger on the channel-adapter path (#895) |

---

## Chat History for Logged-In Users (#587)

**Status**: Implemented (2026-04-29)

Logged-in Trinity users visiting a public chat link can browse and replay their own past chat sessions. Anonymous users see no change to the existing UI.

### Design Principles

- The public link token remains the agent-access credential; the JWT identifies which user's sessions to return.
- Past sessions are opened in **read-only mode** — the chat input is hidden to prevent false continuity with the live agent context.
- Access does not require `agent_sharing` membership; token + JWT is sufficient.
- Only sessions created while logged in are visible ("Logged-in chats only" — anonymous sessions are excluded).

### Data Flow

```
Logged-in user opens /chat/{token}
        |
        v
PublicChat.vue checks authStore.isAuthenticated
  -> true: renders ChatHistoryDropdown in header
        |
        v
User clicks history button
        |
        v
ChatHistoryDropdown fetches
  GET /api/public/sessions/{token}
  Authorization: Bearer <jwt>
        |
        v
Backend: validate token -> resolve agent_name
  SELECT last 20 chat_sessions WHERE
    agent_name = ? AND user_id = ?
  ORDER BY last_message_at DESC
  Includes preview (last message snippet, 120 chars)
        |
        v
Dropdown renders session list
  - Formatted date (Today / Yesterday / 3d ago / Apr 5)
  - Message count
  - Preview snippet
        |
        v
User clicks a session
        |
        v
ChatHistoryDropdown fetches
  GET /api/public/sessions/{token}/{session_id}
  Authorization: Bearer <jwt>
        |
        v
Backend: validate token -> resolve agent_name
  Verify session.agent_name == agent_name
  Verify session.user_id == current_user.id
  Return session with messages
        |
        v
PublicChat.vue: handleHistorySessionSelected({ messages, session })
  viewingHistorySession = session
  messages loaded in read-only mode
  ChatInput hidden
  Amber banner shown: "Viewing past session — Return to current chat"
        |
        v
User clicks "Return to current chat"
        |
        v
exitHistoryView():
  viewingHistorySession = null
  reload current live session or fetchIntro()
  ChatInput restored
```

### Backend Implementation

**Two new endpoints in `src/backend/routers/public.py`:**

`GET /api/public/sessions/{token}` — list sessions:
1. Validate public link token (same `is_link_valid()` check used throughout the module).
2. Resolve `agent_name` from the link row.
3. Require JWT (`current_user` dependency — not optional).
4. Query `chat_sessions` for rows matching `agent_name` and `user_id`, ordered by `last_message_at DESC`, limit 20.
5. For each session, compute `preview` by reading the most recent `chat_messages` row (`role = 'assistant'` preferred) and truncating to 120 chars.
6. Return list of session dicts (id, started_at, last_message_at, message_count, preview).

`GET /api/public/sessions/{token}/{session_id}` — session detail:
1. Validate public link token.
2. Resolve `agent_name`.
3. Require JWT.
4. Fetch session by `session_id`; return 404 if not found.
5. Assert `session.agent_name == agent_name` and `session.user_id == current_user.id`; return 403 otherwise.
6. Fetch all messages for the session ordered by `timestamp ASC`.
7. Return session metadata + messages array.

**No new database tables or migrations.** Both endpoints reuse the existing `chat_sessions` and `chat_messages` tables (documented in the main Database Schema section of `architecture.md`).

### Frontend Layer

#### New Component: `ChatHistoryDropdown.vue`

**File**: `src/frontend/src/components/chat/ChatHistoryDropdown.vue`

| Prop / Emit | Type | Description |
|-------------|------|-------------|
| `token` prop | String | Public link token (used in API calls) |
| `session-selected` emit | `{ messages, session }` | Fired when user selects a session |

**Key behaviors:**
- On open: fetches `GET /api/public/sessions/{token}` using `authStore.authHeader`.
- Renders a dropdown list of sessions, each showing formatted date, message count, and preview snippet.
- Click-outside handler closes the dropdown.
- Date formatting: "Today", "Yesterday", "3d ago", or locale short date (e.g., "Apr 5").

**Imports** (added to `src/frontend/src/components/chat/index.js`):
```javascript
export { default as ChatHistoryDropdown } from './ChatHistoryDropdown.vue'
```

#### Changes to `PublicChat.vue`

**New imports:**
```javascript
import { useAuthStore } from '../stores/auth'
import { ChatHistoryDropdown } from '../components/chat'
```

**New state:**
```javascript
const authStore = useAuthStore()
const viewingHistorySession = ref(null)   // non-null = read-only history mode
```

**New methods:**

| Method | Description |
|--------|-------------|
| `handleHistorySessionSelected({ messages, session })` | Sets `viewingHistorySession`, replaces displayed messages with the past session's messages |
| `exitHistoryView()` | Clears `viewingHistorySession`; reloads current live session via `loadHistory()` or falls back to `fetchIntro()` |

**Template changes:**
- Header: `<ChatHistoryDropdown v-if="authStore.isAuthenticated && linkInfo?.valid" :token="token" @session-selected="handleHistorySessionSelected" />`
- Amber read-only banner: shown when `viewingHistorySession` is set; includes "Return to current chat" button that calls `exitHistoryView()`.
- `<ChatInput>` wrapped in `v-if="!viewingHistorySession"` — hidden during history replay.

### Access Control Summary

| Condition | Sessions endpoint behavior |
|-----------|---------------------------|
| No JWT | 401 Unauthorized |
| Valid JWT, valid token | Returns caller's own sessions only |
| Valid JWT, wrong user's session_id | 403 Forbidden |
| Valid JWT, session belongs to different agent | 403 Forbidden |

### Error Handling

| Error Case | HTTP Status | Notes |
|------------|-------------|-------|
| Invalid / disabled / expired public link token | 404 | Same as all other `/api/public/*` endpoints |
| No JWT provided | 401 | `Depends(get_current_user)` rejects unauthenticated requests |
| Session not found | 404 | Unknown `session_id` |
| Session belongs to wrong user or agent | 403 | Ownership mismatch |

### Files

| File | Description |
|------|-------------|
| `src/backend/routers/public.py` | Two new endpoint handlers: `list_public_sessions()`, `get_public_session()` |
| `src/frontend/src/components/chat/ChatHistoryDropdown.vue` | New dropdown component (new file) |
| `src/frontend/src/components/chat/index.js` | Exports `ChatHistoryDropdown` |
| `src/frontend/src/views/PublicChat.vue` | Imports `useAuthStore`, `ChatHistoryDropdown`; new `viewingHistorySession` ref; `handleHistorySessionSelected()`, `exitHistoryView()` methods; conditional header, banner, input |

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-05-18 | **#895 MEM-001 split storage + channel injection**: `public_user_memory.memory_text` now holds JSON `{agent_notes, conversation_summary}`; the agent-deliberate `write_user_memory` tool (#888) and the every-5-message background summarizer each update their own section so they cannot clobber each other. Legacy plaintext rows surface as `conversation_summary` transparently. Channel adapters (Slack/Telegram/WhatsApp) now mirror the web injection — memory is read, formatted, and passed as `system_prompt=` to `execute_task`, gated on `verified_email and not is_group` (group mode excluded to prevent PII leak from the unlocker's memory). Counter + summarizer trigger added to the channel path on the same gate. Summarizer extracted to `services/platform_prompt_service.summarize_user_memory_background` so web and channel share it. `format_user_memory_block(dict)` rewritten to render both sections; returns `None` when both are empty so callers skip injection. No schema migration. |
| 2026-05-18 | **#890**: `request_verification_code()` now passes `agent_name` to `email_service.send_verification_code()` so the verification email subject and body name the agent. |
| 2026-04-29 | **#587 Chat History for Logged-In Users**: Two new JWT-authenticated endpoints (`GET /api/public/sessions/{token}`, `GET /api/public/sessions/{token}/{session_id}`) in `public.py`. New `ChatHistoryDropdown.vue` component. `PublicChat.vue` gains `viewingHistorySession` ref, `handleHistorySessionSelected()`, `exitHistoryView()`, amber read-only banner, and hidden `ChatInput` while in history mode. No new DB tables — reuses `chat_sessions`/`chat_messages`. |
| 2026-04-27 | **fix #539 Context duplication**: `build_public_chat_context()` was called AFTER `add_public_chat_message(role="user")`, causing the current user message to appear twice in every agent prompt (once in "Previous conversation:", once in "Current message:"). Fixed by swapping the call order — context built first from prior history, user message stored after. Added 6 unit tests in `tests/unit/test_public_chat_context.py`. Updated PUB-005 data flow and backend implementation step ordering to reflect correct call order. |
| 2026-02-19 | **CHAT-001 Shared Components Refactor**: PublicChat.vue now uses shared components from `components/chat/` (ChatMessages, ChatInput, ChatBubble, ChatLoadingIndicator). Shared with new ChatPanel.vue authenticated chat. Updated method line numbers, added Shared Chat Components section. File now 611 lines. |
| 2026-02-18 | **Tab consolidation**: Public Links tab removed from AgentDetail.vue. PublicLinksPanel now embedded within SharingPanel.vue (lines 82-83, 92), accessible via "Sharing" tab. Updated Entry Points, Components table, Frontend Files table, and Related Flows sections. |
| 2025-12-22 | Initial documentation |
| 2025-12-30 | Verified line numbers, updated file references, added detailed method tables, expanded testing section |
| 2026-01-23 | Refreshed line numbers for all backend files: public_links.py endpoints (56, 92, 107, 126, 167), public.py endpoints (43, 73, 130, 166), db_models.py models (301-374). Updated AgentDetail.vue references (167, 169, 230, 443-446). Added delete_agent_public_links() at db/public_links.py:156. Updated main.py references (44-45, 112, 292-293). Corrected file line counts. |
| 2026-02-16 | **Implemented PUB-002 External Public URL**: Added `PUBLIC_CHAT_URL` env var, `external_url` field in API response, globe icon button in UI for copying external links. Configuration section updated. |
| 2026-02-17 | **Expanded PUB-002 documentation**: Added detailed data flow diagram, CORS auto-configuration, complete code snippets with exact line numbers (config.py:43-45,86-88; public_links.py:35-39,42-61,58-59; db_models.py:329-333; PublicLinksPanel.vue:79-102,311-317,461-473). Added API response example and infrastructure options. |
| 2026-02-17 | **Implemented PUB-003 Agent Introduction**: New `GET /api/public/intro/{token}` endpoint sends intro prompt to agent. Frontend `fetchIntro()` displays response as first chat message. Added Agent Introduction section with full implementation details. |
| 2026-02-17 | **Updated for PUB-003**: Refreshed PublicChat.vue method line numbers (276, 314, 337, 376, 411), added `fetchIntro()` to methods table, updated file line count (508 lines), corrected PUB-003 section line number (376-408). |
| 2026-02-17 | **Implemented PUB-004 Public Chat Header Metadata**: `GET /api/public/link/{token}` now returns `agent_display_name`, `agent_description`, `is_autonomous`, `is_read_only`. PublicChat.vue header displays agent name, description, and status badges (AUTO amber, READ-ONLY rose with lock). Updated PublicLinkInfo model (db_models.py:336-346), public.py endpoint (lines 44-103), PublicChat.vue header (lines 4-46). Refreshed method line numbers (306, 344, 367, 406, 441). File now 538 lines. |
| 2026-02-17 | **Implemented PUB-005 Public Chat Session Persistence**: Multi-turn conversation persistence with `public_chat_sessions` and `public_chat_messages` tables. New `db/public_chat.py` with operations class. Updated `POST /api/public/chat/{token}` to persist messages and build context prompt. New `GET /api/public/history/{token}` and `DELETE /api/public/session/{token}` endpoints. Frontend session management with localStorage for anonymous links, history loading on mount, "New Conversation" button. |
| 2026-02-17 | **PUB-005 documentation refresh**: Added exact line numbers for all backend endpoints (chat:214, history:465, session:541), db operations (public_chat.py methods with lines), database schema locations (660-687, 781-783), delegation methods (1404-1432), and frontend methods (loadHistory:429, sendMessage:544, confirmNewConversation:503). Added response model documentation, error handling table, cost tracking details, and expanded test scenarios. Updated file line counts (public.py:603, PublicChat.vue:658, db_models.py:483). |
| 2026-02-17 | **Implemented PUB-006 Public Client Mode Awareness**: Agents now receive `PUBLIC_LINK_MODE_HEADER` constant ("### Trinity: Public Link Access Mode") prepended to all public chat prompts via `build_context_prompt()` in `db/public_chat.py:17-18,265-266`. **UI: Bottom-aligned messages**: Chat messages now stack from bottom up (like iMessage/Slack) using flexbox with spacer div (lines 191-193), new `messagesContainer` ref for scroll handling (line 334). File line count: PublicChat.vue now 684 lines. |
| 2026-02-25 | **SLACK-001 Slack Integration**: Added Slack as delivery channel for public links. New section documenting Slack integration, updated Related Flows to include slack-integration.md. See [slack-integration.md](slack-integration.md) for full implementation details. |
| 2026-03-04 | **EXEC-024 TaskExecutionService refactor**: `POST /api/public/chat/{token}` now routes through `TaskExecutionService.execute_task(triggered_by="public")` instead of raw `httpx` call. Public executions now create `schedule_executions` records, appear in Tasks tab and Dashboard timeline, count toward capacity slots (429 when full), and have credential-sanitized logs. Updated architecture diagram, data flow sections, chat endpoint steps, cost tracking, error handling, and file references. Added `task_execution_service.py` to files list. |
| 2026-03-19 | **MEM-001 Per-User Persistent Memory** (#147): Added `public_user_memory` table (schema.py:360-371, migration #28). Email-verified sessions inject per-user memory via `format_user_memory_block()` (platform_prompt_service.py:97) as `system_prompt` kwarg to `execute_task()` — both sync (public.py:368) and async paths (public.py:685). Background summarization via `_summarize_user_memory()` (public.py:727-784) fires every 5th message using `claude-haiku-4-5-20251001`. New DB methods on `PublicLinkOperations` (public_links.py:439-522). Architecture diagram updated to include `public_user_memory`. Anonymous sessions unaffected. |
| 2026-03-04 | **THINK-001 Dynamic Thinking Status for Public Chat**: Added async mode (`async_mode` field in `PublicChatRequest`), SSE stream proxy (`GET /api/public/executions/{token}/{id}/stream`), execution status polling (`GET /api/public/executions/{token}/{id}/status`), and background task function (`_execute_public_chat_background`). Frontend updated: `sendMessage()` uses async mode, `subscribeToStream()` for SSE, `pollExecution()` for completion, `updateLoadingText()` with anti-flicker timing, `resetHeartbeat()` fallback. Updated endpoints table, data flow, methods table, components, files. public.py now 764 lines, PublicChat.vue now 786 lines. |
