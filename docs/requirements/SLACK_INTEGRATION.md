# Slack Integration for Public Agents (SLACK-001)

> **Status**: Implemented
> **Priority**: P1
> **Author**: Claude + Eugene
> **Created**: 2026-02-25
> **Implemented**: 2026-03-12

## Overview

Enable Slack as a delivery channel for public agent links. Users can chat with Trinity agents via **direct messages (DMs)** to a Slack bot, using the same security model as web-based public links.

## Goals

1. **Extend, don't bypass**: Use existing public links infrastructure
2. **Explicit opt-in**: Agent owners must enable Slack per public link
3. **User authentication**: Verify Slack users before allowing chat (when required)
4. **Instance-agnostic**: Configuration works across any Trinity deployment
5. **Security parity**: Same rate limiting, session management, and audit logging as web
6. **Simplicity**: DMs only, no channel complexity

## Non-Goals

- Channel @mentions or public channel conversations (Phase 1)
- Slack slash commands
- Threading within DMs
- Real-time typing indicators
- Multi-agent routing within single workspace

---

## Architecture

### Deployment Model

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         Instance-Agnostic Architecture                           │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                   │
│   Environment Variables (per instance):                                           │
│   ┌─────────────────────────────────────────────────────────────────────────┐   │
│   │ PUBLIC_CHAT_URL=https://public.example.com     # Public-facing URL       │   │
│   │ FRONTEND_URL=https://admin.example.com         # Admin UI URL            │   │
│   │ SLACK_SIGNING_SECRET=xxxxx                     # From Slack App settings │   │
│   │ SLACK_CLIENT_ID=xxxxx                          # For OAuth flow          │   │
│   │ SLACK_CLIENT_SECRET=xxxxx                      # For OAuth flow          │   │
│   └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                   │
│   URL Derivation:                                                                 │
│   - Slack Events URL: ${PUBLIC_CHAT_URL}/api/public/slack/events                 │
│   - OAuth Callback:   ${PUBLIC_CHAT_URL}/api/public/slack/oauth/callback         │
│   - Admin Redirect:   ${FRONTEND_URL}/agents/{name}?tab=sharing                  │
│                                                                                   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Data Flow (DM-Only)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         Slack DM Message Flow                                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                   │
│   Slack User                                                                      │
│        │                                                                          │
│        │ Sends DM to bot                                                          │
│        v                                                                          │
│   ┌─────────────┐     POST with X-Slack-Signature                                │
│   │ Slack API   │ ──────────────────────────────────────────┐                    │
│   │ message.im  │                                            │                    │
│   └─────────────┘                                            v                    │
│                                            ${PUBLIC_CHAT_URL}/api/public/slack/events
│                                                              │                    │
│                              ┌───────────────────────────────┴─────────────────┐ │
│                              │  1. Verify Slack signature (SLACK_SIGNING_SECRET)│ │
│                              │  2. Lookup link by team_id (workspace → link)    │ │
│                              │  3. Check if user verified (see Auth Flow)       │ │
│                              │  4. Rate limit check                              │ │
│                              │  5. Get/create session (keyed by Slack user_id)  │ │
│                              │  6. Build context prompt (existing logic)        │ │
│                              │  7. POST to agent /api/task                      │ │
│                              │  8. POST to Slack chat.postMessage (to DM)       │ │
│                              └──────────────────────────────────────────────────┘ │
│                                                                                   │
│   Key simplification: One Slack workspace = One public link = One agent          │
│   No channel configuration needed - all DMs in workspace route to same agent     │
│                                                                                   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## User Authentication Flow

When a public link has `require_email=true`, Slack users must verify before chatting.

### Verification Options

| Method | UX | Privacy | Implementation |
|--------|----|---------| --------------|
| **Slack Profile Email** | Seamless (no user action) | Requires `users:read.email` scope | Use Slack API to get email |
| **Email Code** | User enters email, receives code | No extra Slack permissions | Reuse existing verification |
| **Magic Link** | User clicks link in DM | No extra Slack permissions | One-click verification |

**Recommended: Slack Profile Email (primary) + Email Code (fallback)**

### Authentication State Machine

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        Slack User Verification Flow                              │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                   │
│   User sends first message                                                        │
│        │                                                                          │
│        v                                                                          │
│   ┌────────────────────────────┐                                                 │
│   │ Link requires email?        │                                                 │
│   └────────────┬───────────────┘                                                 │
│                │                                                                  │
│       ┌────────┴────────┐                                                        │
│       │ NO              │ YES                                                    │
│       v                 v                                                        │
│   [Chat directly]   ┌──────────────────────────────┐                            │
│                     │ User already verified?        │                            │
│                     │ (check slack_user_verifications)                           │
│                     └────────────┬─────────────────┘                            │
│                                  │                                               │
│                         ┌────────┴────────┐                                     │
│                         │ NO              │ YES                                 │
│                         v                 v                                     │
│                   ┌─────────────┐    [Chat directly]                           │
│                   │ Has Slack   │                                               │
│                   │ email scope?│                                               │
│                   └──────┬──────┘                                               │
│                          │                                                      │
│                 ┌────────┴────────┐                                            │
│                 │ YES             │ NO                                         │
│                 v                 v                                            │
│           ┌───────────┐    ┌───────────────────────┐                          │
│           │ Get email │    │ Send verification     │                          │
│           │ from Slack│    │ prompt to user:       │                          │
│           │ profile   │    │ "Reply with your      │                          │
│           └─────┬─────┘    │ email to verify"      │                          │
│                 │          └───────────┬───────────┘                          │
│                 │                      │                                       │
│                 v                      v                                       │
│           ┌───────────┐         User replies with email                       │
│           │ Auto-     │                │                                       │
│           │ verify    │                v                                       │
│           │ user      │         ┌─────────────────┐                           │
│           └─────┬─────┘         │ Send 6-digit    │                           │
│                 │               │ code to email   │                           │
│                 │               └────────┬────────┘                           │
│                 │                        │                                     │
│                 │                        v                                     │
│                 │               User replies with code                        │
│                 │                        │                                     │
│                 │                        v                                     │
│                 │               ┌─────────────────┐                           │
│                 │               │ Verify code     │                           │
│                 │               │ Store session   │                           │
│                 │               └────────┬────────┘                           │
│                 │                        │                                     │
│                 └────────────────────────┴─────────────────────────────────┐  │
│                                                                             v  │
│                                                              [Chat enabled]    │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Verification Messages

**Initial prompt (no email scope):**
```
👋 Hi! Before we chat, I need to verify your email address.

Please reply with your email address to continue.
```

**Code sent:**
```
📧 I've sent a 6-digit verification code to {email}.

Reply with the code to complete verification. The code expires in 10 minutes.
```

**Verification complete:**
```
✅ Verified! You can now chat with me.

{original_message_response}
```

**Already verified (returning user):**
No interruption - chat proceeds normally.

---

## Database Schema

### New Table: `slack_link_connections`

Connects a Slack workspace to a public link. **One workspace = one link** (simple 1:1 mapping).

```sql
CREATE TABLE slack_link_connections (
    id TEXT PRIMARY KEY,                    -- UUID
    link_id TEXT NOT NULL UNIQUE,           -- FK to agent_public_links (one connection per link)
    slack_team_id TEXT NOT NULL UNIQUE,     -- Slack workspace ID (one link per workspace)
    slack_team_name TEXT,                   -- Workspace name (display)
    slack_bot_token TEXT NOT NULL,          -- Encrypted OAuth token
    connected_by TEXT NOT NULL,             -- FK to users (who set it up)
    connected_at TEXT NOT NULL,             -- ISO timestamp
    enabled INTEGER DEFAULT 1,              -- 1=active, 0=disabled

    FOREIGN KEY (link_id) REFERENCES agent_public_links(id) ON DELETE CASCADE,
    FOREIGN KEY (connected_by) REFERENCES users(id)
);

-- Simple lookup: workspace → link
CREATE INDEX idx_slack_connections_team ON slack_link_connections(slack_team_id);
CREATE INDEX idx_slack_connections_link ON slack_link_connections(link_id);
```

### New Table: `slack_user_verifications`

Tracks verified Slack users per link.

```sql
CREATE TABLE slack_user_verifications (
    id TEXT PRIMARY KEY,                    -- UUID
    link_id TEXT NOT NULL,                  -- FK to agent_public_links
    slack_user_id TEXT NOT NULL,            -- Slack user ID
    slack_team_id TEXT NOT NULL,            -- Slack workspace ID
    verified_email TEXT NOT NULL,           -- Email used for verification
    verification_method TEXT NOT NULL,      -- 'slack_profile' | 'email_code'
    verified_at TEXT NOT NULL,              -- ISO timestamp

    FOREIGN KEY (link_id) REFERENCES agent_public_links(id) ON DELETE CASCADE,
    UNIQUE(link_id, slack_user_id, slack_team_id)
);

CREATE INDEX idx_slack_verifications_user ON slack_user_verifications(slack_user_id, slack_team_id);
```

### New Table: `slack_pending_verifications`

Tracks in-progress email verifications.

```sql
CREATE TABLE slack_pending_verifications (
    id TEXT PRIMARY KEY,                    -- UUID
    link_id TEXT NOT NULL,                  -- FK to agent_public_links
    slack_user_id TEXT NOT NULL,            -- Slack user ID
    slack_team_id TEXT NOT NULL,            -- Slack workspace ID
    email TEXT,                             -- Email (NULL if awaiting_email state)
    code TEXT,                              -- 6-digit code (NULL if awaiting_email state)
    created_at TEXT NOT NULL,               -- ISO timestamp
    expires_at TEXT NOT NULL,               -- Code expiration
    state TEXT DEFAULT 'awaiting_email',    -- 'awaiting_email' | 'awaiting_code'

    FOREIGN KEY (link_id) REFERENCES agent_public_links(id) ON DELETE CASCADE
);

CREATE INDEX idx_slack_pending_user ON slack_pending_verifications(slack_user_id, slack_team_id);
```

### Reuse: `public_chat_sessions` (No Schema Change)

Slack sessions use the existing table with a new identifier type.

```sql
-- Existing identifier_type values: 'email', 'anonymous'
-- Add: 'slack'
-- session_identifier for Slack: '{slack_team_id}:{slack_user_id}'

-- Example row:
-- id: 'sess_abc123'
-- link_id: 'link_xyz'
-- session_identifier: 'T12345678:U87654321'  -- team:user
-- identifier_type: 'slack'
-- message_count: 15
-- ...
```

**Session Behavior:**

| Scenario | Behavior |
|----------|----------|
| User sends first DM | New session created |
| User sends follow-up | Same session, context preserved |
| User DMs next day | Same session continues |
| User wants fresh start | (Future) "New conversation" command |

**Context Injection:**

Same as web public chat - last 10 message pairs (20 messages) injected as context:

```
### Trinity: Public Link Access Mode

Previous conversation:
User: What's your return policy?
Assistant: Our return policy allows...
User: What about international orders?
Assistant: For international orders...

Current message:
User: And what's the timeframe?
```

**Why reuse existing table:**
- Same context injection logic (`build_public_chat_context()`)
- Same cost tracking per message
- Same session statistics
- No new infrastructure needed

---

## API Endpoints

### Public Endpoints (on PUBLIC_CHAT_URL)

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/public/slack/events` | POST | Slack signature | Receive Slack events |
| `/api/public/slack/oauth/callback` | GET | OAuth state | Complete OAuth flow |

### Authenticated Endpoints (on FRONTEND_URL / Tailscale)

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/agents/{name}/public-links/{id}/slack` | GET | JWT | Get Slack connection status |
| `/api/agents/{name}/public-links/{id}/slack/connect` | POST | JWT | Start OAuth flow |
| `/api/agents/{name}/public-links/{id}/slack` | DELETE | JWT | Disconnect Slack |

---

## API Specifications

### POST /api/public/slack/events

Receives all events from Slack Events API.

**Request Headers:**
```
X-Slack-Signature: v0=hash
X-Slack-Request-Timestamp: 1234567890
Content-Type: application/json
```

**Request Body (URL Verification):**
```json
{
  "type": "url_verification",
  "challenge": "abc123xyz"
}
```

**Response (URL Verification):**
```json
{
  "challenge": "abc123xyz"
}
```

**Request Body (Event Callback - Message):**
```json
{
  "type": "event_callback",
  "team_id": "T12345",
  "api_app_id": "A12345",
  "event": {
    "type": "app_mention",
    "user": "U12345",
    "text": "<@U_BOT_ID> Hello, I need help",
    "channel": "C12345",
    "ts": "1234567890.123456"
  },
  "event_id": "Ev12345",
  "event_time": 1234567890
}
```

**Response:**
```json
{
  "ok": true
}
```

**Error Responses:**

| Status | Reason |
|--------|--------|
| 200 | Always return 200 to Slack (errors logged internally) |
| 401 | Invalid signature (logged, return 200 to prevent retries) |

### POST /api/agents/{name}/public-links/{id}/slack/connect

Initiates Slack OAuth flow.

**Response:**
```json
{
  "oauth_url": "https://slack.com/oauth/v2/authorize?client_id=...&scope=...&redirect_uri=...&state=..."
}
```

**State Parameter:** Encrypted JSON containing:
```json
{
  "link_id": "link_abc123",
  "agent_name": "my-agent",
  "user_id": "user_xyz",
  "timestamp": 1234567890
}
```

### GET /api/public/slack/oauth/callback

Handles Slack OAuth redirect.

**Query Parameters:**
- `code`: OAuth authorization code
- `state`: Encrypted state from connect endpoint

**Success Response:** Redirect to `${FRONTEND_URL}/agents/{name}?tab=sharing&slack=connected`

**Error Response:** Redirect to `${FRONTEND_URL}/agents/{name}?tab=sharing&slack=error&reason={reason}`

### GET /api/agents/{name}/public-links/{id}/slack

Get Slack connection status.

**Response (Connected):**
```json
{
  "connected": true,
  "team_id": "T12345678",
  "team_name": "Acme Corp",
  "connected_at": "2026-02-25T10:00:00Z",
  "connected_by": "admin@example.com",
  "enabled": true
}
```

**Response (Not Connected):**
```json
{
  "connected": false
}
```

### DELETE /api/agents/{name}/public-links/{id}/slack

Disconnect Slack integration.

**Response:**
```json
{
  "disconnected": true
}
```

---

## Slack App Configuration

### Required Scopes (DM-Only)

| Scope | Reason | Required |
|-------|--------|----------|
| `im:history` | Receive DM messages | Yes |
| `chat:write` | Send responses | Yes |
| `users:read.email` | Get user email for auto-verification | Optional (better UX) |

### Event Subscriptions

| Event | Description |
|-------|-------------|
| `message.im` | User sends DM to bot |

### App Manifest Template

```yaml
display_information:
  name: Trinity Agent
  description: Chat with Trinity AI agents from Slack
  background_color: "#4A154B"

features:
  bot_user:
    display_name: Trinity Agent
    always_online: true

oauth_config:
  redirect_urls:
    - ${PUBLIC_CHAT_URL}/api/public/slack/oauth/callback
  scopes:
    bot:
      - im:history
      - chat:write
      - users:read.email  # Optional but recommended

settings:
  event_subscriptions:
    request_url: ${PUBLIC_CHAT_URL}/api/public/slack/events
    bot_events:
      - message.im
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  socket_mode_enabled: false
```

---

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SLACK_SIGNING_SECRET` | Yes* | - | Slack app signing secret for request verification |
| `SLACK_CLIENT_ID` | Yes* | - | Slack app client ID for OAuth |
| `SLACK_CLIENT_SECRET` | Yes* | - | Slack app client secret for OAuth |
| `SLACK_AUTO_VERIFY_EMAIL` | No | `true` | Use Slack profile email when available |

*Required only if Slack integration is enabled on any public link.

### Admin Settings (UI)

Add to Settings page:

```
┌─────────────────────────────────────────────────────────────┐
│ Slack Integration                                            │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│ Slack App Credentials (from api.slack.com/apps)             │
│                                                              │
│ Client ID:        [________________________]                 │
│ Client Secret:    [________________________] (encrypted)    │
│ Signing Secret:   [________________________] (encrypted)    │
│                                                              │
│ [Save]                                                       │
│                                                              │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ ℹ️ Setup Instructions                                    │ │
│ │                                                          │ │
│ │ 1. Create a Slack App at api.slack.com/apps             │ │
│ │ 2. Copy credentials above                                │ │
│ │ 3. Set Request URL to:                                   │ │
│ │    ${PUBLIC_CHAT_URL}/api/public/slack/events           │ │
│ │ 4. Add OAuth Redirect URL:                               │ │
│ │    ${PUBLIC_CHAT_URL}/api/public/slack/oauth/callback   │ │
│ │ 5. Install app to your Slack workspace                   │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Frontend UI

### PublicLinksPanel.vue Enhancement

```
┌─────────────────────────────────────────────────────────────┐
│ Public Link: "Customer Support"                              │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│ ☑ Enabled                                                    │
│ ☐ Require Email Verification                                │
│                                                              │
│ ─────────────────────────────────────────────────────────── │
│                                                              │
│ Web Access                                                   │
│ [Copy Internal Link]  [Copy External Link]                   │
│                                                              │
│ ─────────────────────────────────────────────────────────── │
│                                                              │
│ Slack Integration                           [Connect Slack]  │
│                                                              │
│ ── OR (when connected) ──────────────────────────────────── │
│                                                              │
│ Slack Integration                                            │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ 🟢 Connected to Acme Corp workspace                     │ │
│ │    Users can DM the bot to chat with this agent         │ │
│ │    Connected by admin@example.com on Feb 25, 2026       │ │
│ │                                                          │ │
│ │ ☑ Enabled  [Disconnect]                                 │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### User Experience

When Slack is connected:
1. Users in the connected workspace can find the bot in their Slack
2. They send a DM to the bot
3. Bot responds in the same DM conversation
4. If email verification required, bot prompts for email first

---

## Security Considerations

### Request Verification

Every request from Slack must be verified using the signing secret (implemented
as `adapters/transports/slack_webhook.py::_verify_signature`):

```python
import hmac
import hashlib
import time

def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: bytes,
    signature: str
) -> bool:
    # Reject requests older than 5 minutes
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False

    # Compute expected signature
    sig_basestring = f"v0:{timestamp}:{body.decode()}"
    expected = 'v0=' + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)
```

### Token Storage

- Slack bot tokens stored encrypted (AES-256-GCM) in database
- Use existing `credential_encryption.py` service
- Tokens never logged or exposed in API responses

### Rate Limiting

| Limit | Value | Scope |
|-------|-------|-------|
| Messages per user | 30/minute | Per Slack user per link |
| Verification requests | 3/10 minutes | Per Slack user |
| OAuth attempts | 5/hour | Per Trinity user |

### Audit Logging

| Event | Data Logged |
|-------|-------------|
| `slack_connected` | link_id, team_id, channel_id, connected_by |
| `slack_disconnected` | link_id, disconnected_by |
| `slack_message` | link_id, slack_user_id (not content) |
| `slack_verification` | link_id, slack_user_id, method, success |

---

## Implementation Phases

### Phase 1: Core Infrastructure (MVP) ✅
- [x] Database migrations (3 new tables)
- [x] Slack signature verification service
- [x] Event endpoint with URL verification challenge
- [x] Basic DM message → agent → response flow
- [x] Settings page for Slack app credentials
- [x] `chat.postMessage` integration for responses

### Phase 2: OAuth & Connection ✅
- [x] OAuth flow endpoints (connect/callback)
- [x] PublicLinksPanel Slack connection UI
- [x] Token encryption/storage (reuse credential_encryption.py)
- [x] Connection status API
- [x] Disconnect functionality

### Phase 3: User Authentication ✅
- [x] Verification state machine (awaiting_email → awaiting_code → verified)
- [x] Email code verification flow
- [x] Slack profile email auto-verification (if scope available)
- [x] Session persistence for Slack users (reuse public_chat_sessions)

### Phase 4: Polish (Deferred to Hardening)
- [ ] Rich message formatting (markdown → Slack mrkdwn) → See #64
- [ ] Error message templates (rate limit, agent unavailable) → See #64
- [ ] Verification message UX improvements → See #64
- [ ] Admin "test connection" button → See #64

### Phase 5: Documentation ✅
- [x] Feature flow document (`docs/memory/feature-flows/slack-integration.md`)
- [ ] Slack App setup guide for admins → Deferred
- [ ] Troubleshooting guide → Deferred

### Future (Not in Scope)
- Channel @mentions support
- Threading within DMs
- Multiple workspaces per link
- Slash commands

---

## Testing Scenarios

### Unit Tests

| Test | Description |
|------|-------------|
| `test_slack_signature_verification` | Valid/invalid/expired signatures |
| `test_url_verification_challenge` | Returns challenge for Slack URL verification |
| `test_lookup_link_by_team` | Finds correct link for workspace (team_id) |
| `test_user_verification_flow` | Email code verification state machine |
| `test_auto_verify_with_email_scope` | Auto-verifies when Slack email available |

### Integration Tests

| Test | Description |
|------|-------------|
| `test_oauth_flow_complete` | Full OAuth → token storage → redirect |
| `test_dm_to_response` | Slack DM event → agent call → chat.postMessage |
| `test_rate_limiting` | Enforces per-user rate limits |
| `test_require_email_blocks_unverified` | Unverified users get verification prompt |
| `test_session_persistence` | Multiple messages maintain context |

### Manual Tests

1. **Happy path**: Connect Slack → DM the bot → receive response
2. **Verification flow**: Enable "require email" → DM bot → enter email → enter code → chat
3. **Auto-verification**: With `users:read.email` scope, user is auto-verified
4. **Disconnect**: Remove connection, verify DMs no longer get responses
5. **Rate limit**: Send 31 messages in 1 minute, verify 31st is blocked
6. **Multi-turn**: Send follow-up question, verify agent has context

---

## Files to Create/Modify

### New Files

| File | Description |
|------|-------------|
| `src/backend/routers/slack.py` | Slack event and OAuth endpoints |
| `src/backend/services/slack_service.py` | Signature verification, chat.postMessage |
| `src/backend/db/slack.py` | Database operations for Slack tables |
| `docs/memory/feature-flows/slack-integration.md` | Feature flow documentation |
| `docs/guides/SLACK_APP_SETUP.md` | Setup guide for Slack app |

### Modified Files

| File | Changes |
|------|---------|
| `src/backend/db/schema.py` | Add 3 new tables |
| `src/backend/db/migrations.py` | Migration for Slack tables |
| `src/backend/database.py` | Delegation methods |
| `src/backend/main.py` | Mount Slack router |
| `src/backend/config.py` | Slack environment variables |
| `src/frontend/src/components/PublicLinksPanel.vue` | Slack connection UI |
| `src/frontend/src/views/Settings.vue` | Slack credentials section |
| `docs/memory/requirements.md` | Add SLACK-001 feature |
| `docs/memory/feature-flows.md` | Link to new feature flow |

---

## Related Features

- **Upstream**: Public Agent Links (15.1) - provides link infrastructure
- **Reuses**: Email verification service, rate limiting, session management
- **Related**: Agent Notifications (NOTIF-001) - potential Slack notification channel

---

## Design Decisions

1. **DMs only (Phase 1)**: Users DM the bot directly. No channel @mentions.
   - *Rationale*: Simpler UX, no channel configuration needed

2. **One workspace = one link**: Each Slack workspace connects to exactly one public link.
   - *Rationale*: Simple routing - `team_id` → `link_id` lookup
   - *If multiple agents needed*: Create separate Slack apps or use different workspaces

3. **No threading (Phase 1)**: Messages appear flat in DM conversation.
   - *Rationale*: Simpler implementation, DMs are already 1:1 conversations
   - *Future*: Could add threading for organizing multi-topic conversations

4. **Email verification for Slack users**: Same model as web public links.
   - *Auto-verify*: If `users:read.email` scope available, use Slack profile email
   - *Manual*: Otherwise, bot prompts for email and sends verification code

## Open Questions

1. **Bot naming**: Should each connected agent have a custom bot name, or use generic "Trinity Agent"?
   - *Current*: Generic bot name, agent identity shown in responses

2. **Rate limit scope**: Per Slack user, or per workspace?
   - *Current*: Per Slack user (same as web - per IP/email)

---

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-02-25 | Claude + Eugene | Initial draft |
